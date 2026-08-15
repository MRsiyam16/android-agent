@echo off
rem ---------------------------------------------------------------------------
rem  QA Tester AI - the master agent for the Metaesthetics product.
rem
rem  The four launchers next to this one each bring up ONE stack and open the
rem  cockpit: a phone, an app, a flow graph. This one opens the tier above them.
rem  The master agent has no device of its own - it commissions work across every
rem  app in the product, starts and stops runs on all of them, and brings a
rem  device stack up when it needs one.
rem
rem  So there is nothing to plug in before running this. You tell the agent:
rem
rem      "start the Metaesthetics iPad app"
rem          -> it launches the tunnel, the WebDriverAgent runner and the port
rem             forward (accept the UAC prompt; 30-90 seconds; keep the iPad
rem             unlocked), then tells you which modules it can run.
rem
rem      "start the clinic web project"
rem          -> nothing to launch - a browser is started per run - so it checks
rem             Playwright and Chromium are installed and says so.
rem
rem      "also start the patient app on my Android"
rem          -> makes sure adb's daemon is up and the phone is authorised.
rem
rem      "run the booking module on clinic-web and the search module on the iPad"
rem          -> both at once. Different targets do not queue behind each other.
rem
rem  The one thing that cannot run in parallel is two iOS devices: there is a
rem  single WebDriverAgent port, so the iPad and the iPhone would fight over it.
rem  The agent will say so rather than starting the second.
rem
rem  If a server is already running (you double-clicked one of the other
rem  launchers earlier) this attaches to it instead of taking the port - opening
rem  the board must never kill a run that is in progress. In that case closing
rem  this window changes nothing. If it started the server itself, it says so,
rem  and closing the window stops the server.
rem ---------------------------------------------------------------------------

setlocal
title QA Tester AI - master agent (Metaesthetics)
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
echo  Opening the Metaesthetics master agent...
echo  The board will open at http://localhost:8000/manager
echo.

%PY% start_master.py --ecosystem metaesthetics %*
set EXITCODE=%ERRORLEVEL%

rem A non-zero exit means it never got going - no such product, or the port is
rem held by something that is not answering. Hold the window open so the reason
rem is readable instead of vanishing on a double-click.
if not "%EXITCODE%"=="0" (
  echo.
  echo  Exited with code %EXITCODE% - see the message above.
  echo.
  pause
)
endlocal
