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

import contextlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

import project_paths

logger = logging.getLogger("agent.store")

BASE_DIR = Path(__file__).resolve().parent.parent
# The default home only. `project_dir()` asks project_paths, because a project may have been
# pointed at a folder outside this repo — and the agent's transcripts, memory and evidence
# have to follow the board they belong to rather than staying behind here.
PROJECTS_DIR = project_paths.DEFAULT_PROJECTS_DIR

# Every mutation here is read → modify → write, and the callers are concurrent: `post_message`
# fires each turn onto its own task, several modules can run at once, and the device tools
# reach this module through `asyncio.to_thread`. Without a lock, two findings filed close
# together both read `findings.json` at length 1, both append, and both write — one finding is
# lost and they collide on `F002`. `_write_json_atomic` cannot help with that: it makes a
# single write atomic, not the read-modify-write around it.
#
# One process-wide RLock rather than one per package: these files are small and written rarely
# (a few times per test case), so contention is irrelevant, while a per-package registry is
# another thing to get wrong. RLock specifically because the mutations nest — `add_finding`
# calls `update_subproject`, which takes the same lock.
_LOCK = threading.RLock()


class StoreWriteError(OSError):
    """A mutation could not be persisted.

    Raised rather than logged because the caller is usually an agent tool, and a tool that
    returns "Filed F003" for a write that never landed is the harness telling the model
    something it has not verified — the exact thing `record_finding` refuses to let the
    model do. The finding is lost, `finding_count` disagrees with the file, and the only
    trace is a warning nobody reads. An error the agent can retry is strictly better: the
    write is all-or-nothing, so a retry after a failure cannot double-file.

    Realistic trigger, not a hypothetical: a project may live on any drive (see
    project_paths), so an unplugged external disk or a Windows AV file lock lands here.
    """


# Shared with server.py through project_paths, so the two can no longer drift apart.
_safe_name = project_paths.safe_package_name


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
    return project_paths.project_dir(package)


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


def _last_opened_path() -> Path:
    # Read live rather than through the module-level snapshot: this file is not scoped to any
    # one project, so it stays in the default tree wherever that currently points.
    return project_paths.DEFAULT_PROJECTS_DIR / "last-opened.json"


def set_last_opened(package: str, slug: str) -> None:
    """Remember which module was last in use, so the server can pre-warm its Claude Code
    session at startup and the first message of the day is instant.

    One of the two writers here that deliberately ignores a failure: losing this costs a
    cold first message, nothing else. Nothing downstream reports it as having happened, so
    there is no claim for a swallowed failure to falsify.
    """
    _write_json_atomic(_last_opened_path(), {"package": package, "slug": slug, "at": now()})


def get_last_opened() -> Optional[dict[str, str]]:
    data = _read_json(_last_opened_path(), None)
    if isinstance(data, dict) and data.get("package") and data.get("slug"):
        return {"package": str(data["package"]), "slug": str(data["slug"])}
    return None


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return default


def _write_json_atomic(path: Path, payload: Any) -> bool:
    """Write via a temp file + replace, so a crash mid-write can't truncate the original.

    The temp name carries the writer's pid and thread id. A fixed `.tmp` suffix is a shared
    resource: two writers land on the same temp path, and on Windows the second one fails
    outright — `WinError 32, the process cannot access the file` on replace, or a straight
    permission denial on open. `_LOCK` already serialises writers inside this process; this
    keeps a second process (or a future one that skips the lock) from turning a benign race
    into a write that silently does not happen.

    Returns whether the write landed. The bool is not decoration: callers that persist a
    verdict raise `StoreWriteError` on False, because a swallowed failure here is how a
    finding gets lost while the agent is told it was filed.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)
        with contextlib.suppress(OSError, NameError):
            tmp.unlink(missing_ok=True)   # don't leave a half-written temp behind
        return False


def _write_or_raise(path: Path, payload: Any) -> None:
    """Persist, or raise so the caller can tell the agent the truth."""
    if not _write_json_atomic(path, payload):
        raise StoreWriteError(f"Could not write {path}. The change was not saved.")


# --------------------------------------------------------------------------------------
# Sub-projects
# --------------------------------------------------------------------------------------
#: Every status a module can be in, in lifecycle order. A tuple here for the same reason
#: FINDING_KINDS and NOTE_KINDS are: the vocabulary had been written as a comment, and the
#: comment drifted — it documented `running` and `done`, neither of which anything ever
#: wrote, while the value actually written on completion (`tested`) was absent from it. The
#: authority is the frontend, which styles one badge per status (css/agent.css) and branches
#: on `proposed`; these are the three it knows.
SUBPROJECT_STATUSES = ("proposed", "approved", "tested")

#: The manager module, created with the project. It owns the breakdown — it interviews the
#: user, looks at the app, creates modules and reads back what they found — and it is the one
#: module that never files a finding of its own.
MAIN_SLUG = "main"

#: What the manager module was called before it was one. Projects created earlier have their
#: setup interview under `onboarding/`, and the folder name is the slug: renaming it would
#: mean moving the transcript, so instead both names resolve to the manager. Kept as a
#: separate constant rather than a string literal in four places because the whole point is
#: that every caller agrees on which slug is the manager.
LEGACY_MAIN_SLUG = "onboarding"


def is_main_slug(slug: str) -> bool:
    """Whether this module is the project's manager, under either of its names."""
    return slug in (MAIN_SLUG, LEGACY_MAIN_SLUG)


