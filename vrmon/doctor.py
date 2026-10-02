"""Checks that everything this app needs is installed and wired up, and
says how to fix whatever isn't.

    python -m vrmon.doctor           (or the "Check setup" shortcut)

Each check prints OK, WARN (works, but something is limited or missing)
or FAIL (something that will stop recording or capture from working).
Exits non-zero if anything FAILed.
"""

import argparse
import importlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vrmon import __version__, config, winutil  # noqa: E402

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_results: list[tuple[str, str, str, str]] = []


def check(status: str, title: str, detail: str = "", fix: str = "") -> None:
    _results.append((status, title, detail, fix))
    print(f"  [{status:^4}] {title}" + (f" - {detail}" if detail else ""))
    if fix and status != OK:
        print(f"         -> {fix}")


def check_python() -> None:
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro} at {sys.executable}"
    if v < (3, 10):
        check(FAIL, "Python version", detail, "Python 3.10 or newer is required - run setup.bat")
    else:
        check(OK, "Python version", detail)
    missing = []
    for module in ("psutil", "pynvml", "irsdk", "fastapi", "uvicorn", "matplotlib"):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    if missing:
        check(FAIL, "Python packages", f"missing: {', '.join(missing)}", "run setup.bat")
    else:
        check(OK, "Python packages", "all installed")


def check_settings() -> None:
    s = config.SETTINGS
    check(OK, "Settings", f"{config.SETTINGS_PATH} - network {s['network_adapter']}, gpu stats {s['gpu_stats']}, "
          f"watching {s['watch_processes'] or 'nothing extra'}")


def check_iracing() -> None:
    docs = config.IRACING_DOCS_DIR
    if not docs.exists():
        check(FAIL, "iRacing documents folder", f"{docs} not found",
              "install iRacing and run the sim once - it creates this folder")
        return
    if config.IRACING_RENDERER_INI.exists():
        check(OK, "iRacing graphics settings", str(config.IRACING_RENDERER_INI))
    else:
        check(WARN, "iRacing graphics settings", f"{config.IRACING_RENDERER_INI.name} not found",
              "run the sim once on a monitor - settings snapshots need this file")
    app_ini = docs / "app.ini"
    enabled = None
    if app_ini.exists():
        for line in app_ini.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().lower().startswith("irsdkenablemem="):
                enabled = line.split("=", 1)[1].split(";")[0].strip()
    if enabled == "0":
        check(FAIL, "iRacing telemetry", "irsdkEnableMem=0 in app.ini - the sim won't share telemetry",
              f"set irsdkEnableMem=1 in {app_ini} (with the sim closed)")
    else:
        check(OK, "iRacing telemetry", "shared-memory telemetry enabled")


def check_hardware() -> None:
    try:
        from vrmon.collectors.gpu import GpuCollector
        gpu = GpuCollector()
        name, backend = gpu.name, gpu.backend
        gpu.shutdown()
        if "nvml" in backend:
            check(OK, "GPU stats", f"{name} via {backend}")
        else:
            check(WARN, "GPU stats", f"{name} via {backend} - load and VRAM only",
                  "temperature, clocks, power and throttling are only available on NVIDIA GPUs so far")
    except Exception as e:
        check(FAIL, "GPU stats", f"couldn't open any GPU stats source ({e})")

    from vrmon.cpu_topology import build_core_groups
    groups = build_core_groups()
    if groups["E"]:
        check(OK, "CPU", f"hybrid - {len(groups['P'])} performance + {len(groups['E'])} efficiency threads")
    else:
        check(OK, "CPU", f"{len(groups['P'])} threads (no P/E-core split on this CPU)")

    import psutil
    if config.NETWORK_ADAPTER_NAME == "auto":
        from vrmon.collectors.system import _is_physical_adapter
        stats = psutil.net_if_stats()
        up = [n for n in psutil.net_io_counters(pernic=True) if _is_physical_adapter(n) and n in stats and stats[n].isup]
        if up:
            check(OK, "Network", f"auto - counting {', '.join(up)}")
        else:
            check(WARN, "Network", "no connected network adapter found", "network throughput won't be recorded")
    elif config.NETWORK_ADAPTER_NAME in psutil.net_io_counters(pernic=True):
        check(OK, "Network", f"watching {config.NETWORK_ADAPTER_NAME}")
    else:
        check(FAIL, "Network", f"adapter '{config.NETWORK_ADAPTER_NAME}' from settings.json not found",
              f"set network_adapter to \"auto\" in {config.SETTINGS_PATH}")


