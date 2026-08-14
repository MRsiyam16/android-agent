"""Filing QA findings as Blackcode issues, through the `bk` CLI.

Blackcode's only supported interface is its CLI (`bk`) — there is no public HTTP API to call
directly (`bk guide platform/overview`: "It is the ONLY supported interface. The HTTP API
behind it is private plumbing with no public contract."). This module shells out to it rather
than talking HTTP, and never touches auth itself: `bk login` manages its own token in
`~/.config/bk/`, and if that token is missing or stale every call here just surfaces the CLI's
own `error:`/`hint:` text, the same as it would at a terminal.

One thing this module *does* own that `bk` cannot: which Blackcode project a QA project's
findings go to. That mapping lives in this app's own per-project `meta.json`, read and written
directly here (`stored_project_id` / `remember_project_id`) rather than through
`backend/projects.py` — that module sits a layer above `agent/`, which is where this is called
from, and must not be imported from here (see `agent/device_tools.py`'s own import list for
the same rule already being followed).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

import config
import project_paths

logger = logging.getLogger("blackcode")


class BlackcodeError(RuntimeError):
    """A `bk` call failed — not installed, not authenticated, bad project, bad field, etc.

    The message is always something a human (or the agent relaying it) can act on directly:
    either `bk`'s own `error:`/`hint:` text, or a message written the same way here.
    """


# bk's own exit-code contract (`bk guide platform/output-and-exit-codes`) — turned into an
# actionable hint instead of just "bk exited 1", the same courtesy `bk` itself gives at a
# terminal via its `hint:` line.
_EXIT_HINTS = {
    3: "not authenticated — run `bk login` in a terminal, then try again.",
    4: "permission denied for that project — check you're a member of its workspace.",
    5: "not found — that project id (or issue number) does not exist in the active workspace.",
    6: "validation failed — see the message above for which field and why.",
}

# Issue priority is an integer, 1=urgent..5=no-priority (`bk guide issues/items`). This app's
# finding severities are a different, QA-shaped vocabulary (agent/device_tools.py's
# record_finding: low/medium/high/critical, plus "none" for a pass) — mapped once here so nothing
# calling create_issue has to know Blackcode's numbers.
_PRIORITY_BY_SEVERITY = {"critical": 1, "high": 2, "medium": 3, "low": 4, "none": 5}


def is_available() -> bool:
    """Whether the `bk` binary can even be found — cheap, no subprocess."""
    return shutil.which(config.BLACKCODE_CLI) is not None


def _run(args: list[str]) -> Any:
    """Run one `bk` subcommand and return its parsed `--json` output.

    Every call goes through here so the exit-code handling and the "not installed" message
    are written once. `--json` is appended rather than left to each caller, the same rule
    the CLI's own guide gives ("Add --json to every read command... Parse --json").

    Resolved to its full path via `shutil.which` rather than passed as the bare command name:
    npm installs `bk` on Windows as a `bk.CMD` shim, and `CreateProcess` cannot launch a
    `.CMD` directly from a bare name the way a shell's PATH search can — it needs the
    resolved path (or `cmd.exe /c`) to find and run it at all. `which` returning the full
    path sidesteps that without reaching for `shell=True`, and costs nothing extra on
    macOS/Linux, where `bk` is a real binary either way.
    """
    exe = shutil.which(config.BLACKCODE_CLI)
    if exe is None:
        raise BlackcodeError(
            f"The Blackcode CLI (`{config.BLACKCODE_CLI}`) is not installed or not on PATH. "
            f"Install it with `npm install -g @blackcode_sa/bc-issues`.")
    cmd = [exe, *args, "--json"]
    try:
        # `encoding="utf-8"` explicitly — `text=True` alone decodes with
        # `locale.getpreferredencoding()`, which on Windows is the system codepage (cp1252
        # here), not UTF-8. `bk` emits UTF-8 (issue titles/descriptions are full of em dashes
        # and smart quotes), so a cp1252 decode hits an undefined byte on almost any
        # non-trivial payload — a subprocess reader-thread crash that leaves `stdout` as
        # `None` rather than raising here, which is why this failed on every query, not a
        # specific one.
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            timeout=config.BLACKCODE_CLI_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise BlackcodeError(
            f"The Blackcode CLI (`{config.BLACKCODE_CLI}`) is not installed or not on PATH. "
            f"Install it with `npm install -g @blackcode_sa/bc-issues`.") from exc
    except subprocess.TimeoutExpired as exc:
        raise BlackcodeError(
            f"`bk {' '.join(args)}` took longer than "
            f"{config.BLACKCODE_CLI_TIMEOUT_SECONDS:.0f}s.") from exc
    if result.returncode != 0:
        hint = _EXIT_HINTS.get(result.returncode, "")
        detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        raise BlackcodeError(detail + (f" ({hint})" if hint else ""))
    try:
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError as exc:
        raise BlackcodeError(
            f"bk returned output that was not valid JSON: {result.stdout[:200]!r}") from exc


# -- workspace context ------------------------------------------------------------------
# Cached for this process's lifetime: `bk meta` is a live call and the base URL / active
# workspace slug it reports do not change mid-run. Only used to build a human-clickable
# link — a failure here must not fail the filing itself, since the issue was already created
# by the time this runs.
_ctx_cache: dict[str, str] = {}


def _workspace_context() -> tuple[str, str]:
    """(dashboard base URL, active workspace slug)."""
    if not _ctx_cache:
        try:
            meta = _run(["meta"])
            issues_app = (meta.get("apps") or {}).get("issues") or {}
            _ctx_cache["base_url"] = issues_app.get("base_url") or "https://issues.blackcode.ch"
            _ctx_cache["workspace"] = (meta.get("active_workspace") or {}).get("slug", "")
        except BlackcodeError as exc:
            logger.warning("could not fetch bk meta for a dashboard link: %s", exc)
            _ctx_cache["base_url"] = "https://issues.blackcode.ch"
            _ctx_cache["workspace"] = ""
    return _ctx_cache["base_url"], _ctx_cache["workspace"]


# -- projects -----------------------------------------------------------------------------
def list_projects() -> list[dict[str, Any]]:
    """Every Blackcode project in the active workspace — id and name only."""
    data = _run(["issues", "project", "list"])
    items = data if isinstance(data, list) else (data.get("data") or [])
    return [{"id": p["id"], "name": p["name"]} for p in items]


def resolve_project(project: Any) -> int:
    """A project id or exact name to a numeric id.

    Raises with the live project list on a name that does not match, rather than filing
    against a guessed id — the same "ask, don't assume" rule `bk`'s own guide gives for
    status/priority vocabularies.
    """
    text = str(project).strip()
    if text.isdigit():
        return int(text)
    projects = list_projects()
    match = next((p for p in projects if p["name"].lower() == text.lower()), None)
    if match is None:
        names = ", ".join(f"{p['name']!r} (id {p['id']})" for p in projects) or "(none found)"
        raise BlackcodeError(f"No Blackcode project named {project!r}. Known projects: {names}")
    return int(match["id"])


def _meta_path(package: str) -> Path:
    return project_paths.project_dir(package) / "meta.json"


def stored_project_id(package: str) -> Optional[int]:
    """This QA project's remembered Blackcode project id, or None if it has never filed one."""
    path = _meta_path(package)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("blackcode_project_id")
    return int(value) if isinstance(value, int) else None


