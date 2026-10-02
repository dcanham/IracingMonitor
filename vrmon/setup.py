"""Sets this app up on a PC - run by setup.bat after it has created the
Python environment and installed the packages:

    python -m vrmon.setup               set up (asks before anything optional)
    python -m vrmon.setup --yes         same, accepting every default
    python -m vrmon.setup --uninstall   remove the task, shortcuts and autostart

Steps (each skipped if already done):
  1. Download PresentMon (frame timing) into tools/, checking it's signed
     by Intel, and LibreHardwareMonitor's library (GPU temperature/power
     on AMD and Intel cards), checking its pinned checksum.
  2. Offer to install the Windows Performance Toolkit (xperf, for CPU
     interrupt analysis) - optional, via winget.
  3. Create the capture Scheduled Task - one UAC prompt. It runs with
     highest privileges so captures start with no prompt mid-session.
  4. Start Menu + Desktop shortcuts, and optionally start at login.
  5. Run the doctor check.

Your recorded data in data/ is never touched, including by --uninstall.
"""

import argparse
import shutil
import sys
import urllib.request
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vrmon import __version__, config, doctor, winutil  # noqa: E402

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_CAPTURE_SCRIPT = config.REPO_ROOT / "reports" / "presentmon_auto_capture.py"
_LAUNCH_SCRIPT = config.REPO_ROOT / "vrmon" / "launch.py"


def _ask(question: str, default: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return default
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def _start_menu_dir() -> Path:
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / config.APP_NAME


def _desktop_dir() -> Path:
    code, out = winutil.powershell("[Environment]::GetFolderPath('Desktop')")
    return Path(out) if code == 0 and out else Path.home() / "Desktop"


# --- steps ----------------------------------------------------------------


def install_presentmon() -> bool:
    if config.PRESENTMON_EXE.exists():
        print(f"  PresentMon already present: {config.PRESENTMON_EXE}")
        return True
    config.TOOLS_DIR.mkdir(exist_ok=True)
    target = config.TOOLS_DIR / f"PresentMon-{config.PRESENTMON_VERSION}-x64.exe"
    print(f"  downloading PresentMon {config.PRESENTMON_VERSION}...")
    try:
        with urllib.request.urlopen(config.PRESENTMON_URL, timeout=120) as resp, open(target, "wb") as f:
            shutil.copyfileobj(resp, f)
    except OSError as e:
        print(f"  ! download failed: {e}\n    You can download it by hand from {config.PRESENTMON_URL}"
              f"\n    and put it in {config.TOOLS_DIR}")
        target.unlink(missing_ok=True)
        return False
    status, signer = winutil.authenticode_signer(target)
    if status != "Valid" or "Intel Corporation" not in signer:
        target.unlink(missing_ok=True)
        print(f"  ! downloaded file isn't validly signed by Intel ({status}, {signer or 'no signer'}) - deleted it")
        return False
    print(f"  PresentMon downloaded and verified (signed by Intel): {target}")
    config.PRESENTMON_EXE = target
    return True


def install_lhm() -> bool:
    """LibreHardwareMonitor's library - GPU temperature/power/clock on AMD and
    Intel cards. Pinned version, verified by checksum; only its DLLs are kept."""
    if (config.LHM_DIR / "LibreHardwareMonitorLib.dll").exists():
        print(f"  LibreHardwareMonitor already present: {config.LHM_DIR}")
        return True
    import hashlib
    import io
    import zipfile
    print(f"  downloading LibreHardwareMonitor {config.LHM_VERSION} (GPU temperature/power on AMD and Intel)...")
    try:
        with urllib.request.urlopen(config.LHM_URL, timeout=120) as resp:
            data = resp.read()
    except OSError as e:
        print(f"  ! download failed: {e} - GPU temperature/power won't be recorded on AMD/Intel cards")
        return False
    digest = hashlib.sha256(data).hexdigest()
    if digest != config.LHM_SHA256:
        print(f"  ! download didn't match the expected checksum ({digest[:16]}...) - not installed")
        return False
    config.LHM_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.lower().endswith(".dll") and "/" not in name:  # the library + its dependencies
                (config.LHM_DIR / name).write_bytes(z.read(name))
    print(f"  LibreHardwareMonitor installed (checksum verified): {config.LHM_DIR}")
    return True


def install_xperf(assume_yes: bool) -> None:
    if config.XPERF_EXE.exists():
        print(f"  xperf already installed: {config.XPERF_EXE}")
        return
    print("  xperf (Windows Performance Toolkit) shows which CPU core is absorbing driver interrupt load")
    print("  (e.g. from a wheelbase or USB hub). It's optional - frame timing works without it.")
    if not _ask("  Install it now? It's a Microsoft download and shows an admin prompt.", True, assume_yes):
        print("  skipped - you can rerun setup.bat any time to add it")
        return
    if shutil.which("winget") is None:
        print("  ! winget isn't available. Install the Windows ADK by hand, choosing only")
        print("    'Windows Performance Toolkit': https://learn.microsoft.com/windows-hardware/get-started/adk-install")
        return
    code, out = winutil.powershell(
        "winget install --id Microsoft.WindowsADK -e --silent --accept-package-agreements "
        "--accept-source-agreements --override '/quiet /norestart /features OptionId.WindowsPerformanceToolkit'",
        timeout=1800,
    )
    # Re-resolve: XPERF_EXE was decided before the install happened.
    for candidate in config._XPERF_CANDIDATES:
        if candidate.exists():
            config.XPERF_EXE = candidate
            print(f"  xperf installed: {candidate}")
            return
    print(f"  ! install didn't complete (winget said: {out[-300:]})")


def create_capture_task() -> bool:
    task = winutil.capture_task()
    if (task and Path(task["Execute"]) == config.PYTHONW_EXE
            and str(_CAPTURE_SCRIPT).lower() in (task["Arguments"] or "").lower()
            and task["RunLevel"] == "Highest" and task["MultipleInstances"] == "Parallel"):
        print("  capture task already set up")
        return True
    print("  creating the capture task - approve the admin prompt that appears...")
    ran, out = winutil.powershell_elevated(f"""
$action = New-ScheduledTaskAction -Execute '{config.PYTHONW_EXE}' -Argument '"{_CAPTURE_SCRIPT}"' -WorkingDirectory '{config.REPO_ROOT}'
$settings = New-ScheduledTaskSettingsSet -MultipleInstances Parallel -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName '{config.PRESENTMON_TASK_NAME}' -Action $action -Settings $settings -Principal $principal `
    -Description '{config.APP_NAME}: captures frame timing (PresentMon) and CPU interrupt load (xperf) while the iRacing sim runs. Started on demand only.' `
    -Force | Out-Null
'created'
""")
    if not ran:
        print("  ! the admin prompt was declined - frame-time capture won't start automatically.")
        print("    Rerun setup.bat to try again.")
        return False
    if "created" not in out:
        print(f"  ! couldn't create the task: {out[-400:]}")
        return False
    print("  capture task created")
    return True


def _create_shortcut(path: Path, target: Path, arguments: str, description: str, icon: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    winutil.powershell(
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); "
        "$s.TargetPath = '{target}'; $s.Arguments = '{args}'; $s.WorkingDirectory = '{cwd}'; "
        "$s.Description = '{desc}'; $s.IconLocation = '{icon}'; $s.Save()".format(
            lnk=path, target=target, args=arguments.replace("'", "''"), cwd=config.REPO_ROOT,
            desc=description, icon=icon,
        )
    )


def create_shortcuts() -> None:
    start_menu = _start_menu_dir()
    open_icon = r"%SystemRoot%\System32\imageres.dll,144"
    _create_shortcut(start_menu / f"{config.APP_NAME}.lnk", config.PYTHONW_EXE, f'"{_LAUNCH_SCRIPT}"',
                     "Start iRacing Monitor (if needed) and open the dashboard", open_icon)
    _create_shortcut(start_menu / f"Stop {config.APP_NAME}.lnk", config.PYTHONW_EXE, f'"{_LAUNCH_SCRIPT}" --stop',
                     "Stop iRacing Monitor", r"%SystemRoot%\System32\imageres.dll,98")
    _create_shortcut(start_menu / f"{config.APP_NAME} - check setup.lnk", config.PYTHON_EXE, "-m vrmon.doctor --pause",
                     "Check that everything iRacing Monitor needs is set up", r"%SystemRoot%\System32\imageres.dll,76")
    _create_shortcut(_desktop_dir() / f"{config.APP_NAME}.lnk", config.PYTHONW_EXE, f'"{_LAUNCH_SCRIPT}"',
                     "Start iRacing Monitor (if needed) and open the dashboard", open_icon)
    print(f"  shortcuts created: Desktop and Start Menu > {config.APP_NAME}")


def set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, config.AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ,
                              f'"{config.PYTHONW_EXE}" "{_LAUNCH_SCRIPT}" --no-browser')
        else:
            try:
                winreg.DeleteValue(key, config.AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, config.AUTOSTART_VALUE_NAME)
            return True
    except FileNotFoundError:
        return False


