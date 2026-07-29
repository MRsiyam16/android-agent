"""Optional LLM-assisted exploration.

Two best-effort helpers used by run_agent.py when --llm-explore is set:

- pick_action(): a fast model (config.LLM_FAST_MODEL) chooses which untried element
  to tap next, given the screenshot and the candidate list.
- analyze_screen(): a stronger model (config.LLM_SMART_MODEL) periodically reviews a
  screen for signs of a bug (crash dialog, broken layout, blank content).

Both degrade to `None` on any failure (missing API key, network error, rate limit,
malformed response) — callers already have a safe default to fall back to, so this
module must never raise into the exploration loop.
"""
from __future__ import annotations

import logging

import anthropic

import config

logger = logging.getLogger("llm_explorer")

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _action_summary(actions: list[dict], memory_hints: dict[str, bool] | None = None) -> str:
    lines = []
    for a in actions[: config.LLM_MAX_ACTIONS_TO_MODEL]:
        cls = (a.get("class") or "").split(".")[-1]
        line = f'- id={a["id"]} label="{a["label"]}" class={cls} bounds={a["bounds"]}'
        hint = (memory_hints or {}).get(a["id"])
        if hint is True:
            line += "  [previously led to a NEW screen]"
        elif hint is False:
            line += "  [previously led back to an already-known screen]"
        lines.append(line)
    return "\n".join(lines)


PICK_ACTION_TOOL = {
    "name": "pick_action",
    "description": "Choose which candidate UI element to tap next.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action_id": {
                "type": "string",
                "description": "The id of the chosen action, copied exactly from the candidate list.",
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence on why this element is the most useful to explore next.",
            },
        },
        "required": ["action_id", "reasoning"],
        "additionalProperties": False,
    },
}

ANALYZE_SCREEN_TOOL = {
    "name": "report_screen_analysis",
    "description": "Report whether the current screen shows a bug or notable issue.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bug_suspected": {"type": "boolean"},
            "summary": {
                "type": "string",
                "description": "One or two sentences: what's on screen and, if bug_suspected, what looks wrong.",
            },
        },
        "required": ["bug_suspected", "summary"],
        "additionalProperties": False,
    },
}


def pick_action(
    actions: list[dict],
    screenshot_b64: str,
    package: str,
    activity: str,
    memory_hints: dict[str, bool] | None = None,
) -> str | None:
    """Ask the fast model to choose an untried action. Returns the chosen action id,
    or None on any failure — caller falls back to its own default heuristic.

    `memory_hints` (optional): action_id -> True/False from a prior run of this app,
    True meaning that action previously led to a brand-new screen, False meaning it
    looped back to an already-known one. Passed through as extra context only — no
    extra API call.
    """
    if not actions or not screenshot_b64:
        return None

    candidates = actions[: config.LLM_MAX_ACTIONS_TO_MODEL]
    prompt = (
        f"You are exploring the Android app '{package}' (screen: {activity}) to map its UI.\n"
        f"Below are the untried, clickable elements on the current screen. Pick the one most "
        f"likely to reveal new, meaningful app behavior (prefer primary actions, navigation, "
        f"and content over decorative or repetitive elements). Some elements carry a hint from "
        f"a previous exploration run of this app — prefer one marked as leading to a new screen "
        f"over one marked as looping back, all else equal.\n\n{_action_summary(candidates, memory_hints)}"
    )

    try:
        response = _get_client().messages.create(
            model=config.LLM_FAST_MODEL,
            max_tokens=300,
            tools=[PICK_ACTION_TOOL],
            tool_choice={"type": "tool", "name": "pick_action"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": screenshot_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except Exception as exc:
        logger.warning("pick_action LLM call failed: %s", exc)
        return None

    valid_ids = {a["id"] for a in candidates}
    for block in response.content:
        if block.type == "tool_use" and block.name == "pick_action":
            action_id = block.input.get("action_id")
            reasoning = block.input.get("reasoning", "")
            if action_id in valid_ids:
                logger.info("LLM picked action %s: %s", action_id, reasoning)
                return action_id
            logger.warning("LLM returned unknown action_id %r; falling back", action_id)
            return None
    return None


def analyze_screen(screenshot_b64: str, package: str, activity: str) -> dict | None:
    """Ask the smart model to review the current screen for bugs. Returns
    {"bug_suspected": bool, "summary": str}, or None on any failure."""
    if not screenshot_b64:
        return None

    prompt = (
        f"You are QA-reviewing the Android app '{package}' during automated exploration. "
        f"Current screen: {activity}. Look at the screenshot and flag anything that looks "
        f"broken: error messages, crash dialogs, layout overlap/clipping, blank/frozen "
        f"content, or obviously wrong text. If the screen looks normal, say so."
    )

    try:
        response = _get_client().messages.create(
            model=config.LLM_SMART_MODEL,
            max_tokens=500,
            tools=[ANALYZE_SCREEN_TOOL],
            tool_choice={"type": "tool", "name": "report_screen_analysis"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": screenshot_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except Exception as exc:
        logger.warning("analyze_screen LLM call failed: %s", exc)
        return None

    for block in response.content:
        if block.type == "tool_use" and block.name == "report_screen_analysis":
            return {
                "bug_suspected": bool(block.input.get("bug_suspected")),
                "summary": block.input.get("summary", ""),
            }
    return None
