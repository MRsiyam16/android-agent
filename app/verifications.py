"""Verdicts this harness owes to another system, and the evidence behind each one.

`retests.py` is the queue of runs a *fix* prompted and a person has to approve. This is the
other half of the same story, one system further out: Bugmaster fixes a bug on a VPS, cannot
touch the phone on this desk, and asks the QA Verifier to re-run one case against the patched
build. What comes back has to be a verdict Bugmaster can gate a merge on — so it lives on
disk, keyed by *its* job id, and it is read over HTTP by a worker that knows nothing about
this harness's layout. See `docs/VERIFIER.md` and `D:\\bugmaster\\docs\\BRIDGE.md` §5.

Three decisions are worth the words:

**Findings are resolved at write time, not at read time.** The tool is handed finding ids; the
file stores the whole record — kind, title, expected, actual, steps, and the absolute path to
the screenshot. The worker on the other end must never need `agent/store.py`'s idea of where a
module's `findings.json` lives, and a verdict must not change meaning because somebody later
edited a module. This file is a statement of what was true when it was made.

**A `job_id` is reported once.** The manager is an LLM with a `report_verification` tool and no
memory of a turn that timed out halfway; the worker polls and retries. Both of those produce a
second call, and a second call that overwrote the first would silently replace a `fail` with a
`pass` after the fix pipeline had already read the `fail`. `report` refuses instead, and the
refusal carries the verdict already on file so the caller can see it agreed anyway.

**`pass`, `fail`, `blocked` — and blocked is never a pass.** "Could not check" is its own
answer. Bugmaster turns it into a human's problem rather than a merge; anything that collapsed
it into one of the other two would be this harness claiming to have looked at something it did
not look at.

Stored beside `retests.json` in `<projects>/verifications.json`, atomically, for the reasons
written there. Note *which* projects folder: the Verifier runs with `PROJECTS_DIR` pointed at
its own notebook, so a verification never lands in the QA Master's.
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

logger = logging.getLogger("verifications")

SCHEMA_VERSION = 1

#: What a verify job can be answered with. `blocked` is not a soft `fail`: a fail says the
#: build under test is still wrong and sends the fixer round again, blocked says nobody
#: checked and stops the pipeline for a person.
VERDICTS = ("pass", "fail", "blocked")

#: The finding fields a verdict carries out of this harness. Deliberately a fixed list rather
#: than the whole record: `node`, `cluster`, `issue_id` and the account stamp are internal
#: bookkeeping, and shipping them to another system makes them into an interface.
EVIDENCE_FIELDS = ("id", "kind", "title", "expected", "actual", "steps", "evidence")

_LOCK = threading.RLock()


def _path() -> Path:
    return project_paths.DEFAULT_PROJECTS_DIR / "verifications.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load() -> dict[str, Any]:
    path = _path()
    if not path.is_file():
        return {"schema": SCHEMA_VERSION, "jobs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Same bargain as the re-test queue: a corrupt file must not take the bridge down. It
        # does mean an already-answered job looks unanswered, which the worker handles — it
        # polls, and an unanswered job eventually times out as `blocked`. Never as a pass.
        logger.warning("could not read the verification log: %s", exc)
        return {"schema": SCHEMA_VERSION, "jobs": {}}
    if not isinstance(data, dict):
        return {"schema": SCHEMA_VERSION, "jobs": {}}
    data.setdefault("schema", SCHEMA_VERSION)
    if not isinstance(data.get("jobs"), dict):
        data["jobs"] = {}
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
        logger.warning("could not write the verification log: %s", exc)


def resolve_findings(package: str, module: str,
                     finding_ids: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """(records, ids that do not exist) for the findings a verdict is standing on.

    Split out so the tool can refuse *before* writing anything when an id is wrong. A
    verification whose evidence list quietly dropped the finding the verdict was about is
    worse than no verification: the note still claims a bug was found and nothing backs it.
    """
    from agent import store

    by_id = {str(f.get("id")): f for f in store.list_findings(package, module)}
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for raw in finding_ids:
        finding_id = str(raw).strip()
        if not finding_id:
            continue
        found = by_id.get(finding_id)
        if found is None:
            missing.append(finding_id)
            continue
        records.append({key: found.get(key) for key in EVIDENCE_FIELDS})
    return records, missing


def report(job_id: str, *, verdict: str, finding_ids: list[str], note: str,
           package: str, module: str,
           campaign_id: Optional[str] = None) -> dict[str, Any]:
    """Record the answer to one Bugmaster job. Raises ValueError on a bad verdict or a repeat.

    The findings are read here, once, and copied in. After this returns, nothing about the
    module can change what this job was told.
    """
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("a verification needs the job id Bugmaster sent.")
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r} — it is one of {', '.join(VERDICTS)}.")

    findings, missing = resolve_findings(package, module, list(finding_ids or []))
    if missing:
        raise ValueError(f"no finding {', '.join(missing)} in {package}/{module}.")

    with _LOCK:
        data = _load()
        existing = data["jobs"].get(job_id)
        if existing is not None:
            raise ValueError(
                f"{job_id} was already reported as {existing.get('verdict')!r} at "
                f"{existing.get('reported_at')}. A job is answered once: the fix pipeline has "
                f"read that answer and acted on it.")
        record = {
            "job_id": job_id,
            "verdict": verdict,
            "note": str(note or ""),
            "reported_at": _now(),
            "package": package,
            "module": module,
            "campaign_id": campaign_id,
            "findings": findings,
        }
        data["jobs"][job_id] = record
        _save(data)
        return dict(record)


def get(job_id: str) -> Optional[dict[str, Any]]:
    """One job's verdict, or None while it has not been answered.

    None is what the worker polls against — `GET /verifications/{job_id}` is a 404 until the
    manager calls the tool — so it must stay distinguishable from an answer, never a stub.
    """
    found = _load()["jobs"].get(str(job_id or "").strip())
    return dict(found) if found else None


def list_recent(limit: int = 20) -> list[dict[str, Any]]:
    """The most recently reported verifications, newest first."""
    rows = [dict(v) for v in _load()["jobs"].values()]
    rows.sort(key=lambda r: str(r.get("reported_at") or ""), reverse=True)
    return rows[:max(0, int(limit))]
