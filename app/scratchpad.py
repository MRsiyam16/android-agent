"""The one place every agent in a product can write something down for the others.

Everything else in this system is deliberately partitioned. A module's findings, memory and
transcript belong to that module; a project's secrets belong to that project. That is right for
*evidence* — a verdict should be traceable to the agent that watched it happen — and it is
exactly wrong for the small, perishable facts a cross-app job runs on.

The concrete failure: an Android module books an appointment for "Testina Doe, Tuesday 14:30,
ref #4471". The iPad module then has to confirm it arrived. It cannot. The booking details
exist only inside a transcript in another project, and nothing carries them across. So the iPad
module goes looking for *an* appointment, finds one, and reports success about something it
never verified — which is worse than failing.

This is that missing carrier. One shared, per-product notepad:

    put("last-booking", "Testina Doe, Tue 14:30, ref #4471")   <- the Android module
    get("last-booking")                                        <- the iPad module

**Deliberately small, and deliberately not a database.** It holds what is true *right now* for
a job in flight — a booking reference, a test account's email, which environment is under test.
It is not where findings go, not where memory goes, and not a log. Entries are overwritten by
key and dropped when they stop being relevant, and the dashboard shows every one of them so a
stale entry is visible rather than quietly wrong.

**Every tier can write to it, and that is the point.** A module tester has no other way to tell
anyone anything except by filing a finding, which is a verdict — the wrong shape for "here is
the reference number I just created".

Stored beside `clusters.json`, `retests.json` and `campaigns.json` in `projects/`, atomically,
for the reasons written there.
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

logger = logging.getLogger("scratchpad")

SCHEMA_VERSION = 1

#: A cap, so a runaway loop cannot turn the notepad into a log file. Oldest goes first, and the
#: drop is reported rather than silent — a note that vanished is worse than one that was
#: refused, because only one of them is visible.
MAX_ENTRIES = 200

#: Values are meant to be a line or two. Longer than this is almost always a finding or a
#: report being put in the wrong place.
MAX_VALUE = 4000

_LOCK = threading.RLock()


class ScratchpadFull(RuntimeError):
    """The notepad is at its cap and nothing could be evicted."""


def _path() -> Path:
    return project_paths.DEFAULT_PROJECTS_DIR / "scratchpad.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load() -> dict[str, Any]:
    path = _path()
    if not path.is_file():
        return {"schema": SCHEMA_VERSION, "ecosystems": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("could not read the scratchpad: %s", exc)
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
        logger.warning("could not write the scratchpad: %s", exc)


def normalise_key(key: str) -> str:
    """Keys are lowercase and hyphenated, so `Last Booking` and `last-booking` are one note.

    Worth enforcing rather than documenting: the writer and the reader are two different agents
    in two different sessions that never see each other's text, so nothing else would ever
    catch a near-miss. Two spellings of one key is silently losing the handoff.
    """
    cleaned = "-".join(str(key or "").strip().lower().split())
    return "".join(ch for ch in cleaned if ch.isalnum() or ch in "-_.")[:80]


def put(ecosystem: str, key: str, value: str, *, author: str = "",
        note: str = "") -> dict[str, Any]:
    """Write a note. Overwrites the same key, keeping when it was first written."""
    clean = normalise_key(key)
    if not clean:
        raise ValueError("A note needs a key.")
    text = str(value if value is not None else "")
    if len(text) > MAX_VALUE:
        raise ValueError(f"That value is {len(text)} characters; the scratchpad holds "
                         f"{MAX_VALUE}. It is for a fact, not a report — file the detail as a "
                         f"finding and put the reference here.")

    with _LOCK:
        data = _load()
        bucket = data["ecosystems"].setdefault(ecosystem, {})
        existing = bucket.get(clean)

        if existing is None and len(bucket) >= MAX_ENTRIES:
            oldest = min(bucket.values(), key=lambda e: str(e.get("updated_at") or ""))
            bucket.pop(oldest["key"], None)
            logger.warning("scratchpad for %s was full — dropped the oldest note (%s)",
                           ecosystem, oldest["key"])

        entry = {
            "key": clean, "value": text, "note": str(note or ""),
            "author": str(author or ""),
            "created_at": (existing or {}).get("created_at") or _now(),
            "updated_at": _now(),
        }
        bucket[clean] = entry
        _save(data)
        return dict(entry)


def get(ecosystem: str, key: str) -> Optional[dict[str, Any]]:
    entry = _load()["ecosystems"].get(ecosystem, {}).get(normalise_key(key))
    return dict(entry) if entry else None


def list_all(ecosystem: str) -> list[dict[str, Any]]:
    """Every note, most recently written first."""
    bucket = _load()["ecosystems"].get(ecosystem, {})
    return sorted((dict(e) for e in bucket.values()),
                  key=lambda e: str(e.get("updated_at") or ""), reverse=True)


def drop(ecosystem: str, key: str) -> bool:
    with _LOCK:
        data = _load()
        bucket = data["ecosystems"].get(ecosystem, {})
        if normalise_key(key) not in bucket:
            return False
        bucket.pop(normalise_key(key))
        _save(data)
        return True


def clear(ecosystem: str) -> int:
    """Wipe the notepad. Returns how many notes went."""
    with _LOCK:
        data = _load()
        bucket = data["ecosystems"].get(ecosystem, {})
        count = len(bucket)
        data["ecosystems"][ecosystem] = {}
        _save(data)
        return count


def render(ecosystem: str, limit: int = 40) -> str:
    """The notepad as text, for dropping into an agent's brief."""
    rows = list_all(ecosystem)
    if not rows:
        return "(the shared scratchpad is empty)"
    out = []
    for entry in rows[:limit]:
        line = f"- {entry['key']}: {entry['value']}"
        if entry.get("author"):
            line += f"   [{entry['author']}]"
        out.append(line)
    if len(rows) > limit:
        out.append(f"- ...{len(rows) - limit} more notes")
    return "\n".join(out)
