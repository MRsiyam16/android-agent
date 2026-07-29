"""State identity and action extraction for autonomous exploration.

`compute_state_hash` decides what counts as "the same screen". Too strict and every
keystroke becomes a new state, the graph explodes and exploration never finishes; too loose
and genuinely different screens collapse into one, so whole areas of the app are never
visited. Both failure modes are silent — the run completes and simply explores the wrong
thing — which is why they are pinned here.
"""
from __future__ import annotations

from conftest import SCREEN_H, SCREEN_W, dump, node

from extractor import compute_state_hash, extract_actions

PKG, ACT = "com.example.app", ".MainActivity"


def hash_of(xml: str, package: str = PKG, activity: str = ACT) -> str:
    return compute_state_hash(package, activity, xml)


class TestStateHashStability:
    """Screens that only differ by dynamic content must hash identically."""

    def test_clock_change_does_not_fragment_the_state(self):
        a = dump(node(text="Inbox"), node(text="10:42"))
        b = dump(node(text="Inbox"), node(text="11:07"))
        assert hash_of(a) == hash_of(b)

    def test_calculator_result_does_not_fragment_the_state(self):
        # The original pattern was ^\d+$, so "3.14" and "1,234.56" leaked into the signature
        # and turned one calculator screen into dozens of "new" states.
        base = node(**{"class": "android.widget.Button", "content-desc": "7",
                       "clickable": "true", "bounds": "[0,1600][270,1780]"})
        for value in ("42", "-42", "3.14", "1,234.56", "12+34="):
            assert hash_of(dump(node(text=value), base)) == \
                   hash_of(dump(node(text="0"), base)), f"{value!r} fragmented the state"

    def test_relative_timestamps_do_not_fragment_the_state(self):
        for value in ("2m ago", "5 minutes", "just now", "Yesterday", "Edited 2m ago",
                      "120 words", "87%", "Mon, 14 Jul", "14/07/2026"):
            assert hash_of(dump(node(text=value))) == hash_of(dump(node(text="just now"))), \
                f"{value!r} was treated as static content"

    def test_typing_in_a_field_does_not_fragment_the_state(self):
        # Structural dedup is the whole reason a search flow does not explode: "search 'a'"
        # and "search 'ab'" are one state.
        def search(value: str) -> str:
            return dump(node(**{"class": "android.widget.EditText", "text": value,
                                "focusable": "true", "resource-id": "com.example.app:id/q"}))
        assert hash_of(search("a")) == hash_of(search("ab")) == hash_of(search(""))


class TestStateHashSensitivity:
    """Screens that genuinely differ must not collapse together."""

    def test_static_labels_do_change_the_state(self):
        assert hash_of(dump(node(text="Inbox"))) != hash_of(dump(node(text="Archive")))

    def test_activity_change_changes_the_state(self):
        xml = dump(node(text="Inbox"))
        assert hash_of(xml, activity=".MainActivity") != hash_of(xml, activity=".DetailActivity")

    def test_package_change_changes_the_state(self):
        xml = dump(node(text="Inbox"))
        assert hash_of(xml, package="com.example.app") != hash_of(xml, package="com.other.app")

    def test_a_new_control_changes_the_state(self):
        one = dump(node(text="Title"))
        two = dump(node(text="Title"), node(**{"text": "Delete", "clickable": "true"}))
        assert hash_of(one) != hash_of(two)

    def test_disabled_to_enabled_button_is_the_same_structural_state(self):
        # Documents current behaviour rather than asserting it is ideal: `enabled` is not part
        # of the signature, so a form whose submit button greys out does not read as a new
        # screen. If that ever needs to change, this test is the place it will surface.
        off = dump(node(**{"text": "Submit", "clickable": "true", "enabled": "false"}))
        on = dump(node(**{"text": "Submit", "clickable": "true", "enabled": "true"}))
        assert hash_of(off) == hash_of(on)


class TestStateHashRobustness:
    def test_malformed_dump_still_yields_a_usable_hash(self):
        # Must never crash the exploration loop; distinct garbage stays distinguishable.
        assert hash_of("<hierarchy><node unclosed=") != hash_of("<hierarchy><node other=")
        assert len(hash_of("")) == 64

    def test_hash_is_deterministic_across_calls(self):
        xml = dump(node(text="Inbox"), node(**{"text": "Go", "clickable": "true"}))
        assert hash_of(xml) == hash_of(xml)


class TestExtractActions:
    """Candidate taps for autonomous exploration — a deliberately narrower set."""

    def test_out_of_app_actions_are_skipped(self):
        # Exploration should not wander into the camera or a share sheet and spend its
        # remaining steps backtracking out of another app.
        xml = dump(
            node(**{"text": "Take photo", "clickable": "true", "bounds": "[100,600][980,720]"}),
            node(**{"text": "Share", "clickable": "true", "bounds": "[100,800][980,920]"}),
            node(**{"text": "Rename", "clickable": "true", "bounds": "[100,1000][980,1120]"}),
        )
        assert [a["label"] for a in extract_actions(xml, SCREEN_W, SCREEN_H)] == ["Rename"]

    def test_system_ui_elements_are_skipped(self):
        xml = dump(
            node(**{"package": "com.android.systemui", "text": "Wi-Fi", "clickable": "true",
                    "bounds": "[100,600][980,720]"}),
            node(**{"text": "Settings", "clickable": "true", "bounds": "[100,800][980,920]"}),
        )
        assert [a["label"] for a in extract_actions(xml, SCREEN_W, SCREEN_H)] == ["Settings"]

    def test_nav_bar_band_is_excluded_here_by_design(self, calculator_dump):
        # The counterpart to test_screen_reading's bottom-row test: exploration keeps the
        # wide 8% band so it does not mash the gesture nav bar. The agent uses its own
        # reader precisely because this one would hide the key.
        labels = [a["label"] for a in extract_actions(calculator_dump, SCREEN_W, SCREEN_H)]
        assert "equals" not in labels
        assert "7" in labels

    def test_label_falls_back_through_text_desc_id_then_class(self):
        clickable = {"clickable": "true", "bounds": "[100,600][980,720]"}
        assert extract_actions(dump(node(text="A", **{"content-desc": "B", **clickable})),
                               SCREEN_W, SCREEN_H)[0]["label"] == "A"
        assert extract_actions(dump(node(**{"content-desc": "B", **clickable})),
                               SCREEN_W, SCREEN_H)[0]["label"] == "B"
        assert extract_actions(dump(node(**{"resource-id": "com.example.app:id/go", **clickable})),
                               SCREEN_W, SCREEN_H)[0]["label"] == "go"
        assert extract_actions(dump(node(**{"class": "android.widget.Switch", **clickable})),
                               SCREEN_W, SCREEN_H)[0]["label"] == "Switch"

    def test_degenerate_bounds_are_dropped(self):
        xml = dump(
            node(**{"text": "Zero", "clickable": "true", "bounds": "[500,600][500,600]"}),
            node(**{"text": "Inverted", "clickable": "true", "bounds": "[980,720][100,600]"}),
            node(**{"text": "Unparseable", "clickable": "true", "bounds": "garbage"}),
            node(**{"text": "Real", "clickable": "true", "bounds": "[100,600][980,720]"}),
        )
        assert [a["label"] for a in extract_actions(xml, SCREEN_W, SCREEN_H)] == ["Real"]

    def test_malformed_dump_returns_empty_rather_than_raising(self):
        assert extract_actions("<hierarchy><node unclosed=", SCREEN_W, SCREEN_H) == []
        assert extract_actions("", SCREEN_W, SCREEN_H) == []
