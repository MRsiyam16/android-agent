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
