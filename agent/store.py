"""On-disk state for the chat agent: sub-projects, chat logs, memory, credentials.

Everything lives under the existing per-package project folder that server.py already
creates, so a project's flow graph, screenshots, exploration memory and now its test
suites sit together:

    projects/<package>/
        meta.json                     written by server.py (unchanged)
        flow-graph.json               the dashboard board (unchanged)
        memory.json                   exploration memory (memory.py, unchanged)
        screenshots/<hash>.jpg        one per discovered state (unchanged)
        secrets.json                  test credentials — GITIGNORED, never committed
        agent/
            subprojects.json          the module breakdown + per-module status
            <slug>/
                chat.jsonl            append-only chat transcript
                memory.md             what the agent learned about this module
                findings.json         confirmed defects, for the report
                report.md             the written deliverable
                shots/                screenshots the agent captured as evidence

Two deliberate choices:

* `chat.jsonl` is append-only. The dashboard autosave already proved that a whole-file
  rewrite of a live document loses data when two writers race (see SYSTEM_MEMORY.md,
  "dashboard-stale-tab-clobbers-project"); appending one JSON object per line cannot.
* Findings are stored separately from the chat. The transcript is what happened; findings
  are what was concluded. A report must be rebuildable without replaying the conversation.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger("agent.store")

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = BASE_DIR / "projects"


def _safe_name(name: str) -> str:
    """Mirror server.py's _safe_package_name so both agree on folder names."""
    return "".join(c if (c.isalnum() or c in ".-_") else "_" for c in name) or "unknown"


def slugify(title: str) -> str:
    """A stable folder name for a sub-project title ("Auth & Login" -> "auth-login")."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "module"


def now() -> str:
    """UTC timestamp in the same format server.py stamps meta.json with."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_now = now  # internal alias, kept so existing call sites read naturally


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
def project_dir(package: str) -> Path:
    return PROJECTS_DIR / _safe_name(package)


def agent_dir(package: str) -> Path:
    return project_dir(package) / "agent"


def subproject_dir(package: str, slug: str) -> Path:
    return agent_dir(package) / _safe_name(slug)


def shots_dir(package: str, slug: str) -> Path:
    return subproject_dir(package, slug) / "shots"


def _subprojects_path(package: str) -> Path:
    return agent_dir(package) / "subprojects.json"


def _secrets_path(package: str) -> Path:
    return project_dir(package) / "secrets.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Write via a temp file + replace, so a crash mid-write can't truncate the original."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)


# --------------------------------------------------------------------------------------
# Sub-projects
# --------------------------------------------------------------------------------------
def list_subprojects(package: str) -> list[dict[str, Any]]:
    data = _read_json(_subprojects_path(package), {})
    items = data.get("subprojects", []) if isinstance(data, dict) else []
    return [i for i in items if isinstance(i, dict)]


def get_subproject(package: str, slug: str) -> Optional[dict[str, Any]]:
    return next((s for s in list_subprojects(package) if s.get("slug") == slug), None)


def save_subprojects(package: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _write_json_atomic(_subprojects_path(package), {"package": package,
                                                    "updated_at": _now(),
                                                    "subprojects": items})
    return items


def create_subproject(package: str, title: str, scope: str = "",
                      status: str = "proposed", screens: Optional[list[str]] = None,
                      ) -> dict[str, Any]:
    """Add a sub-project. Idempotent on slug: re-creating an existing one updates its scope
    rather than producing a duplicate module the user would have to reconcile by hand."""
    items = list_subprojects(package)
    slug = slugify(title)
    existing = next((s for s in items if s.get("slug") == slug), None)
    if existing:
        if scope:
            existing["scope"] = scope
        if screens:
            existing["screens"] = screens
        existing["updated_at"] = _now()
        save_subprojects(package, items)
        return existing

    entry = {
        "slug": slug,
        "title": title.strip(),
        "scope": scope.strip(),
        "status": status,          # proposed | approved | running | done
        "screens": screens or [],
        "finding_count": 0,
        "created_at": _now(),
        "updated_at": _now(),
        "last_run_at": None,
    }
    items.append(entry)
    save_subprojects(package, items)
    subproject_dir(package, slug).mkdir(parents=True, exist_ok=True)
    return entry


def update_subproject(package: str, slug: str, **updates: Any) -> Optional[dict[str, Any]]:
    items = list_subprojects(package)
    entry = next((s for s in items if s.get("slug") == slug), None)
    if entry is None:
        return None
    entry.update(updates)
    entry["updated_at"] = _now()
    save_subprojects(package, items)
    return entry


def delete_subproject(package: str, slug: str) -> bool:
    """Drop a sub-project from the list. The folder (transcript, findings, evidence) is
    deliberately left on disk — losing a test history to a mis-click is not recoverable."""
    items = list_subprojects(package)
    remaining = [s for s in items if s.get("slug") != slug]
    if len(remaining) == len(items):
        return False
    save_subprojects(package, remaining)
    return True


# --------------------------------------------------------------------------------------
# Chat transcript (append-only)
# --------------------------------------------------------------------------------------
def append_chat(package: str, slug: str, entry: dict[str, Any]) -> dict[str, Any]:
    record = {"ts": _now(), **entry}
    path = subproject_dir(package, slug) / "chat.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Could not append chat for %s/%s: %s", package, slug, exc)
    return record


