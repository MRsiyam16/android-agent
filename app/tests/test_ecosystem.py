"""Grouping projects into one product, and the view across them.

Two things here are worth pinning down rather than trusting.

The first is that membership is derived, not stored: `ecosystem.members` must answer from
each project's own `meta.json` and nothing else. The moment a second file records who is in
an ecosystem, the two can disagree, and the whole point of the cross-project view is that its
numbers can be trusted without re-checking them by hand.

The second is that tagging must survive a concurrent write. The dashboard bumps `meta.json`
(`last_saved_at`, `state_count`) whenever a board is saved, and that really does land while
an agent is running — it happened during the very session this module was written in. A tag
implemented as a whole-file overwrite would silently drop those keys, so the test asserts the
merge in both directions.
"""
from __future__ import annotations

import json

import pytest

import ecosystem
import project_paths
from backend import projects as backend_projects


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    """Point every project path at a tmp dir.

    `project_paths` reads `DEFAULT_PROJECTS_DIR` through helpers rather than capturing it at
    import, so patching the one attribute moves the registry with it. `backend.projects`
    binds `PROJECTS_DIR` at import but builds real paths via `project_paths.project_dir`,
    which is the function being patched — so it follows too.
    """
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    return tmp_path


def _make_project(package: str, **meta):
    backend_projects.write_meta(package, **meta)


def test_membership_is_read_from_each_projects_meta(projects_dir):
    _make_project("com.patient.android", platform="android")
    _make_project("clinic.example.com", platform="web")
    _make_project("com.unrelated.app")

    ecosystem.tag("com.patient.android", "metaesthetics", "patient-android")
    ecosystem.tag("clinic.example.com", "metaesthetics", "clinic-web")

    roles = [m["role"] for m in ecosystem.members("metaesthetics")]
    assert roles == ["clinic-web", "patient-android"]  # sorted by role, not discovery order
    assert "com.unrelated.app" not in [m["package"] for m in ecosystem.members("metaesthetics")]


def test_untagged_project_belongs_to_no_ecosystem(projects_dir):
    _make_project("com.unrelated.app")
    assert ecosystem.ecosystems() == {}
    assert ecosystem.role_of("com.unrelated.app") is None


def test_tagging_preserves_keys_written_by_the_server(projects_dir):
    """A tag must merge into meta.json, never replace it."""
    _make_project("clinic.example.com", platform="web", blackcode_project_id=2)
    ecosystem.tag("clinic.example.com", "metaesthetics", "clinic-web")

    meta = backend_projects.read_meta("clinic.example.com")
    assert meta["blackcode_project_id"] == 2
    assert meta["platform"] == "web"
    assert meta["ecosystem"] == "metaesthetics"


def test_a_later_server_write_preserves_the_tag(projects_dir):
    """The other direction: saving a board must not drop the ecosystem keys."""
    _make_project("clinic.example.com", platform="web")
    ecosystem.tag("clinic.example.com", "metaesthetics", "clinic-web")

    backend_projects.write_meta("clinic.example.com", last_saved_at="2026-08-14T04:38:19Z",
                                state_count=80)

    meta = backend_projects.read_meta("clinic.example.com")
    assert meta["ecosystem"] == "metaesthetics"
    assert meta["role"] == "clinic-web"
    assert meta["state_count"] == 80


def test_untag_leaves_the_rest_of_the_meta_alone(projects_dir):
    _make_project("clinic.example.com", platform="web", blackcode_project_id=2)
    ecosystem.tag("clinic.example.com", "metaesthetics", "clinic-web")
    ecosystem.untag("clinic.example.com")

    meta = backend_projects.read_meta("clinic.example.com")
    assert "ecosystem" not in meta and "role" not in meta
    assert meta["blackcode_project_id"] == 2
    assert ecosystem.members("metaesthetics") == []


def test_missing_platform_reads_as_android(projects_dir):
    """Absent `platform` means android everywhere else in the codebase; keep that here."""
    path = projects_dir / "com.old.project"
    path.mkdir(parents=True)
    (path / "meta.json").write_text(json.dumps({"package": "com.old.project",
                                                "ecosystem": "metaesthetics",
                                                "role": "patient-android"}), encoding="utf-8")
    assert ecosystem.members("metaesthetics")[0]["platform"] == "android"


def _with_findings(package: str, slug: str, title: str, findings: list[dict]):
    from agent import store
    store.create_subproject(package, title)
    for finding in findings:
        store.add_finding(package, slug, finding)


def test_findings_are_tagged_with_the_app_they_came_from(projects_dir):
    _make_project("com.patient.android", platform="android")
    ecosystem.tag("com.patient.android", "metaesthetics", "patient-android")
    _with_findings("com.patient.android", "booking", "Booking", [
        {"title": "b", "kind": "bug", "expected": "x", "actual": "y"},
        {"title": "p", "kind": "pass", "expected": "x", "actual": "y"},
    ])

    defects = ecosystem.findings("metaesthetics")
    assert [f["kind"] for f in defects] == ["bug"]           # passes excluded by default
    assert defects[0]["role"] == "patient-android"
    assert defects[0]["package"] == "com.patient.android"
    assert defects[0]["module_slug"] == "booking"

    everything = ecosystem.findings("metaesthetics", kinds=("bug", "pass"))
    assert len(everything) == 2


def test_summary_counts_across_apps(projects_dir):
    for package, role in (("com.patient.android", "patient-android"),
                          ("clinic.example.com", "clinic-web")):
        _make_project(package)
        ecosystem.tag(package, "metaesthetics", role)
        _with_findings(package, "booking", "Booking",
                       [{"title": "b", "kind": "bug", "expected": "x", "actual": "y"}])

    s = ecosystem.summary("metaesthetics")
    assert s["apps"] == 2
    assert s["defects"] == 2
    assert s["counts"]["bug"] == 2


def test_index_is_written_and_names_every_app(projects_dir):
    _make_project("com.patient.android")
    ecosystem.tag("com.patient.android", "metaesthetics", "patient-android")

    path = ecosystem.write_index()
    assert path is not None
    text = (projects_dir / "ECOSYSTEM.md").read_text(encoding="utf-8")
    assert "metaesthetics" in text and "patient-android" in text


def test_index_is_written_even_with_nothing_tagged(projects_dir):
    """An empty index still has to exist — a missing file reads as a broken tool."""
    assert ecosystem.write_index() is not None
    assert "No project is tagged" in (projects_dir / "ECOSYSTEM.md").read_text(encoding="utf-8")
