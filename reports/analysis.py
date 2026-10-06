"""Turns raw per-collector rows for a session into 1-second buckets and
flags buckets against simple, explicit rules for each hypothesis from the
troubleshooting recap. Deliberately reports which conditions a window
matches rather than a single verdict - several can be true at once, and
sometimes none are (a real stutter with no matching signal is itself
useful information: it means the cause isn't one of these).
"""

import statistics
from dataclasses import dataclass, field

from vrmon import config


@dataclass
class Bucket:
    t: float  # bucket start, seconds relative to session start
    cpu_total_pct: float | None = None
    cpu_p_pct: float | None = None
    cpu_e_pct: float | None = None
    cpu_max_core_pct: float | None = None
    gpu_util_pct: float | None = None
    gpu_mem_util_pct: float | None = None
    encoder_util_pct: float | None = None
    gpu_throttled: bool | None = None
    gpu_throttle_reasons: list = field(default_factory=list)
    iracing_gpu_util_pct: float | None = None
    iracing_encoder_util_pct: float | None = None
    net_sent_mbps: float | None = None
    disk_write_mbps: float | None = None
    frame_rate_avg: float | None = None
    frame_rate_min: float | None = None
    on_track: bool | None = None
    # iRacing's own diagnostics (None on sessions recorded before they were added)
    chan_quality_min: float | None = None       # worst of own / partner connection quality, 0-1
    chan_latency_max: float | None = None       # seconds
    chan_avg_latency: float | None = None       # seconds
    cpu_usage_fg_max: float | None = None       # iRacing's foreground/render thread, 0-1
    hard_page_faults_max: float | None = None   # per second
    sim_paused_s: float | None = None           # time the simulation itself fell behind the clock
    paint_files: int = 0                        # paints Trading Paints wrote in this second or the 2 before
    tags: list = field(default_factory=list)


