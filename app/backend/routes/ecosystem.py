"""The view across projects that are one product.

Every other route module is scoped to one project, because until now a project was the
largest thing the system knew about. These are the endpoints that only mean anything above
that line: which apps make up an ecosystem, and which of their separately-filed findings are
really one defect.

Read-only except for the cluster edits and the re-test queue. Nothing here *drives* a device:
approving a re-test hands an instruction to the module that owns the device and returns, the
same as pressing Send in the cockpit does. Starting a run and driving one are different
powers, and only the first is here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import campaigns as campaigns_mod
import clusters as clusters_mod
import ecosystem as ecosystem_mod
import retests as retests_mod
import scratchpad as scratchpad_mod

logger = logging.getLogger("server.ecosystem")
router = APIRouter()


class ClusterPayload(BaseModel):
    """A cluster as the dashboard edits it. Members are (package, module, finding) triples."""
    title: str
    root: str = ""
    confidence: str = "tentative"
    members: list[dict[str, str]] = Field(default_factory=list)


class MemberPayload(BaseModel):
    package: str
    module: str
    finding: str


class TagPayload(BaseModel):
    ecosystem: str
    role: str


@router.get("/ecosystems")
async def list_ecosystems():
    """Every ecosystem on disk with its members and headline numbers."""
    return [{"name": name, "members": members, **ecosystem_mod.summary(name)}
            for name, members in ecosystem_mod.ecosystems().items()]


@router.get("/ecosystems/{name}")
async def get_ecosystem(name: str):
    members = ecosystem_mod.members(name)
    if not members:
        raise HTTPException(status_code=404, detail=f"No ecosystem named {name!r}")
    return {"name": name, "members": members,
            # `members` omits the supervisor, so anything that wants to talk to the manager
            # cannot find it there. Named explicitly rather than left to the caller to guess.
            "supervisor": ecosystem_mod.supervisor(name),
            **ecosystem_mod.summary(name), "clusters": clusters_mod.summary(name)}


@router.get("/ecosystems/{name}/board")
async def get_board(name: str):
    """Everything the manager dashboard's landing view shows, in one request.

    Four round-trips (apps, totals, clusters, unclustered) rendered as one board is four
    chances for the page to draw a total that disagrees with the rows under it, because each
    reads the findings on disk at a different moment while a module is filing. One call, one
    read, one consistent picture.
    """
    apps = ecosystem_mod.app_index(name)
    if not apps:
        raise HTTPException(status_code=404, detail=f"No ecosystem named {name!r}")
    rows = clusters_mod.list_clusters(name)
    unclustered = [f for f in ecosystem_mod.findings(name) if not f.get("cluster")]
    return {
        "name": name,
        "supervisor": ecosystem_mod.supervisor(name),
        "apps": apps,
        "totals": ecosystem_mod.summary(name),
        "clusters": clusters_mod.summary(name),
        "cross_app": [c for c in rows if c["scope"] == "cross-app"],
        "unclustered": len(unclustered),
        "retests": retests_mod.summary(name),
        # The running indicator's data. In the board payload rather than its own request so
        # the page cannot show a sweep against totals read a moment apart from it.
        "campaigns": campaigns_mod.summary(name),
        "scratchpad": len(scratchpad_mod.list_all(name)),
    }


@router.get("/ecosystems/{name}/campaigns")
async def get_campaigns(name: str, live: bool = False):
    """Jobs over this product's apps, newest first. `live=true` for the ones still going."""
    rows = campaigns_mod.live(name) if live else campaigns_mod.list_all(name)
    return [{**c, **campaigns_mod.progress(c)} for c in rows]


@router.get("/ecosystems/{name}/scratchpad")
async def get_scratchpad(name: str):
    """What the apps have written down for each other — the only state that crosses between
    them. Shown on the board so a stale note is visible rather than quietly wrong."""
    return scratchpad_mod.list_all(name)


@router.delete("/ecosystems/{name}/scratchpad/{key:path}")
async def drop_note(name: str, key: str):
    if not scratchpad_mod.drop(name, key):
        raise HTTPException(status_code=404, detail=f"No note called {key!r}")
    return {"ok": True, "key": key}


@router.post("/ecosystems/{name}/campaigns/{campaign_id:path}/stop")
async def stop_campaign(name: str, campaign_id: str):
    """End a sweep. Nothing already filed is undone, and a module mid-run finishes its turn."""
    campaign = campaigns_mod.get(campaign_id)
    if campaign is None or campaign.get("ecosystem") != name:
        raise HTTPException(status_code=404, detail=f"No campaign {campaign_id!r}")
    return campaigns_mod.set_status(campaign_id, "stopped")


@router.post("/ecosystems/{name}/campaigns/{campaign_id:path}/resume")
async def resume_campaign(name: str, campaign_id: str):
    """Pick a paused sweep back up at its next pending module."""
    from ..campaign_runner import runner

    campaign = campaigns_mod.get(campaign_id)
    if campaign is None or campaign.get("ecosystem") != name:
        raise HTTPException(status_code=404, detail=f"No campaign {campaign_id!r}")
    if campaign["status"] in ("done", "stopped"):
        raise HTTPException(status_code=409,
                            detail=f"That sweep is already {campaign['status']}.")
    asyncio.create_task(runner.advance(campaign_id))
    return {"ok": True, "id": campaign_id, "resuming": True}


