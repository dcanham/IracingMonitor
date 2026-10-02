# iRacing Monitor

A local performance monitor for iRacing on a regular monitor. Leave it
running and it records automatically every time you drive - CPU, GPU,
memory, disk, network, iRacing's own telemetry, true per-frame timing,
what else was running on the PC, and any crashes or driver resets Windows
logged - then builds a report for each session that points at what was
happening whenever your frame rate dipped.

Everything stays on your PC. Nothing is uploaded anywhere.

## What you get

- **A live dashboard** at <http://localhost:8756> - also viewable from a
  phone or tablet on the same network, at `http://<your-PC's-IP>:8756` -
  with a **Reports** list (newest session first, click to open) and a
  **Recent events** feed of Windows crashes and GPU driver resets, iRacing
  launcher and anti-cheat errors, sim crash reports, and when each
  session started and stopped - click an entry for the full details and
  to open the log it came from.
- **A report for every session**, built automatically when you exit the
  sim: frame-rate and frame-time charts, CPU/GPU load, a list of moments
  where FPS dipped and which signal (GPU maxed out, one CPU core
  saturated, another program using the GPU, ...) lined up with each one,
  Windows crash/driver events, and which graphics settings changed since
  your previous session.
- **Side-by-side comparisons** of any two sessions - change one setting,
  drive, and see whether it actually helped.

## Requirements

