@echo off
rem iRacing Monitor setup - double-click to install or update.
rem Safe to run again any time: every step is skipped if already done.
setlocal
cd /d "%~dp0"
title iRacing Monitor setup
echo ============================================================
echo  iRacing Monitor setup
echo ============================================================
echo.

rem Windows' default 260-character path limit: some packages contain deeply
rem nested files, so pip fails if this folder's own path is already long.
set "SETUP_DIR=%~dp0"
powershell -NoProfile -Command "exit [int]($env:SETUP_DIR.Length -gt 110)" >nul 2>&1
if errorlevel 1 (
    echo This folder's path is too long for Windows to install the app's packages:
    echo   %~dp0
    echo Move the iRacingMonitor folder somewhere shorter - e.g. C:\iRacingMonitor
    echo or Documents\iRacingMonitor - and run setup.bat from there.
    goto fail
)

rem Supported Python versions: 3.10 - 3.13. The newest Python is often
rem ahead of the packages this app needs (e.g. no prebuilt matplotlib yet),
rem and pip then tries - and fails - to compile them from source.
set "VERCHECK=import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)"

if not exist ".venv\Scripts\python.exe" goto find_python
".venv\Scripts\python.exe" -c "%VERCHECK%" >nul 2>&1 && goto have_venv
echo The existing .venv uses an unsupported Python version - rebuilding it...
rmdir /s /q .venv

:find_python
rem --- find a supported Python to build the environment from -------------
rem "python" can be the Microsoft Store stub that just opens the Store, so
rem each candidate is checked by actually running it.
set "PY="
for %%V in (3.12 3.13 3.11 3.10) do if not defined PY call :try_py py -%%V
if not defined PY call :try_py python
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" call :try_py "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto make_venv

echo A supported Python (3.10 - 3.13) wasn't found.
where winget >nul 2>&1
if errorlevel 1 goto no_winget
choice /m "Install Python 3.12 now (from python.org, via winget)"
if errorlevel 2 goto no_python
winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements
call :try_py "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto make_venv
echo Python install didn't complete.
goto no_python

:no_winget
echo winget isn't available on this PC to install it automatically.
:no_python
echo.
echo Install Python 3.12 from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo then run setup.bat again.
goto fail

:make_venv
echo Creating the app's Python environment (.venv) with %PY%...
%PY% -m venv .venv
if errorlevel 1 goto fail

:have_venv
echo Installing/updating Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet --disable-pip-version-check
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet --disable-pip-version-check --only-binary=:all:
if errorlevel 1 (
    echo Package install failed. Usually either:
    echo   - no internet connection, or
    echo   - "filename too long" above: move this folder somewhere with a shorter path,
    echo     e.g. C:\iRacingMonitor, delete its .venv folder, and run setup.bat again.
    goto fail
)
echo.
".venv\Scripts\python.exe" -m vrmon.setup %*
echo.
pause
exit /b 0

:fail
echo.
echo Setup did not finish.
pause
exit /b 1

rem --- call :try_py <command...> - sets PY if that command runs a supported Python
:try_py
%* -c "%VERCHECK%" >nul 2>&1 && set PY=%*
exit /b 0
