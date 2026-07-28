"""FastAPI telemetry + remote-control server for the Android App Testing Agent dashboard.

The implementation moved into `backend/` — this file is the entry point and the stable
import surface. `python server.py` still runs the server, and the names below are still
importable from `server`, which is what `run_agent.py`, the tests and any script that
pokes at the in-memory graph expect.

Where things went:

    backend/app.py              the FastAPI app factory
    backend/state.py            in-memory stores + the WebSocket fan-out
    backend/naming.py           breadcrumb screen names and numbering
    backend/projects.py         project folders, meta.json, screenshots
    backend/devices.py          the server's own uiautomator2 connection
    backend/agent_bridge.py     the one SessionRegistry
    backend/schemas.py          every request body
    backend/routes/*.py         the endpoints, grouped by concern

The re-exports are by reference, and the stores they point at are cleared in place rather
than rebound, so `server.state_store` and `backend.state.state_store` are the same object
for the life of the process. `latest_state` is the exception — it is rebound on every
telemetry post, so read it through `backend.state`, not from here.
"""
from __future__ import annotations

import uvicorn

import config
from backend.app import create_app
from backend.naming import (edge_index, is_section_trigger, node_index, node_order,
                            register_screen, reset_screen_naming, screen_names,
                            screen_paths)
from backend.projects import PROJECTS_DIR
from backend.state import history_for_replay, manager, state_store, telemetry_history

app = create_app()

# Private aliases kept because the tests and a few scripts call them by their old names.
_history_for_replay = history_for_replay
_is_section_trigger = is_section_trigger
_register_screen = register_screen
_reset_screen_naming = reset_screen_naming

__all__ = [
    "app", "manager", "state_store", "telemetry_history", "history_for_replay",
    "screen_names", "screen_paths", "node_order", "node_index", "edge_index",
    "register_screen", "reset_screen_naming", "is_section_trigger", "PROJECTS_DIR",
]


if __name__ == "__main__":
    uvicorn.run("server:app", host=config.SERVER_HOST, port=config.SERVER_PORT, reload=False)
