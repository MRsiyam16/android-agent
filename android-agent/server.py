"""FastAPI telemetry + remote-control server for the Android App Testing Agent dashboard."""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import adb_device
import config
import project_paths
from agent import runtime as agent_runtime
from agent import store as agent_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("server")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
# Only the *default* home for projects now — one that has been pointed at a folder elsewhere
# lives wherever the registry says. Ask project_paths, never build a path from this.
PROJECTS_DIR = project_paths.DEFAULT_PROJECTS_DIR

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
# Bounded: this is replayed in full to every browser that connects, and it is the one
# structure that grows without limit for as long as the server is up.
telemetry_history: deque[dict[str, Any]] = deque(maxlen=config.TELEMETRY_HISTORY_LIMIT)
latest_state: dict[str, Any] | None = None
_device_cache: dict[str, Any] = {}


def _history_for_replay() -> list[dict[str, Any]]:
    """The history a newly-connected browser needs, with each screenshot sent once.

    In memory a revisit is cheap — the backfill above assigns the *same* base64 string
    object, so RAM holds one copy per capture. Serialising to JSON discards that sharing
    entirely: a screen visited forty times would put forty full copies of its JPEG on the
    wire, and the replay is exactly when a long session is most likely to stall the browser.

    Sending it on first sight only is enough, because the dashboard's `ingest()` already
    falls back to the screenshot it has for that state (see `existingMeta.screenshot`).
    """
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for record in telemetry_history:
        state_hash = record.get("state_hash", "")
        if state_hash in seen:
            if record.get("screenshot_b64"):
                record = {**record, "screenshot_b64": ""}
        else:
            seen.add(state_hash)
            if not record.get("screenshot_b64"):
                # First appearance in the replay, but the image arrived on a later post —
                # take it from the state store so the node is not left blank.
                known = state_store.get(state_hash) or {}
                if known.get("screenshot_b64"):
                    record = {**record, "screenshot_b64": known["screenshot_b64"]}
        items.append(record)
    return items

# --------------------------------------------------------------------------------------
# Projects: one local folder per app package (projects/<package>/), auto-populated as
# telemetry arrives — meta.json (bookkeeping), screenshots/<state_hash>.jpg (one file per
# newly-discovered state), memory.json (written by memory.py, not this module), and
# flow-graph.json (the same "project blob" shape the dashboard already builds for its
# manual Save/Import feature — see static/dashboard.js's saveBtn handler and loadProject()).
# --------------------------------------------------------------------------------------
# Both of these now defer to project_paths, which is the one place that knows a project may
# have been pointed at a folder outside this repo. Kept as thin aliases because they are
# called from a dozen places in this file and in the tests.
_safe_package_name = project_paths.safe_package_name


def _project_dir(package: str) -> Path:
    return project_paths.project_dir(package)


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
    # Where to put the project folder. Absent means the default `projects/<package>/`, which
    # is what telemetry from a bare `run_agent.py` still creates.
    root: Optional[str] = None


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
    # The app the run was pointed at. `package_name` is only whatever was on screen when the
    # frame was captured, and a run wanders out of its own app constantly — into the Play
    # Store, a browser, the permission controller. Filing by `package_name` is what scattered
    # screenshots into `com.android.vending` and `com.google.android.gms` folders and wrote a
    # deskclock board into Chrome's project. Optional so older clients still post.
    target_package: Optional[str] = None
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


class AttachmentPayload(BaseModel):
    """A reference image pasted or picked in the chat, as a base64 data URL."""
    data_url: str


class ModelPayload(BaseModel):
    """Which Claude model a module's session should run on. None means the CLI default."""
    model: Optional[str] = None


