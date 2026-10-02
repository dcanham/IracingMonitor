"""Recent notable events from every log this app knows about, merged into
one newest-first feed - for the dashboard's "Recent events" panel - plus
the plain-English descriptions of stored Windows events that the reports
use too.

Sources:
- Windows events vrmon has recorded (crashes, blue screens, GPU driver
  resets, hardware errors, unexpected shutdowns) - from the database.
- iRacing's launcher log (Documents\\iRacing\\logs\\main-ui.log, JSON
  lines): warnings and errors, minus known harmless noise.
- iRacing's anti-cheat log (eos_anticheat_errors.txt).
- Sim crash dumps written to Documents\\iRacing\\sentry. (The sim itself
  crashing is also caught via Windows' Application Error event.)
- vrmon's own session start/end, for context.

The iRacing files are read on demand rather than tailed by a collector -
they're small and only change occasionally.
"""

import datetime as dt
import json
import logging
import re
import subprocess
import time

from vrmon import config, store
from vrmon.collectors.windows_events import GPU_DRIVER_PROVIDERS

log = logging.getLogger(__name__)

_IRACING_LOGS = config.IRACING_DOCS_DIR / "logs"
_SENTRY_DIR = config.IRACING_DOCS_DIR / "sentry"

# Launcher errors that appear constantly on a healthy install.
_LAUNCHER_NOISE = ("telemetry_status.json", "Requested telemetry directory")

_ANTICHEAT_HEADER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):\s*$")


def describe_event(e) -> str:
    """Plain-English label for a stored Windows event."""
    provider, event_id, msg = e["provider"], str(e["event_id"]), e["message"] or ""
    if provider == "Display" and event_id == "4101":
        return "GPU driver reset (TDR) - the driver stopped responding and recovered"
    if provider == "Microsoft-Windows-WER-SystemErrorReporting":
        m = re.search(r"bugcheck was: (0x[0-9a-fA-F]+)", msg)
        return f"Blue screen (bugcheck {m.group(1) if m else 'code unknown'})"
    if provider == "Microsoft-Windows-Kernel-Power":
        return "Unclean reboot - crash, freeze or power loss"
    if provider == "EventLog" and event_id == "6008":
        return "Unexpected shutdown"
    if provider == "Microsoft-Windows-WHEA-Logger":
        return "Hardware error (WHEA)"
    if provider == "Application Error":
        return "iRacing crashed"
    if provider == "Application Hang":
        return "iRacing stopped responding"
    if provider == "Windows Error Reporting":
        name = re.search(r"Event Name: (\S+)", msg)
        name = name.group(1) if name else "unknown"
        if name == "LiveKernelEvent":
            code = re.search(r"P1: (\w+)", msg)
            return f"GPU/kernel hang recovered by Windows (LiveKernelEvent {code.group(1) if code else '?'})"
        return f"Crash report ({name})"
    if provider in GPU_DRIVER_PROVIDERS:
        return f"GPU driver error ({provider} event {event_id})"
    return f"{provider} event {event_id}"


def _windows(since: float) -> list[dict]:
    out = []
    for r in store.query_range("windows_events", since, time.time() + 60):
        level = (r["level"] or "").lower()
        out.append({
            "ts": r["ts"], "source": "Windows",
            "level": "error" if level in ("critical", "error") else "warning" if level == "warning" else "info",
            "label": describe_event(r), "detail": (r["message"] or "")[:300],
            "ref": {"kind": "windows", "key": r["record_key"]} if r["record_key"] else None,
        })
    return out


def _launcher(since: float) -> list[dict]:
    """main-ui.log plus its newest rotated copy (it rotates daily)."""
    out = []
    for path in (_IRACING_LOGS / "main-ui.log.0", _IRACING_LOGS / "main-ui.log"):
        try:
            if not path.exists() or path.stat().st_mtime < since:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines):
            try:
                rec = json.loads(line)
                ts = dt.datetime.fromisoformat(rec["time"].replace("Z", "+00:00")).timestamp()
            except (ValueError, KeyError, TypeError):
                continue
            level = rec.get("level", 30)
            msg = str(rec.get("msg", ""))
            err = rec.get("err") if isinstance(rec.get("err"), dict) else {}
            if ts < since or level < 40 or any(n in msg + str(err.get("message", "")) for n in _LAUNCHER_NOISE):
                continue
            # Messages relayed from the launcher's web page are JSON themselves.
            try:
                msg = json.loads(msg).get("msg", msg)
            except (ValueError, AttributeError):
                pass
            out.append({
                "ts": ts, "source": "iRacing launcher", "level": "error" if level >= 50 else "warning",
                "label": msg[:160], "detail": str(err.get("message", ""))[:300],
                "ref": {"kind": "file", "file": path.name, "line": index},
            })
    return out


def _anticheat(since: float) -> list[dict]:
    path = _IRACING_LOGS / "eos_anticheat_errors.txt"
    try:
        if not path.exists() or path.stat().st_mtime < since:
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out, current = [], None
    for index, line in enumerate(lines):
        m = _ANTICHEAT_HEADER_RE.match(line.strip())
        if m:
            current = {"ts": dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp(),
                       "source": "Anti-cheat", "level": "warning", "label": "", "detail": "",
                       "ref": {"kind": "file", "file": path.name, "line": index}}
            out.append(current)
        elif current is not None and line.strip():
            text = line.strip()
            if text.lower().startswith("error"):
                current["level"] = "error"
            current["label"] = (current["label"] + " " + text).strip()[:300]
    for e in out:
        e["label"], e["detail"] = e["label"][:160], e["label"]
    return [e for e in out if e["ts"] >= since and e["label"]]


