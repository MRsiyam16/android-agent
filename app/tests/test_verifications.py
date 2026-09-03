"""The verdict this harness owes another system, and the four ways it must refuse to give one.

Read together with test_retests.py. That file is about work a fix prompted *inside* this
system, which waits for a person. This is the outward-facing half: Bugmaster made a fix it
cannot test, asked for one case to be re-run on a device here, and will gate a merge on the
answer. Every assertion below is about the answer being honest rather than convenient:

* a `pass` may not carry a bug finding — in a verification run a bug means "the fix did not
  work", so a pass over the top of one merges a broken fix;
* a job is answered once, because the pipeline reads the answer and acts on it;
* the findings are copied in at write time, so a verdict cannot change meaning later;
* 404 means "not answered yet", never "no", and the worker's timeout turns silence into
  `blocked` — nothing here turns "could not check" into "checked".
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import ecosystem
import project_paths
import verifications
from agent import ecosystem_tools, prompts, store
from agent.device_tools import DeviceSession
from backend import projects as backend_projects
from backend.app import create_app

NAME = "metaesthetics-verify"
ANDROID = "com.metaestetics.mobile_clientapp"
JOB = "dj_01JQA0000000000000000000"


def call(name: str, args: dict) -> str:
    import mcp.types as mcp_types

    instance = ecosystem_tools.build_ecosystem_server(DeviceSession(NAME, "main"),
                                                      NAME)["instance"]
    handler = instance.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=args))
    return asyncio.run(handler(request)).root.content[0].text


@pytest.fixture
def eco(tmp_path, monkeypatch):
    """A verifier notebook with one app and one module that has already run."""
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    backend_projects.write_meta(ANDROID, platform="android", device_serial="emulator-5554")
    ecosystem.tag(ANDROID, NAME, "patient-android")
    store.create_subproject(ANDROID, "bm-612")
    ecosystem.create_supervisor(NAME)
    store.create_subproject(NAME, "Main")
    return tmp_path


def file_finding(kind: str = "pass", **extra) -> dict:
    return store.add_finding(ANDROID, "bm-612", {
        "kind": kind, "title": "booking a slot", "expected": "the slot is booked",
        "actual": "the slot is booked" if kind == "pass" else "a 500 is shown",
        "steps": ["open booking", "pick 10:00", "confirm"],
        "evidence": r"D:\shots\003-booking.jpg", **extra})


# -- the store --------------------------------------------------------------------------------
class TestReporting:
    def test_a_verdict_carries_the_findings_it_stands_on(self, eco):
        """Resolved at write time. The worker on the other end must never need this harness's
        disk layout, and a verdict must not change meaning because a module was edited later."""
        file_finding("pass")
        record = verifications.report(JOB, verdict="pass", finding_ids=["F001"],
                                      note="re-ran TC-BOOK-004 on emulator-5554", package=ANDROID,
                                      module="bm-612")
        assert record["verdict"] == "pass"
        assert [f["id"] for f in record["findings"]] == ["F001"]
        assert record["findings"][0]["evidence"].endswith("003-booking.jpg")
        assert record["findings"][0]["steps"] == ["open booking", "pick 10:00", "confirm"]

    def test_editing_the_module_afterwards_does_not_change_the_answer(self, eco):
        """The point of copying rather than referencing: this file is a statement of what was
        true when the job was answered."""
        file_finding("pass")
        verifications.report(JOB, verdict="pass", finding_ids=["F001"], note="ok",
                             package=ANDROID, module="bm-612")
        path = store._findings_path(ANDROID, "bm-612")
        path.write_text(json.dumps([{"id": "F001", "kind": "bug", "title": "rewritten"}]),
                        encoding="utf-8")
        assert verifications.get(JOB)["findings"][0]["title"] == "booking a slot"

    def test_an_unanswered_job_is_none_not_an_empty_answer(self, eco):
        """`None` is what the worker polls against; anything else reads as a verdict."""
        assert verifications.get(JOB) is None

    def test_a_job_is_answered_once(self, eco):
        file_finding("pass")
        verifications.report(JOB, verdict="pass", finding_ids=[], note="ok",
                             package=ANDROID, module="bm-612")
        with pytest.raises(ValueError) as exc:
            verifications.report(JOB, verdict="fail", finding_ids=[], note="no",
                                 package=ANDROID, module="bm-612")
        assert "already reported" in str(exc.value)
        assert verifications.get(JOB)["verdict"] == "pass"

    def test_an_unknown_verdict_is_refused(self, eco):
        with pytest.raises(ValueError):
            verifications.report(JOB, verdict="probably", finding_ids=[], note="",
                                 package=ANDROID, module="bm-612")

    def test_a_finding_that_does_not_exist_is_refused_before_anything_is_written(self, eco):
        """A verification whose evidence list quietly dropped the finding the verdict was about
        is worse than no verification: the note still claims something and nothing backs it."""
        with pytest.raises(ValueError) as exc:
            verifications.report(JOB, verdict="fail", finding_ids=["F009"], note="",
                                 package=ANDROID, module="bm-612")
        assert "F009" in str(exc.value)
        assert verifications.get(JOB) is None

    def test_recent_verifications_come_back_newest_first(self, eco):
        for index in range(3):
            verifications.report(f"dj_{index}", verdict="pass", finding_ids=[], note="",
                                 package=ANDROID, module="bm-612")
        rows = verifications.list_recent(2)
        assert len(rows) == 2
        assert {r["job_id"] for r in rows} <= {"dj_0", "dj_1", "dj_2"}

    def test_it_lives_in_this_instances_notebook(self, eco):
        verifications.report(JOB, verdict="blocked", finding_ids=[], note="no device",
                             package=ANDROID, module="bm-612")
        assert (eco / "verifications.json").is_file()


# -- the tool ---------------------------------------------------------------------------------
class TestReportVerificationTool:
    def test_a_pass_is_refused_when_a_listed_finding_is_a_bug(self, eco):
        """The one rule that stops a broken fix merging. A bug in a verification run means the
        fix did not work — a pass carrying one is the answer contradicting its own evidence."""
        file_finding("bug")
        out = call("report_verification", {
            "job_id": JOB, "verdict": "pass", "finding_ids": ["F001"],
            "note": "looks fine to me", "module": "bm-612", "app": "patient-android"})
        assert "Refused" in out
        assert "F001" in out and "fail" in out
        assert verifications.get(JOB) is None

    def test_a_fail_with_the_same_bug_is_recorded(self, eco):
        file_finding("bug")
        out = call("report_verification", {
            "job_id": JOB, "verdict": "fail", "finding_ids": ["F001"],
            "note": "still 500s on confirm", "module": "bm-612", "app": "patient-android"})
        assert "Reported fail" in out
        assert verifications.get(JOB)["verdict"] == "fail"

    def test_it_says_not_to_file_a_blackcode_issue(self, eco):
        """Stated in the tool's own answer as well as the prompt: the build under test is
        deployed nowhere, so a ticket about it describes software no user can reach."""
        file_finding("pass")
        out = call("report_verification", {
            "job_id": JOB, "verdict": "pass", "finding_ids": ["F001"], "note": "fixed",
            "module": "bm-612", "app": "patient-android"})
        assert "Blackcode" in out and "deployed nowhere" in out

    def test_blocked_needs_no_findings(self, eco):
        """"Nobody checked" is its own answer, and it has no evidence by definition."""
        out = call("report_verification", {
            "job_id": JOB, "verdict": "blocked", "finding_ids": [],
            "note": "the emulator never booted", "module": "bm-612", "app": "patient-android"})
        assert "Reported blocked" in out
        assert verifications.get(JOB)["verdict"] == "blocked"

    def test_reporting_the_same_verdict_twice_is_not_an_error(self, eco):
        """The manager can lose a turn and the worker retries. A repeat that agrees is a
        no-op, not a failure to hand back."""
        file_finding("pass")
        args = {"job_id": JOB, "verdict": "pass", "finding_ids": ["F001"], "note": "fixed",
                "module": "bm-612", "app": "patient-android"}
        call("report_verification", args)
        out = call("report_verification", args)
        assert "already reported" in out and "not recorded twice" in out

    def test_a_different_second_verdict_is_refused(self, eco):
        file_finding("pass")
        call("report_verification", {"job_id": JOB, "verdict": "pass", "finding_ids": [],
                                     "note": "fixed", "module": "bm-612",
                                     "app": "patient-android"})
        out = call("report_verification", {"job_id": JOB, "verdict": "fail", "finding_ids": [],
                                           "note": "actually no", "module": "bm-612",
                                           "app": "patient-android"})
        assert "answered once" in out
        assert verifications.get(JOB)["verdict"] == "pass"

    def test_an_unknown_finding_names_the_ones_that_exist(self, eco):
        file_finding("pass")
        out = call("report_verification", {
            "job_id": JOB, "verdict": "fail", "finding_ids": ["F007"], "note": "",
            "module": "bm-612", "app": "patient-android"})
        assert "F007" in out and "F001" in out
        assert verifications.get(JOB) is None

    def test_an_unknown_app_is_refused(self, eco):
        out = call("report_verification", {
            "job_id": JOB, "verdict": "pass", "finding_ids": [], "note": "",
            "module": "bm-612", "app": "nope"})
        assert "patient-android" in out

    def test_a_bad_verdict_names_the_three(self, eco):
        out = call("report_verification", {
            "job_id": JOB, "verdict": "probably-fine", "finding_ids": [], "note": "",
            "module": "bm-612", "app": "patient-android"})
        assert "pass" in out and "fail" in out and "blocked" in out

    def test_listing_says_when_nothing_has_been_answered(self, eco):
        assert "No verification job" in call("list_verifications", {})

    def test_listing_shows_the_verdict_and_the_job(self, eco):
        call("report_verification", {"job_id": JOB, "verdict": "fail", "finding_ids": [],
                                     "note": "still broken", "module": "bm-612",
                                     "app": "patient-android"})
        out = call("list_verifications", {})
        assert JOB in out and "fail" in out


# -- the wire ---------------------------------------------------------------------------------
class TestRoutes:
    def test_an_unreported_job_is_404_not_an_empty_answer(self, eco):
        """The worker distinguishes "no answer" from every possible answer purely by the status
        code, and its own timeout turns a long silence into `blocked`."""
        assert TestClient(create_app()).get(f"/verifications/{JOB}").status_code == 404

    def test_the_body_is_the_shape_bridge_md_promises(self, eco):
        file_finding("pass")
        verifications.report(JOB, verdict="pass", finding_ids=["F001"],
                             note="re-ran TC-BOOK-004", package=ANDROID, module="bm-612",
                             campaign_id="cmp_1")
        body = TestClient(create_app()).get(f"/verifications/{JOB}").json()
        assert set(body) >= {"job_id", "verdict", "note", "reported_at", "package", "module",
                             "campaign_id", "findings"}
        assert set(body["findings"][0]) == {"id", "kind", "title", "expected", "actual",
                                            "steps", "evidence"}

    def test_the_listing_is_capped_and_newest_first(self, eco):
        for index in range(3):
            verifications.report(f"dj_{index}", verdict="pass", finding_ids=[], note="",
                                 package=ANDROID, module="bm-612")
        rows = TestClient(create_app()).get("/verifications?limit=2").json()
        assert len(rows) == 2

    def test_there_is_no_way_to_post_a_verdict(self, eco):
        """A verdict is written by an agent that watched a run. An endpoint that could record
        one would let anything on loopback answer a job nobody ran."""
        client = TestClient(create_app())
        assert client.post(f"/verifications/{JOB}", json={"verdict": "pass"}).status_code == 405


# -- the prompt ---------------------------------------------------------------------------------
def test_the_prompt_tells_the_manager_what_a_bugmaster_message_is(eco):
    """A tool the prompt does not name is one the agent will not reach for — and this one is
    reached for on a message from a machine, with nobody watching to correct it."""
    prompt = prompts.build_system_prompt(NAME, "main", "Main", "")
    assert "Bugmaster verification job" in prompt
    assert "report_verification" in prompt and "list_verifications" in prompt
    assert "run_journey" in prompt
    # The rule the whole separation exists for.
    assert "Never file a Blackcode issue for one of these runs." in prompt
    assert "deployed nowhere" in prompt
