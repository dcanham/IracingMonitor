"""Polls Windows' System and Application event logs for GPU driver resets,
hardware errors, crashes and unclean shutdowns - things that look like an
unexplained hitch (or an abrupt end to a session) in our own telemetry
while actually originating below the game entirely:

- Display 4101: GPU driver timeout-and-recovery (TDR).
- WHEA-Logger warnings/errors: the CPU/chipset correcting (or failing to
  correct) a memory/bus fault. (Its informational records are skipped -
  one is logged on every boot.)
- Kernel-Power 41 / EventLog 6008: the PC went down without a clean
  shutdown - a hard crash, freeze or power loss.
- WER-SystemErrorReporting 1001: "rebooted from a bugcheck" - a blue
  screen, with the bugcheck code.
- Errors from the GPU driver itself (NVIDIA nvlddmkm, AMD, Intel).
- Application Error 1000 / Application Hang 1002 for the iRacing sim:
  the sim itself crashed or stopped responding. Same for Trading Paints,
  plus its .NET Runtime 1026 (unhandled exception) records.
- Windows Error Reporting 1001 (Application log) mentioning iRacing,
  Trading Paints or a
  LiveKernelEvent (e.g. 141, a GPU watchdog-forced hang recovery).
  Filtered to those, not every WER report system-wide, otherwise any
  unrelated background app's crash would flood this; and excluding
  RADAR_PRE_LEAK (Windows' memory-use diagnostic, not a crash) and
  BlueScreen reports - the System bugcheck record above already has the
  blue screen at its real time, whereas WER's copy can be an upload
  retry logged days later.

A hard crash takes vrmon down with it, and the crash's own records are
only written as Windows boots back up - so the first poll looks back to
`since` (when vrmon last recorded anything) to pick up whatever happened
while it wasn't running.

Each event gets a record_key so it's only ever stored once: the log's
record id, or for WER the report id - WER re-logs the same crash report
on every upload retry, sometimes dozens of times.

Uses PowerShell's Get-WinEvent rather than adding a pywin32 dependency,
consistent with how schtasks is already invoked elsewhere in this
project.
"""

import datetime
import logging
import re
import subprocess

from vrmon import config

log = logging.getLogger(__name__)

_PS_SCRIPT = """
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$start = [datetime]'{start_iso}'
function Emit($e) {{
  $msg = ($e.Message -replace "`r`n"," " -replace "`n"," ")
  "$($e.TimeCreated.ToString('o'))|$($e.LogName)|$($e.RecordId)|$($e.ProviderName)|$($e.Id)|$($e.LevelDisplayName)|$msg"
}}

Get-WinEvent -FilterHashtable @{{LogName='System'; StartTime=$start}} -ErrorAction SilentlyContinue |
  Where-Object {{
    ($_.ProviderName -eq 'Display' -and $_.Id -eq 4101) -or
    ($_.ProviderName -eq 'Microsoft-Windows-WHEA-Logger' -and $_.Level -le 3) -or
    ($_.ProviderName -eq 'Microsoft-Windows-Kernel-Power' -and $_.Id -eq 41) -or
    ($_.ProviderName -eq 'EventLog' -and $_.Id -eq 6008) -or
    ($_.ProviderName -eq 'Microsoft-Windows-WER-SystemErrorReporting' -and $_.Id -eq 1001) -or
    ($_.ProviderName -in @({gpu_driver_providers}) -and $_.Level -le 2)
  }} | ForEach-Object {{ Emit $_ }}

Get-WinEvent -FilterHashtable @{{LogName='Application'; StartTime=$start; ProviderName=@('Application Error','Application Hang')}} -ErrorAction SilentlyContinue |
  Where-Object {{ $_.Message -like '*iRacingSim64DX11*' -or $_.Message -like '*Trading Paints*' }} | ForEach-Object {{ Emit $_ }}

Get-WinEvent -FilterHashtable @{{LogName='Application'; StartTime=$start; ProviderName='.NET Runtime'; Id=1026}} -ErrorAction SilentlyContinue |
  Where-Object {{ $_.Message -like '*Trading Paints*' }} | ForEach-Object {{ Emit $_ }}

Get-WinEvent -FilterHashtable @{{LogName='Application'; StartTime=$start; ProviderName='Windows Error Reporting'; Id=1001}} -ErrorAction SilentlyContinue |
  Where-Object {{
    ($_.Message -like '*iRacingSim64DX11*' -or $_.Message -like '*Trading Paints*' -or $_.Message -like '*LiveKernelEvent*') -and
    $_.Message -notlike '*RADAR_PRE_LEAK*' -and $_.Message -notlike '*BlueScreen*'
  }} | ForEach-Object {{ Emit $_ }}
"""

# Kernel-mode display drivers' own event sources: NVIDIA, AMD (current and
# legacy), Intel. Only their errors/criticals are kept.
GPU_DRIVER_PROVIDERS = ["nvlddmkm", "amdkmdag", "amdwddmg", "atikmpag", "atikmdag", "igfx", "igfxn"]

_POLL_TIMEOUT_S = 10
# The first (look-back) poll can cover days of logs.
_FIRST_POLL_TIMEOUT_S = 60
# Don't trawl further back than this, however long vrmon wasn't running.
_MAX_LOOKBACK = datetime.timedelta(days=7)

_WER_REPORT_ID_RE = re.compile(r"Report Id:\s*([0-9a-fA-F-]{36})")


class WindowsEventCollector:
    def __init__(self, since: float | None = None):
        now = datetime.datetime.now(datetime.timezone.utc)
        if since is None:
            self._last_check = now
        else:
            start = datetime.datetime.fromtimestamp(since, datetime.timezone.utc)
            self._last_check = max(start, now - _MAX_LOOKBACK)
        self._first_poll = True

    def poll(self) -> list[dict]:
        start = self._last_check
        now = datetime.datetime.now(datetime.timezone.utc)
        timeout = _FIRST_POLL_TIMEOUT_S if self._first_poll else _POLL_TIMEOUT_S
        self._first_poll = False
        script = _PS_SCRIPT.format(
            start_iso=start.isoformat(),
            gpu_driver_providers=",".join(f"'{p}'" for p in GPU_DRIVER_PROVIDERS),
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=config.NO_WINDOW,
            )
        except Exception:
            log.debug("Windows event log poll failed", exc_info=True)
            self._last_check = now
            return []
        self._last_check = now

        events = []
        for line in result.stdout.splitlines():
            parts = line.strip().split("|", 6)
            if len(parts) < 7:
                continue
            time_str, log_name, record_id, provider, event_id, level, message = parts
            try:
                ts = datetime.datetime.fromisoformat(time_str).timestamp()
            except ValueError:
                continue
            # Windows embeds invisible left-to-right marks in dates in messages.
            message = message.replace("‎", "").replace("‏", "")
            report_id = _WER_REPORT_ID_RE.search(message) if provider == "Windows Error Reporting" else None
            events.append(
                {
                    "ts": ts,
                    "provider": provider,
                    "event_id": event_id,
                    "level": level,
                    "message": message[:4000],
                    "record_key": f"WER|{report_id.group(1).lower()}" if report_id else f"{log_name}|{record_id}",
                }
            )
        # Oldest first: Get-WinEvent returns newest first, and when WER has
        # re-logged a report many times the original is the one to keep.
        events.sort(key=lambda e: e["ts"])
        return events
