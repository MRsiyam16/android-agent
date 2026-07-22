"""FastAPI telemetry + remote-control server for the Android App Testing Agent dashboard."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
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
    record = payload.model_dump()
    state_store[payload.state_hash] = record
    telemetry_history.append(record)
    latest_state = record

    logger.info(
        "telemetry: pkg=%s state=%s..%s elements=%d action=%s",
        payload.package_name, payload.state_hash[:8], payload.state_hash[-4:],
        len(payload.available_elements),
        (payload.executed_action or {}).get("label") if payload.executed_action else "-",
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
    await manager.broadcast({"type": "clear"})
    return {"ok": True}


@app.get("/state/{state_hash}")
async def get_state(state_hash: str):
    record = state_store.get(state_hash)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown state_hash")
    return record


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
