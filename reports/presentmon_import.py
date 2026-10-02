"""Imports a PresentMon (GameTechDev/PresentMon, v2.x metrics) CSV capture
and attaches summary frame-time stats to a session.

Our own telemetry only sees CPU/GPU utilization averaged per second, which
can't distinguish "healthy headroom" from "occasionally missing a tight
per-frame deadline" - PresentMon's per-present timing fills that gap.

Usage:
    python -m reports.presentmon_import <session_id> <csv_path>

Capture command (run elevated, during a normal vrmon-recorded session):
    PresentMon-<version>-x64.exe --process_name iRacingSim64DX11.exe ^
        --output_file capture.csv --date_time

--date_time is required - it's what lets this import line the capture up
against the session's own wall-clock timeline instead of guessing at a
relative-time offset. Without it, every row's CPUStartTime is "seconds
since PresentMon started," which has no fixed relationship to when the
vrmon session itself started.
"""

import csv
import statistics
import sys
from datetime import datetime

from vrmon import store

# The columns this cares about, from PresentMon v2.x's default CSV schema
# (confirmed against GameTechDev/PresentMon's README-ConsoleApplication.md).
_TIMESTAMP_COL = "CPUStartDateTime"
_FRAMETIME_COL = "MsBetweenPresents"
# Real captures (PresentMon 2.5.1) don't have a "DisplayedTime" column
# despite it being in the documented schema - "MsUntilDisplayed" being
# NA is what actually indicates a dropped/never-displayed frame here.
_DISPLAYED_COL = "MsUntilDisplayed"

# 144Hz needs every frame under ~6.94ms; 120Hz under ~8.33ms.
_BUDGET_144HZ_MS = 1000.0 / 144.0
_BUDGET_120HZ_MS = 1000.0 / 120.0


def _parse_timestamp(raw: str) -> float:
    """CPUStartDateTime -> Unix epoch seconds. Format isn't nailed down
    precisely in PresentMon's docs ("nanosecond precision date and time"),
    so this tries a couple of reasonable interpretations before giving up
    - if it fails, the caller gets the raw string in the error to report
    back so this can be fixed against real output."""
    text = raw.strip()
    # Python's datetime can't hold nanosecond precision - trim any
    # fractional-seconds part down to microseconds (6 digits) first.
    if "." in text:
        head, _, frac_and_rest = text.partition(".")
        frac = ""
        rest = ""
        for i, ch in enumerate(frac_and_rest):
            if not ch.isdigit():
                frac, rest = frac_and_rest[:i], frac_and_rest[i:]
                break
        else:
            frac, rest = frac_and_rest, ""
        text = f"{head}.{frac[:6]}{rest}"

    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f",
        # Some rows (observed: the tail end of a capture, where PresentMon
        # appears to occasionally emit a timestamp with no fractional
        # seconds at all) have no "." - same three layouts, without %f.
        "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    raise ValueError(f"could not parse PresentMon timestamp: {raw!r}")


