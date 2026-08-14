"""Runs the manager wants, waiting for you to say yes.

The manager can start a run on its own — see `agent_bridge.start_run`. This queue exists for
the runs it *should not* start on its own, and there is one clear class of those: re-tests
prompted by somebody else's fix.

The distinction is not squeamishness about autonomy. A re-test is reactive work whose trigger
lives outside this system — a developer closed a ticket — and three things about it are
routinely wrong in ways the manager cannot see. The fix may not be deployed to the environment
under test. It may be deployed to staging and not to the app store build on the iPad. And
"closed" in an issue tracker covers "fixed", "duplicate" and "not doing it", which are not the
same instruction. A queue turns each of those into a question rather than a wasted run against
an unchanged build.

So: work the manager plans, it runs. Work a fix triggers, it queues. That is the shape of the
answer given when this was specified — full autonomy, *and* a re-test approval gate — and it
is only contradictory if you read both halves as being about the same runs.

Stored beside `clusters.json` in `projects/`, atomically, for the reasons written there.
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

logger = logging.getLogger("retests")

SCHEMA_VERSION = 1

#: `pending` waits for you. `approved` has been started — kept rather than deleted so the queue
#: is a record of what was run and why, not just an inbox. `dismissed` is a decision too: the
#: next sync must not re-queue something you already said no to.
STATUSES = ("pending", "approved", "dismissed")

_LOCK = threading.RLock()


def _path() -> Path:
    return project_paths.DEFAULT_PROJECTS_DIR / "retests.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load() -> dict[str, Any]:
    path = _path()
    if not path.is_file():
        return {"schema": SCHEMA_VERSION, "ecosystems": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("could not read the re-test queue: %s", exc)
        return {"schema": SCHEMA_VERSION, "ecosystems": {}}
    if not isinstance(data, dict):
        return {"schema": SCHEMA_VERSION, "ecosystems": {}}
    data.setdefault("schema", SCHEMA_VERSION)
    data.setdefault("ecosystems", {})
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
        logger.warning("could not write the re-test queue: %s", exc)


def _key(package: str, module: str, finding: str) -> str:
    return f"{package}|{module}|{finding}"


def queue(ecosystem: str, package: str, module: str, finding: str, *,
          role: str = "", title: str = "", reason: str = "",
          issue_id: Optional[int] = None, issue_url: str = "",
          instruction: str = "") -> Optional[dict[str, Any]]:
    """Add a re-test request, or return None if this finding is already in the queue.

    Idempotent on the finding, and deliberately so: `sync_issue_status` runs repeatedly, and a
    queue that grew a duplicate entry every time somebody checked Blackcode would be unusable
    within a day. A `dismissed` entry counts as present — saying no once should mean no.
    """
    with _LOCK:
        data = _load()
        bucket = data["ecosystems"].setdefault(ecosystem, {})
        key = _key(package, module, finding)
        if key in bucket:
            return None
        bucket[key] = {
            "id": key, "package": package, "module": module, "finding": finding,
            "role": role, "title": title, "reason": reason,
            "issue_id": issue_id, "issue_url": issue_url,
            "instruction": instruction,
            "status": "pending", "queued_at": _now(), "decided_at": None,
        }
        _save(data)
        return dict(bucket[key])


def list_queued(ecosystem: str, status: Optional[str] = None) -> list[dict[str, Any]]:
    """Everything queued for this product, newest first, optionally one status."""
    bucket = _load()["ecosystems"].get(ecosystem, {})
    rows = [dict(v) for v in bucket.values()]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return sorted(rows, key=lambda r: str(r.get("queued_at") or ""), reverse=True)


def get(ecosystem: str, entry_id: str) -> Optional[dict[str, Any]]:
    found = _load()["ecosystems"].get(ecosystem, {}).get(entry_id)
    return dict(found) if found else None


def decide(ecosystem: str, entry_id: str, status: str) -> Optional[dict[str, Any]]:
    """Mark an entry approved or dismissed. Starting the run is the caller's job — this
    module records the decision and nothing else, so a run that fails to start does not
    silently leave the queue saying it succeeded."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with _LOCK:
        data = _load()
        entry = data["ecosystems"].get(ecosystem, {}).get(entry_id)
        if entry is None:
            return None
        entry["status"] = status
        entry["decided_at"] = _now()
        _save(data)
        return dict(entry)


def forget(ecosystem: str, entry_id: str) -> bool:
    """Drop an entry entirely — for a finding that no longer exists, or a mis-queue you want
    the next sync to be free to raise again."""
    with _LOCK:
        data = _load()
        bucket = data["ecosystems"].get(ecosystem, {})
        if entry_id not in bucket:
            return False
        bucket.pop(entry_id)
        _save(data)
        return True


def summary(ecosystem: str) -> dict[str, int]:
    rows = list_queued(ecosystem)
    return {status: sum(1 for r in rows if r.get("status") == status) for status in STATUSES}
