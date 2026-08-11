@echo off
rem ---------------------------------------------------------------------------
rem  QA Tester AI — double-click launcher for testing a website.
rem
rem  The Android launcher next to this one only has to start the server: adb is
rem  either there or the device is not. A browser is the same kind of simple —
rem  no pairing, no tunnel, no signed test runner — so this just confirms
rem  Playwright and its Chromium binary are actually installed before the
rem  dashboard opens, then starts the server exactly as the others do.
rem
rem  Create a website project from the dashboard's New Project sheet (choose
rem  "Website" and paste the URL) once the server is up.
rem
rem  Keep THIS window open: it is the server. Closing it, or pressing Ctrl+C,
rem  stops the dashboard.
rem ---------------------------------------------------------------------------

setlocal
title QA Tester AI - server (Web)
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

echo.
echo  Starting QA Tester AI for the web...
echo  A browser tab will open at http://localhost:8000
echo  A separate, automated Chromium window is what gets tested — leave it alone
echo  while a test is running.
echo.

rem start_web.py checks Playwright + Chromium are installed, then hands off to
rem start.py exactly as start_ios.py does for an iPhone.
%PY% start_web.py %*
set EXITCODE=%ERRORLEVEL%

rem A non-zero exit means the browser stack was not ready, or the server could not
rem start. Hold the window open so the reason stays readable instead of vanishing
rem on a double-click.
if not "%EXITCODE%"=="0" (
  echo.
  echo  Exited with code %EXITCODE% - see the message above.
  echo.
  pause
)
endlocal
