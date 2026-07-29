"""The agent's hands: in-process MCP tools that drive a real Android device.

These are `claude-agent-sdk` SDK-MCP tools, which means they run inside this server process
— no subprocess, no socket, no second copy of the device session. Each chat session builds
its own server bound to its own `DeviceSession` (see `build_device_server`).

Every tool is blocking underneath (uiautomator2 is synchronous), so each one hands off to a
worker thread. Without that, one `dump_hierarchy()` would stall the event loop and with it
the WebSocket feeding the browser — the UI would look frozen exactly when the agent is busy.

The guardrails here are not defensive padding; each one is a false defect this harness has
already produced (see SYSTEM_MEMORY.md):

* `read_screen` ranks packages by node count and says plainly when the app under test is not
  the one on screen. A Messenger chat head or a permission dialog otherwise silently becomes
  "the app is broken".
* `tap_element` prefers an id from the last `read_screen`, and label matching excludes the
  app-bar band by default — `desc='Login'` matching both the back header and the submit
  button is what made a correct app look like it rejected valid input.
* `wait_until_gone` exists so a verdict is never read while a request is in flight.
* `record_finding` refuses to file a defect without a screenshot. Every false defect this
  harness produced was a dump misread that a screenshot would have caught.
* `record_finding` also refuses a verdict read off a stale screen — one the agent has acted
  on since last reading, or one still showing loading text (see `finding_block_reason`). The
  prompt has always carried that rule as prose, and the harness violated it twice anyway;
  prose is advice to a model sixty turns deep, a refusing tool is not.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from claude_agent_sdk import create_sdk_mcp_server, tool

import config
from adb_device import AdbDevice, DeviceError, detect_toolkit
from agent import store

from agent.guards import finding_block_reason, in_flight_text
from agent.screen import package_ranking, screen_elements, screen_texts

logger = logging.getLogger("agent.device_tools")

# Re-exported: these moved to agent/screen.py and agent/guards.py, but the tests and
# runtime.py import them from here and there is no reason to make them care.
__all__ = [
    "DeviceCancelled", "DeviceSession", "build_device_server", "DEVICE_TOOL_NAMES",
    "finding_block_reason", "in_flight_text",
    "package_ranking", "screen_elements", "screen_texts",
]


class DeviceCancelled(RuntimeError):
    """Raised inside a device tool when the user has pressed Stop.

    Distinct from `DeviceError` because it is not a failure: nothing is wrong with the phone,
    the run was called off. Tools translate it into a plain "stopped" result rather than an
    error the agent might try to work around by retrying.
    """


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


class DeviceSession:
    """One agent chat session's view of the device, plus its evidence trail."""

    def __init__(self, package: str, slug: str, serial: Optional[str] = None,
                 emit: Optional[Callable[[dict[str, Any]], Any]] = None):
        self.package = package
        self.slug = slug
        self.serial = serial
        self.emit = emit or (lambda _e: None)

        self._device: Optional[AdbDevice] = None
        self._journey = None
        self._section: Optional[str] = None
        self.last_elements: list[dict[str, Any]] = []
        self.last_xml: str = ""
        self.last_texts: list[str] = []
        self.shot_count = 0
        self.tap_count = 0
        # How many actions have been taken since the screen was last read. The prompt already
        # says "never act twice without reading in between" and "never judge a submit while a
        # request is in flight" — but a rule the model is merely *told* is a rule it can drop
        # sixty turns into a run, and both of this harness's worst false defects came from
        # exactly that. `record_finding` reads these two fields and refuses instead.
        self.actions_since_read = 0

        # Set when the agent needs the user: the run parks on this future until the browser
        # answers, so "blocked" is a real pause rather than a guess written into the report.
        self.pending_question: Optional[dict[str, Any]] = None
        self._answer: Optional[asyncio.Future] = None

        # Raised by Stop. `interrupt()` on the CLI only ends the turn once the tool in flight
        # returns, and the tools worth interrupting are the slow ones — a `wait_for_ui` can
        # hold the turn for two minutes after Stop was pressed, which is indistinguishable
        # from the button not working. Every loop that can run long polls this and bails.
        #
        # threading.Event, not a bool: the polling happens on worker threads via
        # `asyncio.to_thread`, and Event is the flag that is safe to set from the event loop
        # and read from those threads without further ceremony.
        self._cancelled = threading.Event()

    # -- stop --------------------------------------------------------------------
    def cancel(self) -> None:
        """Ask everything in flight to give up at its next checkpoint."""
        self._cancelled.set()
        # A run parked on a question would otherwise sit there forever after a Stop, since
        # nothing is going to answer it now.
        if self._answer is not None and not self._answer.done():
            self._answer.cancel()

    def resume(self) -> None:
        """Clear the flag so the next turn can run. Called when a new message arrives."""
        self._cancelled.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def cancel_event(self) -> threading.Event:
        """For blocking calls that can poll it themselves on a worker thread."""
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise DeviceCancelled("Stopped by the user.")

    def wait_cancellable(self, seconds: float) -> bool:
        """Sleep, but wake immediately on Stop. Returns True if it was cancelled.

        Every `time.sleep` in a settle loop is time the Stop button cannot take back, so the
        waits go through here instead.
        """
        return self._cancelled.wait(seconds)

    # -- device ------------------------------------------------------------------
    def _connect(self) -> AdbDevice:
        if self._device is None:
            self._device = AdbDevice(self.serial)
            self.serial = self._device.serial
        return self._device

    async def device(self) -> AdbDevice:
        """Connect (once), with a timeout.

        `u2.connect()` starts atx-agent on the phone and can block for a long time if the
        device is asleep or the USB link is flaky. Without a bound here, that block would
        freeze the chat with no error at all — the one failure mode that looks identical to
        the agent thinking.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._connect),
                timeout=config.AGENT_TOOL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise DeviceError(
                f"Connecting to the device took longer than "
                f"{config.AGENT_TOOL_TIMEOUT_SECONDS:.0f}s. Check the cable and that the "
                f"screen is on, then try again.") from exc

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a blocking device call off the event loop, with a hard timeout."""
        return await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs),
            timeout=config.AGENT_TOOL_TIMEOUT_SECONDS,
        )

    def journey(self):
        if self._journey is None:
            from journey import Journey
            self._journey = Journey(self._connect(), self.package,
                                    session_id=f"agent-{self.slug}")
        return self._journey

    # -- user questions ----------------------------------------------------------
    async def ask(self, question: str, kind: str = "question",
                  payload: Optional[dict[str, Any]] = None) -> str:
        loop = asyncio.get_running_loop()
        self._answer = loop.create_future()
        self.pending_question = {"kind": kind, "question": question, "payload": payload or {}}
        await self._emit({"type": "agent_blocked", **self.pending_question})
        try:
            return await self._answer
        finally:
            self.pending_question = None
            self._answer = None

    def answer(self, text: str) -> bool:
        if self._answer and not self._answer.done():
            self._answer.set_result(text)
            return True
        return False

    async def _emit(self, event: dict[str, Any]) -> None:
        try:
            result = self.emit({"slug": self.slug, "package": self.package, **event})
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a tool
            logger.warning("emit failed: %s", exc)

    async def capture(self, note: str = "") -> Path:
        """Save a screenshot into the sub-project's evidence folder and return its path."""
        d = await self.device()
        raw = await self.run(d.d.screenshot, format="pillow")
        self.shot_count += 1
        safe = re.sub(r"[^a-z0-9]+", "-", note.lower()).strip("-")[:40] or "shot"
        path = store.shots_dir(self.package, self.slug) / f"{self.shot_count:03d}-{safe}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            lambda: raw.convert("RGB").save(path, format="JPEG",
                                            quality=config.SCREENSHOT_QUALITY, optimize=True))
        # Recorded in the transcript as well as broadcast, so reopening the module still shows
        # the evidence inline instead of a conversation referring to images that aren't there.
        await asyncio.to_thread(store.append_chat, self.package, self.slug,
                                {"role": "shot", "path": str(path), "note": note})
        await self._emit({"type": "agent_screenshot", "path": str(path), "note": note})
        return path


