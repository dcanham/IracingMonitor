"""Central configuration for the iRacing performance monitor.

Anything that differs between PCs lives in data/settings.json (created
with defaults on first run, gitignored along with the rest of data/), so
nobody has to edit this file to set the app up on their machine.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Passed to every subprocess this app launches: when it runs without a
# console (pythonw, from the shortcut or the capture task), each console
# program it starts would otherwise flash up its own window - over the sim.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# --- paths -------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "vrmon.db"

# --- per-PC settings ---------------------------------------------------

SETTINGS_PATH = DATA_DIR / "settings.json"

_SETTINGS_DEFAULTS = {
    # "auto" sums traffic across every connected physical adapter; set an
    # adapter name (as shown by Get-NetAdapter) to watch just that one.
    "network_adapter": "auto",
    # Process-name substrings to record every tick even when they aren't
    # among the heaviest processes - e.g. a known troublemaker.
    "watch_processes": [],
    # "auto": Windows' own GPU counters (any vendor) plus NVIDIA's NVML on
    # NVIDIA cards for temperature/clocks/power/throttling. "nvml" or
    # "windows" forces one source only.
    "gpu_stats": "auto",
    # Blank = auto-detect (see _find_presentmon / XPERF_EXE below).
    "presentmon_exe": "",
    "xperf_exe": "",
}


def _load_settings() -> dict:
    settings = dict(_SETTINGS_DEFAULTS)
    try:
        # utf-8-sig: Notepad and Windows PowerShell can save with a BOM.
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
        settings.update({k: v for k, v in saved.items() if k in _SETTINGS_DEFAULTS})
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        log.warning("couldn't read %s - using defaults", SETTINGS_PATH, exc_info=True)
        return settings
    # Write back so a new or upgraded install always has every current key
    # visible to edit (and none that no longer do anything), without
    # overwriting anything already set.
    try:
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return settings


SETTINGS = _load_settings()

WEB_DIR = REPO_ROOT / "web"
# Generated session / comparison / settings-history reports (gitignored).
REPORTS_DIR = REPO_ROOT / "reports" / "out"


def _documents_dir() -> Path:
    # The real Documents known folder, not ~/Documents - it can be
    # redirected (e.g. to OneDrive\Documents, even with OneDrive disabled).
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            return Path(os.path.expandvars(winreg.QueryValueEx(key, "Personal")[0]))
    except OSError:
        return Path.home() / "Documents"


IRACING_DOCS_DIR = _documents_dir() / "iRacing"

# The renderer config iRacing uses on a flat monitor. (It keeps a separate
# rendererDX11OpenXR.ini for VR, which this app doesn't support.)
IRACING_RENDERER_INI = IRACING_DOCS_DIR / "rendererDX11Monitor.ini"

# --- process / network identification -----------------------------------

# Deliberately just the sim renderer, not iRacingUI.exe (the launcher/UI,
# which stays running in the background whenever the iRacing app is open
# and would otherwise start a "session" before you're actually driving).
IRACING_PROCESS_NAMES = ["iRacingSim64DX11.exe"]

# "auto", or one adapter's name as reported by
# psutil.net_io_counters(pernic=True).
NETWORK_ADAPTER_NAME = SETTINGS["network_adapter"]

# Virtual/tunnel adapters excluded from "auto" - their traffic also flows
# through a physical adapter, so counting them too would double it up.
NETWORK_ADAPTER_EXCLUDE_SUBSTRINGS = [
    "loopback", "vethernet", "virtualbox", "vmware", "hyper-v", "bluetooth",
    "isatap", "teredo", "local area connection*", "npcap", "wsl",
    "tailscale", "wireguard", "openvpn", "tap-", "zerotier", "nordlynx",
]

WATCH_PROCESS_SUBSTRINGS = [s.lower() for s in SETTINGS["watch_processes"]]

GPU_STATS = SETTINGS["gpu_stats"]
if GPU_STATS not in ("auto", "nvml", "windows"):
    log.warning("unknown gpu_stats %r in settings.json - using 'auto'", GPU_STATS)
    GPU_STATS = "auto"

# --- polling intervals (seconds) ----------------------------------------

SYSTEM_POLL_INTERVAL = 0.25
GPU_POLL_INTERVAL = 0.25
PROCESS_POLL_INTERVAL = 0.25
IRACING_POLL_INTERVAL = 0.05  # ~20Hz, irsdk itself ticks at sim rate
# Slower on purpose - this walks every process on the system each tick
# (psutil.process_iter over everything running), which is real overhead
# per-call. Correlating "something else spiked" against a frame-time
# hitch only needs multi-second resolution anyway.
TOP_PROCESS_POLL_INTERVAL = 2.0

# Slower still - each check spawns a PowerShell process to query the
# Windows System event log (Get-WinEvent). TDR/WHEA events are rare by
# nature, so this doesn't need tight resolution either.
WINDOWS_EVENTS_POLL_INTERVAL = 10.0

# How often to re-check for the session's track/car once recording starts.
SETTINGS_CAPTURE_POLL_INTERVAL_S = 1.0

# --- thresholds used by the report / live dashboard ----------------------

GPU_ENCODER_BUSY_PCT = 90.0
GPU_BUSY_PCT = 90.0
# Averaged over a 1s window - a saturated thread hops between cores, so
# its core rarely reads a full 100% for the whole second.
CPU_CORE_SATURATED_PCT = 90.0
FRAMERATE_DROP_PCT = 0.85  # flag a window if FPS falls below 85% of session median

# --- server ---------------------------------------------------------------

SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8756
SERVER_URL = f"http://localhost:{SERVER_PORT}"

# --- PresentMon automation -------------------------------------------------

TOOLS_DIR = REPO_ROOT / "tools"


def _find_presentmon() -> Path:
    """settings.json path if set, else the newest PresentMon console exe in
    tools/ (where setup downloads it) or Downloads (where people tend to
    put it by hand)."""
    if SETTINGS["presentmon_exe"]:
        return Path(SETTINGS["presentmon_exe"])
    for folder in (TOOLS_DIR, Path.home() / "Downloads"):
        found = sorted(folder.glob("PresentMon-*-x64.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        if found:
            return found[0]
    return TOOLS_DIR / "PresentMon-x64.exe"  # nonexistent - doctor reports it as missing


PRESENTMON_EXE = _find_presentmon()
PRESENTMON_OUTPUT_DIR = DATA_DIR / "presentmon"
# Pinned rather than "latest": the CSV import relies on this version's
# column names, and it's the one this app's analysis was validated on.
PRESENTMON_VERSION = "2.5.1"
PRESENTMON_URL = (
    f"https://github.com/GameTechDev/PresentMon/releases/download/"
    f"v{PRESENTMON_VERSION}/PresentMon-{PRESENTMON_VERSION}-x64.exe"
)
PYTHON_EXE = Path(sys.executable)
# Same interpreter without a console window - for the shortcut, the
# start-at-login entry and the capture task.
PYTHONW_EXE = PYTHON_EXE.with_name("pythonw.exe")

# Where vrmon's own log goes when it's started in the background.
LOG_PATH = DATA_DIR / "vrmon.log"

APP_NAME = "iRacing Monitor"
# HKCU\...\Run value name for the optional start-at-login entry.
AUTOSTART_VALUE_NAME = "iRacingMonitor"

# Must match the /tn used when creating the Scheduled Task (see
# reports/presentmon_auto_capture.py's module docstring for the one-time
# setup command). Collector_hub triggers this task the instant a session
# starts recording - it runs pre-elevated (no UAC prompt) because the task
# itself was created with "run with highest privileges".
PRESENTMON_TASK_NAME = "VrmonPresentMonCapture"

# Safety cap - if something goes wrong and the vrmon session never closes
# (e.g. vrmon itself got killed), stop watching after this long rather
# than polling forever.
PRESENTMON_AUTO_MAX_DURATION_S = 3 * 60 * 60

# DPC/ISR (interrupt) capture, via the Windows Performance Toolkit's
# xperf (Windows ADK, "Windows Performance Toolkit" feature only). This is
# what tells us whether any single CPU core (e.g. from USB HID/FFB
# interrupt load) is disproportionately loaded, the same class of data
# LatencyMon shows but fully scriptable.
_XPERF_CANDIDATES = [
    Path(rf"C:\Program Files (x86)\Windows Kits\{ver}\Windows Performance Toolkit\xperf.exe")
    for ver in ("10", "8.1")
]
XPERF_EXE = (
    Path(SETTINGS["xperf_exe"]) if SETTINGS["xperf_exe"]
    else next((p for p in _XPERF_CANDIDATES if p.exists()), _XPERF_CANDIDATES[0])
)
DPCISR_OUTPUT_DIR = DATA_DIR / "dpcisr"
