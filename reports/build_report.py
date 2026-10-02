"""Builds a self-contained HTML report for one logged session, or a
side-by-side comparison report for two.

Usage:
    python -m reports.build_report                    # most recent session
    python -m reports.build_report <session_id>        # one specific session
    python -m reports.build_report <session_id_a> <session_id_b>  # compare two
"""

import html
import json
import statistics
import sys
import time
import webbrowser
from pathlib import Path

from vrmon import config, settings_reader, store
from vrmon.events import describe_event
from reports.analysis import build_buckets, tag_buckets

# A hard crash's own event-log records are only written as Windows boots
# back up, after the session's last sample - so events shortly after a
# session ends still belong to it.
EVENT_GRACE_S = 15 * 60


def session_windows_events(start_ts: float, end_ts: float) -> list[dict]:
    rows = store.query_range("windows_events", start_ts, end_ts + EVENT_GRACE_S)
    return [
        {
            "ts": r["ts"],
            "after_end": r["ts"] > end_ts,
            "label": describe_event(r),
            "level": r["level"],
            "message": r["message"] or "",
        }
        for r in rows
    ]

OUT_DIR = config.REPORTS_DIR
CHART_JS_PATH = config.WEB_DIR / "chart.umd.min.js"

TAG_LABELS = {
    "encoder-bound": "GPU video encoder busy",
    "cpu-core-saturated": "One CPU core saturated",
    "gpu-bound": "GPU busy",
    "gpu-power-thermal-limited": "GPU power/thermal limited",
    "gpu-load-not-from-iracing": "GPU load not from iRacing",
    "possible-core-scheduling-stall": "Possible P/E-core scheduling stall",
    "unexplained-fps-dip": "Unexplained FPS dip",
}

BASE_CSS = """
:root { color-scheme: dark; }
body { background:#0f1115; color:#e6e8ec; font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding:24px; }
h1 { font-size:1.3rem; margin:0 0 4px; }
.muted { color:#8b93a3; }
.overview { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; margin:20px 0; }
.stat { background:#171a21; border:1px solid #262b36; border-radius:10px; padding:10px 14px; }
.stat .num { font-size:1.4rem; font-weight:600; }
.stat .label { font-size:0.75rem; color:#8b93a3; text-transform:uppercase; letter-spacing:.05em; }
.chart-wrap { background:#171a21; border:1px solid #262b36; border-radius:10px; padding:14px; margin-bottom:16px; }
.chart-wrap h2 { font-size:0.85rem; color:#8b93a3; text-transform:uppercase; letter-spacing:.05em; margin:0 0 8px; }
canvas { max-height:220px; }
.window { display:flex; gap:14px; padding:10px 0; border-bottom:1px solid #262b36; }
.window-time { flex-shrink:0; width:60px; font-variant-numeric:tabular-nums; color:#4da3ff; font-weight:600; }
.window-tags { display:flex; flex-direction:column; gap:4px; }
.tag { display:flex; gap:8px; font-size:0.85rem; }
.tag-name { font-weight:600; flex-shrink:0; min-width:260px; }
.tag-detail { color:#8b93a3; }
table.cmp { width:100%; border-collapse:collapse; font-size:0.85rem; }
table.cmp th, table.cmp td { padding:6px 10px; border-bottom:1px solid #262b36; text-align:right; }
table.cmp th:first-child, table.cmp td:first-child { text-align:left; color:#8b93a3; }
table.cmp th { color:#8b93a3; font-weight:600; font-size:0.78rem; text-transform:uppercase; letter-spacing:.04em; }
td.better { color:#35c07a; font-weight:600; }
td.worse { color:#e0503b; font-weight:600; }
td.key { color:#8b93a3; }
td.changed { background:rgba(224,169,45,0.18); color:#e0a92d; font-weight:600; }
a.top-link { color:#4da3ff; text-decoration:none; font-size:0.85rem; }
"""


# --- data gathering ----------------------------------------------------


def _resolve_session(session_id: int | None) -> dict:
    if session_id is not None:
        session = store.get_session(session_id)
        if session is None:
            raise SystemExit(f"no such session: {session_id}")
        return session
    sessions = store.list_sessions()
    if not sessions:
        raise SystemExit("no sessions recorded yet - run `python -m vrmon` while iRacing is running first")
    return sessions[0]


