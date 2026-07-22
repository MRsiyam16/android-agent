"""Thin, error-hardened wrapper around uiautomator2 for a single connected device."""
from __future__ import annotations

import logging
import subprocess

import uiautomator2 as u2

import config

logger = logging.getLogger("adb_device")


class DeviceError(RuntimeError):
    """Raised when the device adapter cannot complete an operation."""


def list_serials() -> list[str]:
    """Return serials reported by `adb devices` for state == 'device' (ready, not offline)."""
    try:
        result = subprocess.run(
            [config.ADB_PATH, "devices"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeviceError(f"Could not run adb at '{config.ADB_PATH}': {exc}") from exc

    serials: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        if state.strip() == "device":
            serials.append(serial)
    return serials


class AdbDevice:
    """Adapter around a `uiautomator2` session for one Android device/emulator."""

    def __init__(self, serial: str | None = None):
        self.serial = serial
        try:
            self.d: u2.Device = u2.connect(serial) if serial else u2.connect()
        except Exception as exc:  # noqa: BLE001 - surface any connect failure uniformly
            raise DeviceError(f"Failed to connect to device '{serial or 'default'}': {exc}") from exc

        try:
            info = self.d.device_info
            self.serial = self.serial or info.get("serial", "unknown")
        except Exception:  # noqa: BLE001
            pass

    # -- screen state -----------------------------------------------------------
    def is_screen_on(self) -> bool:
        try:
            return bool(self.d.info.get("screenOn", True))
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"is_screen_on() failed: {exc}") from exc

    def wake_screen(self) -> None:
        try:
            self.d.shell(["input", "keyevent", "KEYCODE_WAKEUP"])
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"wake_screen() failed: {exc}") from exc

    def is_locked(self) -> bool:
        """Best-effort keyguard/lockscreen detection via dumpsys window."""
        try:
            output = self.d.shell(["dumpsys", "window"]).output
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"is_locked() failed: {exc}") from exc

        for line in output.splitlines():
            line = line.strip()
            if line.startswith("mCurrentFocus") or line.startswith("mFocusedApp"):
                if any(marker in line for marker in ("Keyguard", "Bouncer", "StatusBar")):
                    return True
            if "mDreamingLockscreen=true" in line or "isStatusBarKeyguard=true" in line:
                return True
        return False

    @property
    def window_size(self) -> tuple[int, int]:
        try:
            w, h = self.d.window_size()
            return int(w), int(h)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"window_size() failed: {exc}") from exc

    def dump_xml(self) -> str:
        try:
            return self.d.dump_hierarchy()
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"dump_hierarchy() failed: {exc}") from exc

    def screenshot_b64(self) -> str:
        """Base64 JPEG of the current screen."""
        try:
            import base64
            import io
            img = self.d.screenshot(format="pillow")
            buf = io.BytesIO()
            img.convert("RGB").save(
                buf, format="JPEG", quality=config.SCREENSHOT_QUALITY, optimize=True
            )
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"screenshot capture failed: {exc}") from exc

    def current_app(self) -> dict:
        try:
            info = self.d.app_current()
            return {
                "package": info.get("package", ""),
                "activity": info.get("activity", ""),
            }
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"app_current() failed: {exc}") from exc

    # -- actions -----------------------------------------------------------
    def click(self, x: int, y: int) -> None:
        try:
            self.d.click(x, y)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"click({x},{y}) failed: {exc}") from exc

    def send_keys(self, text: str, clear: bool = False) -> None:
        try:
            if clear:
                self.d.clear_text()
            self.d.send_keys(text)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"send_keys({text!r}) failed: {exc}") from exc

    def press(self, key: str) -> None:
        """key: 'back' | 'home' | 'enter' | 'recent' | volume/power etc (uiautomator2 keys)."""
        try:
            self.d.press(key)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"press({key!r}) failed: {exc}") from exc

    def start_app(self, package: str) -> None:
        try:
            self.d.app_start(package, stop=True)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"app_start({package!r}) failed: {exc}") from exc

    def stop_app(self, package: str) -> None:
        try:
            self.d.app_stop(package)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"app_stop({package!r}) failed: {exc}") from exc
