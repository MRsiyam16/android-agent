"""Thin `emulator`/`adb` wrapper: boot a headless Android AVD and wait for it to finish booting.

The Android sibling of `vbox.py`, and deliberately the same shape — occasional, slow,
subprocess-based lifecycle operations, kept out of `adb_device.py`'s per-tap hot path. Read
that module's header first; the reasoning transfers.

**Why an emulator at all, when there is a real phone on the desk.** Bugmaster's fix pipeline
verifies a patched build before it merges (see `docs/VERIFIER.md`). Most of those fixes are
ordinary screen logic, and running them on the physical Samsung means the phone has to be
plugged in, unlocked, not mid-run for the QA Master, and not in somebody's pocket. An AVD is
always there. It is explicitly *not* good enough for camera, biometrics, push notifications,
calendar sync, OEM behaviour or anything about performance — Bugmaster marks those jobs
`needsRealDevice` and waits for the phone instead.

**Nothing calls this automatically.** `stacks.status("android")` names it in the `fix` hint
when no Android device is attached, and `python emulator.py --ensure` runs it by hand. A
booting emulator takes a minute or two and grabs the audio device; starting one as a side
effect of somebody opening a dashboard is the kind of surprise that gets a feature turned off.

Two things about this machine, both learned the hard way and both encoded here rather than
left to the environment:

* **adb is not on PATH.** Every subprocess call goes through `config.ADB_PATH`, which knows
  how to find the SDK's copy. A bare `"adb"` works in a developer's shell and fails inside the
  server, which is the worst of both.
* **The emulator must outlive the process that starts it.** `emulator.exe` is a foreground
  program that runs until the VM shuts down, so it is spawned detached (`DETACHED_PROCESS` +
  `CREATE_NEW_PROCESS_GROUP` on Windows) — otherwise closing the launcher window, or a Ctrl-C
  meant for the server, takes the device down with it mid-run.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Optional

import config
from adb_device import DeviceError

logger = logging.getLogger("emulator")

#: An adb serial that came from an AVD rather than a cable. `adb devices` names emulators
#: `emulator-<console port>` and always has — it is how the console port is addressed — so the
#: prefix is a reliable discriminator, and it is the only one available without a `getprop`
#: round trip per device.
EMULATOR_PREFIX = "emulator-"


def is_emulator(serial: str) -> bool:
    return str(serial or "").startswith(EMULATOR_PREFIX)


def _adb(*args: str, timeout: float = 15.0) -> str:
    try:
        result = subprocess.run(
            [config.ADB_PATH, *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeviceError(f"Could not run adb at '{config.ADB_PATH}': {exc}") from exc
    return result.stdout or ""


def parse_devices(output: str) -> list[tuple[str, str]]:
    """(serial, state) pairs from `adb devices` output.

    Its own function so the parsing can be tested without a device: the header line, the blank
    lines, the `offline`/`unauthorized` states and the daemon's own startup chatter
    ("* daemon started successfully *") all land in this output, and every one of them has at
    some point been mistaken for a device by a naive split.
    """
    rows: list[tuple[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        rows.append((parts[0], parts[1]))
    return rows


def is_running() -> Optional[str]:
    """The serial of a booted-enough emulator adb can see, or None.

    "adb lists it as `device`" is weaker than "the OS has finished booting" — an AVD answers
    adb well before `sys.boot_completed` is 1 — which is exactly why `ensure_running` polls
    that property afterwards rather than trusting this.
    """
    try:
        rows = parse_devices(_adb("devices"))
    except DeviceError as exc:
        logger.info("could not list devices: %s", exc)
        return None
    for serial, state in rows:
        if is_emulator(serial) and state == "device":
            return serial
    return None


def _emulator_path() -> str:
    """Where `emulator.exe` is, from config, with the environment expanded."""
    return os.path.expandvars(config.EMULATOR_PATH)


def start_headless(avd_name: Optional[str] = None) -> None:
    """Launch the AVD with no window, no audio and no boot animation, detached.

    Returns as soon as the process is spawned; it does not wait. Booting is `wait_until_booted`
    because the two are separable — a caller that already has an emulator coming up wants the
    wait without a second launch.
    """
    avd = avd_name or config.ANDROID_AVD_NAME
    exe = _emulator_path()
    if not os.path.isfile(exe):
        raise DeviceError(
            f"No Android emulator at '{exe}'. Install the SDK's emulator package, or set "
            f"EMULATOR_PATH in app/.env to the full path of emulator.exe.")

    args = [exe, "-avd", avd, "-no-window", "-no-audio", "-no-boot-anim"]
    # Windows-only flags, and absent everywhere else — `subprocess` rejects a creationflags it
    # does not know, so this cannot simply be passed unconditionally.
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True, **kwargs)
    except OSError as exc:
        raise DeviceError(f"Could not launch the emulator ({exe} -avd {avd}): {exc}") from exc
    logger.info("launched AVD %s headless", avd)


def wait_until_booted(timeout: float = 180.0, serial: Optional[str] = None) -> str:
    """Poll until an emulator reports `sys.boot_completed` = 1. Returns its serial.

    Two conditions in one wait, because they fail in the same way: adb has to *see* an
    emulator, and that emulator's framework has to have finished starting. Installing an APK
    or launching an activity in between succeeds at the adb level and then does nothing, which
    reads downstream as a broken app rather than as an emulator that was not ready.
    """
    deadline = time.monotonic() + timeout
    last = "no emulator appeared in `adb devices`"
    while time.monotonic() < deadline:
        found = serial or is_running()
        if found:
            try:
                out = _adb("-s", found, "shell", "getprop", "sys.boot_completed", timeout=10)
            except DeviceError as exc:
                last = str(exc)
            else:
                if out.strip() == "1":
                    return found
                last = f"{found} is up but sys.boot_completed is {out.strip() or 'unset'!r}"
        time.sleep(2)
    raise DeviceError(
        f"The emulator did not finish booting within {timeout:.0f}s ({last}). A cold AVD boot "
        f"can take several minutes on first run — try again, or start it with a window "
        f"(`{_emulator_path()} -avd {config.ANDROID_AVD_NAME}`) to see what it is doing.")


def ensure_running(timeout: float = 180.0) -> str:
    """The serial of a booted emulator, starting one first if none is there.

    Idempotent and safe to call when one is already up: a second `emulator -avd` on the same
    AVD fails with a lock-file error rather than booting a second device, so the check is not a
    nicety. The whole cold-boot wait is absorbed here for the same reason `vbox.ensure_running`
    absorbs its own — nothing above should need a "still booting" concept.
    """
    found = is_running()
    if found:
        return wait_until_booted(timeout=min(timeout, 60.0), serial=found)
    start_headless()
    return wait_until_booted(timeout=timeout)


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Bring the Android emulator up for verification runs.")
    parser.add_argument("--ensure", action="store_true",
                        help="start the AVD if it is not running and wait until it has booted")
    parser.add_argument("--status", action="store_true",
                        help="print the emulator serial adb can see, if any")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="seconds to wait for the boot (default 180)")
    args = parser.parse_args()

    if args.status or not args.ensure:
        found = is_running()
        print(f"  avd    : {config.ANDROID_AVD_NAME}")
        print(f"  binary : {_emulator_path()}")
        print(f"  adb    : {config.ADB_PATH}")
        print(f"  running: {found or 'no emulator is attached'}")
        return 0

    print(f"  starting {config.ANDROID_AVD_NAME} headless (up to {args.timeout:.0f}s) ...")
    try:
        serial = ensure_running(timeout=args.timeout)
    except DeviceError as exc:
        print(f"  failed : {exc}")
        return 1
    print(f"  ready  : {serial}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
