@echo off
rem ---------------------------------------------------------------------------
rem  QA Tester AI — double-click launcher.
rem
rem  Starts the dashboard server, opens it in the default browser, and pre-warms a
rem  Claude Code session for the module you used last, so the first thing you type
rem  to the agent does not wait for the CLI to spawn.
rem
rem  Keep this window open: it *is* the server. Closing it, or pressing Ctrl+C,
rem  stops the dashboard.
rem ---------------------------------------------------------------------------

setlocal
title QA Tester AI - server
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
echo  Starting QA Tester AI...
echo  A browser tab will open at http://localhost:8000
echo  Close this window when you are done.
echo.

rem start.py does the rest: it refuses to double-bind the port, reports the device and
rem agent state, opens the browser, and blocks until you stop it.
%PY% start.py
set EXITCODE=%ERRORLEVEL%

rem A non-zero exit means it never got going (port already held by something else, or a
rem crash on startup). Hold the window open so the reason is readable instead of the
rem window vanishing on a double-click.
if not "%EXITCODE%"=="0" (
  echo.
  echo  The server exited with code %EXITCODE% - see the message above.
  echo.
  pause
)
endlocal
