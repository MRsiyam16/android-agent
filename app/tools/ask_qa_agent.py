"""Let another agent (or a human at a terminal) ask the QA agent what it knows.

Every project's memory already lives on disk in a fixed shape — `agent/store.py` describes it:
a module's `memory.md`, its `findings.json`, its `chat.jsonl`. This is the door into that for
someone who is not the dashboard: a caller that only has a shell.

Two ways in:

    python tools/ask_qa_agent.py projects
    python tools/ask_qa_agent.py modules <package>
    python tools/ask_qa_agent.py findings <package> [--module SLUG] [--kind bug] [--json]

read the stored state directly — instant, free, exact. Good for "what bugs exist" style
questions a script can answer by filtering JSON.

    python tools/ask_qa_agent.py ask <package> "why does checkout look flaky?" [--module SLUG]

hands the same memory to a one-shot Claude Code call (`claude_agent_sdk.query`, no tools, no
device, `max_turns=1`) and returns a synthesized answer, for a question that needs reasoning
over the notes rather than a filter. It costs one subscription-window call; the read-only
commands above cost nothing and should be preferred when a filter would do.

Never drives the phone and never writes anything — asking what the QA agent knows should not
be able to change what it knows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import project_paths  # noqa: E402
from agent import store  # noqa: E402

# Context handed to the synthesizer is capped so one very chatty project can't blow the
# system prompt (and therefore the cost and latency) of a single question out to the whole
# transcript. Findings and memory are what "what does the QA agent know" actually means;
# the raw chat log is included only if asked for, and only its tail.
MAX_FINDINGS_PER_MODULE = 200
MAX_CONTEXT_CHARS = 120_000


# --------------------------------------------------------------------------------------
# read-only lookups — no LLM, no cost
# --------------------------------------------------------------------------------------
def cmd_projects(_args: argparse.Namespace) -> int:
    packages = sorted(project_paths.known_packages())
    rows = []
    for package in packages:
        subprojects = store.list_subprojects(package)
        findings = sum(len(store.list_findings(package, s.get("slug", "")))
                       for s in subprojects)
        rows.append({"package": package, "modules": len(subprojects), "findings": findings,
                     "root": str(project_paths.project_dir(package))})
    if getattr(_args, "json", False):
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No projects found.")
        return 0
    for row in rows:
        print(f"{row['package']:<45} {row['modules']:>2} modules  "
              f"{row['findings']:>3} findings  {row['root']}")
    return 0


def cmd_modules(args: argparse.Namespace) -> int:
    subprojects = store.list_subprojects(args.package)
    if getattr(args, "json", False):
        print(json.dumps(subprojects, indent=2))
        return 0
    if not subprojects:
        print(f"No modules for {args.package}.")
        return 0
    for sp in subprojects:
        print(f"{sp.get('slug'):<20} {sp.get('status'):<10} "
              f"{sp.get('finding_count', 0):>3} findings   {sp.get('title', '')}")
    return 0


def _module_slugs(package: str, module: Optional[str]) -> list[str]:
    if module:
        return [module]
    return [s.get("slug", "") for s in store.list_subprojects(package) if s.get("slug")]


def cmd_findings(args: argparse.Namespace) -> int:
    slugs = _module_slugs(args.package, args.module)
    out: list[dict[str, Any]] = []
    for slug in slugs:
        for finding in store.list_findings(args.package, slug):
            if args.kind and finding.get("kind") != args.kind:
                continue
            out.append({**finding, "module_slug": slug})
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
        return 0
    if not out:
        print("No findings match.")
        return 0
    for f in out:
        print(f"[{f.get('module_slug')}] {f.get('id')} {str(f.get('kind')).upper():<10} "
              f"{f.get('title', '')}")
        if f.get("expected"):
            print(f"    expected: {f['expected']}")
        if f.get("actual"):
            print(f"    actual:   {f['actual']}")
    return 0


# --------------------------------------------------------------------------------------
# context assembly for the synthesized "ask"
# --------------------------------------------------------------------------------------
def _format_finding(finding: dict[str, Any]) -> str:
    kind = str(finding.get("kind") or "bug").upper()
    lines = [f"- {finding.get('id', '?')} [{kind}] {finding.get('title', '')}".rstrip()]
    if finding.get("expected"):
        lines.append(f"  expected: {finding['expected']}")
    if finding.get("actual"):
        lines.append(f"  actual: {finding['actual']}")
    if finding.get("resolved"):
        lines.append("  (marked resolved)")
    return "\n".join(lines)


def gather_context(package: str, module: Optional[str], chat_tail: int) -> str:
    subprojects = {s.get("slug"): s for s in store.list_subprojects(package)}
    slugs = [module] if module else list(subprojects.keys())
    missing = [s for s in slugs if s not in subprojects]
    if missing:
        known = ", ".join(subprojects) or "(none)"
        raise SystemExit(f"No such module(s) {missing} in {package}. Known modules: {known}")

    parts = [f"# Project: {package}", f"Root: {project_paths.project_dir(package)}", ""]
    if not slugs:
        parts.append("This project has no modules yet — nothing has been tested.")

    for slug in slugs:
        sp = subprojects[slug]
        parts.append(f"## Module: {sp.get('title', slug)} (`{slug}`)  "
                     f"status={sp.get('status')}  findings={sp.get('finding_count', 0)}")
        if sp.get("scope"):
            parts.append(f"Scope: {sp['scope']}")

        memory = store.read_memory(package, slug).strip()
        if memory:
            parts.append("\n### What the agent learned about this module\n" + memory)

        findings = store.list_findings(package, slug)
        if findings:
            shown = findings[:MAX_FINDINGS_PER_MODULE]
            parts.append(f"\n### Findings ({len(findings)})")
            parts.extend(_format_finding(f) for f in shown)
            if len(findings) > len(shown):
                parts.append(f"... {len(findings) - len(shown)} more findings omitted "
                             f"for length, oldest dropped first.")

        if chat_tail:
            chat = store.read_chat(package, slug, limit=chat_tail)
            if chat:
                parts.append(f"\n### Last {len(chat)} transcript entries")
                for entry in chat:
                    role = entry.get("role", "?")
                    text = (entry.get("text") or entry.get("summary") or "")[:300]
                    if text:
                        parts.append(f"[{role}] {text}")
        parts.append("")

    context = "\n".join(parts)
    if len(context) > MAX_CONTEXT_CHARS:
        context = (context[:MAX_CONTEXT_CHARS] +
                  "\n\n[...context truncated, it ran over the length this tool will send...]")
    return context


async def _ask(package: str, module: Optional[str], question: str, model: Optional[str],
               effort: Optional[str], chat_tail: int) -> str:
    # Imported lazily: the read-only subcommands above have no reason to require the CLI SDK
    # to be importable, and this is the only path that spawns the Claude Code CLI.
    import tempfile

    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    context = gather_context(package, module, chat_tail)
    system_prompt = (
        "You are the memory of a QA testing harness, being asked a question by another AI "
        "agent (or a developer) who is not the one that ran the tests. Answer using ONLY the "
        "context below, which is real state from actual test runs — module notes and filed "
        "findings, not documentation. Cite finding ids (e.g. F003) when you reference one. "
        "If the context does not contain an answer, say plainly that it is not known rather "
        "than guessing or inventing detail. Be concise — a few sentences or a short list, not "
        "a report.\n\n" + context
    )
    # Passed inline, a Windows command line over ~32,767 chars makes the spawn itself fail
    # with WinError 206, which the SDK reports as CLINotFoundError — "install the CLI" for a
    # prompt that was simply too long. runtime.py hit the same wall; the fix is the same:
    # a project with more than a couple of modules routinely produces a system prompt in the
    # tens of KB, so this always goes to a file rather than trying inline first and refusing
    # to work only once a caller happens to ask about a bigger project.
    prompt_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="ask-qa-agent-", delete=False, encoding="utf-8")
    try:
        prompt_file.write(system_prompt)
        prompt_file.close()
        options = ClaudeAgentOptions(
            system_prompt={"type": "file", "path": prompt_file.name},
            allowed_tools=[],
            permission_mode="bypassPermissions",
            max_turns=1,
            model=model or config.AGENT_PLANNER_MODEL or None,
            effort=effort or config.AGENT_PLANNER_EFFORT,  # type: ignore[arg-type]
        )
        if config.AGENT_PLANNER_FALLBACK:
            options.fallback_model = config.AGENT_PLANNER_FALLBACK

        answer_parts: list[str] = []
        async for message in query(prompt=question, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        answer_parts.append(block.text)
        return "\n".join(answer_parts).strip() or "(the agent returned no text)"
    finally:
        Path(prompt_file.name).unlink(missing_ok=True)


def cmd_ask(args: argparse.Namespace) -> int:
    try:
        answer = asyncio.run(_ask(args.package, args.module, args.question, args.model,
                                  args.effort, args.chat))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - surface CLI/SDK failures plainly to a script
        message = str(exc)
        if getattr(args, "json", False):
            print(json.dumps({"error": message}))
        else:
            print(f"error: {message}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps({"package": args.package, "module": args.module,
                          "question": args.question, "answer": answer}, indent=2))
    else:
        print(answer)
    return 0


# --------------------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("projects", help="list every project this harness knows about")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("modules", help="list a project's test modules")
    p.add_argument("package")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_modules)

    p = sub.add_parser("findings", help="list filed findings, raw, no synthesis")
    p.add_argument("package")
    p.add_argument("--module", help="restrict to one module slug")
    p.add_argument("--kind", choices=["bug", "warning", "suggestion", "pass"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_findings)

    p = sub.add_parser(
        "ask", help="ask a question in English, answered from the project's memory")
    p.add_argument("package")
    p.add_argument("question")
    p.add_argument("--module", help="restrict context to one module slug")
    p.add_argument("--model", help="override the planner model (default: config/subscription)")
    p.add_argument("--effort", help="override reasoning effort (default: config)")
    p.add_argument("--chat", type=int, default=0, metavar="N",
                   help="include the last N transcript entries per module (default: 0, off)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ask)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
