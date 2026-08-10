"""The iOS adapter's translation layer, and the one Android rule that inverts on iPhone.

`IOSDevice.dump_xml` renders WebDriverAgent's JSON element tree into the `<node>` XML that
`agent/screen.py` and `extractor.py` already parse, so the readers above the device layer
did not have to fork. That translation is the load-bearing part of iOS support: if it drifts,
every reader silently starts seeing a different screen than the phone is showing.

The trees below are not generic samples. Each reproduces a shape measured on a real
iPhone 12 (iOS 18.7.8) during the run that motivated this adapter — in particular the
YouTube player, which publishes no play/pause element at all and would otherwise be reported
as a defect.

No device and no WebDriverAgent: `render_dump` is a pure function over a captured tree, the
same way the Android dump readers are pure functions over a captured dump.
"""
from __future__ import annotations

import ios_device
from agent import screen
from ios_device import render_dump

import device as device_mod

# An iPhone 12 in points. Everything the adapter reports — bounds, taps, the downscaled
# screenshot — is in these units, so a coordinate from `screen_elements` is directly tappable.
IPHONE_W, IPHONE_H = 390, 844

BUNDLE = "com.google.ios.youtube"


def wda(type_: str, *, name: str = "", label: str = "", value=None,
        x: int = 0, y: int = 0, w: int = 100, h: int = 40,
        children: list | None = None, **extra) -> dict:
    """One WDA element, with the field names WebDriverAgent actually returns."""
    node: dict = {
        "type": f"XCUIElementType{type_}",
        "name": name,
        "label": label,
        "rect": {"x": x, "y": y, "width": w, "height": h},
        "children": children or [],
    }
    if value is not None:
        node["value"] = value
    node.update(extra)
    return node


def app(*children: dict) -> dict:
    """The Application root WDA puts at the top of every tree."""
    return wda("Application", name="YouTube", label="YouTube",
               x=0, y=0, w=IPHONE_W, h=IPHONE_H, children=list(children))


def elements_of(tree: dict) -> list[dict]:
    return screen.screen_elements(render_dump(tree, BUNDLE), IPHONE_W, IPHONE_H)


# --- the translation itself -------------------------------------------------------

class TestTranslation:
    def test_dump_is_stamped_ios_so_readers_can_dispatch(self):
        """Platform is recoverable from the dump alone — no device, no session.

        `detect_toolkit` and the zero-controls guard both branch on this, and both run in
        places that hold nothing but the string.
        """
        xml = render_dump(app(), BUNDLE, udid="00008101-0006656021F8001E")
        assert 'platform="ios"' in xml
        assert device_mod.platform_from_dump(xml) == device_mod.IOS

    def test_android_dump_is_not_mistaken_for_ios(self, ):
        """Every existing captured dump in this suite must keep reading as Android."""
        assert device_mod.platform_from_dump(
            '<?xml version="1.0"?><hierarchy rotation="0"><node /></hierarchy>'
        ) == device_mod.ANDROID

    def test_button_becomes_a_touchable_element(self):
        tree = app(wda("Button", name="id.ui.navigation.search.button", label="Search",
                       x=344, y=51, w=36, h=36))
        [element] = elements_of(tree)
        assert element["label"] == "Search"
        assert element["resource_id"] == "id.ui.navigation.search.button"
        assert element["class"] == "Button"

    def test_bounds_become_a_tappable_centre_in_points(self):
        """The centre `screen_elements` reports is the point `click()` is given.

        Nothing rescales between the two, which is why the adapter keeps everything in
        points: WDA taps in points but screenshots in pixels, and mixing the two puts a tap
        a third of the way across the screen from its target.
        """
        tree = app(wda("Button", label="Search", x=344, y=51, w=36, h=36))
        [element] = elements_of(tree)
        assert (element["x"], element["y"]) == (362, 69)
        assert element["bounds"] == [344, 51, 380, 87]

    def test_text_field_is_editable_and_carries_its_typed_value(self):
        """`screen.py` decides "is this an input" from the class name, so the mapping onto
        an Android-shaped `EditText` is what makes typing discoverable at all."""
        tree = app(wda("TextField", name="id.navigation.search.text_field",
                       label="Search", value="pera nai chill", x=16, y=51, w=350, h=36))
        [element] = elements_of(tree)
        assert element["editable"] is True
        assert "pera nai chill" in screen.screen_texts(render_dump(tree, BUNDLE))

    def test_switch_reports_its_checked_state(self):
        on = elements_of(app(wda("Switch", label="Autoplay", value="1")))
        off = elements_of(app(wda("Switch", label="Autoplay", value="0")))
        assert on[0]["checked"] == "true"
        assert off[0]["checked"] == "false"

    def test_switch_value_is_not_leaked_as_visible_text(self):
        """A switch's value is "1"/"0", which read as screen text would put a bare "1" in
        front of the agent as if the app had displayed it."""
        assert "1" not in screen.screen_texts(
            render_dump(app(wda("Switch", label="Autoplay", value="1")), BUNDLE))

    def test_disabled_elements_are_not_offered_as_tappable(self):
        assert elements_of(app(wda("Button", label="Submit", isEnabled=False))) == []

    def test_static_text_is_readable_but_not_tappable(self):
        tree = app(wda("StaticText", label="Loading videos", x=20, y=200, w=200, h=20))
        assert elements_of(tree) == []
        assert "Loading videos" in screen.screen_texts(render_dump(tree, BUNDLE))

    def test_labels_needing_xml_escaping_survive(self):
        """App copy contains ampersands and quotes; an unescaped one makes the whole dump
        unparseable, which surfaces as a screen that reads as completely empty."""
        tree = app(wda("Button", label='Terms & "Conditions" <b>'))
        [element] = elements_of(tree)
        assert element["label"] == 'Terms & "Conditions" <b>'

    def test_ownership_ranking_names_the_bundle_under_test(self):
        xml = render_dump(app(wda("Button", label="Search")), BUNDLE)
        assert screen.package_ranking(xml)[0][0] == BUNDLE