def _sim_crash_reports(since: float) -> list[dict]:
    """Only actual crash dumps: the sim's crash reporter (Sentry) also keeps
    ordinary session/lock files in this folder while it runs and deletes
    them on a normal exit, so "any file" would flag every session."""
    out = []
    try:
        for f in _SENTRY_DIR.rglob("*"):
            if f.is_file() and f.suffix.lower() in (".dmp", ".mdmp") and f.stat().st_mtime >= since:
                out.append({"ts": f.stat().st_mtime, "source": "iRacing", "level": "error",
                            "label": "iRacing wrote a crash report", "detail": str(f),
                            "ref": {"kind": "dump", "file": str(f.relative_to(_SENTRY_DIR))}})
    except OSError:
        pass
    return out


def _sessions(since: float) -> list[dict]:
    out = []
    for s in store.list_sessions():
        if s["start_ts"] >= since:
            out.append({"ts": s["start_ts"], "source": "vrmon", "level": "info",
                        "label": f"Recording started - session {s['id']}", "detail": ""})
        if s["end_ts"] and s["end_ts"] >= since:
            minutes = (s["end_ts"] - s["start_ts"]) / 60
            out.append({"ts": s["end_ts"], "source": "vrmon", "level": "info",
                        "label": f"Recording stopped - session {s['id']} ({minutes:.0f} min)", "detail": ""})
    return out


# --- details / open-the-source, for clicking an entry in the dashboard -------
#
# The browser only ever sends back the event's own reference (a record key,
# or a log file's *name*); paths are resolved here against the known
# folders, so nothing outside them can be read or opened.

_LOG_NAME_RE = re.compile(r"^(main-ui\.log(\.\d+)?|eos_anticheat_errors\.txt)$")
_CONTEXT_LINES = 40


def _log_path(name: str):
    if not _LOG_NAME_RE.match(name or ""):
        raise ValueError("not a known iRacing log file")
    return _IRACING_LOGS / name


def _dump_path(rel: str):
    path = (_SENTRY_DIR / rel).resolve()
    if _SENTRY_DIR.resolve() not in path.parents or path.suffix.lower() not in (".dmp", ".mdmp"):
        raise ValueError("not a crash dump in iRacing's sentry folder")
    return path


def _windows_log_name(record_key: str) -> str:
    # "System|1729", "Application|..." - or "WER|<report id>", which is Application.
    log_name = (record_key or "").split("|", 1)[0]
    return "Application" if log_name == "WER" else log_name


def event_detail(kind: str, key: str = "", file: str = "", line: int = 0) -> dict:
    """Full text behind a feed entry: the whole Windows event, or the log
    file around the entry's line (with that line's index to highlight)."""
    if kind == "windows":
        r = store.get_windows_event(key)
        if r is None:
            raise ValueError("event not found")
        when = dt.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "title": describe_event(r),
            "meta": f"{when} - Windows {_windows_log_name(key)} log - {r['provider']} event {r['event_id']} ({r['level']})",
            "lines": (r["message"] or "").split(". "),
            "highlight": None,
            "open_label": f"Open Event Viewer ({_windows_log_name(key)} log)",
        }
    if kind == "file":
        path = _log_path(file)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, line - _CONTEXT_LINES)
        return {
            "title": path.name,
            "meta": f"{path} - lines {start + 1}-{min(len(lines), line + _CONTEXT_LINES + 1)} of {len(lines)}",
            "lines": lines[start:line + _CONTEXT_LINES + 1],
            "highlight": line - start,
            "open_label": "Open log file in Notepad",
        }
    if kind == "dump":
        path = _dump_path(file)
        when = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "title": "iRacing crash dump",
            "meta": f"{when} - {path} ({path.stat().st_size / 1024:.0f} KB)",
            "lines": ["A crash dump is a binary snapshot of the sim at the moment it crashed - it isn't",
                      "readable as text. iRacing support can use it if you report the crash."],
            "highlight": None,
            "open_label": "Show crash dump in Explorer",
        }
    raise ValueError("unknown event kind")


def open_event_source(kind: str, key: str = "", file: str = "") -> None:
    """Opens the entry's source on this PC: Event Viewer at the right log,
    the log file in Notepad, or Explorer with the crash dump selected."""
    if kind == "windows":
        subprocess.Popen(["eventvwr.exe", f"/c:{_windows_log_name(key)}"])
    elif kind == "file":
        subprocess.Popen(["notepad.exe", str(_log_path(file))])
    elif kind == "dump":
        subprocess.Popen(["explorer.exe", f"/select,{_dump_path(file)}"])
    else:
        raise ValueError("unknown event kind")


def recent_events(hours: float = 48, limit: int = 200) -> list[dict]:
    since = time.time() - hours * 3600
    events = []
    for source in (_windows, _launcher, _anticheat, _sim_crash_reports, _sessions):
        try:
            events.extend(source(since))
        except Exception:
            log.debug("recent events source %s failed", source.__name__, exc_info=True)
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:limit]
