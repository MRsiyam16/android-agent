"""The endpoints that only mean anything above one project.

The shadowing check is the load-bearing test here. Every project route matches
`/projects/{package:path}/…`, and `path` is greedy — it swallows slashes, which it has to,
because a web project's package *is* its URL. Adding `POST /projects/{package}/ecosystem`
next to those is exactly the shape that silently routes to the wrong handler, and a web
project is the case that would expose it. So both are asserted directly.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import clusters
import ecosystem
import project_paths
from agent import store
from backend.app import create_app
from backend import projects as backend_projects

NAME = "metaesthetics"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    for package, role in (("com.patient.android", "patient-android"),
                          ("https://clinic.example.com/en", "clinic-web")):
        backend_projects.write_meta(package)
        ecosystem.tag(package, NAME, role)
        store.create_subproject(package, "Search")
        store.add_finding(package, "search", {"title": "search misses surnames", "kind": "bug",
                                              "expected": "x", "actual": "y"})
    return TestClient(create_app())


def test_lists_ecosystems_with_headline_numbers(client):
    body = client.get("/ecosystems").json()
    assert len(body) == 1
    assert body[0]["name"] == NAME
    assert body[0]["apps"] == 2
    assert body[0]["defects"] == 2


def test_unknown_ecosystem_is_404(client):
    assert client.get("/ecosystems/nope").status_code == 404


def test_findings_carry_the_app_that_filed_them(client):
    body = client.get(f"/ecosystems/{NAME}/findings").json()
    assert {f["role"] for f in body} == {"patient-android", "clinic-web"}


def test_cluster_round_trip_and_stamping(client):
    members = [{"package": "com.patient.android", "module": "search", "finding": "F001"},
               {"package": "https://clinic.example.com/en", "module": "search",
                "finding": "F001"}]
    created = client.put(f"/ecosystems/{NAME}/clusters/search-prefix-only",
                         json={"title": "Search is prefix-only", "confidence": "confirmed",
                               "members": members}).json()
    assert created["scope"] == "cross-app"
    assert created["size"] == 2

    # PUT applies the stamps itself — a cache nobody refreshes is worse than none.
    stamped = store.list_findings("com.patient.android", "search")[0]
    assert stamped["cluster"] == "search-prefix-only"

    assert client.get(f"/ecosystems/{NAME}/clusters").json()[0]["id"] == "search-prefix-only"


def test_unclustered_is_the_correlation_queue(client):
    assert len(client.get(f"/ecosystems/{NAME}/findings?unclustered=true").json()) == 2
    client.put(f"/ecosystems/{NAME}/clusters/c1",
               json={"title": "t", "members": [{"package": "com.patient.android",
                                                "module": "search", "finding": "F001"}]})
    left = client.get(f"/ecosystems/{NAME}/findings?unclustered=true").json()
    assert [f["package"] for f in left] == ["https://clinic.example.com/en"]


def test_deleting_a_cluster_clears_stamps_and_404s_the_second_time(client):
    client.put(f"/ecosystems/{NAME}/clusters/c1",
               json={"title": "t", "members": [{"package": "com.patient.android",
                                                "module": "search", "finding": "F001"}]})
    assert client.delete(f"/ecosystems/{NAME}/clusters/c1").status_code == 200
    assert not store.list_findings("com.patient.android", "search")[0].get("cluster")
    assert client.delete(f"/ecosystems/{NAME}/clusters/c1").status_code == 404


def test_member_add_and_remove(client):
    client.put(f"/ecosystems/{NAME}/clusters/c1", json={"title": "t", "members": []})
    body = client.post(f"/ecosystems/{NAME}/clusters/c1/members",
                       json={"package": "com.patient.android", "module": "search",
                             "finding": "F001"}).json()
    assert body["size"] == 1

    body = client.request("DELETE", f"/ecosystems/{NAME}/clusters/c1/members",
                          params={"package": "com.patient.android", "module": "search",
                                  "finding": "F001"}).json()
    assert body["size"] == 0
    assert client.post(f"/ecosystems/{NAME}/clusters/nope/members",
                       json={"package": "p", "module": "m", "finding": "F001"}).status_code == 404


def test_tagging_a_web_project_does_not_hit_a_project_route(client):
    """The greedy-path shadowing case: the package is a URL with slashes in it."""
    body = client.post("/projects/https://clinic.example.com/en/ecosystem",
                       json={"ecosystem": "other", "role": "clinic-web"}).json()
    assert body["package"] == "https://clinic.example.com/en"
    assert body["ecosystem"] == "other"
    assert ecosystem.role_of("https://clinic.example.com/en") == "clinic-web"


def test_untagging_via_blank_ecosystem(client):
    client.post("/projects/com.patient.android/ecosystem", json={"ecosystem": "", "role": ""})
    assert ecosystem.role_of("com.patient.android") is None
    assert client.get("/ecosystems").json()[0]["apps"] == 1


def test_flow_graph_route_still_wins_for_a_url_package(client):
    """The other direction: adding our route must not steal an existing project path."""
    r = client.get("/projects/https://clinic.example.com/en/flow-graph")
    assert r.status_code == 404          # no board saved, but it reached the right handler
    assert "cluster" not in r.text


def test_apply_is_exposed_and_idempotent(client):
    client.put(f"/ecosystems/{NAME}/clusters/c1",
               json={"title": "t", "members": [{"package": "com.patient.android",
                                                "module": "search", "finding": "F001"}]})
    assert client.post(f"/ecosystems/{NAME}/apply").json() == {"stamped": 0, "cleared": 0,
                                                               "failed": 0}


def test_modules_index_spans_every_app(client):
    body = client.get(f"/ecosystems/{NAME}/modules").json()
    assert {m["role"] for m in body} == {"patient-android", "clinic-web"}
    assert all("status" in m and "counts" in m for m in body)
