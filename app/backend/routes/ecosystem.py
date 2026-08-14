"""The view across projects that are one product.

Every other route module is scoped to one project, because until now a project was the
largest thing the system knew about. These are the endpoints that only mean anything above
that line: which apps make up an ecosystem, and which of their separately-filed findings are
really one defect.

Read-only except for the cluster edits. Nothing here starts a run or touches a device — the
manager tier that does is a separate build, and keeping this module incapable of it is the
same discipline as the manager module not being registered `record_finding`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import clusters as clusters_mod
import ecosystem as ecosystem_mod

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
    return {"name": name, "members": members, **ecosystem_mod.summary(name),
            "clusters": clusters_mod.summary(name)}


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
