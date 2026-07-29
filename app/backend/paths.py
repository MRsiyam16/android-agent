"""Where the served files live.

One place, so moving the frontend is a two-line change rather than a hunt through the
route modules. `BASE_DIR` is the repo root, not backend/ — these point out of this
package into its sibling.
"""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
TEMPLATES_DIR = FRONTEND_DIR
STATIC_DIR = FRONTEND_DIR / "static"
