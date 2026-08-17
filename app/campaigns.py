"""A whole app, tested module by module, without anyone typing "next" thirteen times.

The manager could already start a run. What it could not do was *notice one had finished*.
Its session only wakes when something is said to it, so a run it commissioned ended in
silence: the module stopped, the target went quiet, and the campaign sat there until a human
noticed and asked. Thirteen modules meant thirteen prompts to a supervisor whose entire job
is not needing them.

So sequencing lives here, in plain code, rather than in the manager's head. A campaign is an
ordered list of modules for one app, and something outside the conversation walks it.

**Why not just let the manager drive each step?** Because a conversation is the wrong thing to
hang a queue on. It stalls on a rate limit, it can lose the thread across a context boundary,
and it costs a turn per module to do bookkeeping a list does better. The manager is not
demoted by this — it decides *what* the campaign is, it is told what each module found, and it
gets a real turn whenever judgement is actually needed. It is relieved of remembering.

**Where it deliberately stops.** A step that fails, parks on a rate limit, or blocks asking the
user a question pauses the campaign and hands the manager a turn. Those are the moments where
carrying on regardless would turn one bad answer into twelve.

**One campaign per app, never two.** The app is the target and one target has one driver
(`device_locks`), so a second campaign on the same app could only ever queue behind the first
while looking like progress.

Stored beside `clusters.json` and `retests.json` in `projects/`, atomically, for the reasons
written there.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import project_paths

logger = logging.getLogger("campaigns")

SCHEMA_VERSION = 1

#: `running` is walking the list. `paused` stopped at something needing a decision and is
#: resumable. `stopped` was ended deliberately. `done` walked every step.
STATUSES = ("running", "paused", "done", "stopped")

#: A step is `skipped` only when a human or the manager said so — never because it looked
#: uninteresting. `failed` means the run ended badly, which is a result, not an absence.
STEP_STATUSES = ("pending", "running", "done", "failed", "skipped")

_LOCK = threading.RLock()


def _path() -> Path:
    return project_paths.DEFAULT_PROJECTS_DIR / "campaigns.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load() -> dict[str, Any]:
    path = _path()
    if not path.is_file():
        return {"schema": SCHEMA_VERSION, "campaigns": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("could not read campaigns: %s", exc)
        return {"schema": SCHEMA_VERSION, "campaigns": {}}
    if not isinstance(data, dict):
        return {"schema": SCHEMA_VERSION, "campaigns": {}}
    data.setdefault("schema", SCHEMA_VERSION)
    data.setdefault("campaigns", {})
    return data


def _save(data: dict[str, Any]) -> None:
    path = _path()
    data["updated_at"] = _now()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".json.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("could not write campaigns: %s", exc)


# -- reading ---------------------------------------------------------------------------------

def get(campaign_id: str) -> Optional[dict[str, Any]]:
    found = _load()["campaigns"].get(campaign_id)
    return dict(found) if found else None


def list_all(ecosystem: Optional[str] = None,
             status: Optional[str] = None) -> list[dict[str, Any]]:
    rows = [dict(c) for c in _load()["campaigns"].values()]
    if ecosystem:
        rows = [c for c in rows if c.get("ecosystem") == ecosystem]
    if status:
        rows = [c for c in rows if c.get("status") == status]
    return sorted(rows, key=lambda c: str(c.get("created_at") or ""), reverse=True)


def live(ecosystem: Optional[str] = None) -> list[dict[str, Any]]:
    """Campaigns that are still going or waiting on a decision."""
    return [c for c in list_all(ecosystem) if c.get("status") in ("running", "paused")]


def active_for(package: str) -> Optional[dict[str, Any]]:
    """The live campaign on this app, if any. One target, one driver."""
    return next((c for c in live() if c.get("package") == package), None)


def running_step(campaign: dict[str, Any]) -> Optional[dict[str, Any]]:
    return next((s for s in campaign.get("steps", []) if s.get("status") == "running"), None)


def next_pending(campaign: dict[str, Any]) -> Optional[dict[str, Any]]:
    return next((s for s in campaign.get("steps", []) if s.get("status") == "pending"), None)


def progress(campaign: dict[str, Any]) -> dict[str, Any]:
    """Counts for the indicator: how far along, and what it is on right now."""
    steps = campaign.get("steps", [])
    finished = [s for s in steps if s.get("status") in ("done", "failed", "skipped")]
    current = running_step(campaign)
    return {
        "total": len(steps),
        "finished": len(finished),
        "failed": sum(1 for s in steps if s.get("status") == "failed"),
        "skipped": sum(1 for s in steps if s.get("status") == "skipped"),
        "current": current.get("module") if current else None,
        "findings": sum(int(s.get("findings") or 0) for s in steps),
    }


def summary(ecosystem: str) -> dict[str, Any]:
    """What the board shows without opening anything."""
    rows = live(ecosystem)
    return {
        "live": len(rows),
        "campaigns": [{"id": c["id"], "role": c.get("role") or c["package"],
                       "package": c["package"], "status": c["status"],
                       "goal": c.get("goal", ""), "blocked": c.get("blocked"),
                       **progress(c)} for c in rows],
    }


# -- writing ---------------------------------------------------------------------------------

def create(ecosystem: str, package: str, modules: list[dict[str, str]], *,
           role: str = "", goal: str = "", instruction: str = "") -> dict[str, Any]:
    """Plan a campaign over `modules`, in the order given. Raises if one is already live.

    `modules` are `{"slug", "title", "scope"}` — the scope is carried so each step's brief can
    say what that module is *for* without the runner having to re-read the project mid-walk.
    """
    with _LOCK:
        existing = active_for(package)
        if existing is not None:
            raise ValueError(
                f"{role or package} already has a campaign running ({existing['id']}), "
                f"{progress(existing)['finished']}/{len(existing['steps'])} done. Stop it first "
                f"if you want to start a different one.")
        if not modules:
            raise ValueError("A campaign needs at least one module.")

        # Second-resolution alone is not unique: stopping a sweep and starting a replacement
        # is a two-second operation, and a colliding key does not fail — it silently
        # *overwrites* the stopped campaign's record with the new one, destroying the history
        # of what just ran. The suffix costs nothing and removes the whole question.
        data = _load()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        campaign_id = f"{package}@{stamp}"
        bump = 1
        while campaign_id in data["campaigns"]:
            bump += 1
            campaign_id = f"{package}@{stamp}-{bump}"
        campaign = {
            "id": campaign_id,
            "ecosystem": ecosystem,
            "package": package,
            "role": role,
            "goal": goal,
            "instruction": instruction,
            "status": "running",
            "blocked": None,
            "created_at": _now(),
            "updated_at": _now(),
            "steps": [{"module": m["slug"], "title": m.get("title") or m["slug"],
                       "scope": m.get("scope", ""), "status": "pending",
                       "started_at": None, "finished_at": None,
                       "findings": 0, "note": ""} for m in modules],
        }
        data["campaigns"][campaign_id] = campaign
        _save(data)
        return dict(campaign)


def _mutate(campaign_id: str, fn) -> Optional[dict[str, Any]]:
    with _LOCK:
        data = _load()
        campaign = data["campaigns"].get(campaign_id)
        if campaign is None:
            return None
        fn(campaign)
        campaign["updated_at"] = _now()
        _save(data)
        return dict(campaign)


def start_step(campaign_id: str, module: str) -> Optional[dict[str, Any]]:
    def go(campaign: dict[str, Any]) -> None:
        for step in campaign["steps"]:
            if step["module"] == module:
                step["status"] = "running"
                step["started_at"] = _now()
        campaign["status"] = "running"
        campaign["blocked"] = None
    return _mutate(campaign_id, go)


def finish_step(campaign_id: str, module: str, status: str, *,
                findings: int = 0, note: str = "") -> Optional[dict[str, Any]]:
    if status not in STEP_STATUSES:
        raise ValueError(f"unknown step status {status!r}")

    def go(campaign: dict[str, Any]) -> None:
        for step in campaign["steps"]:
            if step["module"] == module and step["status"] == "running":
                step["status"] = status
                step["finished_at"] = _now()
                step["findings"] = findings
                step["note"] = note
    return _mutate(campaign_id, go)


def set_status(campaign_id: str, status: str, *,
               blocked: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    if status not in STATUSES:
        raise ValueError(f"unknown campaign status {status!r}")

    def go(campaign: dict[str, Any]) -> None:
        campaign["status"] = status
        campaign["blocked"] = blocked
        if status in ("done", "stopped"):
            # A campaign that ends with a step still marked `running` would read forever as
            # "currently testing X" on the board, long after nothing is testing anything.
            for step in campaign["steps"]:
                if step["status"] == "running":
                    step["status"] = "failed"
                    step["finished_at"] = _now()
                    step["note"] = step["note"] or f"campaign {status} while this was running"
    return _mutate(campaign_id, go)


def skip_step(campaign_id: str, module: str, note: str = "") -> Optional[dict[str, Any]]:
    def go(campaign: dict[str, Any]) -> None:
        for step in campaign["steps"]:
            if step["module"] == module and step["status"] == "pending":
                step["status"] = "skipped"
                step["finished_at"] = _now()
                step["note"] = note
    return _mutate(campaign_id, go)


def forget(campaign_id: str) -> bool:
    with _LOCK:
        data = _load()
        if campaign_id not in data["campaigns"]:
            return False
        data["campaigns"].pop(campaign_id)
        _save(data)
        return True


def reset_orphans() -> list[dict[str, Any]]:
    """Mark live campaigns paused at startup, and say which.

    A campaign's steps run in this process. After a restart nothing is driving them, but the
    file still says `running` — and a board that reports a module under test when no session
    exists is worse than one that reports nothing. Paused rather than stopped: the work is
    still wanted, it just needs someone to say go.
    """
    touched = []
    for campaign in live():
        if campaign["status"] != "running" and running_step(campaign) is None:
            continue
        updated = set_status(campaign["id"], "paused", blocked={
            "reason": "the server restarted while this was running",
            "module": (running_step(campaign) or {}).get("module")})
        if updated:
            touched.append(updated)
    return touched
