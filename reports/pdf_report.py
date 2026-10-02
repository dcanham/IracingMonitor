"""Builds a print-ready PDF version of a session report - one chart per
page plus a findings summary and settings table, for sharing/printing
instead of viewing the interactive HTML report in a browser.

Usage:
    python -m reports.pdf_report                    # most recent session
    python -m reports.pdf_report <session_id>
"""

import sys
import textwrap
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vrmon import config  # noqa: E402
from reports.build_report import analyze_session, TAG_LABELS  # noqa: E402

OUT_DIR = config.REPORTS_DIR

PAGE_SIZE = (11, 8.5)  # landscape letter
BG = "#0f1115"
PANEL = "#171a21"
GRID = "#262b36"
FG = "#e6e8ec"
MUTED = "#8b93a3"
BLUE = "#4da3ff"
GREEN = "#35c07a"
RED = "#e0503b"
ORANGE = "#e0a92d"
PURPLE = "#b07cff"

_FONT_STYLE = {
    "text.color": FG,
    "axes.edgecolor": GRID,
    "axes.labelcolor": MUTED,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.facecolor": PANEL,
    "figure.facecolor": BG,
    "grid.color": GRID,
    "font.size": 9,
}


def _new_page():
    fig = plt.figure(figsize=PAGE_SIZE, facecolor=BG)
    return fig


def _shade_on_track(ax, labels, on_track):
    """Light shading over the seconds where on_track is true, so every
    time-series chart shows the loading/menu-vs-driving split at a glance."""
    in_span = False
    span_start = None
    for i, ot in enumerate(on_track):
        t = labels[i]
        if ot and not in_span:
            in_span = True
            span_start = t
        elif not ot and in_span:
            in_span = False
            ax.axvspan(span_start, t, color=GREEN, alpha=0.07, lw=0)
    if in_span:
        ax.axvspan(span_start, labels[-1], color=GREEN, alpha=0.07, lw=0)


def _style_axes(ax, title, ylabel):
    # title is intentionally not drawn on the axes - each page already has
    # its own fig.text() header above the chart; an ax-level title here
    # would overlap it.
    ax.set_xlabel("seconds into session", fontsize=8.5)
    ax.set_ylabel(ylabel, fontsize=8.5)
    ax.grid(True, alpha=0.35, lw=0.5)
    for spine in ax.spines.values():
        spine.set_color(GRID)