def import_csv(session_id: int, csv_path: str) -> dict:
    session = store.get_session(session_id)
    if session is None:
        raise SystemExit(f"no such session: {session_id}")
    start_ts = session["start_ts"]
    end_ts = session["end_ts"] or float("inf")

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if _TIMESTAMP_COL not in fieldnames:
            raise SystemExit(
                f"CSV has no '{_TIMESTAMP_COL}' column - re-capture with the --date_time flag "
                f"so rows can be lined up with the session's timeline.\n"
                f"Columns found: {fieldnames}"
            )
        if _FRAMETIME_COL not in fieldnames:
            raise SystemExit(
                f"CSV has no '{_FRAMETIME_COL}' column - make sure the capture used "
                f"PresentMon's default v2.x metrics (not --v1_metrics).\n"
                f"Columns found: {fieldnames}"
            )

        # Two passes: first parse every row's timestamp as-is, then check
        # whether it lines up with the session window at all. PresentMon's
        # --date_time output has turned out NOT to reliably be local wall
        # clock time (observed offsets around a clean multiple of 30
        # minutes from local time, closer to a fixed Microsoft-internal
        # convention than to the system timezone) - rather than hardcode
        # a guessed timezone that might not hold on a different machine
        # or DST state, this detects the offset empirically by comparing
        # the capture's own timestamps against the session's real window.
        rows = []  # (raw_ts_seconds, ms, displayed_raw)
        first_row_ts_raw = None
        unparseable = 0
        for row in reader:
            raw_ts = row.get(_TIMESTAMP_COL)
            if not raw_ts:
                continue
            if first_row_ts_raw is None:
                first_row_ts_raw = raw_ts
            try:
                ts = _parse_timestamp(raw_ts)
            except ValueError:
                # A handful of odd rows (seen: no fractional-seconds at
                # the tail of a capture) shouldn't sink an import that's
                # otherwise got 100k+ good rows - skip and keep going,
                # only fail out if literally nothing parsed at all.
                unparseable += 1
                continue
            raw_ms = row.get(_FRAMETIME_COL)
            rows.append((ts, raw_ms, row.get(_DISPLAYED_COL)))

    if not rows:
        raise SystemExit(f"no usable rows found in {csv_path}")
    if unparseable:
        print(f"note: skipped {unparseable} row(s) with an unparseable {_TIMESTAMP_COL} out of {unparseable + len(rows)} total")

    offset = 0.0
    in_window = sum(1 for ts, _, _ in rows if start_ts <= ts <= end_ts)
    if in_window == 0:
        # Guess the correction from the gap between the capture's first
        # row and the session start, rounded to the nearest half hour -
        # timezone-style offsets land on clean boundaries; a few minutes
        # of residual is expected and fine (capture usually starts a
        # little before vrmon's own recording gate engages).
        guess = start_ts - rows[0][0]
        offset = round(guess / 1800) * 1800
        corrected_in_window = sum(1 for ts, _, _ in rows if start_ts <= ts + offset <= end_ts)
        if corrected_in_window == 0:
            raise SystemExit(
                f"no rows fell inside session #{session_id}'s window "
                f"({session['start_ts']} - {session['end_ts']}), even after trying a "
                f"{offset/3600:+.1f}h auto-correction. First row's {_TIMESTAMP_COL} was "
                f"{first_row_ts_raw!r} - check the capture actually overlapped this session."
            )
        log_msg = f"note: PresentMon timestamps didn't match the session window directly - applied a {offset/3600:+.1f}h offset correction ({corrected_in_window} frames matched)"
        print(log_msg)

    frame_times = []  # (t_relative_seconds, ms)
    dropped = 0
    for ts, raw_ms, raw_displayed in rows:
        ts = ts + offset
        if not (start_ts <= ts <= end_ts):
            continue
        displayed = (raw_displayed or "").strip().upper()
        if displayed == "NA":
            dropped += 1
            continue
        if not raw_ms:
            continue
        try:
            ms = float(raw_ms)
        except ValueError:
            continue
        frame_times.append((ts - start_ts, ms))

    if not frame_times:
        raise SystemExit(
            f"offset correction found matching timestamps but every one of them had "
            f"an empty/unparseable {_FRAMETIME_COL} value - check the CSV isn't truncated."
        )

    ms_values = sorted(ms for _, ms in frame_times)
    n = len(ms_values)

    def pct(p):
        idx = min(int(p * n), n - 1)
        return ms_values[idx]

    p50, p90, p95, p99 = pct(0.50), pct(0.90), pct(0.95), pct(0.99)
    max_ms = ms_values[-1]
    over_694 = sum(1 for v in ms_values if v > _BUDGET_144HZ_MS)
    over_833 = sum(1 for v in ms_values if v > _BUDGET_120HZ_MS)

    per_second: dict[int, list[float]] = {}
    for t, ms in frame_times:
        per_second.setdefault(int(t), []).append(ms)
    per_second_series = {
        "t": sorted(per_second.keys()),
        "avg_ms": [statistics.fmean(per_second[t]) for t in sorted(per_second.keys())],
        "max_ms": [max(per_second[t]) for t in sorted(per_second.keys())],
    }

    import time as _time
    import json as _json

    store.insert_presentmon_summary(
        session_id=session_id,
        imported_ts=_time.time(),
        csv_path=csv_path,
        frame_count=n,
        dropped_count=dropped,
        p50_ms=p50,
        p90_ms=p90,
        p95_ms=p95,
        p99_ms=p99,
        max_ms=max_ms,
        pct_over_694=100.0 * over_694 / n,
        pct_over_833=100.0 * over_833 / n,
        per_second_json=_json.dumps(per_second_series),
    )

    return {
        "frame_count": n,
        "dropped_count": dropped,
        "p50_ms": p50,
        "p90_ms": p90,
        "p95_ms": p95,
        "p99_ms": p99,
        "max_ms": max_ms,
        "pct_over_694": 100.0 * over_694 / n,
        "pct_over_833": 100.0 * over_833 / n,
    }


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: python -m reports.presentmon_import <session_id> <csv_path>")
    session_id = int(sys.argv[1])
    csv_path = sys.argv[2]
    result = import_csv(session_id, csv_path)
    print(f"Imported {result['frame_count']} frames ({result['dropped_count']} dropped) for session {session_id}:")
    print(f"  p50={result['p50_ms']:.2f}ms  p90={result['p90_ms']:.2f}ms  p95={result['p95_ms']:.2f}ms  p99={result['p99_ms']:.2f}ms  max={result['max_ms']:.2f}ms")
    print(f"  missed 144Hz budget (>6.94ms): {result['pct_over_694']:.1f}% of frames")
    print(f"  missed 120Hz budget (>8.33ms): {result['pct_over_833']:.1f}% of frames")


if __name__ == "__main__":
    main()
