@echo off
rem ---------------------------------------------------------------------------
rem  QA Tester AI - double-click launcher for an iPad.
rem
rem  Identical stack to the iPhone launcher next to this one: start_ios.py talks
rem  to pymobiledevice3 and WebDriverAgent, neither of which cares whether the
rem  device on the other end is an iPhone or an iPad. This file exists only so
rem  the iPad has its own icon and title bar rather than sharing the iPhone's.
rem
rem  Three extra console windows will open - the tunnel (elevated, so expect a
rem  UAC prompt), the WDA runner and the port forward. Leave them open; they are
rem  the stack. The runner window streams the log that says what actually went
rem  wrong when something does.
rem
rem  Keep THIS window open too: it is the server, exactly as on Android.
rem
rem  One-time iPad setup (pairing, Developer Mode, sideloading + signing
rem  WebDriverAgentRunner) is not done here and cannot be - see
rem  app/docs/IOS_SETUP.md. That guide was written against an iPhone but every
rem  step is the same for an iPad; nothing in it is iPhone-specific.
rem ---------------------------------------------------------------------------

setlocal
title QA Tester AI - server (iPad)
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

rem pymobiledevice3 is the whole transport - pairing, tunnel, runner launch. Checked
rem here rather than left to a stack trace, because "not installed" and "device not
rem trusted" produce very similar-looking failures further in.
where pymobiledevice3 >nul 2>&1
if errorlevel 1 (
  echo.
  echo  pymobiledevice3 was not found on PATH.
  echo  Install it with:  pip install pymobiledevice3
  echo  See app\docs\IOS_SETUP.md - on Windows the lzfse/pylzss wheels need a
  echo  workaround, which that document spells out.
  echo.
  pause
  exit /b 1
)

echo.
echo  Starting QA Tester AI for iPad...
echo.
echo  Before this can work the iPad must be UNLOCKED and trusted, with
echo  Auto-Lock set to Never. A locked iPad returns black screenshots and
echo  fails every action underneath, which reads exactly like a broken app.
echo.
echo  First time with this iPad? It needs the one-time setup in
echo  app\docs\IOS_SETUP.md (pairing, Developer Mode, sideloading + signing
echo  WebDriverAgentRunner) before this launcher can drive it.
echo.

rem start_ios.py does the rest: finds the device (whichever single iOS device is
rem attached - it does not distinguish iPhone from iPad), resolves the runner
rem bundle id that is actually installed, starts the three processes in
rem dependency order, polls WebDriverAgent's /status, and then hands off to
rem start.py.
%PY% start_ios.py %*
set EXITCODE=%ERRORLEVEL%

rem A non-zero exit means the stack never came up, or the server could not start.
rem Hold the window open so the reason stays readable instead of vanishing on a
rem double-click.
if not "%EXITCODE%"=="0" (
  echo.
  echo  Exited with code %EXITCODE% - see the message above.
  echo  The WDA runner window, if it opened, holds the real error.
  echo.
  pause
)
endlocal
