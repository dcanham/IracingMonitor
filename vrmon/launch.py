"""Starts vrmon in the background (no console window) if it isn't already
running, then opens the dashboard. This is what the desktop/Start Menu
shortcut and the optional start-at-login entry run.

    pythonw vrmon\\launch.py              start if needed, open the dashboard
    pythonw vrmon\\launch.py --no-browser start if needed (start at login)
    pythonw vrmon\\launch.py --stop       stop a running vrmon

Run as a script path rather than `-m vrmon.launch` so it works from a
registry Run entry, which has no way to set the working directory.
"""

import argparse
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil  # noqa: E402

from vrmon import config  # noqa: E402

_START_TIMEOUT_S = 30.0
# Start each run with a fresh log once it gets this big, rather than
# growing forever on an always-on install.
_MAX_LOG_BYTES = 10 * 1024 * 1024


def is_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", config.SERVER_PORT)) == 0


def vrmon_processes() -> list[psutil.Process]:
    """Python processes running `-m vrmon` (the app itself, not this launcher)."""
    procs = []
    for p in psutil.process_iter(["name", "cmdline"]):
        cmd = p.info.get("cmdline") or []
        if "python" in (p.info.get("name") or "").lower() and cmd[-2:] == ["-m", "vrmon"]:
            procs.append(p)
    return procs


def start() -> bool:
    if is_running():
        return True
    if config.LOG_PATH.exists() and config.LOG_PATH.stat().st_size > _MAX_LOG_BYTES:
        config.LOG_PATH.unlink()
    log_file = open(config.LOG_PATH, "a", encoding="utf-8")
    subprocess.Popen(
        [str(config.PYTHONW_EXE), "-m", "vrmon"],
        cwd=str(config.REPO_ROOT),
        stdout=log_file,
        stderr=log_file,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | config.NO_WINDOW,
    )
    deadline = time.monotonic() + _START_TIMEOUT_S
    while time.monotonic() < deadline:
        if is_running():
            return True
        time.sleep(0.5)
    return False


def stop() -> int:
    procs = vrmon_processes()
    for p in procs:
        p.terminate()
    psutil.wait_procs(procs, timeout=10)
    return len(procs)


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Start or stop {config.APP_NAME}")
    parser.add_argument("--stop", action="store_true", help="stop a running vrmon")
    parser.add_argument("--no-browser", action="store_true", help="don't open the dashboard")
    args = parser.parse_args()

    if args.stop:
        stopped = stop()
        print(f"stopped {stopped} vrmon process(es)")
        return 0

    if not start():
        print(f"vrmon didn't start within {_START_TIMEOUT_S:.0f}s - see {config.LOG_PATH}")
        return 1
    if not args.no_browser:
        webbrowser.open(config.SERVER_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
