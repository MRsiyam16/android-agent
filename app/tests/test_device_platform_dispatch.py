"""`device.py`'s three-way platform dispatch, with web added alongside Android and iOS.

`create_device()`, `platform_from_dump()` and `CAPABILITIES` are the seams every reader above
the device layer goes through without knowing which platform it is holding — these tests pin
the contract at that seam, not the adapters behind it (see `test_web_device.py` and
`test_ios_device.py` for those).
"""
from __future__ import annotations

import device as device_mod


class TestCreateDevice:
    def test_an_explicit_web_platform_returns_a_web_device(self):
        """`platform` wins when given — the same rule that already lets an iOS UDID be
        overridden. A web "serial" (a URL) has no shape of its own to infer from, so this is
        not optional the way it is for Android/iOS: nothing else can answer the question.
        """
        dev = device_mod.create_device("https://example.com", platform="web")
        import web_device
        assert isinstance(dev, web_device.WebDevice)
        dev.close()

    def test_a_web_device_satisfies_the_shared_protocol(self):
        """Structural, like AdbDevice and IOSDevice — no shared base class, just the same
        method surface (see device.Device's own docstring for why)."""
        dev = device_mod.create_device("https://example.com", platform="web")
        try:
            assert isinstance(dev, device_mod.Device)
        finally:
            dev.close()

    def test_an_explicit_windows_platform_returns_a_windows_device(self):
        """A VM name, like a web URL, has no shape of its own to infer from — `platform`
        is not optional here either. See test_windows_device.py for the fuller adapter and
        translation suite."""
        dev = device_mod.create_device("notepad-vm", platform="windows")
        import windows_device
        assert isinstance(dev, windows_device.WindowsDevice)
        assert isinstance(dev, device_mod.Device)


class TestPlatformFromDump:
    def test_a_web_stamped_dump_is_recognised(self):
        import web_device
        xml = web_device.render_dom({"tag": "div", "children": []}, "https://example.com")
        assert 'platform="web"' in xml
        assert device_mod.platform_from_dump(xml) == device_mod.WEB

    def test_an_ios_stamped_dump_still_reads_as_ios(self):
        assert device_mod.platform_from_dump(
            '<hierarchy rotation="0" platform="ios"></hierarchy>') == device_mod.IOS

    def test_a_windows_stamped_dump_is_recognised(self):
        import windows_device
        xml = windows_device.render_dump({"control_type": "Window", "children": []},
                                         "notepad.exe", "notepad-vm")
        assert 'platform="windows"' in xml
        assert device_mod.platform_from_dump(xml) == device_mod.WINDOWS

    def test_an_unstamped_dump_still_reads_as_android(self):
        """Every captured dump already in this suite predates this platform and must keep
        reading the way it always has."""
        assert device_mod.platform_from_dump(
            '<?xml version="1.0"?><hierarchy rotation="0"><node /></hierarchy>'
        ) == device_mod.ANDROID


class TestCapabilities:
    def test_web_can_clear_data_unlike_ios(self):
        """The one capability iOS has to honestly decline (no `pm clear` equivalent) is
        genuinely supported on web — cookies and local/session storage are clearable."""
        assert device_mod.supports(device_mod.WEB, "CLEAR_DATA")
        assert not device_mod.supports(device_mod.IOS, "CLEAR_DATA")

    def test_web_has_no_deeplink_or_recents_flag(self):
        """Not gaps to work around: DEEPLINK has no separate meaning once `launch` already
        navigates by URL, and a single browser tab has no app switcher."""
        assert not device_mod.supports(device_mod.WEB, "DEEPLINK")
        assert not device_mod.supports(device_mod.WEB, "RECENTS")

    def test_windows_can_reach_recents_unlike_ios_or_web(self):
        """Win+Tab (Task View) genuinely exists — the one non-Android platform with a real
        app-switcher equivalent. See test_windows_device.py for the fuller capability suite."""
        assert device_mod.supports(device_mod.WINDOWS, "RECENTS")
        assert not device_mod.supports(device_mod.WINDOWS, "CLEAR_DATA")


class TestDetectToolkit:
    def test_a_web_dump_gets_its_own_toolkit_bucket(self):
        """Not routed into Android's classifier: a page that is itself a webview has no
        native/hybrid/webview distinction to make, and a wrong-but-plausible Android bucket
        would quietly teach system_memory the wrong launch-settle budget for it."""
        import web_device
        xml = web_device.render_dom({"tag": "div", "children": []}, "https://example.com")
        assert device_mod.detect_toolkit(xml) == "web"