# --- entry points -----------------------------------------------------------


def install(assume_yes: bool) -> int:
    print(f"{config.APP_NAME} {__version__} setup\n")
    print("1. Frame-time capture tool (PresentMon)")
    install_presentmon()
    print("\n   GPU sensors for AMD/Intel cards (LibreHardwareMonitor)")
    install_lhm()
    print("\n2. CPU interrupt analysis (xperf) - optional")
    install_xperf(assume_yes)
    print("\n3. Automatic capture task")
    create_capture_task()
    print("\n4. Shortcuts")
    create_shortcuts()
    already = autostart_enabled()
    want = _ask("  Start iRacing Monitor automatically when you log in to Windows? "
                "(It uses very little when you're not racing.)", already, assume_yes)
    set_autostart(want)
    print(f"  start at login: {'on' if want else 'off'}")
    print("\n5. Checking everything\n")
    code = doctor.run()
    print(f"\nTo start: double-click '{config.APP_NAME}' on your desktop. It records automatically")
    print("whenever the iRacing sim is running - no need to touch it while you drive.")
    return code


def uninstall() -> int:
    print(f"Removing {config.APP_NAME}'s task, shortcuts and autostart (your data in {config.DATA_DIR} is kept)\n")
    from vrmon.launch import stop
    stop()
    set_autostart(False)
    for folder in (_start_menu_dir(),):
        shutil.rmtree(folder, ignore_errors=True)
    (_desktop_dir() / f"{config.APP_NAME}.lnk").unlink(missing_ok=True)
    print("  shortcuts and autostart removed")
    if winutil.capture_task() is not None:
        print("  removing the capture task - approve the admin prompt...")
        ran, out = winutil.powershell_elevated(
            f"Unregister-ScheduledTask -TaskName '{config.PRESENTMON_TASK_NAME}' -Confirm:$false; 'removed'")
        print("  capture task removed" if ran and "removed" in out else "  ! couldn't remove the capture task")
    print("\nDone. To remove the app completely, delete this folder:", config.REPO_ROOT)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Set up {config.APP_NAME} on this PC")
    parser.add_argument("--yes", action="store_true", help="accept every default without asking")
    parser.add_argument("--uninstall", action="store_true", help="remove the task, shortcuts and autostart")
    args = parser.parse_args()
    return uninstall() if args.uninstall else install(args.yes)


if __name__ == "__main__":
    sys.exit(main())
