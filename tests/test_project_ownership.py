"""Which project a board and a run's screenshots belong to.

This file exists because getting it wrong destroyed four saved boards. The dashboard tracked
"the app whose screen I am looking at" and used it as "the project this board belongs to",
which are the same value right up until a run wanders out of its own app — and a run wanders
constantly, into the Play Store, a browser, the permission controller.

What was lost, and how:

* `com.google.android.youtube/flow-graph.json` holds 39 Metaesthetics screens. Its meta still
  says `last_run_at` 26 Jul and `last_saved_at` 27 Jul — the day a different project was open.
  A debounced autosave fired after a project switch and wrote the outgoing board under the
  incoming project's name.
* `com.android.chrome` holds deskclock screens, and `com.google.android.keep` holds Play Store
  screens: a run that followed a link out of its own app flipped the tracked package, and the
  next autosave filed the whole board under the app it had merely passed through.
* `com.android.vending` and `com.google.android.gms` are project folders that exist only
  because a run touched those packages in passing.

None of it was recoverable. So both halves are pinned here: the server must refuse a save
whose board says it belongs to someone else, and telemetry must file by the run's target
rather than by whatever is on screen.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import project_paths
import server


@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", root)
    monkeypatch.setattr(server, "PROJECTS_DIR", root)
    server.state_store.clear()
    server.telemetry_history.clear()
    server._reset_screen_naming()
    yield root
    server.state_store.clear()
    server.telemetry_history.clear()
    server._reset_screen_naming()


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


def board(package: str | None, screens: int = 1) -> dict:
    return {
        "version": 1,
        "package": package,
        "nodes": [{"hash": f"h{i}", "package": package} for i in range(screens)],
        "edges": [],
        "notes": [], "comments": [], "nodeStatus": [], "nodePositions": [],
        "sections": [], "sectionOrder": [],
    }


class TestSaveGuard:
    def test_a_board_saves_to_its_own_project(self, client):
        resp = client.post("/projects/com.a.app/flow-graph", json=board("com.a.app"))
        assert resp.status_code == 200

    def test_a_board_cannot_be_saved_over_another_project(self, client):
        # The exact shape of the YouTube incident: the board on screen belongs to one project,
        # the request is aimed at another. Refusing is the only outcome that keeps the data.
        client.post("/projects/com.victim.app/flow-graph", json=board("com.victim.app", 39))
        resp = client.post("/projects/com.victim.app/flow-graph",
                           json=board("com.intruder.app", 3))
        assert resp.status_code == 409
        assert "com.intruder.app" in resp.json()["detail"]

        # And the original survives intact, which is the whole point.
        saved = client.get("/projects/com.victim.app/flow-graph").json()
        assert len(saved["nodes"]) == 39
        assert saved["package"] == "com.victim.app"

    def test_an_unstamped_board_is_still_accepted(self, client):
        # Boards saved before the stamp existed carry no package. Rejecting those would make
        # every pre-existing board unsaveable, which trades one kind of data loss for another.
        resp = client.post("/projects/com.a.app/flow-graph", json=board(None))
        assert resp.status_code == 200


class TestTelemetryOwnership:
    def frame(self, **overrides) -> dict:
        payload = {
            "session_id": "s1",
            "package_name": "com.google.android.deskclock",
            "activity_name": ".Main",
            "state_hash": "abc123",
            "screenshot_b64": "",
            "available_elements": [],
        }
        payload.update(overrides)
        return payload

    def test_a_screen_the_run_passed_through_does_not_create_a_project(self, client,
                                                                       isolated_projects_dir):
        # A deskclock run that follows a link into Chrome. Chrome is on screen; the run is
        # still a deskclock run, and only deskclock should exist afterwards.
        client.post("/telemetry", json=self.frame(
            target_package="com.google.android.deskclock",
            package_name="com.android.chrome"))
        existing = {p.name for p in isolated_projects_dir.iterdir() if p.is_dir()}
        assert existing == {"com.google.android.deskclock"}

    def test_the_run_target_owns_the_screenshots(self, client, isolated_projects_dir):
        tiny = ("/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
                "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
                "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")
        client.post("/telemetry", json=self.frame(
            target_package="com.google.android.keep",
            package_name="com.android.vending",
            screenshot_b64=tiny))
        assert (isolated_projects_dir / "com.google.android.keep" / "screenshots").is_dir()
        assert not (isolated_projects_dir / "com.android.vending").exists()

    def test_an_older_client_without_a_target_still_works(self, client, isolated_projects_dir):
        # run_agent.py sends target_package now, but a stale checkout or a hand-rolled poster
        # must not 422 — it just falls back to the screen's own package, as before.
        resp = client.post("/telemetry", json=self.frame())
        assert resp.status_code == 200
        assert (isolated_projects_dir / "com.google.android.deskclock").is_dir()

    def test_the_stored_record_names_the_owning_project(self, client):
        # The browser reads this to decide who owns the board, so it has to be on the record
        # rather than inferred from the screen a second time.
        client.post("/telemetry", json=self.frame(
            target_package="com.google.android.deskclock",
            package_name="com.android.chrome"))
        assert server.state_store["abc123"]["target_package"] == "com.google.android.deskclock"


class TestProjectListing:
    def test_projects_report_where_they_live(self, client, tmp_path):
        elsewhere = tmp_path / "chosen folder"
        resp = client.post("/projects", json={"package": "com.a.app", "root": str(elsewhere)})
        assert resp.status_code == 200
        assert resp.json()["custom"] is True

        listed = {p["package"]: p for p in client.get("/projects").json()}
        assert listed["com.a.app"]["root"] == str(elsewhere.resolve())
        assert (elsewhere / "meta.json").is_file()

    def test_moving_an_existing_project_is_refused_rather_than_silently_split(self, client,
                                                                              tmp_path):
        # Accepting this would leave the old folder full of findings nothing points at any
        # more, which reads to the user as "my test history disappeared".
        client.post("/projects", json={"package": "com.a.app", "root": str(tmp_path / "one")})
        resp = client.post("/projects", json={"package": "com.a.app",
                                              "root": str(tmp_path / "two")})
        assert resp.status_code == 409


class TestOutcomes:
    def test_counts_and_popup_contents_come_from_one_traversal(self, client):
        from agent import store

        store.create_subproject("com.a.app", "Auth", "")
        store.create_subproject("com.a.app", "Checkout", "")
        store.add_finding("com.a.app", "auth", {"title": "bad", "kind": "bug"})
        store.add_finding("com.a.app", "auth", {"title": "fine", "kind": "pass"})
        store.add_finding("com.a.app", "checkout", {"title": "also fine", "kind": "pass"})

        data = client.get("/projects/com.a.app/outcomes").json()
        assert data["counts"] == {"bug": 1, "warning": 0, "suggestion": 0, "pass": 2}

        # The passes span two modules, and the popup groups by module — so the count and the
        # grouped contents must agree, which they can only do by being the same walk.
        passes = data["buckets"]["pass"]
        assert passes["total"] == sum(len(m["items"]) for m in passes["modules"])
        assert {m["title"] for m in passes["modules"]} == {"Auth", "Checkout"}

    def test_a_project_with_no_modules_reports_zeroes_rather_than_failing(self, client):
        data = client.get("/projects/com.nothing.here/outcomes").json()
        assert data["counts"] == {"bug": 0, "warning": 0, "suggestion": 0, "pass": 0}