def read_chat(package: str, slug: str, limit: int = 500) -> list[dict[str, Any]]:
    path = subproject_dir(package, slug) / "chat.jsonl"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a torn final line from a hard kill — skip, don't fail the load
    except OSError as exc:
        logger.warning("Could not read chat for %s/%s: %s", package, slug, exc)
        return []
    return out[-limit:]


def iter_chat(package: str, slug: str) -> Iterator[dict[str, Any]]:
    yield from read_chat(package, slug, limit=10**9)


# --------------------------------------------------------------------------------------
# Agent memory — a markdown file the agent reads and writes with its own file tools
# --------------------------------------------------------------------------------------
def memory_path(package: str, slug: str) -> Path:
    return subproject_dir(package, slug) / "memory.md"


def ensure_memory(package: str, slug: str, title: str) -> Path:
    """Create the memory file with a header the agent can append under.

    Kept as markdown on disk rather than as a JSON blob we manage, because the planner
    already has Read/Write/Edit tools and file-based memory is what it is good at. It also
    means the memory is reviewable and correctable by hand.
    """
    path = memory_path(package, slug)
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# Memory — {title}\n\n"
        f"What I have learned about this module of `{package}`. One fact per bullet.\n"
        "Things worth recording: where a screen lives and how to reach it, which selectors\n"
        "are ambiguous, how long a screen takes to settle, what a correct error message says,\n"
        "and any defect already confirmed (so it is not re-reported as new).\n\n"
        "## Navigation\n\n## Selectors and timing\n\n## Confirmed behaviour\n",
        encoding="utf-8",
    )
    return path


def read_memory(package: str, slug: str) -> str:
    path = memory_path(package, slug)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# --------------------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------------------
def _findings_path(package: str, slug: str) -> Path:
    return subproject_dir(package, slug) / "findings.json"


def list_findings(package: str, slug: str) -> list[dict[str, Any]]:
    data = _read_json(_findings_path(package, slug), [])
    return data if isinstance(data, list) else []


def add_finding(package: str, slug: str, finding: dict[str, Any]) -> dict[str, Any]:
    findings = list_findings(package, slug)
    record = {"id": f"F{len(findings) + 1:03d}", "ts": _now(), **finding}
    findings.append(record)
    _write_json_atomic(_findings_path(package, slug), findings)
    update_subproject(package, slug, finding_count=len(findings))
    return record


# --------------------------------------------------------------------------------------
# Test credentials
# --------------------------------------------------------------------------------------
def get_secrets(package: str) -> dict[str, str]:
    data = _read_json(_secrets_path(package), {})
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def set_secret(package: str, key: str, value: str) -> None:
    secrets = get_secrets(package)
    secrets[key] = value
    _write_json_atomic(_secrets_path(package), secrets)


def secret_keys(package: str) -> list[str]:
    """Names only. The agent is told which credentials exist and asks for one by name;
    values are injected into a tool call server-side and never written into the prompt,
    the transcript, or the report."""
    return sorted(get_secrets(package).keys())
