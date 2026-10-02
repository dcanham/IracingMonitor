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
    tags: list = field(default_factory=list)


def _avg(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def _min(values):
    values = [v for v in values if v is not None]
    return min(values) if values else None


def build_buckets(start_ts: float, end_ts: float, system_rows, gpu_rows, iracing_rows) -> list[Bucket]:
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

    return buckets


def tag_buckets(buckets: list[Bucket]) -> float | None:
    """Mutates each bucket's .tags list in place. Returns the session's
    median frame rate (or None if no iRacing data was captured)."""
    all_fps = [b.frame_rate_avg for b in buckets if b.frame_rate_avg is not None]
    median_fps = statistics.median(all_fps) if all_fps else None
    fps_floor = median_fps * config.FRAMERATE_DROP_PCT if median_fps else None

    for b in buckets:
        fps_dip = fps_floor is not None and b.frame_rate_min is not None and b.frame_rate_min < fps_floor

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
        if fps_dip and not b.tags:
            b.tags.append((
                "unexplained-fps-dip",
                f"FPS dropped to {b.frame_rate_min:.0f} (session median {median_fps:.0f}) with no matching signal below",
            ))

    return median_fps
