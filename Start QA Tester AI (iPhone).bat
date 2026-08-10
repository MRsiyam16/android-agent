@echo off
rem ---------------------------------------------------------------------------
rem  QA Tester AI - double-click launcher for an iPhone.
rem
rem  The Android launcher next to this one only has to start the server: adb is
rem  either there or the device is not. An iPhone needs a tunnel, a code-signed
rem  XCTest runner on the device and a forwarded port before a single tap can be
rem  sent, so this brings those up first, waits until WebDriverAgent itself
rem  reports ready, and only then starts the dashboard.
rem
rem  Three extra console windows will open - the tunnel (elevated, so expect a
rem  UAC prompt), the WDA runner and the port forward. Leave them open; they are
rem  the stack. The runner window streams the log that says what actually went
rem  wrong when something does.
rem
rem  Keep THIS window open too: it is the server, exactly as on Android.
rem
rem  One-time phone setup is not done here and cannot be - see app/docs/IOS_SETUP.md.
rem ---------------------------------------------------------------------------

setlocal
title QA Tester AI - server (iPhone)
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
rem here rather than left to a stack trace, because "not installed" and "phone not
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
echo  Starting QA Tester AI for iPhone...
echo.
echo  Before this can work the phone must be UNLOCKED and trusted, with
echo  Auto-Lock set to Never. A locked phone returns black screenshots and
echo  fails every action underneath, which reads exactly like a broken app.
echo.

rem start_ios.py does the rest: finds the phone, resolves the runner bundle id that
rem is actually installed, starts the three processes in dependency order, polls
rem WebDriverAgent's /status, and then hands off to start.py.
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
