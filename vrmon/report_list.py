"""Index of the generated reports in reports/out, for the dashboard's
Reports panel: each labelled from its session (track, car, when, how
long), newest first, with a session's PDF attached if one was made.

Ordered by when the session happened, not when the file was written - so
rebuilding an old session's report doesn't jump it to the top.
"""

import re
import time
from pathlib import Path

from vrmon import config, store

_SESSION = re.compile(r"^session_(\d+)_report\.html$")
_SESSION_PDF = re.compile(r"^session_(\d+)_report\.pdf$")
_DEBRIEF = re.compile(r"^session(\d+)_debrief\.html$")
_COMPARE = re.compile(r"^compare_(\d+)_vs_(\d+)\.html$")
_HISTORY = re.compile(r"^settings_history\.html$")
_SERVABLE = (_SESSION, _SESSION_PDF, _DEBRIEF, _COMPARE, _HISTORY)


def report_path(name: str) -> Path:
    """The report file for a name from the browser - only names matching a
    report pattern, inside the reports folder."""
    if not any(p.match(name or "") for p in _SERVABLE):
        raise ValueError("not a report")
    path = config.REPORTS_DIR / name
    if not path.is_file():
        raise ValueError("report not found")
    return path


def _session_label(sid: int) -> tuple[str, float | None]:
    """("Fuji Speedway / Porsche 911 GT3 R - Oct 02 14:44 - 3 min", start_ts)"""
    session = store.get_session(sid)
    if session is None:
        return "", None
    settings = store.get_session_settings(sid) or {}
    parts = [" / ".join(x for x in (settings.get("track_name"), settings.get("car_name")) if x)]
    parts.append(time.strftime("%b %d %H:%M", time.localtime(session["start_ts"])))
    if session["end_ts"]:
        parts.append(f"{(session['end_ts'] - session['start_ts']) / 60:.0f} min")
    return " - ".join(p for p in parts if p), session["start_ts"]


def list_reports() -> list[dict]:
    if not config.REPORTS_DIR.exists():
        return []
    files = {f.name: f for f in config.REPORTS_DIR.iterdir() if f.is_file()}
    out = []
    for name, f in files.items():
        mtime = f.stat().st_mtime
        if m := _SESSION.match(name):
            sid = int(m.group(1))
            subtitle, when = _session_label(sid)
            pdf = f"session_{sid}_report.pdf"
            out.append({"file": name, "title": f"Session {sid}", "subtitle": subtitle,
                        "when": when or mtime, "pdf": pdf if pdf in files else None})
        elif m := _DEBRIEF.match(name):
            sid = int(m.group(1))
            subtitle, when = _session_label(sid)
            out.append({"file": name, "title": f"Session {sid} debrief", "subtitle": subtitle,
                        "when": (when or mtime) + 1, "pdf": None})  # +1: just above its session's report
        elif m := _COMPARE.match(name):
            a, b = int(m.group(1)), int(m.group(2))
            later = max(filter(None, (_session_label(a)[1], _session_label(b)[1])), default=mtime)
            out.append({"file": name, "title": f"Compare sessions {a} vs {b}", "subtitle": "side-by-side comparison",
                        "when": later + 2, "pdf": None})
        elif _HISTORY.match(name):
            out.append({"file": name, "title": "Settings history", "subtitle": "every session's graphics settings in one table",
                        "when": mtime, "pdf": None})
    out.sort(key=lambda r: r["when"], reverse=True)
    return out
