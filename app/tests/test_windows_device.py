"""The Windows adapter's translation layer: `windows_agent.py`'s UIA tree -> Android XML.

`windows_device.render_dump` renders the in-guest control server's JSON tree into the same
`<node>` XML `agent/screen.py` and `extractor.py` already parse, exactly as `ios_device.py`
does for WebDriverAgent's tree — see that adapter's tests for the sibling suite this mirrors.

No VM and no `windows_agent.py`: `render_dump` is a pure function over a captured tree, in
the same shape `windows_agent.py`'s `/dump` endpoint actually returns (see its `_walk()`).
"""
from __future__ import annotations

import device as device_mod
from agent import screen
from windows_device import render_dump

import windows_device

WIN_W, WIN_H = 1280, 800

EXE = "notepad.exe"


def uia(control_type: str, *, name: str = "", automation_id: str = "", value=None,
        toggle_state=None, enabled: bool = True,
        left: int = 0, top: int = 0, right: int = 100, bottom: int = 40,
        children: list | None = None) -> dict:
    """One UIA node, in the shape `windows_agent.py`'s `_walk()` actually emits."""
    node: dict = {
        "control_type": control_type,
        "name": name,
        "automation_id": automation_id,
        "class_name": control_type,
        "value": value if value is not None else "",
        "toggle_state": toggle_state,
        "enabled": enabled,
        "rect": {"left": left, "top": top, "right": right, "bottom": bottom},
        "children": children or [],
    }
    return node


def window(*children: dict) -> dict:
    """The top-level Window node `windows_agent.py` puts at the root of every dump."""
    return uia("Window", name="Untitled - Notepad",
               left=0, top=0, right=WIN_W, bottom=WIN_H, children=list(children))


def elements_of(tree: dict) -> list[dict]:
    return screen.screen_elements(render_dump(tree, EXE), WIN_W, WIN_H)


# --- the translation itself -------------------------------------------------------

class TestTranslation:
    def test_dump_is_stamped_windows_so_readers_can_dispatch(self):
        xml = render_dump(window(), EXE, vm_name="notepad-vm")
        assert 'platform="windows"' in xml
        assert device_mod.platform_from_dump(xml) == device_mod.WINDOWS

    def test_android_dump_is_not_mistaken_for_windows(self):
        assert device_mod.platform_from_dump(
            '<?xml version="1.0"?><hierarchy rotation="0"><node /></hierarchy>'
        ) == device_mod.ANDROID

    def test_button_becomes_a_touchable_element(self):
        tree = window(uia("Button", name="Save", automation_id="btnSave",
                          left=10, top=20, right=90, bottom=50))
        [element] = elements_of(tree)
        assert element["label"] == "Save"
        assert element["resource_id"] == "btnSave"
        assert element["class"] == "Button"

    def test_bounds_pass_through_unscaled(self):
        """Unlike iOS's points-vs-pixels split, UIA rects are already absolute screen
        coordinates — no rescale between the dump and a coordinate `click()` accepts."""
        tree = window(uia("Button", name="Save", left=10, top=20, right=90, bottom=50))
        [element] = elements_of(tree)
        assert element["bounds"] == [10, 20, 90, 50]
        assert (element["x"], element["y"]) == (50, 35)

    def test_edit_control_is_editable_and_carries_its_typed_value(self):
        tree = window(uia("Edit", automation_id="txtBody", value="hello world",
                          left=0, top=60, right=WIN_W, bottom=WIN_H))
        [element] = elements_of(tree)
        assert element["editable"] is True
        assert "hello world" in screen.screen_texts(render_dump(tree, EXE))

    def test_checkbox_reports_its_toggle_state(self):
        on = elements_of(window(uia("CheckBox", name="Word wrap", toggle_state=1)))
        off = elements_of(window(uia("CheckBox", name="Word wrap", toggle_state=0)))
        assert on[0]["checked"] == "true"
        assert off[0]["checked"] == "false"

    def test_disabled_elements_are_not_offered_as_tappable(self):
        assert elements_of(window(uia("Button", name="Submit", enabled=False))) == []

    def test_static_text_is_readable_but_not_tappable(self):
        tree = window(uia("Text", name="Ln 1, Col 1", left=0, top=780, right=100, bottom=800))
        assert elements_of(tree) == []
        assert "Ln 1, Col 1" in screen.screen_texts(render_dump(tree, EXE))

    def test_labels_needing_xml_escaping_survive(self):
        tree = window(uia("Button", name='Save & "Exit" <now>'))
        [element] = elements_of(tree)
        assert element["label"] == 'Save & "Exit" <now>'

    def test_ownership_ranking_names_the_exe_under_test(self):
        xml = render_dump(window(uia("Button", name="Save")), EXE)
        assert screen.package_ranking(xml)[0][0] == EXE


