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

from .. import projects, state
from ..schemas import FindingTrackingPayload, ProjectCreatePayload

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
    # Idempotent re-opens post no `platform` at all — only set it when the caller actually
    # named one, so re-opening an existing web project never clobbers it back to "android".
    # `write_meta`'s own default dict covers a genuinely new project with no platform given.
    updates: dict[str, Any] = {}
    if payload.platform:
        updates["platform"] = payload.platform.lower()
    if payload.device_serial:
        updates["device_serial"] = payload.device_serial.strip()
    if payload.snapshot_name:
        updates["snapshot_name"] = payload.snapshot_name.strip()
    # Reachability (a bad URL, an uninstalled app) is discovered honestly on first `launch`,
    # the same way `is_installed` already works — no validation of `package` here.
    meta = projects.write_meta(package, **updates)
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


@router.get("/projects/{package:path}/flow-graph")
async def get_project_flow_graph(package: str):
    path = projects.flow_graph_path(package)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No saved flow graph for this project yet")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read flow graph: {exc}") from exc


@router.post("/projects/{package:path}/flow-graph")
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


@router.get("/projects/{package:path}/telemetry-history")
async def get_project_telemetry_history(package: str):
    """Every state_store record telemetry has ever posted for this package.

    The saved board (`flow-graph.json`) is only as complete as whatever browser tab was
    open and autosaving while the telemetry arrived — see `save_project_flow_graph`. A
    module run driven headlessly through the device MCP tools, with no dashboard tab open,
    still posts every step here (`post_telemetry` writes `state.state_store` unconditionally),
    but none of it reaches disk until something replays it through the same `ingest()` a live
    tab uses. The dashboard calls this on open to catch the saved board up to what actually
    happened; `state_store` never evicts, so this stays complete for as long as the server
    process runs, even long after the run that produced it finished.
    """
    return [record for record in state.state_store.values()
            if record.get("target_package") == package]


@router.get("/projects/{package:path}/outcomes")
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


@router.patch("/projects/{package:path}/modules/{slug}/findings/{finding_id}/tracking")
async def patch_finding_tracking(package: str, slug: str, finding_id: str,
                                  payload: FindingTrackingPayload):
    """Record a finding's external tracking (Blackcode issue link + resolved state).

    Called once when a finding is filed as a Blackcode issue (to set `issue_url`/`issue_id`),
    and again whenever that issue's status changes, so the Bugs pill can show which findings
    are actually resolved without the dashboard having to know anything about Blackcode itself.
    """
    updated = agent_store.set_finding_tracking(
        package, slug, finding_id, **payload.dict(exclude_unset=True))
    if updated is None:
        raise HTTPException(status_code=404, detail=f"No finding {finding_id} in {slug}")
    return updated


@router.get("/projects/{package:path}/notes")
async def project_notes(package: str):
    """The notes the agent pinned to this project's board, across every module.

    Flat and unbucketed, unlike outcomes: each note already names the section it belongs
    beside, and the board places it from that. Served separately from the board file because
    the browser owns that file — these are the agent's, and read-only to the page.
    """
    return {"package": package, "notes": agent_store.list_all_notes(package)}
