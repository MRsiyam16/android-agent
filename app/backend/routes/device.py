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

import config
import device

from .. import agent_bridge, devices, projects, state
from ..schemas import (CommandPayload, ResponsiveSweepPayload, ViewportPayload,
                       WindowsResetPayload)

logger = logging.getLogger("server.device")
router = APIRouter()


async def _resolve_for_project(package: str | None, slug: str | None, serial: str | None):
    """The right device for a dashboard-initiated action (remote control, viewport, sweep).

    Prefers the module's own live agent session when one exists. For a web project this is
    not an optimisation but a correctness requirement: each session owns exactly one running
    browser, and resolving through `devices.resolve_device()` instead would launch a second,
    disconnected browser rather than reaching the one already open and visible on screen.
    Falls back to the shared device cache — keyed by serial, same as ever — for the common
    Android/iOS case of "no module open yet, just talk to whatever is plugged in."
    """
    if package and slug:
        session = agent_bridge.sessions.peek(package, slug)
        if session is not None:
            return await session.device.device()
    meta = projects.read_meta(package) or {} if package else {}
    platform = meta.get("platform")
    eff_serial = serial
    if not eff_serial and (platform or "").lower() == "web":
        eff_serial = package  # a web project's "serial" is its URL, i.e. its package
    if not eff_serial and (platform or "").lower() == "windows":
        # A VM name, unlike an adb serial or iOS UDID, is never "whatever's attached" — it
        # must come from the project's own pin, same rule agent_bridge.device_for() already
        # applies for the agent-session path.
        eff_serial = meta.get("device_serial")
    if not eff_serial:
        eff_serial = state.device_serial()
    return await asyncio.to_thread(devices.resolve_device, eff_serial, platform)


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
    d = await _resolve_for_project(payload.package, payload.slug, payload.device_serial)

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

        elif verb == "reload":
            # Web-only in practice — the dashboard hides this chip for Android/iOS — but
            # dispatches through the same `press()` the other verbs use rather than a new
            # device-protocol method, since only one platform implements it.
            await asyncio.to_thread(d.press, "reload")

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
async def device_frame(package: str | None = None, slug: str | None = None, resync: bool = False):
    """A single frame of the phone for the Agent tab's live view, plus its current screen
    size in points.

    The dashboard uses `width`/`height` to size the frame to the device's actual aspect
    ratio instead of a fixed phone shape — an iPad is nowhere near 9:19.5. `resync=true` is
    the dashboard's "match device" refresh: `IOSDevice.window_size` caches after first read
    (re-querying WDA on every tap would cost a round trip each time), so a rotation is
    invisible to it until something explicitly asks for a fresh read. Android's window_size
    is never cached, so `resync` is a no-op there — `getattr` skips it rather than branching
    on platform.

    Reuses the agent's own device session when one exists, so watching the screen does not
    open a second connection to the same phone while the agent is mid-tap.
    """
    session = agent_bridge.sessions.peek(package, slug) if package and slug else None
    try:
        if session is not None:
            dev = await session.device.device()
            if resync:
                refresh = getattr(dev, "refresh_window_size", None)
                if refresh is not None:
                    await session.device.run(refresh)
            b64 = await session.device.run(dev.screenshot_b64)
            w, h = await session.device.run(lambda: dev.window_size)
        else:
            dev = await asyncio.to_thread(devices.resolve_device, None)
            if resync:
                refresh = getattr(dev, "refresh_window_size", None)
                if refresh is not None:
                    await asyncio.to_thread(refresh)
            b64 = await asyncio.to_thread(dev.screenshot_b64)
            w, h = await asyncio.to_thread(lambda: dev.window_size)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not grab a frame: {exc}") from exc
    return {"screenshot_b64": b64, "width": w, "height": h}