def _avg(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def analyze_session(session_id: int | None) -> dict:
    """Everything needed to render this session, either standalone or as
    one side of a comparison."""
    session = _resolve_session(session_id)
    sid = session["id"]
    start_ts = session["start_ts"]
    end_ts = session["end_ts"] or time.time()

    system_rows = store.query_range("system_samples", start_ts, end_ts)
    gpu_rows = store.query_range("gpu_samples", start_ts, end_ts)
    iracing_rows = store.query_range("iracing_samples", start_ts, end_ts)

    buckets = build_buckets(start_ts, end_ts, system_rows, gpu_rows, iracing_rows)
    median_fps = tag_buckets(buckets)
    anomaly_buckets = [b for b in buckets if b.tags]

    fps_values = [b.frame_rate_avg for b in buckets if b.frame_rate_avg is not None]

    # "on track" excludes garage/loading/menu time (where FPS is
    # meaningless, e.g. the ubiquitous "Min FPS: 0" from the load-in
    # screen) so cross-session FPS comparisons reflect actual driving.
    on_track_buckets = [b for b in buckets if b.on_track]
    on_track_fps = [b.frame_rate_avg for b in on_track_buckets if b.frame_rate_avg is not None]

    throttled_buckets = [b for b in buckets if b.gpu_throttled]
    throttle_reasons_seen = sorted({r for b in throttled_buckets for r in b.gpu_throttle_reasons})

    settings = store.get_session_settings(sid)
    flat_settings = settings_reader.flatten_session_settings(settings) if settings else {}
    windows_events = session_windows_events(start_ts, end_ts)

    stats = {
        "session_id": sid,
        "start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ts)),
        "duration_min": (end_ts - start_ts) / 60,
        "track_name": settings.get("track_name") if settings else None,
        "track_config": settings.get("track_config") if settings else None,
        "car_name": settings.get("car_name") if settings else None,
        "avg_fps": _avg(fps_values),
        "median_fps": median_fps,
        "min_fps": min((b.frame_rate_min for b in buckets if b.frame_rate_min is not None), default=None),
        "avg_fps_on_track": _avg(on_track_fps),
        "min_fps_on_track": min((b.frame_rate_min for b in on_track_buckets if b.frame_rate_min is not None), default=None),
        "avg_cpu_total": _avg([b.cpu_total_pct for b in buckets]),
        "avg_cpu_max_core": _avg([b.cpu_max_core_pct for b in on_track_buckets]),
        "avg_cpu_p": _avg([b.cpu_p_pct for b in buckets]),
        "avg_cpu_e": _avg([b.cpu_e_pct for b in buckets]),
        "peak_cpu_e": max((b.cpu_e_pct for b in buckets if b.cpu_e_pct is not None), default=None),
        "avg_gpu_util": _avg([b.gpu_util_pct for b in buckets]),
        "max_gpu_util": max((b.gpu_util_pct for b in buckets if b.gpu_util_pct is not None), default=None),
        "avg_encoder_util": _avg([b.encoder_util_pct for b in buckets]),
        "max_encoder_util": max((b.encoder_util_pct for b in buckets if b.encoder_util_pct is not None), default=None),
        "avg_net_mbps": _avg([b.net_sent_mbps for b in buckets]),
        "max_net_mbps": max((b.net_sent_mbps for b in buckets if b.net_sent_mbps is not None), default=None),
        "avg_iracing_gpu_share": _avg(
            [b.iracing_gpu_util_pct / b.gpu_util_pct * 100 for b in buckets if b.gpu_util_pct and b.iracing_gpu_util_pct is not None and b.gpu_util_pct > 0]
        ),
        "throttled_seconds": len(throttled_buckets),
        "throttle_reasons_seen": throttle_reasons_seen,
        "anomaly_count": len(anomaly_buckets),
        "windows_event_count": len(windows_events),
    }

    chart_data = {
        "labels": [b.t for b in buckets],
        "cpu_total": [b.cpu_total_pct for b in buckets],
        "cpu_max_core": [b.cpu_max_core_pct for b in buckets],
        "cpu_p": [b.cpu_p_pct for b in buckets],
        "cpu_e": [b.cpu_e_pct for b in buckets],
        "gpu_util": [b.gpu_util_pct for b in buckets],
        "iracing_gpu_util": [b.iracing_gpu_util_pct for b in buckets],
        "encoder_util": [b.encoder_util_pct for b in buckets],
        "net_sent_mbps": [b.net_sent_mbps for b in buckets],
        "frame_rate": [b.frame_rate_avg for b in buckets],
        "on_track": [b.on_track for b in buckets],
    }

    return {
        "stats": stats,
        "chart_data": chart_data,
        "anomaly_buckets": anomaly_buckets,
        "windows_events": windows_events,
        "flat_settings": flat_settings,
        "presentmon": store.get_presentmon_summary(sid),
        "dpcisr": store.get_dpcisr_summary(sid),
    }


# --- single-session report ----------------------------------------------


def build_report(session_id: int | None = None) -> Path:
    data = analyze_session(session_id)
    sid = data["stats"]["session_id"]

    prev_sid = store.previous_session_id(sid)
    prev_settings = {}
    if prev_sid is not None:
        prev_raw = store.get_session_settings(prev_sid)
        prev_settings = settings_reader.flatten_session_settings(prev_raw) if prev_raw else {}
    settings_changes = settings_reader.diff_settings(prev_settings, data["flat_settings"]) if (prev_sid is not None and prev_settings) else []

    OUT_DIR.mkdir(exist_ok=True, parents=True)
    out_path = OUT_DIR / f"session_{sid}_report.html"
    out_path.write_text(
        _render_single_html(data, settings_changes, prev_sid), encoding="utf-8"
    )
    return out_path


def _fmt(v, suffix="", digits=0):
    return "-" if v is None else f"{v:.{digits}f}{suffix}"


def _track_car_suffix(stats) -> str:
    track = stats.get("track_name")
    if not track:
        return ""
    if stats.get("track_config"):
        track = f"{track} ({stats['track_config']})"
    car = stats.get("car_name")
    return f" - {html.escape(track)}" + (f" in {html.escape(car)}" if car else "")


def _render_findings(anomaly_buckets) -> str:
    if not anomaly_buckets:
        return "<p class='muted'>No windows matched any of the configured hypotheses. If you still noticed stutter in this session, the cause likely isn't captured by these signals yet - see the raw charts above for anything visually irregular.</p>"

    rows = []
    for b in anomaly_buckets:
        mm = int(b.t // 60)
        ss = int(b.t % 60)
        tag_html = "".join(
            f"<div class='tag'><span class='tag-name'>{html.escape(TAG_LABELS.get(name, name))}</span>"
            f"<span class='tag-detail'>{html.escape(detail)}</span></div>"
            for name, detail in b.tags
        )
        rows.append(f"<div class='window'><div class='window-time'>+{mm:02d}:{ss:02d}</div><div class='window-tags'>{tag_html}</div></div>")
    return "\n".join(rows)


def _render_windows_events(events) -> str:
    if not events:
        return ("<p class='muted'>No crash, GPU driver reset, hardware error or unexpected-shutdown "
                "events in the Windows logs during this session (or in the 15 minutes after it).</p>")
    rows = []
    for e in events:
        when = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        phase = "after session ended" if e["after_end"] else "during session"
        rows.append(
            f"<div class='window'><div class='window-time' style='width:150px;'>{when}<br>"
            f"<span class='muted' style='font-weight:400;font-size:0.75rem;'>{phase}</span></div>"
            f"<div class='window-tags'><div class='tag'><span class='tag-name'>{html.escape(e['label'])}</span>"
            f"<span class='tag-detail'>{html.escape(e['level'] or '')} - {html.escape(e['message'][:240])}</span></div></div></div>"
        )
    return "\n".join(rows)


def _render_settings_diff_list(changes, compared_label) -> str:
    if not changes:
        return f"<p class='muted'>No graphics settings differ {compared_label}.</p>"
    rows = []
    for key, old, new in changes:
        old_s = html.escape("(not set)" if old is None else str(old))
        new_s = html.escape("(not set)" if new is None else str(new))
        rows.append(
            f"<div class='window'><div class='window-time' style='width:260px;'>{html.escape(key)}</div>"
            f"<div class='window-tags'><div class='tag'><span class='tag-detail'>{old_s} &rarr; <b>{new_s}</b></span></div></div></div>"
        )
    return "\n".join(rows)


def _render_presentmon_summary(pm) -> str:
    if pm is None:
        return (
            "<p class='muted'>No PresentMon capture imported for this session. "
            "Run <code>PresentMon-&lt;version&gt;-x64.exe --process_name iRacingSim64DX11.exe "
            "--output_file capture.csv --date_time</code> during a session, then "
            "<code>python -m reports.presentmon_import &lt;session_id&gt; capture.csv</code>.</p>"
        )
    return f"""
    <div class="overview">
        <div class="stat"><div class="num">{pm['frame_count']}</div><div class="label">Frames captured</div></div>
        <div class="stat"><div class="num">{pm['dropped_count']}</div><div class="label">Dropped frames</div></div>
        <div class="stat"><div class="num">{pm['p50_ms']:.2f}ms</div><div class="label">Median frame time</div></div>
        <div class="stat"><div class="num">{pm['p95_ms']:.2f}ms</div><div class="label">P95 frame time</div></div>
        <div class="stat"><div class="num">{pm['p99_ms']:.2f}ms</div><div class="label">P99 frame time</div></div>
        <div class="stat"><div class="num">{pm['max_ms']:.2f}ms</div><div class="label">Worst frame time</div></div>
        <div class="stat"><div class="num">{pm['pct_over_694']:.1f}%</div><div class="label">Frames missing 144Hz budget (&gt;6.94ms)</div></div>
        <div class="stat"><div class="num">{pm['pct_over_833']:.1f}%</div><div class="label">Frames missing 120Hz budget (&gt;8.33ms)</div></div>
    </div>
    <canvas id="chart-presentmon"></canvas>
    """


def _presentmon_chart_script(pm) -> str:
    if pm is None:
        return ""
    series = pm["per_second"]
    return f"""
(function() {{
    const t144 = {1000.0/144.0};
    const t120 = {1000.0/120.0};
    const labels = {json.dumps(series['t'])};
    new Chart(document.getElementById('chart-presentmon').getContext('2d'), {{
        type: 'line',
        data: {{ labels, datasets: [
            {{ label:'Avg frame time (ms)', data:{json.dumps(series['avg_ms'])}, borderColor:'#4da3ff', borderWidth:1.5, pointRadius:0, tension:0.15 }},
            {{ label:'Worst frame time (ms)', data:{json.dumps(series['max_ms'])}, borderColor:'#e0503b', borderWidth:1.5, pointRadius:0, tension:0.15 }},
            {{ label:'144Hz budget', data: labels.map(() => t144), borderColor:'#35c07a', borderDash:[6,4], borderWidth:1, pointRadius:0 }},
            {{ label:'120Hz budget', data: labels.map(() => t120), borderColor:'#e0a92d', borderDash:[6,4], borderWidth:1, pointRadius:0 }},
        ]}},
        options: {{ animation:false, responsive:true, maintainAspectRatio:false,
            scales: {{ x: {{ display:false }}, y: {{ beginAtZero:true, ticks:{{color:'#8b93a3',font:{{size:10}}}}, grid:{{color:'#262b36'}} }} }},
            plugins: {{ legend: {{ labels:{{color:'#8b93a3',font:{{size:10}}}} }} }} }}
    }});
}})();
"""


def _render_dpcisr_summary(d) -> str:
    if d is None:
        return (
            "<p class='muted'>No DPC/ISR (interrupt load) capture for this session. "
            "Run <code>python -m reports.dpcisr_import &lt;session_id&gt; report.txt</code> "
            "(see README for the xperf capture command) to check whether any CPU core is "
            "disproportionately loaded with driver interrupt handling - e.g. USB/HID from "
            "wheel/pedal force feedback.</p>"
        )
    per_cpu = d["per_cpu"]
    total = sum(per_cpu.values()) or 1
    rows = sorted(per_cpu.items(), key=lambda kv: -kv[1])
    bars = []
    for cpu, usec in rows:
        pct = usec / total * 100
        style = "width:70px;" + ("color:#e0503b;font-weight:600;" if int(cpu) == d["dominant_cpu"] else "")
        bars.append(
            f"<div class='window'><div class='window-time' style='{style}'>CPU {cpu}</div>"
            f"<div class='window-tags'><div class='tag'><span class='tag-detail'>{usec/1000:.1f}ms total "
            f"({pct:.1f}% of all DPC/ISR time)</span></div></div></div>"
        )
    wdf_line = (
        f"<p class='muted'>Wdf01000.sys (USB/HID driver framework) - {d['wdf_total_usec']/1000:.1f}ms total, "
        f"heaviest on CPU {d['wdf_dominant_cpu']} ({d['wdf_dominant_cpu_usec']/1000:.1f}ms).</p>"
        if d["wdf_total_usec"] else "<p class='muted'>Wdf01000.sys (USB/HID) had negligible DPC/ISR time this session.</p>"
    )
    return f"""
    <div class="overview">
        <div class="stat"><div class="num">CPU {d['dominant_cpu']}</div><div class="label">Most-loaded core</div></div>
        <div class="stat"><div class="num">{d['dominant_cpu_pct_of_total']:.0f}%</div><div class="label">Share of all DPC/ISR time</div></div>
        <div class="stat"><div class="num">{d['dominant_cpu_top_module'] or '-'}</div><div class="label">Top driver on that core</div></div>
    </div>
    {wdf_line}
    {''.join(bars)}
    """


def _chart_script(chart_data) -> str:
    return f"""
const data = {json.dumps(chart_data)};
const common = {{ animation:false, responsive:true, maintainAspectRatio:false,
    scales: {{ x: {{ display:false }}, y: {{ beginAtZero:true, ticks:{{color:'#8b93a3',font:{{size:10}}}}, grid:{{color:'#262b36'}} }} }},
    plugins: {{ legend: {{ labels:{{color:'#8b93a3',font:{{size:10}}}} }} }} }};
function mk(id, datasets) {{
    new Chart(document.getElementById(id).getContext('2d'), {{ type:'line',
        data: {{ labels:data.labels, datasets: datasets.map(d => ({{...d, borderWidth:1.5, pointRadius:0, tension:0.15, spanGaps:true}})) }},
        options: common }});
}}
mk('chart-cpu', [
    {{ label:'Total %', data:data.cpu_total, borderColor:'#4da3ff' }},
    {{ label:'Busiest core %', data:data.cpu_max_core, borderColor:'#e0a92d' }},
    {{ label:'P-core %', data:data.cpu_p, borderColor:'#35c07a' }},
    {{ label:'E-core %', data:data.cpu_e, borderColor:'#e0503b' }},
].filter(d => d.data.some(v => v != null)));
mk('chart-gpu', [
    {{ label:'GPU util % (total)', data:data.gpu_util, borderColor:'#4da3ff' }},
    {{ label:"GPU util % (iRacing's share)", data:data.iracing_gpu_util, borderColor:'#35c07a' }},
    {{ label:'Encoder util %', data:data.encoder_util, borderColor:'#e0a92d' }},
]);
mk('chart-net', [
    {{ label:'Send Mbps', data:data.net_sent_mbps, borderColor:'#4da3ff' }},
]);
mk('chart-fps', [{{ label:'FPS', data:data.frame_rate, borderColor:'#4da3ff' }}]);
"""


def _render_single_html(data, settings_changes, prev_sid) -> str:
    s = data["stats"]
    chart_js = CHART_JS_PATH.read_text(encoding="utf-8") if CHART_JS_PATH.exists() else ""
    compared_label = f"since session {prev_sid}" if prev_sid is not None else "- no earlier session with a settings snapshot to compare against"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>iRacing Monitor Report - Session {s['session_id']}</title>
<style>{BASE_CSS}</style>
</head>
<body>
<p><a class="top-link" href="settings_history.html">&larr; settings history across all sessions</a></p>
<h1>iRacing Monitor Report</h1>
<p class="muted">Session {s['session_id']} - started {s['start']}{_track_car_suffix(s)}</p>

<div class="overview">
    <div class="stat"><div class="num">{_fmt(s['duration_min'], ' min', 1)}</div><div class="label">Duration</div></div>
    <div class="stat"><div class="num">{_fmt(s['avg_fps'])}</div><div class="label">Avg FPS</div></div>
    <div class="stat"><div class="num">{_fmt(s['avg_fps_on_track'])}</div><div class="label">Avg FPS (on track only)</div></div>
    <div class="stat"><div class="num">{_fmt(s['median_fps'])}</div><div class="label">Median FPS</div></div>
    <div class="stat"><div class="num">{_fmt(s['min_fps'])}</div><div class="label">Min FPS</div></div>
    <div class="stat"><div class="num">{_fmt(s['avg_cpu_total'], '%')}</div><div class="label">Avg CPU total</div></div>
    <div class="stat"><div class="num">{_fmt(s['avg_cpu_max_core'], '%')}</div><div class="label">Avg busiest core (on track)</div></div>
    <div class="stat"><div class="num">{_fmt(s['avg_gpu_util'], '%')}</div><div class="label">Avg GPU util</div></div>
    <div class="stat"><div class="num">{_fmt(s['avg_iracing_gpu_share'], '%')}</div><div class="label">GPU share from iRacing</div></div>
    <div class="stat"><div class="num">{_fmt(s['max_encoder_util'], '%')}</div><div class="label">Peak encoder util</div></div>
    <div class="stat"><div class="num">{_fmt(s['max_net_mbps'], ' Mbps')}</div><div class="label">Peak send throughput</div></div>
    <div class="stat"><div class="num">{s['throttled_seconds']}s</div><div class="label">GPU power/thermal limited{f" ({', '.join(s['throttle_reasons_seen'])})" if s['throttle_reasons_seen'] else ''}</div></div>
    <div class="stat"><div class="num">{s['anomaly_count']}</div><div class="label">Flagged windows</div></div>
    <div class="stat"><div class="num">{s['windows_event_count']}</div><div class="label">Windows crash/driver events</div></div>
    <div class="stat"><div class="num">{len(settings_changes)}</div><div class="label">Settings changed{f" (vs #{prev_sid})" if prev_sid else ""}</div></div>
</div>

<div class="chart-wrap"><h2>CPU (total / busiest core / P-core and E-core groups on hybrid CPUs)</h2><canvas id="chart-cpu"></canvas></div>
<div class="chart-wrap"><h2>GPU util (total vs iRacing's own share) / video encoder util</h2><canvas id="chart-gpu"></canvas></div>
<div class="chart-wrap"><h2>Network send throughput (Mbps)</h2><canvas id="chart-net"></canvas></div>
<div class="chart-wrap"><h2>iRacing frame rate</h2><canvas id="chart-fps"></canvas></div>

<div class="chart-wrap">
<h2>Windows events - crashes, GPU driver resets, hardware errors ({s['windows_event_count']})</h2>
{_render_windows_events(data['windows_events'])}
</div>

<div class="chart-wrap">
<h2>Flagged windows ({s['anomaly_count']})</h2>
{_render_findings(data['anomaly_buckets'])}
</div>

<div class="chart-wrap">
<h2>Frame time (PresentMon) - true per-frame timing, not averaged</h2>
{_render_presentmon_summary(data['presentmon'])}
</div>

<div class="chart-wrap">
<h2>CPU interrupt load (DPC/ISR) - which core is absorbing driver interrupt time</h2>
{_render_dpcisr_summary(data['dpcisr'])}
</div>

<div class="chart-wrap">
<h2>Graphics settings changed {compared_label}</h2>
{_render_settings_diff_list(settings_changes, compared_label)}
</div>

<script>{chart_js}</script>
<script>{_chart_script(data['chart_data'])}</script>
<script>{_presentmon_chart_script(data['presentmon'])}</script>
</body>
</html>
"""


# --- two-session comparison report ---------------------------------------

_CMP_METRICS = [
    ("windows_event_count", "Windows crash/driver events", 0, "lower"),
    ("avg_fps", "Avg FPS", 0, "higher"),
    ("avg_fps_on_track", "Avg FPS (on track only)", 0, "higher"),
    ("median_fps", "Median FPS", 0, "higher"),
    ("min_fps", "Min FPS", 0, "higher"),
    ("avg_cpu_total", "Avg CPU total %", 0, None),
    ("avg_cpu_max_core", "Avg busiest core % (on track)", 0, None),
    ("avg_cpu_p", "Avg CPU P-core %", 0, None),
    ("avg_cpu_e", "Avg CPU E-core %", 0, None),
    ("peak_cpu_e", "Peak CPU E-core %", 0, None),
    ("avg_gpu_util", "Avg GPU util %", 0, None),
    ("max_gpu_util", "Peak GPU util %", 0, None),
    ("avg_iracing_gpu_share", "Avg GPU share from iRacing %", 0, None),
    ("avg_encoder_util", "Avg video encoder %", 0, None),
    ("max_encoder_util", "Peak video encoder %", 0, None),
    ("avg_net_mbps", "Avg send throughput Mbps", 0, None),
    ("max_net_mbps", "Peak send throughput Mbps", 0, None),
    ("throttled_seconds", "GPU power/thermal limited (s)", 0, "lower"),
    ("anomaly_count", "Flagged windows", 0, "lower"),
]


def build_comparison_report(session_id_a: int, session_id_b: int) -> Path:
    data_a = analyze_session(session_id_a)
    data_b = analyze_session(session_id_b)
    sid_a, sid_b = data_a["stats"]["session_id"], data_b["stats"]["session_id"]

    settings_changes = settings_reader.diff_settings(data_a["flat_settings"], data_b["flat_settings"])

    OUT_DIR.mkdir(exist_ok=True, parents=True)
    out_path = OUT_DIR / f"compare_{sid_a}_vs_{sid_b}.html"
    out_path.write_text(_render_comparison_html(data_a, data_b, settings_changes), encoding="utf-8")
    return out_path


def _render_metrics_table(stats_a, stats_b) -> str:
    rows = []
    for key, label, digits, better in _CMP_METRICS:
        va, vb = stats_a.get(key), stats_b.get(key)
        cls = ""
        if better and va is not None and vb is not None and va != vb:
            improved = (vb > va) if better == "higher" else (vb < va)
            cls = " class='better'" if improved else " class='worse'"
        delta = "-" if va is None or vb is None else f"{vb - va:+.{digits}f}"
        rows.append(
            f"<tr><td class='key'>{label}</td><td>{_fmt(va, '', digits)}</td>"
            f"<td>{_fmt(vb, '', digits)}</td><td{cls}>{delta}</td></tr>"
        )
    return "\n".join(rows)


def _render_settings_table(changes, sid_a, sid_b) -> str:
    if not changes:
        return "<p class='muted'>No graphics settings differ between these two sessions.</p>"
    rows = []
    for key, old, new in changes:
        old_s = html.escape("(not set)" if old is None else str(old))
        new_s = html.escape("(not set)" if new is None else str(new))
        rows.append(f"<tr><td class='key'>{html.escape(key)}</td><td>{old_s}</td><td class='changed'>{new_s}</td></tr>")
    return f"""<table class="cmp">
<thead><tr><th>Setting</th><th>Session #{sid_a}</th><th>Session #{sid_b}</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>"""


def _overlay_chart_script(chart_a, chart_b) -> str:
    # Both sessions' bucket.t already starts at 0, so they're naturally
    # aligned on "seconds since session start" without any wall-clock math.
    n = max(len(chart_a["labels"]), len(chart_b["labels"]))
    labels = list(range(n))
    return f"""
const a = {json.dumps(chart_a)};
const b = {json.dumps(chart_b)};
const labels = {json.dumps(labels)};
const common = {{ animation:false, responsive:true, maintainAspectRatio:false,
    scales: {{ x: {{ display:false }}, y: {{ beginAtZero:true, ticks:{{color:'#8b93a3',font:{{size:10}}}}, grid:{{color:'#262b36'}} }} }},
    plugins: {{ legend: {{ labels:{{color:'#8b93a3',font:{{size:10}}}} }} }} }};
function mk(id, series) {{
    new Chart(document.getElementById(id).getContext('2d'), {{ type:'line',
        data: {{ labels, datasets: series.map(d => ({{...d, borderWidth:1.5, pointRadius:0, tension:0.15, spanGaps:true}})) }},
        options: common }});
}}
mk('chart-fps', [
    {{ label:'A: FPS', data:a.frame_rate, borderColor:'#4da3ff' }},
    {{ label:'B: FPS', data:b.frame_rate, borderColor:'#e0a92d' }},
]);
mk('chart-cpu', [
    {{ label:'A: CPU total', data:a.cpu_total, borderColor:'#4da3ff' }},
    {{ label:'B: CPU total', data:b.cpu_total, borderColor:'#e0a92d' }},
    {{ label:'A: CPU E-core', data:a.cpu_e, borderColor:'#4da3ff', borderDash:[5,4] }},
    {{ label:'B: CPU E-core', data:b.cpu_e, borderColor:'#e0a92d', borderDash:[5,4] }},
]);
mk('chart-gpu', [
    {{ label:'A: GPU util', data:a.gpu_util, borderColor:'#4da3ff' }},
    {{ label:'B: GPU util', data:b.gpu_util, borderColor:'#e0a92d' }},
    {{ label:'A: iRacing share', data:a.iracing_gpu_util, borderColor:'#4da3ff', borderDash:[2,3] }},
    {{ label:'B: iRacing share', data:b.iracing_gpu_util, borderColor:'#e0a92d', borderDash:[2,3] }},
    {{ label:'A: Encoder', data:a.encoder_util, borderColor:'#4da3ff', borderDash:[5,4] }},
    {{ label:'B: Encoder', data:b.encoder_util, borderColor:'#e0a92d', borderDash:[5,4] }},
]);
mk('chart-net', [
    {{ label:'A: Send Mbps', data:a.net_sent_mbps, borderColor:'#4da3ff' }},
    {{ label:'B: Send Mbps', data:b.net_sent_mbps, borderColor:'#e0a92d' }},
]);
"""


def _track_mismatch_banner(sa, sb) -> str:
    track_a = (sa.get("track_name"), sa.get("track_config"))
    track_b = (sb.get("track_name"), sb.get("track_config"))
    car_a, car_b = sa.get("car_name"), sb.get("car_name")
    if track_a == (None, None) or track_b == (None, None):
        return ""
    if track_a != track_b or car_a != car_b:
        return (
            "<p style='color:#e0503b;font-weight:600;'>Warning: these sessions are not on the same "
            f"track/car ({html.escape(str(track_a[0]))}"
            f"{'/' + html.escape(str(car_a)) if car_a else ''} vs "
            f"{html.escape(str(track_b[0]))}{'/' + html.escape(str(car_b)) if car_b else ''}) - "
            "performance differences below may be driven by that, not the settings changes.</p>"
        )
    return ""


def _render_comparison_html(data_a, data_b, settings_changes) -> str:
    sa, sb = data_a["stats"], data_b["stats"]
    chart_js = CHART_JS_PATH.read_text(encoding="utf-8") if CHART_JS_PATH.exists() else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>iRacing Monitor - Compare Session {sa['session_id']} vs {sb['session_id']}</title>
<style>{BASE_CSS}</style>
</head>
<body>
<p><a class="top-link" href="settings_history.html">&larr; settings history across all sessions</a></p>
<h1>Comparing session #{sa['session_id']} vs #{sb['session_id']}</h1>
<p class="muted">A = #{sa['session_id']} ({sa['start']}, {_fmt(sa['duration_min'], ' min', 1)}){_track_car_suffix(sa)} &nbsp;|&nbsp;
B = #{sb['session_id']} ({sb['start']}, {_fmt(sb['duration_min'], ' min', 1)}){_track_car_suffix(sb)}</p>
{_track_mismatch_banner(sa, sb)}

<div class="chart-wrap">
<h2>Performance: A vs B (green = better, red = worse, where "better" is defined)</h2>
<table class="cmp">
<thead><tr><th>Metric</th><th>A (#{sa['session_id']})</th><th>B (#{sb['session_id']})</th><th>Delta (B - A)</th></tr></thead>
<tbody>{_render_metrics_table(sa, sb)}</tbody>
</table>
</div>

<div class="chart-wrap"><h2>Frame rate - A (blue) vs B (amber), by seconds into session</h2><canvas id="chart-fps"></canvas></div>
<div class="chart-wrap"><h2>CPU total (solid) / E-core group (dashed) - A vs B</h2><canvas id="chart-cpu"></canvas></div>
<div class="chart-wrap"><h2>GPU util (solid) / video encoder (dashed) - A vs B</h2><canvas id="chart-gpu"></canvas></div>
<div class="chart-wrap"><h2>Network send throughput - A vs B</h2><canvas id="chart-net"></canvas></div>

<div class="chart-wrap">
<h2>Settings that differ between A and B ({len(settings_changes)})</h2>
{_render_settings_table(settings_changes, sa['session_id'], sb['session_id'])}
</div>

<script>{chart_js}</script>
<script>{_overlay_chart_script(data_a['chart_data'], data_b['chart_data'])}</script>
</body>
</html>
"""


def main():
    args = sys.argv[1:]
    if len(args) >= 2:
        out_path = build_comparison_report(int(args[0]), int(args[1]))
    elif len(args) == 1:
        out_path = build_report(int(args[0]))
    else:
        out_path = build_report(None)
    print(f"Report written to {out_path}")
    webbrowser.open(out_path.as_uri())


if __name__ == "__main__":
    main()
