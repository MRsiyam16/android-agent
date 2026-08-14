"""Starting runs from the manager, and the one class of run it must not start alone.

The split is the whole design. Work the manager *planned* — a coverage gap, a seam nobody has
checked — it runs. Work a *fix* prompted, it queues, because three things about a re-test are
routinely wrong in ways it cannot see from here: the fix may not be deployed to the
environment under test, it may be in staging but not in the iPad build, and "closed" in a
tracker covers fixed, duplicate and will-not-do.

Read together with test_device_locks.py: the lock is what makes `run_module` safe rather than
reckless, since starting a run and driving one are different powers and this tier has only the
first.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import clusters
import device_locks
import ecosystem
import project_paths
import retests
from agent import ecosystem_tools, prompts, store
from agent.device_tools import DeviceSession
from backend import agent_bridge
from backend import projects as backend_projects
from backend.app import create_app

NAME = "metaesthetics"
ANDROID = "com.patient.android"
WEB = "clinic.example.com"


@pytest.fixture(autouse=True)
def clean_locks():
    device_locks.reset()
    yield
    device_locks.reset()


@pytest.fixture
def eco(tmp_path, monkeypatch):
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(agent_bridge, "attached", lambda: [])
    for package, role, platform in ((ANDROID, "patient-android", "android"),
                                    (WEB, "clinic-web", "web")):
        backend_projects.write_meta(package, platform=platform)
        ecosystem.tag(package, NAME, role)
        store.create_subproject(package, "Search")
        store.add_finding(package, "search",
                          {"title": "search misses surnames", "kind": "bug",
                           "expected": "surname matches", "actual": "no results"})
    ecosystem.create_supervisor(NAME)
    store.create_subproject(NAME, "Main")
    return tmp_path


def call(name: str, args: dict) -> str:
    import mcp.types as mcp_types

    instance = ecosystem_tools.build_ecosystem_server(DeviceSession(NAME, "main"),
                                                      NAME)["instance"]
    handler = instance.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=args))
    return asyncio.run(handler(request)).root.content[0].text


# -- starting a run -------------------------------------------------------------------------
class TestRunModule:
    def test_it_starts_the_module_through_the_same_path_the_send_button_uses(self, eco,
                                                                             monkeypatch):
        """A second way to start a run is a second set of rules about which device it lands
        on. There is one `start_run`, and the tool and the route both call it."""
        started = []
        monkeypatch.setattr(agent_bridge, "start_run",
                            lambda p, s, t, **kw: started.append((p, s, t))
                            or {"package": p, "slug": s, "target": "x", "started": True})

        out = call("run_module", {"app": "patient-android", "module": "search",
                                  "instruction": "check surname search again"})
        assert "Started patient-android/search" in out
        assert started == [(ANDROID, "search", "check surname search again")]

    def test_an_unknown_module_is_refused_before_anything_is_started(self, eco):
        out = call("run_module", {"app": "patient-android", "module": "nope",
                                  "instruction": "go"})
        assert "no module" in out.lower()

    def test_an_unknown_app_names_the_ones_that_exist(self, eco):
        out = call("run_module", {"app": "nope", "module": "search", "instruction": "go"})
        assert "patient-android" in out and "clinic-web" in out

    def test_a_run_with_no_instruction_is_refused(self, eco):
        """A module started with nothing to establish is a run whose result nobody can read."""
        assert "needs an instruction" in call(
            "run_module", {"app": "patient-android", "module": "search", "instruction": "  "})

    def test_a_busy_target_is_refused_and_says_who_has_it(self, eco):
        """The lock is what makes this power safe. Refused up front, not inside the background
        task — a refusal that arrives after the call returned "ok" reaches a tool as nothing."""
        device_locks.acquire(
            device_locks.key_for("android", None, ANDROID), ANDROID, "checkout")
        out = call("run_module", {"app": "patient-android", "module": "search",
                                  "instruction": "go"})
        assert "already driving" in out
        assert "checkout" in out

    def test_two_different_targets_both_start(self, eco, monkeypatch):
        """The point of all of it: an app on a device and a website do not queue behind each
        other."""
        started = []
        monkeypatch.setattr(agent_bridge, "start_run",
                            lambda p, s, t, **kw: started.append(p)
                            or {"package": p, "slug": s, "target": p, "started": True})
        call("run_module", {"app": "patient-android", "module": "search", "instruction": "a"})
        call("run_module", {"app": "clinic-web", "module": "search", "instruction": "b"})
        assert started == [ANDROID, WEB]


# -- the queue ------------------------------------------------------------------------------
class TestQueue:
    def test_queueing_starts_nothing(self, eco, monkeypatch):
        """The entire point of the queue. If this ever starts a run, the approval gate is
        decoration."""
        monkeypatch.setattr(agent_bridge, "start_run",
                            lambda *a, **k: pytest.fail("queueing must not start a run"))
        out = call("queue_retest", {"app": "patient-android", "module": "search",
                                    "finding": "F001", "reason": "issue #12 closed"})
        assert "has NOT started" in out
        assert retests.summary(NAME)["pending"] == 1

    def test_the_same_finding_is_not_queued_twice(self, eco):
        """`sync_issue_status` runs repeatedly; a queue that grew an entry each time somebody
        checked Blackcode would be unusable within a day."""
        args = {"app": "patient-android", "module": "search", "finding": "F001",
                "reason": "closed"}
        call("queue_retest", args)
        assert "already on the re-test queue" in call("queue_retest", args)
        assert retests.summary(NAME)["pending"] == 1

    def test_a_dismissed_entry_is_not_raised_again(self, eco):
        """Saying no once should mean no. A dismissal that the next sync overrides is not a
        decision, it is a delay."""
        call("queue_retest", {"app": "patient-android", "module": "search", "finding": "F001",
                              "reason": "closed"})
        entry_id = retests.list_queued(NAME)[0]["id"]
        retests.decide(NAME, entry_id, "dismissed")
        assert retests.queue(NAME, ANDROID, "search", "F001") is None

    def test_queueing_an_unknown_finding_is_refused(self, eco):
        out = call("queue_retest", {"app": "patient-android", "module": "search",
                                    "finding": "F999", "reason": "x"})
        assert "No finding 'F999'" in out


# -- approval -------------------------------------------------------------------------------
class TestApproval:
    @pytest.fixture
    def queued(self, eco):
        call("queue_retest", {"app": "patient-android", "module": "search", "finding": "F001",
                              "reason": "issue #12 closed as done"})
        return retests.list_queued(NAME)[0]["id"]

    def test_approving_starts_the_run_and_records_the_decision(self, queued, monkeypatch):
        started = []
        monkeypatch.setattr(agent_bridge, "start_run",
                            lambda p, s, t, **kw: started.append((p, s, t)) or {"started": True})
        body = TestClient(create_app()).post(
            f"/ecosystems/{NAME}/retests/{queued}/approve").json()
        assert body["status"] == "approved"
        assert started and started[0][:2] == (ANDROID, "search")

    def test_the_instruction_tells_the_tester_not_to_assume_the_fix_landed(self, queued,
                                                                          monkeypatch):
        """It is re-checking a defect somebody said was fixed, which is the exact situation
        where an agent is most likely to record a pass it did not verify."""
        sent = []
        monkeypatch.setattr(agent_bridge, "start_run",
                            lambda p, s, t, **kw: sent.append(t) or {"started": True})
        TestClient(create_app()).post(f"/ecosystems/{NAME}/retests/{queued}/approve")
        assert "Do not assume the fix landed" in sent[0]
        assert "F001" in sent[0]

    def test_a_refused_run_leaves_the_entry_pending(self, queued):
        """The one failure this queue exists to prevent: an approval recorded as done while
        the module never ran would read later as a defect that was re-checked and passed."""
        device_locks.acquire(
            device_locks.key_for("android", None, ANDROID), ANDROID, "checkout")
        resp = TestClient(create_app()).post(f"/ecosystems/{NAME}/retests/{queued}/approve")
        assert resp.status_code == 409
        assert retests.get(NAME, queued)["status"] == "pending"

    def test_approving_twice_is_refused(self, queued, monkeypatch):
        monkeypatch.setattr(agent_bridge, "start_run", lambda *a, **k: {"started": True})
        client = TestClient(create_app())
        client.post(f"/ecosystems/{NAME}/retests/{queued}/approve")
        assert client.post(f"/ecosystems/{NAME}/retests/{queued}/approve").status_code == 409

    def test_dismissing_starts_nothing_and_is_recorded(self, queued, monkeypatch):
        monkeypatch.setattr(agent_bridge, "start_run",
                            lambda *a, **k: pytest.fail("dismiss must not start a run"))
        body = TestClient(create_app()).post(
            f"/ecosystems/{NAME}/retests/{queued}/dismiss").json()
        assert body["status"] == "dismissed"

    def test_the_board_reports_what_is_waiting(self, queued):
        board = TestClient(create_app()).get(f"/ecosystems/{NAME}/board").json()
        assert board["retests"]["pending"] == 1


# -- the loop closing ------------------------------------------------------------------------
def test_a_fix_confirmed_in_blackcode_queues_its_own_retest(eco, monkeypatch):
    """The whole point of wiring these together: a defect closing in the tracker is exactly
    when somebody should look at the device again, and exactly when nobody remembers to."""
    import blackcode

    monkeypatch.setattr(blackcode, "is_available", lambda: True)
    monkeypatch.setattr(blackcode, "issue_status", lambda number: {
        "number": number, "status": "done", "resolved": True,
        "url": f"https://issues.blackcode.ch/i/{number}"})
    store.set_finding_tracking(ANDROID, "search", "F001", issue_id=12,
                               issue_url="https://issues.blackcode.ch/i/12")

    out = call("sync_issue_status", {})
    assert "now resolved" in out
    assert "Queued 1 re-test" in out

    pending = retests.list_queued(NAME, "pending")
    assert len(pending) == 1
    assert pending[0]["finding"] == "F001"
    assert "#12" in pending[0]["reason"]

    # ...and running it again does not queue a second one.
    assert "Queued" not in call("sync_issue_status", {})


def test_the_prompt_describes_the_split_and_every_tool_it_has(eco):
    prompt = prompts.build_system_prompt(NAME, "main", "Main", "")
    for name in ecosystem_tools.ECOSYSTEM_TOOL_NAMES:
        assert name.rsplit("__", 1)[-1] in prompt
    # The claim the whole design rests on, in the prompt as well as in the code.
    assert "You can start a run; you cannot drive one." in prompt
    assert "{" not in prompt.replace("{ecosystem}", "")
