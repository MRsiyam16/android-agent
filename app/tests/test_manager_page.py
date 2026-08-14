"""The manager dashboard: its page, its one board request, and the event stamping it needs.

Three separate things are asserted here because they fail in three different ways, and only
one of them is visible on screen:

  * the page and the board payload — a missing route or key is an empty dashboard;
  * `app_index` excluding the manager module — a phantom "never run" gap in every app, which
    looks like a finding rather than a bug in the counting;
  * `package` on every emitted event — the manager's module slug is `main`, and so is every
    project's, so without it another project's manager streams into this one's transcript.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import ecosystem
import project_paths
from agent import store
from backend.app import create_app
from backend import projects as backend_projects

NAME = "metaesthetics"
ANDROID = "com.patient.android"
WEB = "https://clinic.example.com/en"


@pytest.fixture
def eco(tmp_path, monkeypatch):
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    for package, role in ((ANDROID, "patient-android"), (WEB, "clinic-web")):
        backend_projects.write_meta(package)
        ecosystem.tag(package, NAME, role)
        store.create_subproject(package, "Search")
        store.add_finding(package, "search", {"title": "search misses surnames", "kind": "bug",
                                              "expected": "x", "actual": "y"})
    ecosystem.create_supervisor(NAME)
    return NAME


@pytest.fixture
def client(eco):
    return TestClient(create_app())


# -- the page ---------------------------------------------------------------------------
def test_manager_page_is_served(client):
    resp = client.get("/manager")
    assert resp.status_code == 200
    assert "manager/main.js" in resp.text


def test_the_two_dashboards_are_different_documents(client):
    """The whole point of the split. The cockpit loads vis-network and the graph styles; the
    manager loads neither, because it has no graph and never will."""
    cockpit = client.get("/").text
    manager = client.get("/manager").text
    # The library URL, not the bare name: both pages *mention* vis-network in a comment.
    assert "unpkg.com/vis-network" in cockpit and "unpkg.com/vis-network" not in manager
    assert "graph.css" in cockpit and "graph.css" not in manager
    # ...and the manager does not carry the cockpit's boot graph.
    assert "js/main.js" in cockpit and "js/main.js" not in manager


# -- the board --------------------------------------------------------------------------
def test_board_answers_the_whole_landing_view_in_one_request(client):
    body = client.get(f"/ecosystems/{NAME}/board").json()
    assert {"name", "supervisor", "apps", "totals", "clusters", "cross_app",
            "unclustered"} <= set(body)
    assert len(body["apps"]) == 2
    assert body["totals"]["defects"] == 2
    assert body["unclustered"] == 2       # nothing has been clustered yet
    assert body["cross_app"] == []


def test_board_totals_agree_with_the_rows_under_them(client):
    """One read of disk, so a total cannot disagree with the cards it sits above."""
    body = client.get(f"/ecosystems/{NAME}/board").json()
    assert sum(app["defects"] for app in body["apps"]) == body["totals"]["defects"]
    assert sum(app["modules"] for app in body["apps"]) == body["totals"]["modules"]


def test_board_is_404_for_an_unknown_ecosystem(client):
    assert client.get("/ecosystems/nope/board").status_code == 404


def test_board_names_the_manager_project(client):
    """`members` omits the supervisor, so without this the page cannot find who to talk to."""
    assert client.get(f"/ecosystems/{NAME}/board").json()["supervisor"] == NAME
    assert client.get(f"/ecosystems/{NAME}").json()["supervisor"] == NAME


def test_the_supervisor_is_not_one_of_the_apps(client):
    packages = {app["package"] for app in client.get(f"/ecosystems/{NAME}/board").json()["apps"]}
    assert NAME not in packages


def test_cross_app_clusters_reach_the_board(client):
    client.put(f"/ecosystems/{NAME}/clusters/search-prefix-only",
               json={"title": "Search is prefix-only", "confidence": "confirmed",
                     "members": [{"package": ANDROID, "module": "search", "finding": "F001"},
                                 {"package": WEB, "module": "search", "finding": "F001"}]})
    body = client.get(f"/ecosystems/{NAME}/board").json()
    assert [c["id"] for c in body["cross_app"]] == ["search-prefix-only"]
    assert body["unclustered"] == 0
    assert body["clusters"]["distinct"] == 1


# -- app_index --------------------------------------------------------------------------
def test_app_index_carries_what_a_card_shows(eco):
    rows = {app["role"]: app for app in ecosystem.app_index(NAME)}
    assert set(rows) == {"patient-android", "clinic-web"}
    assert rows["patient-android"]["defects"] == 1
    assert rows["patient-android"]["counts"]["bug"] == 1


def test_the_manager_module_is_not_counted_as_an_untested_gap(eco):
    """Every project has a `main`, it is never marked tested and it never runs. Counting it
    would put one phantom gap in every app on the board — an artefact that reads as a finding.
    """
    store.create_subproject(ANDROID, "Main")
    app = next(a for a in ecosystem.app_index(NAME) if a["package"] == ANDROID)
    assert "main" not in app["untested"]
    assert "main" not in app["never_run"]
    # ...but a real module that nobody has opened is still reported.
    assert "search" in app["never_run"]


def test_summary_still_agrees_with_app_index_after_the_refactor(eco):
    apps = ecosystem.app_index(NAME)
    summary = ecosystem.summary(NAME)
    assert summary["apps"] == len(apps)
    assert summary["defects"] == sum(a["defects"] for a in apps)
    assert summary["modules"] == sum(a["modules"] for a in apps)
    # A kind nobody filed is absent, not zero — those read differently in a tally.
    assert "suggestion" not in summary["counts"]


# -- event stamping ---------------------------------------------------------------------
def test_every_event_carries_the_project_that_emitted_it(eco):
    """Filtering on slug alone is not enough: `main` is every project's manager module, and
    the ecosystem manager's own slug is `main` too."""
    from agent.runtime import AgentSession

    seen = []

    async def collect(event):
        seen.append(event)

    async def drive():
        session = AgentSession(ANDROID, "main", collect)
        await session.emit({"type": "agent_text", "text": "hello"})
        await session.emit({"type": "agent_busy", "busy": False})

    asyncio.run(drive())
    assert [e["package"] for e in seen] == [ANDROID, ANDROID]
    assert [e["slug"] for e in seen] == ["main", "main"]


def test_an_event_may_still_name_its_own_project(eco):
    """The stamp is a default, not an override — `agent_ready` already sets its own."""
    from agent.runtime import AgentSession

    seen = []

    async def collect(event):
        seen.append(event)

    async def drive():
        session = AgentSession(ANDROID, "search", collect)
        await session.emit({"type": "agent_ready", "package": WEB})

    asyncio.run(drive())
    assert seen[0]["package"] == WEB


# -- the socket -------------------------------------------------------------------------
def test_the_manager_can_join_the_socket_without_the_screenshot_backlog(client):
    """The backlog is every screen of the last run with its JPEG attached, and the manager has
    no graph to draw with it."""
    with client.websocket_connect("/ws?replay=false") as ws:
        # Nothing is pushed on connect, so a receive would block; assert by what is absent.
        ws.send_text("ping")
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "history"
