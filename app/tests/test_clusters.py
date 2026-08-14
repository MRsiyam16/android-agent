"""Findings that turned out to be one defect.

The load-bearing claim this module makes is a number — "131 filed defects are 98 distinct
ones". Everything here exists to stop that number from being quietly wrong.

Three ways it could be: a cluster whose member no longer exists on disk shrinking silently
(so the saving looks bigger than it is); `scope` being stored rather than derived, and so
disagreeing with the membership it describes; and the `cluster` stamp on a finding drifting
away from the file that owns it after a split or a delete.
"""
from __future__ import annotations

import pytest

import clusters
import ecosystem
import project_paths
from agent import store
from backend import projects as backend_projects

NAME = "metaesthetics"


@pytest.fixture
def eco(tmp_path, monkeypatch):
    """Two tagged apps, each with one module, in an isolated projects dir."""
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    for package, role in (("com.patient.android", "patient-android"),
                          ("clinic.example.com", "clinic-web")):
        backend_projects.write_meta(package)
        ecosystem.tag(package, NAME, role)
        store.create_subproject(package, "Search")
    return tmp_path


def _file(package: str, title: str, kind: str = "bug") -> str:
    return store.add_finding(package, "search",
                             {"title": title, "kind": kind,
                              "expected": "x", "actual": "y"})["id"]


def _two_app_cluster(cluster_id: str = "search-prefix-only", **kw):
    a = _file("com.patient.android", "doctor search misses surnames")
    b = _file("clinic.example.com", "doctor search misses surnames")
    clusters.save(NAME, cluster_id, title="Search is prefix-only",
                  members=[{"package": "com.patient.android", "module": "search", "finding": a},
                           {"package": "clinic.example.com", "module": "search", "finding": b}],
                  **kw)
    return a, b


def test_scope_is_derived_from_the_members_apps(eco):
    _two_app_cluster()
    cluster = clusters.get(NAME, "search-prefix-only")
    assert cluster["scope"] == "cross-app"
    assert cluster["roles"] == ["clinic-web", "patient-android"]

    clusters.remove_member(NAME, "search-prefix-only", "clinic.example.com", "search", "F001")
    assert clusters.get(NAME, "search-prefix-only")["scope"] == "single-app"


def test_summary_collapses_duplicates(eco):
    _two_app_cluster()
    _file("com.patient.android", "something else entirely")

    s = clusters.summary(NAME)
    assert s["filed"] == 3
    assert s["absorbed"] == 1        # a 2-member cluster absorbs one duplicate
    assert s["distinct"] == 2
    assert s["cross_app"] == 1


def test_a_member_whose_finding_vanished_becomes_an_orphan(eco):
    """A cluster must not shrink silently — that would inflate the saving it claims."""
    a, _ = _two_app_cluster()
    clusters.add_member(NAME, "search-prefix-only", "com.patient.android", "search", "F404")

    cluster = clusters.get(NAME, "search-prefix-only")
    assert cluster["size"] == 2
    assert cluster["orphans"] == [{"package": "com.patient.android", "module": "search",
                                   "finding": "F404"}]
    assert clusters.summary(NAME)["orphans"] == 1


def test_apply_stamps_every_member(eco):
    a, b = _two_app_cluster()
    assert clusters.apply(NAME)["stamped"] == 2

    stamped = store.list_findings("com.patient.android", "search")[0]
    assert stamped["cluster"] == "search-prefix-only"
    assert clusters.cluster_of(NAME, "clinic.example.com", "search", b) == "search-prefix-only"


def test_apply_is_idempotent(eco):
    _two_app_cluster()
    clusters.apply(NAME)
    assert clusters.apply(NAME) == {"stamped": 0, "cleared": 0, "failed": 0}


def test_removing_a_member_clears_its_stamp(eco):
    a, _ = _two_app_cluster()
    clusters.apply(NAME)
    clusters.remove_member(NAME, "search-prefix-only", "com.patient.android", "search", a)

    finding = store.list_findings("com.patient.android", "search")[0]
    assert not finding.get("cluster")
    assert clusters.cluster_of(NAME, "com.patient.android", "search", a) is None


def test_deleting_a_cluster_clears_every_stamp(eco):
    _two_app_cluster()
    clusters.apply(NAME)
    assert clusters.delete(NAME, "search-prefix-only") is True

    for package in ("com.patient.android", "clinic.example.com"):
        assert not store.list_findings(package, "search")[0].get("cluster")
    assert clusters.list_clusters(NAME) == []
    assert clusters.delete(NAME, "search-prefix-only") is False


def test_apply_clears_a_stamp_no_cluster_claims_any_more(eco):
    """The file is authoritative: a stale stamp is overwritten, not preserved."""
    a, _ = _two_app_cluster()
    clusters.apply(NAME)
    store.set_finding_tracking("com.patient.android", "search", a, cluster="some-old-id")

    assert clusters.apply(NAME)["stamped"] == 1
    assert store.list_findings("com.patient.android", "search")[0]["cluster"] == \
        "search-prefix-only"


def test_unknown_confidence_falls_back_to_tentative(eco):
    _two_app_cluster(confidence="definitely-for-sure")
    assert clusters.get(NAME, "search-prefix-only")["confidence"] == "tentative"


def test_confidence_is_kept_when_valid(eco):
    _two_app_cluster(confidence="confirmed")
    assert clusters.get(NAME, "search-prefix-only")["confidence"] == "confirmed"


def test_a_cluster_is_resolved_only_when_every_report_is(eco):
    """One client fixed and another not is the partial-backend-fix case worth seeing."""
    a, b = _two_app_cluster()
    assert clusters.get(NAME, "search-prefix-only")["resolved"] is False

    store.set_finding_tracking("com.patient.android", "search", a, resolved=True)
    assert clusters.get(NAME, "search-prefix-only")["resolved"] is False

    store.set_finding_tracking("clinic.example.com", "search", b, resolved=True)
    assert clusters.get(NAME, "search-prefix-only")["resolved"] is True


def test_add_member_is_idempotent(eco):
    _two_app_cluster()
    clusters.add_member(NAME, "search-prefix-only", "com.patient.android", "search", "F001")
    assert clusters.get(NAME, "search-prefix-only")["size"] == 2


def test_writing_to_an_unknown_cluster_returns_none(eco):
    assert clusters.add_member(NAME, "nope", "com.patient.android", "search", "F001") is None
    assert clusters.remove_member(NAME, "nope", "com.patient.android", "search", "F001") is None
    assert clusters.get(NAME, "nope") is None


def test_cross_app_clusters_sort_first(eco):
    a, b = _two_app_cluster()
    c = _file("com.patient.android", "android back exits app")
    d = _file("com.patient.android", "android back exits app again")
    clusters.save(NAME, "android-back", title="Back exits the app",
                  members=[{"package": "com.patient.android", "module": "search", "finding": c},
                           {"package": "com.patient.android", "module": "search", "finding": d}])

    ids = [x["id"] for x in clusters.list_clusters(NAME)]
    assert ids == ["search-prefix-only", "android-back"]   # cross-app first, despite equal size


def test_a_corrupt_store_reads_as_no_clusters(eco):
    """Losing the grouping must not take the findings with it."""
    _two_app_cluster()
    (eco / "clusters.json").write_text("{not json", encoding="utf-8")
    assert clusters.list_clusters(NAME) == []
    assert clusters.summary(NAME)["distinct"] == clusters.summary(NAME)["filed"]
