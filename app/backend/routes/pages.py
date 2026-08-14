"""The plain document routes: the two dashboards and the favicon.

There are two because they are two jobs. `/` is the testing cockpit — one project, one device,
one flow graph, and about 190 KB of JavaScript that exists to drive a phone and draw an app's
screens. `/manager` is the product view: five apps at once, no device, nothing to tap. Putting
the second inside the first meant hiding it behind a pill and a sheet, because that page's
whole shell is built around the one project you have open.

Separate pages, one server. They share the API, the design tokens and the chat styling; they
share no boot graph, which is the point — the cockpit's 63 import cycles are why everything
there initialises at once, and the manager should not inherit that to show a table.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..paths import STATIC_DIR, TEMPLATES_DIR

router = APIRouter()


@router.get("/")
async def dashboard():
    return FileResponse(TEMPLATES_DIR / "dashboard.html")


@router.get("/manager")
async def manager():
    return FileResponse(TEMPLATES_DIR / "manager.html")


@router.get("/favicon.ico")
async def favicon():
    path = STATIC_DIR / "favicon.ico"
    if path.exists():
        return FileResponse(path)
    raise HTTPException(status_code=404)
