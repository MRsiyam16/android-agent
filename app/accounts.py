"""Which clinic, which doctor, which patient a run was signed in as.

A defect report that says "creating a Procedure fails with 'Missing or insufficient
permissions'" is most of the way to useless to whoever has to fix it. Permissions are *per
account*. The first question back is always "which clinic?", and the answer was living in the
prose of one finding out of five, phrased differently each time, or nowhere at all.

That is what this fixes. Each project records the accounts it is tested as; every finding is
stamped with a snapshot of them at the moment it is filed; and an issue carries the union across
everything it covers. The developer opening the ticket sees the clinic, the doctor and the
patient without asking.

**Stamped rather than remembered.** The stamp happens inside `store.add_finding`, so a finding
gets it whether or not the agent thought to mention it. An agent that has to remember to include
context in prose will include it in the first three findings of a run and none of the rest —
which is exactly the pattern the existing data shows.

**A snapshot, not a reference.** The stamp is copied onto the finding rather than looked up
later, because the accounts change: a run creates a new doctor, switches to a different clinic,
registers a fresh patient. A finding filed an hour ago belongs to the account that was signed in
an hour ago, and resolving it live would quietly re-attribute old defects to whoever is signed
in now.

Roles are free text, because the products differ and inventing an enum here would just mean the
interesting one is always missing. The conventional set is `clinic`, `doctor`, `patient`,
`admin`.

Stored beside the other cross-project state in `projects/accounts.json`.
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

logger = logging.getLogger("accounts")

SCHEMA_VERSION = 1

#: What the products in this system actually have. Not enforced — a role outside it is
#: accepted and stored — but these are what the prompts suggest, so reports stay comparable.
CONVENTIONAL_ROLES = ("clinic", "doctor", "patient", "admin")

_LOCK = threading.RLock()


def _path() -> Path:
    return project_paths.DEFAULT_PROJECTS_DIR / "accounts.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load() -> dict[str, Any]:
    path = _path()
    if not path.is_file():
        return {"schema": SCHEMA_VERSION, "projects": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("could not read test accounts: %s", exc)
        return {"schema": SCHEMA_VERSION, "projects": {}}
    if not isinstance(data, dict):
        return {"schema": SCHEMA_VERSION, "projects": {}}
    data.setdefault("schema", SCHEMA_VERSION)
    data.setdefault("projects", {})
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
        logger.warning("could not write test accounts: %s", exc)


def set_account(package: str, role: str, *, email: str = "", label: str = "",
                note: str = "") -> dict[str, Any]:
    """Record which account this project is being tested as, in one role.

    Overwrites that role for that project. Switching from one clinic to another is a change of
    fact, not a second fact — keeping both would make every later finding ambiguous about which
    one it belonged to.
    """
    clean = " ".join(str(role or "").strip().lower().split())
    if not clean:
        raise ValueError("An account needs a role — clinic, doctor, patient, admin.")
    if not (email or label):
        raise ValueError("An account needs at least an email or a label to identify it.")

    with _LOCK:
        data = _load()
        bucket = data["projects"].setdefault(package, {})
        bucket[clean] = {"role": clean, "email": str(email or ""), "label": str(label or ""),
                         "note": str(note or ""), "updated_at": _now()}
        _save(data)
        return dict(bucket[clean])


def forget(package: str, role: str) -> bool:
    with _LOCK:
        data = _load()
        bucket = data["projects"].get(package, {})
        key = " ".join(str(role or "").strip().lower().split())
        if key not in bucket:
            return False
        bucket.pop(key)
        _save(data)
        return True


def get(package: str) -> dict[str, dict[str, Any]]:
    """Every account recorded for this project, by role."""
    return {k: dict(v) for k, v in _load()["projects"].get(package, {}).items()}


def stamp(package: str) -> list[dict[str, str]]:
    """The snapshot copied onto a finding at file time. Empty when nothing is recorded.

    Deliberately a plain list of small dicts rather than the whole record: this is embedded in
    every finding on disk, and `updated_at` on a copy is noise that would only ever be read as
    the finding's own timestamp.
    """
    return [{"role": v["role"], "email": v.get("email", ""), "label": v.get("label", "")}
            for v in sorted(get(package).values(), key=lambda a: a["role"])
            if v.get("email") or v.get("label")]


def describe(entries: list[dict[str, str]]) -> str:
    """One line per account, for a prompt or a console."""
    if not entries:
        return "(no test accounts recorded)"
    out = []
    for a in entries:
        who = a.get("email") or ""
        label = a.get("label") or ""
        out.append(f"{a['role']}: " + (f"{who}" if who else "") +
                   (f" ({label})" if label and who else label))
    return "\n".join(out)


def as_markdown(by_package: dict[str, list[dict[str, str]]],
                roles: Optional[dict[str, str]] = None) -> str:
    """The "Accounts under test" table an issue carries.

    A table rather than a paragraph because the reader is scanning for one row — theirs. The
    app column uses the ecosystem role (`clinic-web`) where there is one, since a developer
    knows the app by that name and not by its package.
    """
    rows = []
    for package, entries in by_package.items():
        for a in entries:
            who = a.get("email") or a.get("label") or ""
            extra = a.get("label") if a.get("email") and a.get("label") else ""
            rows.append(f"| {(roles or {}).get(package, package)} | {a['role']} | {who} | "
                        f"{extra} |")
    if not rows:
        return ""
    return ("\n## Accounts under test\n\n"
            "Permissions and visibility are per account, so the same build behaves differently "
            "for different ones. These are the accounts that were signed in when this was "
            "reproduced.\n\n"
            "| App | Role | Account | Notes |\n|---|---|---|---|\n" + "\n".join(rows) + "\n")