def check_capture_tools() -> None:
    pm = config.PRESENTMON_EXE
    if not pm.exists():
        check(FAIL, "PresentMon (frame timing)", "not found", "run setup.bat to download it")
    else:
        status, signer = winutil.authenticode_signer(pm)
        if status == "Valid" and "Intel Corporation" in signer:
            check(OK, "PresentMon (frame timing)", f"{pm.name}, signed by Intel")
        else:
            check(FAIL, "PresentMon (frame timing)", f"{pm} signature is {status or 'unknown'}",
                  "delete it and run setup.bat to download a fresh copy")

    if config.XPERF_EXE.exists():
        check(OK, "xperf (CPU interrupt analysis)", str(config.XPERF_EXE))
    else:
        check(WARN, "xperf (CPU interrupt analysis)", "not installed - optional",
              "run setup.bat to install the Windows Performance Toolkit")

    task = winutil.capture_task()
    if task is None:
        check(FAIL, "Capture task", "not set up - PresentMon/xperf won't start automatically", "run setup.bat")
        return
    script = config.REPO_ROOT / "reports" / "presentmon_auto_capture.py"
    problems = []
    if not Path(task["Execute"]).exists():
        problems.append(f"its Python ({task['Execute']}) no longer exists")
    if str(script).lower() not in (task["Arguments"] or "").lower():
        problems.append("it points at a different copy of this app")
    if task["RunLevel"] != "Highest":
        problems.append("it doesn't run with highest privileges")
    if task["MultipleInstances"] != "Parallel":
        problems.append("a session started soon after another would get no capture")
    if problems:
        check(FAIL, "Capture task", "; ".join(problems), "run setup.bat to recreate it")
    else:
        check(OK, "Capture task", "ready")


def check_runtime() -> None:
    from vrmon.launch import is_running, vrmon_processes
    if is_running():
        if vrmon_processes():
            check(OK, "vrmon", f"running - dashboard at {config.SERVER_URL}")
        else:
            check(FAIL, "vrmon", f"port {config.SERVER_PORT} is in use by another program",
                  f"close whatever is using port {config.SERVER_PORT}, or change SERVER_PORT in vrmon/config.py")
    else:
        check(WARN, "vrmon", "not running - nothing will be recorded",
              f"start it with the '{config.APP_NAME}' shortcut")

    code, out = winutil.powershell("Get-WinEvent -LogName System -MaxEvents 1 -ErrorAction Stop | Out-Null; 'ok'")
    if code == 0 and out.endswith("ok"):
        check(OK, "Windows event logs", "readable (crash / driver-reset detection works)")
    else:
        check(WARN, "Windows event logs", f"couldn't read the System log ({out[:120]})",
              "crash and GPU driver reset events won't be recorded")

    free_gb = shutil.disk_usage(config.DATA_DIR).free / 1e9
    if free_gb < 5:
        check(WARN, "Disk space", f"{free_gb:.1f} GB free for {config.DATA_DIR}",
              "each session's frame-time capture can be a few hundred MB")
    else:
        check(OK, "Disk space", f"{free_gb:.0f} GB free")


def run() -> int:
    print(f"{config.APP_NAME} {__version__} - setup check\n")
    for group, fn in (("Python", check_python), ("Settings", check_settings), ("iRacing", check_iracing),
                      ("Hardware", check_hardware), ("Frame-time capture", check_capture_tools),
                      ("Runtime", check_runtime)):
        print(group)
        try:
            fn()
        except Exception as e:
            check(FAIL, f"{group} checks", f"crashed: {e}")
        print()
    fails = sum(1 for r in _results if r[0] == FAIL)
    warns = sum(1 for r in _results if r[0] == WARN)
    if fails:
        print(f"{fails} problem(s) need fixing before recording will fully work.")
    elif warns:
        print(f"Ready to record ({warns} warning(s) above - optional extras or limits).")
    else:
        print("Everything is ready.")
    return 1 if fails else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check this app's setup")
    parser.add_argument("--pause", action="store_true", help="wait for Enter before exiting (for the shortcut)")
    args = parser.parse_args()
    code = run()
    if args.pause:
        input("\nPress Enter to close...")
    return code


if __name__ == "__main__":
    sys.exit(main())
