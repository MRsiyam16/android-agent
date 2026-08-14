"""The one SessionRegistry, the wire it pushes agent events down, and how a run gets started.

Sessions live in this process: the planner is the Claude Code CLI driven through
claude-agent-sdk, and the device tools are in-process MCP tools, so an agent's every step
can be pushed straight out over the dashboard's WebSocket.

Separate from routes/agent.py because the startup pre-warm and the shutdown close both
need the registry without pulling in the route module — and now because starting a run is no
longer only something a browser does. The ecosystem manager can commission a module and run
it, and it must go through *exactly* the path the dashboard's Send button goes through:
same device selection, same registry, same background task. A second way to start a run is a
second set of rules about which phone it lands on.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from agent import runtime as agent_runtime
from agent import store as agent_store

from . import projects
from .state import manager

logger = logging.getLogger("server.agent_bridge")


async def _emit(event: dict[str, Any]) -> None:
    await manager.broadcast(event)


sessions = agent_runtime.SessionRegistry(_emit)


class RunRefused(RuntimeError):
    """A run could not be started. The message says why, in terms the caller can act on."""


# -- which device -------------------------------------------------------------------------
#: Attached devices, cached briefly. `device_for` runs on every warm, message and run, and an
#: uncached listing shells out to adb *and* pymobiledevice3 each time.
_attached_cache: dict[str, Any] = {"at": 0.0, "value": []}


def attached() -> list[dict[str, str]]:
    import device as device_mod

    now = time.monotonic()
    if now - float(_attached_cache["at"]) > 5:
        try:
            _attached_cache["value"] = device_mod.list_devices()
        except Exception:  # noqa: BLE001 - a listing failure must not block a run
            _attached_cache["value"] = []
        _attached_cache["at"] = now
    return list(_attached_cache["value"])


def forget_attached() -> None:
    """Drop the device cache, so the next read re-checks rather than serving a stale pin."""
    _attached_cache["at"] = 0.0


def device_for(package: str) -> tuple[Optional[str], Optional[str]]:
    """(serial, platform) for this project's sessions.

    Every session used to inherit `state.device_serial()` — the serial of whatever device last
    posted telemetry. With one phone that is right by accident. With an iPad and an iPhone
    both in play it is wrong half the time, and running the iPad suite would quietly drive the
    iPhone: same adapter, same shaped UDID, no error anywhere.

    So the order is: what the project is pinned to, then the live serial *if it is the right
    kind of device*, then the only attached device of that kind. Nothing here guesses across
    platforms — an iOS project is never handed an Android serial just because it is the one
    that happens to be there.

    Web projects have no device at all; their target is the URL, which is the package.
    """
    from . import state

    meta = projects.read_meta(package) or {}
    platform = meta.get("platform")

    if (platform or "").lower() == "web":
        return None, platform

    pinned = meta.get("device_serial")
    if pinned:
        return str(pinned), platform

    live = state.device_serial()
    found = attached()
    if live:
        match = next((d for d in found if d["serial"] == live), None)
        # No match means nothing is listing it — trust it rather than override, since a
        # headless run posting telemetry is exactly a device this process cannot enumerate.
        if match is None or not platform or match.get("platform") == platform:
            return live, platform

    same_kind = [d for d in found if not platform or d.get("platform") == platform]
    if len(same_kind) == 1:
        return same_kind[0]["serial"], platform
    # Two of the same kind and no pin: let the adapter pick and say so in the logs, rather
    # than choosing one here and being confidently wrong about which iPad you meant.
    if len(same_kind) > 1:
        logger.info("%s: %d %s devices attached and none pinned — pin one with "
                    "POST /projects/{package}/device", package, len(same_kind), platform)
    return None, platform


# -- starting one -------------------------------------------------------------------------
def session_for(package: str, slug: str, serial: Optional[str] = None):
    """The live session for this module, built against the right device."""
    pinned, platform = device_for(package)
    return sessions.get(package, slug, serial=serial or pinned, platform=platform)


def start_run(package: str, slug: str, text: str, *, serial: Optional[str] = None) -> dict:
    """Start a turn in the background, or raise `RunRefused` saying why not.

    The checks are here rather than in the route because the route is no longer the only
    caller. Two of them the browser could rely on the UI to prevent and a tool cannot:

    * **The module must exist.** A tool given a slug that was never created would otherwise
      create a session against nothing and report success.
    * **The target must be free.** `send()` refuses a busy device by itself, but it does so
      *inside* the background task — which reaches a browser as an error in the transcript
      and reaches a tool as nothing at all, because by then the call has already returned
      "ok". Checked up front so a refusal is the return value.

    Returns what actually happened, so a caller can report it rather than assume it.
    """
    import device_locks

    if agent_store.get_subproject(package, slug) is None:
        raise RunRefused(f"{package} has no module {slug!r}.")

    session = session_for(package, slug, serial=serial)
    key = device_locks.key_for(session.device.resolved_platform, session.device.serial, package)
    holder = device_locks.holder(key)
    if holder is not None and (holder["package"], holder["slug"]) != (package, slug):
        raise RunRefused(
            f"{holder['package']} / {holder['slug']} is already driving {key} (since "
            f"{holder['since']}). Two agents on one target interleave their actions, so each "
            f"one's findings describe a screen the other just changed.")
    if session.busy:
        raise RunRefused(f"{package} / {slug} is already running.")

    asyncio.create_task(session.send(text))
    return {"package": package, "slug": slug, "target": key, "started": True}
