"""One driver per target, so several tests can run at once without fighting.

Nothing here stopped two runs sharing a phone before this module. Every session builds its own
`Device`, so two of them would each connect to the same serial and interleave taps on one
screen — and the resulting findings would each be a verdict about a screen the *other* agent
had just changed. That is worse than a crash: it produces confident, well-evidenced, wrong
results, filed under two different modules.

What was missing is only the mutual exclusion. Running an iPad suite and a website suite at the
same time was always safe — different hardware, different adapters, no shared state — and this
module is what makes the difference between the two cases explicit rather than a thing you have
to remember.

**Held for the length of a turn, not the length of a session.** Several modules of one app are
legitimately open at once; you just cannot *run* two of them together. Acquiring on connect
would make opening a second module look like a conflict, and holding it after a turn ends
would make a dashboard you left open block a run you start elsewhere.

**Keyed by target, not by project.** Native runs key on the device serial: two projects on one
phone conflict, and one project across two phones does not. Web runs key on the site, because
two browsers are cheap but one account's state is not — two suites signing into the same site
would log each other out and file the results as defects.

Deliberately in-process. Every session lives in one server, and a lock in a file would add a
second source of truth about a thing this process already knows exactly.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("device_locks")

#: Web sessions get their own browser, so the thing worth protecting is the site's account
#: state rather than any hardware. Prefixed so a URL can never collide with a device serial.
WEB_PREFIX = "web:"

_LOCK = threading.RLock()
_held: dict[str, dict[str, Any]] = {}


class DeviceBusy(RuntimeError):
    """Another module is driving this target. Carries who, so the message can say so."""

    def __init__(self, key: str, holder: dict[str, Any]):
        self.key = key
        self.holder = holder
        super().__init__(
            f"{holder['package']} / {holder['slug']} is already driving {key} "
            f"(since {holder['since']}). Two agents on one target interleave their actions, so "
            f"each one's findings describe a screen the other just changed. Stop that run, or "
            f"wait for it to finish.")


def key_for(platform: str, serial: Optional[str], package: str) -> str:
    """The target this session will contend for.

    Web keys on the project because that is what its runs share — the site and the account on
    it. Native keys on the serial, falling back to the package only when no serial is known,
    which is the safe direction: it over-locks one project against itself rather than letting
    two runs onto a phone whose identity nobody established.
    """
    if (platform or "").lower() == "web":
        return WEB_PREFIX + package
    return str(serial or f"unknown-device:{package}")


def acquire(key: str, package: str, slug: str) -> None:
    """Take the target for this module, or raise `DeviceBusy` naming who has it.

    Re-entrant for the same module: a session that already holds its target and starts another
    turn keeps it rather than deadlocking against itself.
    """
    with _LOCK:
        holder = _held.get(key)
        if holder is not None:
            if (holder["package"], holder["slug"]) == (package, slug):
                holder["depth"] += 1
                return
            raise DeviceBusy(key, holder)
        _held[key] = {"package": package, "slug": slug, "depth": 1,
                      "since": time.strftime("%H:%M:%S", time.localtime())}
        logger.info("locked %s for %s/%s", key, package, slug)


def release(key: str, package: str, slug: str) -> None:
    """Give the target back. A module that does not hold it is a no-op, never an error —
    this runs in `finally` blocks, where raising would mask whatever actually went wrong."""
    with _LOCK:
        holder = _held.get(key)
        if holder is None or (holder["package"], holder["slug"]) != (package, slug):
            return
        holder["depth"] -= 1
        if holder["depth"] <= 0:
            _held.pop(key, None)
            logger.info("released %s from %s/%s", key, package, slug)


def release_all(package: str, slug: str) -> None:
    """Drop every target this module holds — for session close, where the depth is moot."""
    with _LOCK:
        for key, holder in list(_held.items()):
            if (holder["package"], holder["slug"]) == (package, slug):
                _held.pop(key, None)
                logger.info("released %s from %s/%s (session closed)", key, package, slug)


def holder(key: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        found = _held.get(key)
        return dict(found) if found else None


def held() -> dict[str, dict[str, Any]]:
    """Every target currently taken, for the dashboard's status."""
    with _LOCK:
        return {key: dict(value) for key, value in _held.items()}


def reset() -> None:
    """Drop every lock. For tests, and for a server that has just restarted into a process
    where the old holders no longer exist."""
    with _LOCK:
        _held.clear()