@router.post("/device/viewport")
async def set_viewport(payload: ViewportPayload):
    """Resize a web project's browser viewport — the mobile/tablet/desktop/custom picker.

    Deliberately not a `Device` protocol method (see `device.py`'s own rule: add one there
    only once every adapter implements it). `set_viewport` is web-only, so this checks for it
    with `getattr` instead — the same pattern `/device/frame` already uses above for iOS-only
    `refresh_window_size`.
    """
    d = await _resolve_for_project(payload.package, payload.slug, None)
    setter = getattr(d, "set_viewport", None)
    if setter is None:
        raise HTTPException(
            status_code=400, detail="This device does not support resizing its viewport.")
    try:
        w, h = await asyncio.to_thread(setter, payload.width, payload.height)
        b64 = await asyncio.to_thread(d.screenshot_b64)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not resize the viewport: {exc}") from exc
    return {"ok": True, "width": w, "height": h, "screenshot_b64": b64}


@router.post("/device/windows/restore-snapshot")
async def restore_windows_snapshot(payload: WindowsResetPayload):
    """Manually reset a Windows target to its clean VM snapshot.

    Deliberately not the agent's `reset_app_data` tool, and deliberately no timeout wrapper
    here — a VirtualBox snapshot restore reboots the whole guest and routinely takes minutes,
    which is exactly why this lives on its own route instead of overloading a tool the agent
    can call mid-conversation. Same `getattr`-gated "adapter has more capability than the
    shared Device Protocol" shape as `/device/viewport` above.
    """
    d = await _resolve_for_project(payload.package, payload.slug, payload.device_serial)
    restore = getattr(d, "restore_snapshot", None)
    if restore is None:
        raise HTTPException(
            status_code=400, detail="This device does not support a snapshot restore.")
    snapshot_name = payload.snapshot_name
    if not snapshot_name and payload.package:
        snapshot_name = (projects.read_meta(payload.package) or {}).get("snapshot_name")
    try:
        await asyncio.to_thread(restore, snapshot_name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Snapshot restore failed: {exc}") from exc
    return {"ok": True}


@router.post("/device/responsive-sweep")
async def responsive_sweep(payload: ResponsiveSweepPayload):
    """The dashboard's one-click breakpoint sweep — presentation only, files nothing.

    Deliberately does not write to `findings.json`: this route has no `(package, slug)` module
    context the way `agent/store.add_finding` requires, and improvising one would either force
    a spurious module picker or mis-attribute a finding to whichever module happens to be
    selected. The agent's own `check_responsive` tool (agent/device_tools.py) runs the same
    sweep and is the actual filing path, through `record_finding` like everything else.

    Restores the viewport to whatever it was before the sweep, same as the agent tool does.
    """
    d = await _resolve_for_project(payload.package, payload.slug, None)
    setter = getattr(d, "set_viewport", None)
    checker = getattr(d, "responsive_issues", None)
    if setter is None or checker is None:
        raise HTTPException(
            status_code=400, detail="This device does not support a responsive sweep.")

    raw = payload.breakpoints or [
        {"name": name, "width": w, "height": h}
        for name, (w, h) in config.WEB_BREAKPOINTS.items()]
    try:
        original = await asyncio.to_thread(lambda: d.window_size)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not read the viewport: {exc}") from exc

    results = []
    try:
        for bp in raw:
            name, width, height = str(bp.get("name") or ""), int(bp["width"]), int(bp["height"])
            try:
                await asyncio.to_thread(setter, width, height)
                await asyncio.sleep(0.4)  # let the re-layout settle, same as the agent tool
                b64 = await asyncio.to_thread(d.screenshot_b64)
                issues = await asyncio.to_thread(checker)
            except Exception as exc:  # noqa: BLE001
                results.append({"name": name, "width": width, "height": height,
                                "issues": [f"FAILED: {exc}"], "screenshot_b64": ""})
                continue
            results.append({"name": name, "width": width, "height": height,
                            "issues": issues, "screenshot_b64": b64})
    finally:
        try:
            await asyncio.to_thread(setter, *original)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not restore viewport after sweep: %s", exc)

    return {"ok": True, "breakpoints": results}
