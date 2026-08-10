"""One command to bring the iPhone stack up: `python start_ios.py`

`start.py` is enough on Android because adb is either there or the device is not. An iPhone
needs three long-lived processes and a code-signed test runner already on the device before a
single tap can be sent, and each of them fails in a way that looks like a different bug from
inside a test — a dropped socket, a black screenshot, a launch that "succeeds" and does
nothing. So this starts them in dependency order, checks the thing each one was supposed to
achieve, and refuses to hand over to the dashboard until WebDriverAgent itself reports ready.

What it does NOT do is set the phone up. Apple drivers, pymobiledevice3, Developer Mode with
UI Automation, and a WebDriverAgentRunner whose *nested* `.xctest` is signed (Sideloadly alone
does not do that) are one-time work, documented in `docs/IOS_SETUP.md`. Nothing here can
substitute for any of it. What it can do is say which one is missing instead of leaving you
with `[]` and no next step.

Each long-lived process gets its own console window, deliberately: `dvt xcuitest` streams the
runner log, and that stream is the only proof WDA is actually up rather than merely launched.
Closing a window stops that piece of the stack.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import config
from start import port_owner

DOC = "docs/IOS_SETUP.md"

# tunneld's fixed port. Not configurable in pymobiledevice3 10.3.0, and used here only to
# answer "is it already up?" so a second launcher does not stack a second UAC prompt.
TUNNELD_PORT = 49151


def wda_port() -> int:
    """The forwarded port, taken from WDA_URL so the forward always matches the client."""
    return urlparse(config.WDA_URL).port or 8100


# -- probes ------------------------------------------------------------------------

def wda_ready() -> tuple[bool, str]:
    """Ask WebDriverAgent whether it can accept commands.

    The one check worth trusting: the runner process existing proves nothing, and neither
    does the forward accepting a connection. `IOSDevice.ensure_ready()` asks the same
    question at the start of a run — asking it here means a failure lands in a launcher with
    the fixing command next to it, not halfway through a test.
    """
    try:
        with urllib.request.urlopen(f"{config.WDA_URL}/status", timeout=4) as resp:
            body = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return False, "no answer"
    value = body.get("value", body) if isinstance(body, dict) else {}
    if not isinstance(value, dict):
        return False, "unexpected /status payload"
    return bool(value.get("ready")), str(value.get("message", ""))


def ios_devices() -> list[dict[str, str]]:
    """Attached iPhones, via the adapter's own listing so both agree on what is connected."""
    import ios_device
    return [ios_device.describe_serial(s) for s in ios_device.list_serials()]


# -- why is the phone not there? ---------------------------------------------------
#
# `usbmux list` returning [] is the single most common way this stack fails, and on its own
# it says nothing about which of five unrelated things is wrong. Each layer below is checked
# separately so the launcher can name the one that is actually broken instead of printing a
# list of everything that has ever gone wrong for anyone.
#
# The order is the dependency chain: drivers installed -> service socket up -> Windows sees
# the USB device -> the phone has been trusted. The first one that fails is the answer, and
# fixing a later one before an earlier one has no effect.

LOCKDOWN_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                            "Apple", "Lockdown")

# usbmuxd's fixed port on Windows. Owned by AppleMobileDeviceService; if nothing is
# listening, the driver package is installed but its socket is not up.
USBMUXD_PORT = 27015


def _powershell(script: str, timeout: float = 30) -> str | None:
    """Run a PowerShell one-liner, or None if it could not run at all."""
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout or ""


