"""The cheap tier: high-volume vision calls over OpenRouter.

Why this exists at all. The planner (Claude Code on the local Max subscription) is metered
by *rate-limit window*, not by token. The scarce resource is therefore the number of planner
turns, and the two things that consume them fastest are both mechanical:

  1. looking at a screenshot to confirm the obvious ("is the login form up?"),
  2. choosing which of forty elements to tap next while mapping an unfamiliar screen.

Both are delegated here, to a small vision model costing ~$0.10/M input. The planner keeps
the work that actually needs judgement: what to test, whether behaviour is correct, and
whether something is a defect. It still looks at an image itself before filing a finding —
a cheap model's "looks fine" is not evidence, and this module never decides a verdict.

Every call reports its real cost (OpenRouter returns it per request), which the session
totals and shows in the UI, so the spend is visible rather than assumed.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

import config

logger = logging.getLogger("agent.stepper")


class StepperUnavailable(RuntimeError):
    """No OpenRouter key, or the service refused. Callers fall back to the planner."""


class Stepper:
    """Thin OpenRouter client that tracks what it has spent."""

    def __init__(self, model: Optional[str] = None):
        self.model = model or config.AGENT_STEPPER_MODEL
        self.calls = 0
        self.cost_usd = 0.0

    async def _chat(self, messages: list[dict[str, Any]], max_tokens: int = 400) -> str:
        if not config.OPENROUTER_API_KEY:
            raise StepperUnavailable(
                "OPENROUTER_API_KEY is not set — add it to android-agent/.env")
        payload = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        headers = {
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            # OpenRouter attributes traffic by these; harmless if absent, useful in dashboards.
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "QA Tester AI",
        }
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(f"{config.OPENROUTER_BASE_URL}/chat/completions",
                                         json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise StepperUnavailable(f"OpenRouter request failed: {exc}") from exc
        if resp.status_code != 200:
            raise StepperUnavailable(f"OpenRouter {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        self.calls += 1
        try:
            self.cost_usd += float(data.get("usage", {}).get("cost") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise StepperUnavailable(f"Unexpected OpenRouter response: {data}") from exc

    @staticmethod
    def _image_block(path: Path) -> dict[str, Any]:
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}

    @staticmethod
    def _json_from(text: str) -> dict[str, Any]:
        """Small models like to wrap JSON in prose or a fenced block. Recover it."""
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
        return {"raw": text}

    async def check_screen(self, image: Path, expectation: str) -> dict[str, Any]:
        content = [
            self._image_block(image),
            {"type": "text", "text":
                "This is a screenshot of an Android app under test.\n"
                f"Question: {expectation}\n\n"
                "Reply with JSON only: "
                '{"answer": "yes"|"no"|"unclear", "visible_text": "the main text you can '
                'actually read", "note": "one sentence on anything that looks broken — '
                'overlapping text, clipped labels, a blank area, an error dialog"}\n'
                "Describe only what is literally visible. Do not infer or assume."},
        ]
        out = await self._chat([{"role": "user", "content": content}], max_tokens=500)
        return self._json_from(out)

    async def pick_element(self, elements: list[dict[str, Any]], goal: str,
                           image: Optional[Path] = None,
                           avoid: Optional[list[str]] = None) -> dict[str, Any]:
        listing = "\n".join(
            f"- id={e['id']} label={e['label']!r} class={e.get('class', '')}"
            f"{' [APP BAR]' if e.get('in_appbar') else ''}"
            f"{' [input]' if e.get('editable') else ''}"
            for e in elements[:config.LLM_MAX_ACTIONS_TO_MODEL]
        )
        avoid_note = ("\nAlready tried on this screen, avoid these: " + ", ".join(avoid)
                      if avoid else "")
        content: list[dict[str, Any]] = []
        if image is not None:
            content.append(self._image_block(image))
        content.append({"type": "text", "text":
            f"You are helping map an Android app's UI. Goal: {goal}\n"
            f"Touchable elements on the current screen:\n{listing}{avoid_note}\n\n"
            "Pick the single element most likely to advance the goal. Prefer primary actions "
            "and navigation into new content; avoid app-bar back buttons and anything that "
            "leaves the app.\n"
            'Reply with JSON only: {"id": "<exact id>", "why": "one short sentence"}'})
        out = await self._chat([{"role": "user", "content": content}], max_tokens=200)
        result = self._json_from(out)
        valid = {e["id"] for e in elements}
        if result.get("id") not in valid:
            return {"id": None, "why": f"model returned an unusable id ({result!r})"}
        return result


def build_stepper_server(stepper: Stepper, session):
    """MCP tools that let the planner delegate the cheap, high-volume decisions."""

    def _ok(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}]}

    def _err(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    @tool("check_screen",
          "Ask the cheap vision model a yes/no question about the screen right now. Use this "
          "for routine confirmations ('is the login form showing?') instead of taking a "
          "screenshot and reading the image yourself — it is far cheaper and does not consume "
          "your own rate limit. For anything you are about to call a defect, look at the "
          "image yourself as well: this model's opinion is not evidence.",
          {"type": "object",
           "properties": {"expectation": {"type": "string",
                                          "description": "The question, e.g. 'Is an error message shown under the email field?'"}},
           "required": ["expectation"], "additionalProperties": False})
    async def check_screen(args: dict[str, Any]) -> dict[str, Any]:
        try:
            path = await session.capture("check")
            result = await stepper.check_screen(path, str(args["expectation"]))
        except StepperUnavailable as exc:
            return _err(f"Cheap tier unavailable ({exc}). Take a screenshot and read it "
                        f"yourself instead.")
        except Exception as exc:  # noqa: BLE001
            return _err(f"check_screen failed: {exc}")
        return _ok(json.dumps(result, ensure_ascii=False)
                   + f"\n(screenshot at {path} if you want to look yourself)")

    @tool("pick_next_element",
          "Ask the cheap model which element to tap next toward a goal, given the current "
          "screen. Use this while mapping unfamiliar screens so exploration does not burn "
          "your own turns. You stay responsible for the plan and the verdicts.",
          {"type": "object",
           "properties": {"goal": {"type": "string"},
                          "avoid": {"type": "array", "items": {"type": "string"},
                                    "description": "Element ids already tried on this screen"}},
           "required": ["goal"], "additionalProperties": False})
    async def pick_next_element(args: dict[str, Any]) -> dict[str, Any]:
        if not session.last_elements:
            return _err("No screen has been read yet — call read_screen first.")
        try:
            result = await stepper.pick_element(session.last_elements, str(args["goal"]),
                                               avoid=args.get("avoid"))
        except StepperUnavailable as exc:
            return _err(f"Cheap tier unavailable ({exc}). Choose an element yourself from the "
                        f"last read_screen.")
        if not result.get("id"):
            return _err(f"The cheap model could not choose ({result.get('why')}). Pick one "
                        f"yourself from the last read_screen.")
        return _ok(f"Suggested id={result['id']} — {result.get('why', '')}\n"
                   f"Tap it with tap_element if you agree.")

    return create_sdk_mcp_server(name="cheap", version="1.0.0",
                                 tools=[check_screen, pick_next_element])


STEPPER_TOOL_NAMES = ["mcp__cheap__check_screen", "mcp__cheap__pick_next_element"]


def stepper_tool_names() -> list[str]:
    return list(STEPPER_TOOL_NAMES)
