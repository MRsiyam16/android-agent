"""Jobs: a sweep of one app, a journey across several, and the manager sitting between steps.

The manager could always start a run. What it could not do was notice one had *finished* — its
session only wakes when something is said to it, so a commissioned run ended in silence and the
job sat there until a human asked. Thirteen modules meant thirteen prompts to a supervisor whose
whole job is not needing them.

So the loop under test is: a step ends, the manager is handed a turn to read it, and the next
step starts when that turn ends. The manager between every step is the expensive choice and the
deliberate one — it is what lets step 2's brief say "look for ref #4471", because something read
step 1's note before writing it. A journey without that gap is two unrelated tests in a row.

Most of these tests are therefore about *endings*, because every kind of ending used to mean the
same thing (nothing) and now means five different things: done, failed, parked, blocked, and the
manager's own turn finishing.

The other half is restraint, and where it points. A failure goes to the *manager* to diagnose,
not to the user — the user is raised only when nobody else can clear it, and then loudly.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import campaigns
import device_locks
import ecosystem
import project_paths
import scratchpad
from agent import ecosystem_tools, prompts, store
from agent.device_tools import DeviceSession
from backend import agent_bridge
from backend import projects as backend_projects
from backend.app import create_app
from backend.campaign_runner import CampaignRunner

NAME = "metaesthetics"
WEB = "clinic.example.com"
ANDROID = "com.patient.android"
IPAD = "ipad Test"
MODULES = ["booking", "search", "checkout"]


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(agent_bridge, "attached", lambda: [])
    monkeypatch.setattr("notify.send", lambda *a, **k: None)
    device_locks.reset()
    for package, role, platform in ((WEB, "clinic-web", "web"),
                                    (ANDROID, "patient-android", "android"),
                                    (IPAD, "doctor-ipad", "ios")):
        backend_projects.write_meta(package, platform=platform)
        ecosystem.tag(package, NAME, role)
    for slug in MODULES:
        store.create_subproject(WEB, slug.title(), f"cover {slug}", "approved")
    store.create_subproject(ANDROID, "Booking", "book an appointment", "approved")
    store.create_subproject(IPAD, "Appointments", "the doctor's list", "approved")
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


def sweep(modules=None) -> dict:
    return campaigns.create(
        NAME, [{"package": WEB, "role": "clinic-web", "module": m, "title": m.title(),
                "scope": "", "expect": ""} for m in (modules or MODULES)],
        kind="sweep", role="clinic-web", goal="full sweep")


def journey() -> dict:
    return campaigns.create(
        NAME,
        [{"package": ANDROID, "role": "patient-android", "module": "booking",
          "title": "Booking", "scope": "", "expect": "book one and write down its reference"},
         {"package": IPAD, "role": "doctor-ipad", "module": "appointments",
          "title": "Appointments", "scope": "", "expect": "find that exact appointment"}],
        kind="journey", role="patient-android -> doctor-ipad",
        goal="does a booking reach the doctor's iPad")


def drive(events: list[dict]) -> CampaignRunner:
    """Feed events to a fresh runner, the way `agent_bridge._emit` does."""
    runner = CampaignRunner()

    async def go():
        for event in events:
            await runner.notice(event)

    asyncio.run(go())
    return runner


def done(slug: str, package: str = WEB, findings: int = 0) -> dict:
    return {"type": "agent_done", "package": package, "slug": slug, "findings": findings}


def manager_done() -> dict:
    """The manager finishing its review turn — what actually releases the next step."""
    return {"type": "agent_done", "package": NAME, "slug": "main", "findings": 0}


# -- the plan ---------------------------------------------------------------------------------
class TestPlanning:
    def test_a_sweep_records_every_module_in_the_order_given(self):
        campaign = sweep()
        assert [s["module"] for s in campaign["steps"]] == MODULES
        assert all(s["status"] == "pending" for s in campaign["steps"])

    def test_a_journey_carries_each_step_app_and_what_it_must_establish(self):
        """`expect` is the whole difference between one job and two unrelated tests."""
        campaign = journey()
        assert [s["package"] for s in campaign["steps"]] == [ANDROID, IPAD]
        assert campaigns.apps(campaign) == [ANDROID, IPAD]
        assert "reference" in campaign["steps"][0]["expect"]

    def test_a_second_job_on_an_app_a_journey_holds_is_refused(self):
        """A journey reserves every app it names for its whole length. Giving one up between
        steps means a sweep could take it and strand the journey halfway, with the first half
        already done and not repeatable."""
        journey()
        with pytest.raises(ValueError) as exc:
            campaigns.create(NAME, [{"package": IPAD, "role": "doctor-ipad",
                                     "module": "appointments", "title": "A"}], kind="sweep")
        assert "already held by a job" in str(exc.value)

    def test_stopping_frees_the_app_for_a_new_job(self):
        first = sweep()
        campaigns.set_status(first["id"], "stopped")
        assert sweep()["id"] != first["id"]

    def test_a_stopped_job_does_not_leave_a_step_reading_as_running(self):
        """Otherwise the board says "testing booking" forever, long after nothing is."""
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        campaigns.set_status(campaign["id"], "stopped")
        assert campaigns.running_step(campaigns.get(campaign["id"])) is None

    def test_a_restart_pauses_what_was_running_rather_than_leaving_it_live(self):
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        touched = campaigns.reset_orphans()
        assert [c["id"] for c in touched] == [campaign["id"]]
        after = campaigns.get(campaign["id"])
        assert after["status"] == "paused"
        assert "restarted" in after["blocked"]["reason"]

    def test_a_v1_record_is_read_forward_rather_than_lost(self, tmp_path):
        """Campaigns written before steps knew which app they were in."""
        import json

        (tmp_path / "campaigns.json").write_text(json.dumps({
            "schema": 1,
            "campaigns": {"old@1": {"id": "old@1", "ecosystem": NAME, "package": WEB,
                                    "role": "clinic-web", "status": "done", "steps": [
                                        {"module": "booking", "status": "done"}]}}}),
            encoding="utf-8")
        loaded = campaigns.get("old@1")
        assert loaded["steps"][0]["package"] == WEB
        assert loaded["kind"] == "sweep"


# -- the loop ---------------------------------------------------------------------------------
class TestTheLoop:
    def test_a_step_ending_hands_the_manager_a_turn_and_does_not_start_the_next_yet(self,
                                                                                    started):
        """The gap is the feature. Starting the next module immediately is what left the
        manager unable to tell step two what step one found."""
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([done("booking", findings=3)])

        after = campaigns.get(campaign["id"])
        assert after["status"] == "reviewing"
        assert after["steps"][0]["status"] == "done"
        assert after["steps"][1]["status"] == "pending"      # not started
        assert [c[:2] for c in started] == [(NAME, "main")]  # only the manager was called

    def test_the_managers_turn_ending_starts_the_next_step(self, started):
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([done("booking"), manager_done()])

        assert [c[1] for c in started] == ["main", "search"]
        assert campaigns.get(campaign["id"])["steps"][1]["status"] == "running"

    def test_the_job_moves_even_if_the_managers_turn_errored(self, started):
        """The manager's control comes from having the turn and the tools, not from being
        load-bearing. A job that stalls because a review turn failed is worse than one that
        carries on unreviewed."""
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([done("booking"),
               {"type": "agent_error", "package": NAME, "slug": "main", "message": "boom"}])
        assert campaigns.get(campaign["id"])["steps"][1]["status"] == "running"

    def test_a_job_whose_review_turn_was_refused_gets_one_at_the_next_idle_moment(self,
                                                                                  monkeypatch):
        """Two jobs can end a step at once and there is one manager, so the second's turn is
        refused. Without the flag that job waits forever for a turn nobody ever gave it."""
        calls = []

        def busy_once(package, slug, text, **kwargs):
            calls.append((package, slug))
            if package == NAME and len(calls) == 1:
                raise agent_bridge.RunRefused("the manager is already running")
            return {"package": package, "slug": slug, "target": package, "started": True,
                    "watching": False, "watch_url": ""}

        monkeypatch.setattr(agent_bridge, "start_run", busy_once)
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)

        drive([done("booking")])
        assert campaigns.get(campaign["id"])["review_asked"] is False

        drive([manager_done()])
        assert campaigns.get(campaign["id"])["review_asked"] is True

    def test_a_refused_review_is_offered_again_on_a_clock_not_on_an_event(self, monkeypatch):
        """The deadlock this replaced, found by running a real journey: a step ended while the
        manager was mid-sentence, the turn was refused, and the recovery waited for the
        manager's *next* turn — which never came, because it was idle precisely for want of
        anything to say. Live job, `reviewing` forever, nothing able to move it.
        """
        import backend.campaign_runner as runner_mod

        calls = []

        def busy_once(package, slug, text, **kwargs):
            calls.append((package, slug))
            if package == NAME and len(calls) == 1:
                raise agent_bridge.RunRefused("the manager is already running")
            return {"package": package, "slug": slug, "target": package, "started": True,
                    "watching": False, "watch_url": ""}

        monkeypatch.setattr(agent_bridge, "start_run", busy_once)
        monkeypatch.setattr(runner_mod, "REVIEW_RETRY_SECONDS", 0.01)
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)

        async def go():
            runner = CampaignRunner()
            await runner.notice(done("booking"))
            # No further events at all — nothing but the clock may move this.
            await asyncio.sleep(0.15)

        asyncio.run(go())
        assert campaigns.get(campaign["id"])["review_asked"] is True

    def test_a_manager_that_never_frees_up_pauses_the_job_visibly(self, monkeypatch):
        """Giving up has to be a pause on the board, not a silent stall — a job nobody could
        be told about looks exactly like one still running."""
        import backend.campaign_runner as runner_mod

        monkeypatch.setattr(agent_bridge, "start_run",
                            lambda p, s, t, **kw: (_ for _ in ()).throw(
                                agent_bridge.RunRefused("always busy")))
        monkeypatch.setattr(runner_mod, "REVIEW_RETRY_SECONDS", 0.001)
        monkeypatch.setattr(runner_mod, "REVIEW_RETRY_LIMIT", 3)
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)

        async def go():
            runner = CampaignRunner()
            await runner.notice(done("booking"))
            await asyncio.sleep(0.1)

        asyncio.run(go())
        after = campaigns.get(campaign["id"])
        assert after["status"] == "paused"
        assert "never became free" in after["blocked"]["reason"]

    def test_what_a_step_said_is_carried_into_the_next_steps_brief(self, started):
        """The handoff. Without it the iPad step looks for *an* appointment, finds one, and
        reports a pass it never made."""
        campaign = journey()
        campaigns.start_step(campaign["id"], "booking", ANDROID)
        store.append_chat(ANDROID, "booking",
                          {"role": "agent", "text": "Booked Testina Doe, Tue 14:30, ref #4471"})

        drive([done("booking", package=ANDROID), manager_done()])

        assert campaigns.get(campaign["id"])["steps"][0]["reported"].startswith("Booked Testina")
        ipad_brief = next(c[2] for c in started if c[1] == "appointments")
        assert "ref #4471" in ipad_brief
        assert "find that exact appointment" in ipad_brief

    def test_the_scratchpad_reaches_the_next_step(self, started):
        campaign = journey()
        campaigns.start_step(campaign["id"], "booking", ANDROID)
        scratchpad.put(NAME, "last-booking", "ref #4471, Tue 14:30", author="patient-android")

        drive([done("booking", package=ANDROID), manager_done()])
        ipad_brief = next(c[2] for c in started if c[1] == "appointments")
        assert "last-booking" in ipad_brief and "#4471" in ipad_brief

    def test_the_last_step_completes_the_job_and_asks_for_a_verdict(self, started):
        campaign = sweep(["booking"])
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([done("booking"), manager_done()])

        assert campaigns.get(campaign["id"])["status"] == "done"
        assert any("finished" in c[2] for c in started if c[0] == NAME)

    def test_an_event_from_a_module_that_is_not_the_running_step_is_ignored(self, started):
        """A run someone starts by hand in the cockpit must not advance a job."""
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([done("checkout")])
        assert started == []
        assert campaigns.get(campaign["id"])["steps"][0]["status"] == "running"

    def test_two_endings_for_one_step_ask_for_one_review(self, started):
        """`agent_done` and a trailing `agent_busy:false` arrive back to back."""
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([done("booking"), done("booking")])
        assert len([c for c in started if c[0] == NAME]) == 1

    def test_a_module_that_will_not_start_does_not_wedge_the_job(self, monkeypatch):
        """A pending step left pending would be picked up again on the next advance and fail
        identically, forever."""
        def refuse(package, slug, text, **kwargs):
            if slug == "search":
                raise agent_bridge.RunRefused("no such module")
            return {"package": package, "slug": slug, "target": package, "started": True,
                    "watching": False, "watch_url": ""}

        monkeypatch.setattr(agent_bridge, "start_run", refuse)
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([done("booking"), manager_done()])

        after = campaigns.get(campaign["id"])
        assert after["steps"][1]["status"] == "failed"
        assert after["steps"][2]["status"] == "running"


# -- knowing when to stop, and who to bother --------------------------------------------------
class TestRestraint:
    def test_a_question_pauses_the_job_and_says_what_was_asked(self, started):
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([{"type": "agent_blocked", "package": WEB, "slug": "booking",
                "question": "Which card should I pay with?"}])

        after = campaigns.get(campaign["id"])
        assert after["status"] == "paused"
        assert after["blocked"]["question"] == "Which card should I pay with?"

    def test_a_question_reaches_the_user_out_of_band(self, monkeypatch, started):
        """The dashboard is worth nothing at 2am with the tab closed, which is exactly when an
        unattended job hits a locked device."""
        sent = []
        monkeypatch.setattr("notify.send", lambda title, body: sent.append((title, body)))
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([{"type": "agent_blocked", "package": WEB, "slug": "booking",
                "question": "Unlock the iPad please"}])
        assert sent and "Unlock the iPad" in sent[0][1]

    def test_answering_the_question_puts_the_job_back_to_running(self, started):
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([{"type": "agent_blocked", "package": WEB, "slug": "booking", "question": "?"},
               {"type": "agent_unblocked", "package": WEB, "slug": "booking"}])
        after = campaigns.get(campaign["id"])
        assert after["status"] == "running"
        assert campaigns.running_step(after)["module"] == "booking"

    def test_a_rate_limit_pauses_rather_than_burning_the_remaining_steps(self, started):
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([{"type": "agent_parked", "package": WEB, "slug": "booking"}])

        after = campaigns.get(campaign["id"])
        assert after["status"] == "paused"
        assert "window" in after["blocked"]["reason"]
        assert [c for c in started if c[0] != NAME] == []

    def test_a_failed_step_goes_to_the_manager_to_diagnose_not_to_the_user(self, started,
                                                                          monkeypatch):
        """"Check what went wrong, fix it if you can, call me only if you need me." The
        instruction is in the brief, not left to the manager's disposition."""
        sent = []
        monkeypatch.setattr("notify.send", lambda title, body: sent.append(body))
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([{"type": "agent_error", "package": WEB, "slug": "booking",
                "message": "the browser closed"}])

        review = next(c[2] for c in started if c[0] == NAME)
        assert "work out why before telling the user" in review
        assert "control_campaign" in review and "retry" in review
        assert sent == [], "the user must not be interrupted for a failure the manager can fix"

    def test_a_failed_step_still_lets_the_job_carry_on(self, started):
        """A module that broke is a result. Twelve modules skipped because the first errored
        is not."""
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        drive([{"type": "agent_error", "package": WEB, "slug": "booking", "message": "boom"},
               manager_done()])

        after = campaigns.get(campaign["id"])
        assert after["steps"][0]["status"] == "failed"
        assert after["steps"][1]["status"] == "running"

    def test_a_busy_target_pauses_instead_of_failing(self, started):
        """Somebody took the device between two steps. That is a wait, not a broken job."""
        import backend.campaign_runner as runner_mod

        monkeypatch_wait = runner_mod.TARGET_WAIT_SECONDS
        runner_mod.TARGET_WAIT_SECONDS = 0.2
        try:
            campaign = sweep()
            campaigns.start_step(campaign["id"], "booking", WEB)
            device_locks.acquire(device_locks.key_for("web", None, WEB), WEB, "someone-else")
            asyncio.run(CampaignRunner().advance(campaign["id"]))
        finally:
            runner_mod.TARGET_WAIT_SECONDS = monkeypatch_wait

        after = campaigns.get(campaign["id"])
        assert after["status"] == "paused"
        assert "someone-else" in after["blocked"]["reason"]


