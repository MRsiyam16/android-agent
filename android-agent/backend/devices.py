"""Direct uiautomator2 access for the remote-control panel and the live-frame view.

This is the connection the *server* opens on its own behalf. The chat agent has its own
device session per module (see agent/runtime.py); where one exists, the routes prefer it
so that watching the screen does not open a second connection to the same phone while the
agent is mid-tap.
"""
from __future__ import annotations

import base64
import io
from typing import Any, Optional

from fastapi import HTTPException

import config

_device_cache: dict[str, Any] = {}


def resolve_device(serial: Optional[str]):
    """Lazily connect (and cache) a uiautomator2 session for remote control commands."""
    import uiautomator2 as u2

    key = serial or "__default__"
    if key not in _device_cache:
        try:
            _device_cache[key] = u2.connect(serial) if serial else u2.connect()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Could not connect to device: {exc}") from exc
    return _device_cache[key]


def screenshot_b64(d) -> str:
    img = d.screenshot(format="pillow")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=config.SCREENSHOT_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")
