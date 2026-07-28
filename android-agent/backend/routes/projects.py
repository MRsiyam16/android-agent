"""Project CRUD, the saved board, and the outcome pills.

The ownership check in save_project_flow_graph is the load-bearing part of this module:
it is what stands between an autosave with a stale package and four destroyed boards.
See tests/test_project_ownership.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

import project_paths
from agent import store as agent_store

from .. import projects
from ..schemas import ProjectCreatePayload

logger = logging.getLogger("server.projects")
router = APIRouter()


@router.get("/projects")
async def list_projects():
    """All known projects (one per app package tested), newest-run-first.

    Enumerated by package rather than by walking one directory, because a project may have
    been pointed at a folder anywhere on disk. Each entry carries its root so the UI can show
    where it actually lives.
    """
    found = []
    for package in project_paths.known_packages():
        meta = projects.read_meta(package)
        if meta is None:
            continue
        found.append({**meta, **project_paths.describe(package)})
    found.sort(key=lambda m: m.get("last_run_at") or "", reverse=True)
    return found


@router.post("/projects")
async def create_project(payload: ProjectCreatePayload):
    """Idempotent: returns the existing project's meta if it's already there."""
    package = payload.package.strip()
    if not package:
        raise HTTPException(status_code=400, detail="package is required")
    if payload.root:
        # Registered before the meta is written, so `write_meta` creates the folder in the
        # chosen place rather than making one under projects/ and leaving it orphaned.
        existing = project_paths.registered_root(package)
        if existing and Path(existing).resolve() != Path(payload.root).expanduser().resolve() \
                and projects.meta_path(package).is_file():
            raise HTTPException(
                status_code=409,
                detail=f"{package} already lives in {existing}. Move or delete that folder "
                       f"first if you want it somewhere else.")
        project_paths.register(package, payload.root)
    meta = projects.write_meta(package)
    return {**meta, **project_paths.describe(package)}


@router.post("/ui/pick-folder")
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


@router.get("/projects/{package}/flow-graph")
async def get_project_flow_graph(package: str):
    path = projects.flow_graph_path(package)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No saved flow graph for this project yet")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read flow graph: {exc}") from exc


@router.post("/projects/{package}/flow-graph")
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
        projects.project_dir(package).mkdir(parents=True, exist_ok=True)
        projects.flow_graph_path(package).write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not save flow graph: {exc}") from exc

    projects.write_meta(
        package,
        last_saved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        state_count=len(payload.get("nodes") or []),
        edge_count=len(payload.get("edges") or []),
    )
    return {"ok": True}


@router.get("/projects/{package}/outcomes")
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
