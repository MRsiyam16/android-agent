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


@router.get("/agent/status")
async def agent_status():
    """Which sessions are live, busy, blocked on a question, or parked on a rate limit."""
    return {
        "sessions": sessions.status(),
        "planner": "claude-code-cli (subscription)",
        "cheap_tier": config.AGENT_USE_CHEAP_TIER,
        "stepper_model": config.AGENT_STEPPER_MODEL if config.AGENT_USE_CHEAP_TIER else None,
        "last_opened": agent_store.get_last_opened(),
    }


@router.post("/agent/{package}/{slug}/warm")
async def warm_agent(package: str, slug: str, payload: AgentTriggerPayload | None = None):
    """Spawn the module's Claude Code session now, without sending it anything.

    Called on startup for the last-used module and again whenever you select one in the UI, so
    the CLI's spawn cost is paid while you are still typing rather than after you hit send.
    """
    if agent_store.get_subproject(package, slug) is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    agent_store.set_last_opened(package, slug)
    serial = (payload.device_serial if payload else None) or state.device_serial()
    try:
        return await sessions.warm(package, slug, serial=serial)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/agent/{package}/subprojects")
async def list_subprojects(package: str):
    return agent_store.list_subprojects(package)


@router.post("/agent/{package}/subprojects")
async def create_subproject(package: str, payload: SubprojectPayload):
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    projects.ensure_project(package)
    return agent_store.create_subproject(package, payload.title, payload.scope,
                                         status="approved")


@router.patch("/agent/{package}/subprojects/{slug}")
async def patch_subproject(package: str, slug: str, payload: SubprojectUpdatePayload):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    entry = agent_store.update_subproject(package, slug, **updates)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    await state.manager.broadcast({"type": "agent_subproject_updated", "package": package,
                                   "subproject": entry})
    return entry


@router.delete("/agent/{package}/subprojects/{slug}")
async def remove_subproject(package: str, slug: str):
    """Removes it from the list only. The transcript, findings and evidence stay on disk —
    a mis-click should not be able to destroy a test history."""
    if not agent_store.delete_subproject(package, slug):
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    await sessions.close(package, slug)
    return {"ok": True, "note": "Folder kept on disk; only the listing entry was removed."}


@router.get("/agent/{package}/{slug}/chat")
async def get_chat(package: str, slug: str, limit: int = 400):
    session = sessions.peek(package, slug)
    return {
        "messages": agent_store.read_chat(package, slug, limit=limit),
        "findings": agent_store.list_findings(package, slug),
        "busy": bool(session and session.busy),
        "blocked": session.device.pending_question if session else None,
        "parked": session.parked_reason if session else None,
    }


@router.post("/agent/{package}/{slug}/message")
async def post_message(package: str, slug: str, payload: AgentMessagePayload):
    """Hand a message to the agent and return immediately."""
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    if agent_store.get_subproject(package, slug) is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project — create it first")
    serial = payload.device_serial or state.device_serial()
    session = sessions.get(package, slug, serial=serial)
    asyncio.create_task(session.send(payload.text))
    return {"ok": True, "accepted": True}


@router.post("/agent/{package}/{slug}/attachment")
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


@router.post("/agent/{package}/recon")
async def start_recon(package: str, payload: AgentTriggerPayload | None = None):
    """Kick off the recon pass that proposes the module breakdown for a new project."""
    from agent.prompts import recon_prompt

    agent_store.create_subproject(package, "Recon", "map the app and propose modules",
                                  status="approved")
    projects.ensure_project(package)
    serial = (payload.device_serial if payload else None) or state.device_serial()
    session = sessions.get(package, "recon", serial=serial)
    asyncio.create_task(session.send(recon_prompt()))
    return {"ok": True, "slug": "recon"}


@router.post("/agent/{package}/onboarding")
async def start_onboarding(package: str, payload: AgentTriggerPayload | None = None):
    """Start the new-project interview: goals, then permission, then recon, then a proposal.

    Runs in its own module so the interview has somewhere to live. It is a real conversation
    worth keeping — what the user said they cared about is the context every later module
    should be read against.
    """
    from agent.prompts import onboarding_prompt

    agent_store.create_subproject(
        package, "Onboarding",
        "what the user wants from this app, and the module breakdown that follows from it",
        status="approved")
    projects.ensure_project(package)
    serial = (payload.device_serial if payload else None) or state.device_serial()
    session = sessions.get(package, "onboarding", serial=serial)
    asyncio.create_task(session.send(onboarding_prompt(package)))
    return {"ok": True, "slug": "onboarding"}


@router.post("/agent/{package}/{slug}/stop")
async def stop_agent(package: str, slug: str):
    session = sessions.peek(package, slug)
    if session is None:
        # Nothing running is the outcome Stop was asking for, so this is success rather than
        # a 404 the UI has to explain. The old 404 is why pressing Stop on an idle module
        # printed an error and looked like the button was broken.
        return {"ok": True, "stopped": False, "note": "Nothing was running."}
    stopped = await session.interrupt()
    return {"ok": True, "stopped": stopped}


@router.get("/agent/{package}/{slug}/models")
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


@router.post("/agent/{package}/{slug}/model")
async def set_model(package: str, slug: str, payload: ModelPayload):
    """Move a module onto a different model. Reconnects, resuming the conversation."""
    if agent_store.get_subproject(package, slug) is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    session = sessions.get(package, slug)
    try:
        return await session.set_model(payload.model)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/agent/{package}/{slug}/findings")
async def get_findings(package: str, slug: str):
    return agent_store.list_findings(package, slug)


@router.post("/agent/{package}/secrets")
async def put_secret(package: str, payload: SecretPayload):
    """Store a test credential. Values are write-only over the API: the response lists names
    only, and the agent enters one via a tool without it ever entering the transcript."""
    if not payload.name.strip() or not payload.value:
        raise HTTPException(status_code=400, detail="name and value are required")
    agent_store.set_secret(package, payload.name.strip(), payload.value)
    return {"ok": True, "names": agent_store.secret_keys(package)}


@router.get("/agent/{package}/secrets")
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
