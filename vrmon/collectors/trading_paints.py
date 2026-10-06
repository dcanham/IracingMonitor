"""Trading Paints - the paint downloader most iRacers run alongside the sim.

During a session it downloads other drivers' paints into iRacing's paint
folder as they join, and the sim then loads each new texture - so a
download landing mid-lap is a candidate for a hitch. This records:

- The Trading Paints process: running or not, CPU (% of one core, like
  process.py), memory, disk read/write rate. A stop or restart while the
  sim is running is logged as an event.
- Every file it writes to, replaces or deletes in Documents\\iRacing\\paint
  (the paints themselves), with size.
- Its log (output.txt). Trading Paints rewrites it from scratch each
  session, so a copy is kept each time it changes, and any line that
  looks like an error is logged as an event. Its lines carry no times of
  their own, so an error's time is when the log was written, not
  necessarily the moment it happened.

Its settings file (config.xml) is never read - it holds the login.
"""

import logging
import os
import re
import time
from pathlib import Path

import psutil

from vrmon import config

log = logging.getLogger(__name__)

# "Trading Paints.exe", or a beta build like "Trading Paints B12.exe".
PROCESS_PREFIX = "trading paints"
PAINT_DIR = config.IRACING_DOCS_DIR / "paint"
LOG_PATH = Path(os.environ.get("APPDATA", "")) / "TradingPaints Downloader" / "output.txt"

_ERROR_RE = re.compile(r"error|exception|fail|could not|couldn't|unable|timed? ?out|denied", re.I)

# Scanning the paint folder is a directory walk - fine every couple of
# seconds even with thousands of paints, not every tick.
_PAINT_SCAN_INTERVAL_S = 2.0

# Paint writes (or deletes) less than this far apart count as one burst -
# Trading Paints fetches a whole session's paints within a few seconds.
_BURST_GAP_S = 60.0
_PROBLEM_KINDS = ("stopped", "restarted", "log_error")
_MAX_PROBLEMS = 10


class _Burst:
    """A run of paint writes or deletes close together - "the last
    download" / "the last cleanup" on the dashboard."""

    def __init__(self, ts: float):
        self.start = self.last = ts
        self.files: dict[str, int] = {}  # path -> size (latest, if rewritten)

    def as_dict(self) -> dict:
        return {"ts": self.start, "end_ts": self.last, "count": len(self.files),
                "bytes": sum(self.files.values())}


