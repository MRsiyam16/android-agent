"""A whole app tested module by module, with nobody typing "next".

The manager could always start a run. What it could not do was notice one had *finished* — its
session only wakes when something is said to it, so a commissioned run ended in silence and the
campaign sat there until a human asked. Thirteen modules meant thirteen prompts to a supervisor
whose whole job is not needing them.

What is under test here is the seam that fixes it: the event stream every run already emits is
read by something outside the conversation, and that something walks the list. So the tests are
mostly about *endings* — what happens when a step finishes, fails, parks, or stops to ask a
question — because every one of those used to mean the same thing (nothing) and now means four
different things.

The other half is restraint. A runner that carried on regardless would turn one bad answer into
twelve, so a failure pauses and hands the manager a turn. That is the "only ask when it
absolutely needs to" rule, implemented rather than requested.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import campaigns
import device_locks
import ecosystem
import project_paths
from agent import ecosystem_tools, prompts, store
from agent.device_tools import DeviceSession
from backend import agent_bridge
from backend import projects as backend_projects
from backend.app import create_app
from backend.campaign_runner import CampaignRunner

NAME = "metaesthetics"
WEB = "clinic.example.com"
MODULES = ["booking", "search", "checkout"]


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(agent_bridge, "attached", lambda: [])
    device_locks.reset()
    backend_projects.write_meta(WEB, platform="web")
    ecosystem.tag(WEB, NAME, "clinic-web")
    for slug in MODULES:
        store.create_subproject(WEB, slug.title(), f"cover {slug}", "approved")
    ecosystem.create_supervisor(NAME)
    store.create_subproject(NAME, "Main")
    yield tmp_path
    device_locks.reset()


@pytest.fixture
def ready_stacks(monkeypatch):
    import stacks

    monkeypatch.setattr(stacks, "status", lambda platform: {
        "platform": platform, "ready": True, "detail": "stubbed", "fix": "",
        "devices": [], "starting": False})


@pytest.fixture
def started(monkeypatch):
    """Every run start, recorded instead of performed."""
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(agent_bridge, "start_run",
                        lambda p, s, t, **kw: calls.append((p, s, t))
                        or {"package": p, "slug": s, "target": p, "started": True,
                            "watching": False, "watch_url": ""})
    return calls


def call(name: str, args: dict) -> str:
    import mcp.types as mcp_types

    instance = ecosystem_tools.build_ecosystem_server(DeviceSession(NAME, "main"),
                                                      NAME)["instance"]
    handler = instance.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=args))
    return asyncio.run(handler(request)).root.content[0].text


def plan(modules=None) -> dict:
    return campaigns.create(
        NAME, WEB, [{"slug": s, "title": s.title(), "scope": ""} for s in (modules or MODULES)],
        role="clinic-web", goal="full sweep")


def drive(events: list[dict]) -> CampaignRunner:
    """Feed events to a fresh runner, the way `agent_bridge._emit` does."""
    runner = CampaignRunner()

    async def go():
        for event in events:
            await runner.notice(event)

    asyncio.run(go())
    return runner


def done(slug: str, findings: int = 0) -> dict:
    return {"type": "agent_done", "package": WEB, "slug": slug, "findings": findings}


# -- the plan ---------------------------------------------------------------------------------
class TestPlanning:
    def test_a_campaign_records_every_module_in_the_order_given(self):
        campaign = plan()
        assert [s["module"] for s in campaign["steps"]] == MODULES
        assert all(s["status"] == "pending" for s in campaign["steps"])

    def test_a_second_campaign_on_the_same_app_is_refused(self):
        """One app is one target and one target has one driver. A second sweep could only
        queue behind the first while looking, on the board, like twice the progress."""
        plan()
        with pytest.raises(ValueError) as exc:
            plan()
        assert "already has a campaign running" in str(exc.value)

    def test_stopping_frees_the_app_for_a_new_campaign(self):
        first = plan()
        campaigns.set_status(first["id"], "stopped")
        assert plan()["id"] != first["id"]

    def test_a_stopped_campaign_does_not_leave_a_step_reading_as_running(self):
        """Otherwise the board says "testing booking" forever, long after nothing is."""
        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        campaigns.set_status(campaign["id"], "stopped")
        assert campaigns.running_step(campaigns.get(campaign["id"])) is None

    def test_a_restart_pauses_what_was_running_rather_than_leaving_it_live(self):
        """Steps run in the server process. After a restart nothing is driving them, and a
        board reporting a module under test that nothing is testing is worse than one
        reporting nothing — it is the same shape as progress."""
        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        touched = campaigns.reset_orphans()
        assert [c["id"] for c in touched] == [campaign["id"]]
        after = campaigns.get(campaign["id"])
        assert after["status"] == "paused"
        assert "restarted" in after["blocked"]["reason"]


# -- walking it -------------------------------------------------------------------------------
class TestAdvancing:
    def test_finishing_one_module_starts_the_next_with_no_prompting(self, started):
        """The whole point. This is the step that used to require a human to type "check"."""
        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        drive([done("booking", findings=3)])

        assert [c[1] for c in started] == ["search"]
        after = campaigns.get(campaign["id"])
        assert after["steps"][0]["status"] == "done"
        assert after["steps"][0]["findings"] == 3
        assert after["steps"][1]["status"] == "running"

    def test_the_last_module_completes_the_campaign(self, started):
        campaign = plan(["booking"])
        campaigns.start_step(campaign["id"], "booking")
        drive([done("booking")])
        assert campaigns.get(campaign["id"])["status"] == "done"

    def test_the_manager_is_handed_a_turn_when_the_sweep_ends(self, started):
        """Not a notice: finishing is exactly when somebody should read what thirteen modules
        filed and say where the app stands."""
        campaign = plan(["booking"])
        campaigns.start_step(campaign["id"], "booking")
        drive([done("booking", findings=2)])

        to_manager = [c for c in started if c[0] == NAME]
        assert to_manager, "the manager was never told the sweep finished"
        assert "finished" in to_manager[0][2]

    def test_a_step_ending_is_reported_without_spending_a_turn(self, started):
        """A line per module in the manager's transcript is free. A turn per module is
        thirteen turns of bookkeeping a list does better."""
        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        drive([done("booking", findings=1)])

        transcript = store.read_chat(NAME, "main")
        assert any("booking" in str(m.get("text", "")) for m in transcript)
        # ...and the only run started was the next module, not a manager turn.
        assert [c[0] for c in started] == [WEB]

    def test_an_event_from_a_module_that_is_not_the_running_step_is_ignored(self, started):
        """A run someone starts by hand in the cockpit must not advance a sweep."""
        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        drive([done("checkout")])
        assert started == []
        assert campaigns.get(campaign["id"])["steps"][0]["status"] == "running"

    def test_two_endings_for_one_step_start_the_next_module_once(self, started):
        """`agent_done` and a trailing `agent_busy:false` arrive back to back."""
        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        drive([done("booking"), done("booking")])
        assert [c[1] for c in started] == ["search"]

    def test_a_module_that_will_not_start_does_not_wedge_the_sweep(self, monkeypatch):
        """A pending step left pending would be picked up again on the next advance and fail
        identically, forever."""
        def refuse(package, slug, text, **kwargs):
            if slug == "search":
                raise agent_bridge.RunRefused("no such module")
            return {"package": package, "slug": slug, "target": package, "started": True,
                    "watching": False, "watch_url": ""}

        monkeypatch.setattr(agent_bridge, "start_run", refuse)
        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        drive([done("booking")])

        after = campaigns.get(campaign["id"])
        assert after["steps"][1]["status"] == "failed"
        assert after["steps"][2]["status"] == "running"


# -- knowing when to stop -----------------------------------------------------------------------
class TestRestraint:
    def test_a_question_pauses_the_sweep_and_says_what_was_asked(self, started):
        """Carrying on past a module waiting for a human is how one unanswered question
        becomes twelve modules of guesses."""
        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        drive([{"type": "agent_blocked", "package": WEB, "slug": "booking",
                "question": "Which card should I pay with?"}])

        after = campaigns.get(campaign["id"])
        assert after["status"] == "paused"
        assert after["blocked"]["question"] == "Which card should I pay with?"
        assert [c[1] for c in started] == ["main"]      # the manager, not the next module

    def test_answering_the_question_puts_the_sweep_back_to_running(self, started):
        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        drive([{"type": "agent_blocked", "package": WEB, "slug": "booking", "question": "?"},
               {"type": "agent_unblocked", "package": WEB, "slug": "booking"}])
        after = campaigns.get(campaign["id"])
        assert after["status"] == "running"
        assert campaigns.running_step(after)["module"] == "booking"

    def test_a_rate_limit_pauses_rather_than_burning_through_the_remaining_modules(self,
                                                                                   started):
        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        drive([{"type": "agent_parked", "package": WEB, "slug": "booking",
                "reason": "rate_limit"}])

        after = campaigns.get(campaign["id"])
        assert after["status"] == "paused"
        assert "window" in after["blocked"]["reason"]
        assert [c[1] for c in started] == []           # nothing else was started

    def test_a_failed_module_still_advances_but_is_recorded_as_failed(self, started):
        """A module that broke is a result. Twelve modules skipped because the first one
        errored is not."""
        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        drive([{"type": "agent_error", "package": WEB, "slug": "booking",
                "message": "the browser closed"}])

        after = campaigns.get(campaign["id"])
        assert after["steps"][0]["status"] == "failed"
        assert "browser closed" in after["steps"][0]["note"]
        assert [c[1] for c in started] == ["search"]

    def test_a_busy_target_pauses_instead_of_being_refused_into_a_failure(self, started):
        """Somebody took the device between two modules. That is a wait, not a broken sweep."""
        import backend.campaign_runner as runner_mod

        campaign = plan()
        campaigns.start_step(campaign["id"], "booking")
        device_locks.acquire(device_locks.key_for("web", None, WEB), WEB, "someone-else")

        runner = CampaignRunner()
        asyncio.run(asyncio.wait_for(_advance_with_short_wait(runner, campaign["id"],
                                                             runner_mod), 10))
        after = campaigns.get(campaign["id"])
        assert after["status"] == "paused"
        assert "someone-else" in after["blocked"]["reason"]


async def _advance_with_short_wait(runner, campaign_id, runner_mod):
    runner_mod.TARGET_WAIT_SECONDS = 0.2
    await runner.advance(campaign_id)


# -- the tools --------------------------------------------------------------------------------
class TestTools:
    def test_test_app_plans_every_module_and_starts_the_first(self, ready_stacks, started):
        out = call("test_app", {"app": "clinic-web", "goal": "release check"})
        assert "3 modules" in out
        campaign = campaigns.active_for(WEB)
        assert campaign is not None
        assert campaign["goal"] == "release check"

    def test_test_app_refuses_when_the_stack_is_down(self, monkeypatch, started):
        import stacks

        monkeypatch.setattr(stacks, "status", lambda p: {
            "platform": p, "ready": False, "detail": "no browser", "fix": "install it",
            "devices": [], "starting": False})
        out = call("test_app", {"app": "clinic-web"})
        assert "not up" in out
        assert campaigns.active_for(WEB) is None

    def test_only_untested_skips_what_has_already_run(self, ready_stacks, started):
        store.update_subproject(WEB, "booking", status="tested")
        call("test_app", {"app": "clinic-web", "only_untested": True})
        assert [s["module"] for s in campaigns.active_for(WEB)["steps"]] == ["search", "checkout"]

    def test_naming_an_unknown_module_starts_nothing(self, ready_stacks, started):
        out = call("test_app", {"app": "clinic-web", "modules": ["booking", "nope"]})
        assert "nope" in out
        assert campaigns.active_for(WEB) is None

    def test_a_sweep_with_nothing_left_to_run_says_so_rather_than_starting_empty(
            self, ready_stacks, started):
        for slug in MODULES:
            store.update_subproject(WEB, slug, status="tested")
        out = call("test_app", {"app": "clinic-web", "only_untested": True})
        assert "already marked tested" in out

    def test_campaign_status_shows_each_step(self, ready_stacks, started):
        plan()
        campaigns.start_step(campaigns.active_for(WEB)["id"], "booking")
        out = call("campaign_status", {})
        assert "booking" in out and "running" in out and "checkout" in out

    def test_control_campaign_stops_it(self, started):
        plan()
        out = call("control_campaign", {"app": "clinic-web", "action": "stop"})
        assert "Stopped" in out
        assert campaigns.active_for(WEB) is None

    def test_control_campaign_skips_one_module(self, started):
        plan()
        call("control_campaign", {"app": "clinic-web", "action": "skip", "module": "search"})
        steps = {s["module"]: s["status"] for s in campaigns.active_for(WEB)["steps"]}
        assert steps["search"] == "skipped"


# -- what the board shows -----------------------------------------------------------------------
def test_the_board_carries_the_running_indicator():
    campaign = plan()
    campaigns.start_step(campaign["id"], "booking")
    body = TestClient(create_app()).get(f"/ecosystems/{NAME}/board").json()
    assert body["campaigns"]["live"] == 1
    assert body["campaigns"]["campaigns"][0]["current"] == "booking"
    assert body["campaigns"]["campaigns"][0]["total"] == 3


def test_a_finished_campaign_leaves_the_indicator_empty():
    campaign = plan()
    campaigns.set_status(campaign["id"], "done")
    body = TestClient(create_app()).get(f"/ecosystems/{NAME}/board").json()
    assert body["campaigns"]["live"] == 0


def test_the_prompt_tells_the_manager_it_no_longer_babysits():
    prompt = prompts.build_system_prompt(NAME, "main", "Main", "")
    for tool in ("test_app", "campaign_status", "control_campaign"):
        assert tool in prompt
    assert "You do not poll it" in prompt
