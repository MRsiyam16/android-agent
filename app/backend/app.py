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
from .routes import pages as page_routes
from .routes import projects as project_routes
from .routes import telemetry as telemetry_routes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("server")


def create_app() -> FastAPI:
    app = FastAPI(title="Android App Testing Agent — Telemetry Server")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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