class TradingPaintsCollector:
    def __init__(self):
        self._proc: psutil.Process | None = None
        self._last_io = None  # (monotonic time, read bytes, write bytes)
        self._was_running: bool | None = None
        self._last_pid: int | None = None
        self._process_name: str | None = None
        self._paint_files: dict[str, tuple[float, int]] | None = None
        self._last_paint_scan = 0.0
        self._log_mtime: float | None = None
        self._download: _Burst | None = None
        self._cleanup: _Burst | None = None
        self._problems: list[dict] = []

    def seed(self, rows) -> None:
        """Replays stored tp_events (oldest first) so the dashboard has the
        last download/cleanup/errors straight after vrmon starts."""
        for r in rows:
            self._track({"kind": r["kind"], "detail": r["detail"], "size": r["size"]}, r["ts"])

    def _track(self, e: dict, ts: float) -> None:
        kind = e["kind"]
        if kind in ("paint_added", "paint_updated", "paint_deleted"):
            attr = "_cleanup" if kind == "paint_deleted" else "_download"
            burst = getattr(self, attr)
            if burst is None or ts - burst.last > _BURST_GAP_S:
                burst = _Burst(ts)
                setattr(self, attr, burst)
            burst.last = ts
            burst.files[e["detail"]] = e.get("size") or 0
        elif kind in _PROBLEM_KINDS:
            self._problems = ([{"ts": ts, "kind": kind, "detail": e["detail"]}] + self._problems)[:_MAX_PROBLEMS]

    def status(self) -> dict:
        files = self._paint_files or {}
        return {
            "process_name": self._process_name,
            "last_download": self._download.as_dict() if self._download else None,
            "last_cleanup": self._cleanup.as_dict() if self._cleanup else None,
            "paint_folder": {"count": len(files), "bytes": sum(size for _mtime, size in files.values())}
                            if self._paint_files is not None else None,
            "problems": self._problems,
        }

    def _find_process(self) -> psutil.Process | None:
        if self._proc is not None:
            try:
                if self._proc.is_running():
                    return self._proc
            except psutil.Error:
                pass
            self._proc = None
        for proc in psutil.process_iter(["name"]):
            name = (proc.info["name"] or "").lower()
            if name.startswith(PROCESS_PREFIX) and name.endswith(".exe"):
                self._proc = proc
                self._process_name = proc.info["name"][:-4]
                self._last_io = None
                try:
                    proc.cpu_percent()  # prime - the first call always returns 0
                except psutil.Error:
                    pass
                return proc
        return None

    def sample(self) -> dict:
        """Process stats. Also returns "events" - start/stop/restart, new
        paint files, log errors - for the caller to store."""
        events: list[dict] = []
        proc = self._find_process()
        out = {"running": proc is not None, "pid": None, "cpu_pct": None, "rss_mb": None,
               "read_bps": None, "write_bps": None, "threads": None}
        if proc is not None:
            try:
                with proc.oneshot():
                    out["pid"] = proc.pid
                    out["cpu_pct"] = proc.cpu_percent()
                    out["rss_mb"] = proc.memory_info().rss / 1e6
                    out["threads"] = proc.num_threads()
                    io = proc.io_counters()
                now = time.monotonic()
                if self._last_io is not None:
                    dt = now - self._last_io[0]
                    if dt > 0:
                        out["read_bps"] = max(0.0, (io.read_bytes - self._last_io[1]) / dt)
                        out["write_bps"] = max(0.0, (io.write_bytes - self._last_io[2]) / dt)
                self._last_io = (now, io.read_bytes, io.write_bytes)
            except psutil.Error:
                log.debug("Trading Paints process read failed", exc_info=True)

        if self._was_running is not None:
            if self._was_running and not out["running"]:
                events.append({"kind": "stopped", "detail": f"Trading Paints closed (pid {self._last_pid})"})
            elif not self._was_running and out["running"]:
                events.append({"kind": "started", "detail": f"Trading Paints started (pid {out['pid']})"})
            elif out["running"] and self._last_pid and out["pid"] != self._last_pid:
                events.append({"kind": "restarted", "detail": f"Trading Paints restarted (pid {self._last_pid} -> {out['pid']})"})
        self._was_running = out["running"]
        if out["pid"]:
            self._last_pid = out["pid"]

        now = time.monotonic()
        if now - self._last_paint_scan >= _PAINT_SCAN_INTERVAL_S:
            self._last_paint_scan = now
            events += self._scan_paints()
            log_event, log_text = self._check_log()
            events += log_event
            if log_text is not None:
                out["log_text"] = log_text

        ts = time.time()
        for e in events:
            self._track(e, ts)
        out["events"] = events
        out["status"] = self.status()
        return out

    def _scan_paints(self) -> list[dict]:
        current: dict[str, tuple[float, int]] = {}

        # os.scandir rather than os.walk + os.stat: on Windows the listing
        # already carries each file's size and time, so this stays cheap
        # (~20ms for 15,000 paints, vs ~120ms) for people who keep every
        # paint they've ever downloaded.
        def scan(path: str, prefix: str) -> None:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            scan(entry.path, prefix + entry.name + os.sep)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            current[prefix + entry.name] = (st.st_mtime, st.st_size)
                    except OSError:
                        continue  # deleted or still being written mid-scan

        try:
            scan(str(PAINT_DIR), "")
        except FileNotFoundError:
            pass  # no paint folder yet - nothing downloaded
        except OSError:
            log.debug("paint folder scan failed", exc_info=True)
            return []

        events = []
        # The first scan is the baseline - what was already there.
        if self._paint_files is not None:
            for rel, (mtime, size) in current.items():
                old = self._paint_files.get(rel)
                if old is None:
                    events.append({"kind": "paint_added", "detail": rel, "size": size})
                elif old != (mtime, size):
                    events.append({"kind": "paint_updated", "detail": rel, "size": size})
            for rel in self._paint_files.keys() - current.keys():
                # Size as last seen - how much space the cleanup freed.
                events.append({"kind": "paint_deleted", "detail": rel, "size": self._paint_files[rel][1]})
        self._paint_files = current
        return events

    def _check_log(self) -> tuple[list[dict], str | None]:
        try:
            mtime = LOG_PATH.stat().st_mtime
        except OSError:
            return [], None
        if mtime == self._log_mtime:
            return [], None
        first = self._log_mtime is None
        self._log_mtime = mtime
        try:
            text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return [], None
        events = []
        if not first:
            # The YAML block is the sim's session info echoed back - only
            # look at Trading Paints' own lines after it.
            own = text.split("\n...", 1)[-1]
            for line in own.splitlines():
                line = line.strip()
                # .NET stack trace lines belong to the exception line above.
                if line.startswith(("at ", "---")):
                    continue
                if _ERROR_RE.search(line):
                    events.append({"kind": "log_error", "detail": line[:500]})
        return events, text
