"""The two endpoints another system reads this harness through.

Every other route module here is read by the dashboard — a browser on the same machine, whose
author can be told to look somewhere else when a shape changes. These two are read by
Bugmaster's bridge worker (`D:\\bugmaster\\docs\\BRIDGE.md` §5.3), which polls
`GET /verifications/<job_id>` every 15 seconds while a fix waits on a merge gate. That makes
the response body a **contract**, not a convenience:

* **404 until reported, and 404 means "not yet".** The worker distinguishes "no answer" from
  every possible answer purely by the status code, and its own timeout turns a long silence
  into `blocked`. An empty 200, or a stub with a null verdict, would be read as an answer.
* **The findings come back whole, with absolute screenshot paths.** The worker fetches those
  through the existing `GET /agent/shot?path=…` and base64s them into its result. Nothing on
  the other side knows where a module's findings file lives, and nothing should.
* **Read-only.** A verdict is written by the manager's `report_verification` tool, on the
  review turn of a run it watched. There is deliberately no `POST` here: an HTTP endpoint that
  could record a verdict would let anything on loopback answer a job the agent never ran, and
  "verified" would stop meaning "somebody looked".

They live on the QA Verifier instance (port 8001, `PROJECTS_DIR=verify-projects`), but the
router is mounted on every instance — one server module, one set of routes. On the QA Master
the log is simply empty, which is the honest answer there.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

import verifications as verifications_mod

logger = logging.getLogger("server.verifications")
router = APIRouter()


@router.get("/verifications")
async def list_verifications(limit: int = Query(20, ge=1, le=200)):
    """Verifications this instance has reported, newest first.

    For a person looking at what the bridge has been doing, and for the worker to sanity-check
    that it is talking to the instance it thinks it is. The per-job endpoint below is the one
    the poll loop uses.
    """
    return verifications_mod.list_recent(limit)


@router.get("/verifications/{job_id}")
async def get_verification(job_id: str):
    """One job's verdict. 404 while the manager has not answered it — see the module header."""
    found = verifications_mod.get(job_id)
    if found is None:
        raise HTTPException(status_code=404,
                            detail=f"No verification has been reported for {job_id!r}.")
    return found