- Windows 10 or 11
- iRacing, played on a monitor (VR isn't supported)
- Administrator approval once during setup (one Windows UAC prompt)
- Python 3.10-3.13 - setup offers to install it for you if it's missing

| Graphics card | What's recorded |
|---|---|
| NVIDIA | Everything: load, iRacing's share, video encoder, VRAM, temperature, power draw, core clock, clock throttling |
| AMD / Intel | Load, iRacing's share, video encoder, VRAM, temperature, power draw, core clock - everything except throttle reasons |

AMD and Intel sensors come from
[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
(MPL-2.0), which setup downloads for you.

## Install

1. Download the latest **`iRacingMonitor-v<version>.zip`** from the
   [Releases page](https://github.com/dcanham/IracingMonitor/releases/latest)
   and unzip it somewhere permanent, e.g. `Documents\iRacing Monitor`.
   (Or `git clone` this repository.)
2. Double-click **`setup.bat`**. It will:
   - find (or offer to install) a suitable Python and install the
     packages this app needs into its own private folder (`.venv`),
   - download [PresentMon](https://github.com/GameTechDev/PresentMon)
     (Intel's open-source frame-timing tool) and check its signature, and
     LibreHardwareMonitor's sensor library (GPU temperature/power on AMD
     and Intel cards),
   - offer to install xperf (Microsoft's Windows Performance Toolkit) -
     optional; it shows which CPU core is busy handling driver interrupts
     from devices like your wheelbase,
   - set up the automatic capture task - **approve the admin prompt**,
   - add **iRacing Monitor** shortcuts to your desktop and Start Menu, and
     ask whether to start it automatically when you log in,
   - finish with a check of everything (see [Check setup](#check-setup)).

You can run `setup.bat` again at any time - after an update, or to fix
something the check flagged. Anything already done is skipped.

## Using it

1. Double-click **iRacing Monitor** on your desktop. It starts in the
   background (no window) and opens the dashboard. The first time,
   Windows Firewall may ask whether Python can use the network - allow
   **Private networks** if you want to view the dashboard from your
   phone, or cancel if you only use it on this PC (it works either way).
2. Drive as normal. Recording starts by itself when the iRacing sim
   loads and stops when you exit it - one session per sim launch.
   Nothing to do while you're driving.
3. When you exit the sim, the session's report is built automatically and
   appears at the top of the dashboard's **Reports** list within a
   minute. (The files live in `reports\out`.)

To stop it, use **Stop iRacing Monitor** in the Start Menu. If you chose
start-at-login, it's already running whenever you're logged in - it uses
very little when you're not racing.

### Reports by hand

From the app's folder, in a terminal:

```
.venv\Scripts\python -m reports.build_report               # latest session
.venv\Scripts\python -m reports.build_report 12            # session 12
.venv\Scripts\python -m reports.build_report 12 15         # compare 12 vs 15
.venv\Scripts\python -m reports.pdf_report 12              # printable PDF
.venv\Scripts\python -m reports.settings_history           # every session's settings in one table
```

Session numbers are on the dashboard while you drive, and in each
report's name.

## Settings

Per-PC settings live in `data\settings.json`, created the first time the
app runs. You rarely need to touch it.

| Setting | Default | What it does |
|---|---|---|
| `network_adapter` | `"auto"` | `"auto"` adds up every connected network adapter. Or put one adapter's name (as shown in Settings > Network) to watch only that one. |
| `watch_processes` | `[]` | Program names to record every few seconds even when they aren't among the busiest - e.g. `["discord", "obs"]` if you suspect one. |
| `gpu_stats` | `"auto"` | `"auto"` uses Windows' own GPU counters, plus NVIDIA's own stats on NVIDIA cards or LibreHardwareMonitor on AMD/Intel. `"windows"`, `"nvml"` or `"lhm"` forces a source. |
| `presentmon_exe`, `xperf_exe` | `""` | Leave blank to find them automatically. |

## Check setup

Start Menu > **iRacing Monitor - check setup** (or
`.venv\Scripts\python -m vrmon.doctor`) checks everything the app needs
and says how to fix anything that's wrong:

```
Frame-time capture
  [ OK ] PresentMon (frame timing) - PresentMon-2.5.1-x64.exe, signed by Intel
  [ OK ] xperf (CPU interrupt analysis) - ...\xperf.exe
  [ OK ] Capture task - ready
```

Common problems:

- **Capture task FAIL** - the admin prompt was declined during setup, or
  the app folder was moved. Run `setup.bat` again.
- **iRacing telemetry FAIL** - iRacing's `app.ini` has
  `irsdkEnableMem=0`. Set it to `1` with the sim closed.
- **Port 8756 in use** - another program is using the dashboard's port.
  Close it, or change `SERVER_PORT` in `vrmon\config.py`.
- **No frame-time data in a report** - check
  `data\presentmon\auto_capture.log` for what happened during that
  session's capture.

## What it records

| Source | What | How often |
|---|---|---|
| System | CPU total, busiest single core, P-/E-core groups on hybrid Intel CPUs, RAM, page file, disk, network | 4x per second |
| GPU | Load, iRacing's share, encoder/decoder, VRAM (total and iRacing's), temp/power/throttling on NVIDIA | 4x per second |
| iRacing | Frame rate, on-track state, lap and track position, track/car, drivers joining and leaving | 20x per second |
| Processes | The busiest programs on the PC | every 2 seconds |
| Windows logs | Crashes, blue screens, GPU driver resets, hardware errors, unexpected shutdowns - including from a hard crash, picked up when the app next starts | every 10 seconds |
| iRacing logs | Launcher and anti-cheat errors, sim crash reports (shown in the dashboard's Recent events) | on demand |
| PresentMon | Every single frame's timing | per frame |
| xperf | CPU interrupt (DPC/ISR) load per core and driver | whole session |
| Graphics settings | A snapshot of `rendererDX11Monitor.ini` | each session start |

Data is only written to disk while the sim is running, so leaving the app
on all the time doesn't fill your drive. Everything lives in the `data`
folder.

## Updating

Download the newest release's ZIP from the
[Releases page](https://github.com/dcanham/IracingMonitor/releases) and
unzip it over your existing folder - your recordings and settings in
`data` aren't part of the ZIP, so they're kept - or `git pull`. Then run
`setup.bat` again. The setup check shows which version you're on.

## Uninstall

```
.venv\Scripts\python -m vrmon.setup --uninstall
```

removes the capture task, shortcuts and start-at-login entry (one admin
prompt). Then delete the app's folder - or keep `data` if you want your
recorded sessions.

## Project layout

```
setup.bat          installer / updater
vrmon/             the recorder and live dashboard
  collectors/      one module per data source
  collector_hub.py background threads, recording gate, session tracking
  setup.py         setup steps (run by setup.bat), --uninstall
  doctor.py        setup check
  launch.py        what the shortcut runs: start in the background, open dashboard
  config.py        defaults + data/settings.json loading
reports/           session analysis, HTML/PDF reports, PresentMon/xperf import
web/               dashboard (Chart.js vendored, no internet needed)
data/              your recordings and settings (not in git)
tools/             PresentMon + LibreHardwareMonitor, downloaded by setup (not in git)
```
