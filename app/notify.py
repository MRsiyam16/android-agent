"""A Windows notification, for the times a job stops and needs a human.

The dashboard already shows this — an amber strip, a reason, the module it stopped at. That is
enough when the page is open. It is worth nothing at 2am with the tab closed, which is exactly
when an unattended sweep hits a locked iPad and then waits eight hours for somebody to notice.

So this is the out-of-band half. Deliberately narrow:

* **Only when a person is actually needed.** A notification for every module that finished is a
  notification you turn off within a day, and then the one that mattered is off too.
* **Never fatal, never blocking.** It runs in a thread and swallows everything. A machine with
  notifications disabled, a locked-down PowerShell policy, a non-Windows host — none of that is
  a reason to stop testing.
* **No new dependency.** `win10toast` and friends are one more thing to install and to break on
  a Python upgrade. PowerShell is already required by the iOS launcher.

Two mechanisms, tried in order: the real toast API, then a tray balloon, which works on hosts
where the toast API refuses without an installed app identity.
"""
from __future__ import annotations

import logging
import subprocess
import threading

import config

logger = logging.getLogger("notify")

#: Shown as the toast's source. A registered AppUserModelID would be tidier; this one is
#: present on every Windows install, which matters more than the label being exact.
_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:QA_TITLE)) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:QA_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:QA_APPID).Show($toast)
"""

_BALLOON = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Information
$icon.BalloonTipTitle = $env:QA_TITLE
$icon.BalloonTipText = $env:QA_BODY
$icon.Visible = $true
$icon.ShowBalloonTip(20000)
Start-Sleep -Seconds 12
$icon.Dispose()
"""


def _run(script: str, title: str, body: str) -> bool:
    """Run a PowerShell script with the text passed through the environment.

    Through the environment rather than interpolated into the script: the body is a defect
    title or a question an agent wrote, and quoting arbitrary text into PowerShell source is
    how a stray backtick or `$(` turns a notification into an execution.
    """
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
            env={**_env(), "QA_TITLE": title, "QA_BODY": body, "QA_APPID": _APP_ID})
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("notification failed: %s", exc)
        return False


def _env() -> dict[str, str]:
    import os

    return dict(os.environ)


def send(title: str, body: str) -> None:
    """Show a notification. Returns immediately; never raises."""
    if not config.AGENT_DESKTOP_NOTIFICATIONS:
        return

    # Trimmed rather than wrapped: a toast shows two lines and silently truncates the rest, so
    # the useful half has to be at the front.
    text = " ".join(str(body or "").split())[:240]
    head = " ".join(str(title or "QA Tester AI").split())[:64]

    def _go() -> None:
        if _run(_TOAST, head, text):
            return
        if _run(_BALLOON, head, text):
            return
        logger.info("could not show a desktop notification (%s) — the dashboard still has it",
                    head)

    threading.Thread(target=_go, name="notify", daemon=True).start()