def remember_project_id(package: str, project_id: int) -> None:
    """Save this package's Blackcode project id so future filings don't need it repeated.

    Read-modify-write against the same meta.json `backend/projects.py` owns and merge-updates
    the same way — this only ever adds or overwrites the one key, never touches the rest, so
    it stays safe to run concurrently with a dashboard save of the same file.
    """
    path = _meta_path(package)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["blackcode_project_id"] = project_id
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not remember blackcode project id for %s: %s", package, exc)


# -- issues -------------------------------------------------------------------------------
def create_issue(project_id: int, title: str, description: str, severity: str = "medium",
                 evidence_path: Optional[str] = None) -> dict[str, Any]:
    """File one issue in Blackcode. Returns {"number": int, "url": str}.

    `description` goes through `--description-file` rather than `--description`: the guide is
    explicit that a literal string is not a safe way to send multi-line content ("the only way
    to be sure you send real newlines"), and a finding's expected/actual/steps text is exactly
    that. The evidence screenshot, when there is one, is passed as `--file` so it embeds
    inline in the issue body — Blackcode previews images there rather than just attaching them.
    """
    priority = _PRIORITY_BY_SEVERITY.get((severity or "medium").lower(), 3)
    args = ["issues", "issue", "create",
            "--project", str(project_id), "--title", title, "--priority", str(priority)]
    if evidence_path and Path(evidence_path).is_file():
        args += ["--file", str(evidence_path)]

    fh = tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8", newline="\n")
    try:
        fh.write(description)
        fh.close()
        args += ["--description-file", fh.name]
        created = _run(args)
    finally:
        try:
            Path(fh.name).unlink(missing_ok=True)
        except OSError:
            pass

    number = created.get("id")
    if number is None:
        raise BlackcodeError(f"bk did not return an issue number: {created!r}")
    return {"number": int(number), "url": _issue_url(number)}


def _issue_url(number: Any) -> str:
    base_url, workspace = _workspace_context()
    return f"{base_url}/dashboard/{workspace}/issues/{number}" if workspace else ""


def search_issues(query: str = "", project_id: Optional[int] = None,
                  status: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
    """Issues matching a text search and/or filters — for checking whether something is
    already tracked before filing a duplicate, or browsing what's open.

    `query` is server-side ("Search title/description, or the #id" — `bk issues issue list
    --help`); `status`/`project_id` narrow further. An empty query with a status/project still
    works — it's just a filtered list.
    """
    args = ["issues", "issue", "list"]
    if project_id is not None:
        args += ["--project", str(project_id)]
    if status:
        args += ["--status", status]
    if query:
        args += ["--search", query]
    data = _run(args)
    items = data.get("data") if isinstance(data, dict) else (data or [])
    out = []
    for it in items[:max(1, limit)]:
        number = it.get("id")
        out.append({
            "number": number, "title": it.get("title"), "status": it.get("status"),
            "priority": it.get("priority"), "project_id": it.get("project_id"),
            "project_name": it.get("project_name"), "url": _issue_url(number),
        })
    return out


#: An issue in either of these statuses is no longer open work — see `bk meta`'s
#: `apps.issues.vocabulary.issue_statuses` (backlog/todo/in_progress/done/cancelled). Fetched
#: live in principle, but this app only ever needs the binary "is it still open" question, and
#: hardcoding the two terminal values here is no different from `record_finding`'s own fixed
#: severity enum — a genuinely new terminal status would show up as a live `bk meta` value this
#: still cannot see, which is a `learn_lesson` moment, not a silent misclassification.
_RESOLVED_STATUSES = {"done", "cancelled"}


def issue_status(number: int) -> dict[str, Any]:
    """Live status of one issue — for finding out whether something already filed got fixed."""
    data = _run(["issues", "issue", "view", str(number)])
    status = data.get("status")
    return {
        "number": data.get("id"), "status": status, "resolved": status in _RESOLVED_STATUSES,
        "priority": data.get("priority"), "assignees": data.get("assignees") or [],
        "updated_at": data.get("updated_at"), "completed_at": data.get("completed_at"),
        "url": _issue_url(data.get("id")),
    }