def _cover_page(pdf, stats, flat_settings, presentmon, dpcisr, anomaly_buckets, windows_events):
    fig = _new_page()
    fig.suptitle("iRacing Monitor - Session Report", color=FG, fontsize=20, fontweight="bold", x=0.06, y=0.94, ha="left")

    track = " / ".join(x for x in [stats.get("track_name"), stats.get("track_config")] if x) or "unknown track"
    sub = f"Session {stats['session_id']}  |  {stats['start']}  |  {stats['duration_min']:.1f} min  |  {track}  |  {stats.get('car_name') or 'unknown car'}"
    fig.text(0.06, 0.885, sub, color=MUTED, fontsize=11)

    # --- stat grid ---
    cells = [
        ("Avg FPS", f"{stats['avg_fps']:.1f}" if stats["avg_fps"] else "-"),
        ("Median FPS", f"{stats['median_fps']:.1f}" if stats["median_fps"] else "-"),
        ("On-track avg FPS", f"{stats['avg_fps_on_track']:.1f}" if stats["avg_fps_on_track"] else "-"),
        ("Avg CPU total", f"{stats['avg_cpu_total']:.0f}%" if stats["avg_cpu_total"] else "-"),
        ("Avg busiest core", f"{stats['avg_cpu_max_core']:.0f}%" if stats["avg_cpu_max_core"] else "-"),
        ("Avg / max GPU", f"{stats['avg_gpu_util']:.0f}% / {stats['max_gpu_util']:.0f}%" if stats["avg_gpu_util"] else "-"),
        ("Avg / max encoder", f"{stats['avg_encoder_util']:.0f}% / {stats['max_encoder_util']:.0f}%" if stats["avg_encoder_util"] else "-"),
        ("Avg / max network", f"{stats['avg_net_mbps']:.0f} / {stats['max_net_mbps']:.0f} Mbps" if stats["avg_net_mbps"] else "-"),
        ("iRacing GPU share", f"{stats['avg_iracing_gpu_share']:.0f}%" if stats["avg_iracing_gpu_share"] else "-"),
        ("Throttled seconds", str(stats["throttled_seconds"])),
        ("Anomaly windows", str(stats["anomaly_count"])),
    ]
    if presentmon:
        cells.append(("PresentMon frames", f"{presentmon['frame_count']:,} ({presentmon['dropped_count']} dropped)"))
        cells.append(("Frame time p50/p95/p99", f"{presentmon['p50_ms']:.1f} / {presentmon['p95_ms']:.1f} / {presentmon['p99_ms']:.1f} ms"))
        cells.append(("Worst frame", f"{presentmon['max_ms']:.0f} ms"))
    if dpcisr:
        cells.append(("DPC/ISR dominant core", f"CPU {dpcisr['dominant_cpu']} ({dpcisr['dominant_cpu_pct_of_total']:.0f}%, {dpcisr['dominant_cpu_top_module']})"))

    cols = 3
    x0, y0 = 0.06, 0.77
    cw, ch = 0.30, 0.115
    for i, (label, value) in enumerate(cells):
        col, row = i % cols, i // cols
        x = x0 + col * cw
        y = y0 - row * ch
        ax = fig.add_axes([x, y, cw - 0.02, ch - 0.02])
        ax.axis("off")
        ax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ax.transAxes, facecolor=PANEL, edgecolor=GRID, lw=0.8))
        ax.text(0.06, 0.62, value, transform=ax.transAxes, color=FG, fontsize=15, fontweight="bold", va="center")
        ax.text(0.06, 0.22, label.upper(), transform=ax.transAxes, color=MUTED, fontsize=7.5, va="center")

    # --- findings summary text (computed from this session's data only) ---
    tag_counts = {}
    on_track_flagged = 0
    for b in anomaly_buckets:
        for t in b.tags:
            key = t[0] if isinstance(t, tuple) else t
            tag_counts[key] = tag_counts.get(key, 0) + 1
        on_track_flagged += bool(b.on_track)
    rows = -(-len(cells) // cols)  # ceil
    findings_y = y0 - (rows - 1) * ch - 0.05
    fig.text(0.06, findings_y, "FINDINGS", color=MUTED, fontsize=9, fontweight="bold")
    if tag_counts:
        top = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:4]
        breakdown = ", ".join(f"{n}x {TAG_LABELS.get(k, k)}" for k, n in top)
        lines = [
            f"- {len(anomaly_buckets)} flagged window(s), {on_track_flagged} of them on track. Most common: {breakdown}.",
        ]
    else:
        lines = ["- No windows flagged by any rule."]
    if dpcisr:
        lines.append(
            f"- DPC/ISR: CPU {dpcisr['dominant_cpu']} carried the most interrupt time "
            f"({dpcisr['dominant_cpu_pct_of_total']:.0f}% of the total), top driver {dpcisr['dominant_cpu_top_module']}."
        )
    else:
        lines.append("- DPC/ISR trace not captured for this session.")
    if windows_events:
        listed = "; ".join(
            f"{time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['label']}"
            + (" (after the session ended)" if e["after_end"] else "")
            for e in windows_events[:4]
        )
        more = f" (+{len(windows_events) - 4} more)" if len(windows_events) > 4 else ""
        lines.append(f"- Windows logged {len(windows_events)} crash/driver event(s): {listed}{more}.")
    else:
        lines.append("- No crash, GPU driver reset, hardware error or unexpected-shutdown events in the Windows logs.")
    lines.append("- See following pages for the full frame-time, CPU/GPU, network, and interrupt-load charts.")
    wrapped = [w for line in lines for w in textwrap.wrap(line, 135, subsequent_indent="  ")]
    for i, line in enumerate(wrapped):
        fig.text(0.065, findings_y - 0.045 - i * 0.03, line, color=FG, fontsize=9.5)

    pdf.savefig(fig)
    plt.close(fig)


