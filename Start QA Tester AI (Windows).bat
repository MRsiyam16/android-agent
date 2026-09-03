@echo off
rem ---------------------------------------------------------------------------
rem  QA Tester AI - double-click launcher for a Windows desktop app.
rem
rem  Unlike the iPhone launcher next to this one, there is no stack to bring up
rem  before the dashboard starts: a Windows target lives in a VirtualBox VM that
rem  WindowsDevice boots itself, lazily, the first time a Windows project's
rem  module actually runs - not here. This launcher only confirms VirtualBox
rem  itself is present, then starts the server exactly as the Android one does.
rem
rem  One-time VM setup (create the VM, install Windows, Guest Additions,
rem  auto-logon, the in-guest control agent) is not done here and cannot be -
rem  see app/docs/WINDOWS_SETUP.md.
rem
rem  Keep THIS window open: it is the server. Closing it, or pressing Ctrl+C,
rem  stops the dashboard.
rem ---------------------------------------------------------------------------

setlocal
title QA Tester AI - server (Windows)
cd /d "%~dp0app"

rem App labels are not all Latin and the Windows console is cp1252; without this a
rem non-ASCII label raises UnicodeEncodeError and takes the whole run down.
set PYTHONIOENCODING=utf-8

rem Prefer the launcher, fall back to python on PATH.
set PY=
where py >nul 2>&1 && set PY=py
if not defined PY (
  where python >nul 2>&1 && set PY=python
)
if not defined PY (
  echo.
  echo  Python was not found on PATH.
  echo  Install Python 3.8+ and tick "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

rem VBoxManage is the whole VM lifecycle - boot, snapshot restore, reading a guest's
rem IP. Checked here rather than left to a stack trace mid-run, since "VirtualBox
rem not installed" and "VM not set up yet" would otherwise look like the same failure
rem further in. Checks the default install path first (config.VBOXMANAGE_PATH's own
rem default), then PATH, then lets VBOXMANAGE_PATH override either if already set.
if not defined VBOXMANAGE_PATH (
  if exist "%ProgramFiles%\Oracle\VirtualBox\VBoxManage.exe" (
    set "VBOXMANAGE_PATH=%ProgramFiles%\Oracle\VirtualBox\VBoxManage.exe"
  )
)
if not defined VBOXMANAGE_PATH (
  where VBoxManage >nul 2>&1 && set VBOXMANAGE_PATH=VBoxManage
)
if not defined VBOXMANAGE_PATH (
  echo.
  echo  VirtualBox was not found.
  echo  Install it from https://www.virtualbox.org/, or set VBOXMANAGE_PATH to
  echo  wherever VBoxManage.exe actually lives.
  echo  See app\docs\WINDOWS_SETUP.md for the one-time VM setup this all assumes.
  echo.
  pause
  exit /b 1
)

echo.
echo  Starting QA Tester AI for a Windows desktop app...
echo  A browser tab will open at http://localhost:8000
echo.
echo  A Windows project's VM boots itself the first time its module runs - the
echo  first action against a fresh VM can take a couple of minutes. See
echo  app\docs\WINDOWS_SETUP.md if it never comes up.
echo.

rem No start_windows.py: nothing needs to be brought up ahead of the dashboard the
rem way iOS's tunnel/runner/forward do. start.py is the whole story here.
%PY% start.py %*
set EXITCODE=%ERRORLEVEL%

rem A non-zero exit means the server never got going (port already held by
rem something else, or a crash on startup). Hold the window open so the reason
rem stays readable instead of vanishing on a double-click.
if not "%EXITCODE%"=="0" (
  echo.
  echo  Exited with code %EXITCODE% - see the message above.
  echo.
  pause
)
endlocal