# --- the keyboard, which is the Gboard lesson again -------------------------------

class TestKeyboardExclusion:
    """The iOS keyboard is the same trap as Gboard: dozens of nodes that are not the app.

    Left attributed to the app under test, its keys land in the element list on every typing
    flow and its node count distorts the ownership ranking — the exact failure
    `screen._is_ime` was written to prevent on Android.
    """

    def _tree(self) -> dict:
        keys = [wda("Button", label=c, x=i * 30, y=600, w=28, h=40)
                for i, c in enumerate("qwertyuiop")]
        return app(
            wda("Button", name="id.ui.navigation.search.button", label="Search",
                x=344, y=51, w=36, h=36),
            wda("Keyboard", label="keyboard", x=0, y=560, w=IPHONE_W, h=280,
                children=keys),
        )

    def test_keys_are_not_offered_as_app_elements(self):
        labels = [e["label"] for e in elements_of(self._tree())]
        assert labels == ["Search"]

    def test_keyboard_does_not_win_the_ownership_ranking(self):
        ranking = screen.package_ranking(render_dump(self._tree(), BUNDLE))
        assert ranking[0][0] == BUNDLE
        assert all("keyboard" not in pkg for pkg, _ in ranking)


# --- the player: the false defect this adapter exists to prevent -------------------

def player_tree() -> dict:
    """YouTube's watch screen, as measured on iOS 18.7.8.

    Every named element under the player is here. Note what is absent: play, pause, next and
    previous. They are drawn inside the `Video Player` surface and XCUITest cannot see them,
    even though a screenshot shows them plainly.
    """
    return app(
        wda("Other", name="id.player", x=0, y=47, w=IPHONE_W, h=176, children=[
            wda("Other", name="id.player.overlay", x=0, y=47, w=IPHONE_W, h=176),
            wda("Button", name="Video Player", label="Video Player",
                x=0, y=47, w=IPHONE_W, h=176),
            wda("Button", name="id.player.watch.fullscreen.button",
                label="Full screen", x=336, y=207, w=40, h=30),
            wda("Other", name="id.player.scrubber.slider", label="Track Position",
                x=0, y=223, w=IPHONE_W, h=8),
        ]),
    )


