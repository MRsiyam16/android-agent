"""Telemetry in, graph out: the path a run's discoveries take to the browser.

POST /telemetry is the hot one — every step of every run lands here, gets named and
numbered, is filed into the owning project, and is broadcast to any connected dashboard.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from .. import naming, projects, state
from ..schemas import StatusPayload, TelemetryPayload

logger = logging.getLogger("server.telemetry")
router = APIRouter()


@router.post("/telemetry")
async def post_telemetry(payload: TelemetryPayload):
    action_label = (payload.executed_action or {}).get("label") if payload.executed_action else None
    naming.register_screen(payload.state_hash, payload.parent_state_hash, action_label,
                           payload.step_label)

    record = payload.model_dump()
    if not record.get("screenshot_b64"):
        # Agents skip re-capturing a screenshot for screens they've already reported (see
        # run_agent.py) — backfill from whatever we already have for this state_hash so a
        # blank revisit post never regresses an already-known screen to a blank image.
        existing = state.state_store.get(payload.state_hash)
        if existing and existing.get("screenshot_b64"):
            record["screenshot_b64"] = existing["screenshot_b64"]
    record["screen_name"] = naming.screen_names[payload.state_hash]
    record["screen_number"] = naming.node_index[payload.state_hash]
    state.state_store[payload.state_hash] = record
    state.telemetry_history.append(record)
    state.latest_state = record

    # A journey step is a link in a chain, so it earns an edge from its parent even with no
    # tap behind it — verdict/checkpoint steps have no action, and skipping them would break
    # the flow into disconnected fragments. Exploration still needs an action to draw an edge.
    if payload.parent_state_hash and (payload.executed_action or payload.step_label):
        edge_key = f"{payload.parent_state_hash}->{payload.state_hash}->{action_label}"
        naming.edge_index.setdefault(edge_key, {
            "from_hash": payload.parent_state_hash,
            "to_hash": payload.state_hash,
            "label": action_label or ("next" if payload.step_label else "?"),
        })

    # Everything that says "this belongs to project X" keys off the run's target, so a
    # screen the run merely passed through no longer creates a project of its own.
    owner = payload.target_package or payload.package_name
    record["target_package"] = owner
    if owner:
        projects.ensure_project(owner)
        projects.write_meta(owner, last_run_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        projects.save_screenshot_if_new(owner, payload.state_hash, payload.screenshot_b64)

    logger.info(
        "telemetry: pkg=%s state=%s..%s (#%d %s) elements=%d action=%s",
        payload.package_name, payload.state_hash[:8], payload.state_hash[-4:],
        record["screen_number"], record["screen_name"],
        len(payload.available_elements), action_label or "-",
    )

    await state.manager.broadcast({"type": "telemetry", "payload": record})
    return {"ok": True, "node_count": len(state.state_store),
            "history_count": len(state.telemetry_history)}


@router.post("/status")
async def post_status(payload: StatusPayload):
    logger.info("status[%s]: %s%s", payload.level, payload.message,
                " (popup)" if payload.popup else "")
    await state.manager.broadcast({
        "type": "status", "message": payload.message, "level": payload.level,
        "popup": payload.popup,
    })
    return {"ok": True}


@router.post("/clear")
async def clear_state():
    state.state_store.clear()
    state.telemetry_history.clear()
    state.latest_state = None
    naming.reset_screen_naming()
    await state.manager.broadcast({"type": "clear"})
    return {"ok": True}


@router.get("/state/{state_hash}")
async def get_state(state_hash: str):
    record = state.state_store.get(state_hash)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown state_hash")
    return record


@router.get("/map", response_class=PlainTextResponse)
async def text_map():
    """A compact, human/agent-readable snapshot of the whole discovered graph — screens
    with their breadcrumb names and sequential numbers, then every transition between
    them. Meant so an agent (or you) can read the entire flow in one request instead of
    walking state_store node by node. Built fresh from the current in-memory graph on
    each request — cheap, since the underlying naming/edge data is already maintained
    incrementally as telemetry arrives, not recomputed here."""
    lines = [
        "# App Flow Map",
        f"# {len(naming.node_order)} screens, {len(naming.edge_index)} transitions",
        "",
        "## Screens",
    ]
    for state_hash in naming.node_order:
        record = state.state_store.get(state_hash)
        if record is None:
            continue
        lines.append(
            f"[{naming.node_index[state_hash]}] {naming.screen_names[state_hash]}  "
            f"(package={record.get('package_name', '')}, "
            f"activity={record.get('activity_name', '')}, "
            f"hash={state_hash[:8]})"
        )

    lines += ["", "## Transitions"]
    for edge in naming.edge_index.values():
        from_name = naming.screen_names.get(edge["from_hash"], edge["from_hash"][:8])
        to_name = naming.screen_names.get(edge["to_hash"], edge["to_hash"][:8])
        lines.append(f"{from_name} --[{edge['label']}]--> {to_name}")

    return "\n".join(lines) + "\n"


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await state.manager.connect(websocket)
    try:
        await websocket.send_json({"type": "history", "items": state.history_for_replay()})
        while True:
            # Dashboard doesn't need to send anything up this channel; just keep the
            # connection alive and drain any pings/keepalive frames the client sends.
            await websocket.receive_text()
    except WebSocketDisconnect:
        state.manager.disconnect(websocket)
    except Exception:  # noqa: BLE001
        state.manager.disconnect(websocket)
