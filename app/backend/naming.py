"""Screen naming (breadcrumb paths) + graph-for-agents bookkeeping.

Built once per state_hash, the first time it's seen, so a screen's name/number never
changes across a session even as more telemetry comes in for it. This is also the single
source of truth consumed by both the dashboard (node header labels) and the text-only
/map endpoint, so the two views never disagree about what a screen is called.

The dicts below are cleared in place, never rebound — `server.py` re-exports them by
reference and the tests assert against those re-exports.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_GENERIC_LABELS = {
    "relativelayout", "framelayout", "linearlayout", "view", "imageview",
    "textview", "button", "edittext", "calculator input field", "result preview",
    "unlabeled element",
}
_NUMERIC_ISH_RE = re.compile(r"^[\d+\-×÷=%.()]+$")
_WORDY_RE = re.compile(r"^[a-zA-Z][a-zA-Z\s]*$")


def is_section_trigger(label: Optional[str]) -> bool:
    """Heuristic: is this action label meaningful enough to name a new section after
    (e.g. "Settings", "Search") rather than generic chrome (e.g. "Button", "3+4=")?"""
    if not label:
        return False
    norm = label.strip().lower()
    if norm in _GENERIC_LABELS:
        return False
    if _NUMERIC_ISH_RE.match(norm):
        return False
    if len(norm) <= 1:
        return False
    return bool(_WORDY_RE.match(label.strip()))


screen_paths: dict[str, list[str]] = {}       # state_hash -> breadcrumb segments, e.g. ["Settings", "Notifications"]
screen_names: dict[str, str] = {}             # state_hash -> display name, e.g. "Settings > Notifications: 2"
_path_leaf_counts: dict[tuple, int] = {}      # breadcrumb tuple -> how many states share that exact path
node_order: list[str] = []                    # state_hash, in first-discovery order
node_index: dict[str, int] = {}               # state_hash -> 1-based sequential screen number
edge_index: dict[str, dict[str, Any]] = {}    # "from->to->label" -> {from_hash, to_hash, label}


def register_screen(
    state_hash: str,
    parent_hash: Optional[str],
    action_label: Optional[str],
    explicit_name: Optional[str] = None,
) -> None:
    """Assign a stable breadcrumb name + sequential number the first time a state is seen.

    `explicit_name` is used verbatim when the caller already knows what the node means —
    scripted journeys (see `journey.py`) name each step themselves ("3. Tap 5 -> 7+5"),
    which is far more informative than a breadcrumb derived from keypad labels. Autonomous
    exploration passes nothing here and keeps the derived-breadcrumb behaviour."""
    if state_hash in screen_names:
        return

    if explicit_name:
        screen_paths[state_hash] = [explicit_name]
        screen_names[state_hash] = explicit_name
        node_order.append(state_hash)
        node_index[state_hash] = len(node_order)
        return

    parent_path = screen_paths.get(parent_hash, []) if parent_hash else []
    if is_section_trigger(action_label):
        path = [*parent_path, action_label.strip()]
    elif parent_path:
        path = parent_path
    else:
        path = ["Home"]

    leaf_key = tuple(path)
    count = _path_leaf_counts.get(leaf_key, 0) + 1
    _path_leaf_counts[leaf_key] = count

    name = " > ".join(path)
    if count > 1:
        name = f"{name}: {count}"

    screen_paths[state_hash] = path
    screen_names[state_hash] = name
    node_order.append(state_hash)
    node_index[state_hash] = len(node_order)


def reset_screen_naming() -> None:
    screen_paths.clear()
    screen_names.clear()
    _path_leaf_counts.clear()
    node_order.clear()
    node_index.clear()
    edge_index.clear()
