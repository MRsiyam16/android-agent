"""Where each project's folder lives on disk.

Until now a project was always `android-agent/projects/<package>/`, hardcoded identically in
`server.py` and `agent/store.py`. Letting the user pick a folder means that mapping stops
being a formula and becomes data, and both of those modules have to agree on it — so it lives
here, in a module neither of them owns. `agent/store.py` importing `server.py` to ask would be
a circular import; duplicating the lookup would be two things to keep in step.

    projects/registry.json
        {"projects": {"com.example.app": "D:/QA Projects/My App"}}

A package with no entry keeps the old location, so every project that already exists on disk
goes on working with no migration. The registry only ever records the exception.

Paths are stored absolute and resolved on write. A relative path in this file would be read
back against the server's working directory, which is not the same on a double-clicked .bat as
it is from a terminal — the project would simply appear empty depending on how it was started.

**Two harnesses, one checkout.** `PROJECTS_DIR` moves the whole notebook — the project folders
*and* every cross-project file beside them: `registry.json`, `clusters.json`, `campaigns.json`,
`retests.json`, `verifications.json`, `scratchpad.json`, `accounts.json`, `ECOSYSTEM.md`,
`last-opened.json`, `_trash/`. That is the point of it being one setting rather than one per
file.

It exists because a second instance now runs beside the first (`start_verifier.py`, port 8001,
`PROJECTS_DIR=verify-projects`). The QA Master tests the staging build and files what it finds;
the Verifier re-runs one case against a *patched* build that is deployed nowhere, on behalf of
Bugmaster's fix pipeline. Sharing a notebook would mix those two, and the mixture is not merely
untidy — it is wrong in both directions. A `bug` filed against an unmerged patch would show up
on the product board as a defect in the shipped app and get filed to Blackcode. A `pass`
recorded on a patch would read, later, as the staging build having been re-tested. The learned
memory drifts the same way: "the booking screen is fine now" is a fact about a build that
exists only in a worktree on this PC.

Absolute values are used as given; a relative one is resolved against `BASE_DIR` (`app/`), so
`PROJECTS_DIR=verify-projects` means the same folder whether the process was started from a
terminal, from a .bat that cds itself, or from a service. Read at import, because every path in
the system hangs off it and a value that could change mid-process would leave half the writes
in one notebook and half in the other.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("project_paths")

BASE_DIR = Path(__file__).resolve().parent

def _configured_projects_dir() -> Path:
    """`PROJECTS_DIR` if it is set, else the `projects/` folder beside this file.

    A relative value is resolved against `BASE_DIR` rather than the working directory, for the
    same reason the registry stores absolute paths: `app/` is a fixed point and the cwd is not.
    """
    raw = os.environ.get("PROJECTS_DIR", "").strip().strip('"')
    if not raw:
        return BASE_DIR / "projects"
    chosen = Path(raw).expanduser()
    return chosen if chosen.is_absolute() else BASE_DIR / chosen


#: Where projects live unless one has been pointed elsewhere. Read through the helpers below
#: rather than captured at import, so a test can repoint it at a tmp directory and have every
#: path — including the registry's own — follow. A module-level constant baked into
#: `_REGISTRY_PATH` at import time would leave the registry behind in the real projects folder
#: while everything else moved, which is worse than not being patchable at all.
DEFAULT_PROJECTS_DIR = _configured_projects_dir()


def _registry_path() -> Path:
    return DEFAULT_PROJECTS_DIR / "registry.json"

# Read-modify-write, same as the rest of the on-disk state, and reachable from both the
# request handlers and the agent's tool threads.
_LOCK = threading.RLock()


def safe_package_name(package: str) -> str:
    """The on-disk folder name for a package.

    Kept identical to the two private copies it replaces (`server._safe_package_name` and
    `store._safe_name`) so no existing folder has to be renamed.
    """
    return "".join(c if (c.isalnum() or c in ".-_") else "_" for c in package) or "unknown"


def _read_registry() -> dict[str, str]:
    path = _registry_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # A corrupt registry must not take every project offline: falling back to the default
        # layout finds the ones that never moved, which is strictly better than nothing.
        logger.warning("Could not read project registry (%s); using default locations", exc)
        return {}
    entries = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {str(k): str(v) for k, v in entries.items() if k and v}


def _write_registry(entries: dict[str, str]) -> None:
    try:
        DEFAULT_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        path = _registry_path()
        tmp = path.with_suffix(f".json.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"projects": entries}, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Could not write project registry: %s", exc)


def project_dir(package: str) -> Path:
    """The root folder for a package — its registered location, or the default one."""
    custom = _read_registry().get(package)
    if custom:
        return Path(custom)
    return DEFAULT_PROJECTS_DIR / safe_package_name(package)


def register(package: str, root: str | os.PathLike[str]) -> Path:
    """Point a package at a folder of the user's choosing and create it.

    The chosen folder *is* the project root — screenshots, findings and the board go directly
    inside it. It is not a parent to create a `<package>` folder under: the user picked that
    folder in a dialog having just named it, and silently nesting another level would put the
    files somewhere they did not ask for and would not think to look.
    """
    path = Path(root).expanduser().resolve()
    with _LOCK:
        entries = _read_registry()
        entries[package] = str(path)
        _write_registry(entries)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create project folder %s: %s", path, exc)
    return path


def unregister(package: str) -> None:
    """Forget a custom location. The folder itself is left alone."""
    with _LOCK:
        entries = _read_registry()
        if entries.pop(package, None) is not None:
            _write_registry(entries)


def registered_root(package: str) -> Optional[str]:
    """The custom folder for this package, or None if it sits in the default location."""
    return _read_registry().get(package)


def known_packages() -> list[str]:
    """Every package that has a project on disk, registered or default.

    Both halves are needed: a project moved to D:/ is only in the registry, while every
    project that predates the registry is only a folder under `projects/`.
    """
    found: dict[str, None] = {}
    for package, root in _read_registry().items():
        if (Path(root) / "meta.json").is_file():
            found[package] = None
    if DEFAULT_PROJECTS_DIR.is_dir():
        for entry in DEFAULT_PROJECTS_DIR.iterdir():
            if not entry.is_dir():
                continue
            meta = entry / "meta.json"
            if not meta.is_file():
                continue
            # The folder name is a sanitised package, so read the real one back out of the
            # meta rather than trying to reverse the sanitisation.
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                package = str(data.get("package") or entry.name)
            except (OSError, ValueError):
                package = entry.name
            found.setdefault(package, None)
    return list(found)


def describe(package: str) -> dict[str, Any]:
    """Location facts for the UI: where the project is, and whether that was chosen."""
    root = project_dir(package)
    return {"root": str(root), "custom": registered_root(package) is not None,
            "exists": root.is_dir()}