def _fps_page(pdf, chart_data, stats):
    fig = _new_page()
    ax = fig.add_axes([0.07, 0.12, 0.88, 0.72])
    labels = chart_data["labels"]
    fps = chart_data["frame_rate"]
    on_track = chart_data["on_track"]

    _shade_on_track(ax, labels, on_track)
    ax.plot(labels, fps, color=BLUE, lw=1.2)
    if stats["median_fps"]:
        ax.axhline(stats["median_fps"], color=MUTED, lw=0.8, ls="--", label=f"median {stats['median_fps']:.0f} fps")
    ax.axhline(144, color=GREEN, lw=0.8, ls=":", label="144 fps target")
    ax.legend(loc="lower right", frameon=False, fontsize=8, labelcolor=FG)
    _style_axes(ax, "iRacing FrameRate telemetry", "fps")
    fig.text(0.07, 0.88, "Frame Rate Over Time", color=FG, fontsize=16, fontweight="bold")
    fig.text(0.07, 0.855, "Shaded region = on track (IsOnTrack true). Unshaded = loading/menu.", color=MUTED, fontsize=9)
    pdf.savefig(fig)
    plt.close(fig)


def _cpu_page(pdf, chart_data):
    fig = _new_page()
    ax = fig.add_axes([0.07, 0.12, 0.88, 0.72])
    labels = chart_data["labels"]
    _shade_on_track(ax, labels, chart_data["on_track"])
    ax.plot(labels, chart_data["cpu_total"], color=FG, lw=1.1, label="total")
    for key, color, name in (("cpu_max_core", RED, "busiest core"),
                             ("cpu_p", BLUE, "P-core group"),
                             ("cpu_e", ORANGE, "E-core group")):
        # Busiest core is absent on older sessions; P/E only exist on hybrid CPUs.
        if any(v is not None for v in chart_data.get(key, [])):
            ax.plot(labels, chart_data[key], color=color, lw=1.1, label=name)
    ax.set_ylim(0, 100)
    ax.legend(loc="upper right", frameon=False, fontsize=8, labelcolor=FG)
    _style_axes(ax, "CPU Utilization", "%")
    fig.text(0.07, 0.88, "CPU Utilization Over Time", color=FG, fontsize=16, fontweight="bold")
    fig.text(0.07, 0.855, "Busiest core near 100% while the total stays low = one thread (usually the sim's main thread) is the limit.", color=MUTED, fontsize=9)
    pdf.savefig(fig)
    plt.close(fig)


def _gpu_page(pdf, chart_data):
    fig = _new_page()
    ax = fig.add_axes([0.07, 0.12, 0.88, 0.72])
    labels = chart_data["labels"]
    _shade_on_track(ax, labels, chart_data["on_track"])
    ax.plot(labels, chart_data["gpu_util"], color=GREEN, lw=1.1, label="GPU util (total)")
    ax.plot(labels, chart_data["iracing_gpu_util"], color=BLUE, lw=1.1, ls="--", label="GPU util (iRacing process)")
    ax.plot(labels, chart_data["encoder_util"], color=PURPLE, lw=1.1, label="video encoder util")
    ax.set_ylim(0, 100)
    ax.legend(loc="upper right", frameon=False, fontsize=8, labelcolor=FG)
    _style_axes(ax, "GPU / Encoder Utilization", "%")
    fig.text(0.07, 0.88, "GPU & Encoder Utilization Over Time", color=FG, fontsize=16, fontweight="bold")
    pdf.savefig(fig)
    plt.close(fig)


