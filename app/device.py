"""Picking a device adapter, and the platform-dispatching helpers that go with it.

`AdbDevice` and `IOSDevice` expose the same methods, so everything above this module can
hold either without knowing which — `DeviceSession`, the agent's tools, `run_agent.py` and
`journey.py` all just call `click`, `dump_xml`, `wait_for_ui` and so on.

Import this rather than either adapter directly. `ios_device` is imported lazily inside the
factory: it needs `requests` and the pymobiledevice3 CLI, and an Android-only checkout that
has neither should not fail to start because of an iOS driver it will never construct.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional, Protocol, runtime_checkable

import adb_device

logger = logging.getLogger("device")

ANDROID = "android"
IOS = "ios"
WEB = "web"
WINDOWS = "windows"


@runtime_checkable
class Device(Protocol):
    """The surface both adapters implement.

    Documented as a Protocol rather than a base class on purpose: `AdbDevice` predates it
    and satisfies it structurally, so nothing had to change to adopt this. Add a method here
    only once *both* adapters implement it — a method that exists on one platform belongs
    behind a `capabilities` check instead.
    """

    serial: Optional[str]

    def is_screen_on(self) -> bool: ...
    def wake_screen(self) -> None: ...
    def is_locked(self) -> bool: ...
    @property
    def window_size(self) -> tuple[int, int]: ...
    def dump_xml(self) -> str: ...
    def screenshot_b64(self) -> str: ...
    def current_app(self) -> dict: ...
    def click(self, x: int, y: int) -> None: ...
    def long_click(self, x: int, y: int, duration: float = 0.8) -> None: ...
    def swipe(self, fx: int, fy: int, tx: int, ty: int, duration: float = 0.2) -> None: ...
    def scroll(self, direction: str = "down", scale: float = 0.6) -> None: ...
    def send_keys(self, text: str, clear: bool = False) -> None: ...
    def press(self, key: str) -> None: ...
    def start_app(self, package: str) -> None: ...
    def stop_app(self, package: str) -> None: ...
    def is_installed(self, package: str) -> bool: ...
    def similar_packages(self, hint: str) -> list[str]: ...
    def clear_app_data(self, package: str) -> bool: ...
    def wait_for_ui(self, package: str, timeout: float | None = None, poll: float = 1.0,
                    cancelled: Any = None) -> tuple[str, float]: ...
    def clear_logs(self) -> None: ...
    def read_new_crashes(self, package: str) -> str | None: ...


# What a platform can actually do, for the places where the two genuinely differ. Checking a
# flag beats catching an exception from a method that was never going to work, and beats
# pretending parity and failing silently on one platform.
#
#   DEEPLINK      launch straight to a screen via a URL. Android: `am start -d`. iOS: no —
#                 the launch API takes a bundle id, and a URL scheme is rejected as one.
#                 Web: no — `launch` navigates by URL already, so there is no separate
#                 deep-link path to flag as present or absent.
#   CLEAR_DATA    drop persisted app data. Android: `pm clear`. iOS: no equivalent at all.
#                 Web: yes — cookies and local/session storage, genuinely supported.
#   LIVE_LOG      stream a running log. Android: logcat. iOS: crash reports only, written
#                 asynchronously seconds after the fact. Web has its own analogue (console
#                 errors, synchronous) but it is different enough in shape to leave unflagged.
#   RECENTS       open the app switcher. Android: yes. iOS: not reachable via XCUITest. Web:
#                 no such concept in a single browser tab. Windows: yes, Win+Tab (Task View)
#                 genuinely exists — the one platform besides Android that has this.
#
#   Windows has none of DEEPLINK (no generic desktop-app URI-scheme convention), CLEAR_DATA
#   (the only real reset is a full VM snapshot restore — see windows_device.py's module
#   docstring for why that is deliberately not wired to clear_app_data), or LIVE_LOG (no
#   crash-log reading yet, see docs/WINDOWS_SETUP.md).
CAPABILITIES: dict[str, frozenset[str]] = {
    ANDROID: frozenset({"DEEPLINK", "CLEAR_DATA", "LIVE_LOG", "RECENTS"}),
    IOS: frozenset(),
    WEB: frozenset({"CLEAR_DATA"}),
    WINDOWS: frozenset({"RECENTS"}),
}

_UDID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}$|^[0-9A-Fa-f]{40}$")


def platform_of(serial: Optional[str]) -> str:
    """Which platform a serial belongs to.

    Shape first, because it is free and unambiguous: an iOS UDID is `8hex-16hex` (or 40 hex
    on devices before the iPhone X era) and no adb serial looks like that. Only if the shape
    is inconclusive is the question put to the devices themselves, which costs a subprocess
    per platform.
    """
    if not serial:
        return ANDROID if adb_device.list_serials() else (
            IOS if _ios_serials() else ANDROID)
    if _UDID_RE.match(serial.strip()):
        return IOS
    try:
        if serial in adb_device.list_serials():
            return ANDROID
    except adb_device.DeviceError:
        pass
    return IOS if serial in _ios_serials() else ANDROID


def platform_from_serial(serial: Optional[str]) -> Optional[str]:
    """Platform from the *shape* of a serial alone, or None if it says nothing.

    Unlike `platform_of` this never shells out, which is what makes it safe on paths that
    run per screen read or per prompt build. An iOS UDID is unmistakable; an adb serial can
    be almost any string, so absence of a UDID shape is not evidence of Android and the
    answer is None rather than a guess.
    """
    if serial and _UDID_RE.match(serial.strip()):
        return IOS
    return None


def platform_from_dump(xml: str) -> str:
    """Which platform produced a dump.

    `ios_device.dump_xml` stamps `platform="ios"` on the root element and `web_device.dump_xml`
    stamps `platform="web"`, so a reader holding nothing but the string can still branch — no
    device, no session, no subprocess. Anything unstamped is Android, which keeps every
    existing captured dump in `tests/` correct.
    """
    head = (xml or "")[:200]
    if 'platform="web"' in head:
        return WEB
    if 'platform="windows"' in head:
        return WINDOWS
    return IOS if 'platform="ios"' in head else ANDROID


def _ios_serials() -> list[str]:
    try:
        import ios_device
        return ios_device.list_serials()
    except Exception as exc:  # noqa: BLE001 - a missing iOS toolchain is not an error here
        logger.debug("iOS device listing unavailable: %s", exc)
        return []


def create_device(serial: Optional[str] = None,
                  platform: Optional[str] = None) -> Device:
    """Connect to a device, choosing the adapter by platform.

    `platform` wins when given — pass it when the caller already knows, so a phone that is
    unplugged at this exact moment produces a clear connection error rather than being
    misrouted to the other adapter and a confusing one. This is not optional for web or
    windows: a URL has no shape `platform_of()` can infer the way a UDID's shape identifies
    iOS, and neither does a VM name, so both must always arrive with `platform` explicit (see
    backend/projects.py's `platform` field) rather than being guessed here.
    """
    resolved = (platform or platform_of(serial)).lower()
    if resolved == WEB:
        import web_device
        return web_device.WebDevice(serial)
    if resolved == IOS:
        import ios_device
        return ios_device.IOSDevice(serial)
    if resolved == WINDOWS:
        import windows_device
        return windows_device.WindowsDevice(serial)
    return adb_device.AdbDevice(serial)


def list_devices() -> list[dict[str, str]]:
    """Every connected device on both platforms, each labelled with its platform.

    Neither platform's absence is an error: a machine with no Android SDK, or no iPhone
    tooling, should still list what it does have.
    """
    out: list[dict[str, str]] = []
    try:
        for serial in adb_device.list_serials():
            out.append({**adb_device.describe_serial(serial), "platform": ANDROID})
    except adb_device.DeviceError as exc:
        logger.debug("adb listing failed: %s", exc)

    try:
        import ios_device
        for serial in ios_device.list_serials():
            out.append({**ios_device.describe_serial(serial), "platform": IOS})
    except Exception as exc:  # noqa: BLE001
        logger.debug("iOS listing failed: %s", exc)
    return out


def supports(platform: str, capability: str) -> bool:
    """Whether a platform can do something at all — see CAPABILITIES for the list."""
    return capability in CAPABILITIES.get((platform or "").lower(), frozenset())


def detect_toolkit(xml: str) -> str:
    """Classify the UI toolkit, dispatching on which platform produced the dump.

    Keeps the toolkit readers pure functions over a string, testable from a captured dump.
    """
    platform = platform_from_dump(xml)
    if platform == WEB:
        return "web"
    if platform == IOS:
        import ios_device
        return ios_device.detect_toolkit(xml)
    if platform == WINDOWS:
        import windows_device
        return windows_device.detect_toolkit(xml)
    return adb_device.detect_toolkit(xml)