# -- the tools --------------------------------------------------------------------------------
class TestTools:
    def test_test_app_plans_every_module_and_starts_the_first(self, ready_stacks, started):
        out = call("test_app", {"app": "clinic-web", "goal": "release check"})
        assert "3 modules" in out
        assert campaigns.active_for(WEB)["goal"] == "release check"

    def test_a_stack_that_is_down_stops_the_job_before_it_starts(self, monkeypatch, started):
        import stacks

        monkeypatch.setattr(stacks, "status", lambda p: {
            "platform": p, "ready": False, "detail": "no browser", "fix": "install it",
            "devices": [], "starting": False})
        out = call("test_app", {"app": "clinic-web"})
        assert "have to be ready first" in out
        assert campaigns.active_for(WEB) is None

    def test_a_journey_checks_every_app_it_will_touch_before_step_one(self, monkeypatch,
                                                                      started):
        """The failure this prevents is the expensive one: finding out at step three that the
        iPad was never reachable, with the booking already made and not repeatable."""
        import stacks

        monkeypatch.setattr(stacks, "status", lambda p: {
            "platform": p, "ready": p != "ios", "detail": "asleep" if p == "ios" else "ok",
            "fix": "unlock it" if p == "ios" else "", "devices": [], "starting": False})
        out = call("run_journey", {"goal": "does a booking reach the iPad", "steps": [
            {"app": "patient-android", "module": "booking", "expect": "book one"},
            {"app": "doctor-ipad", "module": "appointments", "expect": "find it"}]})
        assert "doctor-ipad" in out and "unlock it" in out
        assert campaigns.active_for(ANDROID) is None

    def test_a_journey_holds_every_app_it_names(self, ready_stacks, started):
        call("run_journey", {"goal": "g", "steps": [
            {"app": "patient-android", "module": "booking", "expect": "book one"},
            {"app": "doctor-ipad", "module": "appointments", "expect": "find it"}]})
        assert campaigns.active_for(ANDROID) is not None
        assert campaigns.active_for(IPAD) is not None

    def test_a_one_step_journey_is_refused_as_being_a_run_module(self, ready_stacks, started):
        out = call("run_journey", {"goal": "g", "steps": [
            {"app": "patient-android", "module": "booking", "expect": "book one"}]})
        assert "at least two steps" in out

    def test_a_journey_naming_an_unknown_module_creates_it_and_runs(self, ready_stacks,
                                                                    started):
        """It used to refuse, and the refusal cost a round trip for nothing: the caller has
        already said which app, which slug and what the step must establish, which is all
        `create_module` is handed. Losing that round trip lost a live verification verdict —
        see `docs/VERIFIER.md`."""
        out = call("run_journey", {"goal": "g", "steps": [
            {"app": "patient-android", "module": "booking", "expect": "x"},
            {"app": "doctor-ipad", "module": "nope", "expect": "check the new list"}]})
        assert "Journey planned and started" in out
        entry = store.get_subproject(IPAD, "nope")
        assert entry is not None and entry["scope"] == "check the new list"
        assert campaigns.active_for(ANDROID) is not None

    def test_a_journey_naming_an_unknown_app_still_starts_nothing(self, ready_stacks, started):
        """The slug is the caller's to invent; the app is not. A role that does not exist is a
        mistake, and creating something for it would hide it."""
        out = call("run_journey", {"goal": "g", "steps": [
            {"app": "patient-android", "module": "booking", "expect": "x"},
            {"app": "no-such-app", "module": "nope", "expect": "y"}]})
        assert "step 2" in out and "no app" in out
        assert campaigns.active_for(ANDROID) is None

    def test_only_untested_skips_what_has_already_run(self, ready_stacks, started):
        store.update_subproject(WEB, "booking", status="tested")
        call("test_app", {"app": "clinic-web", "only_untested": True})
        assert [s["module"] for s in campaigns.active_for(WEB)["steps"]] == ["search", "checkout"]

    def test_retry_puts_a_failed_step_back(self, ready_stacks, started):
        campaign = sweep()
        campaigns.start_step(campaign["id"], "booking", WEB)
        campaigns.finish_step(campaign["id"], "booking", "failed", package=WEB, note="stack")
        out = call("control_campaign", {"app": "clinic-web", "action": "retry",
                                        "module": "booking"})
        assert "queued again" in out
        assert campaigns.get(campaign["id"])["steps"][0]["status"] in ("pending", "running")

    def test_retrying_a_step_that_never_failed_is_refused(self, ready_stacks, started):
        """Caught in review: the guard read the state *after* the mutation, and "is it pending
        now" is equally true of a step that had never run — so it cheerfully reported retrying
        something that had not happened yet."""
        sweep()
        out = call("control_campaign", {"app": "clinic-web", "action": "retry",
                                        "module": "booking"})
        assert "not failed" in out
        assert campaigns.active_for(WEB)["steps"][0]["status"] == "pending"

    def test_the_manager_can_rebrief_a_step_that_has_not_run(self, ready_stacks, started):
        journey()
        out = call("set_step_brief", {"app": "doctor-ipad", "module": "appointments",
                                      "expect": "look for ref #4471 specifically"})
        assert "#4471" in out
        campaign = campaigns.active_for(IPAD)
        assert campaign["steps"][1]["expect"] == "look for ref #4471 specifically"

    def test_a_step_already_running_cannot_be_rebriefed(self, ready_stacks, started):
        campaign = journey()
        campaigns.start_step(campaign["id"], "booking", ANDROID)
        out = call("set_step_brief", {"app": "patient-android", "module": "booking",
                                      "expect": "too late"})
        assert "cannot be re-briefed" in out

    def test_campaign_status_shows_each_step_with_its_app(self, ready_stacks, started):
        journey()
        out = call("campaign_status", {})
        assert "patient-android/booking" in out and "doctor-ipad/appointments" in out


