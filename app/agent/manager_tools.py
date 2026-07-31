"""The Main module's extra tools: manage the modules, read their work, roll it up.

Registered only for the manager module (`store.is_main_slug`), on top of the device tools it
shares with every tester — the manager needs the phone, because "suggest a module for the part
of the app I have not covered" means looking at the app.

What is deliberately absent is as important as what is here. The manager has no way to write
into another module: no `record_finding` for someone else's suite, no edit of their memory, no
start-a-run. Three reasons, in the order they matter:

* **A finding must stay a verdict.** It is one named test case with a screenshot behind it.
  The manager forms impressions while mapping an app — "the cart total looked stale" — and if
  those land in the same list, the project's bug count is partly guesswork and nothing
  downstream can separate the two. The prompt says so; having no tool is what enforces it.
* **One phone, one driver.** Each module owns a `DeviceSession`, but there is a single device
  behind all of them and no lock across sessions. A manager that could start another module's
  run would have two agents tapping the same screen and each reading the other's consequences
  as its app's behaviour — the same class of failure as a dump misread, but harder to spot
  because both agents are behaving correctly.
* **A module's memory is its own.** It is written by the agent that earned it, from runs it
  watched. A manager rewriting it from a summary would replace observation with inference.

So everything here either reads, or creates a module and says so. `read_module` is the wide
one, and it reads three different files rather than the transcript on purpose: findings are
the verdicts, notes are what the agent said about each case on the board, and memory is what
it learned. The conversation is much longer than all three together and mostly consists of
the agent narrating taps.
"""
from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent import store

__all__ = ["build_manager_server", "MANAGER_TOOL_NAMES", "manager_tool_names"]

#: How much of a module's memory file `read_module` will quote. Memory grows for the life of a
#: project — one confirmed behaviour per bullet, a run at a time — and the manager often reads
#: several modules in one turn. Truncated *and labelled as truncated*: an agent that thinks it
#: has read a whole file will answer "there is nothing about X in Checkout's memory" when the
#: answer was past the cut.
MEMORY_CHARS = 4000

#: How many findings `project_report` lists in full. The per-module counts are always complete;
#: this caps only the detail lines, and the tool says how many it did not print.
REPORT_DETAIL_LIMIT = 40

#: Worst first, the order a reviewer wants: bugs before the things that worked. Mirrors
#: store.FINDING_KINDS, which is ordered for the same reason.
_KIND_ORDER = ("bug", "warning", "suggestion", "pass")


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    tally = {kind: 0 for kind in _KIND_ORDER}
    for finding in findings:
        kind = str(finding.get("kind") or "bug")
        tally[kind] = tally.get(kind, 0) + 1
    return tally


def _tally_line(tally: dict[str, int]) -> str:
    """Renders as `2 bugs, 1 warning, 5 passes`, or `nothing filed` when every count is zero."""
    plural = {"bug": "bugs", "warning": "warnings", "suggestion": "suggestions",
              "pass": "passes"}
    parts = [f"{tally[k]} {k if tally[k] == 1 else plural[k]}"
             for k in _KIND_ORDER if tally.get(k)]
    return ", ".join(parts) if parts else "nothing filed"


