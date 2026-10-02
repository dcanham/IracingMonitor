r"""Watches for a vrmon recording session and automatically captures,
imports, and reports PresentMon frame-time AND DPC/ISR (interrupt load)
data for it - zero manual steps once set up, so nothing needs to happen
inside the sim.

How it fits together:
  1. collector_hub.py calls `schtasks /run /tn VrmonPresentMonCapture`
     (non-elevated - just asks the scheduler to run it) the instant a
     session starts recording.
  2. That Scheduled Task runs THIS script, pre-elevated - no UAC prompt,
     because the task itself was created with "run with highest
     privileges" (see the one-time setup command below).
  3. This script finds the just-started session via vrmon's own API,
     launches PresentMon (frame timing) AND xperf (DPC/ISR - which CPU
     core is absorbing interrupt load, and from which driver) both
     targeting the same session, and polls the same API until that
     session's end_ts is set (recording stopped) - it does NOT wait to
     be killed, since a non-elevated process (vrmon) can't terminate
     this elevated one anyway.
  4. Stops both captures, imports both, builds the session report.

ONE-TIME SETUP: setup.bat creates the task (python -m vrmon.setup, one
UAC prompt). It has no trigger - it only ever runs via `schtasks /run` -
it runs with highest privileges, which is what makes every future run
pre-elevated with no prompt, and it allows parallel instances, so a new
session starting while the previous one's import is still finishing
still gets captured. `python -m vrmon.doctor` checks it's wired up;
`python -m vrmon.setup --uninstall` removes it.

The task runs this script under pythonw (no console window popping up
over the sim), so every tool it launches is started without a window.
xperf is optional - without it, only PresentMon is captured.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vrmon import config  # noqa: E402
from reports import build_report, dpcisr_import, presentmon_import  # noqa: E402

LOG_PATH = config.PRESENTMON_OUTPUT_DIR / "auto_capture.log"

_FIND_SESSION_TIMEOUT_S = 20.0
_FIND_SESSION_POLL_S = 1.0
_END_POLL_S = 5.0
_STARTUP_CHECK_S = 3.0


def _log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    try:
        config.PRESENTMON_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _api_get(path: str, timeout: float = 5.0):
    with urllib.request.urlopen(f"{config.SERVER_URL}{path}", timeout=timeout) as resp:
        return json.loads(resp.read())


def _find_open_session() -> dict | None:
    """The session vrmon just started recording - identified by having
    no end_ts yet. Retries briefly in case this script wins a race
    against the DB row actually being written."""
    deadline = time.monotonic() + _FIND_SESSION_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            sessions = _api_get("/api/sessions")
        except (urllib.error.URLError, OSError) as e:
            _log(f"waiting for vrmon API ({e})...")
            time.sleep(_FIND_SESSION_POLL_S)
            continue
        for s in sessions:
            if s.get("end_ts") is None:
                return s
        time.sleep(_FIND_SESSION_POLL_S)
    return None


def _wait_for_session_end(session_id: int) -> None:
    deadline = time.monotonic() + config.PRESENTMON_AUTO_MAX_DURATION_S
    while time.monotonic() < deadline:
        try:
            session = _api_get(f"/api/sessions/{session_id}")
            if session.get("end_ts") is not None:
                return
        except (urllib.error.URLError, OSError) as e:
            _log(f"lost contact with vrmon API ({e}) - still waiting")
        time.sleep(_END_POLL_S)
    _log(f"session {session_id} never closed within the safety cap - stopping capture anyway")


def _xperf(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(config.XPERF_EXE), *args], capture_output=True, text=True,
                          creationflags=config.NO_WINDOW)


def main() -> int:
    _log("=== presentmon_auto_capture starting ===")

    session = _find_open_session()
    if session is None:
        _log("no open vrmon session found within the timeout - nothing to capture, exiting")
        return 1
    session_id = session["id"]
    _log(f"capturing for session {session_id}")

    if not config.PRESENTMON_EXE.exists():
        _log(f"PresentMon not found at {config.PRESENTMON_EXE} - run setup.bat again to download it")
        return 1

    config.PRESENTMON_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = config.PRESENTMON_OUTPUT_DIR / f"presentmon_{timestamp}.csv"

    cmd = [
        str(config.PRESENTMON_EXE),
        "--process_name", config.IRACING_PROCESS_NAMES[0],
        "--output_file", str(csv_path),
        "--date_time",
        "--stop_existing_session",
    ]
    _log(f"launching: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            creationflags=config.NO_WINDOW)

    time.sleep(_STARTUP_CHECK_S)
    if proc.poll() is not None:
        stderr = proc.stderr.read() if proc.stderr else ""
        _log(f"PresentMon exited immediately (code {proc.returncode}) - not elevated? {stderr.strip()}")
        return 1

    config.DPCISR_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    etl_path = config.DPCISR_OUTPUT_DIR / f"dpcisr_{timestamp}.etl"
    report_path = config.DPCISR_OUTPUT_DIR / f"dpcisr_{timestamp}.txt"
    xperf_started = False
    if not config.XPERF_EXE.exists():
        _log("xperf not installed (optional) - capturing PresentMon only, no DPC/ISR data")
    else:
        # "NT Kernel Logger" is a single, reserved ETW session name - if a
        # previous run (or any other tool) left one active without cleanly
        # stopping it, -on fails with "file already exists" even though the
        # actual output file is unique per run. Clear any stale session
        # first, ignoring the error if there wasn't one to clear - same
        # pattern as PresentMon's --stop_existing_session.
        _xperf(["-stop"])
        # Default buffer allocation loses events on anything longer than a
        # very short capture (confirmed: ~59k events lost on a real 3-minute
        # session) - 1MB buffers x 200-400 of them (200-400MB) comfortably
        # covers a full driving session without excessive memory pressure.
        xperf_start = _xperf([
            "-on", "base+interrupt+dpc",
            "-f", str(etl_path),
            "-buffersize", "1024", "-minbuffers", "200", "-maxbuffers", "400",
        ])
        if xperf_start.returncode == 0:
            xperf_started = True
            _log(f"xperf DPC/ISR capture started -> {etl_path}")
        else:
            _log(f"xperf failed to start (code {xperf_start.returncode}): {xperf_start.stderr.strip()} - continuing with PresentMon only")

    _log("capture running - waiting for the session to end...")
    _wait_for_session_end(session_id)

    _log("stopping capture...")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _log("capture didn't stop gracefully - killing it")
        proc.kill()
        proc.wait(timeout=10)

    if xperf_started:
        xperf_stop = _xperf(["-stop"])
        if xperf_stop.returncode != 0:
            _log(f"xperf -stop failed (code {xperf_stop.returncode}): {xperf_stop.stderr.strip()}")
            xperf_started = False

    time.sleep(2)  # let the CSV finish flushing to disk
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        _log(f"no CSV (or empty) at {csv_path} - nothing to import")
    else:
        try:
            result = presentmon_import.import_csv(session_id, str(csv_path))
            _log(
                f"imported {result['frame_count']} frames "
                f"(p50={result['p50_ms']:.2f}ms, missed144={result['pct_over_694']:.1f}%) "
                f"for session {session_id}"
            )
        except SystemExit as e:
            _log(f"PresentMon import failed: {e}")

    if xperf_started and etl_path.exists() and etl_path.stat().st_size > 0:
        analyze = _xperf(["-i", str(etl_path), "-o", str(report_path), "-a", "dpcisr", "-summary"])
        analysis_ok = False
        if analyze.returncode != 0:
            # Keep the .etl on failure (e.g. lost-events warnings) rather
            # than silently discarding a capture we couldn't use - worth
            # investigating rather than losing outright.
            _log(f"xperf analysis failed (code {analyze.returncode}), keeping {etl_path} for inspection: {analyze.stderr.strip()}")
        elif report_path.exists() and report_path.stat().st_size > 0:
            try:
                summary = dpcisr_import.import_report(session_id, str(report_path))
                _log(
                    f"DPC/ISR: dominant core {summary['dominant_cpu']} "
                    f"({summary['dominant_cpu_pct_of_total']:.1f}% of load, top driver {summary['dominant_cpu_top_module']}); "
                    f"Wdf01000.sys heaviest on core {summary['wdf_dominant_cpu']}"
                )
                analysis_ok = True
            except SystemExit as e:
                _log(f"DPC/ISR import failed: {e}")

        if analysis_ok:
            # The .etl is large (tens of MB per session) and report.txt
            # already has everything we need from it - don't let these
            # pile up on disk once we know we got a usable report from it.
            try:
                etl_path.unlink()
            except OSError:
                pass

    try:
        out_path = build_report.build_report(session_id)
        _log(f"report built: {out_path}")
    except SystemExit as e:
        _log(f"report build failed: {e}")
        return 1

    _log("=== done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        _log("unhandled exception:")
        import traceback
        _log(traceback.format_exc())
        sys.exit(1)
