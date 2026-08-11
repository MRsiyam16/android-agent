"""Which platform a project targets, and where that answer has to come from.

Android and iOS never needed this recorded anywhere: `device.platform_of()` infers it from the
*shape* of whatever serial is attached, and a UDID's shape is unambiguous. A website's "serial"
is a URL, which has no comparable shape — `https://example.com` looks exactly like every other
string `platform_of()` has ever seen and defaulted to Android. So `platform` is the one field
this feature actually needed to add to `meta.json`, and these tests exist to keep two specific
ways that field could quietly go wrong from going wrong: a brand-new project defaulting to the
wrong platform, and an idempotent re-open clobbering an existing project's platform back to it.
"""
from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import project_paths
import server
from backend import projects


@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", root)
    monkeypatch.setattr(server, "PROJECTS_DIR", root)
    return root


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


class TestWriteMetaDefault:
    def test_a_brand_new_project_defaults_to_android(self):
        """No caller has ever had to say "android" explicitly — this is what has to keep
        every existing Android/iOS call site working unchanged."""
        meta = projects.write_meta("com.example.app")
        assert meta["platform"] == "android"

    def test_an_explicit_platform_is_kept(self):
        meta = projects.write_meta("https://example.com", platform="web")
        assert meta["platform"] == "web"

    def test_a_pre_existing_project_with_no_platform_key_reads_back_as_missing(self):
        """Old projects on disk are not migrated — `meta.json` written before this field
        existed simply lacks the key, and every reader is expected to treat that as
        "android" rather than requiring a rewrite of every project folder on disk."""
        projects.write_meta("com.legacy.app")
        path = projects.meta_path("com.legacy.app")
        import json
        raw = json.loads(path.read_text(encoding="utf-8"))
        del raw["platform"]
        path.write_text(json.dumps(raw), encoding="utf-8")

        meta = projects.read_meta("com.legacy.app")
        assert "platform" not in meta
        assert (meta.get("platform") or "android") == "android"


class TestCreateProjectRoute:
    def test_creating_a_web_project_persists_its_platform(self, client):
        resp = client.post("/projects", json={"package": "https://example.com",
                                              "platform": "web"})
        assert resp.status_code == 200
        assert resp.json()["platform"] == "web"

    def test_omitting_platform_defaults_to_android(self, client):
        resp = client.post("/projects", json={"package": "com.example.app"})
        assert resp.status_code == 200
        assert resp.json()["platform"] == "android"

    def test_reopening_a_web_project_without_naming_a_platform_does_not_revert_it(self, client):
        """The exact bug this guards against: the dashboard's idempotent "make sure this
        project exists" call posts only `{package}` — no `platform` — every time a project
        that already exists is reopened. Defaulting the payload's missing platform to
        "android" here would silently turn every reopened web project back into an Android
        one on its very next visit.
        """
        client.post("/projects", json={"package": "https://example.com", "platform": "web"})
        resp = client.post("/projects", json={"package": "https://example.com"})
        assert resp.status_code == 200
        assert resp.json()["platform"] == "web"

    def test_a_url_with_slashes_round_trips_through_the_package_path_param(self, client):
        """`package` is used as a path parameter across `/agent/{package:path}/...` and
        `/projects/{package:path}/...` specifically so a URL's slashes survive routing —
        the default single-segment `{package}` matcher 404s the instant `package` contains
        one. This is the create/list round trip; the routing itself is exercised end to end
        by hitting a couple of the affected routes directly.
        """
        url = "https://the-internet.herokuapp.com/"
        create = client.post("/projects", json={"package": url, "platform": "web"})
        assert create.status_code == 200
        assert create.json()["package"] == url

        listed = client.get("/projects")
        assert any(p["package"] == url and p["platform"] == "web" for p in listed.json())

        # Percent-encoded, matching what the frontend actually sends (every call site there
        # goes through `encodeURIComponent(agent.package)`) — this exercises the same
        # decode-then-match-a-slash-containing-segment path {package:path} exists for.
        subprojects = client.get(f"/agent/{quote(url, safe='')}/subprojects")
        assert subprojects.status_code == 200