class TestCustomDrawnSurface:
    def test_player_exposes_no_transport_controls(self):
        """Documents the limitation as an assertion rather than as prose.

        Asserted as an exact set rather than by searching for "play": the fullscreen
        button's id is `id.player.watch.fullscreen.button`, and a substring test matches the
        "play" inside "player" — which is how a check like this quietly passes for the wrong
        reason. If a future WebDriverAgent or YouTube build starts publishing real transport
        controls, this fails and the coordinate fallback in the prompt can be relaxed.
        """
        labels = {e["label"] for e in elements_of(player_tree())}
        assert labels == {"Video Player", "Full screen"}

    def test_the_surface_itself_is_still_tappable(self):
        """Tapping the surface is how the overlay is raised, so it must survive."""
        assert "Video Player" in {e["label"] for e in elements_of(player_tree())}


class TestZeroControlsGuard:
    """On Android zero touchable controls means something is covering the screen. On iOS it
    routinely means a custom-drawn surface. Telling an iOS agent to "deal with the overlay"
    sends it hunting for a problem that is not there — this harness's definition of a false
    defect — so the two platforms must be told different things.
    """

    class _Session:
        package = BUNDLE

    def _note(self, xml: str) -> str:
        from agent.device_tools import _render_screen
        return _render_screen(self._Session(), xml, IPHONE_W, IPHONE_H, [], [])

    def test_ios_is_not_told_to_blame_an_overlay(self):
        note = self._note(render_dump(app(), BUNDLE))
        assert "screenshot" in note.lower()
        assert "custom-drawn" in note.lower()

    def test_android_keeps_the_original_overlay_warning(self):
        note = self._note('<?xml version="1.0"?><hierarchy rotation="0"></hierarchy>')
        assert "overlay is intercepting input" in note


# --- toolkit and platform dispatch --------------------------------------------------

class TestDispatch:
    def test_toolkit_names_are_prefixed_so_they_cannot_pool_with_android(self):
        """System memory keys learned launch waits by toolkit. An iPhone's numbers filed
        under a bare "native" would be averaged with Android's and teach both a wrong
        budget."""
        toolkit = device_mod.detect_toolkit(
            render_dump(app(wda("Button", name="id.a", label="A")), BUNDLE))
        assert toolkit.startswith("ios-")

    def test_webview_is_recognised(self):
        assert ios_device.detect_toolkit(
            render_dump(app(wda("WebView", label="content")), BUNDLE)) == "ios-webview"

    def test_android_dumps_still_reach_the_android_reader(self):
        import adb_device
        xml = ('<?xml version="1.0"?><hierarchy rotation="0">'
               '<node class="android.webkit.WebView" /></hierarchy>')
        assert device_mod.detect_toolkit(xml) == adb_device.detect_toolkit(xml) == "webview"

    def test_udid_shape_identifies_ios_without_touching_a_device(self):
        assert device_mod.platform_from_serial("00008101-0006656021F8001E") == device_mod.IOS

    def test_an_adb_serial_is_not_guessed_at(self):
        """None, not "android": an adb serial can be almost any string, so the absence of a
        UDID shape is not evidence — and a guess here would route a real iPhone to the
        Android adapter."""
        assert device_mod.platform_from_serial("R58M12ABCDE") is None
        assert device_mod.platform_from_serial(None) is None


class TestCapabilities:
    def test_ios_gaps_are_declared_rather_than_discovered_at_runtime(self):
        """These are absent capabilities, not unimplemented methods: iOS has no `pm clear`
        and cannot launch to a screen by URL. Callers branch on the flag instead of catching
        an exception from a call that was never going to work."""
        for capability in ("CLEAR_DATA", "DEEPLINK", "LIVE_LOG", "RECENTS"):
            assert device_mod.supports(device_mod.ANDROID, capability)
            assert not device_mod.supports(device_mod.IOS, capability)


class TestPrompt:
    def test_ios_modules_are_warned_about_the_inverted_rule(self):
        from agent import prompts
        text = prompts.build_system_prompt(BUNDLE, "player", "Player", "", platform="ios")
        assert "Zero touchable controls is often an ordinary iOS screen" in text
        assert "no `pm clear`" in text

    def test_android_prompt_is_unchanged_by_default(self):
        from agent import prompts
        assert "iPhone: where the rules above change" not in prompts.build_system_prompt(
            "com.example.app", "auth", "Auth", "")
