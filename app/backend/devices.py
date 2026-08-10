"""Direct device access for the remote-control panel and the live-frame view.

This is the connection the *server* opens on its own behalf, going through the same
`device.create_device()` platform dispatch the agent's own sessions use (agent/device_tools.py)
so a manual `/tap ...` or the frame fallback works the same on an iPhone/iPad as on Android.
The chat agent has its own device session per module (see agent/runtime.py); where one
exists, the routes prefer it so that watching the screen does not open a second connection
to the same phone while the agent is mid-tap.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

import device

_device_cache: dict[str, Any] = {}


def resolve_device(serial: Optional[str]):
    """Lazily connect (and cache) a device session for remote control commands."""
    key = serial or "__default__"
    if key not in _device_cache:
        try:
            _device_cache[key] = device.create_device(serial)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Could not connect to device: {exc}") from exc
    return _device_cache[key]
