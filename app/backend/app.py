"""Builds the FastAPI app: mounts, error handling, lifecycle, routers.

Route handlers live in routes/; nothing in this file knows what any endpoint does.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agent import store as agent_store

from . import agent_bridge, projects
from .paths import STATIC_DIR
from .routes import agent as agent_routes
from .routes import device as device_routes
from .routes import ecosystem as ecosystem_routes
from .routes import pages as page_routes
from .routes import projects as project_routes
from .routes import telemetry as telemetry_routes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("server")


class RevalidatingStatic(StaticFiles):
    """Static files the browser must re-check before reusing.

    The frontend is 25 ES modules importing each other by relative path. A `?v=` on the one
    `<script>` tag busts that file and nothing it imports, so an edit to `modules.js` or
    `state.js` was invisible until someone happened to hard-refresh — and this has already
    cost real debugging time: a cached `ecosystem.js` threw during boot, left the project list
    empty, and printed nothing to the console.

    `no-cache` does not mean "do not cache". It means "revalidate first": the browser keeps
    the file and sends a conditional request, and StaticFiles answers 304 from the ETag it
    already computes. Over loopback that is a fraction of a millisecond per file, paid only on
    a page load — against a class of bug whose symptom is code that provably cannot run.

    It also makes the tabs the server opens for a run trustworthy. Those arrive without anyone
    pressing refresh, so "stale until hard-refreshed" would mean "stale forever".
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="Android App Testing Agent — Telemetry Server")
    app.mount("/static", RevalidatingStatic(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(RequestValidationError)
    async def log_validation_errors(request: Request, exc: RequestValidationError):
        logger.warning("422 on %s: %s", request.url.path, exc.errors())
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    # Order matters only within a router, not between them: no two routers declare a path
    # the other could shadow. Kept in reading order — pages, the telemetry hot path, then
    # the things built on top of it.
    app.include_router(page_routes.router)
    app.include_router(telemetry_routes.router)
    app.include_router(project_routes.router)
    app.include_router(device_routes.router)
    app.include_router(agent_routes.router)
    # Last because it is the only router that reaches across projects: everything it reads is
    # built from what the routers above own. Its one `/projects/...` path takes a literal
    # `/ecosystem` suffix, which no route above can shadow.
    app.include_router(ecosystem_routes.router)

    @app.on_event("startup")
    async def _pause_orphaned_campaigns() -> None:
        """A sweep's steps run in this process, so a restart leaves none of them running.

        The file would still say `running` and the board would keep reporting a module under
        test that nothing is testing — which is worse than reporting nothing, because it is
        the same shape as progress. Paused rather than stopped: the work is still wanted.
        """
        import campaigns

        for campaign in campaigns.reset_orphans():
            logger.info("campaign %s was live when the server stopped — paused at %s",
                        campaign["id"], (campaign.get("blocked") or {}).get("module"))

    @app.on_event("startup")
    async def _prewarm_agent() -> None:
        """Bring up a Claude Code session for the last-used module in the background.

        Deliberately fire-and-forget: a slow or failed CLI spawn must not delay the server
        binding its port, and the dashboard is perfectly usable without the agent.
        """
        target = agent_store.get_last_opened()
        if not target:
            return

        async def _warm() -> None:
            try:
                meta = projects.read_meta(target["package"]) or {}
                result = await agent_bridge.sessions.warm(
                    target["package"], target["slug"], platform=meta.get("platform"))
                logger.info("pre-warmed agent session for %s/%s (model=%s)",
                            target["package"], target["slug"], result.get("model"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not pre-warm the agent session: %s", exc)

        asyncio.create_task(_warm())

    @app.on_event("shutdown")
    async def _close_agent_sessions() -> None:
        await agent_bridge.sessions.close_all()

    return app
