"""The prewritten prompts the composer offers on an empty module.

Small feature, two things worth pinning.

The first is that the presets keep working as *prompts*. Each one leans on behaviour the
system prompt promises — a `case` name so a run draws as its own chain, `add_note`'s gutter
placement, `record_finding`'s `step` outline, `wait_until_gone` before judging a submit. That
is why the wording lives in `agent/prompts.py` next to the rules rather than in the frontend:
a preset that drifts from the prompt is a preset asking for a tool behaviour that no longer
exists, and it would fail silently — the agent would simply do something else and nobody
would know the instruction had rotted. The assertions below are the tripwire for that.

The second is the endpoint's path. `/agent/prompt-presets` sits among `/agent/{package}/...`
routes, and "prompt-presets" is a perfectly good package name as far as the router is
concerned. If a later route ever claims two segments with a `{package}` in the second
position, this file is what notices.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server
from agent import prompts


@pytest.fixture
def client():
    return TestClient(server.app)


class TestTheList:
    def test_every_preset_has_what_the_composer_renders(self):
        """id, label, blurb, text. A preset missing `text` would render as a button that
        sends nothing, which looks exactly like the agent ignoring you."""
        assert prompts.PRESET_PROMPTS, "the whole feature is an empty strip without these"
        for preset in prompts.PRESET_PROMPTS:
            assert set(preset) == {"id", "label", "blurb", "text"}, preset.get("id")
            for field, value in preset.items():
                assert value.strip(), f"{preset.get('id')}.{field} is empty"

    def test_ids_are_unique(self):
        ids = [p["id"] for p in prompts.PRESET_PROMPTS]
        assert len(ids) == len(set(ids))

    def test_labels_are_short_enough_to_read_in_the_rail(self):
        """The strip sits between the chat log and the composer in a rail that can be dragged
        down to 300px. A label that wraps to three lines pushes the input box off-screen."""
        for preset in prompts.PRESET_PROMPTS:
            assert len(preset["label"]) <= 48, preset["label"]

    def test_the_end_to_end_preset_is_first(self):
        """It is the one that gets used on most modules, and the strip is read top-down."""
        assert prompts.PRESET_PROMPTS[0]["id"] == "end-to-end"

    def test_preset_prompts_hands_out_copies(self):
        """The route serialises these straight to the browser. A caller mutating what it got
        back would edit the module-level list for the life of the process, and every later
        module would be offered the changed wording with nothing to say it had changed."""
        first = prompts.preset_prompts()
        first[0]["text"] = "wiped"
        assert prompts.preset_prompts()[0]["text"] != "wiped"


class TestTheyStillAskForThingsTheAgentCanDo:
    """Each assertion is a preset leaning on a specific promise in the system prompt.

    Not style checks. If `journey_step` stopped taking a `case`, or `add_note` stopped being
    per-case, these presets would still send — and would quietly produce runs drawn wrongly on
    the board. Failing here is the signal to rewrite the preset, not to delete the test.
    """

    @pytest.fixture
    def by_id(self):
        return {p["id"]: p["text"] for p in prompts.PRESET_PROMPTS}

    def test_the_tools_they_name_are_tools_the_agent_has(self, by_id):
        from agent.device_tools import DEVICE_TOOL_NAMES

        available = {name.replace("mcp__device__", "") for name in DEVICE_TOOL_NAMES}
        # Every backticked identifier in a preset that looks like a tool call must exist.
        # `case` and `section` are argument names, so they are excluded by construction: the
        # set is checked against tool names only.
        for preset_id, text in by_id.items():
            for candidate in ("journey_step", "add_note", "record_finding", "link_finding",
                              "list_steps", "list_findings", "wait_until_gone",
                              "propose_subprojects", "ask_user"):
                if candidate in text:
                    assert candidate in available, f"{preset_id} asks for {candidate}"

    def test_the_end_to_end_preset_asks_for_a_new_case(self, by_id):
        """The point of it: a chain of its own on the board instead of extending an old one."""
        text = by_id["end-to-end"]
        assert "case name" in text
        assert "journey_step" in text and "add_note" in text

    def test_the_mark_up_preset_does_not_drive_the_phone(self, by_id):
        """It exists for a finished run. If it started testing it would append to a run the
        user asked it to annotate."""
        text = by_id["review-and-mark-up"]
        assert "Do not test anything on the phone" in text
        assert "list_steps" in text and "link_finding" in text

    def test_the_mark_up_preset_carries_the_section_spelling_rule(self, by_id):
        """Marking up a finished run has no current case, so `add_note` needs `section`
        spelled exactly as `list_steps` shows it, module prefix included. Leaving that out is
        why the preset would otherwise fail on its first note."""
        assert "module prefix included" in by_id["review-and-mark-up"]

    def test_the_negative_input_preset_carries_the_in_flight_rule(self, by_id):
        """Both of this harness's worst incidents were verdicts read off a mid-flight screen,
        and a preset that fires off negative-input cases is the one most likely to hit it."""
        text = by_id["negative-inputs"]
        assert "wait_until_gone" in text
        assert "reactive" in text or "reactively" in text


class TestTheEndpoint:
    def test_it_serves_the_list(self, client):
        resp = client.get("/agent/prompt-presets")
        assert resp.status_code == 200
        assert [p["id"] for p in resp.json()["presets"]] == [
            p["id"] for p in prompts.PRESET_PROMPTS]

    def test_it_is_not_swallowed_by_the_package_routes(self, client):
        """"prompt-presets" is a valid `{package}`. This is the assertion that fails if a
        route matching two segments with a package in the second is ever added above it."""
        body = client.get("/agent/prompt-presets").json()
        assert "presets" in body, f"something else answered: {body}"

    def test_it_needs_no_project(self, client):
        """The strip is rendered before a module is chosen, and on a fresh install there is no
        project at all. A 404 here would mean the presets only appear once you have one."""
        assert client.get("/agent/prompt-presets").status_code == 200
