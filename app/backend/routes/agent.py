"""The Agent tab: modules, transcripts, models, findings, credentials, evidence.

A turn is handed to a background task and the request returns immediately — a turn can
last many minutes, everything it does arrives over the WebSocket, and holding the request
open would hit proxy timeouts while giving the browser nothing to show.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

import config
import project_paths
from agent import store as agent_store

from .. import agent_bridge, projects, state
from ..schemas import (AgentMessagePayload, AgentTriggerPayload, AttachmentPayload,
                       ModelPayload, SecretPayload, SubprojectPayload,
                       SubprojectUpdatePayload)

logger = logging.getLogger("server.agent")
router = APIRouter()

sessions = agent_bridge.sessions


#: Attached devices, cached briefly. `_device_for` runs on every warm and every message, and
#: an uncached listing shells out to adb *and* pymobiledevice3 each time.
_attached_cache: dict[str, object] = {"at": 0.0, "value": []}


def _attached() -> list[dict[str, str]]:
    import device as device_mod

    now = time.monotonic()
    if now - float(_attached_cache["at"]) > 5:
        try:
            _attached_cache["value"] = device_mod.list_devices()
        except Exception:  # noqa: BLE001 - a listing failure must not block a run
            _attached_cache["value"] = []
        _attached_cache["at"] = now
    return list(_attached_cache["value"])   # type: ignore[arg-type]


def _device_for(package: str) -> tuple[Optional[str], Optional[str]]:
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
    meta = projects.read_meta(package) or {}
    platform = meta.get("platform")

    if (platform or "").lower() == "web":
        return None, platform

    pinned = meta.get("device_serial")
    if pinned:
        return str(pinned), platform

    live = state.device_serial()
    attached = _attached()
    if live:
        match = next((d for d in attached if d["serial"] == live), None)
        # No match means nothing is listing it — trust it rather than override, since a
        # headless run posting telemetry is exactly a device this process cannot enumerate.
        if match is None or not platform or match.get("platform") == platform:
            return live, platform

    same_kind = [d for d in attached if not platform or d.get("platform") == platform]
    if len(same_kind) == 1:
        return same_kind[0]["serial"], platform
    # Two of the same kind and no pin: let the adapter pick and say so in the logs, rather
    # than choosing one here and being confidently wrong about which iPad you meant.
    if len(same_kind) > 1:
        logger.info("%s: %d %s devices attached and none pinned — pin one with "
                    "POST /projects/{package}/device", package, len(same_kind), platform)
    return None, platform


@router.get("/agent/status")
async def agent_status():
    """Which sessions are live, busy, blocked on a question, or parked on a rate limit."""
    import device_locks

    return {
        "sessions": sessions.status(),
        "planner": "claude-code-cli (subscription)",
        "cheap_tier": config.AGENT_USE_CHEAP_TIER,
        "stepper_model": config.AGENT_STEPPER_MODEL if config.AGENT_USE_CHEAP_TIER else None,
        "last_opened": agent_store.get_last_opened(),
        # Which targets are taken and by whom. Concurrent runs are the normal case now, so
        # "why will this one not start" needs an answer that is visible rather than inferred.
        "device_locks": device_locks.held(),
    }


@router.get("/devices")
async def list_attached_devices():
    """Every attached device, and which project each is pinned to.

    Pinning matters once more than one device of the same kind is in play: an iPad and an
    iPhone are both `ios` with identically-shaped UDIDs, so without a pin a run on one can
    silently drive the other.
    """
    attached = await asyncio.to_thread(_attached)
    pins: dict[str, list[str]] = {}
    for package in project_paths.known_packages():
        serial = (projects.read_meta(package) or {}).get("device_serial")
        if serial:
            pins.setdefault(str(serial), []).append(package)
    return [{**d, "pinned_to": pins.get(d["serial"], [])} for d in attached]


@router.post("/projects/{package:path}/device")
async def pin_device(package: str, payload: AgentTriggerPayload | None = None):
    """Pin a project to one device, or unpin it by sending no serial.

    Stored on the project's own meta, the same route `platform` and `blackcode_project_id`
    took — so which phone a suite runs on is a property of the suite rather than of whichever
    device happened to post telemetry last.
    """
    serial = (payload.device_serial if payload else None) or None
    meta = projects.write_meta(package, device_serial=serial)
    _attached_cache["at"] = 0.0     # so the next read re-checks rather than serving a stale pin
    return {"package": package, "device_serial": meta.get("device_serial")}


@router.get("/agent/prompt-presets")
async def prompt_presets():
    """The prewritten prompts the composer offers on an empty module.

    Kept beside `/agent/status` with the other fixed paths rather than down among the
    `/agent/{package:path}/...` routes. Nothing currently claims two segments with a `{package:path}`
    in the second position, so there is no live conflict — but "prompt-presets" is a
    perfectly good package name as far as the router is concerned, and grouping the literals
    is what stops a later `/agent/{package:path}` from quietly capturing this one.
    """
    from agent.prompts import preset_prompts

    return {"presets": preset_prompts()}


@router.post("/agent/{package:path}/{slug}/warm")
async def warm_agent(package: str, slug: str, payload: AgentTriggerPayload | None = None):
    """Spawn the module's Claude Code session now, without sending it anything.

    Called on startup for the last-used module and again whenever you select one in the UI, so
    the CLI's spawn cost is paid while you are still typing rather than after you hit send.
    """
    if agent_store.get_subproject(package, slug) is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    agent_store.set_last_opened(package, slug)
    pinned, platform = _device_for(package)
    serial = (payload.device_serial if payload else None) or pinned
    try:
        return await sessions.warm(package, slug, serial=serial, platform=platform)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/agent/{package:path}/subprojects")
async def list_subprojects(package: str):
    return agent_store.list_subprojects(package)


@router.post("/agent/{package:path}/subprojects")
async def create_subproject(package: str, payload: SubprojectPayload):
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    projects.ensure_project(package)
    return agent_store.create_subproject(package, payload.title, payload.scope,
                                         status="approved")


@router.patch("/agent/{package:path}/subprojects/{slug}")
async def patch_subproject(package: str, slug: str, payload: SubprojectUpdatePayload):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    entry = agent_store.update_subproject(package, slug, **updates)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    await state.manager.broadcast({"type": "agent_subproject_updated", "package": package,
                                   "subproject": entry})
    return entry


@router.delete("/agent/{package:path}/subprojects/{slug}")
async def remove_subproject(package: str, slug: str):
    """Removes it from the list only. The transcript, findings and evidence stay on disk —
    a mis-click should not be able to destroy a test history."""
    if not agent_store.delete_subproject(package, slug):
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    await sessions.close(package, slug)
    return {"ok": True, "note": "Folder kept on disk; only the listing entry was removed."}


@router.get("/agent/{package:path}/{slug}/chat")
async def get_chat(package: str, slug: str, limit: int = 400):
    session = sessions.peek(package, slug)
    return {
        "messages": agent_store.read_chat(package, slug, limit=limit),
        "findings": agent_store.list_findings(package, slug),
        "busy": bool(session and session.busy),
        "blocked": session.device.pending_question if session else None,
        "parked": session.parked_reason if session else None,
    }


@router.post("/agent/{package:path}/{slug}/message")
async def post_message(package: str, slug: str, payload: AgentMessagePayload):
    """Hand a message to the agent and return immediately."""
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    if agent_store.get_subproject(package, slug) is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project — create it first")
    pinned, platform = _device_for(package)
    serial = payload.device_serial or pinned
    session = sessions.get(package, slug, serial=serial, platform=platform)
    asyncio.create_task(session.send(payload.text))
    return {"ok": True, "accepted": True}


@router.post("/agent/{package:path}/{slug}/attachment")
async def upload_attachment(package: str, slug: str, payload: AttachmentPayload):
    """Store an image the user attached to a chat message, and return its path.

    Written into the module's own `shots/` folder alongside the agent's own screenshots, and
    handed to the agent as a *path* rather than inlined into the message. The agent already
    has `Read`, and reading an image file is how it looks at its own evidence — so a reference
    image arrives through the same door, and the transcript stays text.
    """
    if agent_store.get_subproject(package, slug) is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project")

    raw = payload.data_url
    match = re.fullmatch(r"data:image/(png|jpeg|jpg|webp|gif);base64,(.+)", raw, re.DOTALL)
    if not match:
        raise HTTPException(status_code=400,
                            detail="Expected a base64 image data URL (png, jpeg, webp or gif)")
    ext = "jpg" if match.group(1) in ("jpeg", "jpg") else match.group(1)
    try:
        blob = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode the image: {exc}") from exc
    if len(blob) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image is larger than 12 MB")

    shots = agent_store.shots_dir(package, slug)
    shots.mkdir(parents=True, exist_ok=True)
    # Timestamped and counted: two images attached in the same second must not collide, and a
    # name derived from the user's filename would let a crafted one escape the folder.
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    existing = len(list(shots.glob(f"ref-{stamp}-*")))
    path = shots / f"ref-{stamp}-{existing + 1:02d}.{ext}"
    try:
        path.write_bytes(blob)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not save the image: {exc}") from exc
    return {"ok": True, "path": str(path)}


@router.post("/agent/{package:path}/recon")
async def start_recon(package: str, payload: AgentTriggerPayload | None = None):
    """Kick off the recon pass that proposes the module breakdown for a new project."""
    from agent.prompts import recon_prompt

    agent_store.create_subproject(package, "Recon", "map the app and propose modules",
                                  status="approved")
    projects.ensure_project(package)
    pinned, platform = _device_for(package)
    serial = (payload.device_serial if payload else None) or pinned
    session = sessions.get(package, "recon", serial=serial, platform=platform)
    asyncio.create_task(session.send(recon_prompt()))
    return {"ok": True, "slug": "recon"}


@router.post("/agent/{package:path}/main")
async def start_main(package: str, payload: AgentTriggerPayload | None = None):
    """Create the project's manager module and start it on the setup interview.

    The interview — goals, then permission, then recon, then a proposal — is how the module
    breakdown ends up answering the user's priorities rather than just enumerating screens.
    What is new is that the module does not go quiet afterwards: it stays as this project's
    manager, with tools to create modules and to read what the others found, so "add a module
    for the part you have not covered" and "where does this project stand" have somewhere to
    be asked. See `agent/manager_tools.py`.

    Idempotent by construction. `create_subproject` is idempotent on slug and `main_slug`
    resolves to whatever this project already has, so posting twice re-opens the same
    conversation rather than starting a second one over the top of it — which matters because
    the button that calls this is next to project creation and gets double-clicked.
    """
    from agent.prompts import onboarding_prompt

    slug = agent_store.main_slug(package)
    agent_store.create_subproject(
        package, "Main" if slug == agent_store.MAIN_SLUG else "Onboarding",
        "manages this project: what the user wants from the app, the module breakdown that "
        "follows from it, and what the modules have found",
        status="approved")
    projects.ensure_project(package)
    pinned, platform = _device_for(package)
    serial = (payload.device_serial if payload else None) or pinned
    session = sessions.get(package, slug, serial=serial, platform=platform)
    asyncio.create_task(session.send(onboarding_prompt(package)))
    return {"ok": True, "slug": slug}


@router.post("/agent/{package:path}/onboarding")
async def start_onboarding(package: str, payload: AgentTriggerPayload | None = None):
    """What `/main` used to be called, kept because a stale browser tab still posts it.

    The frontend modules are cached hard (see CLAUDE.md — a hard reload is needed after any
    change under `frontend/static/`), so the version of `main.js` that posts `/onboarding` can
    still be the one loaded in a tab that has been open since before this rename. Without
    this, creating a project in that tab would leave a project with no manager module and an
    error where the interview should have been.

    Not a redirect: a 307 on a POST is honoured by browsers but this is called by `fetch`
    against a JSON API, and forwarding the call is simpler to reason about than a status code
    the caller has to follow.
    """
    return await start_main(package, payload)


@router.post("/agent/{package:path}/{slug}/stop")
async def stop_agent(package: str, slug: str):
    session = sessions.peek(package, slug)
    if session is None:
        # Nothing running is the outcome Stop was asking for, so this is success rather than
        # a 404 the UI has to explain. The old 404 is why pressing Stop on an idle module
        # printed an error and looked like the button was broken.
        return {"ok": True, "stopped": False, "note": "Nothing was running."}
    stopped = await session.interrupt()
    return {"ok": True, "stopped": stopped}


@router.get("/agent/{package:path}/{slug}/models")
async def list_models(package: str, slug: str):
    """Models this CLI can run, and which one this module is on.

    Read from the live session rather than hardcoded, so the list is whatever the installed
    CLI and the signed-in subscription actually offer.
    """
    session = sessions.peek(package, slug)
    if session is None:
        return {"models": [], "current": None, "requested": None}
    return {"models": session.available_models,
            "current": session.model,
            "requested": session.requested_model}


@router.post("/agent/{package:path}/{slug}/model")
async def set_model(package: str, slug: str, payload: ModelPayload):
    """Move a module onto a different model. Reconnects, resuming the conversation."""
    if agent_store.get_subproject(package, slug) is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    pinned, platform = _device_for(package)
    session = sessions.get(package, slug, serial=pinned, platform=platform)
    try:
        return await session.set_model(payload.model)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/agent/{package:path}/{slug}/findings")
async def get_findings(package: str, slug: str):
    return agent_store.list_findings(package, slug)


@router.post("/agent/{package:path}/secrets")
async def put_secret(package: str, payload: SecretPayload):
    """Store a test credential. Values are write-only over the API: the response lists names
    only, and the agent enters one via a tool without it ever entering the transcript."""
    if not payload.name.strip() or not payload.value:
        raise HTTPException(status_code=400, detail="name and value are required")
    agent_store.set_secret(package, payload.name.strip(), payload.value)
    return {"ok": True, "names": agent_store.secret_keys(package)}


@router.get("/agent/{package:path}/secrets")
async def list_secrets(package: str):
    return {"names": agent_store.secret_keys(package)}


@router.get("/agent/shot")
async def get_shot(path: str):
    """Serve a screenshot the agent captured, for the chat thumbnails.

    `path` arrives from the browser, so it is resolved and checked against the set of known
    project roots before being opened rather than trusted. The check is a whitelist of roots
    rather than one fixed tree because a project may now live anywhere the user pointed it —
    but "anywhere the user pointed it" is still a closed set, not "anywhere on disk".
    """
    try:
        resolved = Path(path).resolve()
        roots = [project_paths.DEFAULT_PROJECTS_DIR.resolve()]
        for package in project_paths.known_packages():
            with contextlib.suppress(OSError, ValueError):
                roots.append(project_paths.project_dir(package).resolve())
        if not any(resolved.is_relative_to(r) for r in roots) or not resolved.is_file():
            raise HTTPException(status_code=404, detail="Not an agent screenshot")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Bad path: {exc}") from exc
    return FileResponse(resolved, media_type="image/jpeg")