# Text that means a request is still in flight. Judging a submit while one of these is on

def _render_screen(session: DeviceSession, xml: str, w: int, h: int,
                   texts: list[str], elements: list[dict[str, Any]]) -> str:
    ranking = package_ranking(xml)
    top_pkg = ranking[0][0] if ranking else ""
    lines: list[str] = []

    if not ranking:
        lines.append("WARNING: the dump has no readable nodes. The UI has probably not "
                     "rendered yet — wait, then read again. Do not conclude anything.")
    elif top_pkg != session.package:
        lines.append(
            f"WARNING: the screen is owned by `{top_pkg}` ({ranking[0][1]} nodes), not the app "
            f"under test (`{session.package}`). A dump shows only the topmost window, so the "
            f"app's own screen is hidden behind this one — most likely a system permission "
            f"dialog, another app's floating overlay, or the launcher. Deal with this first; "
            f"anything you conclude about the app right now would be about the wrong window.")
    if len(ranking) > 1:
        lines.append("packages on screen: " + ", ".join(f"{p} ({n})" for p, n in ranking[:4]))

    lines.append(f"screen {w}x{h} · toolkit={detect_toolkit(xml)} · "
                 f"{len(elements)} touchable elements")
    if not elements:
        lines.append("NOTE: zero touchable controls. That is almost never a real app state — "
                     "it usually means an overlay is intercepting input or the screen is "
                     "still loading.")

    lines.append("\n--- visible text ---")
    lines.append("\n".join(f"  {t}" for t in texts[:80]) or "  (no text — screen not ready?)")
    if len(texts) > 80:
        lines.append(f"  … and {len(texts) - 80} more")

    lines.append("\n--- touchable elements (tap by id) ---")
    for e in elements[:60]:
        flags = []
        if e["editable"]:
            flags.append("input")
        if e["checked"] in ("true", "false"):
            flags.append(f"checked={e['checked']}")
        if e["in_appbar"]:
            flags.append("APP BAR")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        rid = f" #{e['resource_id'].split('/')[-1]}" if e["resource_id"] else ""
        lines.append(f"  id={e['id']:<12} {e['label'][:52]!r}{rid} ({e['class']}){suffix}")
    if len(elements) > 60:
        lines.append(f"  … and {len(elements) - 60} more")
    return "\n".join(lines)