def _module_line(entry: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    slug = str(entry.get("slug") or "")
    title = str(entry.get("title") or slug)
    bits = [f"{slug}  ({title})",
            f"status: {entry.get('status', '?')}",
            _tally_line(_counts(findings))]
    last_run = entry.get("last_run_at")
    bits.append(f"last run: {last_run}" if last_run else "never run")
    scope = str(entry.get("scope") or "").strip()
    line = "  |  ".join(bits)
    return f"{line}\n      scope: {scope}" if scope else line


def build_manager_server(session: Any) -> dict[str, Any]:
    """The manager's MCP server, bound to one manager session's package.

    Takes the same `DeviceSession` the device tools are built from, for its `package`, its
    `slug` and its `_emit` — not for the device. Nothing here touches the phone.
    """

    @tool("list_modules",
          "Every module in this project with its status, how many outcomes it has filed and "
          "when it last ran. This is the state of the project — start here rather than "
          "reasoning from what you remember, because modules run between your turns.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def list_modules(_args: dict[str, Any]) -> dict[str, Any]:
        entries = await asyncio.to_thread(store.list_subprojects, session.package)
        if not entries:
            return _ok("This project has no modules yet. Use propose_subprojects to suggest a "
                       "breakdown, or create_module if the user has asked for a specific one.")
        lines = []
        for entry in entries:
            slug = str(entry.get("slug") or "")
            if not slug:
                continue
            findings = await asyncio.to_thread(store.list_findings, session.package, slug)
            marker = "  <- you" if slug == session.slug else ""
            lines.append(_module_line(entry, findings) + marker)
        return _ok("\n".join(lines))

    @tool("read_module",
          "One module's work: every outcome it filed, the notes it pinned to the board, and "
          "its memory file. Use it to answer what a module actually found, or to notice that "
          "two modules have filed the same defect. It does not include the conversation — the "
          "findings are the verdicts and the notes are the agent's account of each case.",
          {"type": "object",
           "properties": {"slug": {"type": "string",
                                   "description": "Module slug, as list_modules shows it"}},
           "required": ["slug"], "additionalProperties": False})
    async def read_module(args: dict[str, Any]) -> dict[str, Any]:
        slug = str(args.get("slug") or "").strip()
        entry = await asyncio.to_thread(store.get_subproject, session.package, slug)
        if entry is None:
            # Named, not guessed at. A manager that silently read the wrong module would
            # attribute one suite's defects to another, and the summary would look fine.
            known = [str(s.get("slug")) for s in
                     await asyncio.to_thread(store.list_subprojects, session.package)]
            return _err(f"No module {slug!r} in this project. The modules are: "
                        + (", ".join(known) if known else "none yet") + ".")

        findings = await asyncio.to_thread(store.list_findings, session.package, slug)
        notes = await asyncio.to_thread(store.list_notes, session.package, slug)
        memory = await asyncio.to_thread(store.read_memory, session.package, slug)

        out = [f"# {entry.get('title') or slug}  (`{slug}`)",
               f"Status: {entry.get('status', '?')}",
               f"Scope: {entry.get('scope') or '(none recorded)'}",
               f"Last run: {entry.get('last_run_at') or 'never'}",
               "",
               f"## Outcomes — {_tally_line(_counts(findings))}"]
        if not findings:
            out.append("Nothing filed. Either it has not run, or it ran and recorded nothing "
                       "— those are different, and `last run` above tells you which.")
        for finding in sorted(findings,
                              key=lambda f: _KIND_ORDER.index(str(f.get("kind") or "bug"))
                              if str(f.get("kind") or "bug") in _KIND_ORDER else 99):
            head = (f"{finding.get('id')} [{finding.get('kind', 'bug')}"
                    f"/{finding.get('severity', '?')}] {finding.get('title', '')}")
            out.append(head)
            for label in ("expected", "actual"):
                if finding.get(label):
                    out.append(f"    {label}: {finding[label]}")
            if finding.get("node"):
                out.append(f"    screen: {finding['node']}")

        out += ["", f"## Board notes ({len(notes)})"]
        if not notes:
            out.append("None pinned.")
        for note in notes:
            out.append(f"[{note.get('kind', 'pass')}] {note.get('section', '?')}: "
                       f"{note.get('text', '')}")

        out += ["", "## Its memory file"]
        if not memory.strip():
            out.append("Empty — it has not recorded anything about this part of the app yet.")
        elif len(memory) > MEMORY_CHARS:
            out.append(memory[:MEMORY_CHARS])
            out.append(f"\n[...truncated. {len(memory) - MEMORY_CHARS} more characters in "
                       f"{store.memory_path(session.package, slug)} — Read that file directly "
                       f"if you need the rest. Do not conclude something is absent from this "
                       f"module's memory from what is printed above.]")
        else:
            out.append(memory)
        return _ok("\n".join(out))

    @tool("create_module",
          "Create one module, for when the user has asked for it. For your own suggestions use "
          "propose_subprojects instead, which arrives as a proposal they approve. Creating a "
          "module does not run it — it waits for the user to open it and say what to test.",
          {"type": "object",
           "properties": {
               "title": {"type": "string",
                         "description": "What the app calls this area, not a generic label"},
               "scope": {"type": "string",
                         "description": "What testing this module covers, in a line"},
               "screens": {"type": "array", "items": {"type": "string"},
                           "description": "Screens it covers, if you know them"},
           },
           "required": ["title", "scope"], "additionalProperties": False})
    async def create_module(args: dict[str, Any]) -> dict[str, Any]:
        title = str(args.get("title") or "").strip()
        scope = str(args.get("scope") or "").strip()
        if not title:
            return _err("A module needs a title.")

        slug = store.slugify(title)
        if store.is_main_slug(slug):
            # There is one manager and it is this session. A second module on the same slug
            # would either collide with this conversation's folder or quietly become a tester
            # holding the manager's prompt.
            return _err(f"{slug!r} is the manager module — this one. Pick a name for the part "
                        f"of the app the module covers.")

        existed = await asyncio.to_thread(store.get_subproject, session.package, slug)
        try:
            entry = await asyncio.to_thread(
                store.create_subproject, session.package, title, scope, "approved",
                args.get("screens") or [])
        except OSError as exc:
            # OSError, not StoreWriteError: `create_subproject` writes the listing through
            # `save_subprojects`, which deliberately does not raise, and then mkdirs the
            # module's folder — which does. A project may live on any drive (see
            # project_paths), so an unplugged disk or a Windows AV lock lands here, and
            # reporting a module the rail will not show is worse than reporting the failure.
            return _err(f"The module was NOT created: {exc}")

        await session._emit({"type": "agent_subprojects_proposed", "subprojects": [entry]})
        # `create_subproject` is idempotent on slug, so this call may have updated an existing
        # module rather than added one. Saying "created" either way is the harness reporting
        # something it did not verify — and the user would go looking for a new module in the
        # rail that is not there.
        if existed:
            return _ok(f"{slug!r} already existed, so its scope was updated rather than a "
                       f"second module being created. It now reads: {entry.get('scope') or '(none)'}. "
                       f"Tell the user it was an update, not a new module.")
        return _ok(f"Created module {slug!r} ({entry['title']}), status approved. It is in the "
                   f"rail and has run nothing — the user opens it and tells it what to test.")

    @tool("project_report",
          "Every module's outcomes gathered in one place, worst first, with the totals. Read "
          "this before saying anything about where the project stands — counting across six "
          "modules from memory is how a summary starts being wrong.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def project_report(_args: dict[str, Any]) -> dict[str, Any]:
        entries = await asyncio.to_thread(store.list_subprojects, session.package)
        if not entries:
            return _ok("No modules, so nothing to report on yet.")
        findings = await asyncio.to_thread(store.list_all_findings, session.package)

        out = [f"# {session.package} — every module's outcomes", "",
               f"Total: {_tally_line(_counts(findings))} across {len(entries)} modules.", ""]

        never_run = []
        for entry in entries:
            slug = str(entry.get("slug") or "")
            if not slug or store.is_main_slug(slug):
                continue    # the manager files nothing; a zero row for it is noise
            mine = [f for f in findings if f.get("module_slug") == slug]
            out.append(f"{slug}: {_tally_line(_counts(mine))}"
                       f"  ({entry.get('status', '?')})")
            if not entry.get("last_run_at"):
                never_run.append(slug)

        if never_run:
            # The distinction the report exists to protect: a module with no bugs may be a
            # module that works, or one nobody has run. Reporting "no defects" for the second
            # is the most expensive wrong sentence this tool could help write.
            out += ["", "Never run — these have no outcomes because nothing has tested them, "
                        "not because they passed: " + ", ".join(never_run)]

        detail = sorted(findings,
                        key=lambda f: _KIND_ORDER.index(str(f.get("kind") or "bug"))
                        if str(f.get("kind") or "bug") in _KIND_ORDER else 99)
        out += ["", f"## Detail, worst first ({len(detail)})"]
        for finding in detail[:REPORT_DETAIL_LIMIT]:
            out.append(f"{finding.get('module_slug')}/{finding.get('id')} "
                       f"[{finding.get('kind', 'bug')}/{finding.get('severity', '?')}] "
                       f"{finding.get('title', '')}")
        if len(detail) > REPORT_DETAIL_LIMIT:
            out.append(f"[...{len(detail) - REPORT_DETAIL_LIMIT} more not printed. The counts "
                       f"above are complete; use read_module for a module's full list.]")
        return _ok("\n".join(out))

    return create_sdk_mcp_server(
        name="manager",
        version="1.0.0",
        tools=[list_modules, read_module, create_module, project_report],
    )


# Tool names as the agent sees them, for the allow-list in runtime.py.
MANAGER_TOOL_NAMES = [
    "mcp__manager__list_modules", "mcp__manager__read_module",
    "mcp__manager__create_module", "mcp__manager__project_report",
]


def manager_tool_names() -> list[str]:
    """A copy, so a caller extending its own allow-list cannot append to this list."""
    return list(MANAGER_TOOL_NAMES)