def _net_page(pdf, chart_data):
    fig = _new_page()
    ax = fig.add_axes([0.07, 0.12, 0.88, 0.72])
    labels = chart_data["labels"]
    _shade_on_track(ax, labels, chart_data["on_track"])
    ax.plot(labels, chart_data["net_sent_mbps"], color=BLUE, lw=1.1, label="network throughput")
    ax.legend(loc="upper right", frameon=False, fontsize=8, labelcolor=FG)
    _style_axes(ax, "Network Throughput (sent)", "Mbps")
    fig.text(0.07, 0.88, "Network Throughput Over Time", color=FG, fontsize=16, fontweight="bold")
    pdf.savefig(fig)
    plt.close(fig)


def _presentmon_page(pdf, presentmon):
    if not presentmon or not presentmon.get("per_second", {}).get("t"):
        return
    fig = _new_page()
    ax = fig.add_axes([0.07, 0.12, 0.88, 0.72])
    ps = presentmon["per_second"]
    ax.plot(ps["t"], ps["avg_ms"], color=BLUE, lw=1.1, label="avg frame time")
    ax.plot(ps["t"], ps["max_ms"], color=RED, lw=0.9, label="max frame time", alpha=0.85)
    ax.axhline(6.94, color=GREEN, lw=0.8, ls=":", label="6.94ms (144 Hz budget)")
    ax.set_yscale("log")
    ax.legend(loc="upper right", frameon=False, fontsize=8, labelcolor=FG)
    _style_axes(ax, "PresentMon per-frame timing (log scale)", "ms (log)")
    fig.text(0.07, 0.88, "PresentMon Frame Time (Ground Truth)", color=FG, fontsize=16, fontweight="bold")
    fig.text(
        0.07, 0.855,
        f"{presentmon['frame_count']:,} frames captured, {presentmon['dropped_count']} dropped  |  "
        f"p50={presentmon['p50_ms']:.1f}ms  p90={presentmon['p90_ms']:.1f}ms  p95={presentmon['p95_ms']:.1f}ms  "
        f"p99={presentmon['p99_ms']:.1f}ms  max={presentmon['max_ms']:.0f}ms",
        color=MUTED, fontsize=9,
    )
    pdf.savefig(fig)
    plt.close(fig)


def _dpcisr_page(pdf, dpcisr):
    if not dpcisr or not dpcisr.get("per_cpu"):
        return
    fig = _new_page()
    ax = fig.add_axes([0.07, 0.12, 0.88, 0.68])
    per_cpu = dpcisr["per_cpu"]
    cpus = sorted(per_cpu.keys(), key=lambda k: int(k))
    total = sum(per_cpu.values()) or 1
    pct = [per_cpu[c] / total * 100 for c in cpus]
    colors = [RED if int(c) == dpcisr["dominant_cpu"] else (ORANGE if int(c) == dpcisr.get("wdf_dominant_cpu") else BLUE) for c in cpus]
    ax.bar([f"CPU{c}" for c in cpus], pct, color=colors)
    ax.tick_params(axis="x", rotation=45, labelsize=7.5)
    _style_axes(ax, "DPC/ISR load share by logical CPU", "% of total interrupt load")
    ax.set_xlabel("")

    fig.text(0.07, 0.88, "Interrupt (DPC/ISR) Load Distribution", color=FG, fontsize=16, fontweight="bold")
    fig.text(
        0.07, 0.855,
        f"Red = dominant core (CPU {dpcisr['dominant_cpu']}, {dpcisr['dominant_cpu_pct_of_total']:.0f}% of total load, top driver {dpcisr['dominant_cpu_top_module']}).  "
        f"Orange = heaviest USB/HID core (CPU {dpcisr['wdf_dominant_cpu']}, Wdf01000.sys, {dpcisr['wdf_dominant_cpu_usec']/1000:.0f}ms total).",
        color=MUTED, fontsize=8.5,
    )
    pdf.savefig(fig)
    plt.close(fig)


