"""Which clinic, which doctor, which patient — recorded once, carried everywhere.

Asked for by a developer reading a ticket, in the plainest possible terms: "please share the
clinic email, dr email, patient email with bugs, it helps to debug". The request is obviously
right and the reason it had to be asked is the interesting part.

The data said it clearly. Of the five findings behind Blackcode issue #441 — a *permissions*
defect, where the account is the entire question — exactly one named the account it happened to,
buried in a sentence, and four named nothing at all. Not because the agents were careless: an
agent that has to remember to mention context in prose does it for the first few findings of a
run and then stops.

So it is not something an agent is asked to include. It is stamped onto every finding by
`store.add_finding` at the moment of filing, and the issue builders read it back. The tests here
are mostly about that: the stamp happens without being asked, it is a snapshot rather than a
live lookup, and it survives into the ticket.
"""
from __future__ import annotations

import pytest

import accounts
import project_paths
from agent import store

PACKAGE = "clinic.example.com"


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    store.create_subproject(PACKAGE, "Procedures")
    return tmp_path


class TestRecording:
    def test_an_account_needs_something_that_identifies_it(self):
        with pytest.raises(ValueError):
            accounts.set_account(PACKAGE, "clinic")

    def test_a_role_is_required(self):
        with pytest.raises(ValueError):
            accounts.set_account(PACKAGE, "  ", email="a@b.com")

    def test_switching_account_replaces_rather_than_appends(self):
        """Two clinics on one project would make every later finding ambiguous about which one
        it belonged to — which is the exact ambiguity this exists to remove."""
        accounts.set_account(PACKAGE, "clinic", email="first@x.com")
        accounts.set_account(PACKAGE, "clinic", email="second@x.com")
        assert [a["email"] for a in accounts.stamp(PACKAGE)] == ["second@x.com"]

    def test_roles_are_normalised_so_one_role_is_one_row(self):
        accounts.set_account(PACKAGE, "Clinic", email="a@x.com")
        accounts.set_account(PACKAGE, "  clinic ", email="b@x.com")
        assert len(accounts.stamp(PACKAGE)) == 1


class TestTheStamp:
    def test_a_finding_carries_the_accounts_without_being_asked(self):
        """The whole design. Nothing in the filing call mentions accounts."""
        accounts.set_account(PACKAGE, "clinic", email="qa.mira@x.com",
                             label="QA Mira Test Clinic")
        finding = store.add_finding(PACKAGE, "procedures", {
            "title": "Cannot create a Procedure", "kind": "bug",
            "expected": "it saves", "actual": "Missing or insufficient permissions"})
        assert finding["accounts"] == [
            {"role": "clinic", "email": "qa.mira@x.com", "label": "QA Mira Test Clinic"}]

    def test_a_finding_filed_before_the_account_changed_keeps_the_old_one(self):
        """A snapshot, not a lookup. A run creates doctors and switches clinics; resolving this
        live would quietly re-attribute last week's defect to today's account."""
        accounts.set_account(PACKAGE, "clinic", email="first@x.com")
        early = store.add_finding(PACKAGE, "procedures", {"title": "a", "kind": "bug"})
        accounts.set_account(PACKAGE, "clinic", email="second@x.com")
        later = store.add_finding(PACKAGE, "procedures", {"title": "b", "kind": "bug"})

        assert early["accounts"][0]["email"] == "first@x.com"
        assert later["accounts"][0]["email"] == "second@x.com"
        assert store.list_findings(PACKAGE, "procedures")[0]["accounts"][0]["email"] \
            == "first@x.com"

    def test_no_accounts_recorded_leaves_the_finding_clean(self):
        """Absent, not an empty list — a finding from a project nobody has set up should not
        carry a field that reads as "we checked and there were none"."""
        finding = store.add_finding(PACKAGE, "procedures", {"title": "a", "kind": "bug"})
        assert "accounts" not in finding

    def test_a_broken_accounts_file_never_stops_a_finding_being_filed(self, monkeypatch):
        """Context is worth having and never worth losing a verdict for."""
        monkeypatch.setattr(accounts, "stamp",
                            lambda package: (_ for _ in ()).throw(RuntimeError("disk gone")))
        finding = store.add_finding(PACKAGE, "procedures", {"title": "a", "kind": "bug"})
        assert finding["id"] == "F001"


class TestTheIssueBody:
    def test_the_table_names_the_app_by_its_role_not_its_package(self):
        """A developer knows the app as `clinic-web`. `clinic.example.com` means nothing."""
        body = accounts.as_markdown(
            {PACKAGE: [{"role": "clinic", "email": "qa@x.com", "label": "QA Mira"}]},
            {PACKAGE: "clinic-web"})
        assert "| clinic-web | clinic | qa@x.com | QA Mira |" in body
        assert PACKAGE not in body

    def test_it_says_why_the_account_matters_rather_than_just_listing_it(self):
        body = accounts.as_markdown({PACKAGE: [{"role": "clinic", "email": "a@x.com",
                                                "label": ""}]})
        assert "per account" in body

    def test_nothing_recorded_produces_no_section_at_all(self):
        assert accounts.as_markdown({}) == ""

    def test_a_cluster_issue_carries_every_app_s_accounts(self, tmp_path, monkeypatch):
        """The point for a cross-app defect: it reproduced under a clinic account *and* a
        patient account, and which pair it was is the first thing a permissions bug needs."""
        import clusters
        import ecosystem
        from agent import ecosystem_tools
        from backend import projects as backend_projects

        patient = "com.patient.android"
        for package, role, platform in ((PACKAGE, "clinic-web", "web"),
                                        (patient, "patient-android", "android")):
            backend_projects.write_meta(package, platform=platform)
            ecosystem.tag(package, "eco", role)
            store.create_subproject(package, "Booking")
        accounts.set_account(PACKAGE, "clinic", email="clinic@x.com", label="QA Mira")
        accounts.set_account(patient, "patient", email="patient@x.com")
        for package in (PACKAGE, patient):
            store.add_finding(package, "booking", {"title": "no permissions", "kind": "bug"})

        clusters.save("eco", "perms", title="One permissions fault", members=[
            {"package": PACKAGE, "module": "booking", "finding": "F001"},
            {"package": patient, "module": "booking", "finding": "F001"}])
        cluster = clusters.get("eco", "perms")
        full = [(m, store.list_findings(m["package"], m["module"])[0])
                for m in cluster["members"]]

        body = ecosystem_tools._cluster_description("eco", cluster, full)
        assert "Accounts under test" in body
        assert "clinic@x.com" in body and "patient@x.com" in body
        assert "clinic-web" in body and "patient-android" in body