# -- the scratchpad ---------------------------------------------------------------------------
class TestScratchpad:
    def test_a_note_written_by_one_app_is_readable_by_another(self):
        scratchpad.put(NAME, "last-booking", "ref #4471", author="patient-android/booking")
        assert scratchpad.get(NAME, "last-booking")["value"] == "ref #4471"

    def test_keys_are_normalised_so_two_spellings_are_one_note(self):
        """The writer and the reader are two agents in two sessions that never see each
        other's text. Nothing else would ever catch a near-miss."""
        scratchpad.put(NAME, "Last Booking", "first")
        scratchpad.put(NAME, "last-booking", "second")
        assert len(scratchpad.list_all(NAME)) == 1
        assert scratchpad.get(NAME, "LAST BOOKING")["value"] == "second"

    def test_an_overlong_value_is_refused_with_a_reason(self):
        with pytest.raises(ValueError) as exc:
            scratchpad.put(NAME, "essay", "x" * 5000)
        assert "not a report" in str(exc.value)

    def test_a_tester_can_write_a_note_and_says_which_product_it_went_to(self):
        import mcp.types as mcp_types

        from agent.device_tools import build_device_server

        instance = build_device_server(DeviceSession(ANDROID, "booking"))["instance"]
        handler = instance.request_handlers[mcp_types.CallToolRequest]
        request = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name="note_put", arguments={"key": "last-booking", "value": "ref #4471"}))
        out = asyncio.run(handler(request)).root.content[0].text
        assert NAME in out
        entry = scratchpad.get(NAME, "last-booking")
        assert entry["author"] == "patient-android/booking"

    def test_the_board_counts_the_notes_and_the_route_can_drop_one(self):
        scratchpad.put(NAME, "keep", "a")
        scratchpad.put(NAME, "drop-me", "b")
        client = TestClient(create_app())
        assert client.get(f"/ecosystems/{NAME}/board").json()["scratchpad"] == 2
        assert client.delete(f"/ecosystems/{NAME}/scratchpad/drop-me").status_code == 200
        assert [n["key"] for n in scratchpad.list_all(NAME)] == ["keep"]