def main_slug(package: str) -> str:
    """The slug this project's manager module actually lives under.

    A project made today has `main/`; one made before the manager existed has `onboarding/`
    and keeps it. Returns `MAIN_SLUG` when there is no manager yet, which is what a caller
    about to create one wants — so "find it" and "where would it go" are the same call and
    cannot disagree.

    Prefers `main` when a project somehow has both, because that is the one the current code
    creates and writes to.
    """
    slugs = {str(s.get("slug") or "") for s in list_subprojects(package)}
    if MAIN_SLUG in slugs:
        return MAIN_SLUG
    if LEGACY_MAIN_SLUG in slugs:
        return LEGACY_MAIN_SLUG
    return MAIN_SLUG


def list_subprojects(package: str) -> list[dict[str, Any]]:
    data = _read_json(_subprojects_path(package), {})
    items = data.get("subprojects", []) if isinstance(data, dict) else []
    return [i for i in items if isinstance(i, dict)]


def get_subproject(package: str, slug: str) -> Optional[dict[str, Any]]:
    return next((s for s in list_subprojects(package) if s.get("slug") == slug), None)


def save_subprojects(package: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The module list. The other deliberate non-raiser, and the reasoning is asymmetric.

    `add_finding` writes findings.json first and only then bumps `finding_count` through
    here. If this write is the one that fails, the count reads low while the verdict itself
    is safely on disk — the report and the pills are rebuilt from the per-module findings
    files (`list_all_findings`), so nothing is lost and nothing is over-claimed. Raising
    would undo a filing that actually succeeded, which is the worse of the two errors.
    """
    _write_json_atomic(_subprojects_path(package), {"package": package,
                                                    "updated_at": _now(),
                                                    "subprojects": items})
    return items


def create_subproject(package: str, title: str, scope: str = "",
                      status: str = "proposed", screens: Optional[list[str]] = None,
                      ) -> dict[str, Any]:
    """Add a sub-project. Idempotent on slug: re-creating an existing one updates its scope
    rather than producing a duplicate module the user would have to reconcile by hand."""
    with _LOCK:
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
            "status": status,          # one of SUBPROJECT_STATUSES
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
    with _LOCK:
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
    with _LOCK:
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
    # Append-only is what makes the transcript safe against a *crash*; the lock is what makes
    # it safe against a concurrent *writer*. An agent's text block can be several KB, which is
    # past the point where a single write lands indivisibly — two unlocked appends can
    # interleave into one corrupt line, and `read_chat` would silently drop it.
    try:
        with _LOCK:
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


#: Every outcome a test case can end in. Ordered worst-first, which is the order the report
#: and the popups read in — a reviewer wants the bugs before the things that worked.
FINDING_KINDS = ("bug", "warning", "suggestion", "pass")


def list_findings(package: str, slug: str) -> list[dict[str, Any]]:
    data = _read_json(_findings_path(package, slug), [])
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # Findings filed before `kind` existed were all defects, by construction — the tool
        # could not record anything else. Defaulting them to "bug" keeps old projects showing
        # what they always showed, rather than dropping out of every bucket.
        kind = item.get("kind")
        out.append({**item, "kind": kind if kind in FINDING_KINDS else "bug"})
    return out


def list_all_findings(package: str) -> list[dict[str, Any]]:
    """Every module's outcomes for one project, each tagged with the module it came from.

    Read from the per-module files rather than a project-level aggregate that would have to be
    kept in step with them. One writer per file is what makes concurrent modules safe (see
    `_LOCK`); a shared rollup file would put every module back in contention for one lock and
    give the aggregate its own way of going stale.
    """
    out: list[dict[str, Any]] = []
    for entry in list_subprojects(package):
        slug = str(entry.get("slug") or "")
        if not slug:
            continue
        title = str(entry.get("title") or slug)
        for finding in list_findings(package, slug):
            out.append({**finding, "module_slug": slug, "module_title": title})
    return out


def add_finding(package: str, slug: str, finding: dict[str, Any]) -> dict[str, Any]:
    """File an outcome. Raises StoreWriteError if it could not be persisted.

    The count is bumped only after the findings file is safely on disk. Bumping first is
    how `finding_count` ends up claiming a verdict that `findings.json` does not contain —
    the pill in the top bar would show it and the report would not.
    """
    # Held across the whole sequence: the id is derived from the current count, so an
    # interleaved second filing would otherwise hand out F002 twice and drop one of them.
    with _LOCK:
        findings = list_findings(package, slug)
        record = {"id": f"F{len(findings) + 1:03d}", "ts": _now(), **finding}
        findings.append(record)
        _write_or_raise(_findings_path(package, slug), findings)
        update_subproject(package, slug, finding_count=len(findings))
        return record


def link_finding(package: str, slug: str, finding_id: str,
                 node: str) -> Optional[dict[str, Any]]:
    """Point an already-filed finding at the screen it was about. None if there is no such id.

    Filing and knowing which node to name do not always happen at the same moment: the agent
    often records the verdict before it records the step that shows it, and a whole run can
    be reviewed after the fact, when every screen is on the board and the right one is
    obvious. Without this the only way to outline a screen would be to file the finding
    again, which would leave two verdicts for one case and inflate the counts.
    """
    with _LOCK:
        findings = list_findings(package, slug)
        found = None
        for item in findings:
            if item.get("id") == finding_id:
                item["node"] = node
                found = item
        if found is None:
            return None
        _write_or_raise(_findings_path(package, slug), findings)
        return found


#: Fields `set_finding_tracking` is allowed to touch. Kept narrow on purpose — this is the
#: one place a finding is edited after the fact from outside the test run that filed it, so
#: it must not become a general-purpose patch that could silently rewrite `expected`/`actual`.
#:
#: `cluster` is the id of the defect this finding turned out to be one report of — five apps
#: on one backend means the same defect gets filed once per app by agents that cannot see
#: each other, and a quarter of the first ecosystem-wide pass was duplicates. `clusters.py`
#: owns the grouping and stamps this field; it is a cache of that file, not a second source
#: of truth, so a module agent can see "already known" without loading the whole ecosystem.
FINDING_TRACKING_FIELDS = ("resolved", "issue_url", "issue_id", "cluster")


def set_finding_tracking(package: str, slug: str, finding_id: str,
                          **fields: Any) -> Optional[dict[str, Any]]:
    """Record where a finding is tracked externally (e.g. a Blackcode issue) and whether
    it's resolved. None if there is no such finding id.

    Separate from `link_finding` because that one is the agent pointing a finding at a
    screen it already knows about mid-run; this one is the dashboard recording an outcome
    that happens entirely outside the test run, at any point after the finding was filed.
    Only keys in FINDING_TRACKING_FIELDS are applied — anything else in `fields` is ignored
    rather than raising, so a caller can pass a dict straight through without pre-filtering.
    """
    with _LOCK:
        findings = list_findings(package, slug)
        found = None
        for item in findings:
            if item.get("id") == finding_id:
                for key in FINDING_TRACKING_FIELDS:
                    if key in fields:
                        item[key] = fields[key]
                found = item
        if found is None:
            return None
        _write_or_raise(_findings_path(package, slug), findings)
        return found


# --------------------------------------------------------------------------------------
# Flow-graph steps
#
# A thin index of what this module has drawn: node id, label, section. The screens themselves
# live on the board and their screenshots in the project — this is only what is needed to
# answer "which node was that?", which is the question between filing a finding and
# outlining the screen it was about.
#
# Written here rather than read back off flow-graph.json, which is megabytes of base64 JPEG
# (the deskclock board is 6.5 MB) and is not even guaranteed to exist: the board is saved by
# the browser, so a run with no tab open draws nothing and still needs its steps recorded.
# --------------------------------------------------------------------------------------
def _steps_path(package: str, slug: str) -> Path:
    return subproject_dir(package, slug) / "steps.json"


def _recorded_steps(package: str, slug: str) -> list[dict[str, Any]]:
    """Only what this module wrote. Never the board fallback — see record_step."""
    data = _read_json(_steps_path(package, slug), [])
    if not isinstance(data, list):
        return []
    return [s for s in data if isinstance(s, dict) and s.get("node")]


def list_steps(package: str, slug: str) -> list[dict[str, Any]]:
    return _recorded_steps(package, slug) or _steps_from_board(package, slug)


def _steps_from_board(package: str, slug: str) -> list[dict[str, Any]]:
    """Recover a module's steps from the saved board, for runs that predate steps.json.

    The board already holds every node this module drew, each carrying the section it was
    filed under and the name built from its step label — so this reads the run's own record
    rather than reconstructing one. Nothing is inferred: a node is this module's if its
    section says so.

    Parsed here and not by the agent. The file is mostly base64 JPEG — the deskclock board is
    6.5 MB, about 1.6M tokens — which is why `.claude/settings.json` denies reading it and
    `tools/inspect_board.py` exists. Server-side the screenshots are simply ignored.
    """
    path = project_dir(package) / "flow-graph.json"
    if not path.is_file():
        return []
    try:
        with path.open(encoding="utf-8") as fh:
            board = json.load(fh)
    except (OSError, ValueError):
        return []
    steps: list[dict[str, Any]] = []
    for node in board.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        section = str(node.get("section") or "")
        if section != slug and not section.startswith(f"{slug} / "):
            continue
        steps.append({"node": node.get("hash"), "section": section,
                      "label": str(node.get("screenName") or ""), "ts": ""})
    return [s for s in steps if s["node"]]


def record_step(package: str, slug: str, node: str, label: str, section: str) -> None:
    """Append one step. Re-posting the same node id updates it rather than duplicating.

    Reads `_recorded_steps`, not `list_steps`. Going through the public one would fold the
    board fallback into the file on the very first step of a re-run — writing every node the
    module drew in some earlier session into this one's index, undated and possibly long
    since gone from the board. The fallback answers a question; it is not a starting point.
    """
    with _LOCK:
        steps = [s for s in _recorded_steps(package, slug) if s.get("node") != node]
        steps.append({"node": node, "label": label, "section": section, "ts": _now()})
        _write_or_raise(_steps_path(package, slug), steps)


# --------------------------------------------------------------------------------------
# Board notes
#
# What the agent wants to say about a test case, in its own words, pinned beside that case
# on the flow graph. A finding is a verdict in a fixed shape — expected, actual, evidence —
# and it lives in a list you open. A note is prose on the board itself, which is where
# someone actually looks when they are trying to understand a run rather than audit it.
#
# Kept here rather than in the board file, for the same reason findings are: the board is
# written by the browser's autosave, so a note the agent filed while no tab was open would
# be lost, and one filed while a tab *was* open would race the autosave for the same file.
# The browser reads these and draws them; it never writes them back.
# --------------------------------------------------------------------------------------
def _notes_path(package: str, slug: str) -> Path:
    return subproject_dir(package, slug) / "notes.json"


#: Green, amber, red — the note's colour and the colour of its flow's connectors. Matches
#: FINDING_KINDS so a case's note and its verdicts cannot disagree about what happened.
NOTE_KINDS = ("bug", "warning", "suggestion", "pass")


def list_notes(package: str, slug: str) -> list[dict[str, Any]]:
    data = _read_json(_notes_path(package, slug), [])
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        out.append({**item, "kind": kind if kind in NOTE_KINDS else "pass"})
    return out


def list_all_notes(package: str) -> list[dict[str, Any]]:
    """Every module's notes for one project, tagged with the module they came from."""
    out: list[dict[str, Any]] = []
    for entry in list_subprojects(package):
        slug = str(entry.get("slug") or "")
        if not slug:
            continue
        title = str(entry.get("title") or slug)
        for note in list_notes(package, slug):
            out.append({**note, "module_slug": slug, "module_title": title})
    return out


def add_note(package: str, slug: str, note: dict[str, Any]) -> dict[str, Any]:
    # Same lock and the same reason as add_finding: the id comes from the current count, so
    # two notes written in one breath would otherwise both be N002 and one would vanish.
    with _LOCK:
        notes = list_notes(package, slug)
        # Re-writing the note for a section replaces it. A case gets one note; an agent that
        # revises its conclusion after seeing more should not leave the earlier, wrong one
        # sitting on the board underneath the new one.
        section = note.get("section")
        if section:
            notes = [n for n in notes if n.get("section") != section]
        record = {"id": f"N{len(notes) + 1:03d}", "ts": _now(), **note}
        notes.append(record)
        _write_or_raise(_notes_path(package, slug), notes)
        return record


# --------------------------------------------------------------------------------------
# Test credentials
# --------------------------------------------------------------------------------------
def get_secrets(package: str) -> dict[str, str]:
    data = _read_json(_secrets_path(package), {})
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def set_secret(package: str, key: str, value: str) -> None:
    """Store a test credential. Raises StoreWriteError if it could not be persisted.

    Loudly, because the value is deliberately not kept anywhere else: the transcript stores
    a redaction, so a swallowed failure here loses the credential outright and the next
    `use_credential` asks the user for something they already gave.
    """
    # Two credentials answered in quick succession (the agent asks for an email, then a
    # password) would otherwise race and leave only one of them stored.
    with _LOCK:
        secrets = get_secrets(package)
        secrets[key] = value
        _write_or_raise(_secrets_path(package), secrets)


def secret_keys(package: str) -> list[str]:
    """Names only. The agent is told which credentials exist and asks for one by name;
    values are injected into a tool call server-side and never written into the prompt,
    the transcript, or the report."""
    return sorted(get_secrets(package).keys())