def build_device_server(session: DeviceSession):
    """Build an MCP server whose tools are bound to this one session."""

    # ---------------------------------------------------------------- observing
    @tool("read_screen",
          "Read the current screen: which package owns it, all visible text, and every "
          "touchable element with an id you can tap. Call this after every action that could "
          "change the screen. Returns a warning if another app or a system dialog is covering "
          "the app under test.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def read_screen(_args: dict[str, Any]) -> dict[str, Any]:
        d = await session.device()
        try:
            xml = await session.run(d.dump_xml)
            w, h = await session.run(lambda: d.window_size)
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Could not read the screen: {exc}")
        texts = screen_texts(xml)
        elements = screen_elements(xml, w, h)
        session.last_xml, session.last_elements, session.last_texts = xml, elements, texts
        session.actions_since_read = 0
        return _ok(_render_screen(session, xml, w, h, texts, elements))

    @tool("screenshot",
          "Capture a screenshot and save it as evidence. Returns a file path — use the Read "
          "tool on that path to actually look at the image. Required before filing any finding.",
          {"type": "object",
           "properties": {"note": {"type": "string",
                                   "description": "Optional short label, e.g. 'login-empty-submit'"}},
           "additionalProperties": False})
    async def screenshot(args: dict[str, Any]) -> dict[str, Any]:
        try:
            path = await session.capture(args.get("note") or "shot")
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Screenshot failed: {exc}")
        return _ok(f"Saved to {path}\nRead that path to view the image.")

    @tool("wait_for_text",
          "Poll the screen until the given text appears (substring, case-insensitive). Use "
          "this instead of a fixed sleep: a splash screen publishes nodes with no text, so "
          "text is the only honest readiness signal.",
          {"type": "object",
           "properties": {"text": {"type": "string"},
                          "timeout": {"type": "number", "description": "Seconds, default 20"}},
           "required": ["text"], "additionalProperties": False})
    async def wait_for_text(args: dict[str, Any]) -> dict[str, Any]:
        needle = str(args["text"]).lower()
        deadline = time.monotonic() + float(args.get("timeout") or 20)
        d = await session.device()
        last: list[str] = []
        while time.monotonic() < deadline:
            # Checked every pass, not just at entry: this loop can hold the turn for the full
            # timeout, and that whole window is time the Stop button has to be able to reclaim.
            if session.cancelled:
                return _ok("Stopped before the text appeared.")
            try:
                xml = await session.run(d.dump_xml)
            except (DeviceError, asyncio.TimeoutError):
                xml = ""
            last = screen_texts(xml)
            if any(needle in t.lower() for t in last):
                return _ok(f"Appeared: {args['text']!r}")
            await asyncio.sleep(0.6)
        preview = ", ".join(last[:20]) or "(no text on screen)"
        return _ok(f"TIMEOUT: {args['text']!r} did not appear. Currently on screen: {preview}\n"
                   f"This is not yet a defect — screenshot first and confirm what is actually "
                   f"displayed before concluding anything.")

    @tool("wait_until_gone",
          "Poll until the given text disappears — a loading spinner, 'Please wait', "
          "'Creating your account…'. Never judge the result of a submit while a request is "
          "still in flight; a progress overlay hides the form underneath and makes a correct "
          "rejection look like it was accepted.",
          {"type": "object",
           "properties": {"text": {"type": "string"},
                          "timeout": {"type": "number", "description": "Seconds, default 30"}},
           "required": ["text"], "additionalProperties": False})
    async def wait_until_gone(args: dict[str, Any]) -> dict[str, Any]:
        needle = str(args["text"]).lower()
        deadline = time.monotonic() + float(args.get("timeout") or 30)
        d = await session.device()
        while time.monotonic() < deadline:
            if session.cancelled:
                return _ok("Stopped while waiting for the text to clear.")
            try:
                xml = await session.run(d.dump_xml)
            except (DeviceError, asyncio.TimeoutError):
                xml = ""
            if not any(needle in t.lower() for t in screen_texts(xml)):
                return _ok(f"Gone: {args['text']!r} — safe to read the result now.")
            await asyncio.sleep(0.6)
        return _ok(f"TIMEOUT: {args['text']!r} is still on screen after "
                   f"{args.get('timeout') or 30}s. The request may be hung — that itself may "
                   f"be the finding. Screenshot it.")

    @tool("check_crash",
          "Check the device log for a crash or ANR involving the app under test since the "
          "last call. Call after any action that might destabilise it.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def check_crash(_args: dict[str, Any]) -> dict[str, Any]:
        d = await session.device()
        excerpt = await session.run(d.read_new_crashes, session.package)
        if excerpt:
            return _ok(f"CRASH / ANR detected:\n{excerpt}")
        return _ok("No crash or ANR in the log since the last check.")

    # ---------------------------------------------------------------- acting
    @tool("launch",
          "Force-stop and relaunch the app under test, then wait for its UI to render. "
          "Returns what is on screen once it settles.",
          {"type": "object",
           "properties": {"package": {"type": "string",
                                      "description": "Defaults to the app under test"}},
           "additionalProperties": False})
    async def launch(args: dict[str, Any]) -> dict[str, Any]:
        pkg = args.get("package") or session.package
        d = await session.device()
        try:
            if await session.run(d.is_locked):
                return _err("The device is locked and I cannot unlock it. Ask the user to "
                            "unlock the phone, then try again.")
            if not await session.run(d.is_screen_on):
                await session.run(d.wake_screen)
            # Check this before launching, not after: `monkey -p` on a missing package fails
            # quietly and leaves the launcher on screen, which looks exactly like an app that
            # refused to start — and invites a defect report about a package that isn't there.
            if not await session.run(d.is_installed, pkg):
                similar = await session.run(d.similar_packages, pkg)
                hint = ("  Installed packages with a similar name: " + ", ".join(similar)
                        if similar else "  No installed package has a similar name.")
                return _err(f"`{pkg}` is not installed on this device, so there is nothing to "
                            f"launch. This is not a defect in the app.\n{hint}\n"
                            f"Tell the user which package you actually need, or launch one of "
                            f"the above if it is clearly the same app.")
            await session.run(d.start_app, pkg)
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Launch failed: {exc}")
        # The one call in the whole toolkit that can hold the turn for a couple of minutes, so
        # it gets the cancel flag directly rather than being raced from outside.
        xml, waited = await asyncio.to_thread(d.wait_for_ui, pkg, None, 1.0, session.cancel_event)
        if session.cancelled:
            return _ok(f"Stopped while waiting for {pkg} to become readable.")
        w, h = await session.run(lambda: d.window_size)
        elements = screen_elements(xml, w, h)
        session.last_xml, session.last_elements = xml, elements
        session.last_texts = screen_texts(xml)
        session.actions_since_read = 0
        header = f"Launched {pkg}; UI became readable after {waited}s.\n"
        if pkg != session.package:
            # Otherwise the mislabelling is silent: findings and flow-graph steps are keyed to
            # the project, so they would be filed against an app that was never opened.
            header += (f"NOTE: this project is `{session.package}`, so anything you record now "
                       f"is filed under that package even though you are testing `{pkg}`. "
                       f"Mention the mismatch in your reply so the user can move the module to "
                       f"the right project.\n")
        return _ok(header + _render_screen(session, xml, w, h, screen_texts(xml), elements))

    @tool("tap_element",
          "Tap an element by the id from the last read_screen. Ids are the reliable way to "
          "tap: matching on a label instead can hit the wrong widget, because the app bar "
          "often carries the same accessibility label as the screen's primary button.",
          {"type": "object",
           "properties": {"id": {"type": "string", "description": "id from read_screen, e.g. 540_1180"},
                          "why": {"type": "string", "description": "What you expect this to do"}},
           "required": ["id"], "additionalProperties": False})
    async def tap_element(args: dict[str, Any]) -> dict[str, Any]:
        target = str(args["id"])
        match = next((e for e in session.last_elements if e["id"] == target), None)
        if match is None:
            known = ", ".join(e["id"] for e in session.last_elements[:15])
            return _err(f"No element with id={target} on the last read screen. Call read_screen "
                        f"again — the screen may have changed. Known ids: {known}")
        d = await session.device()
        try:
            await session.run(d.click, match["x"], match["y"])
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Tap failed: {exc}")
        session.tap_count += 1
        session.actions_since_read += 1
        await session._emit({"type": "agent_tap", "x": match["x"], "y": match["y"],
                             "label": match["label"]})
        return _ok(f"Tapped {match['label']!r} at ({match['x']},{match['y']}). "
                   f"Call read_screen to see the result.")

    @tool("tap_text",
          "Tap the element whose label matches this text. Excludes the app-bar band by "
          "default so a back header sharing the button's label cannot be hit by mistake. "
          "Fails when the text is ambiguous, rather than guessing.",
          {"type": "object",
           "properties": {"text": {"type": "string"},
                          "include_appbar": {"type": "boolean",
                                             "description": "Set true only to tap the header itself"}},
           "required": ["text"], "additionalProperties": False})
    async def tap_text(args: dict[str, Any]) -> dict[str, Any]:
        needle = str(args["text"]).lower()
        include_appbar = bool(args.get("include_appbar"))
        pool = [e for e in session.last_elements if include_appbar or not e["in_appbar"]]
        exact = [e for e in pool if e["label"].lower() == needle]
        partial = [e for e in pool if needle in e["label"].lower()]
        candidates = exact or partial
        if not candidates:
            hidden = [e for e in session.last_elements if needle in e["label"].lower()]
            if hidden:
                return _err(f"{args['text']!r} only matches an element in the app bar. If you "
                            f"really mean the header, pass include_appbar=true — but if you "
                            f"meant the screen's own button, it is not on this screen.")
            return _err(f"Nothing matches {args['text']!r}. Call read_screen and tap by id.")
        if len(candidates) > 1:
            listing = "; ".join(f"id={e['id']} {e['label']!r}" for e in candidates[:6])
            return _err(f"{args['text']!r} is ambiguous — {len(candidates)} matches: {listing}. "
                        f"Tap by id instead so the choice is explicit.")
        match = candidates[0]
        d = await session.device()
        try:
            await session.run(d.click, match["x"], match["y"])
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Tap failed: {exc}")
        session.tap_count += 1
        session.actions_since_read += 1
        await session._emit({"type": "agent_tap", "x": match["x"], "y": match["y"],
                             "label": match["label"]})
        return _ok(f"Tapped {match['label']!r} at ({match['x']},{match['y']}). "
                   f"Call read_screen to see the result.")

    @tool("tap_xy", "Tap raw coordinates. Prefer tap_element; use this only when an element "
                    "has no dump entry (a canvas, a custom-drawn control).",
          {"type": "object",
           "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
           "required": ["x", "y"], "additionalProperties": False})
    async def tap_xy(args: dict[str, Any]) -> dict[str, Any]:
        d = await session.device()
        try:
            await session.run(d.click, int(args["x"]), int(args["y"]))
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Tap failed: {exc}")
        session.tap_count += 1
        session.actions_since_read += 1
        await session._emit({"type": "agent_tap", "x": int(args["x"]), "y": int(args["y"]),
                             "label": "raw tap"})
        return _ok(f"Tapped ({args['x']},{args['y']}).")

    @tool("type_text",
          "Type into the focused field. Tap the field first. Forms usually validate as you "
          "type, so an error can already be on screen before you submit.",
          {"type": "object",
           "properties": {"text": {"type": "string"},
                          "clear": {"type": "boolean", "description": "Clear the field first"}},
           "required": ["text"], "additionalProperties": False})
    async def type_text(args: dict[str, Any]) -> dict[str, Any]:
        d = await session.device()
        try:
            await session.run(d.send_keys, str(args["text"]), bool(args.get("clear")))
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Typing failed: {exc}")
        session.actions_since_read += 1
        return _ok(f"Typed {args['text']!r}.")

    @tool("use_credential",
          "Type a stored test credential into the focused field without revealing its value. "
          "Use this instead of asking the user to paste a password into the chat. Call "
          "list_credentials to see which names exist.",
          {"type": "object",
           "properties": {"name": {"type": "string", "description": "e.g. test_email, test_password"}},
           "required": ["name"], "additionalProperties": False})
    async def use_credential(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args["name"])
        secrets = store.get_secrets(session.package)
        if name not in secrets:
            available = ", ".join(sorted(secrets)) or "(none stored)"
            answer = await session.ask(
                f"I need a credential named `{name}` to continue, and none is stored for "
                f"`{session.package}`. What should I use? It will be saved to "
                f"projects/{session.package}/secrets.json (gitignored) and never written into "
                f"the transcript or the report.\n\nStored so far: {available}",
                kind="credential", payload={"name": name})
            if not answer.strip():
                return _err(f"No credential provided for {name!r}; cannot continue this case.")
            store.set_secret(session.package, name, answer.strip())
            secrets = store.get_secrets(session.package)
        d = await session.device()
        try:
            await session.run(d.send_keys, secrets[name], True)
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Typing the credential failed: {exc}")
        session.actions_since_read += 1
        return _ok(f"Typed the stored value for {name!r} (not shown here).")

    @tool("list_credentials", "Names of the test credentials stored for this app. Values are "
                              "never returned — use use_credential to enter one.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def list_credentials(_args: dict[str, Any]) -> dict[str, Any]:
        keys = store.secret_keys(session.package)
        return _ok("Stored credentials: " + (", ".join(keys) if keys else "(none yet)"))

    @tool("press", "Press a hardware/navigation key: back, home, enter, recent, delete.",
          {"type": "object",
           "properties": {"key": {"type": "string", "enum": ["back", "home", "enter", "recent", "delete"]}},
           "required": ["key"], "additionalProperties": False})
    async def press(args: dict[str, Any]) -> dict[str, Any]:
        d = await session.device()
        try:
            await session.run(d.press, str(args["key"]))
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"press failed: {exc}")
        session.actions_since_read += 1
        return _ok(f"Pressed {args['key']}.")

    @tool("scroll", "Scroll the screen. Direction is the direction the *content* moves, so "
                    "'down' reveals what is further down the page. Swipes within the middle "
                    "band so the gesture is not stolen by the status bar or the nav bar. Note "
                    "that on a launcher home screen these map to the launcher's own gestures — "
                    "'up' pulls the notification shade down, 'down' opens the app drawer.",
          {"type": "object",
           "properties": {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                          "scale": {"type": "number", "description": "Fraction of screen height, default 0.6"}},
           "required": ["direction"], "additionalProperties": False})
    async def scroll(args: dict[str, Any]) -> dict[str, Any]:
        d = await session.device()
        try:
            await session.run(d.scroll, str(args["direction"]), float(args.get("scale") or 0.6))
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"scroll failed: {exc}")
        session.actions_since_read += 1
        return _ok(f"Scrolled {args['direction']}. Call read_screen to see what is visible now.")

    @tool("reset_app_data",
          "Wipe the app's data to drop a persisted session (pm clear). Needed between "
          "sign-up cases, since a successful registration leaves you logged in. Expect a "
          "system permission prompt on the next launch, which belongs to another package.",
          {"type": "object",
           "properties": {"confirm": {"type": "boolean",
                                      "description": "Must be true — this destroys app state"}},
           "required": ["confirm"], "additionalProperties": False})
    async def reset_app_data(args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("confirm"):
            return _err("reset_app_data needs confirm=true; it destroys the app's local state.")
        d = await session.device()
        cleared = await session.run(d.clear_app_data, session.package)
        if not cleared:
            return _err("pm clear did not succeed in any form on this ROM — the persisted "
                        "session is still there. Sign out through the UI instead.")
        return _ok("App data cleared. The next launch is a cold start: it may sit on a splash "
                   "screen, and a permission prompt from com.android.permissioncontroller may "
                   "appear over it.")

    # ---------------------------------------------------------------- recording
    @tool("journey_step",
          "Record the current screen as the next step of the test case on the dashboard's "
          "Flow Graph. Call once per meaningful step so the case renders as a readable chain. "
          "Returns the node id — record the screen a verdict is about *before* filing it, and "
          "pass that id to record_finding as `step`, so the board can outline the screen.",
          {"type": "object",
           "properties": {
               "label": {"type": "string", "description": "What this step did, e.g. \"Submit empty form\""},
               "case": {"type": "string", "description": "Test case name; starts a new chain when it changes"},
           },
           "required": ["label"], "additionalProperties": False})
    async def journey_step(args: dict[str, Any]) -> dict[str, Any]:
        await session.device()
        j = session.journey()
        case = args.get("case")
        section = f"{session.slug} / {case}" if case else session.slug
        if section != session._section:
            await asyncio.to_thread(j.start_section, section)
            session._section = section
            j._prev_node = None  # a new case starts its own chain, not a branch off the last one
        try:
            node = await asyncio.to_thread(j.step, str(args["label"]),
                                           None, session.last_xml or None)
        except Exception as exc:  # noqa: BLE001
            return _err(f"Could not post the step: {exc}")
        await session._emit({"type": "agent_journey_step", "label": args["label"],
                             "section": section, "node": node})
        # The id is returned because record_finding takes it: it is the only way to say
        # *which* screen a verdict is about, and the agent cannot pass back a value it was
        # never told.
        return _ok(f"Recorded step {j.step_count} of {section!r} on the flow graph "
                   f"as node {node}. Pass step=\"{node}\" to record_finding if the outcome "
                   f"you file next is about this screen.")

    @tool("record_finding",
          "Record the outcome of a test case. Every case ends in one of these, including the "
          "ones that pass — a module with no passes recorded is indistinguishable from a "
          "module that was never tested. Requires a screenshot path as evidence: every false "
          "defect this harness has produced was a dump misread, and a screenshot is what "
          "catches that. State expected vs actual concretely.\n"
          "  pass       — you checked it and it behaved correctly\n"
          "  warning    — it works, but something about it is questionable or fragile\n"
          "  bug        — it is broken; expected and actual genuinely disagree\n"
          "  suggestion — it is not wrong, but the app would be better if it did X",
          {"type": "object",
           "properties": {
               "title": {"type": "string"},
               "kind": {"type": "string", "enum": ["pass", "warning", "bug", "suggestion"],
                        "description": "What this outcome is. Defaults to bug for backwards "
                                       "compatibility, but state it explicitly."},
               "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"],
                            "description": "How bad. Ignored for a pass; use low for a "
                                           "suggestion unless it matters more than that."},
               "expected": {"type": "string"},
               "actual": {"type": "string"},
               "steps": {"type": "array", "items": {"type": "string"},
                         "description": "Reproduction steps, one per item"},
               "evidence": {"type": "string", "description": "Path returned by the screenshot tool"},
               "step": {"type": "string",
                        "description": "Node id returned by journey_step for the screen this "
                                       "verdict is about. Pass it and the board outlines that "
                                       "screen — red for a bug, amber for a warning or a "
                                       "suggestion. Omit it if this outcome is not about one "
                                       "particular screen you recorded; do not guess an id."},
           },
           "required": ["title", "expected", "actual", "evidence"],
           "additionalProperties": False})
    async def record_finding(args: dict[str, Any]) -> dict[str, Any]:
        evidence = str(args.get("evidence") or "")
        if not evidence or not Path(evidence).is_file():
            return _err("The evidence screenshot does not exist. Call screenshot, Read the "
                        "image to confirm what it actually shows, then file the finding with "
                        "that path.")

        # The timing rule, enforced rather than requested. The prompt has always said not to
        # judge a submit mid-flight and not to act twice without reading — and the harness
        # still shipped "unknown credentials were accepted" and "a second account was
        # created", both because a verdict was read off a screen that had moved on. A rule the
        # model is told can be forgotten sixty turns into a run; a tool that refuses cannot.
        #
        # This guards a `pass` exactly as hard as a `bug`. Both of the incidents above were
        # *premature verdicts*, and one of them flipped to PASS once the overlay cleared —
        # a pass read off a mid-flight screen is as wrong as a defect read off one, and
        # "the app is fine" is the more expensive of the two to be wrong about.
        blocked = finding_block_reason(session)
        if blocked is not None:
            return _err(blocked)

        kind = str(args.get("kind") or "bug")
        # Which flow-graph node this verdict is about, so the board can outline the screen a
        # defect was found on — how a reader gets from "1 bug" to *where* without opening
        # anything.
        #
        # Taken from the argument and nowhere else. The obvious shortcut is the journey's
        # last posted node, and it is wrong: read a transcript and the agent files the
        # verdict *before* recording the step that shows it about as often as after, so
        # "most recent node" lands on the previous test case. An outline is a claim about a
        # specific screen, and a red badge on a screen that is fine is the same category of
        # mistake as the dump misreads the screenshot rule exists to stop. Unlinked is
        # honest; approximately linked is not.
        node = str(args.get("step") or "").strip() or None
        record = await asyncio.to_thread(
            store.add_finding, session.package, session.slug,
            {"title": args["title"], "kind": kind,
             "severity": args.get("severity") or ("none" if kind == "pass" else "medium"),
             "expected": args["expected"], "actual": args["actual"],
             "steps": args.get("steps") or [], "evidence": evidence, "node": node})
        await session._emit({"type": "agent_finding", "finding": record})
        return _ok(f"Filed {record['id']} [{kind}]: {record['title']}.")

    @tool("list_findings", "Outcomes already recorded for this module — check before filing, so "
                           "the same case is not recorded twice.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def list_findings(_args: dict[str, Any]) -> dict[str, Any]:
        findings = store.list_findings(session.package, session.slug)
        if not findings:
            return _ok("Nothing recorded for this module yet.")
        return _ok("\n".join(
            f"{f['id']} [{f.get('kind', 'bug')}/{f.get('severity', '?')}] {f['title']}"
            for f in findings))

    # ---------------------------------------------------------------- humans & modules
    @tool("ask_user",
          "Pause and ask the user something only they can answer — a missing credential, a "
          "paywall, an OTP, or which of two readings of the spec is intended. The run parks "
          "until they reply, so use it when proceeding on a guess would make the result "
          "worthless, not for routine choices.",
          {"type": "object",
           "properties": {"question": {"type": "string"}},
           "required": ["question"], "additionalProperties": False})
    async def ask_user(args: dict[str, Any]) -> dict[str, Any]:
        answer = await session.ask(str(args["question"]))
        return _ok(f"The user replied: {answer}")

    @tool("propose_subprojects",
          "After a recon pass, propose how the app splits into testable modules. The user "
          "approves, renames or merges them before any testing starts — so propose what the "
          "app actually shows, not a generic template.",
          {"type": "object",
           "properties": {"modules": {
               "type": "array",
               "items": {"type": "object",
                         "properties": {"title": {"type": "string"},
                                        "scope": {"type": "string",
                                                  "description": "What testing this module covers"},
                                        "screens": {"type": "array", "items": {"type": "string"}}},
                         "required": ["title", "scope"], "additionalProperties": False}}},
           "required": ["modules"], "additionalProperties": False})
    async def propose_subprojects(args: dict[str, Any]) -> dict[str, Any]:
        modules = args.get("modules") or []
        if not modules:
            return _err("Propose at least one module.")
        created = [await asyncio.to_thread(
            store.create_subproject, session.package, m["title"], m.get("scope", ""),
            "proposed", m.get("screens") or []) for m in modules]
        await session._emit({"type": "agent_subprojects_proposed", "subprojects": created})
        answer = await session.ask(
            "Approve this module breakdown, or tell me what to rename, merge or drop.",
            kind="approval", payload={"subprojects": created})
        return _ok(f"Proposed {len(created)} modules. The user said: {answer}")

    return create_sdk_mcp_server(
        name="device",
        version="1.0.0",
        tools=[read_screen, screenshot, wait_for_text, wait_until_gone, check_crash,
               launch, tap_element, tap_text, tap_xy, type_text, use_credential,
               list_credentials, press, scroll, reset_app_data,
               journey_step, record_finding, list_findings, ask_user, propose_subprojects],
    )


# Tool names as the agent sees them, for the allow-list in runtime.py.
DEVICE_TOOL_NAMES = [
    "mcp__device__read_screen", "mcp__device__screenshot", "mcp__device__wait_for_text",
    "mcp__device__wait_until_gone", "mcp__device__check_crash", "mcp__device__launch",
    "mcp__device__tap_element", "mcp__device__tap_text", "mcp__device__tap_xy",
    "mcp__device__type_text", "mcp__device__use_credential", "mcp__device__list_credentials",
    "mcp__device__press", "mcp__device__scroll", "mcp__device__reset_app_data",
    "mcp__device__journey_step", "mcp__device__record_finding", "mcp__device__list_findings",
    "mcp__device__ask_user", "mcp__device__propose_subprojects",
]