@router.get("/ecosystems/{name}/modules")
async def get_modules(name: str):
    """Every module across the ecosystem's apps, with status and outcome tally.

    The cheap half of the map a supervisor needs: what areas exist, which have been tested,
    and where the defects are.
    """
    return ecosystem_mod.module_index(name)


@router.get("/ecosystems/{name}/findings")
async def get_findings(name: str, kind: Optional[str] = None, unclustered: bool = False):
    """Defects across the ecosystem, tagged with the app and role that filed them.

    `unclustered=true` is the working queue for correlation — everything no cluster has
    claimed yet, which is where a new duplicate would be hiding.
    """
    kinds = (kind,) if kind else ecosystem_mod.DEFECT_KINDS
    found = ecosystem_mod.findings(name, kinds=kinds)
    if unclustered:
        found = [f for f in found if not f.get("cluster")]
    return found


@router.get("/ecosystems/{name}/clusters")
async def get_clusters(name: str):
    """Findings that are one defect, cross-app first then largest first."""
    return clusters_mod.list_clusters(name)


@router.get("/ecosystems/{name}/clusters/{cluster_id}")
async def get_cluster(name: str, cluster_id: str):
    cluster = clusters_mod.get(name, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail=f"No cluster {cluster_id!r} in {name!r}")
    return cluster


@router.put("/ecosystems/{name}/clusters/{cluster_id}")
async def put_cluster(name: str, cluster_id: str, payload: ClusterPayload):
    """Create or replace a cluster, then re-stamp the findings it claims.

    `apply` runs on every write rather than being a separate call the caller might forget:
    the stamp on a finding is a cache of this file, and a cache nobody refreshes is worse
    than no cache at all.
    """
    cluster = clusters_mod.save(name, cluster_id, title=payload.title, root=payload.root,
                                confidence=payload.confidence, members=payload.members)
    clusters_mod.apply(name)
    return cluster


@router.delete("/ecosystems/{name}/clusters/{cluster_id}")
async def delete_cluster(name: str, cluster_id: str):
    if not clusters_mod.delete(name, cluster_id):
        raise HTTPException(status_code=404, detail=f"No cluster {cluster_id!r} in {name!r}")
    return {"deleted": cluster_id}


@router.post("/ecosystems/{name}/clusters/{cluster_id}/members")
async def add_member(name: str, cluster_id: str, payload: MemberPayload):
    cluster = clusters_mod.add_member(name, cluster_id, payload.package, payload.module,
                                      payload.finding)
    if cluster is None:
        raise HTTPException(status_code=404, detail=f"No cluster {cluster_id!r} in {name!r}")
    return cluster


@router.delete("/ecosystems/{name}/clusters/{cluster_id}/members")
async def remove_member(name: str, cluster_id: str, package: str, module: str, finding: str):
    cluster = clusters_mod.remove_member(name, cluster_id, package, module, finding)
    if cluster is None:
        raise HTTPException(status_code=404, detail=f"No cluster {cluster_id!r} in {name!r}")
    return cluster


@router.post("/ecosystems/{name}/apply")
async def apply_stamps(name: str):
    """Rebuild every finding's cluster stamp from the store. Idempotent."""
    return clusters_mod.apply(name)


@router.get("/ecosystems/{name}/retests")
async def get_retests(name: str, status: Optional[str] = None):
    """The re-test queue: runs the manager wants, waiting on you."""
    return retests_mod.list_queued(name, status)


@router.post("/ecosystems/{name}/retests/{entry_id:path}/approve")
async def approve_retest(name: str, entry_id: str):
    """Approve a queued re-test and start it.

    The run is started *before* the entry is marked approved, and the entry is left pending if
    it will not start. An approval that records success while the module never ran is the one
    failure this queue exists to prevent — it would read, later, as a defect that was
    re-checked and passed.
    """
    from .. import agent_bridge

    entry = retests_mod.get(name, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No queued re-test {entry_id!r}")
    if entry["status"] != "pending":
        raise HTTPException(status_code=409,
                            detail=f"That re-test was already {entry['status']}.")

    instruction = entry.get("instruction") or (
        f"Re-test {entry['finding']} — \"{entry['title']}\". {entry['reason']} "
        f"Run the case again and record what you actually observe now: a pass if it is fixed, "
        f"or a fresh finding if it is not. Do not assume the fix landed in this build.")
    try:
        # `watch`: approving happens on the manager board, and the run itself happens in a
        # different project's session — so without a tab the only sign it is under way is a
        # counter changing. Approving is exactly the moment someone wants to watch.
        started = agent_bridge.start_run(entry["package"], entry["module"], instruction,
                                         watch=True)
    except agent_bridge.RunRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**retests_mod.decide(name, entry_id, "approved"),
            "watch_url": started["watch_url"]}


@router.post("/ecosystems/{name}/retests/{entry_id:path}/dismiss")
async def dismiss_retest(name: str, entry_id: str):
    """Say no. Recorded rather than deleted, so the next sync does not raise it again."""
    entry = retests_mod.decide(name, entry_id, "dismissed")
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No queued re-test {entry_id!r}")
    return entry


@router.post("/projects/{package:path}/ecosystem")
async def tag_project(package: str, payload: TagPayload):
    """Put a project in an ecosystem under a role, or remove it when both are blank."""
    if not payload.ecosystem:
        ecosystem_mod.untag(package)
        ecosystem_mod.write_index()
        return {"package": package, "ecosystem": None, "role": None}
    meta = ecosystem_mod.tag(package, payload.ecosystem, payload.role)
    ecosystem_mod.write_index()
    return meta