# -- what the board shows -----------------------------------------------------------------------
def test_the_board_carries_the_running_indicator():
    campaign = sweep()
    campaigns.start_step(campaign["id"], "booking", WEB)
    body = TestClient(create_app()).get(f"/ecosystems/{NAME}/board").json()
    assert body["campaigns"]["live"] == 1
    assert body["campaigns"]["campaigns"][0]["current"] == "booking"
    assert body["campaigns"]["campaigns"][0]["total"] == 3


def test_a_finished_job_leaves_the_indicator_empty():
    campaign = sweep()
    campaigns.set_status(campaign["id"], "done")
    assert TestClient(create_app()).get(
        f"/ecosystems/{NAME}/board").json()["campaigns"]["live"] == 0


def test_the_prompts_describe_what_each_tier_can_now_do():
    manager = prompts.build_system_prompt(NAME, "main", "Main", "")
    for tool in ("test_app", "run_journey", "campaign_status", "control_campaign",
                 "set_step_brief", "note_put", "note_list", "note_drop"):
        assert tool in manager, tool
    assert "You are between every step" in manager
    assert "work out why before telling the user" in manager

    tester = prompts.build_system_prompt(WEB, "booking", "Booking", "", platform="web")
    assert "note_put" in tester and "note_get" in tester
    assert "only thing you produce that another app" in tester
