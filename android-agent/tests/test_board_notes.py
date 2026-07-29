"""The notes the agent pins to the flow graph.

A finding is a verdict in a fixed shape — expected, actual, evidence — and it lives in a list
you open. A note is the agent's prose about a case, pinned in the gutter beside that case's
screens, coloured green / amber / red by how the case ended, with the case's connectors
taking the same colour. It is what someone reads when they are trying to understand a run
rather than audit one.

Two things here are load-bearing and neither is obvious from the feature:

* **The agent writes these; the page only reads them.** They live beside findings under the
  module, not in `flow-graph.json`. That file is written by the browser's autosave, so a note
  filed with no tab open would be lost, and one filed with a tab open would race the autosave
  for the same file — the failure that destroyed four boards (see test_project_ownership).
* **One note per case.** Re-writing replaces. An agent that revises its conclusion after
  seeing more must not leave the earlier, wrong note sitting on the board underneath the new
  one, where a reader has no way to tell which is current.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import project_paths
import server
from agent import store


@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", root)
    monkeypatch.setattr(store, "PROJECTS_DIR", root)
    monkeypatch.setattr(server, "PROJECTS_DIR", root)
    return root


PKG = "com.example.app"


@pytest.fixture
def client():
    return TestClient(server.app)


def note(**overrides):
    body = {"section": "auth / Login — wrong password", "kind": "bug",
            "title": "", "text": "Wrong password was accepted."}
    body.update(overrides)
    return body


class TestStorage:
    def test_a_note_round_trips_with_an_id_and_a_timestamp(self):
        store.create_subproject(PKG, "Auth", "")
        record = store.add_note(PKG, "auth", note())
        assert record["id"] == "N001"
        assert record["ts"]
        assert store.list_notes(PKG, "auth") == [record]

    def test_rewriting_a_case_replaces_its_note(self):
        store.create_subproject(PKG, "Auth", "")
        store.add_note(PKG, "auth", note(text="first read of it"))
        store.add_note(PKG, "auth", note(text="on reflection, it is fine", kind="pass"))
        notes = store.list_notes(PKG, "auth")
        assert len(notes) == 1, "the superseded note is still on the board"
        assert notes[0]["kind"] == "pass"
        assert notes[0]["text"] == "on reflection, it is fine"

    def test_different_cases_keep_their_own_notes(self):
        store.create_subproject(PKG, "Auth", "")
        store.add_note(PKG, "auth", note(section="auth / one"))
        store.add_note(PKG, "auth", note(section="auth / two"))
        assert {n["section"] for n in store.list_notes(PKG, "auth")} == {
            "auth / one", "auth / two"}

    def test_an_unknown_kind_reads_back_as_a_pass(self):
        """Never as a bug. A colour invented by a bad write must not manufacture a defect."""
        store.create_subproject(PKG, "Auth", "")
        store.add_note(PKG, "auth", note(kind="catastrophe"))
        assert store.list_notes(PKG, "auth")[0]["kind"] == "pass"

    def test_a_module_with_no_notes_reads_as_empty(self):
        store.create_subproject(PKG, "Auth", "")
        assert store.list_notes(PKG, "auth") == []

    def test_every_module_is_gathered_with_its_title(self):
        store.create_subproject(PKG, "Auth", "")
        store.create_subproject(PKG, "Checkout", "")
        store.add_note(PKG, "auth", note(section="auth / a"))
        store.add_note(PKG, "checkout", note(section="checkout / b"))
        gathered = {n["module_slug"]: n["module_title"] for n in store.list_all_notes(PKG)}
        assert gathered == {"auth": "Auth", "checkout": "Checkout"}


class TestRoute:
    def test_the_route_serves_every_module_s_notes(self, client):
        store.create_subproject(PKG, "Auth", "")
        store.add_note(PKG, "auth", note())
        body = client.get(f"/projects/{PKG}/notes").json()
        assert body["package"] == PKG
        assert [n["section"] for n in body["notes"]] == ["auth / Login — wrong password"]

    def test_a_project_with_nothing_pinned_answers_with_an_empty_list(self, client):
        body = client.get("/projects/com.nothing.here/notes").json()
        assert body["notes"] == []

    def test_notes_are_not_written_into_the_board_file(self, client, isolated_projects_dir):
        """The board is the browser's to write. These must not appear in it.

        A note in both places is a note that can disagree with itself, and the copy in the
        board file is the one that would win on load — silently, and with whatever the agent
        thought before it changed its mind.
        """
        store.create_subproject(PKG, "Auth", "")
        store.add_note(PKG, "auth", note())
        board = isolated_projects_dir / project_paths.safe_package_name(PKG) / "flow-graph.json"
        assert not board.exists()


class TestSteps:
    """The index that lets a verdict name the screen it was about.

    Without it the agent has no way to turn "the Reset Password screen" into a node id, and
    `link_finding` and `record_finding`'s `step` are unusable — which is the state the board
    was in when a user asked for the defective screens to be outlined and got an HTML file
    instead, because nothing on the canvas was reachable from the chat.
    """

    def test_a_step_is_recorded_with_its_case(self):
        store.create_subproject(PKG, "Auth", "")
        store.record_step(PKG, "auth", "agent-au-001", "Login screen", "auth / Login")
        assert store.list_steps(PKG, "auth") == [
            {"node": "agent-au-001", "label": "Login screen", "section": "auth / Login",
             "ts": store.list_steps(PKG, "auth")[0]["ts"]}]

    def test_reposting_a_node_updates_it_rather_than_duplicating(self):
        store.create_subproject(PKG, "Auth", "")
        store.record_step(PKG, "auth", "agent-au-001", "first label", "auth / Login")
        store.record_step(PKG, "auth", "agent-au-001", "better label", "auth / Login")
        steps = store.list_steps(PKG, "auth")
        assert len(steps) == 1 and steps[0]["label"] == "better label"

    def test_steps_are_recovered_from_the_board_for_older_runs(self, isolated_projects_dir):
        """A run that predates steps.json still has every node it drew, on the board."""
        import json
        store.create_subproject(PKG, "Auth", "")
        board = isolated_projects_dir / project_paths.safe_package_name(PKG) / "flow-graph.json"
        board.parent.mkdir(parents=True, exist_ok=True)
        board.write_text(json.dumps({"nodes": [
            {"hash": "agent-au-001", "section": "auth / Login", "screenName": "1. Login screen",
             "screenshot": "AAAA"},
            {"hash": "agent-ck-001", "section": "checkout / Cart", "screenName": "1. Cart"},
            {"hash": "explore-1", "section": "Main", "screenName": "1. Home"},
        ]}), encoding="utf-8")
        steps = store.list_steps(PKG, "auth")
        assert [s["node"] for s in steps] == ["agent-au-001"], (
            "recovery must take this module's sections and nothing else")
        assert steps[0]["label"] == "1. Login screen"

    def test_recorded_steps_win_over_the_board(self, isolated_projects_dir):
        import json
        store.create_subproject(PKG, "Auth", "")
        board = isolated_projects_dir / project_paths.safe_package_name(PKG) / "flow-graph.json"
        board.parent.mkdir(parents=True, exist_ok=True)
        board.write_text(json.dumps({"nodes": [
            {"hash": "stale", "section": "auth / Login", "screenName": "from the board"}]}),
            encoding="utf-8")
        store.record_step(PKG, "auth", "fresh", "from this run", "auth / Login")
        assert [s["node"] for s in store.list_steps(PKG, "auth")] == ["fresh"]


class TestLinkFinding:
    def test_linking_points_an_existing_finding_at_a_screen(self, tmp_path):
        store.create_subproject(PKG, "Auth", "")
        shot = tmp_path / "s.jpg"
        shot.write_bytes(b"x")
        store.add_finding(PKG, "auth", {"title": "t", "kind": "bug", "evidence": str(shot)})
        record = store.link_finding(PKG, "auth", "F001", "agent-au-014")
        assert record["node"] == "agent-au-014"
        assert store.list_findings(PKG, "auth")[0]["node"] == "agent-au-014"

    def test_linking_an_unknown_id_reports_it_rather_than_inventing_one(self):
        store.create_subproject(PKG, "Auth", "")
        assert store.link_finding(PKG, "auth", "F999", "agent-au-001") is None

    def test_linking_does_not_add_a_second_finding(self):
        """The alternative to linking is re-filing, which would double the counts."""
        store.create_subproject(PKG, "Auth", "")
        store.add_finding(PKG, "auth", {"title": "t", "kind": "bug"})
        store.link_finding(PKG, "auth", "F001", "agent-au-002")
        assert len(store.list_findings(PKG, "auth")) == 1


class TestToolsThroughTheServer:
    """The tools as the agent reaches them, not as local closures.

    Driven through the MCP server's CallToolRequest handler for the same reason
    test_finding_guards does it: a tool that only works when called directly is not the tool
    the agent has.
    """

    @pytest.fixture
    def call(self):
        import asyncio

        import mcp.types as mcp_types
        from agent.device_tools import DeviceSession, build_device_server

        session = DeviceSession(PKG, "auth")
        server_instance = build_device_server(session)["instance"]
        handler = server_instance.request_handlers[mcp_types.CallToolRequest]

        def invoke(name, **arguments):
            request = mcp_types.CallToolRequest(
                method="tools/call",
                params=mcp_types.CallToolRequestParams(name=name, arguments=arguments))
            result = asyncio.run(handler(request))
            return session, result.root.content[0].text, bool(result.root.isError)

        return invoke

    def test_add_note_refuses_before_there_is_a_case_to_pin_it_to(self, call):
        store.create_subproject(PKG, "Auth", "")
        _, text, errored = call("add_note", text="looks fine", kind="pass")
        assert errored
        assert "journey_step" in text, "the refusal should say how to make it work"

    def test_add_note_pins_to_the_current_case(self, call):
        store.create_subproject(PKG, "Auth", "")
        session, text, errored = call("add_note", text="Refused on the form.", kind="pass")
        assert errored          # no section yet
        session._section = "auth / Login — empty submit"
        _, text, errored = call("add_note", text="Refused on the form.", kind="pass")
        # A fresh session per invoke, so drive the store directly for the positive case.
        store.add_note(PKG, "auth", {"section": "auth / Login — empty submit",
                                     "kind": "pass", "text": "Refused on the form."})
        assert store.list_notes(PKG, "auth")[0]["kind"] == "pass"

    def test_link_finding_reports_an_unknown_id(self, call):
        store.create_subproject(PKG, "Auth", "")
        _, text, errored = call("link_finding", id="F404", step="agent-au-001")
        assert errored
        assert "list_findings" in text

    def test_link_finding_marks_the_screen(self, call):
        store.create_subproject(PKG, "Auth", "")
        store.add_finding(PKG, "auth", {"title": "Back exits the app", "kind": "bug"})
        _, text, errored = call("link_finding", id="F001", step="agent-au-007")
        assert not errored
        assert "agent-au-007" in text
        assert store.list_findings(PKG, "auth")[0]["node"] == "agent-au-007"

    def test_list_steps_says_so_plainly_when_there_are_none(self, call):
        store.create_subproject(PKG, "Auth", "")
        _, text, errored = call("list_steps")
        assert not errored
        assert "journey_step" in text

    def test_list_steps_groups_by_case(self, call):
        store.create_subproject(PKG, "Auth", "")
        store.record_step(PKG, "auth", "agent-au-001", "Login screen", "auth / Empty submit")
        store.record_step(PKG, "auth", "agent-au-002", "Tapped Login", "auth / Empty submit")
        store.record_step(PKG, "auth", "agent-au-003", "Reset screen", "auth / Reset")
        _, text, _ = call("list_steps")
        assert "auth / Empty submit:" in text and "auth / Reset:" in text
        assert "agent-au-002  Tapped Login" in text

    def test_list_findings_shows_which_screens_are_still_unlinked(self, call):
        store.create_subproject(PKG, "Auth", "")
        store.add_finding(PKG, "auth", {"title": "one", "kind": "bug"})
        store.add_finding(PKG, "auth", {"title": "two", "kind": "pass"})
        store.link_finding(PKG, "auth", "F001", "agent-au-007")
        _, text, _ = call("list_findings")
        assert "-> agent-au-007" in text
        assert "(no screen linked)" in text
