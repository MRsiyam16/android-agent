"""Where a project's files live, and the bookkeeping written into them.

One local folder per app package, auto-populated as telemetry arrives — meta.json
(bookkeeping), screenshots/<state_hash>.jpg (one file per newly-discovered state),
memory.json (written by memory.py, not this module), and flow-graph.json (the same
"project blob" the dashboard builds for its Save).

Every path here defers to project_paths, which is the one place that knows a project may
have been pointed at a folder outside this repo. Never build a path from PROJECTS_DIR.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from pathlib import Path
from typing import Any

import project_paths

logger = logging.getLogger("server.projects")

# Only the *default* home for projects — one that has been pointed at a folder elsewhere
# lives wherever the registry says.
PROJECTS_DIR = project_paths.DEFAULT_PROJECTS_DIR

safe_package_name = project_paths.safe_package_name


def project_dir(package: str) -> Path:
    return project_paths.project_dir(package)


def screenshots_dir(package: str) -> Path:
    return project_dir(package) / "screenshots"


def meta_path(package: str) -> Path:
    return project_dir(package) / "meta.json"


def flow_graph_path(package: str) -> Path:
    return project_dir(package) / "flow-graph.json"


def read_meta(package: str) -> dict[str, Any] | None:
    path = meta_path(package)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read project meta for %s: %s", package, exc)
        return None


def write_meta(package: str, **updates: Any) -> dict[str, Any]:
    """Merge-update meta.json for a project, creating it (and the project dir) if needed."""
    meta = read_meta(package) or {
        "package": package,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_run_at": None,
        "last_saved_at": None,
        "state_count": 0,
        "edge_count": 0,
    }
    meta.update(updates)
    try:
        project_dir(package).mkdir(parents=True, exist_ok=True)
        meta_path(package).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write project meta for %s: %s", package, exc)
    return meta


def ensure_project(package: str) -> None:
    """Auto-create a project the first time telemetry arrives for a package."""
    if meta_path(package).is_file():
        return
    write_meta(package)


def save_screenshot_if_new(package: str, state_hash: str, screenshot_b64: str) -> None:
    if not screenshot_b64:
        return
    dest = screenshots_dir(package) / f"{state_hash}.jpg"
    if dest.exists():
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(screenshot_b64))
    except (OSError, binascii.Error) as exc:
        logger.warning("Could not save screenshot for %s/%s: %s", package, state_hash[:8], exc)
