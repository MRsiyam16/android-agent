@echo off
rem ---------------------------------------------------------------------------
rem  QA Tester AI - the QA Verifier, for Bugmaster's fix pipeline.
rem
rem  This is a SECOND instance of the harness, not a mode of the first one. It
rem  runs on port 8001 with its own notebook (app\verify-projects\), and the
rem  QA Master on 8000 can be running at the same time - that is the point.
rem
rem  What it is for: Bugmaster fixes a bug on a server, cannot reach the phone
rem  on this desk, and needs somebody to check the fix on a real device before
rem  it merges. Its bridge worker sends a message that begins
rem
rem      Bugmaster verification job dj_... for Blackcode #612
rem
rem  the manager here runs exactly one test step on the named app, then reports
rem  pass / fail / blocked, and the worker reads the verdict back from
rem  http://localhost:8001/verifications/<job id>.
rem
rem  Why a separate notebook: the build under test is a patch that is deployed
rem  nowhere. A bug found here means "the fix did not work", not "the shipped
rem  app is broken" - so it must never land on the product board, get clustered
rem  with real findings, or be filed to Blackcode. Two PROJECTS_DIRs is the
rem  whole of that separation.
rem
rem  It REFUSES to start while the QA Master is driving a device. Both reach the
rem  same phone through the same adb, and a device lock cannot be seen across a
rem  port. If the master is simply not running, that is fine.
rem
rem  Nothing to plug in first. Most jobs run on the headless emulator, which the
rem  worker brings up itself; jobs that need real hardware (camera, biometrics,
rem  push, performance) wait for the phone. To start the emulator by hand:
rem
rem      cd app  &  py emulator.py --ensure
rem ---------------------------------------------------------------------------

setlocal
title QA Verifier (Bugmaster)
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

rem The agent itself is the Claude Code CLI. Checked here rather than left to a
rem failed pre-warm, because "not installed" and "not signed in" both surface as
rem the same unhelpful spawn error further in.
where claude >nul 2>&1
if errorlevel 1 (
  echo.
  echo  The Claude Code CLI was not found on PATH.
  echo  Install it with:  npm i -g @anthropic-ai/claude-code
  echo  Then run `claude` once and sign in with your subscription.
  echo.
  pause
  exit /b 1
)

echo.
echo  Opening the QA Verifier...
echo  The board will open at http://localhost:8001/manager
echo.

%PY% start_verifier.py %*
set EXITCODE=%ERRORLEVEL%

rem A non-zero exit means it never got going - the master is holding a device,
rem or the port is held by something that is not answering. Hold the window open
rem so the reason is readable instead of vanishing on a double-click.
if not "%EXITCODE%"=="0" (
  echo.
  echo  Exited with code %EXITCODE% - see the message above.
  echo.
  pause
)
endlocal
