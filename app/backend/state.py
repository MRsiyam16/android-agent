"""In-memory telemetry stores and the WebSocket fan-out.

Everything here is process-lifetime state: there is no database, and a restart starts
the graph over. The dashboard's Save is what makes a run durable.

`latest_state` is deliberately a module attribute rather than something importable by
value. Several routes rebind it, and `from .state import latest_state` would hand each
of them a private copy that silently stops tracking. Read and write it as
`state.latest_state`.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from fastapi import WebSocket

import config

state_store: dict[str, dict[str, Any]] = {}
# Bounded: this is replayed in full to every browser that connects, and it is the one
# structure that grows without limit for as long as the server is up.
telemetry_history: deque[dict[str, Any]] = deque(maxlen=config.TELEMETRY_HISTORY_LIMIT)
latest_state: dict[str, Any] | None = None


def device_serial() -> str | None:
    """The serial of the phone the current run is on, if a run has reported one.

    Five routes needed `(latest_state or {}).get("device_serial")` and each one had to
    remember that `latest_state` may be None. Naming it once is less to get wrong.
    """
    return (latest_state or {}).get("device_serial")


def history_for_replay() -> list[dict[str, Any]]:
    """The history a newly-connected browser needs, with each screenshot sent once.

    In memory a revisit is cheap — the backfill in the telemetry route assigns the *same*
    base64 string object, so RAM holds one copy per capture. Serialising to JSON discards
    that sharing entirely: a screen visited forty times would put forty full copies of its
    JPEG on the wire, and the replay is exactly when a long session is most likely to stall
    the browser.

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
