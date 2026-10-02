"""Snapshots the heaviest non-iRacing processes each tick.

The main process.py collector only ever tracks iRacing's own process -
so "something else on the system spiked right when the hitch happened"
was previously invisible. This fills that gap: top-N by CPU each tick,
plus specific processes worth tracking regardless of whether they made
the top-N ("watch_processes" in data/settings.json).
"""

import logging

import psutil

from vrmon.config import WATCH_PROCESS_SUBSTRINGS

log = logging.getLogger(__name__)

_TOP_N = 8


class TopProcessCollector:
    def __init__(self):
        self._procs: dict[int, psutil.Process] = {}

    def _refresh_proc_list(self) -> None:
        seen = set()
        for p in psutil.process_iter(["pid"]):
            if p.pid == 0:
                # "System Idle Process" - psutil reports a nonsensical
                # >1000% cpu_percent for this one (it's an artifact of
                # how idle time inverts to a percentage, not real
                # competing load), so it's excluded outright rather than
                # dominating the top-N with a meaningless number.
                continue
            seen.add(p.pid)
            if p.pid not in self._procs:
                try:
                    proc = psutil.Process(p.pid)
                    proc.cpu_percent(None)  # prime - the first call after this is meaningless
                    self._procs[p.pid] = proc
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        for pid in list(self._procs):
            if pid not in seen:
                del self._procs[pid]

    def sample(self) -> list[dict]:
        """Non-blocking - cpu_percent(None) reports the delta since this
        process was last sampled, using the collector loop's own natural
        cadence as the measurement window (psutil's recommended pattern
        for continuous per-process monitoring)."""
        self._refresh_proc_list()

        rows = []
        for pid, proc in list(self._procs.items()):
            try:
                cpu = proc.cpu_percent(None)
                rss_mb = proc.memory_info().rss / (1024 * 1024)
                name = proc.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            rows.append({"pid": pid, "name": name, "cpu_pct": cpu, "rss_mb": rss_mb})

        rows.sort(key=lambda r: r["cpu_pct"], reverse=True)
        top = rows[:_TOP_N]

        top_pids = {r["pid"] for r in top}
        for r in rows:
            if r["pid"] in top_pids:
                continue
            if any(sub in r["name"].lower() for sub in WATCH_PROCESS_SUBSTRINGS):
                top.append(r)

        return top
