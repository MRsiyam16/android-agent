"""Generates/updates a human- and agent-readable Markdown report per app project
(android-agent/projects/<package>/REPORT.md), built from memory.py's persistent
per-app data.

The point: a *different* AI agent session (or a human) picking this project back
up later should be able to read this one file and know what's already been
explored, what's broken, and what's still open — instead of re-discovering it, or
re-running an already-covered flow, from scratch.

The file has two sections, split by HTML comment sentinels:
- AUTO    — fully rewritten on every write_report() call (states/screens/bugs
  summary derived from memory.py). Never hand-edit this part, it will be lost.
- MANUAL  — preserved verbatim across calls. Free-text findings a human or an
  agent adds by hand (e.g. "checklist note flow not yet verified, device
  disconnected mid-test") via append_manual_note().
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import memory as memory_module
from memory import AppMemory

AUTO_BEGIN = "<!-- AUTO:BEGIN -->"
AUTO_END = "<!-- AUTO:END -->"
MANUAL_BEGIN = "<!-- MANUAL:BEGIN -->"
MANUAL_END = "<!-- MANUAL:END -->"

DEFAULT_MANUAL_BODY = (
    "_No manual notes yet. Add findings here (flows tried, bugs seen, screens still "
    "unverified) so a future session can pick up where this one left off instead of "
    "re-exploring already-covered ground._"
)


def _report_path(package: str) -> str:
    return os.path.join(memory_module.project_dir(package), "REPORT.md")


def _load_meta(package: str) -> dict:
    path = os.path.join(memory_module.project_dir(package), "meta.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _render_auto_section(mem: AppMemory, meta: dict) -> str:
    lines: list[str] = [AUTO_BEGIN, ""]
    lines.append(f"**Last tested:** {meta.get('last_run_at', 'unknown')}")
    lines.append(f"**States discovered:** {len(mem.known_state_hashes)}")
    lines.append(f"**Bugs/crashes flagged:** {len(mem.bug_reports)}")
    lines.append("")

    lines.append("## Known screens (by activity)")
    by_activity: dict[str, int] = {}
    for state_hash in mem.known_state_hashes:
        activity = mem.state_activities.get(state_hash, "(unknown activity)")
        by_activity[activity] = by_activity.get(activity, 0) + 1
    if by_activity:
        for activity, count in sorted(by_activity.items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{activity}` — {count} state(s)")
    else:
        lines.append("- No states recorded yet.")
    lines.append("")

    lines.append("## Bugs & crashes flagged")
    if mem.bug_reports:
        for state_hash, entry in mem.bug_reports.items():
            activity = entry.get("activity", "?")
            summary = entry.get("summary", "")
            lines.append(f"- `{state_hash[:8]}` ({activity}): {summary}")
    else:
        lines.append("- None flagged yet.")
    lines.append("")
    lines.append(AUTO_END)
    return "\n".join(lines)


def _extract_manual_section(existing_text: str) -> str:
    """Pull out whatever's between the MANUAL sentinels in an existing report, so a
    regenerate never clobbers hand-written notes. Falls back to the default
    placeholder if the file doesn't exist yet or the sentinels aren't found."""
    if not existing_text:
        return DEFAULT_MANUAL_BODY
    start = existing_text.find(MANUAL_BEGIN)
    end = existing_text.find(MANUAL_END)
    if start == -1 or end == -1 or end < start:
        return DEFAULT_MANUAL_BODY
    body = existing_text[start + len(MANUAL_BEGIN) : end].strip("\n")
    return body if body.strip() else DEFAULT_MANUAL_BODY


def write_report(package: str, mem: AppMemory | None = None) -> str:
    """(Re)generate projects/<package>/REPORT.md. Safe to call every run — the AUTO
    section is fully rewritten from current memory.py data, the MANUAL section is
    preserved verbatim from whatever was there before. Returns the file path."""
    mem = mem or memory_module.load(package)
    meta = _load_meta(package)
    path = _report_path(package)

    existing_text = ""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing_text = f.read()
        except OSError:
            existing_text = ""
    manual_body = _extract_manual_section(existing_text)

    text = (
        f"# QA Test Report — {package}\n\n"
        f"{_render_auto_section(mem, meta)}\n\n"
        f"## Manual notes / known gaps\n"
        f"_(preserved across runs — edit freely; a future agent should read this "
        f"section first)_\n\n"
        f"{MANUAL_BEGIN}\n{manual_body}\n{MANUAL_END}\n"
    )

    os.makedirs(memory_module.project_dir(package), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, path)
    return path


def append_manual_note(package: str, note: str) -> str:
    """Add a timestamped bullet to the MANUAL section without disturbing anything
    else already there. Creates the report first via write_report() if it doesn't
    exist yet. Returns the file path."""
    path = _report_path(package)
    if not os.path.isfile(path):
        write_report(package)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    new_bullet = f"- [{stamp}] {note}"

    start = text.find(MANUAL_BEGIN)
    end = text.find(MANUAL_END)
    if start == -1 or end == -1 or end < start:
        # Sentinels missing somehow — append at the end rather than lose the note.
        text = text.rstrip("\n") + f"\n\n{new_bullet}\n"
    else:
        body = text[start + len(MANUAL_BEGIN) : end].strip("\n")
        new_body = new_bullet if body.strip() == DEFAULT_MANUAL_BODY.strip() or not body.strip() else body + "\n" + new_bullet
        text = text[: start + len(MANUAL_BEGIN)] + "\n" + new_body + "\n" + text[end:]

    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, path)
    return path
