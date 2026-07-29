"""What the agent is told the screen contains — device_tools' three readers.

These three functions decide what the testing agent believes about the phone. If
`package_ranking` names the wrong owner, every conclusion in the run is about the wrong
window; if `screen_elements` drops a control, the agent reports a missing feature that is
right there. Both have happened.
"""
from __future__ import annotations

from conftest import dump, node

from agent.device_tools import package_ranking, screen_elements, screen_texts


class TestPackageRanking:
    """Which app owns the screen. Volume decides, not document order."""

    def test_status_bar_first_in_document_does_not_win(self, login_form_dump):
        # The first package= attribute in a real dump is usually com.android.systemui, so
        # reading position 0 to answer "which app is on screen" is wrong.
        assert package_ranking(login_form_dump)[0][0] == "com.example.app"

    def test_soft_keyboard_excluded_despite_dominating_node_count(self, keyboard_open_dump):
        ranking = package_ranking(keyboard_open_dump)
        packages = [pkg for pkg, _ in ranking]
        assert "com.google.android.inputmethod.latin" not in packages
        # 180 IME nodes vs 2 app nodes: without the exclusion the app loses its own screen
        # and the wrong-app guard fires on every search flow.
        assert ranking[0][0] == "com.example.app"

    def test_permission_dialog_is_reported_as_the_owner(self, permission_dialog_dump):
        # Not a defect in the app — but the agent must be told the app is not what it is
        # looking at, or it concludes the app rendered nothing.
        assert package_ranking(permission_dialog_dump)[0][0] == "com.android.permissioncontroller"

    def test_malformed_dump_returns_empty_rather_than_raising(self):
        assert package_ranking("<hierarchy><node unclosed=") == []
        assert package_ranking("") == []


class TestParseCache:
    """The readers share one parse of the dump. A stale hit would be a wrong screen."""

    def test_a_different_dump_is_never_served_the_previous_tree(self):
        first = dump(node(text="Inbox"))
        second = dump(node(text="Archive"))
        assert screen_texts(first) == ["Inbox"]
        assert screen_texts(second) == ["Archive"]
        assert screen_texts(first) == ["Inbox"]

    def test_equal_but_distinct_strings_agree(self):
        # Two dumps that compare equal must give the same answer whichever object arrives —
        # the cache checks identity first, then falls back to equality.
        a = dump(node(text="Inbox"))
        b = (a + " ")[:-1]  # equal value, genuinely distinct object
        assert a is not b and a == b
        assert screen_texts(a) == screen_texts(b) == ["Inbox"]

    def test_a_malformed_dump_does_not_poison_the_next_read(self):
        assert screen_texts("<hierarchy><node unclosed=") == []
        assert screen_texts(dump(node(text="Inbox"))) == ["Inbox"]

    def test_all_three_readers_agree_on_one_dump(self, login_form_dump, screen):
        # The three of them are handed the same string in immediate succession by
        # read_screen; each must see the same tree.
        assert package_ranking(login_form_dump)[0][0] == "com.example.app"
        assert "Sign in to continue" in screen_texts(login_form_dump)
        # Back header, two fields, submit. The caption is text, not a tap target — which is
        # exactly the distinction the three readers exist to keep straight.
        assert len(screen_elements(login_form_dump, *screen)) == 4


class TestScreenTexts:
    """What the screen says. Deliberately includes input values, unlike the state hash."""

    def test_includes_edittext_values(self, keyboard_open_dump):
        # compute_state_hash strips these on purpose; a tester asking "what does it say?"
        # needs them — a typed value or a validation message is precisely the answer.
        assert "gre" in screen_texts(keyboard_open_dump)

    def test_splash_screen_has_nodes_but_no_text(self, splash_dump):
        # The readiness signal: wait for text, not for node count.
        assert splash_dump.count("<node") == 15
        assert screen_texts(splash_dump) == []

    def test_deduplicates_but_keeps_document_order(self):
        xml = dump(
            node(text="Alpha"), node(text="Beta"), node(text="Alpha"),
            node(**{"content-desc": "Gamma"}),
        )
        assert screen_texts(xml) == ["Alpha", "Beta", "Gamma"]

    def test_keyboard_key_labels_do_not_flood_the_text(self, keyboard_open_dump):
        texts = screen_texts(keyboard_open_dump)
        assert not any(t.startswith("key-") for t in texts)


class TestScreenElements:
    """What the agent can tap, and how it is told to disambiguate."""

    def test_appbar_and_primary_button_are_both_present_but_flagged_apart(
            self, login_form_dump, screen):
        elements = screen_elements(login_form_dump, *screen)
        logins = [e for e in elements if e["label"] == "Login"]
        assert len(logins) == 2, "both the back header and the submit button carry this label"
        # The flag is what lets tap_text exclude the header — without it, first-match taps
        # the app bar, navigates back, and looks like the app rejected the input.
        assert sorted(e["in_appbar"] for e in logins) == [False, True]

    def test_bottom_row_control_survives_the_agents_tighter_band(self, calculator_dump, screen):
        # The whole reason screen_elements exists instead of reusing extract_actions: at
        # EXCLUDE_BOTTOM_PCT=0.08 this element is 82px below the cutoff and simply absent.
        labels = [e["label"] for e in screen_elements(calculator_dump, *screen)]
        assert "equals" in labels

    def test_editable_fields_are_marked(self, login_form_dump, screen):
        elements = screen_elements(login_form_dump, *screen)
        editable = [e for e in elements if e["editable"]]
        assert {e["resource_id"].split("/")[-1] for e in editable} == {"email", "password"}

    def test_disabled_elements_are_not_offered(self, screen):
        xml = dump(
            node(**{"text": "Submit", "clickable": "true", "enabled": "false",
                    "bounds": "[100,1000][980,1120]"}),
            node(**{"text": "Cancel", "clickable": "true", "bounds": "[100,1200][980,1320]"}),
        )
        assert [e["label"] for e in screen_elements(xml, *screen)] == ["Cancel"]

    def test_ids_are_stable_centre_coordinates(self, screen):
        xml = dump(node(**{"text": "Go", "clickable": "true", "bounds": "[100,600][980,720]"}))
        element = screen_elements(xml, *screen)[0]
        assert element["id"] == "540_660" == f"{element['x']}_{element['y']}"

    def test_elements_stacked_at_one_point_are_collapsed(self, screen):
        # A clickable row wrapping a clickable label at identical bounds is one tap target,
        # not two; offering both invites the agent to "try the other one" forever.
        same = {"clickable": "true", "bounds": "[100,600][980,720]"}
        xml = dump(node(text="Row", **same), node(**{"content-desc": "Row label", **same}))
        assert len(screen_elements(xml, *screen)) == 1

    def test_zero_controls_on_a_covered_screen_is_detectable(self, splash_dump, screen):
        # read_screen turns this into "almost never a real app state" — the tell for an
        # overlay eating input, or a screen that has not rendered.
        assert screen_elements(splash_dump, *screen) == []

    def test_malformed_dump_returns_empty_rather_than_raising(self, screen):
        assert screen_elements("<hierarchy><node unclosed=", *screen) == []
        assert screen_elements("", *screen) == []

    def test_zero_sized_screen_returns_empty(self, login_form_dump):
        assert screen_elements(login_form_dump, 0, 0) == []