def amds_state() -> str:
    """'running' | 'stopped' | 'missing' | 'unknown' for Apple Mobile Device Service.

    Note that 'stopped' here is weaker evidence than it looks: `docs/IOS_SETUP.md` records
    machines where AMDS works correctly but never signals RUNNING to the service manager and
    sits in START_PENDING forever. That is why the usbmuxd socket is checked separately —
    the socket is the thing that actually has to be up.
    """
    try:
        done = subprocess.run(["sc", "query", "Apple Mobile Device Service"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    out = f"{done.stdout}{done.stderr}"
    if "does not exist" in out or done.returncode == 1060:
        return "missing"
    if "RUNNING" in out:
        return "running"
    if "PENDING" in out or "STOPPED" in out:
        return "stopped"
    return "unknown"


def apple_usb_devices() -> list[str] | None:
    """Apple USB devices Windows currently has enumerated, or None if unknowable.

    This separates "the cable/port is dead" from "the phone is not trusted": a charge-only
    cable or a flaky front-panel header shows nothing here at all, while an untrusted phone
    enumerates perfectly and only fails later, at pairing.
    """
    out = _powershell(
        "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
        "Where-Object { $_.FriendlyName -match 'Apple Mobile Device|iPhone' } | "
        "Select-Object -ExpandProperty FriendlyName")
    if out is None:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def pairing_records() -> list[str] | None:
    """UDIDs this machine has a pairing record for, or None if the folder is unreadable.

    `SystemConfiguration.plist` is always present and is not a pairing record — a folder
    holding only that one is the signature of a Trust prompt that was never completed.
    """
    try:
        names = os.listdir(LOCKDOWN_DIR)
    except (OSError, PermissionError):
        return None
    return [n[:-6] for n in names
            if n.lower().endswith(".plist") and n != "SystemConfiguration.plist"]


def store_app_installed() -> bool:
    """Whether the Microsoft Store 'Apple Devices' app is present.

    Worth calling out only because it looks like it should be enough and is not: it is
    sandboxed and ships no transport third-party tooling can use. People install it, see
    their phone appear in it, and reasonably conclude the drivers are fine.
    """
    out = _powershell("if (Get-AppxPackage -Name '*AppleInc.AppleDevices*' "
                      "-ErrorAction SilentlyContinue) { 'yes' }")
    return bool(out and "yes" in out)


def explain_no_device() -> None:
    """Check each layer and print the one next step that will actually change something."""
    print("\n  Working out why...\n")

    service = amds_state()
    socket_up = bool(port_owner(USBMUXD_PORT))
    usb = apple_usb_devices()
    paired = pairing_records()

    def row(ok: bool | None, label: str, detail: str) -> None:
        print(f"    [{ {True: 'ok  ', False: 'FAIL', None: '??  '}[ok] }] {label:<38}{detail}")

    row(service in ("running", "stopped"),
        "Apple Mobile Device Support installed", service)
    row(socket_up,
        f"usbmuxd listening on 127.0.0.1:{USBMUXD_PORT}",
        "bound" if socket_up else "nothing bound")
    row(None if usb is None else bool(usb),
        "Windows sees the iPhone on USB",
        f"{len(usb)} device(s)" if usb is not None else "could not check")
    row(None if paired is None else bool(paired),
        "this PC is trusted by a phone",
        f"{len(paired)} pairing record(s)" if paired is not None else "could not check")

    print("\n  Do this next:\n")

    if service == "missing":
        print("    The Apple driver package is not installed. Nothing can see the phone")
        print("    without it:")
        print("        winget install --id Apple.AppleMobileDeviceSupport")
        if store_app_installed():
            print("\n    You do have the Microsoft Store 'Apple Devices' app. That is not the")
            print("    same thing and does not help here - it is sandboxed and ships no")
            print("    transport this tooling can use. Install the package above as well.")
        print("\n    If the installer fails with 1603, run it right after a reboot")
        print(f"    (see {DOC}, 'AMDS never reports RUNNING').")

    elif not socket_up:
        print("    The drivers are installed but usbmuxd is not listening, so every lookup")
        print("    fails before it reaches the phone. Restart the service from an")
        print("    Administrator PowerShell:")
        print('        Restart-Service "Apple Mobile Device Service"')
        print("\n    Then replug the phone and run this launcher again.")

    elif usb is not None and not usb:
        print("    Windows is not enumerating the phone at all, which makes this a cable or")
        print("    port problem rather than anything to do with iOS:")
        print("      * use a rear-panel USB port - not a front header, not a hub")
        print("      * use a data cable; charge-only cables enumerate nothing")
        print("      * try the other end of the cable, and a different port")
        print("\n    The phone should chime and show the charging indicator when it is")
        print("    seen properly.")

    elif paired is not None and not paired:
        print("    Windows sees the phone, but this PC has never been trusted by it - there")
        print("    is no pairing record. On the phone:")
        print("      1. unlock it and keep it unlocked")
        print("      2. replug the cable")
        print("      3. tap Trust on the 'Trust This Computer?' prompt, then enter the")
        print("         passcode")
        print("\n    The prompt only appears while the phone is unlocked, which is why a")
        print("    locked phone seems to do nothing at all.")

    else:
        # Every layer checks out, including a pairing record from a previous session. What
        # is left is a phone that is locked right now, or a trust relationship the phone has
        # since dropped — neither is visible from the host as anything but an empty list.
        print("    Every layer on this PC checks out, including a pairing record from an")
        print("    earlier session - so the phone itself is refusing the connection. In")
        print("    order:")
        print("      1. Unlock the phone and LEAVE it unlocked, then replug the cable.")
        print("         A locked phone answers nothing over usbmux, which looks identical")
        print("         to an unplugged one.")
        print("      2. Watch for 'Trust This Computer?' and tap Trust + passcode. If it")
        print("         appears, the phone had dropped the pairing.")
        print("      3. If no prompt appears and it still is not found, the stale pairing")
        print("         is blocking a new one. On the phone:")
        print("             Settings > General > Transfer or Reset iPhone")
        print("               > Reset > Reset Location & Privacy")
        print("         then replug and tap Trust. This resets privacy prompts only - it")
        print("         erases no data.")
        print("      4. Still nothing: try the other USB port and cable before anything")
        print("         more drastic.")
        print("\n    While you are there, set Auto-Lock to Never")
        print("    (Settings > Display & Brightness) - the phone re-locking mid-run breaks")
        print("    a session the same way.")

    print(f"\n  Anything stranger than this is in {DOC}.")


def wait_for_phone(timeout: float) -> list[dict[str, str]]:
    """Poll until an iPhone shows up, so the fix can be applied without re-running.

    The whole point of the steps above is that they are performed *on the phone*, with the
    launcher already open — re-running it after every attempt turns a 20-second fix into a
    guessing game.
    """
    print(f"\n  Watching for the phone for up to {int(timeout)}s - do the above now and it")
    print("  will be picked up automatically. Ctrl+C to give up.\n")
    print("  waiting ", end="", flush=True)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            time.sleep(3)
            try:
                found = ios_devices()
            except Exception:  # noqa: BLE001 - a mid-replug failure is expected, keep going
                found = []
            if found:
                print(" found it")
                return found
            print(".", end="", flush=True)
    except KeyboardInterrupt:
        print("\n  Stopped.")
        return []
    print(" gave up")
    return []


def find_runner_bundle(udid: str) -> tuple[str | None, list[str]]:
    """Resolve the WebDriverAgentRunner bundle id actually installed on this phone.

    Worth the several seconds it costs. A free Apple ID cannot claim Facebook's bundle id, so
    Sideloadly appends the signing team id and the configured default
    (`com.facebook.WebDriverAgentRunner.xctrunner`) is almost never the installed one — and
    launching a bundle id that does not exist fails with the same `Error code: 2` as an
    expired certificate, which sends you looking at the wrong thing entirely.

    Returns (resolved id or None, every runner-looking id found).
    """
    import ios_device
    try:
        apps = ios_device._pmd_json("apps", "list", "--udid", udid, timeout=90) or {}
    except Exception:  # noqa: BLE001 - listing is a convenience; never fail the launch on it
        return None, []
    if not isinstance(apps, dict):
        return None, []

    if config.WDA_BUNDLE_ID in apps:
        return config.WDA_BUNDLE_ID, [config.WDA_BUNDLE_ID]

    candidates = [b for b in apps if "webdriveragent" in b.lower()]
    return (candidates[0] if len(candidates) == 1 else None), candidates


# -- process launching -------------------------------------------------------------

def spawn_console(title: str, args: list[str]) -> None:
    """Run a long-lived command in its own console window.

    The trailing `pause` is load-bearing: a process that dies on startup (untrusted
    certificate, tunnel refused) otherwise takes its own error message with it when the
    window closes, which is indistinguishable from never having started.
    """
    inner = subprocess.list2cmdline(args)
    command = f'title {title} & {inner} & echo. & echo [{title} exited] & pause'
    subprocess.Popen(command, shell=True, creationflags=subprocess.CREATE_NEW_CONSOLE)


def spawn_elevated(args: list[str]) -> bool:
    """Start a command via UAC. Returns False if the prompt was declined.

    Only `remote tunneld` needs this — it creates a network interface. Elevating the whole
    launcher instead would run the dashboard, the agent and the browser as administrator too,
    which is a much larger blast radius for one process's requirement.
    """
    arglist = ", ".join(f"'{a}'" for a in args[1:])
    script = f"Start-Process -Verb RunAs -FilePath '{args[0]}'" + (
        f" -ArgumentList {arglist}" if arglist else "")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def wait_for(check, timeout: float, label: str) -> bool:
    """Poll `check` until it returns truthy, printing progress on one line."""
    deadline = time.monotonic() + timeout
    print(f"  waiting for {label} ", end="", flush=True)
    while time.monotonic() < deadline:
        if check():
            print(" ok")
            return True
        print(".", end="", flush=True)
        time.sleep(2)
    print(" timed out")
    return False


# -- the stack ---------------------------------------------------------------------

def bring_up(udid: str, bundle_id: str, wda_timeout: float, mount: bool) -> bool:
    """Start tunneld, the XCTest runner and the port forward, in that order.

    The order is a dependency chain, not a preference: `dvt xcuitest --tunnel` cannot find a
    tunnel that is not yet serving, and the Developer Disk Image mount goes over that same
    tunnel on iOS 17+.
    """
    pmd = config.PYMOBILEDEVICE3_PATH

    # 1. tunneld — elevated, and only if nothing already holds the port. Re-running it would
    #    cost a second UAC prompt to produce a process that immediately fails to bind.
    if port_owner(TUNNELD_PORT):
        print(f"  tunnel : already serving on 127.0.0.1:{TUNNELD_PORT}")
    else:
        print("  tunnel : starting `remote tunneld` (accept the UAC prompt) ...")
        if not spawn_elevated([pmd, "remote", "tunneld"]):
            print("           UAC was declined. tunneld must run as Administrator.")
            return False
        if not wait_for(lambda: bool(port_owner(TUNNELD_PORT)), 45, f"port {TUNNELD_PORT}"):
            print(f"           tunneld never bound. Start it by hand and watch it fail:\n"
                  f"             {pmd} remote tunneld")
            return False

    # 2. Developer Disk Image. Persists until the phone reboots, so it is usually a no-op —
    #    but when it is not, every later step fails with an error about the runner instead.
    if mount:
        print("  ddi    : checking the Developer Disk Image ...")
        try:
            done = subprocess.run([pmd, "mounter", "auto-mount"],
                                  capture_output=True, text=True, timeout=180)
            out = f"{done.stdout}{done.stderr}"
            if "already mounted" in out.lower():
                print("           already mounted")
            elif done.returncode == 0:
                print("           mounted")
            else:
                first = next((l.strip() for l in out.splitlines() if l.strip()), "failed")
                print(f"           {first[:160]}")
                print(f"           Not fatal here, but if the runner will not start see {DOC}")
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"           skipped ({exc})")

    # 3. The XCTest runner. This is WDA itself; its window streams the runner log, and
    #    `didBeginExecutingTestPlan` in it is the only real proof the server loop is up.
    print(f"  runner : launching {bundle_id}")
    spawn_console("WDA runner", [pmd, "developer", "dvt", "xcuitest", bundle_id,
                                 "--tunnel", udid])

    # 4. The forward. Started straight away: it is cheap, and it retries on its own until the
    #    runner opens the port on the device.
    port = wda_port()
    print(f"  forward: {port} -> device {port}")
    spawn_console("WDA forward", [pmd, "usbmux", "forward", str(port), str(port),
                                  "--udid", udid])

    print()
    if wait_for(lambda: wda_ready()[0], wda_timeout, f"WebDriverAgent on {config.WDA_URL}"):
        return True

    ready, message = wda_ready()
    print(f"\n  WebDriverAgent did not become ready ({message or 'no answer'}).")
    print("  Check the 'WDA runner' window — the real error is always there:")
    print("    * closes at `_IDE_startExecutingTestPlanWithProtocolVersion`")
    print(f"        -> the nested .xctest is unsigned. See {DOC}, 'The signing trap'.")
    print("    * `Error code: 2, Domain: com.apple.dt.deviceprocesscontrolservice`")
    print("        -> certificate untrusted or expired (free Apple ID lasts 7 days).")
    print("           Settings > General > VPN & Device Management > Trust.")
    print("    * runner starts but every query fails")
    print("        -> Settings > Privacy & Security > Developer Mode > Enable UI Automation.")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Bring the iPhone stack up, then start the QA Tester AI dashboard.")
    ap.add_argument("--udid", help="target a specific iPhone (default: the only one attached)")
    ap.add_argument("--bundle-id", help="WebDriverAgentRunner bundle id (default: WDA_BUNDLE_ID)")
    ap.add_argument("--stack-only", action="store_true",
                    help="bring up tunnel + WDA and stop, without starting the dashboard")
    ap.add_argument("--skip-stack", action="store_true",
                    help="assume WDA is already running; just verify it and start the dashboard")
    ap.add_argument("--no-mount", action="store_true",
                    help="skip the Developer Disk Image check")
    ap.add_argument("--wait", type=float, default=180.0,
                    help="seconds to keep watching for the phone after printing the "
                         "connection steps (0 to exit immediately)")
    ap.add_argument("--wda-timeout", type=float, default=150.0,
                    help="seconds to wait for WebDriverAgent to report ready (default 150)")
    ap.add_argument("--no-browser", action="store_true", help="passed through to start.py")
    ap.add_argument("--force", action="store_true", help="passed through to start.py")
    args = ap.parse_args()

    print("\nQA Tester AI - iPhone\n")

    try:
        import ios_device  # noqa: F401  - imported for the dependency check only
    except ImportError as exc:
        print(f"  The iOS adapter cannot be imported: {exc}")
        print("  Install its dependencies:  pip install pymobiledevice3 requests pillow")
        return 1

    # -- which phone -----------------------------------------------------------
    try:
        found = ios_devices()
    except Exception as exc:  # noqa: BLE001 - a broken toolchain must not raise a traceback
        print(f"  Could not list iPhones: {exc}")
        print(f"  Check `{config.PYMOBILEDEVICE3_PATH} usbmux list` runs at all.")
        return 1

    if not found:
        print("  device : none found over usbmux "
              "(`pymobiledevice3 usbmux list` returned []).")
        explain_no_device()
        found = wait_for_phone(args.wait) if args.wait > 0 else []
        if not found:
            return 1

    if args.udid:
        match = next((d for d in found if d["serial"] == args.udid), None)
        if not match:
            print(f"  device : {args.udid} is not attached. Found: "
                  f"{', '.join(d['serial'] for d in found)}")
            return 1
    elif len(found) > 1:
        print("  device : more than one iPhone attached — name one with --udid:")
        for d in found:
            print(f"             {d['serial']}  {d.get('label', '')}")
        return 1
    else:
        match = found[0]

    udid = match["serial"]
    print(f"  device : {udid}  {match.get('label', '')}")
    print("           Auto-Lock must be Never — a locked phone returns black screenshots")
    print("           and fails every action underneath, which reads as a broken app.")

    # -- already up? -----------------------------------------------------------
    ready, message = wda_ready()
    if ready:
        print(f"  wda    : already ready at {config.WDA_URL}")
    elif args.skip_stack:
        print(f"  wda    : not answering at {config.WDA_URL} ({message or 'no answer'})")
        print("           --skip-stack was given, so nothing was started. Drop it to bring "
              "the stack up.")
        return 1
    else:
        # -- which runner ------------------------------------------------------
        bundle_id = args.bundle_id or config.WDA_BUNDLE_ID
        if not args.bundle_id:
            resolved, candidates = find_runner_bundle(udid)
            if resolved and resolved != config.WDA_BUNDLE_ID:
                print(f"  runner : WDA_BUNDLE_ID is '{config.WDA_BUNDLE_ID}', which is not "
                      f"installed.")
                print(f"           Using the one that is: {resolved}")
                print(f"           Pin it in app/.env so runs outside this launcher agree:")
                print(f"             WDA_BUNDLE_ID={resolved}")
                bundle_id = resolved
            elif not resolved and candidates:
                print("  runner : several WebDriverAgentRunner builds are installed — pick "
                      "one with --bundle-id:")
                for c in candidates:
                    print(f"             {c}")
                return 1
            elif not resolved and not candidates:
                print("  runner : no WebDriverAgentRunner is installed on this phone.")
                print(f"           It has to be sideloaded and re-signed first — {DOC},")
                print("           steps 6 and 7. Sideloadly alone leaves the nested .xctest")
                print("           unsigned and the runner dies on connect.")
                return 1

        print()
        if not bring_up(udid, bundle_id, args.wda_timeout, mount=not args.no_mount):
            return 1
        print(f"\n  wda    : ready at {config.WDA_URL}")

    if args.stack_only:
        print("\n  Stack is up. Start the dashboard with `python start.py`.\n")
        return 0

    # -- hand over -------------------------------------------------------------
    # Subprocess rather than an import so start.py sees a clean argv and keeps owning the
    # port check, the browser and the shutdown path exactly as it does on Android.
    print("\n  Starting the dashboard ...\n")
    forwarded = [a for a, on in (("--no-browser", args.no_browser),
                                 ("--force", args.force)) if on]
    try:
        return subprocess.call([sys.executable, "start.py", *forwarded])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
