"""Breadcrumb naming for discovered screens.

These names are the single source of truth shared by the dashboard's node headers and the
text-only /map endpoint, so the two views can never disagree about what a screen is called.
The naming is also assigned once, on first sight, and must stay put: a screen that renumbers
itself mid-session makes every note the user has written about "screen 12" wrong.
"""
from __future__ import annotations

import pytest

import server


@pytest.fixture(autouse=True)
def clean_naming_state():
    server._reset_screen_naming()
    yield
    server._reset_screen_naming()


class TestSectionTrigger:
    """Which action labels are meaningful enough to name a section after."""

    @pytest.mark.parametrize("label", ["Settings", "Search", "Notifications", "My Account"])
    def test_wordy_labels_start_a_section(self, label):
        assert server._is_section_trigger(label) is True

    @pytest.mark.parametrize("label", [
        "Button", "TextView", "LinearLayout", "unlabeled element",  # generic chrome
        "3+4=", "42", "7",                                          # keypad noise
        "", None, "x",                                              # nothing to name
    ])
    def test_generic_and_numeric_labels_do_not(self, label):
        # Without this a calculator run produces sections called "7 > 8 > +" — the breadcrumb
        # becomes a keystroke log instead of a map.
        assert server._is_section_trigger(label) is False

    def test_matching_is_case_insensitive_for_the_generic_list(self):
        assert server._is_section_trigger("BUTTON") is False
        assert server._is_section_trigger("FrameLayout") is False


class TestScreenRegistration:
    def test_first_screen_is_home_and_numbered_one(self):
        server._register_screen("aaa", None, None)
        assert server.screen_names["aaa"] == "Home"
        assert server.node_index["aaa"] == 1

    def test_meaningful_action_extends_the_breadcrumb(self):
        server._register_screen("aaa", None, None)
        server._register_screen("bbb", "aaa", "Settings")
        server._register_screen("ccc", "bbb", "Notifications")
        # The root is "Home", and every path is rooted there — the breadcrumb reads as a
        # route from the app's entry point rather than a floating fragment.
        assert server.screen_names["ccc"] == "Home > Settings > Notifications"

    def test_generic_action_inherits_the_parents_path(self):
        server._register_screen("aaa", None, None)
        server._register_screen("bbb", "aaa", "Settings")
        server._register_screen("ccc", "bbb", "Button")
        # Same place as its parent as far as naming goes, so it is disambiguated by number.
        assert server.screen_names["ccc"] == "Home > Settings: 2"

    def test_screens_sharing_a_path_are_numbered_apart(self):
        server._register_screen("aaa", None, None)
        server._register_screen("bbb", "aaa", "Settings")
        server._register_screen("ccc", "aaa", "Settings")
        assert server.screen_names["bbb"] == "Home > Settings"
        assert server.screen_names["ccc"] == "Home > Settings: 2"

    def test_a_name_is_assigned_once_and_never_changes(self):
        # Re-registration happens on every revisit; if it renamed, the user's annotations
        # would drift onto different screens as exploration continued.
        server._register_screen("aaa", None, "Settings")
        first = server.screen_names["aaa"]
        server._register_screen("aaa", "bbb", "Something Else")
        assert server.screen_names["aaa"] == first
        assert server.node_index["aaa"] == 1
        assert len(server.node_order) == 1

    def test_explicit_name_is_used_verbatim_for_journey_steps(self):
        # A scripted journey names its own steps ("3. Tap 5 -> 7+5"), which beats any
        # breadcrumb derived from keypad labels.
        server._register_screen("aaa", None, "7", explicit_name="3. Tap 5 -> 7+5")
        assert server.screen_names["aaa"] == "3. Tap 5 -> 7+5"

    def test_numbering_follows_discovery_order(self):
        for i, h in enumerate(["aaa", "bbb", "ccc"], start=1):
            server._register_screen(h, None, None)
            assert server.node_index[h] == i
        assert server.node_order == ["aaa", "bbb", "ccc"]

    def test_reset_clears_everything_the_dashboard_reads(self):
        server._register_screen("aaa", None, "Settings")
        server._reset_screen_naming()
        assert not server.screen_names and not server.node_index and not server.node_order