# --- toolkit and platform dispatch --------------------------------------------------

class TestDispatch:
    def test_create_device_returns_a_windows_device(self):
        dev = device_mod.create_device("notepad-vm", platform="windows")
        assert isinstance(dev, windows_device.WindowsDevice)

    def test_a_windows_device_satisfies_the_shared_protocol(self):
        dev = device_mod.create_device("notepad-vm", platform="windows")
        assert isinstance(dev, device_mod.Device)

    def test_a_vm_name_is_not_guessed_at(self):
        """A VM name has no shape of its own, unlike an iOS UDID — a Windows target must
        always arrive with platform="windows" explicit, the same rule web already follows."""
        assert device_mod.platform_of("notepad-vm") != device_mod.WINDOWS

    def test_toolkit_defaults_to_native(self):
        xml = render_dump(window(uia("Button", name="Save")), EXE)
        assert device_mod.detect_toolkit(xml) == "win-native"


class TestCapabilities:
    def test_recents_is_supported_unlike_ios_or_web(self):
        """Win+Tab (Task View) genuinely exists on Windows — the one non-Android platform
        with a real app-switcher equivalent."""
        assert device_mod.supports(device_mod.WINDOWS, "RECENTS")
        assert not device_mod.supports(device_mod.IOS, "RECENTS")
        assert not device_mod.supports(device_mod.WEB, "RECENTS")

    def test_clear_data_is_declared_absent_rather_than_discovered_at_runtime(self):
        """Not an unimplemented stub: the only real reset is a VM snapshot restore, which is
        deliberately kept outside this capability — see windows_device.py's module
        docstring."""
        assert not device_mod.supports(device_mod.WINDOWS, "CLEAR_DATA")

    def test_deeplink_and_live_log_are_absent(self):
        assert not device_mod.supports(device_mod.WINDOWS, "DEEPLINK")
        assert not device_mod.supports(device_mod.WINDOWS, "LIVE_LOG")


# --- adapter-level behavior that needs no live agent --------------------------------

class TestWindowsDeviceOffline:
    """Methods whose contract is decidable without a network call — no VM, no
    windows_agent.py, matching how ios_device.clear_app_data() is tested."""

    def test_construction_requires_a_serial(self):
        import pytest
        from adb_device import DeviceError
        with pytest.raises(DeviceError):
            windows_device.WindowsDevice(None)

    def test_clear_app_data_always_declines(self):
        dev = windows_device.WindowsDevice("notepad-vm")
        assert dev.clear_app_data(EXE) is False

    def test_is_screen_on_is_always_true(self):
        """No display-power concept in a VM — the framebuffer composites regardless."""
        dev = windows_device.WindowsDevice("notepad-vm")
        assert dev.is_screen_on() is True

    def test_similar_packages_is_honestly_empty(self):
        dev = windows_device.WindowsDevice("notepad-vm")
        assert dev.similar_packages("notepad") == []

    def test_press_rejects_an_unknown_key_before_any_network_call(self):
        import pytest
        from adb_device import DeviceError
        dev = windows_device.WindowsDevice("notepad-vm")
        with pytest.raises(DeviceError):
            dev.press("volume_up")
