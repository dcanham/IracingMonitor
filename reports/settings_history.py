"""Builds a single HTML page comparing graphics/streaming settings AND
frame rate across every recorded session - for the "change one thing,
record, compare" workflow. Only settings that actually differ between at
least two sessions are shown (out of ~230 total iRacing renderer keys
alone), so a real change doesn't get lost in unchanged noise.

Usage:
    python -m reports.settings_history
"""

import html
import json
import statistics
import time
import webbrowser

from vrmon import config, settings_reader, store


def _hashable(value):
    """Some setting values are lists (e.g. VD's DontWarnApps) - make them
    usable as set/dict members for the "did this change" check below."""
    return json.dumps(value, sort_keys=True) if isinstance(value, (list, dict)) else value


def _session_fps(session_id: int, start_ts: float, end_ts: float) -> dict:
    rows = store.query_range("iracing_samples", start_ts, end_ts)
    values = [r["frame_rate"] for r in rows if r["frame_rate"] is not None]
    if not values:
        return {"median": None, "min": None}
    return {"median": statistics.median(values), "min": min(values)}


def build_history():
    sessions = list(reversed(store.list_sessions()))  # chronological
    columns = []
    flat_per_session = {}

    for s in sessions:
        settings = store.get_session_settings(s["id"])
        if settings is None:
            continue
        end_ts = s["end_ts"] or time.time()
        fps = _session_fps(s["id"], s["start_ts"], end_ts)
        columns.append(
            {
                "id": s["id"],
                "start": time.strftime("%Y-%m-%d %H:%M", time.localtime(s["start_ts"])),
                "median_fps": fps["median"],
                "min_fps": fps["min"],
            }
        )
        flat_per_session[s["id"]] = settings_reader.flatten_session_settings(settings)

    all_keys = sorted({k for flat in flat_per_session.values() for k in flat})
    changing_keys = [
        k
        for k in all_keys
        if len({_hashable(flat_per_session[c["id"]].get(k)) for c in columns}) > 1
    ]

    rows = []
    for k in changing_keys:
        values = [flat_per_session[c["id"]].get(k) for c in columns]
        rows.append((k, values))

    out_dir = config.REPORTS_DIR
    out_dir.mkdir(exist_ok=True, parents=True)
    out_path = out_dir / "settings_history.html"
    out_path.write_text(_render_html(columns, rows), encoding="utf-8")
    return out_path


def _fmt_fps(v):
    return "-" if v is None else f"{v:.0f}"


def _render_html(columns, rows) -> str:
    if not columns:
        body = "<p class='muted'>No sessions with a captured settings snapshot yet. Run the iRacing sim at least once while vrmon is running first.</p>"
    else:
        header = "".join(f"<th>#{c['id']}<br><span class='muted'>{c['start']}</span></th>" for c in columns)
        fps_row = "".join(f"<td>{_fmt_fps(c['median_fps'])} <span class='muted'>(min {_fmt_fps(c['min_fps'])})</span></td>" for c in columns)

        if not rows:
            body_rows = "<tr><td colspan='99' class='muted'>No settings differed across these sessions.</td></tr>"
        else:
            body_rows = ""
            for key, values in rows:
                cells = ""
                for i, v in enumerate(values):
                    changed = i > 0 and v != values[i - 1]
                    cls = " class='changed'" if changed else ""
                    cells += f"<td{cls}>{html.escape('-' if v is None else str(v))}</td>"
                body_rows += f"<tr><td class='key'>{html.escape(key)}</td>{cells}</tr>"

        body = f"""
        <table>
        <thead><tr><th>Setting</th>{header}</tr></thead>
        <tbody>
        <tr><td class="key">Frame rate (median / min)</td>{fps_row}</tr>
        {body_rows}
        </tbody>
        </table>
        """

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>iRacing Monitor - Settings History</title>
<style>
:root {{ color-scheme: dark; }}
body {{ background:#0f1115; color:#e6e8ec; font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding:24px; }}
h1 {{ font-size:1.3rem; margin:0 0 4px; }}
.muted {{ color:#8b93a3; font-size:0.8rem; }}
table {{ border-collapse:collapse; font-size:0.82rem; margin-top:16px; }}
th, td {{ padding:6px 10px; border-bottom:1px solid #262b36; text-align:left; white-space:nowrap; }}
th {{ color:#8b93a3; font-weight:600; position:sticky; top:0; background:#0f1115; }}
td.key {{ color:#8b93a3; position:sticky; left:0; background:#0f1115; }}
td.changed {{ background:rgba(224,169,45,0.18); color:#e0a92d; font-weight:600; }}
</style>
</head>
<body>
<h1>Settings history across recorded sessions</h1>
<p class="muted">Only settings that changed at least once are shown. Highlighted cells changed from the previous session in the row.</p>
{body}
</body>
</html>
"""


def main():
    out_path = build_history()
    print(f"Settings history written to {out_path}")
    webbrowser.open(out_path.as_uri())


if __name__ == "__main__":
    main()
