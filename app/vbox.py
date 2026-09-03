"""Thin `VBoxManage` CLI wrapper: boot/poweroff/snapshot-restore a headless Windows VM.

Kept separate from `windows_device.py`'s HTTP hot path deliberately — these are occasional,
slow, subprocess-based operations (VM lifecycle), not per-tap calls. `windows_device.py`
calls `ensure_running()` once, lazily, on first use; nothing else here is on any hot path.

Every operation needs Guest Additions installed and running inside the VM — `guest_ip()` reads
a guest property Guest Additions publishes, and has no fallback if they are missing or not
yet started. See docs/WINDOWS_SETUP.md.
"""
from __future__ import annotations

import logging
import subprocess
import time
from typing import Optional

import requests

import config
from adb_device import DeviceError

logger = logging.getLogger("vbox")


def _run(*args: str, timeout: float = 30.0) -> str:
    try:
        result = subprocess.run(
            [config.VBOXMANAGE_PATH, *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeviceError(
            f"Could not run VBoxManage at '{config.VBOXMANAGE_PATH}': {exc}") from exc
    return result.stdout or ""


def is_running(vm_name: str) -> bool:
    out = _run("list", "runningvms")
    # Each line looks like: "VM Name" {uuid}
    return any(line.strip().startswith(f'"{vm_name}"') for line in out.splitlines())


def start_headless(vm_name: str) -> None:
    out = _run("startvm", vm_name, "--type", "headless", timeout=60)
    if "error" in out.lower() and "already" not in out.lower():
        raise DeviceError(f"VBoxManage startvm {vm_name!r} failed: {out.strip()}")


def poweroff(vm_name: str) -> None:
    if not is_running(vm_name):
        return
    _run("controlvm", vm_name, "poweroff", timeout=30)
    # controlvm returns before the VM has actually released its lock file; a snapshot
    # restore issued immediately after routinely fails with "machine is locked".
    deadline = time.monotonic() + 20
    while is_running(vm_name) and time.monotonic() < deadline:
        time.sleep(1)


def restore_snapshot(vm_name: str, snapshot_name: str) -> None:
    """Power off (if running), restore `snapshot_name`, and boot headless again.

    Slow by nature — tens of seconds to a few minutes depending on disk size. Callers should
    give this its own generous timeout rather than reusing a hot-path one; see
    `windows_device.WindowsDevice.restore_snapshot()`, which is deliberately not part of the
    shared `Device` Protocol for exactly this reason.
    """
    poweroff(vm_name)
    out = _run("snapshot", vm_name, "restore", snapshot_name, timeout=120)
    if "error" in out.lower():
        raise DeviceError(
            f"VBoxManage snapshot restore {vm_name!r} -> {snapshot_name!r} failed: "
            f"{out.strip()}. Check the snapshot name with "
            f"'VBoxManage snapshot {vm_name} list'.")
    start_headless(vm_name)


def guest_ip(vm_name: str, timeout: float = 60.0) -> str:
    """Poll the guest property Guest Additions publishes until it holds a real IPv4."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = _run("guestproperty", "get", vm_name, "/VirtualBox/GuestInfo/Net/0/V4/IP")
        # A value line looks like: "Value: 10.0.2.15"; unset looks like: "No value set!"
        if out.strip().lower().startswith("value:"):
            ip = out.split(":", 1)[1].strip()
            if ip:
                return ip
        time.sleep(2)
    raise DeviceError(
        f"VM '{vm_name}' never reported an IP via Guest Additions within {timeout:.0f}s. "
        f"Check Guest Additions are installed and running inside the guest.")


def wait_until_agent_reachable(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{base_url}/status", timeout=5)
            if response.ok:
                return
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(config.WINDOWS_AGENT_POLL_SECONDS)
    raise DeviceError(
        f"windows_agent.py never answered at {base_url} within {timeout:.0f}s "
        f"(last error: {last_error}). Check it is registered as a Scheduled Task that runs "
        f"at logon, and that auto-logon is configured — see docs/WINDOWS_SETUP.md.")


def ensure_running(vm_name: str) -> str:
    """Boot `vm_name` if it isn't already running, and confirm its control agent answers.

    Returns the base URL `windows_device.py` should call. The whole cold-boot wait is
    absorbed here so nothing above the device layer needs a "still booting" concept —
    `create_device()` either returns a ready adapter or this raises `DeviceError`.
    """
    if not is_running(vm_name):
        start_headless(vm_name)
    remaining = config.WINDOWS_VM_BOOT_TIMEOUT_SECONDS
    started = time.monotonic()
    ip = guest_ip(vm_name, timeout=remaining)
    remaining -= (time.monotonic() - started)
    base_url = f"http://{ip}:{config.WINDOWS_AGENT_PORT}"
    wait_until_agent_reachable(base_url, timeout=max(10.0, remaining))
    return base_url
