"""FastAPI telemetry + remote-control server for the Android App Testing Agent dashboard."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("server")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

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


def _register_screen(state_hash: str, parent_hash: Optional[str], action_label: Optional[str]) -> None:
    """Assign a stable breadcrumb name + sequential number the first time a state is seen."""
    if state_hash in screen_names:
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


class CommandPayload(BaseModel):
    command: str
    device_serial: Optional[str] = None


class StatusPayload(BaseModel):
    session_id: Optional[str] = None
    message: str
    level: str = "info"


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
    _register_screen(payload.state_hash, payload.parent_state_hash, action_label)

    record = payload.model_dump()
    record["screen_name"] = screen_names[payload.state_hash]
    record["screen_number"] = node_index[payload.state_hash]
    state_store[payload.state_hash] = record
    telemetry_history.append(record)
    latest_state = record

    if payload.executed_action and payload.parent_state_hash:
        edge_key = f"{payload.parent_state_hash}->{payload.state_hash}->{action_label}"
        edge_index.setdefault(edge_key, {
            "from_hash": payload.parent_state_hash,
            "to_hash": payload.state_hash,
            "label": action_label or "?",
        })

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
    logger.info("status[%s]: %s", payload.level, payload.message)
    await manager.broadcast({"type": "status", "message": payload.message, "level": payload.level})
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
