"""Optional per-app persistent memory for the exploration agent.

Stored as memory.json inside that app's project folder
(android-agent/projects/<package>/memory.json) — purely local disk state, no
network calls of its own. This lets a run:

- resume where a previous run left off (previously-tried actions are replayed into
  the fresh in-memory graph so they aren't re-tapped),
- skip re-running the smart bug-review model on a screen whose bug status was
  already recorded in an earlier run,
- give the fast tap-picking model a hint about which candidates historically led
  somewhere new vs. back to an already-known screen.

Enabled via --memory / config.USE_MEMORY; if disabled, callers simply never touch
this module. Any read/write failure logs a warning and falls back to an empty/
unsaved memory rather than interrupting exploration.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("memory")

PROJECTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")


def _project_dir(package: str) -> str:
    safe_name = "".join(c if (c.isalnum() or c in ".-_") else "_" for c in package) or "unknown"
    return os.path.join(PROJECTS_DIR, safe_name)


def _memory_path(package: str) -> str:
    return os.path.join(_project_dir(package), "memory.json")


def project_dir(package: str) -> str:
    """Public accessor for report.py — same per-package folder memory.json lives in."""
    return _project_dir(package)


@dataclass
class AppMemory:
    package: str
    known_state_hashes: set[str] = field(default_factory=set)
    tried_action_ids: dict[str, set[str]] = field(default_factory=dict)       # state_hash -> action ids
    action_outcomes: dict[str, dict[str, bool]] = field(default_factory=dict)  # state_hash -> {action_id: led_to_new_state}
    bug_reports: dict[str, dict] = field(default_factory=dict)                # state_hash -> {bug_suspected, summary, activity}
    state_activities: dict[str, str] = field(default_factory=dict)            # state_hash -> activity name, for report.py

    def is_known(self, state_hash: str) -> bool:
        return state_hash in self.known_state_hashes

    def record_new_state(self, state_hash: str) -> None:
        self.known_state_hashes.add(state_hash)

    def record_activity(self, state_hash: str, activity: str) -> None:
        self.state_activities[state_hash] = activity

    def record_tried(self, state_hash: str, action_id: str, led_to_new_state: bool) -> None:
        self.tried_action_ids.setdefault(state_hash, set()).add(action_id)
        self.action_outcomes.setdefault(state_hash, {})[action_id] = led_to_new_state

    def record_analysis(self, state_hash: str, activity: str, analysis: dict) -> None:
        self.bug_reports[state_hash] = {**analysis, "activity": activity}

    def previously_tried(self, state_hash: str) -> set[str]:
        return set(self.tried_action_ids.get(state_hash, ()))

    def outcome_hints(self, state_hash: str) -> dict[str, bool]:
        """action_id -> True if it previously led to a new state, False if it looped
        back to an already-known one. Only actions with a recorded outcome appear."""
        return dict(self.action_outcomes.get(state_hash, {}))

    def cached_analysis(self, state_hash: str) -> dict | None:
        return self.bug_reports.get(state_hash)


def load(package: str) -> AppMemory:
    mem = AppMemory(package=package)
    path = _memory_path(package)
    if not os.path.isfile(path):
        return mem
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mem.known_state_hashes = set(data.get("known_state_hashes", []))
        mem.tried_action_ids = {k: set(v) for k, v in data.get("tried_action_ids", {}).items()}
        mem.action_outcomes = data.get("action_outcomes", {})
        mem.bug_reports = data.get("bug_reports", {})
        mem.state_activities = data.get("state_activities", {})
        logger.info(
            "Loaded memory for %s: %d known states, %d bug reports",
            package, len(mem.known_state_hashes), len(mem.bug_reports),
        )
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Could not load memory for %s (%s) — starting fresh", package, exc)
        return AppMemory(package=package)
    return mem


def save(mem: AppMemory) -> None:
    try:
        os.makedirs(_project_dir(mem.package), exist_ok=True)
        path = _memory_path(mem.package)
        data = {
            "package": mem.package,
            "known_state_hashes": sorted(mem.known_state_hashes),
            "tried_action_ids": {k: sorted(v) for k, v in mem.tried_action_ids.items()},
            "action_outcomes": mem.action_outcomes,
            "bug_reports": mem.bug_reports,
            "state_activities": mem.state_activities,
        }
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning("Could not save memory for %s: %s", mem.package, exc)
