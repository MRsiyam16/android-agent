"""Driving the phone directly: the remote-control panel, the top-bar device chip, and the
Agent tab's live frame.

Distinct from the agent's own device tools (agent/device_tools.py), which run inside a
module's session with this harness's failure modes built in as guardrails. These three
endpoints are the dashboard reaching for the phone on its own.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException

import device

from .. import agent_bridge, devices, state
from ..schemas import CommandPayload

logger = logging.getLogger("server.device")
router = APIRouter()


@router.post("/command")
async def run_command(payload: CommandPayload):
    """Run one remote-control command and return the frame it produced.

    Every uiautomator2 call here goes through a worker thread, including the connect. They
    are all blocking, a tap plus a screenshot is a few hundred milliseconds, and `u2.connect`
    on a sleeping phone is far worse — `device_tools.DeviceSession.device` bounds exactly
    that call because unbounded it "would freeze the chat with no error at all". On the event
    loop they take the WebSocket down with them, which is what feeds the dashboard the very
    frames this endpoint exists to produce. `/device/info` and `/device/frame` below already
    thread their work; this endpoint was the one that did not.
    """
    serial = payload.device_serial or state.device_serial()
    d = await asyncio.to_thread(devices.resolve_device, serial)

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
            await asyncio.to_thread(d.click, x, y)

        elif verb == "click" and len(parts) >= 2:
            target = raw[len("click"):].strip().strip('"').strip("'")
            elements = (state.latest_state or {}).get("available_elements", [])
            match = next(
                (e for e in elements if target.lower() in (e.get("label") or "").lower()),
                None,
            )
            if not match:
                raise HTTPException(status_code=404, detail=f"No known element matches '{target}'")
            await asyncio.to_thread(d.click, match["x"], match["y"])

        elif verb == "type" and len(parts) >= 2:
            text = raw[len("type"):].strip()
            await asyncio.to_thread(d.send_keys, text)

        elif verb == "back":
            await asyncio.to_thread(d.press, "back")

        elif verb == "home":
            await asyncio.to_thread(d.press, "home")

        elif verb == "launch" and len(parts) >= 2:
            package = parts[1]
            await asyncio.to_thread(d.start_app, package)

        else:
            raise HTTPException(status_code=400, detail=f"Unrecognized command: '{raw}'")

    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Command failed: {exc}") from exc

    try:
        shot = await asyncio.to_thread(d.screenshot_b64)
    except Exception as exc:  # noqa: BLE001
        logger.warning("post-command screenshot failed: %s", exc)
        shot = ""

    return {"ok": True, "command": raw, "screenshot_b64": shot}


_device_info_cache: dict[str, Any] = {"at": 0.0, "value": None}


@router.get("/device/info")
async def device_info():
    """Which device is attached, for the top bar — Android or iOS, whichever answers.

    Cached for a few seconds because the dashboard polls this and every miss shells out to
    adb and pymobiledevice3. The serial the *run* is using wins over the first one listed:
    with two devices attached, naming the wrong one in the chrome is worse than naming none.
    """
    now = time.monotonic()
    if _device_info_cache["value"] is not None and now - _device_info_cache["at"] < 5:
        return _device_info_cache["value"]

    def _probe() -> dict[str, Any]:
        found = device.list_devices()
        if not found:
            return {"serial": None, "label": None, "count": 0}
        active = state.device_serial()
        match = next((d for d in found if d["serial"] == active), None) or found[0]
        return {**match, "count": len(found)}

    info = await asyncio.to_thread(_probe)
    _device_info_cache.update({"at": now, "value": info})
    return info


@router.get("/device/frame")
async def device_frame(package: str | None = None, slug: str | None = None):
    """A single frame of the phone for the Agent tab's live view.

    Reuses the agent's own device session when one exists, so watching the screen does not
    open a second uiautomator2 connection to the same phone while the agent is mid-tap.
    """
    session = agent_bridge.sessions.peek(package, slug) if package and slug else None
    try:
        if session is not None:
            device = await session.device.device()
            b64 = await session.device.run(device.screenshot_b64)
        else:
            d = await asyncio.to_thread(devices.resolve_device, None)
            b64 = await asyncio.to_thread(devices.screenshot_b64, d)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not grab a frame: {exc}") from exc
    return {"screenshot_b64": b64}