def _anomaly_page(pdf, anomaly_buckets):
    if not anomaly_buckets:
        return
    fig = _new_page()
    fig.text(0.06, 0.92, "Flagged Anomaly Windows", color=FG, fontsize=16, fontweight="bold")
    fig.text(0.06, 0.895, "Automatically tagged by the rule engine in reports/analysis.py - each window lists every hypothesis it matches.", color=MUTED, fontsize=9)

    y = 0.84
    for b in anomaly_buckets:
        if y < 0.06:
            pdf.savefig(fig)
            plt.close(fig)
            fig = _new_page()
            y = 0.92
        fig.text(0.06, y, f"t={b.t:.0f}s", color=BLUE, fontsize=10, fontweight="bold")
        for tag in b.tags:
            name, detail = tag if isinstance(tag, tuple) else (tag, "")
            label = TAG_LABELS.get(name, name)
            fig.text(0.13, y, f"{label}", color=FG, fontsize=9, fontweight="bold")
            fig.text(0.40, y, detail, color=MUTED, fontsize=8.5)
            y -= 0.032
        y -= 0.012
    pdf.savefig(fig)
    plt.close(fig)


_SETTINGS_HIGHLIGHT = [
    "iRacing.Graphics Options.ResolutionScaling",
    "iRacing.Graphics Options.fullScreen",
    "iRacing.Graphics Options.MaxPreRenderedFrames",
    "iRacing.Graphics Options.EnableHDR",
    "iRacing.Graphics Options.VerticalSync",
    "iRacing.Graphics Options.LimitFrameRate",
    "iRacing.Graphics Options.DesiredFPSLimit",
    "iRacing.Graphics Options.MSAASamples",
    "iRacing.Graphics Options.AntiAliasMethod",
    "iRacing.Graphics Options.ShadowDetail",
    "iRacing.Graphics Options.CarDetail",
    "iRacing.Graphics Options.ObjectDetail",
    "iRacing.Graphics Options.ShaderQuality",
    "iRacing.Graphics Options.SSRLevel",
    "iRacing.Graphics Options.TwoPassTrees",
    "iRacing.Graphics Options.NvReflexMode",
]


def _settings_page(pdf, flat_settings):
    if not flat_settings:
        return
    fig = _new_page()
    fig.text(0.06, 0.92, "Key Settings Snapshot", color=FG, fontsize=16, fontweight="bold")
    fig.text(0.06, 0.895, "Full snapshot (150+ keys) is stored in the database; these are the ones that most affect frame time.", color=MUTED, fontsize=9)

    y = 0.84
    for key in _SETTINGS_HIGHLIGHT:
        if key not in flat_settings:
            continue
        value = flat_settings[key]
        fig.text(0.06, y, key, color=MUTED, fontsize=9)
        fig.text(0.62, y, str(value), color=FG, fontsize=9, fontweight="bold")
        y -= 0.036
    pdf.savefig(fig)
    plt.close(fig)


def build_pdf_report(session_id: int | None) -> Path:
    data = analyze_session(session_id)
    stats = data["stats"]
    sid = stats["session_id"]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"session_{sid}_report.pdf"

    with matplotlib.rc_context(_FONT_STYLE):
        with PdfPages(out_path) as pdf:
            _cover_page(pdf, stats, data["flat_settings"], data["presentmon"], data["dpcisr"],
                        data["anomaly_buckets"], data["windows_events"])
            _fps_page(pdf, data["chart_data"], stats)
            _cpu_page(pdf, data["chart_data"])
            _gpu_page(pdf, data["chart_data"])
            _net_page(pdf, data["chart_data"])
            _presentmon_page(pdf, data["presentmon"])
            _dpcisr_page(pdf, data["dpcisr"])
            _anomaly_page(pdf, data["anomaly_buckets"])
            _settings_page(pdf, data["flat_settings"])

            d = pdf.infodict()
            d["Title"] = f"iRacing Monitor Report - Session {sid}"
            d["CreationDate"] = time.strftime("%Y-%m-%d")

    return out_path


if __name__ == "__main__":
    arg_session = int(sys.argv[1]) if len(sys.argv) > 1 else None
    path = build_pdf_report(arg_session)
    print(f"PDF report written to {path}")
