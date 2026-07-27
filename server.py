"""FastAPI telemetry + remote-control server for the Android App Testing Agent dashboard."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from agent import runtime as agent_runtime
from agent import store as agent_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("server")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
PROJECTS_DIR = BASE_DIR / "projects"

app = FastAPI(title="Android App Testing Agent — Telemetry Server")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(RequestValidationError)
async def log_validation_errors(request: Request, exc: RequestValidationError):
    logger.warning("422 on %s: %s", request.url.path, exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# --------------------------------------------------------------------------------------
# In-memory stores
# --------------------------------------------------------------------------------------
state_store: dict[str, dict[str, Any]] = {}
telemetry_history: list[dict[str, Any]] = []
latest_state: dict[str, Any] | None = None
_device_cache: dict[str, Any] = {}

# --------------------------------------------------------------------------------------
# Projects: one local folder per app package (projects/<package>/), auto-populated as
# telemetry arrives — meta.json (bookkeeping), screenshots/<state_hash>.jpg (one file per
# newly-discovered state), memory.json (written by memory.py, not this module), and
# flow-graph.json (the same "project blob" shape the dashboard already builds for its
# manual Save/Import feature — see static/dashboard.js's saveBtn handler and loadProject()).
# --------------------------------------------------------------------------------------
def _safe_package_name(package: str) -> str:
    return "".join(c if (c.isalnum() or c in ".-_") else "_" for c in package) or "unknown"


def _project_dir(package: str) -> Path:
    return PROJECTS_DIR / _safe_package_name(package)


def _screenshots_dir(package: str) -> Path:
    return _project_dir(package) / "screenshots"


def _meta_path(package: str) -> Path:
    return _project_dir(package) / "meta.json"


def _flow_graph_path(package: str) -> Path:
    return _project_dir(package) / "flow-graph.json"


def _read_meta(package: str) -> dict[str, Any] | None:
    path = _meta_path(package)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read project meta for %s: %s", package, exc)
        return None


def _write_meta(package: str, **updates: Any) -> dict[str, Any]:
    """Merge-update meta.json for a project, creating it (and the project dir) if needed."""
    meta = _read_meta(package) or {
        "package": package,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_run_at": None,
        "last_saved_at": None,
        "state_count": 0,
        "edge_count": 0,
    }
    meta.update(updates)
    try:
        _project_dir(package).mkdir(parents=True, exist_ok=True)
        _meta_path(package).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write project meta for %s: %s", package, exc)
    return meta


def _ensure_project(package: str) -> None:
    """Auto-create a project the first time telemetry arrives for a package."""
    if _meta_path(package).is_file():
        return
    _write_meta(package)


def _save_screenshot_if_new(package: str, state_hash: str, screenshot_b64: str) -> None:
    if not screenshot_b64:
        return
    dest = _screenshots_dir(package) / f"{state_hash}.jpg"
    if dest.exists():
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(screenshot_b64))
    except (OSError, binascii.Error) as exc:
        logger.warning("Could not save screenshot for %s/%s: %s", package, state_hash[:8], exc)


class ProjectCreatePayload(BaseModel):
    package: str


# --------------------------------------------------------------------------------------
# Screen naming (breadcrumb paths) + graph-for-agents bookkeeping.
#
# Built once per state_hash, the first time it's seen, so a screen's name/number never
# changes across a session even as more telemetry comes in for it. This is also the single
# source of truth consumed by both the dashboard (node header labels) and the text-only
# /map endpoint below, so the two views never disagree about what a screen is called.
# --------------------------------------------------------------------------------------
_GENERIC_LABELS = {
    "relativelayout", "framelayout", "linearlayout", "view", "imageview",
    "textview", "button", "edittext", "calculator input field", "result preview",
    "unlabeled element",
}
_NUMERIC_ISH_RE = re.compile(r"^[\d+\-×÷=%.()]+$")
_WORDY_RE = re.compile(r"^[a-zA-Z][a-zA-Z\s]*$")


def _is_section_trigger(label: Optional[str]) -> bool:
    """Heuristic: is this action label meaningful enough to name a new section after
    (e.g. "Settings", "Search") rather than generic chrome (e.g. "Button", "3+4=")?"""
    if not label:
        return False
    norm = label.strip().lower()
    if norm in _GENERIC_LABELS:
        return False
    if _NUMERIC_ISH_RE.match(norm):
        return False
    if len(norm) <= 1:
        return False
    return bool(_WORDY_RE.match(label.strip()))


screen_paths: dict[str, list[str]] = {}       # state_hash -> breadcrumb segments, e.g. ["Settings", "Notifications"]
screen_names: dict[str, str] = {}             # state_hash -> display name, e.g. "Settings > Notifications: 2"
_path_leaf_counts: dict[tuple, int] = {}      # breadcrumb tuple -> how many states share that exact path
node_order: list[str] = []                   # state_hash, in first-discovery order
node_index: dict[str, int] = {}               # state_hash -> 1-based sequential screen number
edge_index: dict[str, dict[str, Any]] = {}    # "from->to->label" -> {from_hash, to_hash, label}


def _register_screen(
    state_hash: str,
    parent_hash: Optional[str],
    action_label: Optional[str],
    explicit_name: Optional[str] = None,
) -> None:
    """Assign a stable breadcrumb name + sequential number the first time a state is seen.

    `explicit_name` is used verbatim when the caller already knows what the node means —
    scripted journeys (see `journey.py`) name each step themselves ("3. Tap 5 -> 7+5"),
    which is far more informative than a breadcrumb derived from keypad labels. Autonomous
    exploration passes nothing here and keeps the derived-breadcrumb behaviour."""
    if state_hash in screen_names:
        return

    if explicit_name:
        screen_paths[state_hash] = [explicit_name]
        screen_names[state_hash] = explicit_name
        node_order.append(state_hash)
        node_index[state_hash] = len(node_order)
        return

    parent_path = screen_paths.get(parent_hash, []) if parent_hash else []
    if _is_section_trigger(action_label):
        path = [*parent_path, action_label.strip()]
    elif parent_path:
        path = parent_path
    else:
        path = ["Home"]

    leaf_key = tuple(path)
    count = _path_leaf_counts.get(leaf_key, 0) + 1
    _path_leaf_counts[leaf_key] = count

    name = " > ".join(path)
    if count > 1:
        name = f"{name}: {count}"

    screen_paths[state_hash] = path
    screen_names[state_hash] = name
    node_order.append(state_hash)
    node_index[state_hash] = len(node_order)


def _reset_screen_naming() -> None:
    screen_paths.clear()
    screen_names.clear()
    _path_leaf_counts.clear()
    node_order.clear()
    node_index.clear()
    edge_index.clear()


class TelemetryPayload(BaseModel):
    session_id: str
    device_serial: Optional[str] = None
    package_name: str
    activity_name: str = ""
    state_hash: str
    parent_state_hash: Optional[str] = None
    screenshot_b64: str = ""
    available_elements: list[dict] = []
    executed_action: Optional[dict] = None
    # Scripted-journey extras. A journey posts one node per step (its `state_hash` is a
    # per-step id, not a structural hash) so the flow renders as the ordered chain the
    # test actually walked, instead of collapsing onto one self-looping screen.
    step_label: Optional[str] = None
    section: Optional[str] = None
    # The structural hash of the screen this step landed on, so a journey step can still be
    # correlated back to a screen discovered by autonomous exploration.
    state_hash_struct: Optional[str] = None


class CommandPayload(BaseModel):
    command: str
    device_serial: Optional[str] = None


class StatusPayload(BaseModel):
    session_id: Optional[str] = None
    message: str
    level: str = "info"
    popup: bool = False


# --------------------------------------------------------------------------------------
# WebSocket connection manager
# --------------------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# --------------------------------------------------------------------------------------
# Chat agent (Agent tab). Sessions live in this process: the planner is the Claude Code CLI
# driven through claude-agent-sdk, and the device tools are in-process MCP tools, so an
# agent's every step can be pushed straight out over the WebSocket above.
# --------------------------------------------------------------------------------------
async def _agent_emit(event: dict[str, Any]) -> None:
    await manager.broadcast(event)


agent_sessions = agent_runtime.SessionRegistry(_agent_emit)


class AgentMessagePayload(BaseModel):
    text: str
    device_serial: Optional[str] = None


class SubprojectPayload(BaseModel):
    title: str
    scope: str = ""


class SubprojectUpdatePayload(BaseModel):
    title: Optional[str] = None
    scope: Optional[str] = None
    status: Optional[str] = None


class SecretPayload(BaseModel):
    name: str
    value: str


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------
@app.get("/")
async def dashboard():
    return FileResponse(TEMPLATES_DIR / "dashboard.html")


@app.get("/favicon.ico")
async def favicon():
    path = STATIC_DIR / "favicon.ico"
    if path.exists():
        return FileResponse(path)
    raise HTTPException(status_code=404)


@app.post("/telemetry")
async def post_telemetry(payload: TelemetryPayload):
    global latest_state
    action_label = (payload.executed_action or {}).get("label") if payload.executed_action else None
    _register_screen(payload.state_hash, payload.parent_state_hash, action_label, payload.step_label)

    record = payload.model_dump()
    if not record.get("screenshot_b64"):
        # Agents skip re-capturing a screenshot for screens they've already reported (see
        # run_agent.py) — backfill from whatever we already have for this state_hash so a
        # blank revisit post never regresses an already-known screen to a blank image.
        existing = state_store.get(payload.state_hash)
        if existing and existing.get("screenshot_b64"):
            record["screenshot_b64"] = existing["screenshot_b64"]
    record["screen_name"] = screen_names[payload.state_hash]
    record["screen_number"] = node_index[payload.state_hash]
    state_store[payload.state_hash] = record
    telemetry_history.append(record)
    latest_state = record

    # A journey step is a link in a chain, so it earns an edge from its parent even with no
    # tap behind it — verdict/checkpoint steps have no action, and skipping them would break
    # the flow into disconnected fragments. Exploration still needs an action to draw an edge.
    if payload.parent_state_hash and (payload.executed_action or payload.step_label):
        edge_key = f"{payload.parent_state_hash}->{payload.state_hash}->{action_label}"
        edge_index.setdefault(edge_key, {
            "from_hash": payload.parent_state_hash,
            "to_hash": payload.state_hash,
            "label": action_label or ("next" if payload.step_label else "?"),
        })

    if payload.package_name:
        _ensure_project(payload.package_name)
        _write_meta(payload.package_name, last_run_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        _save_screenshot_if_new(payload.package_name, payload.state_hash, payload.screenshot_b64)

    logger.info(
        "telemetry: pkg=%s state=%s..%s (#%d %s) elements=%d action=%s",
        payload.package_name, payload.state_hash[:8], payload.state_hash[-4:],
        record["screen_number"], record["screen_name"],
        len(payload.available_elements), action_label or "-",
    )

    await manager.broadcast({"type": "telemetry", "payload": record})
    return {"ok": True, "node_count": len(state_store), "history_count": len(telemetry_history)}


@app.post("/status")
async def post_status(payload: StatusPayload):
    logger.info("status[%s]: %s%s", payload.level, payload.message, " (popup)" if payload.popup else "")
    await manager.broadcast({
        "type": "status", "message": payload.message, "level": payload.level, "popup": payload.popup,
    })
    return {"ok": True}


@app.post("/clear")
async def clear_state():
    global latest_state
    state_store.clear()
    telemetry_history.clear()
    latest_state = None
    _reset_screen_naming()
    await manager.broadcast({"type": "clear"})
    return {"ok": True}


@app.get("/projects")
async def list_projects():
    """All known projects (one per app package tested), newest-run-first."""
    if not PROJECTS_DIR.is_dir():
        return []
    projects = []
    for entry in PROJECTS_DIR.iterdir():
        if not entry.is_dir():
            continue
        meta = _read_meta(entry.name)
        if meta is not None:
            projects.append(meta)
    projects.sort(key=lambda m: m.get("last_run_at") or "", reverse=True)
    return projects


@app.post("/projects")
async def create_project(payload: ProjectCreatePayload):
    """Idempotent: returns the existing project's meta if it's already there."""
    if not payload.package.strip():
        raise HTTPException(status_code=400, detail="package is required")
    meta = _write_meta(payload.package)
    return meta


@app.get("/projects/{package}/flow-graph")
async def get_project_flow_graph(package: str):
    path = _flow_graph_path(package)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No saved flow graph for this project yet")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read flow graph: {exc}") from exc


@app.post("/projects/{package}/flow-graph")
async def save_project_flow_graph(package: str, payload: dict):
    try:
        _project_dir(package).mkdir(parents=True, exist_ok=True)
        _flow_graph_path(package).write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not save flow graph: {exc}") from exc

    _write_meta(
        package,
        last_saved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        state_count=len(payload.get("nodes") or []),
        edge_count=len(payload.get("edges") or []),
    )
    return {"ok": True}


@app.get("/state/{state_hash}")
async def get_state(state_hash: str):
    record = state_store.get(state_hash)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown state_hash")
    return record


@app.get("/map", response_class=PlainTextResponse)
async def text_map():
    """A compact, human/agent-readable snapshot of the whole discovered graph — screens
    with their breadcrumb names and sequential numbers, then every transition between
    them. Meant so an agent (or you) can read the entire flow in one request instead of
    walking state_store node by node. Built fresh from the current in-memory graph on
    each request — cheap, since the underlying naming/edge data is already maintained
    incrementally as telemetry arrives, not recomputed here."""
    lines = [
        "# App Flow Map",
        f"# {len(node_order)} screens, {len(edge_index)} transitions",
        "",
        "## Screens",
    ]
    for state_hash in node_order:
        record = state_store.get(state_hash)
        if record is None:
            continue
        lines.append(
            f"[{node_index[state_hash]}] {screen_names[state_hash]}  "
            f"(package={record.get('package_name', '')}, activity={record.get('activity_name', '')}, "
            f"hash={state_hash[:8]})"
        )

    lines += ["", "## Transitions"]
    for edge in edge_index.values():
        from_name = screen_names.get(edge["from_hash"], edge["from_hash"][:8])
        to_name = screen_names.get(edge["to_hash"], edge["to_hash"][:8])
        lines.append(f"{from_name} --[{edge['label']}]--> {to_name}")

    return "\n".join(lines) + "\n"


def _resolve_device(serial: Optional[str]):
    """Lazily connect (and cache) a uiautomator2 session for remote control commands."""
    import uiautomator2 as u2

    key = serial or "__default__"
    if key not in _device_cache:
        try:
            _device_cache[key] = u2.connect(serial) if serial else u2.connect()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Could not connect to device: {exc}") from exc
    return _device_cache[key]


def _screenshot_b64(d) -> str:
    import base64
    import io
    img = d.screenshot(format="pillow")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=config.SCREENSHOT_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@app.post("/command")
async def run_command(payload: CommandPayload):
    serial = payload.device_serial or (latest_state or {}).get("device_serial")
    d = _resolve_device(serial)

    raw = payload.command.strip()
    parts = raw.split()
    if not parts:
        raise HTTPException(status_code=400, detail="Empty command")
    verb = parts[0].lower()

    try:
        if verb == "screenshot":
            pass  # no-op: just capture the current frame below

        elif verb == "tap" and len(parts) >= 3:
            x, y = int(parts[1]), int(parts[2])
            d.click(x, y)

        elif verb == "click" and len(parts) >= 2:
            target = raw[len("click"):].strip().strip('"').strip("'")
            elements = (latest_state or {}).get("available_elements", [])
            match = next(
                (e for e in elements if target.lower() in (e.get("label") or "").lower()),
                None,
            )
            if not match:
                raise HTTPException(status_code=404, detail=f"No known element matches '{target}'")
            d.click(match["x"], match["y"])

        elif verb == "type" and len(parts) >= 2:
            text = raw[len("type"):].strip()
            d.send_keys(text)

        elif verb == "back":
            d.press("back")

        elif verb == "home":
            d.press("home")

        elif verb == "launch" and len(parts) >= 2:
            package = parts[1]
            d.app_start(package, stop=True)

        else:
            raise HTTPException(status_code=400, detail=f"Unrecognized command: '{raw}'")

    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Command failed: {exc}") from exc

    try:
        screenshot_b64 = _screenshot_b64(d)
    except Exception as exc:  # noqa: BLE001
        logger.warning("post-command screenshot failed: %s", exc)
        screenshot_b64 = ""

    return {"ok": True, "command": raw, "screenshot_b64": screenshot_b64}


# --------------------------------------------------------------------------------------
# Agent routes
# --------------------------------------------------------------------------------------
@app.get("/agent/status")
async def agent_status():
    """Which sessions are live, busy, blocked on a question, or parked on a rate limit."""
    return {
        "sessions": agent_sessions.status(),
        "planner": "claude-code-cli (subscription)",
        "cheap_tier": config.AGENT_USE_CHEAP_TIER,
        "stepper_model": config.AGENT_STEPPER_MODEL if config.AGENT_USE_CHEAP_TIER else None,
        "last_opened": agent_store.get_last_opened(),
    }


@app.post("/agent/{package}/{slug}/warm")
async def warm_agent(package: str, slug: str, payload: AgentMessagePayload | None = None):
    """Spawn the module's Claude Code session now, without sending it anything.

    Called on startup for the last-used module and again whenever you select one in the UI, so
    the CLI's spawn cost is paid while you are still typing rather than after you hit send.
    """
    if agent_store.get_subproject(package, slug) is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    agent_store.set_last_opened(package, slug)
    serial = (payload.device_serial if payload else None) or \
        (latest_state or {}).get("device_serial")
    try:
        return await agent_sessions.warm(package, slug, serial=serial)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/agent/{package}/subprojects")
async def list_subprojects(package: str):
    return agent_store.list_subprojects(package)


@app.post("/agent/{package}/subprojects")
async def create_subproject(package: str, payload: SubprojectPayload):
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    _ensure_project(package)
    return agent_store.create_subproject(package, payload.title, payload.scope,
                                         status="approved")


@app.patch("/agent/{package}/subprojects/{slug}")
async def patch_subproject(package: str, slug: str, payload: SubprojectUpdatePayload):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    entry = agent_store.update_subproject(package, slug, **updates)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    await manager.broadcast({"type": "agent_subproject_updated", "package": package,
                             "subproject": entry})
    return entry


@app.delete("/agent/{package}/subprojects/{slug}")
async def remove_subproject(package: str, slug: str):
    """Removes it from the list only. The transcript, findings and evidence stay on disk —
    a mis-click should not be able to destroy a test history."""
    if not agent_store.delete_subproject(package, slug):
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    await agent_sessions.close(package, slug)
    return {"ok": True, "note": "Folder kept on disk; only the listing entry was removed."}


@app.get("/agent/{package}/{slug}/chat")
async def get_chat(package: str, slug: str, limit: int = 400):
    session = agent_sessions.peek(package, slug)
    return {
        "messages": agent_store.read_chat(package, slug, limit=limit),
        "findings": agent_store.list_findings(package, slug),
        "busy": bool(session and session.busy),
        "blocked": session.device.pending_question if session else None,
        "parked": session.parked_reason if session else None,
    }


@app.post("/agent/{package}/{slug}/message")
async def post_message(package: str, slug: str, payload: AgentMessagePayload):
    """Hand a message to the agent and return immediately.

    The turn runs as a background task because it can last many minutes; everything it does
    arrives over the WebSocket. Holding the HTTP request open for the whole run would hit
    proxy timeouts and give the browser nothing to show in the meantime.
    """
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    if agent_store.get_subproject(package, slug) is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project — create it first")
    serial = payload.device_serial or (latest_state or {}).get("device_serial")
    session = agent_sessions.get(package, slug, serial=serial)
    asyncio.create_task(session.send(payload.text))
    return {"ok": True, "accepted": True}


@app.post("/agent/{package}/recon")
async def start_recon(package: str, payload: AgentMessagePayload | None = None):
    """Kick off the recon pass that proposes the module breakdown for a new project."""
    from agent.prompts import RECON_PROMPT

    agent_store.create_subproject(package, "Recon", "map the app and propose modules",
                                  status="approved")
    _ensure_project(package)
    serial = (payload.device_serial if payload else None) or \
        (latest_state or {}).get("device_serial")
    session = agent_sessions.get(package, "recon", serial=serial)
    asyncio.create_task(session.send(RECON_PROMPT))
    return {"ok": True, "slug": "recon"}


@app.post("/agent/{package}/{slug}/stop")
async def stop_agent(package: str, slug: str):
    session = agent_sessions.peek(package, slug)
    if session is None:
        raise HTTPException(status_code=404, detail="No live session for this sub-project")
    stopped = await session.interrupt()
    return {"ok": True, "stopped": stopped}


@app.get("/agent/{package}/{slug}/findings")
async def get_findings(package: str, slug: str):
    return agent_store.list_findings(package, slug)


@app.post("/agent/{package}/secrets")
async def put_secret(package: str, payload: SecretPayload):
    """Store a test credential. Values are write-only over the API: the response lists names
    only, and the agent enters one via a tool without it ever entering the transcript."""
    if not payload.name.strip() or not payload.value:
        raise HTTPException(status_code=400, detail="name and value are required")
    agent_store.set_secret(package, payload.name.strip(), payload.value)
    return {"ok": True, "names": agent_store.secret_keys(package)}


@app.get("/agent/{package}/secrets")
async def list_secrets(package: str):
    return {"names": agent_store.secret_keys(package)}


@app.get("/agent/shot")
async def get_shot(path: str):
    """Serve a screenshot the agent captured, for the chat thumbnails.

    Confined to the projects/ tree: `path` arrives from the browser, so it is resolved and
    checked before being opened rather than trusted.
    """
    try:
        resolved = Path(path).resolve()
        root = agent_store.PROJECTS_DIR.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise HTTPException(status_code=404, detail="Not an agent screenshot")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Bad path: {exc}") from exc
    return FileResponse(resolved, media_type="image/jpeg")


@app.get("/device/frame")
async def device_frame(package: str | None = None, slug: str | None = None):
    """A single frame of the phone for the Agent tab's live view.

    Reuses the agent's own device session when one exists, so watching the screen does not
    open a second uiautomator2 connection to the same phone while the agent is mid-tap.
    """
    session = agent_sessions.peek(package, slug) if package and slug else None
    try:
        if session is not None:
            device = await session.device.device()
            b64 = await session.device.run(device.screenshot_b64)
        else:
            d = _resolve_device(None)
            b64 = await asyncio.to_thread(_screenshot_b64, d)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not grab a frame: {exc}") from exc
    return {"screenshot_b64": b64}


@app.on_event("startup")
async def _prewarm_agent() -> None:
    """Bring up a Claude Code session for the last-used module in the background.

    Deliberately fire-and-forget: a slow or failed CLI spawn must not delay the server
    binding its port, and the dashboard is perfectly usable without the agent.
    """
    target = agent_store.get_last_opened()
    if not target:
        return

    async def _warm() -> None:
        try:
            result = await agent_sessions.warm(target["package"], target["slug"])
            logger.info("pre-warmed agent session for %s/%s (model=%s)",
                        target["package"], target["slug"], result.get("model"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not pre-warm the agent session: %s", exc)

    asyncio.create_task(_warm())


@app.on_event("shutdown")
async def _close_agent_sessions() -> None:
    await agent_sessions.close_all()


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_json({"type": "history", "items": telemetry_history})
        while True:
            # Dashboard doesn't need to send anything up this channel; just keep the
            # connection alive and drain any pings/keepalive frames the client sends.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:  # noqa: BLE001
        manager.disconnect(websocket)


if __name__ == "__main__":
    uvicorn.run("server:app", host=config.SERVER_HOST, port=config.SERVER_PORT, reload=False)
