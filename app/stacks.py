"""Bringing a platform's device stack up — the thing that has to exist before a run can start.

`device_locks` answers "may this run take the target?". This answers the question underneath
it: **is there a target at all?** They are different failures and they look nothing alike. A
busy lock refuses in a sentence. A missing stack does not refuse — the run starts, the first
device tool times out, and the transcript fills with an agent reasoning about why the app is
not responding when the truth is that WebDriverAgent was never launched.

Each platform needs a different amount of nothing:

* **web** — nothing to start. Chromium is launched per project by `web_device.WebDevice` on
  demand. What can be wrong is that Playwright or its browser binary is not installed, which
  is a one-time fact about the machine, so it is checked once and cached.
* **android** — one background daemon (`adb start-server`) that adb starts for itself the
  first time anything asks. Effectively always ready; the real question is whether a phone is
  plugged in and authorised.
* **ios** — three long-lived processes, a signed test runner already on the device, and a UAC
  prompt. This is the one that genuinely has to be *started*, and it is started by handing the
  work to `start_ios.py --stack-only`, which already knows the dependency order and every way
  each step fails. Duplicating that ladder here would give us a second, worse copy of it.

**One WebDriverAgent port, one iOS device.** `config.WDA_URL` is a single fixed address, so an
iPhone and an iPad cannot both be driven at once — the second stack would bind the same port
and the runs would silently share a phone. That is stated everywhere it matters rather than
guarded against, because the fix is per-project WDA ports (`IOSDevice(wda_url=...)` already
takes one) and not a lock. iPad + web + Android concurrently is fine; two iOS devices is not.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import config

logger = logging.getLogger("stacks")

ROOT = Path(__file__).resolve().parent

ANDROID = "android"
IOS = "ios"
WEB = "web"
PLATFORMS = (ANDROID, IOS, WEB)

#: The browser check launches a real Chromium and closes it again — about a second, and the
#: answer cannot change without someone running an installer. Cached for the process.
_web_check: dict[str, Any] = {"done": False, "ready": False, "detail": "", "fix": ""}

#: iOS stacks this process has spawned, by UDID. Kept so `start` can say "already starting"
#: rather than stacking a second UAC prompt on top of a tunnel that is 20 seconds from ready.
_ios_starting: dict[str, float] = {}

#: How long a spawned iOS stack is considered "still coming up" before another start is
#: allowed. Longer than the tunnel takes, shorter than a user's patience.
IOS_START_GRACE_SECONDS = 240.0


def _row(platform: str, ready: bool, detail: str, *, fix: str = "",
         devices: Optional[list[dict[str, str]]] = None,
         starting: bool = False) -> dict[str, Any]:
    return {"platform": platform, "ready": ready, "detail": detail, "fix": fix,
            "devices": devices or [], "starting": starting}


# -- what is there right now ----------------------------------------------------------------

def _android_status() -> dict[str, Any]:
    """An emulator counts. `adb devices` is the whole answer, and it does not distinguish.

    `emulator-5554` in state `device` is an Android target that can be launched into, tapped,
    dumped and screenshotted — every adapter in this codebase talks to it through the same
    `-s <serial>` that a cable gives. Treating "no phone plugged in" as "Android is down" was
    wrong the moment the verifier started running patched builds on an AVD: the run would be
    refused with a fix telling somebody to find a USB cable for a device already up.

    So each device is stamped `emulator` and the detail says which, because the distinction
    still matters to a *person* choosing where to run something — an AVD cannot vouch for a
    camera, a fingerprint, a push notification or how the app performs on real hardware.
    """
    import adb_device
    import emulator

    try:
        serials = adb_device.list_serials()
    except adb_device.DeviceError as exc:
        return _row(ANDROID, False, f"adb is not usable: {exc}",
                    fix="Install the Android platform-tools and put adb on PATH, or set "
                        "ADB_PATH in app/.env.")
    devices = [{**adb_device.describe_serial(s), "emulator": emulator.is_emulator(s)}
               for s in serials]
    if not devices:
        return _row(ANDROID, False, "adb works, but no phone or emulator is attached.",
                    fix="Plug the phone in, unlock it, and accept the USB-debugging prompt — "
                        "`adb devices` should then list it as `device`, not `unauthorized`. "
                        "For a fix-verification run that does not need real hardware, start "
                        "the emulator instead: `python emulator.py --ensure` (headless AVD "
                        f"{config.ANDROID_AVD_NAME}, a minute or two to boot).")
    physical = [d for d in devices if not d["emulator"]]
    detail = (f"{len(devices)} device(s): "
              + ", ".join(f"{d.get('label') or d['serial']}"
                          + (" [emulator]" if d["emulator"] else "") for d in devices))
    if not physical:
        detail += (" — emulator only, which is fine for most runs but cannot vouch for the "
                   "camera, biometrics, push notifications or performance.")
    return _row(ANDROID, True, detail, devices=devices)


def _ios_status() -> dict[str, Any]:
    """Ready means WebDriverAgent answers, not that a phone is plugged in.

    The distinction is the whole reason this module exists: a trusted, unlocked iPad with no
    runner running is indistinguishable from a working one until the first tap fails.
    """
    try:
        import ios_device
        from start_ios import wda_ready
    except ImportError as exc:
        return _row(IOS, False, f"the iOS adapter cannot be imported: {exc}",
                    fix="pip install pymobiledevice3 requests pillow")

    try:
        serials = ios_device.list_serials()
    except Exception as exc:  # noqa: BLE001 - a broken toolchain is a status, not a crash
        return _row(IOS, False, f"could not list iOS devices: {exc}",
                    fix="Check that `pymobiledevice3 usbmux list` runs at all — see "
                        "app/docs/IOS_SETUP.md.")
    devices = [ios_device.describe_serial(s) for s in serials]

    ready, message = wda_ready()
    starting = any(time.monotonic() - at < IOS_START_GRACE_SECONDS
                   for at in _ios_starting.values())
    if ready:
        return _row(IOS, True, f"WebDriverAgent is answering at {config.WDA_URL}",
                    devices=devices)
    if not devices:
        return _row(IOS, False, "no iOS device is attached over usbmux.",
                    fix="Plug the iPad in, unlock it, and leave it unlocked. If it still does "
                        "not appear, run `Start QA Tester AI (iPad).bat` once — it diagnoses "
                        "each layer (drivers, usbmuxd, USB, pairing) and names the one that "
                        "is broken.",
                    starting=starting)
    return _row(IOS, False,
                f"{len(devices)} device(s) attached but WebDriverAgent is not answering "
                f"({message or 'no answer'}).",
                fix="Start the stack — tunnel, runner and port forward. This asks for "
                    "Administrator (the tunnel creates a network interface).",
                devices=devices, starting=starting)


def _web_status() -> dict[str, Any]:
    if not _web_check["done"]:
        _web_check["done"] = True
        try:
            import playwright  # noqa: F401
        except ImportError as exc:
            _web_check.update(ready=False, detail=f"Playwright is not installed: {exc}",
                              fix="pip install playwright")
        else:
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    pw.chromium.launch().close()
            except Exception as exc:  # noqa: BLE001 - anything here means "not ready"
                first = str(exc).splitlines()[0] if str(exc) else "launch failed"
                _web_check.update(ready=False, detail=f"Chromium will not launch: {first}",
                                  fix="playwright install chromium")
            else:
                _web_check.update(
                    ready=True,
                    detail="Playwright and Chromium are installed. Nothing to start — a "
                           "browser is launched per project when a run begins.",
                    fix="")
    return _row(WEB, bool(_web_check["ready"]), str(_web_check["detail"]),
                fix=str(_web_check["fix"]))


def status(platform: str) -> dict[str, Any]:
    """Whether this platform can accept a run right now, and what to do if not."""
    kind = (platform or "").lower()
    if kind == ANDROID:
        return _android_status()
    if kind == IOS:
        return _ios_status()
    if kind == WEB:
        return _web_status()
    return _row(kind or "unknown", False, f"unknown platform {platform!r}",
                fix=f"Platforms are: {', '.join(PLATFORMS)}.")


def status_all(platforms: Optional[list[str]] = None) -> list[dict[str, Any]]:
    return [status(p) for p in (platforms or list(PLATFORMS))]


# -- starting one ---------------------------------------------------------------------------

def _spawn_ios_stack(udid: Optional[str]) -> None:
    """Hand the ladder to `start_ios.py`, in its own console window.

    Its own window because the stack it starts is three more windows and a UAC prompt, and
    because the runner log it streams is the only place the real error appears when a
    certificate has expired. A pipe would swallow exactly the thing you need to read.
    """
    args = [sys.executable, "start_ios.py", "--stack-only", "--wait", "0"]
    if udid:
        args += ["--udid", udid]
    inner = subprocess.list2cmdline(args)
    command = (f'title QA Tester AI - iOS stack & cd /d "{ROOT}" & {inner} '
               f'& echo. & echo [stack launcher exited] & pause')
    subprocess.Popen(command, shell=True, creationflags=subprocess.CREATE_NEW_CONSOLE)


def start(platform: str, serial: Optional[str] = None,
          wait_seconds: float = 0.0) -> dict[str, Any]:
    """Bring a platform's stack up, and report what actually happened.

    Never raises. Every failure here is a condition someone has to fix on a desk — a cable, a
    UAC prompt, an expired certificate — so the answer is always a status with a next step in
    it rather than an exception the caller has to translate.

    `wait_seconds` polls for readiness after starting. Zero returns immediately, which is what
    a chat tool wants: the iOS stack takes 30-90 seconds and a tool call that blocks that long
    reads as a hung agent.
    """
    kind = (platform or "").lower()
    current = status(kind)
    if current["ready"]:
        return {**current, "started": False, "note": "Already up — nothing was started."}

    if kind == WEB:
        # Nothing to start, and saying so is the honest answer. A "started" web stack that
        # merely re-ran a failing import would read as progress.
        return {**current, "started": False,
                "note": "There is nothing to start for the web: Chromium is launched per "
                        "project when a run begins. This is a machine-setup problem, and the "
                        "fix above is the whole of it."}

    if kind == ANDROID:
        try:
            subprocess.run([config.ADB_PATH, "start-server"],
                           capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return {**current, "started": False, "note": f"adb start-server failed: {exc}"}
        after = _android_status()
        return {**after, "started": True,
                "note": "adb's daemon is up. Anything still missing is the phone itself — "
                        "adb starts its own daemon on demand, so this is rarely the problem."}

    if kind == IOS:
        if not current["devices"] and not serial:
            return {**current, "started": False,
                    "note": "Nothing was started: there is no iOS device to start it for."}
        target = serial or (current["devices"][0]["serial"] if current["devices"] else None)
        recent = _ios_starting.get(str(target), 0.0)
        if time.monotonic() - recent < IOS_START_GRACE_SECONDS:
            return {**current, "started": False, "starting": True,
                    "note": f"A stack for {target} was already launched "
                            f"{int(time.monotonic() - recent)}s ago and is still coming up. "
                            f"Give it a moment rather than stacking a second UAC prompt on it."}
        _ios_starting[str(target)] = time.monotonic()
        try:
            _spawn_ios_stack(target)
        except OSError as exc:
            _ios_starting.pop(str(target), None)
            return {**current, "started": False, "note": f"Could not launch the stack: {exc}"}

        note = ("Launching the iOS stack in its own window: tunnel (accept the UAC prompt), "
                "WebDriverAgent runner, port forward. It takes 30-90 seconds and the iPad must "
                "stay unlocked throughout. The runner window holds the real error if it fails.")
        if wait_seconds > 0:
            from start_ios import wda_ready
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if wda_ready()[0]:
                    return {**_ios_status(), "started": True,
                            "note": "The iOS stack is up and WebDriverAgent is answering."}
                time.sleep(3)
        return {**current, "started": True, "starting": True, "note": note}

    return {**current, "started": False, "note": f"Nothing to start for {platform!r}."}


def forget_web_check() -> None:
    """Re-run the browser check on the next status — for after someone installs Chromium."""
    _web_check["done"] = False
