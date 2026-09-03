"""Thin, error-hardened wrapper around a headless VirtualBox VM for one Windows desktop app.

Mirrors `ios_device.IOSDevice` method for method, so everything above the device layer —
`agent/device_tools.py`, `agent/screen.py`, `extractor.py`, `run_agent.py` — works unchanged.
Pick one with `device.create_device()`.

Three decisions are load-bearing and worth reading before changing anything here.

**The dump is synthesised Android XML**, exactly like the iOS adapter. `windows_agent.py`
(the in-guest control server, see its module docstring) returns a real nested UIA control
tree as JSON; `render_dump()` here walks it into the same `<node ...>` shape `screen.py`
already parses, mapping UIA control types onto the Android attributes it keys off
(`clickable`, `content-desc`, `bounds`, …) via `_UIA_TO_ANDROID_CLASS` — the same shape as
`ios_device._XCUI_TO_ANDROID_CLASS`.

**`serial` is a VM name, not a device identifier.** `VBoxManage startvm <name>` takes it
directly, so there is no separate lookup step the way an adb serial or iOS UDID needs.
`package` holds the target executable's path — the same overloading `web_device.py` already
uses for a URL.

**Booting a VM is absorbed entirely inside this adapter's own connect path**, via
`_ensure_ready()`, called lazily on first use exactly like `IOSDevice`'s WDA session. Nothing
above the device layer needs to know a cold boot can take tens of seconds — `create_device()`
either returns a ready adapter or raises `DeviceError` after `config.WINDOWS_VM_BOOT_TIMEOUT_
SECONDS`, the same contract every other platform already provides.

`clear_app_data()` always returns `False` — a VM snapshot restore is a poweroff+restore+reboot
that routinely exceeds `config.AGENT_TOOL_TIMEOUT_SECONDS` and resets the whole desktop, not
just the app under test. It is deliberately not wired to this method; see `restore_snapshot()`
below, which exists on the adapter but outside the shared `Device` Protocol, reachable only
through a dedicated route with its own timeout — not from the agent's tools.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any, Optional

import requests

import config
import vbox
import system_memory as sysmem
# Shared deliberately, exactly as ios_device and web_device do: callers already catch
# adb_device.DeviceError, and raising a different-but-identical class here would silently
# slip past every existing handler.
from adb_device import DeviceError

logger = logging.getLogger("windows_device")


# UIA control types a tester can act on directly. `screen.screen_elements` has no UIA flags
# to key off (UIA publishes no `clickable`/`focusable` booleans of its own), so the control
# type is the only signal available — same situation `ios_device.py` is in with WDA types.
_INTERACTIVE = {
    "Button", "Hyperlink", "MenuItem", "TabItem", "ListItem",
    "CheckBox", "RadioButton", "Edit", "Document", "ComboBox", "Slider",
}
_EDITABLE = {"Edit", "Document"}
_TOGGLEABLE = {"CheckBox", "RadioButton"}

# Mapped onto Android-shaped class names because screen.screen_elements decides "is this a
# text field" with `class.endswith("EditText")`, and shows `class.split(".")[-1]` to the
# agent as the element kind — emitting raw UIA control-type strings would break the first and
# read oddly in the second.
_UIA_TO_ANDROID_CLASS = {
    "Button": "win.widget.Button",
    "Hyperlink": "win.widget.Button",
    "MenuItem": "win.widget.Button",
    "TabItem": "win.widget.Button",
    "ListItem": "win.widget.Button",
    "CheckBox": "win.widget.CheckBox",
    "RadioButton": "win.widget.CheckBox",
    "Edit": "win.widget.EditText",
    "Document": "win.widget.EditText",
    "ComboBox": "win.widget.Spinner",
    "Slider": "win.widget.SeekBar",
    "Text": "win.widget.TextView",
    "Image": "win.widget.ImageView",
}

# Best-effort key mapping — see WindowsDevice.press()'s docstring for which of these are
# universal versus app-dependent conventions, the same honesty ios_device.py already applies
# to its own edge-swipe "back" emulation.
_PRESS_KEYS = {"back", "home", "enter", "delete", "recent"}


class WindowsDevice:
    """Adapter around a headless VirtualBox VM running `windows_agent.py`.

    Construction is cheap and does not touch the VM; it boots (if needed) and connects on
    first use, so listing projects in the dashboard cannot disturb a run in flight — the same
    rule `IOSDevice`/`WebDevice` already follow.
    """

    def __init__(self, serial: Optional[str] = None):
        if not serial:
            raise DeviceError(
                "A Windows target needs a VM name — pass device_serial when creating the "
                "project (see docs/WINDOWS_SETUP.md).")
        self.serial = serial
        self._base_url: Optional[str] = None
        self._size: Optional[tuple[int, int]] = None

    # -- transport / VM lifecycle --------------------------------------------------
    def _ensure_ready(self) -> str:
        """Boot the VM if needed and confirm the guest agent answers, once per adapter."""
        if self._base_url:
            return self._base_url
        # Dev-only bypass: talk straight to a locally running windows_agent.py with no VM
        # involved at all — see docs/WINDOWS_SETUP.md's stage-1 development note. Never a
        # supported end-user mode; a real Windows project always names a VM.
        if self.serial == "localhost":
            self._base_url = f"http://127.0.0.1:{config.WINDOWS_AGENT_PORT}"
            return self._base_url
        self._base_url = vbox.ensure_running(self.serial)
        return self._base_url

    def _call(self, method: str, path: str, payload: Optional[dict] = None,
              timeout: float = 30.0) -> Any:
        base = self._ensure_ready()
        url = f"{base}{path}"
        try:
            response = requests.request(method, url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise DeviceError(
                f"Windows agent unreachable at {base}: {exc}. Is the VM '{self.serial}' "
                f"running and the control server started? See docs/WINDOWS_SETUP.md.") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("detail", ""))[:300]
            except ValueError:
                pass
            raise DeviceError(f"windows_agent {method} {path} returned "
                              f"HTTP {response.status_code}: {detail}")
        try:
            return response.json()
        except ValueError:
            return {}

    # -- screen state ---------------------------------------------------------------
    def is_screen_on(self) -> bool:
        """Always True — a VM's framebuffer composites whether anyone is looking or not."""
        return True

    def is_locked(self) -> bool:
        try:
            return bool(self._call("GET", "/is_locked", timeout=15).get("locked"))
        except DeviceError:
            return False

    def wake_screen(self) -> None:
        """Best-effort mouse jiggle. Configure the guest to never idle-lock (see setup docs)
        rather than relying on this — an actually locked workstation cannot be unlocked from
        here without the guest's own credentials."""
        try:
            self._call("POST", "/wake", timeout=15)
        except DeviceError as exc:
            raise DeviceError(f"wake_screen() failed: {exc}") from exc

    @property
    def window_size(self) -> tuple[int, int]:
        if self._size is None:
            value = self._call("GET", "/window_size", timeout=15)
            try:
                self._size = (int(value["width"]), int(value["height"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise DeviceError(f"window_size() returned {value!r}") from exc
        return self._size

    def dump_xml(self) -> str:
        """The current foreground window as Android-shaped XML (see module docstring)."""
        try:
            tree = self._call("GET", "/dump", timeout=30)
        except DeviceError as exc:
            raise DeviceError(f"dump_xml() failed: {exc}") from exc
        try:
            app = self.current_app()
            package = app.get("package", "unknown")
        except DeviceError:
            package = "unknown"
        return render_dump(tree or {}, package, self.serial or "")

    def screenshot_b64(self) -> str:
        try:
            value = self._call("GET", "/screenshot", timeout=30)
        except DeviceError as exc:
            raise DeviceError(f"screenshot capture failed: {exc}") from exc
        image = value.get("image") if isinstance(value, dict) else None
        if not image:
            raise DeviceError("windows_agent returned no screenshot data")
        return image

    def current_app(self) -> dict:
        """Foreground app. `activity` carries the window title — the closest Windows
        analogue Android's activity name has."""
        try:
            info = self._call("GET", "/current_app", timeout=15) or {}
        except DeviceError as exc:
            raise DeviceError(f"current_app() failed: {exc}") from exc
        return {"package": info.get("exe", ""), "activity": info.get("title", "")}

    # -- actions ---------------------------------------------------------------------
    def click(self, x: int, y: int) -> None:
        try:
            self._call("POST", "/click", {"x": int(x), "y": int(y)}, timeout=15)
        except DeviceError as exc:
            raise DeviceError(f"click({x},{y}) failed: {exc}") from exc

    def long_click(self, x: int, y: int, duration: float = 0.8) -> None:
        try:
            self._call("POST", "/long_click",
                       {"x": int(x), "y": int(y), "duration": float(duration)},
                       timeout=15 + duration)
        except DeviceError as exc:
            raise DeviceError(f"long_click({x},{y}) failed: {exc}") from exc

    def swipe(self, fx: int, fy: int, tx: int, ty: int, duration: float = 0.2) -> None:
        try:
            self._call("POST", "/drag", {
                "fx": int(fx), "fy": int(fy), "tx": int(tx), "ty": int(ty),
                "duration": float(duration)}, timeout=15 + duration)
        except DeviceError as exc:
            raise DeviceError(f"swipe({fx},{fy}->{tx},{ty}) failed: {exc}") from exc

    def scroll(self, direction: str = "down", scale: float = 0.6) -> None:
        w, h = self.window_size
        try:
            self._call("POST", "/scroll", {
                "x": w // 2, "y": h // 2, "direction": direction, "amount": 3}, timeout=15)
        except DeviceError as exc:
            raise DeviceError(f"scroll(direction={direction!r}) failed: {exc}") from exc

    def send_keys(self, text: str, clear: bool = False) -> None:
        try:
            self._call("POST", "/send_keys", {"text": text, "clear": clear}, timeout=30)
        except DeviceError as exc:
            raise DeviceError(f"send_keys({text!r}) failed: {exc}") from exc

    def press(self, key: str) -> None:
        """key: 'back' | 'home' | 'enter' | 'delete' | 'recent'.

        Windows has no hardware back button; 'back' sends Alt+Left, which works in Explorer,
        browsers and many apps with navigation history but is not universal — unlike Android,
        a clean failure here can mean nothing happened rather than the wrong thing happening.
        'recent' sends Win+Tab (Task View), which — unlike iOS/Web — genuinely exists on
        Windows, so it is supported rather than raising.
        """
        key = (key or "").lower()
        if key not in _PRESS_KEYS:
            raise DeviceError(f"press({key!r}) — expected one of {sorted(_PRESS_KEYS)}")
        try:
            self._call("POST", "/press", {"key": key}, timeout=15)
        except DeviceError as exc:
            raise DeviceError(f"press({key!r}) failed: {exc}") from exc

    def start_app(self, package: str) -> None:
        try:
            self._call("POST", "/launch", {"path": package, "args": []}, timeout=30)
        except DeviceError as exc:
            raise DeviceError(f"start_app({package!r}) failed: {exc}") from exc

    def stop_app(self, package: str) -> None:
        import os
        exe = os.path.basename(package)
        try:
            self._call("POST", "/stop", {"exe": exe}, timeout=15)
        except DeviceError as exc:
            raise DeviceError(f"stop_app({package!r}) failed: {exc}") from exc

    def is_installed(self, package: str) -> bool:
        try:
            value = self._call(
                "GET", f"/is_installed?path={requests.utils.quote(package)}", timeout=15)
            return bool(value.get("installed"))
        except DeviceError as exc:
            raise DeviceError(f"is_installed({package!r}) failed: {exc}") from exc

    def similar_packages(self, hint: str) -> list[str]:
        """No installed-app registry to search for an arbitrary exe path — honest emptiness,
        same reasoning `web_device.similar_packages` already uses."""
        return []

    def clear_app_data(self, package: str) -> bool:
        """Always False — see the module docstring for why this is not wired to a VM
        snapshot restore. Use `restore_snapshot()` (a separate, non-Protocol operation with
        its own timeout) instead."""
        sysmem.learn(
            "windows-no-fast-app-data-clear",
            "Windows has no fast, in-place 'clear app data' from the host: the only real "
            "reset is a VM snapshot restore, which reboots the whole desktop and takes far "
            "longer than clear_app_data's callers expect. Use restore_snapshot() as a "
            "separate, manually-triggered action instead.",
            evidence=f"clear_app_data({package}) is unavailable on the Windows adapter",
        )
        return False

    def restore_snapshot(self, snapshot_name: Optional[str] = None) -> None:
        """Power off, restore to a named snapshot, and reboot the VM.

        Not part of the `Device` Protocol and not reachable from `agent/device_tools.py` —
        the same "adapter has more capability than the shared interface" shape
        `web_device.set_viewport` already uses. Reachable only through a dedicated backend
        route with its own generous timeout. Tens of seconds to a few minutes; callers should
        not expect this to return quickly.
        """
        name = snapshot_name or config.WINDOWS_DEFAULT_SNAPSHOT
        vbox.restore_snapshot(self.serial, name)
        self._base_url = None  # force a fresh boot-and-reconnect on next use
        self._size = None

    def wait_for_ui(self, package: str, timeout: float | None = None, poll: float = 1.0,
                    cancelled: Any = None) -> tuple[str, float]:
        """Block until `package` owns the screen with rendered content.

        Same contract as the Android/iOS adapters: an empty or foreign dump right after a
        launch means "not ready", not "the app is broken" — text, not node count, is the
        readiness test.
        """
        toolkit_guess = sysmem.environment("last_toolkit", "win-native")
        budget = timeout if timeout is not None else max(
            8.0, sysmem.suggest_launch_settle(toolkit_guess, default=8.0) * 1.5)

        started = time.monotonic()
        deadline = started + budget
        last_xml = ""
        while True:
            if cancelled is not None and cancelled.is_set():
                return last_xml, round(time.monotonic() - started, 2)
            try:
                last_xml = self.dump_xml()
            except DeviceError:
                last_xml = ""
            if last_xml:
                exe_name = (package or "").lower()
                owns = f'package="{exe_name}"' in last_xml.lower()
                has_text = bool(re.search(r'\stext="[^"]+"', last_xml)
                                or re.search(r'content-desc="[^"]+"', last_xml))
                if owns and has_text:
                    elapsed = time.monotonic() - started
                    toolkit = detect_toolkit(last_xml)
                    sysmem.observe_launch(toolkit, elapsed)
                    sysmem.observe_environment("last_toolkit", toolkit)
                    return last_xml, round(elapsed, 2)
            if time.monotonic() >= deadline:
                elapsed = time.monotonic() - started
                sysmem.learn(
                    "win-ui-never-settled",
                    "The UI did not publish readable content within the learned budget — "
                    "screenshot before concluding anything about the app.",
                    evidence=f"waited {elapsed:.0f}s for {package} with no readable dump",
                )
                return last_xml, round(elapsed, 2)
            time.sleep(poll)

    # -- crash detection ----------------------------------------------------------------
    def clear_logs(self) -> None:
        """No-op for v1 — no Windows Event Log crash reading yet."""

    def read_new_crashes(self, package: str) -> str | None:
        """No-op for v1 — see docs/WINDOWS_SETUP.md's known-gaps section."""
        return None


def detect_toolkit(xml: str) -> str:
    """Classify the Windows UI toolkit from a synthesised dump, mirroring
    `adb_device.detect_toolkit`'s reasoning: timings are keyed by toolkit so learned waits
    transfer across apps rather than being filed uselessly under one app's exe name."""
    if not xml:
        return "unknown"
    nodes = xml.count("<node")
    ids = len(re.findall(r'resource-id="[^"]+"', xml))
    if "win.widget.ImageView" in xml and nodes > 5 and ids <= max(1, nodes // 20):
        return "win-custom-drawn"
    return "win-native"


def render_dump(root: dict, package: str, vm_name: str = "") -> str:
    """Render a `windows_agent.py` UIA tree as the `<node>` XML `screen.py` already parses.

    A module-level pure function, not a method, so the translation can be tested from a
    captured tree with no VM and no windows_agent — the same way `ios_device.render_dump` is
    tested from a captured WDA tree.
    """
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<hierarchy rotation="0" platform="windows" vm="{_attr(vm_name)}">',
    ]
    _render_node(root, package, parts)
    parts.append("</hierarchy>")
    return "".join(parts)


def _render_node(node: dict, package: str, out: list[str]) -> None:
    if not isinstance(node, dict):
        return

    kind = str(node.get("control_type") or "Other")
    rect = node.get("rect") or {}
    try:
        left, top = int(rect.get("left", 0)), int(rect.get("top", 0))
        right, bottom = int(rect.get("right", 0)), int(rect.get("bottom", 0))
    except (TypeError, ValueError):
        left = top = right = bottom = 0

    interactive = kind in _INTERACTIVE
    editable = kind in _EDITABLE
    checkable = kind in _TOGGLEABLE

    checked = ""
    if checkable:
        # 0=off, 1=on, 2=indeterminate — see windows_agent.py's _toggle_state().
        checked = "true" if node.get("toggle_state") == 1 else "false"

    attrs = {
        "class": _UIA_TO_ANDROID_CLASS.get(kind, "win.view.View"),
        "win-type": kind,
        "package": package,
        "text": str(node.get("value") or ""),
        "content-desc": str(node.get("name") or ""),
        "resource-id": str(node.get("automation_id") or ""),
        "bounds": f"[{left},{top}][{right},{bottom}]",
        "enabled": "true" if node.get("enabled", True) else "false",
        "clickable": "true" if interactive else "false",
        "focusable": "true" if editable else "false",
        "checkable": "true" if checkable else "false",
    }
    if checked:
        attrs["checked"] = checked

    rendered = " ".join(f'{k}="{_attr(v)}"' for k, v in attrs.items())
    children = node.get("children") or []
    if children:
        out.append(f"<node {rendered}>")
        for child in children:
            _render_node(child, package, out)
        out.append("</node>")
    else:
        out.append(f"<node {rendered} />")


def _attr(value: Any) -> str:
    """Escape a value for an XML attribute."""
    return (str(value)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("\n", " ").replace("\r", " "))
