"""The two plain document routes: the dashboard itself and its favicon."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..paths import STATIC_DIR, TEMPLATES_DIR

router = APIRouter()


@router.get("/")
async def dashboard():
    return FileResponse(TEMPLATES_DIR / "dashboard.html")


@router.get("/favicon.ico")
async def favicon():
    path = STATIC_DIR / "favicon.ico"
    if path.exists():
        return FileResponse(path)
    raise HTTPException(status_code=404)