class AgentTriggerPayload(BaseModel):
    """For endpoints that start something rather than say something: /warm and /recon.

    They used to share `AgentMessagePayload`, whose `text` is required — so the `{}` the
    dashboard posts failed validation and both endpoints answered 422. Pre-warming swallowed
    it (it is an optimisation, and the client catches), which meant the advertised "your
    first message does not wait for the CLI to spawn" quietly never happened; Recon surfaced
    it as an error instead. Declaring what these actually accept fixes both.
    """
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

    # Everything that says "this belongs to project X" keys off the run's target, so a
    # screen the run merely passed through no longer creates a project of its own.
    owner = payload.target_package or payload.package_name
    record["target_package"] = owner
    if owner:
        _ensure_project(owner)
        _write_meta(owner, last_run_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        _save_screenshot_if_new(owner, payload.state_hash, payload.screenshot_b64)

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
    """All known projects (one per app package tested), newest-run-first.

    Enumerated by package rather than by walking one directory, because a project may have
    been pointed at a folder anywhere on disk. Each entry carries its root so the UI can show
    where it actually lives.
    """
    projects = []
    for package in project_paths.known_packages():
        meta = _read_meta(package)
        if meta is None:
            continue
        projects.append({**meta, **project_paths.describe(package)})
    projects.sort(key=lambda m: m.get("last_run_at") or "", reverse=True)
    return projects


@app.post("/projects")
async def create_project(payload: ProjectCreatePayload):
    """Idempotent: returns the existing project's meta if it's already there."""
    package = payload.package.strip()
    if not package:
        raise HTTPException(status_code=400, detail="package is required")
    if payload.root:
        # Registered before the meta is written, so `_write_meta` creates the folder in the
        # chosen place rather than making one under projects/ and leaving it orphaned.
        existing = project_paths.registered_root(package)
        if existing and Path(existing).resolve() != Path(payload.root).expanduser().resolve() \
                and _meta_path(package).is_file():
            raise HTTPException(
                status_code=409,
                detail=f"{package} already lives in {existing}. Move or delete that folder "
                       f"first if you want it somewhere else.")
        project_paths.register(package, payload.root)
    meta = _write_meta(package)
    return {**meta, **project_paths.describe(package)}


@app.post("/ui/pick-folder")
async def pick_folder():
    """Open the machine's own folder dialog and return what was chosen.

    A web page cannot hand the server a filesystem path: Chrome's directory picker yields a
    sandboxed handle that only JavaScript can read, and it is this process — not the browser —
    that writes screenshots, transcripts and findings. Since the server and the browser are
    the same machine here, the honest way to get a real path is to open a real dialog.

    Tk must own a thread with no event loop running on it, so this goes through a worker
    rather than the request's thread. `topmost` is not decoration: without it the dialog opens
    *behind* the browser window and the click looks like it did nothing at all.
    """
    def _ask() -> Optional[str]:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError as exc:  # a Python built without Tk
            raise HTTPException(
                status_code=501,
                detail="This Python has no tkinter, so the folder dialog cannot open. Type the "
                       "folder path instead.") from exc
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            chosen = filedialog.askdirectory(title="Choose a folder for this project",
                                             mustexist=False)
        finally:
            root.destroy()
        return chosen or None

    try:
        chosen = await asyncio.to_thread(_ask)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - any Tk failure should read as "type it instead"
        logger.warning("folder dialog failed: %s", exc)
        raise HTTPException(status_code=500,
                            detail=f"Could not open the folder dialog: {exc}") from exc
    # Cancelling is a normal outcome, not an error — the UI just leaves the field alone.
    return {"path": chosen, "cancelled": chosen is None}


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
    # The board carries the project it was loaded from. If that disagrees with the URL, the
    # browser is about to write one app's board over another's file — which is exactly how
    # the YouTube, Chrome and Keep boards were destroyed: autosave fired with a stale
    # `currentPackage` after a project switch, and the server took it without question.
    # Refuse rather than accept a save that is provably about to lose data.
    claimed = payload.get("package")
    if claimed and claimed != package:
        logger.warning("refused flow-graph save: board belongs to %s, URL said %s",
                       claimed, package)
        raise HTTPException(
            status_code=409,
            detail=f"This board belongs to {claimed}, not {package}. Refusing to overwrite "
                   f"{package}'s saved board with it.")
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
async def warm_agent(package: str, slug: str, payload: AgentTriggerPayload | None = None):
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


@app.post("/agent/{package}/{slug}/attachment")
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


@app.post("/agent/{package}/recon")
async def start_recon(package: str, payload: AgentTriggerPayload | None = None):
    """Kick off the recon pass that proposes the module breakdown for a new project."""
    from agent.prompts import recon_prompt

    agent_store.create_subproject(package, "Recon", "map the app and propose modules",
                                  status="approved")
    _ensure_project(package)
    serial = (payload.device_serial if payload else None) or \
        (latest_state or {}).get("device_serial")
    session = agent_sessions.get(package, "recon", serial=serial)
    asyncio.create_task(session.send(recon_prompt()))
    return {"ok": True, "slug": "recon"}


@app.post("/agent/{package}/onboarding")
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
    _ensure_project(package)
    serial = (payload.device_serial if payload else None) or \
        (latest_state or {}).get("device_serial")
    session = agent_sessions.get(package, "onboarding", serial=serial)
    asyncio.create_task(session.send(onboarding_prompt(package)))
    return {"ok": True, "slug": "onboarding"}


@app.post("/agent/{package}/{slug}/stop")
async def stop_agent(package: str, slug: str):
    session = agent_sessions.peek(package, slug)
    if session is None:
        # Nothing running is the outcome Stop was asking for, so this is success rather than
        # a 404 the UI has to explain. The old 404 is why pressing Stop on an idle module
        # printed an error and looked like the button was broken.
        return {"ok": True, "stopped": False, "note": "Nothing was running."}
    stopped = await session.interrupt()
    return {"ok": True, "stopped": stopped}


@app.get("/agent/{package}/{slug}/models")
async def list_models(package: str, slug: str):
    """Models this CLI can run, and which one this module is on.

    Read from the live session rather than hardcoded, so the list is whatever the installed
    CLI and the signed-in subscription actually offer.
    """
    session = agent_sessions.peek(package, slug)
    if session is None:
        return {"models": [], "current": None, "requested": None}
    return {"models": session.available_models,
            "current": session.model,
            "requested": session.requested_model}


@app.post("/agent/{package}/{slug}/model")
async def set_model(package: str, slug: str, payload: ModelPayload):
    """Move a module onto a different model. Reconnects, resuming the conversation."""
    if agent_store.get_subproject(package, slug) is None:
        raise HTTPException(status_code=404, detail="Unknown sub-project")
    session = agent_sessions.get(package, slug)
    try:
        return await session.set_model(payload.model)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/agent/{package}/{slug}/findings")
async def get_findings(package: str, slug: str):
    return agent_store.list_findings(package, slug)


@app.get("/projects/{package}/outcomes")
async def project_outcomes(package: str):
    """Every outcome across every module, bucketed by kind then module.

    Grouping happens here rather than in the browser so the counts on the top-bar pills and
    the contents of the popups can never disagree — they are the same traversal.
    """
    buckets: dict[str, dict[str, Any]] = {
        kind: {"kind": kind, "total": 0, "modules": {}}
        for kind in agent_store.FINDING_KINDS
    }
    for finding in agent_store.list_all_findings(package):
        bucket = buckets[finding["kind"]]
        module = bucket["modules"].setdefault(
            finding["module_slug"],
            {"slug": finding["module_slug"], "title": finding["module_title"], "items": []})
        module["items"].append(finding)
        bucket["total"] += 1
    return {
        "package": package,
        "counts": {kind: buckets[kind]["total"] for kind in agent_store.FINDING_KINDS},
        "buckets": {kind: {"total": buckets[kind]["total"],
                           "modules": list(buckets[kind]["modules"].values())}
                    for kind in agent_store.FINDING_KINDS},
    }


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


_device_info_cache: dict[str, Any] = {"at": 0.0, "value": None}


@app.get("/device/info")
async def device_info():
    """Which phone is attached, for the top bar.

    Cached for a few seconds because the dashboard polls this and every miss shells out to
    adb twice. The serial the *run* is using wins over the first one adb lists: with two
    devices attached, naming the wrong one in the chrome is worse than naming none.
    """
    now = time.monotonic()
    if _device_info_cache["value"] is not None and now - _device_info_cache["at"] < 5:
        return _device_info_cache["value"]

    def _probe() -> dict[str, Any]:
        try:
            serials = adb_device.list_serials()
        except adb_device.DeviceError as exc:
            return {"serial": None, "label": None, "count": 0, "error": str(exc)}
        if not serials:
            return {"serial": None, "label": None, "count": 0}
        active = (latest_state or {}).get("device_serial")
        serial = active if active in serials else serials[0]
        return {**adb_device.describe_serial(serial), "count": len(serials)}

    info = await asyncio.to_thread(_probe)
    _device_info_cache.update({"at": now, "value": info})
    return info


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
        await websocket.send_json({"type": "history", "items": _history_for_replay()})
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
