"""What walks a campaign: start a module, wait for it to end, start the next one.

The manager's session only wakes when something is said to it. That is the whole reason this
file exists — a run it commissioned finished in silence, and the campaign sat there until a
human noticed and typed "check". Here, the thing that notices is the event stream every run
already emits.

**How it hears.** `agent_bridge._emit` sees every event from every session, stamped with its
package and slug. `notice()` is handed each one and cares about four:

    agent_done      the step finished     -> record findings, start the next module
    agent_error     the step broke        -> record it, pause, hand the manager a turn
    agent_parked    the window ran out    -> pause; nothing is wrong except the clock
    agent_blocked   it needs the user     -> pause and say so loudly, on the board

**Why it waits before starting the next one.** `agent_done` is emitted inside the turn, and
the target's lock is not released until the turn unwinds a moment later. Starting immediately
would be refused by `device_locks` for a target that is about to be free — so the runner polls
for the lock to actually clear, which also handles the case where a person took the device
between two steps.

**What it tells the manager, and when.** Every step ends with a line in the manager's
transcript: which module, what it filed, how long. That is a write, not a turn — the manager
reads it next time it speaks, and the user reads it immediately. A real turn is spent only
where judgement is needed: a step that failed, a step waiting on the user, and the end of the
campaign. "Only ask when it absolutely needs to" is implemented here, not requested in a
prompt.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import campaigns
import device_locks
from agent import store as agent_store

logger = logging.getLogger("server.campaigns")

#: How long to wait for the finished step's target to actually come free before giving up.
#: Generous: the release happens milliseconds after `agent_done`, so anything approaching this
#: means somebody else took the device, which is a pause rather than a failure.
TARGET_WAIT_SECONDS = 90.0

#: Events that end a step, mapped to what they mean for it.
_ENDINGS = {"agent_done": "done", "agent_error": "failed", "agent_parked": "failed"}


def _module_brief(campaign: dict[str, Any], step: dict[str, Any], index: int,
                  total: int) -> str:
    """The instruction one step is started with.

    It says where the module sits in a plan on purpose. A tester told only "test search" will
    happily spend forty turns on search; told it is 4 of 13 in a sweep of the whole app, it
    covers its own area and stops, which is the difference between a campaign finishing and a
    campaign timing out on module four.
    """
    lines = [
        f"You are step {index} of {total} in a sweep of this whole app"
        + (f" — {campaign['goal']}" if campaign.get("goal") else "") + ".",
        "",
        f"Test **{step['title']}** and nothing else. Other modules cover the other areas and "
        f"will run after you; work that belongs to them is theirs, not yours.",
    ]
    if step.get("scope"):
        lines += ["", f"This module's scope: {step['scope']}"]
    if campaign.get("instruction"):
        lines += ["", campaign["instruction"]]
    lines += [
        "",
        "Record what you actually observe — a finding per case, pass or fail, with the "
        "screenshot behind it. When you have covered this module's scope, stop and say what "
        "you established. Do not move on to another part of the app.",
        "",
        "If something blocks you that only a human can clear — a credential you do not have, "
        "a payment, a destructive action — ask. Everything else, decide and carry on: nobody "
        "is watching this run step by step.",
    ]
    return "\n".join(lines)


class CampaignRunner:
    """Walks campaigns. One instance, held by `agent_bridge`."""

    def __init__(self) -> None:
        #: Campaigns with an advance in flight, so two events for one step cannot start the
        #: next module twice. `agent_done` and `agent_busy:false` arrive back to back.
        self._advancing: set[str] = set()

    # -- talking to the manager ------------------------------------------------------------
    async def _tell(self, campaign: dict[str, Any], text: str, *, turn: bool = False) -> None:
        """Put something in the manager's transcript, optionally as a real turn.

        A write is free and immediate; a turn costs the subscription window and can be refused
        if the manager is mid-sentence. So the default is a write, and `turn=True` is reserved
        for the moments this whole design exists to surface.
        """
        import ecosystem as ecosystem_mod

        from . import agent_bridge

        supervisor = ecosystem_mod.supervisor(campaign["ecosystem"])
        if not supervisor:
            return
        slug = agent_store.main_slug(supervisor)

        if turn:
            try:
                agent_bridge.start_run(supervisor, slug, text)
                return
            except Exception as exc:  # noqa: BLE001 - a busy manager is not a campaign failure
                logger.info("could not hand the manager a turn (%s) — writing instead", exc)

        await asyncio.to_thread(agent_store.append_chat, supervisor, slug,
                                {"role": "notice", "text": text})
        await agent_bridge.emit({"type": "agent_notice", "package": supervisor, "slug": slug,
                                 "text": text})

    async def _broadcast(self, campaign: dict[str, Any]) -> None:
        """Push the campaign's shape to every open board, so the indicator is live."""
        from . import agent_bridge

        await agent_bridge.emit({
            "type": "agent_campaign", "package": campaign["package"],
            "ecosystem": campaign["ecosystem"], "campaign": campaign["id"],
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
        """Start the next pending module, or finish the campaign."""
        if campaign_id in self._advancing:
            return
        self._advancing.add(campaign_id)
        try:
            await self._advance(campaign_id)
        except Exception:  # noqa: BLE001 - a campaign must never take the server down
            logger.exception("campaign %s could not advance", campaign_id)
        finally:
            self._advancing.discard(campaign_id)

    async def _advance(self, campaign_id: str) -> None:
        """Start the next module that will actually start.

        A loop rather than a recursive call: a module that refuses to start has to be skipped
        and the one after it tried, and `advance` is guarded against re-entry — so recursing
        would hit its own guard and silently wedge the sweep at the first bad module. That is
        exactly the bug this loop replaced.
        """
        from . import agent_bridge

        while True:
            campaign = campaigns.get(campaign_id)
            if campaign is None or campaign["status"] not in ("running", "paused"):
                return

            step = campaigns.next_pending(campaign)
            if step is None:
                await self._complete(campaign)
                return

            busy = await self._wait_for_target(campaign["package"])
            if busy:
                updated = campaigns.set_status(
                    campaign_id, "paused",
                    blocked={"reason": busy, "module": step["module"]})
                await self._broadcast(updated or campaign)
                await self._tell(campaign,
                                 f"The sweep of **{campaign.get('role') or campaign['package']}"
                                 f"** paused before `{step['module']}`: {busy}. Say 'resume' "
                                 f"once the target is free.", turn=True)
                return

            index = campaign["steps"].index(step) + 1
            brief = _module_brief(campaign, step, index, len(campaign["steps"]))
            try:
                agent_bridge.start_run(campaign["package"], step["module"], brief, watch=True)
            except Exception as exc:  # noqa: BLE001 - RunRefused and anything else read alike
                # Marked running first because that is the state `finish_step` acts on, and a
                # step that never started still has to *end* — left pending it would be picked
                # up on the next pass and fail identically, forever.
                campaigns.start_step(campaign_id, step["module"])
                updated = campaigns.finish_step(campaign_id, step["module"], "failed",
                                                note=f"could not start: {exc}")
                await self._broadcast(updated or campaign)
                await self._tell(campaign,
                                 f"`{step['module']}` would not start: {exc} — moving on to "
                                 f"the next module.")
                continue

            updated = campaigns.start_step(campaign_id, step["module"])
            if updated:
                await self._broadcast(updated)
            logger.info("campaign %s: started %s (%d/%d)", campaign_id, step["module"], index,
                        len(campaign["steps"]))
            return

    async def _complete(self, campaign: dict[str, Any]) -> None:
        updated = campaigns.set_status(campaign["id"], "done") or campaign
        await self._broadcast(updated)
        counts = campaigns.progress(updated)
        lines = [f"The sweep of **{updated.get('role') or updated['package']}** is finished — "
                 f"{counts['finished']} of {counts['total']} modules, {counts['findings']} "
                 f"findings filed"
                 + (f", {counts['failed']} module(s) failed" if counts["failed"] else "") + ".",
                 ""]
        for step in updated["steps"]:
            lines.append(f"- `{step['module']}` — {step['status']}"
                         + (f", {step['findings']} findings" if step.get("findings") else "")
                         + (f" ({step['note']})" if step.get("note") else ""))
        lines += ["", "Read what they filed and tell the user where the app stands: what is "
                      "broken, what is newly confirmed, and which of it is one defect rather "
                      "than several. Cluster the duplicates while you are in it."]
        await self._tell(updated, "\n".join(lines), turn=True)

    # -- listening ---------------------------------------------------------------------------
    async def notice(self, event: dict[str, Any]) -> None:
        """Every event from every session passes through here. Cheap on the common path."""
        kind = str(event.get("type") or "")
        if kind not in _ENDINGS and kind not in ("agent_blocked", "agent_unblocked"):
            return
        package, slug = str(event.get("package") or ""), str(event.get("slug") or "")
        if not package or not slug:
            return

        campaign = campaigns.active_for(package)
        if campaign is None:
            return
        step = campaigns.running_step(campaign)
        if step is None or step["module"] != slug:
            return

        if kind == "agent_blocked":
            question = str(event.get("question") or "")
            updated = campaigns.set_status(campaign["id"], "paused", blocked={
                "reason": "waiting for you", "module": slug, "question": question[:400]})
            await self._broadcast(updated or campaign)
            await self._tell(campaign,
                             f"`{slug}` has stopped to ask the user something and the campaign "
                             f"is paused until it is answered:\n\n> {question[:400]}\n\n"
                             f"Answer it in that module's own chat. The sweep continues by "
                             f"itself once it does.", turn=True)
            return

        if kind == "agent_unblocked":
            # Answered — the run is still going, so this is not a step ending.
            updated = campaigns.set_status(campaign["id"], "running")
            await self._broadcast(updated or campaign)
            return

        outcome = _ENDINGS[kind]
        note = ""
        if kind == "agent_parked":
            note = "the subscription window ran out mid-run"
        elif kind == "agent_error":
            note = str(event.get("message") or "")[:300]

        updated = campaigns.finish_step(campaign["id"], slug, outcome,
                                        findings=int(event.get("findings") or 0), note=note)
        campaign = updated or campaign
        await self._broadcast(campaign)

        counts = campaigns.progress(campaign)
        headline = (f"`{slug}` finished — {event.get('findings') or 0} findings"
                    if outcome == "done" else f"`{slug}` ended badly: {note or outcome}")
        await self._tell(campaign,
                         f"{headline}. That is {counts['finished']} of {counts['total']} "
                         f"modules on {campaign.get('role') or campaign['package']}.")

        if kind == "agent_parked":
            paused = campaigns.set_status(campaign["id"], "paused", blocked={
                "reason": "the subscription window ran out", "module": slug})
            await self._broadcast(paused or campaign)
            await self._tell(campaign,
                             "The campaign is paused because the subscription window is "
                             "exhausted, not because anything is wrong with the app. Say "
                             "'resume' once it resets.")
            return

        await self.advance(campaign["id"])


runner = CampaignRunner()
