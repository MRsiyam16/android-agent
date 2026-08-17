"""What walks a job: start a step, wait for it to end, let the manager read it, start the next.

The manager's session only wakes when something is said to it. That is the whole reason this
file exists — a run it commissioned finished in silence, and the job sat there until a human
noticed and typed "check". Here, the thing that notices is the event stream every run already
emits, and what it does with the news is hand the manager a turn.

**The loop.**

    step ends  ->  record what it filed
               ->  status: reviewing
               ->  give the manager a turn: here is what finished, here is the scratchpad,
                   here is what is left; fix anything fixable
    that turn ends  ->  start the next step, with whatever the manager wrote into the brief

The manager between every step is the expensive choice and the deliberate one. It is what lets
step 2's brief say "look for ref #4471" — because something read step 1's note before writing
it. A journey without that is two unrelated tests that happen to run in order.

**It cannot stall on the manager.** The next step starts when the manager's turn *ends*,
whatever the manager did with it. A turn that errors, runs out of window, or simply says "ok"
all advance the job. The manager's control comes from having the turn and the tools, not from
being load-bearing.

**Failures go to the manager, not to the user.** A step that fails hands over the error and an
instruction to diagnose it: bring a stack back up, retry the step, re-target it. The user is
raised only when the manager cannot proceed without them — and then loudly, on the dashboard
and as a desktop notification, because the usual reason is a locked device at 2am.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import campaigns
import device_locks
import notify
import scratchpad
from agent import store as agent_store

logger = logging.getLogger("server.campaigns")

#: How long to wait for a finished step's target to actually come free before giving up. The
#: release happens milliseconds after `agent_done`, so anything near this means somebody else
#: took the device — a pause, not a failure.
TARGET_WAIT_SECONDS = 90.0

#: Events that end a step, mapped to what they mean for it.
_ENDINGS = {"agent_done": "done", "agent_error": "failed", "agent_parked": "failed"}

#: How long to wait before offering the manager its review turn again, and how many times.
#:
#: A step can end while the manager is still mid-sentence about the previous one, and a turn
#: offered then is refused. The original recovery waited for the manager's *next* turn to
#: notice — which assumes it will take one, and it will not: it is idle precisely because
#: nobody is talking to it. That deadlocked a live journey with `status=reviewing,
#: review_asked=False` and nothing in the system able to move it. So the retry is on a clock
#: rather than on an event that may never arrive.
REVIEW_RETRY_SECONDS = 20.0
REVIEW_RETRY_LIMIT = 15


def _step_brief(campaign: dict[str, Any], step: dict[str, Any], index: int, total: int,
                previous: Optional[dict[str, Any]]) -> str:
    """The instruction one step is started with.

    Three things it must carry, and each was a real failure before it did:

    * **Where it sits in a plan.** A tester told only "test search" spends forty turns on
      search. Told it is 4 of 13, it covers its area and stops.
    * **What the step before it established.** Without this the iPad step goes looking for
      *an* appointment, finds one, and reports success about something it never verified.
    * **The shared scratchpad.** The same handoff, for facts written by steps further back or
      by the manager itself.
    """
    role = step.get("role") or step.get("package")
    lines = [
        f"You are step {index} of {total} in a job across this product"
        + (f" — {campaign['goal']}" if campaign.get("goal") else "") + ".",
        "",
        f"Your part is **{step['title']}** in {role}, and only that. The other steps cover the "
        f"other parts; work that belongs to them is theirs.",
    ]
    if step.get("scope"):
        lines += ["", f"This module's scope: {step['scope']}"]
    if step.get("expect"):
        lines += ["", f"**What this step must establish:** {step['expect']}"]

    if previous and previous.get("reported"):
        lines += ["", f"**The step before you ({previous.get('role')}/{previous['module']}) "
                      f"reported:**", previous["reported"]]

    notes = scratchpad.render(campaign["ecosystem"])
    if notes and "empty" not in notes:
        lines += ["", "**Shared scratchpad** — what other apps' agents have written down for "
                      "this job. Use it rather than guessing:", notes]

    if campaign.get("instruction"):
        lines += ["", campaign["instruction"]]

    lines += [
        "",
        "Record what you actually observe — a finding per case, pass or fail, with the "
        "screenshot behind it.",
        "",
        "**Before you finish**, write anything a later step will need into the shared "
        "scratchpad with `note_put` — a reference number, an account you created, a time slot. "
        "Another app's agent cannot see your screen or your transcript; the scratchpad is the "
        "only thing that crosses. Then say plainly what you established, because that is "
        "handed to the next step verbatim.",
        "",
        "If something blocks you that only a human can clear — a credential you do not have, a "
        "payment, a destructive action — ask. Everything else, decide and carry on: nobody is "
        "watching this step by step.",
    ]
    return "\n".join(lines)


def _review_brief(campaign: dict[str, Any], step: dict[str, Any], outcome: str,
                  findings: int, note: str) -> str:
    """What the manager is handed when a step ends. Its turn is the gap between two steps."""
    counts = campaigns.progress(campaign)
    remaining = [s for s in campaign["steps"] if s["status"] == "pending"]
    role = step.get("role") or step.get("package")

    lines = [f"Step {counts['finished']} of {counts['total']} of the "
             f"{campaign.get('kind', 'sweep')} just ended: **{role}/{step['module']}**"
             f" — {outcome}."]
    if outcome == "done":
        lines.append(f"It filed {findings} finding(s).")
    else:
        lines.append(f"It did not finish cleanly: {note or outcome}")
    if step.get("reported"):
        lines += ["", "What it said it established:", step["reported"]]

    notes = scratchpad.render(campaign["ecosystem"])
    lines += ["", "Shared scratchpad right now:", notes]

    if remaining:
        lines += ["", "Still to run, in order:"]
        lines += [f"- {s.get('role') or s['package']}/{s['module']}"
                  + (f" — {s['expect']}" if s.get("expect") else "") for s in remaining]
    else:
        lines += ["", "That was the last step."]

    if outcome != "done":
        lines += [
            "",
            "**This one failed, so work out why before telling the user.** Read what it filed "
            "and what it said. A stack that went down you can bring back with `start_app` and "
            "then `control_campaign` action=retry. A module pointed at the wrong thing you can "
            "fix with `update_module`. A step whose premise no longer holds you can skip. "
            "Raise it with the user only if you genuinely cannot proceed without them — and "
            "say exactly what you need, because they are being interrupted.",
        ]
    lines += [
        "",
        "**The next step starts as soon as this turn ends**, so do what you want done first: "
        "`note_put` anything the next step should know, `control_campaign` to skip, retry or "
        "stop, or `set_step_brief` to tell the next module what to look for now that you have "
        "read this. If nothing needs changing, say so briefly and it will carry on.",
    ]
    return "\n".join(lines)


class CampaignRunner:
    """Walks jobs. One instance, held by `agent_bridge`."""

    def __init__(self) -> None:
        #: Jobs with an advance in flight, so two events for one step cannot start the next
        #: step twice. `agent_done` and a trailing `agent_busy:false` arrive back to back.
        self._advancing: set[str] = set()

    # -- talking to the manager ------------------------------------------------------------
    @staticmethod
    def _supervisor(campaign: dict[str, Any]) -> tuple[Optional[str], str]:
        import ecosystem as ecosystem_mod

        package = ecosystem_mod.supervisor(campaign["ecosystem"])
        return package, (agent_store.main_slug(package) if package else "main")

    async def _write(self, campaign: dict[str, Any], text: str) -> None:
        """A line in the manager's transcript. Free, immediate, needs no reply."""
        from . import agent_bridge

        package, slug = self._supervisor(campaign)
        if not package:
            return
        await asyncio.to_thread(agent_store.append_chat, package, slug,
                                {"role": "notice", "text": text})
        await agent_bridge.emit({"type": "agent_notice", "package": package, "slug": slug,
                                 "text": text})

    async def _hand_turn(self, campaign: dict[str, Any], text: str) -> bool:
        """Give the manager an actual turn. False when it was busy and could not take one."""
        from . import agent_bridge

        package, slug = self._supervisor(campaign)
        if not package:
            return False
        try:
            agent_bridge.start_run(package, slug, text)
            return True
        except Exception as exc:  # noqa: BLE001 - a busy manager is not a job failure
            logger.info("manager could not take a turn for %s (%s)", campaign["id"], exc)
            return False

    async def _raise_with_user(self, campaign: dict[str, Any], headline: str,
                               detail: str) -> None:
        """The one path that interrupts a person. Dashboard, chat, and a desktop notification.

        The notification is the out-of-band half and exists for one situation: an unattended
        job hits a locked device at 2am and otherwise waits eight hours for somebody to open
        the tab.
        """
        await self._write(campaign, f"**{headline}**\n\n{detail}")
        notify.send(f"QA Tester AI — {campaign.get('role') or campaign['ecosystem']}",
                    f"{headline} {detail}")

    async def _broadcast(self, campaign: dict[str, Any]) -> None:
        """Push the job's shape to every open board, so the indicator is live."""
        from . import agent_bridge

        await agent_bridge.emit({
            "type": "agent_campaign", "ecosystem": campaign["ecosystem"],
            "campaign": campaign["id"], "kind": campaign.get("kind", "sweep"),
            "status": campaign["status"], "role": campaign.get("role", ""),
            "blocked": campaign.get("blocked"), **campaigns.progress(campaign)})

    # -- starting ---------------------------------------------------------------------------
    async def _wait_for_target(self, package: str) -> Optional[str]:
        """Block until nothing holds this app's target. Returns why not, or None when free."""
        from . import agent_bridge

        pinned, platform = agent_bridge.device_for(package)
        key = device_locks.key_for(platform, pinned, package)
        deadline = asyncio.get_running_loop().time() + TARGET_WAIT_SECONDS
        while True:
            holder = device_locks.holder(key)
            if holder is None:
                return None
            if asyncio.get_running_loop().time() >= deadline:
                return (f"{holder['package']} / {holder['slug']} has been driving {key} since "
                        f"{holder['since']} and has not let go")
            await asyncio.sleep(0.5)

    async def advance(self, campaign_id: str) -> None:
        """Start the next pending step, or finish the job."""
        if campaign_id in self._advancing:
            return
        self._advancing.add(campaign_id)
        try:
            await self._advance(campaign_id)
        except Exception:  # noqa: BLE001 - a job must never take the server down
            logger.exception("job %s could not advance", campaign_id)
        finally:
            self._advancing.discard(campaign_id)

    async def _advance(self, campaign_id: str) -> None:
        """Start the next step that will actually start.

        A loop rather than a recursive call: a module that refuses to start has to be skipped
        and the one after it tried, and `advance` is guarded against re-entry — so recursing
        would hit its own guard and silently wedge the job at the first bad step.
        """
        from . import agent_bridge

        while True:
            campaign = campaigns.get(campaign_id)
            if campaign is None or campaign["status"] not in ("running", "reviewing", "paused"):
                return

            step = campaigns.next_pending(campaign)
            if step is None:
                await self._complete(campaign)
                return

            busy = await self._wait_for_target(step["package"])
            if busy:
                updated = campaigns.set_status(
                    campaign_id, "paused",
                    blocked={"reason": busy, "module": step["module"]})
                await self._broadcast(updated or campaign)
                await self._raise_with_user(
                    campaign, "A job is stuck on a busy device",
                    f"The {campaign.get('kind', 'job')} paused before "
                    f"{step.get('role')}/{step['module']}: {busy}. Say 'resume' once it is "
                    f"free.")
                return

            index = campaign["steps"].index(step) + 1
            brief = _step_brief(campaign, step, index, len(campaign["steps"]),
                                campaigns.last_finished_step(campaign))
            try:
                agent_bridge.start_run(step["package"], step["module"], brief, watch=True)
            except Exception as exc:  # noqa: BLE001 - RunRefused and anything else read alike
                # Marked running first because that is the state `finish_step` acts on, and a
                # step that never started still has to *end* — left pending it would be picked
                # up on the next pass and fail identically, forever.
                campaigns.start_step(campaign_id, step["module"], step["package"])
                updated = campaigns.finish_step(campaign_id, step["module"], "failed",
                                                package=step["package"],
                                                note=f"could not start: {exc}")
                await self._broadcast(updated or campaign)
                await self._write(campaign, f"`{step['module']}` would not start: {exc} — "
                                            f"moving on to the next step.")
                continue

            updated = campaigns.start_step(campaign_id, step["module"], step["package"])
            if updated:
                await self._broadcast(updated)
            logger.info("job %s: started %s/%s (%d/%d)", campaign_id, step["package"],
                        step["module"], index, len(campaign["steps"]))
            return

    async def _complete(self, campaign: dict[str, Any]) -> None:
        updated = campaigns.set_status(campaign["id"], "done") or campaign
        await self._broadcast(updated)
        counts = campaigns.progress(updated)
        lines = [f"The {updated.get('kind', 'job')} is finished — {counts['finished']} of "
                 f"{counts['total']} steps, {counts['findings']} findings filed"
                 + (f", {counts['failed']} failed" if counts["failed"] else "") + ".",
                 ""]
        for step in updated["steps"]:
            lines.append(f"- {step.get('role') or step['package']}/{step['module']} — "
                         f"{step['status']}"
                         + (f", {step['findings']} findings" if step.get("findings") else "")
                         + (f" ({step['note']})" if step.get("note") else ""))
        lines += ["", "Read what they filed and tell the user where this stands: what is "
                      "broken, what is newly confirmed, and which of it is one defect rather "
                      "than several. Cluster the duplicates while you are in it. Then clear "
                      "any scratchpad notes that were only for this job."]
        await self._hand_turn(updated, "\n".join(lines))

    # -- listening ---------------------------------------------------------------------------
    async def notice(self, event: dict[str, Any]) -> None:
        """Every event from every session passes through here. Cheap on the common path."""
        kind = str(event.get("type") or "")
        if kind not in _ENDINGS and kind not in ("agent_blocked", "agent_unblocked"):
            return
        package, slug = str(event.get("package") or ""), str(event.get("slug") or "")
        if not package or not slug:
            return

        # The manager finishing a turn is what releases the next step. Checked first: the
        # supervisor project can itself be in an ecosystem, and this is not a step ending.
        if await self._manager_turn_ended(package, slug, kind):
            return

        campaign = campaigns.active_for(package)
        if campaign is None:
            return
        step = campaigns.running_step(campaign)
        if step is None or step["module"] != slug or step["package"] != package:
            return

        if kind == "agent_blocked":
            question = str(event.get("question") or "")
            updated = campaigns.set_status(campaign["id"], "paused", blocked={
                "reason": "waiting for you", "module": slug, "question": question[:400]})
            await self._broadcast(updated or campaign)
            await self._raise_with_user(
                campaign, f"{step.get('role')}/{slug} needs you",
                f"{question[:300]} — answer it in that module's own chat and the job carries "
                f"on by itself.")
            return

        if kind == "agent_unblocked":
            updated = campaigns.set_status(campaign["id"], "running")
            await self._broadcast(updated or campaign)
            return

        await self._step_ended(campaign, step, kind, event)

    async def _step_ended(self, campaign: dict[str, Any], step: dict[str, Any], kind: str,
                          event: dict[str, Any]) -> None:
        outcome = _ENDINGS[kind]
        note = ""
        if kind == "agent_parked":
            note = "the subscription window ran out mid-run"
        elif kind == "agent_error":
            note = str(event.get("message") or "")[:300]

        # The last thing the module said is what the next step is handed. Read from the
        # transcript rather than the event, which carries counts and not words.
        reported = await asyncio.to_thread(_last_agent_text, step["package"], step["module"])
        if reported:
            campaigns.record_report(campaign["id"], step["module"], reported)

        findings = int(event.get("findings") or 0)
        updated = campaigns.finish_step(campaign["id"], step["module"], outcome,
                                        package=step["package"], findings=findings, note=note)
        campaign = updated or campaign

        if kind == "agent_parked":
            paused = campaigns.set_status(campaign["id"], "paused", blocked={
                "reason": "the subscription window ran out", "module": step["module"]})
            await self._broadcast(paused or campaign)
            await self._write(campaign,
                              "Paused: the subscription window is exhausted, not anything "
                              "wrong with the app. Say 'resume' once it resets.")
            return

        reviewing = campaigns.set_status(campaign["id"], "reviewing") or campaign
        await self._broadcast(reviewing)
        brief = _review_brief(reviewing, step, outcome, findings, note)
        asked = await self._hand_turn(reviewing, brief)
        campaigns.set_review_asked(campaign["id"], asked)
        if not asked:
            # The manager was mid-sentence about the previous step. Retried on a clock rather
            # than on its next turn ending: it is idle *because* nobody is talking to it, so
            # waiting for a turn it has no reason to take is waiting forever.
            logger.info("job %s: manager busy, will offer the review again", campaign["id"])
            asyncio.create_task(self._retry_review(campaign["id"], brief))

    async def _retry_review(self, campaign_id: str, brief: str) -> None:
        """Keep offering the manager its review turn until it takes one, then give up loudly.

        Giving up is a pause, not a silent stall: a job that stopped because nobody could be
        told about it must say so on the board, or it looks exactly like one still running.
        """
        for _ in range(REVIEW_RETRY_LIMIT):
            await asyncio.sleep(REVIEW_RETRY_SECONDS)
            campaign = campaigns.get(campaign_id)
            if campaign is None or campaign["status"] != "reviewing":
                return                      # someone else moved it on
            if campaign.get("review_asked"):
                return
            if await self._hand_turn(campaign, brief):
                campaigns.set_review_asked(campaign_id, True)
                return

        campaign = campaigns.get(campaign_id)
        if campaign is None or campaign["status"] != "reviewing":
            return
        paused = campaigns.set_status(campaign_id, "paused", blocked={
            "reason": "the manager never became free to review the last step",
            "module": (campaigns.last_finished_step(campaign) or {}).get("module")})
        await self._broadcast(paused or campaign)
        await self._raise_with_user(
            campaign, "A job is waiting on the manager",
            "The last step finished but the manager has been busy ever since, so nothing has "
            "read it and the next step has not started. Say 'resume' in the manager chat.")

    async def _manager_turn_ended(self, package: str, slug: str, kind: str) -> bool:
        """If this was the manager finishing a turn, move every job that was waiting on it."""
        import ecosystem as ecosystem_mod

        name = ecosystem_mod.supervises(package)
        if not name or kind not in _ENDINGS:
            return False

        for campaign in campaigns.list_all(name, status="reviewing"):
            if campaign.get("review_asked"):
                campaigns.set_status(campaign["id"], "running")
                await self.advance(campaign["id"])
            else:
                step = campaigns.last_finished_step(campaign)
                if step is None:
                    continue
                asked = await self._hand_turn(
                    campaign, _review_brief(campaign, step, step["status"],
                                            int(step.get("findings") or 0),
                                            str(step.get("note") or "")))
                campaigns.set_review_asked(campaign["id"], asked)
        return True


def _last_agent_text(package: str, slug: str) -> str:
    """The module's closing words — what it says it established.

    The last agent-authored block in its transcript. Handed verbatim to the next step, which is
    why it is worth taking the real text rather than a summary: "booked for Testina Doe,
    Tuesday 14:30, ref #4471" is the whole value of the handoff, and any paraphrase loses
    exactly the parts the next step has to match on.
    """
    try:
        messages = agent_store.read_chat(package, slug, limit=60)
    except Exception:  # noqa: BLE001 - a missing transcript is not a failure
        return ""
    for entry in reversed(messages):
        if entry.get("role") == "agent" and str(entry.get("text") or "").strip():
            return str(entry["text"])[:2000]
    return ""


runner = CampaignRunner()