def _avg(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def _min(values):
    values = [v for v in values if v is not None]
    return min(values) if values else None


# The sim loads a new paint shortly after Trading Paints writes it.
PAINT_LOAD_WINDOW_S = 2


def build_buckets(start_ts: float, end_ts: float, system_rows, gpu_rows, iracing_rows, tp_events=()) -> list[Bucket]:
    duration = max(end_ts - start_ts, 1)
    n_buckets = int(duration) + 1
    buckets = [Bucket(t=i) for i in range(n_buckets)]

    def idx(ts):
        i = int(ts - start_ts)
        return i if 0 <= i < n_buckets else None

    grouped_system = [[] for _ in range(n_buckets)]
    for r in system_rows:
        i = idx(r["ts"])
        if i is not None:
            grouped_system[i].append(r)
    grouped_gpu = [[] for _ in range(n_buckets)]
    for r in gpu_rows:
        i = idx(r["ts"])
        if i is not None:
            grouped_gpu[i].append(r)
    grouped_iracing = [[] for _ in range(n_buckets)]
    for r in iracing_rows:
        i = idx(r["ts"])
        if i is not None:
            grouped_iracing[i].append(r)

    for i, b in enumerate(buckets):
        sys_rows = grouped_system[i]
        b.cpu_total_pct = _avg([r["cpu_total_pct"] for r in sys_rows])
        b.cpu_p_pct = _avg([r["cpu_p_pct"] for r in sys_rows])
        b.cpu_e_pct = _avg([r["cpu_e_pct"] for r in sys_rows])
        # Absent on sessions recorded before this stat existed.
        b.cpu_max_core_pct = _avg([r["cpu_max_core_pct"] for r in sys_rows if "cpu_max_core_pct" in r.keys()])
        b.net_sent_mbps = _avg([r["net_sent_bps"] for r in sys_rows if r["net_sent_bps"] is not None])
        if b.net_sent_mbps is not None:
            b.net_sent_mbps = b.net_sent_mbps * 8 / 1e6
        b.disk_write_mbps = _avg([r["disk_write_bps"] for r in sys_rows if r["disk_write_bps"] is not None])
        if b.disk_write_mbps is not None:
            b.disk_write_mbps = b.disk_write_mbps / 1e6

        gpu_rows_i = grouped_gpu[i]
        b.gpu_util_pct = _avg([r["gpu_util_pct"] for r in gpu_rows_i])
        b.gpu_mem_util_pct = _avg([r["mem_util_pct"] for r in gpu_rows_i])
        b.encoder_util_pct = _avg([r["encoder_util_pct"] for r in gpu_rows_i])
        b.iracing_gpu_util_pct = _avg([r["iracing_sm_util_pct"] for r in gpu_rows_i])
        b.iracing_encoder_util_pct = _avg([r["iracing_enc_util_pct"] for r in gpu_rows_i])
        throttle_flags = [r["throttled"] for r in gpu_rows_i if r["throttled"] is not None]
        b.gpu_throttled = bool(any(throttle_flags)) if throttle_flags else None
        reasons = set()
        for r in gpu_rows_i:
            if r["throttle_reasons"]:
                reasons.update(r["throttle_reasons"].split(","))
        b.gpu_throttle_reasons = sorted(reasons)

        all_ir_rows = grouped_iracing[i]
        ir_rows = [r for r in all_ir_rows if r["frame_rate"] is not None]
        b.frame_rate_avg = _avg([r["frame_rate"] for r in ir_rows])
        b.frame_rate_min = _min([r["frame_rate"] for r in ir_rows])
        on_track_values = [r["on_track"] for r in all_ir_rows if r["on_track"] is not None]
        b.on_track = bool(any(on_track_values)) if on_track_values else None
        if all_ir_rows and "chan_quality" in all_ir_rows[0].keys():
            _iracing_diagnostics(b, all_ir_rows)

    if iracing_rows and "session_tick" in iracing_rows[0].keys():
        _sim_pauses(buckets, iracing_rows, idx)

    for r in tp_events:
        if r["kind"] in ("paint_added", "paint_updated"):
            i = idx(r["ts"])
            if i is not None:
                for j in range(i, min(i + PAINT_LOAD_WINDOW_S + 1, n_buckets)):
                    buckets[j].paint_files += 1

    return buckets


def _max(values):
    values = [v for v in values if v is not None]
    return max(values) if values else None


def _iracing_diagnostics(b: Bucket, rows) -> None:
    qualities = [r["chan_quality"] for r in rows] + [r["chan_partner_quality"] for r in rows]
    b.chan_quality_min = _min(qualities)
    b.chan_latency_max = _max([r["chan_latency"] for r in rows])
    b.chan_avg_latency = _avg([r["chan_avg_latency"] for r in rows])
    b.cpu_usage_fg_max = _max([r["cpu_usage_fg"] for r in rows])
    b.hard_page_faults_max = _max([r["mem_page_fault_sec"] for r in rows])


def _sim_pauses(buckets: list[Bucket], rows, idx) -> None:
    """The sim ticks at 60 Hz: between two consecutive samples, ticks/60
    should keep pace with wall-clock time. If it falls behind, the
    simulation itself paused (not just the rendering) - while driving,
    that's a real hitch. Checked across the whole session rather than per
    bucket so a pause straddling a second boundary isn't missed; it's
    charged to the bucket where the sim resumed."""
    ticked = sorted((r for r in rows if r["session_tick"] is not None and r["on_track"]), key=lambda r: r["ts"])
    for a, c in zip(ticked, ticked[1:]):
        i = idx(c["ts"])
        if i is None:
            continue
        b = buckets[i]
        if b.sim_paused_s is None:
            b.sim_paused_s = 0.0
        dt = c["ts"] - a["ts"]
        # A long gap between samples is vrmon not sampling (or a new
        # session), not something the sim did.
        if dt > 2.0:
            continue
        lag = dt - (c["session_tick"] - a["session_tick"]) / 60.0
        if lag > 0.05:
            b.sim_paused_s += lag


def tag_buckets(buckets: list[Bucket]) -> float | None:
    """Mutates each bucket's .tags list in place. Returns the session's
    median frame rate (or None if no iRacing data was captured)."""
    # FPS only means something while driving - loading screens, the garage,
    # menus and replays run at whatever rate and would otherwise flood the
    # report with "dips". Falls back to every second if the session has no
    # on-track data at all.
    driving = any(b.on_track for b in buckets)
    all_fps = [b.frame_rate_avg for b in buckets if b.frame_rate_avg is not None and (b.on_track or not driving)]
    median_fps = statistics.median(all_fps) if all_fps else None
    fps_floor = median_fps * config.FRAMERATE_DROP_PCT if median_fps else None

    for b in buckets:
        fps_dip = (fps_floor is not None and b.frame_rate_min is not None and b.frame_rate_min < fps_floor
                   and (b.on_track or not driving))

        if b.encoder_util_pct is not None and b.encoder_util_pct >= config.GPU_ENCODER_BUSY_PCT:
            b.tags.append(("encoder-bound", f"GPU video encoder util {b.encoder_util_pct:.0f}%"))
        if b.gpu_util_pct is not None and b.gpu_util_pct >= config.GPU_BUSY_PCT:
            b.tags.append(("gpu-bound", f"GPU util {b.gpu_util_pct:.0f}%"))
        if b.gpu_throttled:
            b.tags.append((
                "gpu-power-thermal-limited",
                f"GPU clocks actively capped by: {', '.join(b.gpu_throttle_reasons)}",
            ))
        if (
            b.gpu_util_pct is not None
            and b.iracing_gpu_util_pct is not None
            and b.gpu_util_pct >= config.GPU_BUSY_PCT
            and b.gpu_util_pct - b.iracing_gpu_util_pct >= 20
        ):
            b.tags.append((
                "gpu-load-not-from-iracing",
                f"GPU util {b.gpu_util_pct:.0f}% but iRacing's own share was only "
                f"{b.iracing_gpu_util_pct:.0f}% - something else running is using the rest",
            ))
        if (
            b.cpu_e_pct is not None
            and b.cpu_p_pct is not None
            and b.cpu_e_pct >= 50
            and b.cpu_p_pct < 70
            and fps_dip
        ):
            b.tags.append((
                "possible-core-scheduling-stall",
                f"E-core group busy ({b.cpu_e_pct:.0f}%) while P-core group had headroom ({b.cpu_p_pct:.0f}%), during a frame-rate dip",
            ))
        if b.cpu_max_core_pct is not None and b.cpu_max_core_pct >= config.CPU_CORE_SATURATED_PCT and fps_dip:
            b.tags.append((
                "cpu-core-saturated",
                f"busiest CPU core at {b.cpu_max_core_pct:.0f}% (all-core average {b.cpu_total_pct or 0:.0f}%) "
                f"during a frame-rate dip - a single thread, likely the sim's main thread, is the limit",
            ))
        # iRacing's own diagnostics. Connection problems and sim pauses are
        # flagged whether or not FPS dipped: a network "warp" or a paused
        # simulation can feel like a jump even at a steady frame rate.
        if b.on_track and b.chan_quality_min is not None and b.chan_quality_min < config.IRACING_CHAN_QUALITY_MIN:
            b.tags.append((
                "connection-degraded",
                f"connection quality to the iRacing server dropped to {b.chan_quality_min * 100:.0f}% "
                f"(normally ~100%) - lost/late packets, which can warp cars or stutter the sim",
            ))
        if (b.on_track and b.chan_latency_max is not None and b.chan_avg_latency is not None
                and b.chan_latency_max - b.chan_avg_latency >= config.IRACING_LATENCY_SPIKE_S):
            b.tags.append((
                "latency-spike",
                f"latency to the iRacing server spiked to {b.chan_latency_max * 1000:.0f}ms "
                f"(average {b.chan_avg_latency * 1000:.0f}ms)",
            ))
        if b.on_track and b.sim_paused_s is not None and b.sim_paused_s >= config.IRACING_SIM_PAUSE_S:
            b.tags.append((
                "sim-paused",
                f"the simulation itself paused for ~{b.sim_paused_s * 1000:.0f}ms (its 60Hz tick fell behind the clock) "
                f"- not just the rendering",
            ))
        if fps_dip and b.hard_page_faults_max is not None and b.hard_page_faults_max >= config.IRACING_HARD_PAGE_FAULTS:
            b.tags.append((
                "hard-page-faults",
                f"iRacing had {b.hard_page_faults_max:.0f} hard page faults/s - waiting for data to be read back from disk",
            ))
        if fps_dip and b.cpu_usage_fg_max is not None and b.cpu_usage_fg_max >= config.IRACING_FG_THREAD_BUSY:
            b.tags.append((
                "render-thread-busy",
                f"iRacing's render thread was busy {b.cpu_usage_fg_max * 100:.0f}% of the time - "
                f"it, not the GPU, is holding the frame rate back",
            ))
        if fps_dip and b.paint_files:
            b.tags.append((
                "trading-paints-download",
                f"Trading Paints wrote {b.paint_files} paint file{'s' if b.paint_files != 1 else ''} in the "
                f"{PAINT_LOAD_WINDOW_S}s before - iRacing loads each new paint as it arrives",
            ))
        if fps_dip and not b.tags:
            b.tags.append((
                "unexplained-fps-dip",
                f"FPS dropped to {b.frame_rate_min:.0f} (session median {median_fps:.0f}) with no matching signal below",
            ))

    return median_fps
