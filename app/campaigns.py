"""A job made of steps, each one a module in some app, walked in order without being nagged.

Two shapes, one object, because they only differ in whether the steps stay in one room:

* a **sweep** — every module of one app, in order. "Test the clinic web."
* a **journey** — steps across *different* apps that only mean anything together. "Book on the
  patient app, then check it reached the iPad." Neither half is a test on its own: the Android
  step proves nothing without the iPad step, and the iPad step cannot even know what to look
  for without what the Android step wrote down.

Making them one thing is not tidiness. A journey is a sweep whose steps stopped agreeing about
which app they are in, and every hard part — ordering, pausing, what happens when one fails,
what the board shows — is identical. Two implementations would have drifted within a week.

**The manager is between every step.** It is handed a turn when each one ends, with what that
module filed and what it wrote to the shared scratchpad, and the next step starts when that
turn finishes. That costs a turn per step and buys the thing the whole three-tier design is
for: the iPad step's brief can say "look for ref #4471", because something read the Android
step's note before writing it.

**One live job per app.** An app is a target and a target has one driver (`device_locks`), so a
second job touching it could only queue behind the first while looking like progress. A journey
therefore locks every app it names, for its whole length.

Stored beside `clusters.json`, `retests.json` and `scratchpad.json` in `projects/`, atomically.
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

#: v2 moved the app from the campaign onto each step, which is what let a job cross apps.
SCHEMA_VERSION = 2

#: `reviewing` is the manager reading what the last step did before the next one starts. It is
#: a real state and not a detail: a job sitting in it is *waiting on a turn*, which is a
#: different thing from running and a different thing from paused, and the board says so.
STATUSES = ("running", "reviewing", "paused", "done", "stopped")

#: A step is `skipped` only when a human or the manager said so — never because it looked
#: uninteresting. `failed` means the run ended badly, which is a result, not an absence.
STEP_STATUSES = ("pending", "running", "done", "failed", "skipped")

KINDS = ("sweep", "journey")

_LOCK = threading.RLock()


def _path() -> Path:
    return project_paths.DEFAULT_PROJECTS_DIR / "campaigns.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _migrate(campaign: dict[str, Any]) -> dict[str, Any]:
    """Bring a v1 record forward: one app on the campaign, none on its steps."""
    package = campaign.get("package")
    for step in campaign.get("steps", []):
        step.setdefault("package", package)
        step.setdefault("role", campaign.get("role", ""))
        step.setdefault("expect", "")
    campaign.setdefault("kind", "sweep")
    return campaign


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
    data.setdefault("campaigns", {})
    if int(data.get("schema") or 1) < SCHEMA_VERSION:
        for campaign in data["campaigns"].values():
            _migrate(campaign)
        data["schema"] = SCHEMA_VERSION
    return data


def _save(data: dict[str, Any]) -> None:
    path = _path()
    data["schema"] = SCHEMA_VERSION
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
    """Jobs that are still going, mid-review, or waiting on a decision."""
    return [c for c in list_all(ecosystem)
            if c.get("status") in ("running", "reviewing", "paused")]


def apps(campaign: dict[str, Any]) -> list[str]:
    """Every app this job touches, in the order it first touches them."""
    seen: list[str] = []
    for step in campaign.get("steps", []):
        package = str(step.get("package") or "")
        if package and package not in seen:
            seen.append(package)
    return seen


def active_for(package: str) -> Optional[dict[str, Any]]:
    """The live job holding this app, if any — including a journey that only visits it later.

    Reserved for the whole job rather than only while its step runs: a journey that gave up its
    app between steps could find a sweep had taken it, and would then fail halfway through with
    the first half already done and unrepeatable.
    """
    return next((c for c in live() if package in apps(c)), None)


def running_step(campaign: dict[str, Any]) -> Optional[dict[str, Any]]:
    return next((s for s in campaign.get("steps", []) if s.get("status") == "running"), None)


def last_finished_step(campaign: dict[str, Any]) -> Optional[dict[str, Any]]:
    done = [s for s in campaign.get("steps", [])
            if s.get("status") in ("done", "failed", "skipped")]
    return done[-1] if done else None


def next_pending(campaign: dict[str, Any]) -> Optional[dict[str, Any]]:
    return next((s for s in campaign.get("steps", []) if s.get("status") == "pending"), None)


def progress(campaign: dict[str, Any]) -> dict[str, Any]:
    """Counts for the indicator: how far along, and what it is on right now."""
    steps = campaign.get("steps", [])
    finished = [s for s in steps if s.get("status") in ("done", "failed", "skipped")]
    current = running_step(campaign) or next_pending(campaign)
    return {
        "total": len(steps),
        "finished": len(finished),
        "failed": sum(1 for s in steps if s.get("status") == "failed"),
        "skipped": sum(1 for s in steps if s.get("status") == "skipped"),
        "current": current.get("module") if current else None,
        "current_role": current.get("role") if current else None,
        "findings": sum(int(s.get("findings") or 0) for s in steps),
        "apps": apps(campaign),
    }


def summary(ecosystem: str) -> dict[str, Any]:
    """What the board shows without opening anything."""
    rows = live(ecosystem)
    return {
        "live": len(rows),
        "campaigns": [{"id": c["id"], "kind": c.get("kind", "sweep"),
                       "role": c.get("role") or ", ".join(apps(c)),
                       "status": c["status"], "goal": c.get("goal", ""),
                       "blocked": c.get("blocked"), **progress(c)} for c in rows],
    }


# -- writing ---------------------------------------------------------------------------------

def create(ecosystem: str, steps: list[dict[str, Any]], *, kind: str = "sweep",
           role: str = "", goal: str = "", instruction: str = "") -> dict[str, Any]:
    """Plan a job over `steps`, in the order given.

    Each step is `{package, role, module, title, scope, expect}`. `expect` is what that step
    is supposed to establish for the *next* one — the reason a journey works at all.

    Raises if any app it names is already held by a live job.
    """
    if not steps:
        raise ValueError("A job needs at least one step.")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")

    with _LOCK:
        wanted = []
        for step in steps:
            package = str(step.get("package") or "")
            if package and package not in wanted:
                wanted.append(package)
        for package in wanted:
            existing = active_for(package)
            if existing is not None:
                counts = progress(existing)
                raise ValueError(
                    f"{step.get('role') or package} is already held by a job "
                    f"({existing['id']}, {counts['finished']}/{counts['total']} done). Stop "
                    f"that one first if you want to start a different one.")

        data = _load()
        # Second-resolution alone is not unique: stopping a job and starting a replacement is a
        # two-second operation, and a colliding key does not fail — it silently *overwrites*
        # the stopped record, destroying the history of what just ran.
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        base = wanted[0] if wanted else ecosystem
        campaign_id = f"{base}@{stamp}"
        bump = 1
        while campaign_id in data["campaigns"]:
            bump += 1
            campaign_id = f"{base}@{stamp}-{bump}"

        campaign = {
            "id": campaign_id,
            "ecosystem": ecosystem,
            "kind": kind,
            "role": role,
            "goal": goal,
            "instruction": instruction,
            "status": "running",
            "blocked": None,
            "review_asked": False,
            "created_at": _now(),
            "updated_at": _now(),
            "steps": [{"package": str(s.get("package") or ""),
                       "role": str(s.get("role") or ""),
                       "module": str(s.get("module") or ""),
                       "title": str(s.get("title") or s.get("module") or ""),
                       "scope": str(s.get("scope") or ""),
                       "expect": str(s.get("expect") or ""),
                       "status": "pending", "started_at": None, "finished_at": None,
                       "findings": 0, "note": "", "reported": ""} for s in steps],
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


def start_step(campaign_id: str, module: str,
               package: Optional[str] = None) -> Optional[dict[str, Any]]:
    def go(campaign: dict[str, Any]) -> None:
        for step in campaign["steps"]:
            if step["module"] == module and (package is None or step["package"] == package):
                if step["status"] in ("pending", "failed"):
                    step["status"] = "running"
                    step["started_at"] = _now()
                    step["finished_at"] = None
                    break
        campaign["status"] = "running"
        campaign["blocked"] = None
        campaign["review_asked"] = False
    return _mutate(campaign_id, go)


def finish_step(campaign_id: str, module: str, status: str, *, package: Optional[str] = None,
                findings: int = 0, note: str = "") -> Optional[dict[str, Any]]:
    if status not in STEP_STATUSES:
        raise ValueError(f"unknown step status {status!r}")

    def go(campaign: dict[str, Any]) -> None:
        for step in campaign["steps"]:
            if (step["module"] == module and step["status"] == "running"
                    and (package is None or step["package"] == package)):
                step["status"] = status
                step["finished_at"] = _now()
                step["findings"] = findings
                step["note"] = note
                break
    return _mutate(campaign_id, go)


def set_status(campaign_id: str, status: str, *,
               blocked: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")

    def go(campaign: dict[str, Any]) -> None:
        campaign["status"] = status
        campaign["blocked"] = blocked
        if status != "reviewing":
            campaign["review_asked"] = False
        if status in ("done", "stopped"):
            # A job that ends with a step still marked `running` would read forever as
            # "currently testing X" on the board, long after nothing is testing anything.
            for step in campaign["steps"]:
                if step["status"] == "running":
                    step["status"] = "failed"
                    step["finished_at"] = _now()
                    step["note"] = step["note"] or f"job {status} while this was running"
    return _mutate(campaign_id, go)


def set_review_asked(campaign_id: str, asked: bool) -> Optional[dict[str, Any]]:
    """Whether the manager has actually been handed its turn for the step that just ended.

    Two jobs can end a step at once and there is one manager, so the second's turn is refused.
    Without this flag that job would sit in `reviewing` forever waiting for a turn nobody ever
    gave it — the flag is what lets the next idle moment notice and hand it over.
    """
    def go(campaign: dict[str, Any]) -> None:
        campaign["review_asked"] = bool(asked)
    return _mutate(campaign_id, go)


def record_report(campaign_id: str, module: str, text: str) -> Optional[dict[str, Any]]:
    """What a step said it established, kept on the step for the board and the next brief."""
    def go(campaign: dict[str, Any]) -> None:
        for step in campaign["steps"]:
            if step["module"] == module:
                step["reported"] = str(text or "")[:2000]
    return _mutate(campaign_id, go)


def set_step_brief(campaign_id: str, module: str, expect: str) -> Optional[dict[str, Any]]:
    """Redirect a step that has not run yet — how the manager acts on what it just read."""
    def go(campaign: dict[str, Any]) -> None:
        for step in campaign["steps"]:
            if step["module"] == module and step["status"] == "pending":
                step["expect"] = str(expect or "")
    return _mutate(campaign_id, go)


def skip_step(campaign_id: str, module: str, note: str = "") -> Optional[dict[str, Any]]:
    def go(campaign: dict[str, Any]) -> None:
        for step in campaign["steps"]:
            if step["module"] == module and step["status"] == "pending":
                step["status"] = "skipped"
                step["finished_at"] = _now()
                step["note"] = note
    return _mutate(campaign_id, go)


def retry_step(campaign_id: str, module: str) -> Optional[dict[str, Any]]:
    """Put a failed step back in the queue — the manager's answer to a fixable failure."""
    def go(campaign: dict[str, Any]) -> None:
        for step in campaign["steps"]:
            if step["module"] == module and step["status"] == "failed":
                step["status"] = "pending"
                step["started_at"] = None
                step["finished_at"] = None
                step["note"] = f"retrying — previous attempt: {step.get('note') or 'failed'}"
                break
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
    """Mark live jobs paused at startup, and say which.

    A job's steps run in this process. After a restart nothing is driving them, but the file
    still says `running` — and a board that reports a module under test when no session exists
    is worse than one that reports nothing, because it is the same shape as progress. Paused
    rather than stopped: the work is still wanted, it just needs someone to say go.
    """
    touched = []
    for campaign in live():
        if campaign["status"] == "paused" and running_step(campaign) is None:
            continue
        updated = set_status(campaign["id"], "paused", blocked={
            "reason": "the server restarted while this was running",
            "module": (running_step(campaign) or {}).get("module")})
        if updated:
            touched.append(updated)
    return touched
