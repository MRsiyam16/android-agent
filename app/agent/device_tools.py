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
import base64
import io
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from claude_agent_sdk import create_sdk_mcp_server, tool
from PIL import Image

import config
from adb_device import DeviceError
from device import Device, create_device, detect_toolkit, platform_from_dump
from agent import store
from agent.store import StoreWriteError

from agent.guards import finding_block_reason, in_flight_text
from agent.screen import package_ranking, screen_elements, screen_texts

logger = logging.getLogger("agent.device_tools")

# Re-exported: these moved to agent/screen.py and agent/guards.py, but the tests and
# runtime.py import them from here and there is no reason to make them care.
__all__ = [
    "DeviceCancelled", "DeviceSession", "build_device_server", "DEVICE_TOOL_NAMES",
    "MANAGER_DEVICE_TOOL_NAMES", "VERDICT_TOOLS",
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


def _same_origin(a: str, b: str) -> bool:
    """Whether two web targets are the same site, ignoring scheme, `www.` and path.

    A web project's "package" is its home URL, so navigating to any other page of the same
    site produces a string that differs from it. That is a different *page*, not a different
    *app*, and the two must not be confused: findings are keyed to the project, and warning
    about a mismatch here would be warning about normal navigation.
    """
    def host(value: str) -> str:
        text = str(value or "").strip().split("://", 1)[-1]
        return text.split("/", 1)[0].split("?", 1)[0].removeprefix("www.").lower()

    left, right = host(a), host(b)
    return bool(left) and left == right


class DeviceSession:
    """One agent chat session's view of the device, plus its evidence trail.

    Normally one device ("a"). A module wired to a *pair* of devices — see `peer_serial` —
    also gets a "b" slot, entirely mirrored: its own connection, its own last-read screen,
    its own action count. The two never share state, because the whole point of driving two
    phones from one chat is comparing what one shows against what the other shows a moment
    later — collapsing them into one `last_xml` would make every read of device b overwrite
    the evidence a verdict about device a was about to be judged against.

    This is deliberately narrow rather than a general N-device session: nothing here
    generalises past two, and every device tool that takes `device` defaults to "a" so an
    ordinary single-device module (the overwhelming majority) never sees the parameter at
    all — `has_peer` gates it out of every tool schema built for a session without one.
    """

    def __init__(self, package: str, slug: str, serial: Optional[str] = None,
                 emit: Optional[Callable[[dict[str, Any]], Any]] = None,
                 platform: Optional[str] = None,
                 peer_serial: Optional[str] = None, peer_platform: Optional[str] = None,
                 package_a: Optional[str] = None, package_b: Optional[str] = None):
        # `package` holds whatever identifies the app on this platform: an Android package
        # name, or an iOS bundle id. The two are the same concept and are never mixed within
        # a project, so they share the field rather than the name being generalised across
        # ~500 call sites.
        self.package = package
        self.slug = slug
        self.serial = serial
        self.platform = platform
        self.emit = emit or (lambda _e: None)

        # The identifier to launch/compare against on each slot. Almost always just
        # `package` twice over — but a project's package is a *storage key* (the folder its
        # findings live under), and on a platform where the real package/bundle id differs
        # from that key (a project named for the product rather than for one platform's
        # install id), `package_a`/`package_b` let launch() and the "wrong app on screen"
        # check target the thing that is actually installed rather than the folder name.
        self.package_a = package_a or package
        self.package_b = package_b or package

        self._device: Optional[Device] = None
        self._journey = None
        self._section: Optional[str] = None
        self.last_elements: list[dict[str, Any]] = []
        self.last_xml: str = ""
        self.last_texts: list[str] = []
        self.shot_count = 0
        self.tap_count = 0

        # -- device b: only populated when this module drives a second phone ----------------
        self.peer_serial = peer_serial
        self.peer_platform = peer_platform
        self.has_peer = bool(peer_serial or peer_platform)
        self._device_b: Optional[Device] = None
        self.last_elements_b: list[dict[str, Any]] = []
        self.last_xml_b: str = ""
        self.last_texts_b: list[str] = []
        self.shot_count_b = 0
        self.tap_count_b = 0
        self.actions_since_read_b = 0
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
        # nothing is going to answer it now. Resolved, not `.cancel()`led: cancelling the
        # Future raises asyncio.CancelledError inside the parked `ask()` call, and the SDK's
        # control-request handler treats that specific exception as "the CLI already
        # abandoned this request" and deliberately sends back no response at all — which
        # leaves the CLI subprocess waiting forever for a tool result and makes Stop unable
        # to recover the run. `ask()` turns the cancelled flag into a normal `DeviceCancelled`
        # once this unblocks it, which every tool call here is expected to survive.
        if self._answer is not None and not self._answer.done():
            self._answer.set_result("")

    def resume(self) -> None:
        """Clear the flag so the next turn can run. Called when a new message arrives."""
        self._cancelled.clear()

    @property
    def resolved_platform(self) -> str:
        """Which platform this session drives, without touching the device.

        Shape-only inference on purpose: this is read while building the system prompt and on
        every screen render, and a subprocess on either path would be paid hundreds of times a
        run to answer a question the serial usually already settles.
        """
        from device import ANDROID, platform_from_serial
        return self.platform or platform_from_serial(self.serial) or ANDROID

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
    def _connect(self) -> Device:
        if self._device is None:
            self._device = create_device(self.serial, self.platform)
            self.serial = self._device.serial
        return self._device

    async def device(self) -> Device:
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

    # -- device b: the peer phone, only reachable when `has_peer` -----------------
    def _connect_b(self) -> Device:
        if self._device_b is None:
            self._device_b = create_device(self.peer_serial, self.peer_platform)
            self.peer_serial = self._device_b.serial
        return self._device_b

    async def device_b(self) -> Device:
        """Same as `device()`, for the peer phone."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._connect_b),
                timeout=config.AGENT_TOOL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise DeviceError(
                f"Connecting to device b took longer than "
                f"{config.AGENT_TOOL_TIMEOUT_SECONDS:.0f}s. Check the cable and that the "
                f"screen is on, then try again.") from exc

    async def device_at(self, which: str) -> Device:
        """Either slot, by name — the one dispatch point every tool routes through."""
        return await (self.device_b() if which == "b" else self.device())

    @property
    def resolved_platform_b(self) -> str:
        from device import ANDROID, platform_from_serial
        return self.peer_platform or platform_from_serial(self.peer_serial) or ANDROID

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
            answer = await self._answer
        finally:
            self.pending_question = None
            self._answer = None
        if self.cancelled:
            raise DeviceCancelled("Stopped by the user before the question was answered.")
        return answer

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

    async def capture(self, note: str = "", which: str = "a") -> Path:
        """Save a screenshot into the sub-project's evidence folder and return its path.

        `which="b"` shoots the peer phone instead, prefixed `b-` in the filename — both
        devices share one evidence folder, and without the prefix a device-a and a device-b
        shot from the same module could collide on the same running count.
        """
        d = await self.device_at(which)
        # `screenshot_b64` is the cross-platform method on the `Device` protocol; `d.d` is
        # AdbDevice's uiautomator2 handle and does not exist on IOSDevice at all — calling it
        # directly here used to raise `'IOSDevice' object has no attribute 'd'` on every iOS
        # capture, which silently blocked every finding on that platform (record_finding
        # requires a screenshot).
        b64 = await self.run(d.screenshot_b64)
        raw = Image.open(io.BytesIO(base64.b64decode(b64)))
        if which == "b":
            self.shot_count_b += 1
            count, prefix = self.shot_count_b, "b-"
        else:
            self.shot_count += 1
            count, prefix = self.shot_count, ""
        safe = re.sub(r"[^a-z0-9]+", "-", note.lower()).strip("-")[:40] or "shot"
        path = store.shots_dir(self.package, self.slug) / f"{prefix}{count:03d}-{safe}.jpg"
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


# -- dual-device dispatch --------------------------------------------------------------------
# Every device tool routes through these instead of touching `session.<attr>` directly, so the
# same tool body serves slot a and slot b without becoming two copies of itself. A session with
# no peer only ever sees which="a" — `_which` coerces "b" back to "a" when `has_peer` is False,
# so a stray argument on an ordinary single-device module is a no-op rather than a wrong-target
# tap on a phone that was never connected.
_DEVICE_ARG_SCHEMA = {
    "type": "string", "enum": ["a", "b"],
    "description": "Which phone this call targets. Defaults to 'a'. See the system prompt "
                   "for which device is 'a' and which is 'b'.",
}


def _with_device_arg(props: dict[str, Any], session: DeviceSession) -> dict[str, Any]:
    """A tool's `properties` dict, plus `device` when — and only when — this session has a
    second phone. Omitted entirely otherwise, so an ordinary module never sees a parameter
    that would always have to resolve to "a" anyway."""
    if not session.has_peer:
        return props
    return {**props, "device": _DEVICE_ARG_SCHEMA}


def _which(session: DeviceSession, args: dict[str, Any]) -> str:
    picked = str(args.get("device") or "a").strip().lower()
    return "b" if picked == "b" and session.has_peer else "a"


def _target_package(session: DeviceSession, which: str) -> str:
    return session.package_b if which == "b" else session.package_a


def _last(session: DeviceSession, which: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    if which == "b":
        return session.last_xml_b, session.last_elements_b, session.last_texts_b
    return session.last_xml, session.last_elements, session.last_texts


def _set_last(session: DeviceSession, which: str, xml: str, elements: list[dict[str, Any]],
             texts: list[str]) -> None:
    if which == "b":
        session.last_xml_b, session.last_elements_b, session.last_texts_b = xml, elements, texts
        session.actions_since_read_b = 0
    else:
        session.last_xml, session.last_elements, session.last_texts = xml, elements, texts
        session.actions_since_read = 0


def _bump_actions(session: DeviceSession, which: str) -> None:
    if which == "b":
        session.actions_since_read_b += 1
    else:
        session.actions_since_read += 1


def _bump_taps(session: DeviceSession, which: str) -> None:
    if which == "b":
        session.tap_count_b += 1
    else:
        session.tap_count += 1


def _peer_block_reason(session: DeviceSession) -> Optional[str]:
    """`guards.finding_block_reason`, mirrored for device b.

    Not folded into that function: it takes a `DeviceSession` and reads `.actions_since_read`
    / `.last_texts` directly, which are — deliberately — always device a's. Duplicating the
    two checks here (rather than generalising guards.py itself) keeps the single-device path,
    which is every other project in this harness, completely untouched.
    """
    if session.actions_since_read_b:
        return (
            f"You have acted {session.actions_since_read_b} time(s) on device b since its "
            f"last read_screen, so what you are describing is not what its screen currently "
            f"shows. Call read_screen(device=\"b\"), confirm the state, and file it then.")
    stalled = in_flight_text(session.last_texts_b)
    if stalled is not None:
        return (
            f"Device b's last read screen still showed {stalled!r}, which means its request "
            f"had not finished. Call wait_until_gone(text={stalled!r}, device=\"b\"), read "
            f"again, and judge that.")
    return None


def _render_screen(session: DeviceSession, xml: str, w: int, h: int,
                   texts: list[str], elements: list[dict[str, Any]],
                   target_package: Optional[str] = None) -> str:
    target = target_package or session.package
    ranking = package_ranking(xml)
    top_pkg = ranking[0][0] if ranking else ""
    lines: list[str] = []

    if not ranking:
        lines.append("WARNING: the dump has no readable nodes. The UI has probably not "
                     "rendered yet — wait, then read again. Do not conclude anything.")
    elif top_pkg != target:
        lines.append(
            f"WARNING: the screen is owned by `{top_pkg}` ({ranking[0][1]} nodes), not the app "
            f"under test (`{target}`). A dump shows only the topmost window, so the "
            f"app's own screen is hidden behind this one — most likely a system permission "
            f"dialog, another app's floating overlay, or the launcher. Deal with this first; "
            f"anything you conclude about the app right now would be about the wrong window.")
    if len(ranking) > 1:
        lines.append("packages on screen: " + ", ".join(f"{p} ({n})" for p, n in ranking[:4]))

    lines.append(f"screen {w}x{h} · toolkit={detect_toolkit(xml)} · "
                 f"{len(elements)} touchable elements")
    if not elements:
        # The Android reading of "zero controls" does not hold on iOS. There, a custom-drawn
        # surface publishes one element for the whole area and nothing for the buttons inside
        # it, so an empty list is an ordinary state rather than a covered screen — and telling
        # the agent to "deal with the overlay" would send it hunting for a problem that is not
        # there, which is how this harness invents defects. Measured on YouTube's player,
        # which exposes only Video Player, a fullscreen toggle and a scrubber.
        if platform_from_dump(xml) == "ios":
            lines.append(
                "NOTE: zero touchable controls. On iOS this is often NOT an overlay: a "
                "custom-drawn surface (video player, game, canvas) publishes one element for "
                "the whole area and none for the controls painted inside it. Screenshot "
                "first. If the screenshot shows controls the dump does not, tap by "
                "coordinate inside the surface rather than concluding anything is wrong.")
        else:
            lines.append("NOTE: zero touchable controls. That is almost never a real app "
                         "state — it usually means an overlay is intercepting input or the "
                         "screen is still loading.")

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


def _issue_description(session: DeviceSession, finding: dict[str, Any]) -> str:
    """Markdown body for a Blackcode issue filed from a finding — see `file_issue`."""
    lines: list[str] = []
    if finding.get("expected"):
        lines += ["## Expected", finding["expected"], ""]
    if finding.get("actual"):
        lines += ["## Actual", finding["actual"], ""]
    steps = finding.get("steps") or []
    if steps:
        lines.append("## Steps")
        lines += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
        lines.append("")

    # Which account this happened to. Taken from the finding's own stamp rather than read live,
    # so a ticket filed today for a finding from last week names the account that was signed in
    # last week. Falls back to the project's current accounts for findings recorded before the
    # stamp existed.
    import accounts
    import ecosystem as ecosystem_mod

    stamped = finding.get("accounts") or accounts.stamp(session.package)
    if stamped:
        role = ecosystem_mod.role_of(session.package) or session.package
        lines.append(accounts.as_markdown({session.package: stamped}, {session.package: role}))

    lines.append(f"---\nFiled automatically by QA Tester AI — module `{session.slug}`, "
                 f"finding `{finding['id']}`.")
    return "\n".join(lines)


def build_device_server(session: DeviceSession, *, can_file_findings: bool = True):
    """Build an MCP server whose tools are bound to this one session.

    `can_file_findings=False` omits the verdict tools (see `VERDICT_TOOLS`) — the manager
    module's setting. Keyword-only and defaulting to True so every existing caller, and every
    tester session, is unaffected.
    """

    # ---------------------------------------------------------------- observing
    @tool("read_screen",
          "Read the current screen: which package owns it, all visible text, and every "
          "touchable element with an id you can tap. Call this after every action that could "
          "change the screen. Returns a warning if another app or a system dialog is covering "
          "the app under test.",
          {"type": "object", "properties": _with_device_arg({}, session),
           "additionalProperties": False})
    async def read_screen(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        d = await session.device_at(which)
        try:
            xml = await session.run(d.dump_xml)
            w, h = await session.run(lambda: d.window_size)
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Could not read the screen: {exc}")
        texts = screen_texts(xml)
        elements = screen_elements(xml, w, h)
        _set_last(session, which, xml, elements, texts)
        return _ok(_render_screen(session, xml, w, h, texts, elements,
                                  target_package=_target_package(session, which)))

    @tool("screenshot",
          "Capture a screenshot and save it as evidence. Returns a file path — use the Read "
          "tool on that path to actually look at the image. Required before filing any finding.",
          {"type": "object",
           "properties": _with_device_arg(
               {"note": {"type": "string",
                        "description": "Optional short label, e.g. 'login-empty-submit'"}},
               session),
           "additionalProperties": False})
    async def screenshot(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        try:
            path = await session.capture(args.get("note") or "shot", which=which)
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Screenshot failed: {exc}")
        return _ok(f"Saved to {path}\nRead that path to view the image.")

    @tool("wait_for_text",
          "Poll the screen until the given text appears (substring, case-insensitive). Use "
          "this instead of a fixed sleep: a splash screen publishes nodes with no text, so "
          "text is the only honest readiness signal.",
          {"type": "object",
           "properties": _with_device_arg(
               {"text": {"type": "string"},
                "timeout": {"type": "number", "description": "Seconds, default 20"}},
               session),
           "required": ["text"], "additionalProperties": False})
    async def wait_for_text(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        needle = str(args["text"]).lower()
        deadline = time.monotonic() + float(args.get("timeout") or 20)
        d = await session.device_at(which)
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
           "properties": _with_device_arg(
               {"text": {"type": "string"},
                "timeout": {"type": "number", "description": "Seconds, default 30"}},
               session),
           "required": ["text"], "additionalProperties": False})
    async def wait_until_gone(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        needle = str(args["text"]).lower()
        deadline = time.monotonic() + float(args.get("timeout") or 30)
        d = await session.device_at(which)
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
          {"type": "object", "properties": _with_device_arg({}, session),
           "additionalProperties": False})
    async def check_crash(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        d = await session.device_at(which)
        excerpt = await session.run(d.read_new_crashes, _target_package(session, which))
        if excerpt:
            return _ok(f"CRASH / ANR detected:\n{excerpt}")
        return _ok("No crash or ANR in the log since the last check.")

    # ---------------------------------------------------------------- acting
    @tool("launch",
          "Force-stop and relaunch the app under test, then wait for its UI to render.\n\n"
          "ON A WEB PROJECT THIS IS ALSO THE ADDRESS BAR. Pass any URL — including a deep "
          "path and query string — and it navigates there: "
          "`launch({\"url\": \"https://site.example/claim?token=abc\"})`. You are not limited "
          "to the project's home page, and there is no separate goto/navigate tool to look "
          "for. This is how you follow a link the app showed you but did not make clickable.\n\n"
          "Returns what is on screen once it settles.",
          {"type": "object",
           "properties": _with_device_arg({
               "url": {"type": "string",
                       "description": "Web projects: the full URL to navigate to, any path or "
                                      "query string. Defaults to the project's own URL."},
               "package": {"type": "string",
                           "description": "Phone projects: the package/bundle id to launch. "
                                          "Defaults to the app under test. On web this is "
                                          "treated the same as `url`."},
           }, session),
           "additionalProperties": False})
    async def launch(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        # `url` and `package` are the same argument. The tool description has said since it was
        # written that a web project's `package` is a URL, and an agent still spent a run
        # concluding the harness could not navigate anywhere: it guessed `launch({url: ...})`,
        # got InputValidationError, guessed `launch({path: ...})`, got the same, and wrote a
        # permanent "this harness cannot open a URL" lesson into system memory over a
        # capability that was one argument name away. Twice-guessed is the name it wants.
        pkg = args.get("url") or args.get("package") or _target_package(session, which)
        d = await session.device_at(which)
        resolved_platform = session.resolved_platform_b if which == "b" else session.resolved_platform
        is_web = resolved_platform == "web"
        try:
            if await session.run(d.is_locked):
                return _err("The device is locked and I cannot unlock it. Ask the user to "
                            "unlock the phone, then try again.")
            if not await session.run(d.is_screen_on):
                await session.run(d.wake_screen)
            # Skipped on web: there is no "installed" concept for a URL — an unreachable one
            # fails honestly inside start_app's navigation instead, which is the same honesty
            # `is_installed` exists to provide for Android/iOS.
            if not is_web and not await session.run(d.is_installed, pkg):
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
        _set_last(session, which, xml, elements, screen_texts(xml))
        header = f"Launched {pkg}; UI became readable after {waited}s.\n"
        # Same site, different page is not a mislabelled project. Without this, every deep-link
        # navigation on a web project — the normal way to reach a page the app links to but
        # does not make clickable — was answered with "anything you record now is filed under
        # the wrong app", which reads as a warning not to do it.
        target = _target_package(session, which)
        same_site = is_web and _same_origin(pkg, target)
        if pkg != target and not same_site:
            # Otherwise the mislabelling is silent: findings and flow-graph steps are keyed to
            # the project, so they would be filed against an app that was never opened.
            header += (f"NOTE: this project is `{session.package}`, so anything you record now "
                       f"is filed under that package even though you are testing `{pkg}`. "
                       f"Mention the mismatch in your reply so the user can move the module to "
                       f"the right project.\n")
        return _ok(header + _render_screen(session, xml, w, h, screen_texts(xml), elements,
                                           target_package=target))

    @tool("tap_element",
          "Tap an element by the id from the last read_screen. Ids are the reliable way to "
          "tap: matching on a label instead can hit the wrong widget, because the app bar "
          "often carries the same accessibility label as the screen's primary button.",
          {"type": "object",
           "properties": _with_device_arg({
               "id": {"type": "string", "description": "id from read_screen, e.g. 540_1180"},
               "why": {"type": "string", "description": "What you expect this to do"}}, session),
           "required": ["id"], "additionalProperties": False})
    async def tap_element(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        target = str(args["id"])
        _, elements, _ = _last(session, which)
        match = next((e for e in elements if e["id"] == target), None)
        if match is None:
            known = ", ".join(e["id"] for e in elements[:15])
            return _err(f"No element with id={target} on the last read screen. Call read_screen "
                        f"again — the screen may have changed. Known ids: {known}")
        d = await session.device_at(which)
        try:
            await session.run(d.click, match["x"], match["y"])
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Tap failed: {exc}")
        _bump_taps(session, which)
        _bump_actions(session, which)
        await session._emit({"type": "agent_tap", "x": match["x"], "y": match["y"],
                             "label": match["label"]})
        return _ok(f"Tapped {match['label']!r} at ({match['x']},{match['y']}). "
                   f"Call read_screen to see the result.")

    @tool("tap_text",
          "Tap the element whose label matches this text. Excludes the app-bar band by "
          "default so a back header sharing the button's label cannot be hit by mistake. "
          "Fails when the text is ambiguous, rather than guessing.",
          {"type": "object",
           "properties": _with_device_arg({
               "text": {"type": "string"},
               "include_appbar": {"type": "boolean",
                                  "description": "Set true only to tap the header itself"}},
               session),
           "required": ["text"], "additionalProperties": False})
    async def tap_text(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        needle = str(args["text"]).lower()
        include_appbar = bool(args.get("include_appbar"))
        _, elements, _ = _last(session, which)
        pool = [e for e in elements if include_appbar or not e["in_appbar"]]
        exact = [e for e in pool if e["label"].lower() == needle]
        partial = [e for e in pool if needle in e["label"].lower()]
        candidates = exact or partial
        if not candidates:
            hidden = [e for e in elements if needle in e["label"].lower()]
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
        d = await session.device_at(which)
        try:
            await session.run(d.click, match["x"], match["y"])
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Tap failed: {exc}")
        _bump_taps(session, which)
        _bump_actions(session, which)
        await session._emit({"type": "agent_tap", "x": match["x"], "y": match["y"],
                             "label": match["label"]})
        return _ok(f"Tapped {match['label']!r} at ({match['x']},{match['y']}). "
                   f"Call read_screen to see the result.")

    @tool("select_option",
          "Web only. Pick an option on a native <select> dropdown by its id from read_screen "
          "(web-tag=\"select\") and the option's visible text. Do NOT tap the element open and "
          "then tap_element/tap_xy an option's position — the open option list renders as a "
          "browser-level popup outside the page, so screenshots can show it but a page click "
          "at that position lands on the page behind it and just closes the dropdown without "
          "selecting anything. This sets the value directly, bypassing that popup entirely.",
          {"type": "object",
           "properties": _with_device_arg({
               "id": {"type": "string", "description": "Element id from read_screen"},
               "option": {"type": "string",
                          "description": "The option's visible text (or its value)"}}, session),
           "required": ["id", "option"], "additionalProperties": False})
    async def select_option(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        target = str(args["id"])
        _, elements, _ = _last(session, which)
        match = next((e for e in elements if e["id"] == target), None)
        if match is None:
            known = ", ".join(e["id"] for e in elements[:15])
            return _err(f"No element with id={target} on the last read screen. Call read_screen "
                        f"again — the screen may have changed. Known ids: {known}")
        d = await session.device_at(which)
        select_fn = getattr(d, "select_option", None)
        if select_fn is None:
            return _err("select_option is web-only; this session is not driving a browser.")
        try:
            selected = await session.run(select_fn, match["x"], match["y"], str(args["option"]))
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"select_option failed: {exc}")
        _bump_actions(session, which)
        return _ok(f"Selected {selected!r} on {match['label']!r}. "
                   f"Call read_screen to see the result.")

    @tool("tap_xy", "Tap raw coordinates. Prefer tap_element; use this only when an element "
                    "has no dump entry (a canvas, a custom-drawn control).",
          {"type": "object",
           "properties": _with_device_arg(
               {"x": {"type": "integer"}, "y": {"type": "integer"}}, session),
           "required": ["x", "y"], "additionalProperties": False})
    async def tap_xy(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        d = await session.device_at(which)
        try:
            await session.run(d.click, int(args["x"]), int(args["y"]))
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Tap failed: {exc}")
        _bump_taps(session, which)
        _bump_actions(session, which)
        await session._emit({"type": "agent_tap", "x": int(args["x"]), "y": int(args["y"]),
                             "label": "raw tap"})
        return _ok(f"Tapped ({args['x']},{args['y']}).")

    @tool("type_text",
          "Type into the focused field. Tap the field first. Forms usually validate as you "
          "type, so an error can already be on screen before you submit.",
          {"type": "object",
           "properties": _with_device_arg({
               "text": {"type": "string"},
               "clear": {"type": "boolean", "description": "Clear the field first"}}, session),
           "required": ["text"], "additionalProperties": False})
    async def type_text(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        d = await session.device_at(which)
        try:
            await session.run(d.send_keys, str(args["text"]), bool(args.get("clear")))
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Typing failed: {exc}")
        _bump_actions(session, which)
        return _ok(f"Typed {args['text']!r}.")

    @tool("use_credential",
          "Type a stored test credential into the focused field without revealing its value. "
          "Use this instead of asking the user to paste a password into the chat. Call "
          "list_credentials to see which names exist.",
          {"type": "object",
           "properties": _with_device_arg(
               {"name": {"type": "string", "description": "e.g. test_email, test_password"}},
               session),
           "required": ["name"], "additionalProperties": False})
    async def use_credential(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
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
            try:
                await asyncio.to_thread(store.set_secret, session.package, name,
                                        answer.strip())
            except StoreWriteError as exc:
                return _err(f"The credential was NOT saved: {exc} The transcript keeps only "
                            f"a redaction, so the value is gone — ask the user for it again "
                            f"once the project folder is reachable.")
            secrets = await asyncio.to_thread(store.get_secrets, session.package)
        d = await session.device_at(which)
        try:
            await session.run(d.send_keys, secrets[name], True)
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Typing the credential failed: {exc}")
        _bump_actions(session, which)
        return _ok(f"Typed the stored value for {name!r} (not shown here).")

    @tool("list_credentials", "Names of the test credentials stored for this app. Values are "
                              "never returned — use use_credential to enter one.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def list_credentials(_args: dict[str, Any]) -> dict[str, Any]:
        keys = store.secret_keys(session.package)
        return _ok("Stored credentials: " + (", ".join(keys) if keys else "(none yet)"))

    @tool("press", "Press a hardware/navigation key: back, home, enter, recent, delete.",
          {"type": "object",
           "properties": _with_device_arg(
               {"key": {"type": "string",
                        "enum": ["back", "home", "enter", "recent", "delete"]}}, session),
           "required": ["key"], "additionalProperties": False})
    async def press(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        d = await session.device_at(which)
        try:
            await session.run(d.press, str(args["key"]))
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"press failed: {exc}")
        _bump_actions(session, which)
        return _ok(f"Pressed {args['key']}.")

    @tool("scroll", "Scroll the screen. Direction is the direction the *content* moves, so "
                    "'down' reveals what is further down the page. Swipes within the middle "
                    "band so the gesture is not stolen by the status bar or the nav bar. Note "
                    "that on a launcher home screen these map to the launcher's own gestures — "
                    "'up' pulls the notification shade down, 'down' opens the app drawer.",
          {"type": "object",
           "properties": _with_device_arg({
               "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
               "scale": {"type": "number", "description": "Fraction of screen height, default 0.6"}},
               session),
           "required": ["direction"], "additionalProperties": False})
    async def scroll(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        d = await session.device_at(which)
        try:
            await session.run(d.scroll, str(args["direction"]), float(args.get("scale") or 0.6))
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"scroll failed: {exc}")
        _bump_actions(session, which)
        return _ok(f"Scrolled {args['direction']}. Call read_screen to see what is visible now.")

    @tool("reset_app_data",
          "Wipe the app's data to drop a persisted session (pm clear on Android; cookies and "
          "local/session storage on web). Needed between sign-up cases, since a successful "
          "registration leaves you logged in. Expect a system permission prompt on the next "
          "launch, which belongs to another package. Always False on iOS — sign out through "
          "the app's own UI there instead.",
          {"type": "object",
           "properties": _with_device_arg(
               {"confirm": {"type": "boolean",
                            "description": "Must be true — this destroys app state"}}, session),
           "required": ["confirm"], "additionalProperties": False})
    async def reset_app_data(args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("confirm"):
            return _err("reset_app_data needs confirm=true; it destroys the app's local state.")
        which = _which(session, args)
        d = await session.device_at(which)
        cleared = await session.run(d.clear_app_data, _target_package(session, which))
        if not cleared:
            return _err("Clearing app data is not supported on this platform (or did not "
                        "succeed) — the persisted session is still there. Sign out through "
                        "the UI instead.")
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
           "properties": _with_device_arg({
               "label": {"type": "string", "description": "What this step did, e.g. \"Submit empty form\""},
               "case": {"type": "string", "description": "Test case name; starts a new chain when it changes"},
           }, session),
           "required": ["label"], "additionalProperties": False})
    async def journey_step(args: dict[str, Any]) -> dict[str, Any]:
        which = _which(session, args)
        await session.device_at(which)
        j = session.journey()
        case = args.get("case")
        section = f"{session.slug} / {case}" if case else session.slug
        if section != session._section:
            await asyncio.to_thread(j.start_section, section)
            session._section = section
            j._prev_node = None  # a new case starts its own chain, not a branch off the last one
        step_xml, _, _ = _last(session, which)
        try:
            node = await asyncio.to_thread(j.step, str(args["label"]),
                                           None, step_xml or None)
        except Exception as exc:  # noqa: BLE001
            return _err(f"Could not post the step: {exc}")
        # Indexed so the agent can find this node again by its label — see list_steps. The
        # board has the screen, but nothing else records which id it was given.
        try:
            await asyncio.to_thread(store.record_step, session.package, session.slug,
                                    node, str(args["label"]), section)
        except StoreWriteError as exc:
            return _err(f"The step index was NOT saved: {exc} The screen may be on the "
                        f"board, but list_steps will not find it, so do not rely on "
                        f"node {node} for link_finding.")
        await session._emit({"type": "agent_journey_step", "label": args["label"],
                             "section": section, "node": node})
        # The id is returned because record_finding takes it: it is the only way to say
        # *which* screen a verdict is about, and the agent cannot pass back a value it was
        # never told.
        posted = (
            f"Recorded step {j.step_count} of {section!r} on the flow graph as node {node}."
            if j.last_step_posted else
            # Deliberately not `_err`, which would invite a retry: every call mints a fresh
            # node id, so re-stepping the same screen after a failed post puts a duplicate
            # on the board rather than replacing anything. The local index did land, so the
            # id stays usable — the one thing this must not do is claim the board has it.
            f"WARNING: the flow graph did NOT receive this step — the telemetry post "
            f"failed, so node {node} is not drawn on the board. It is recorded locally and "
            f"list_steps will show it. Do not call journey_step again for this same screen; "
            f"a retry mints a new id and would duplicate the step rather than repair it. "
            f"Carry on testing and mention this in your reply."
        )
        return _ok(f"{posted} Pass step=\"{node}\" to record_finding if the outcome "
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
           } | ({"device": {**_DEVICE_ARG_SCHEMA,
                            "description": "Which phone's last-read screen the timing check "
                                           "should apply to. Defaults to 'a'."}}
                if session.has_peer else {}),
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
        which = _which(session, args)
        blocked = _peer_block_reason(session) if which == "b" else finding_block_reason(session)
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
        # A failed write is reported as a failure. Saying "Filed F003" for a verdict that
        # never reached the disk is the harness doing to the agent exactly what
        # `record_finding` refuses to let the agent do to the user: assert something it has
        # not verified. Retrying is safe — the write is all-or-nothing, so nothing was
        # half-filed and no id was consumed.
        try:
            record = await asyncio.to_thread(
                store.add_finding, session.package, session.slug,
                {"title": args["title"], "kind": kind,
                 "severity": args.get("severity") or ("none" if kind == "pass" else "medium"),
                 "expected": args["expected"], "actual": args["actual"],
                 "steps": args.get("steps") or [], "evidence": evidence, "node": node})
        except StoreWriteError as exc:
            return _err(f"The finding was NOT saved: {exc} Nothing was recorded, so this "
                        f"outcome is still unfiled. Check that the project folder is "
                        f"reachable, then file it again — retrying cannot double-file it.")
        await session._emit({"type": "agent_finding", "finding": record})
        return _ok(f"Filed {record['id']} [{kind}]: {record['title']}.")

    @tool("add_note",
          "Pin a note beside the current test case on the flow graph, in your own words: "
          "what you did here and what the app did back. The note's colour and the colour of "
          "the case's arrows come from `kind` — green for pass, amber for warning or "
          "suggestion, red for bug — so someone can read the shape of a run without opening "
          "anything.\n"
          "Write it for a person who was not watching: name the screen, the input and the "
          "wording the app answered with. One note per case, at the end of it; writing again "
          "for the same case replaces the earlier note rather than stacking up.",
          {"type": "object",
           "properties": {
               "text": {"type": "string",
                        "description": "The note body. A few sentences of plain prose, or a "
                                       "short Markdown list. No screenshots — the screens are "
                                       "already on the board next to it."},
               "kind": {"type": "string", "enum": ["pass", "warning", "bug", "suggestion"],
                        "description": "How this case ended. Drives the colour."},
               "title": {"type": "string",
                         "description": "Optional heading. Defaults to the case name."},
               "section": {"type": "string",
                           "description": "Which case to pin it beside, exactly as list_steps "
                                          "spells it. Omit during a live run and it uses the "
                                          "case you are currently recording steps under."},
           },
           "required": ["text", "kind"], "additionalProperties": False})
    async def add_note(args: dict[str, Any]) -> dict[str, Any]:
        # A note is anchored to a case, because that is where on the board it gets pinned.
        #
        # `session._section` is only set by journey_step, so it is None for the whole of a
        # review pass — going back over a finished run to mark it up, which is exactly when
        # notes are most wanted. Taking the name as an argument is what makes that possible;
        # falling back to the live section is what keeps it a one-liner during a run.
        section = str(args.get("section") or "").strip() or session._section
        if not section:
            return _err("Which case? Pass `section` spelled as list_steps shows it, or call "
                        "journey_step first and this will follow the case you are recording.")
        # Checked against the board rather than trusted. A note whose section matches no case
        # is not an error anywhere downstream — the page simply never finds a row to put it
        # beside, so it silently does not exist, which is the worst way for this to fail.
        known = {s["section"] for s in store.list_steps(session.package, session.slug)}
        if known and section not in known:
            listed = "\n".join(f"  {name}" for name in sorted(known))
            return _err(f"No case named {section!r} on this module's board. It must match "
                        f"exactly, including the module prefix. These exist:\n{listed}")
        kind = str(args["kind"])
        try:
            record = await asyncio.to_thread(
                store.add_note, session.package, session.slug,
                {"section": section, "kind": kind,
                 "title": str(args.get("title") or "").strip(),
                 "text": str(args["text"])})
        except StoreWriteError as exc:
            return _err(f"The note was NOT saved: {exc} Nothing was pinned to the board. "
                        f"Writing it again is safe — a note for a section replaces the one "
                        f"before it, so a retry cannot stack up duplicates.")
        await session._emit({"type": "agent_note", "note": record})
        return _ok(f"Pinned {record['id']} [{kind}] beside {section!r}.")

    @tool("link_finding",
          "Point an outcome you already filed at the screen it was about, so the board "
          "outlines that screen — red for a bug, amber for a warning or a suggestion. Use it "
          "when you filed the verdict before recording the step, or when reviewing a finished "
          "run where every screen is on the board. Only ever name a screen you have actually "
          "identified: the outline is a claim, and a red badge on a screen that is fine is "
          "the same kind of mistake as a dump misread.",
          {"type": "object",
           "properties": {
               "id": {"type": "string", "description": "Finding id, e.g. F007"},
               "step": {"type": "string", "description": "Node id from journey_step, e.g. agent-au-014"},
           },
           "required": ["id", "step"], "additionalProperties": False})
    async def link_finding(args: dict[str, Any]) -> dict[str, Any]:
        try:
            record = await asyncio.to_thread(
                store.link_finding, session.package, session.slug,
                str(args["id"]).strip(), str(args["step"]).strip())
        except StoreWriteError as exc:
            return _err(f"The link was NOT saved: {exc} The finding still points nowhere.")
        if record is None:
            return _err(f"No finding {args['id']!r} in this module. Call list_findings to "
                        f"see the ids.")
        await session._emit({"type": "agent_finding", "finding": record})
        return _ok(f"{record['id']} now points at {args['step']}; the board outlines that "
                   f"screen as {record.get('kind', 'bug')}.")

    @tool("list_steps",
          "The screens this module has put on the flow graph, as node id and label, grouped "
          "by test case. This is how you find the id to hand to link_finding or to "
          "record_finding's `step` — read the labels and pick the screen the outcome is "
          "actually about rather than assuming the most recent one.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def list_steps(_args: dict[str, Any]) -> dict[str, Any]:
        steps = store.list_steps(session.package, session.slug)
        if not steps:
            return _ok("This module has not recorded any flow-graph steps yet — "
                       "call journey_step as you walk a case.")
        lines: list[str] = []
        section = None
        for step in steps:
            if step.get("section") != section:
                section = step.get("section")
                lines.append(f"\n{section}:")
            lines.append(f"  {step['node']}  {step['label']}")
        return _ok("\n".join(lines).strip())

    @tool("list_findings", "Outcomes already recorded for this module — check before filing, so "
                           "the same case is not recorded twice.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def list_findings(_args: dict[str, Any]) -> dict[str, Any]:
        findings = store.list_findings(session.package, session.slug)
        if not findings:
            return _ok("Nothing recorded for this module yet.")
        return _ok("\n".join(
            f"{f['id']} [{f.get('kind', 'bug')}/{f.get('severity', '?')}] {f['title']}"
            + (f"  -> {f['node']}" if f.get("node") else "  (no screen linked)")
            for f in findings))

    @tool("file_issue",
          "Push an already-recorded finding out to Blackcode as a real, tracked issue — with "
          "the finding's evidence screenshot embedded inline. Only for bug/warning/suggestion "
          "findings; a pass is not an issue. This is a visible action outside this dashboard "
          "(a real ticket a team will see), so call it when the user asks you to file, raise, "
          "track or log a finding in Blackcode — not on your own initiative just because a "
          "finding exists. The first filing for this project needs `project` (a Blackcode "
          "project id, or its exact name like 'ClinicApp'); once given, it is remembered and "
          "later calls can omit it. Saves the resulting issue link on the finding itself, so "
          "it shows up on the outcomes board too.",
          {"type": "object",
           "properties": {
               "id": {"type": "string", "description": "Finding id, e.g. F007"},
               "project": {"type": "string",
                          "description": "Blackcode project id or exact name. Required only "
                                          "the first time this project files an issue."},
           },
           "required": ["id"], "additionalProperties": False})
    async def file_issue(args: dict[str, Any]) -> dict[str, Any]:
        import blackcode
        if not blackcode.is_available():
            return _err(
                f"The Blackcode CLI (`{config.BLACKCODE_CLI}`) is not installed or not on "
                f"PATH. Install it with `npm install -g @blackcode_sa/bc-issues`.")

        finding_id = str(args["id"]).strip()
        findings = store.list_findings(session.package, session.slug)
        finding = next((f for f in findings if f.get("id") == finding_id), None)
        if finding is None:
            known = ", ".join(f["id"] for f in findings) or "(none filed yet)"
            return _err(f"No finding {finding_id!r} in this module. Known ids: {known}")
        if finding.get("kind") == "pass":
            return _err(f"{finding_id} is a pass, not an issue — nothing to file.")
        if finding.get("issue_url"):
            return _err(f"{finding_id} is already filed: {finding['issue_url']}")

        try:
            project_arg = args.get("project")
            if project_arg:
                project_id = await asyncio.to_thread(blackcode.resolve_project, project_arg)
                await asyncio.to_thread(blackcode.remember_project_id, session.package, project_id)
            else:
                project_id = await asyncio.to_thread(blackcode.stored_project_id, session.package)
                if project_id is None:
                    projects = await asyncio.to_thread(blackcode.list_projects)
                    names = ", ".join(f"{p['name']!r} (id {p['id']})" for p in projects)
                    return _err(
                        f"No Blackcode project is set for this project yet. Call file_issue "
                        f"again with `project` set to one of: {names}")
        except blackcode.BlackcodeError as exc:
            return _err(str(exc))

        description = _issue_description(session, finding)
        try:
            result = await asyncio.to_thread(
                blackcode.create_issue, project_id, finding["title"], description,
                finding.get("severity", "medium"), finding.get("evidence"))
        except blackcode.BlackcodeError as exc:
            return _err(f"Could not file the issue: {exc}")

        try:
            await asyncio.to_thread(
                store.set_finding_tracking, session.package, session.slug, finding_id,
                issue_id=result["number"], issue_url=result["url"])
        except StoreWriteError as exc:
            return _err(f"Filed as Blackcode issue #{result['number']} ({result['url']}), but "
                       f"could NOT save that link on the finding: {exc}")
        return _ok(f"Filed {finding_id} as Blackcode issue #{result['number']}: {result['url']}")

    @tool("search_issues",
          "Search or browse existing Blackcode issues — for checking whether something is "
          "already tracked before filing a duplicate, or answering 'what's open on X'. "
          "Read-only; does not touch this module's findings. `query` matches title/description "
          "(or a bare issue number). Omit `project` to search the whole workspace.",
          {"type": "object",
           "properties": {
               "query": {"type": "string", "description": "Text to search for, or an issue #number"},
               "project": {"type": "string", "description": "Blackcode project id or exact name"},
               "status": {"type": "string",
                         "description": "backlog/todo/in_progress/done/cancelled"},
           },
           "additionalProperties": False})
    async def search_issues(args: dict[str, Any]) -> dict[str, Any]:
        import blackcode
        if not blackcode.is_available():
            return _err(
                f"The Blackcode CLI (`{config.BLACKCODE_CLI}`) is not installed or not on "
                f"PATH. Install it with `npm install -g @blackcode_sa/bc-issues`.")
        try:
            project_id = None
            if args.get("project"):
                project_id = await asyncio.to_thread(blackcode.resolve_project, args["project"])
            results = await asyncio.to_thread(
                blackcode.search_issues, args.get("query") or "", project_id, args.get("status"))
        except blackcode.BlackcodeError as exc:
            return _err(str(exc))
        if not results:
            return _ok("No matching issues.")
        return _ok("\n".join(
            f"#{r['number']} [{r['status']}] {r['title']} ({r['project_name']}) — {r['url']}"
            for r in results))

    @tool("check_issue_status",
          "Check the live status of finding(s) already filed to Blackcode via file_issue — "
          "'is this fixed yet'. Pass `id` for one finding, or omit it to check every finding "
          "in this module that has been filed. Updates the finding's resolved flag here to "
          "match Blackcode's status (done/cancelled = resolved) and reports what changed.",
          {"type": "object",
           "properties": {"id": {"type": "string", "description": "Finding id, e.g. F007 — "
                                                                    "omit to check all filed findings"}},
           "additionalProperties": False})
    async def check_issue_status(args: dict[str, Any]) -> dict[str, Any]:
        import blackcode
        if not blackcode.is_available():
            return _err(
                f"The Blackcode CLI (`{config.BLACKCODE_CLI}`) is not installed or not on "
                f"PATH. Install it with `npm install -g @blackcode_sa/bc-issues`.")

        findings = store.list_findings(session.package, session.slug)
        target_id = args.get("id")
        if target_id:
            finding = next((f for f in findings if f.get("id") == str(target_id).strip()), None)
            if finding is None:
                known = ", ".join(f["id"] for f in findings) or "(none filed yet)"
                return _err(f"No finding {target_id!r} in this module. Known ids: {known}")
            candidates = [finding]
        else:
            candidates = [f for f in findings if f.get("issue_id")]
            if not candidates:
                return _ok("No findings in this module have been filed to Blackcode yet.")

        lines = []
        for finding in candidates:
            number = finding.get("issue_id")
            if not number:
                lines.append(f"{finding['id']}: not filed to Blackcode yet.")
                continue
            try:
                live = await asyncio.to_thread(blackcode.issue_status, number)
            except blackcode.BlackcodeError as exc:
                lines.append(f"{finding['id']} (#{number}): could not check — {exc}")
                continue
            was_resolved = bool(finding.get("resolved"))
            if live["resolved"] != was_resolved:
                try:
                    await asyncio.to_thread(
                        store.set_finding_tracking, session.package, session.slug,
                        finding["id"], resolved=live["resolved"])
                except StoreWriteError as exc:
                    lines.append(f"{finding['id']} (#{number}): now {live['status']}, but "
                                 f"could NOT save that here — {exc}")
                    continue
                change = "now resolved" if live["resolved"] else "reopened"
                lines.append(f"{finding['id']} (#{number}): {live['status']} — {change}")
            else:
                lines.append(f"{finding['id']} (#{number}): {live['status']} (unchanged)")
        return _ok("\n".join(lines))

    @tool("set_test_account",
          "Record which account you are signed in as — the clinic, the doctor, the patient. "
          "Call it as soon as you log in, and again whenever you switch or create one. Every "
          "finding you file from then on is stamped with it automatically, and it goes into "
          "any issue raised from those findings.\n\n"
          "This matters more than it looks. Permissions and visibility are per account, so "
          "\"creating a Procedure fails with insufficient permissions\" is not yet a "
          "reportable defect — the first question a developer asks is which clinic, and "
          "without an answer the ticket stalls. Do not put it in the finding text instead: it "
          "gets written differently every time and is unsearchable.",
          {"type": "object",
           "properties": {
               "role": {"type": "string",
                        "description": "clinic, doctor, patient or admin"},
               "email": {"type": "string", "description": "The account's login email"},
               "label": {"type": "string",
                         "description": "How a human names it — \"QA Mira Test Clinic — Main "
                                        "Branch\", \"Dr Handoff Doctor\""},
               "note": {"type": "string",
                        "description": "Anything about it worth carrying, e.g. plan tier"},
           },
           "required": ["role"], "additionalProperties": False})
    async def set_test_account(args: dict[str, Any]) -> dict[str, Any]:
        import accounts

        try:
            entry = await asyncio.to_thread(
                accounts.set_account, session.package, str(args.get("role") or ""),
                email=str(args.get("email") or ""), label=str(args.get("label") or ""),
                note=str(args.get("note") or ""))
        except ValueError as exc:
            return _err(str(exc))
        who = entry.get("email") or entry.get("label")
        return _ok(f"Recorded: this project is being tested as {entry['role']} {who}. Every "
                   f"finding you file from now on carries it.")

    # -- the shared scratchpad ---------------------------------------------------------------
    #
    # The only channel out of this module that another app's agent can read. Everything else a
    # tester produces is partitioned by design — findings, memory and transcript all belong to
    # the module that wrote them — and that partitioning is exactly what made cross-app work
    # impossible: an Android module booked "Testina Doe, Tue 14:30, ref #4471" and the iPad
    # module that had to confirm it arrived could not find out what to look for. It went
    # looking for *an* appointment, found one, and reported success about something it had
    # never verified.
    #
    # A finding is the wrong shape for this. A finding is a verdict about the app; a booking
    # reference is a fact about the world that the next step needs.
    @tool("note_put",
          "Write a fact onto the product's shared scratchpad, where the agents testing the "
          "OTHER apps can read it. This is the only thing you produce that crosses out of this "
          "project — they cannot see your screen, your transcript or your findings. Use it "
          "whenever you create something a later step will have to find: a booking reference, "
          "an account you registered, a time slot, an order number. Facts, not verdicts; a "
          "verdict is a finding.",
          {"type": "object",
           "properties": {
               "key": {"type": "string",
                       "description": "Short kebab-case name, e.g. last-booking-ref"},
               "value": {"type": "string", "description": "The fact, in a line or two"},
           },
           "required": ["key", "value"], "additionalProperties": False})
    async def note_put(args: dict[str, Any]) -> dict[str, Any]:
        import ecosystem as ecosystem_mod
        import scratchpad

        name = await asyncio.to_thread(ecosystem_mod.ecosystem_of, session.package)
        if not name:
            return _err("This project is not part of a product, so there is nobody to leave a "
                        "note for. Record it as a finding or in your memory file instead.")
        try:
            entry = await asyncio.to_thread(
                scratchpad.put, name, str(args.get("key") or ""), str(args.get("value") or ""),
                author=f"{ecosystem_mod.role_of(session.package) or session.package}/"
                       f"{session.slug}")
        except ValueError as exc:
            return _err(str(exc))
        return _ok(f"Noted `{entry['key']}` on the {name} scratchpad. The other apps' agents "
                   f"can read it now.")

    @tool("note_get",
          "Read what the other apps' agents have written down. Check it before hunting for "
          "something another app was supposed to have created — the reference you need is "
          "usually already here, and looking for 'any appointment' instead of the right one is "
          "how a cross-app check reports a pass it never made.",
          {"type": "object",
           "properties": {"key": {"type": "string",
                                  "description": "One note's key. Omit for all of them."}},
           "additionalProperties": False})
    async def note_get(args: dict[str, Any]) -> dict[str, Any]:
        import ecosystem as ecosystem_mod
        import scratchpad

        name = await asyncio.to_thread(ecosystem_mod.ecosystem_of, session.package)
        if not name:
            return _err("This project is not part of a product, so there is no shared "
                        "scratchpad.")
        if args.get("key"):
            entry = await asyncio.to_thread(scratchpad.get, name, str(args["key"]))
            if entry is None:
                return _err(f"No note called `{args['key']}`. Everything on the pad:\n"
                            + await asyncio.to_thread(scratchpad.render, name))
            return _ok(f"{entry['key']}: {entry['value']}\n"
                       f"   written by {entry.get('author') or 'someone'} at "
                       f"{entry.get('updated_at')}")
        return _ok(await asyncio.to_thread(scratchpad.render, name))

    @tool("learn_lesson",
          "Record an operating lesson about *driving this harness* — never about the app "
          "under test; that belongs in record_finding or your memory file. Call this whenever "
          "something that is not your mistake cost you a stuck turn: a harness bug, a "
          "confusing device/browser quirk, a wrong default, a selector heuristic that "
          "misfired, a timing assumption that was too aggressive. Every future session, on "
          "any project, reads this back before it starts, under 'Operating notes learned from "
          "previous runs' — so call it yourself the moment you work out what went wrong. Do "
          "not just describe the obstacle in your reply and wait for someone to relay it; "
          "that is the difference between this harness getting smarter on its own and staying "
          "exactly as broken as it was for you. Reusing an id raises that lesson's confidence "
          "instead of duplicating it, so reuse one if you are confirming a lesson you already "
          "recorded.",
          {"type": "object",
           "properties": {
               "id": {"type": "string",
                     "description": "Short, stable, kebab-case id, e.g. "
                                     "'web-package-missing-scheme'."},
               "lesson": {"type": "string",
                         "description": "The rule itself, stated as an instruction for a "
                                        "future session — e.g. 'A web project's package can "
                                        "be a bare domain with no scheme; treat launch as "
                                        "https:// by default.'"},
               "evidence": {"type": "string",
                           "description": "What actually happened that taught you this."},
           },
           "required": ["id", "lesson"], "additionalProperties": False})
    async def learn_lesson(args: dict[str, Any]) -> dict[str, Any]:
        lesson_id = str(args["id"]).strip()
        text = str(args["lesson"]).strip()
        if not lesson_id or not text:
            return _err("Both id and lesson are required.")
        try:
            import system_memory as sysmem
            await asyncio.to_thread(sysmem.learn, lesson_id, text, str(args.get("evidence") or ""))
        except Exception as exc:  # noqa: BLE001
            return _err(f"Could not record the lesson: {exc}")
        return _ok(f"Recorded `{lesson_id}`. Every session after this one, on any project, "
                   f"will see it in its system prompt under 'Operating notes learned from "
                   f"previous runs'.")

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
        try:
            answer = await session.ask(str(args["question"]))
        except DeviceCancelled:
            return _ok("Stopped by the user before this was answered.")
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
        try:
            answer = await session.ask(
                "Approve this module breakdown, or tell me what to rename, merge or drop.",
                kind="approval", payload={"subprojects": created})
        except DeviceCancelled:
            return _ok(f"Proposed {len(created)} modules, but the user stopped before "
                       "approving them.")
        return _ok(f"Proposed {len(created)} modules. The user said: {answer}")

    @tool("check_responsive",
          "Web only. Resize the browser through mobile/tablet/desktop breakpoints (or your "
          "own), screenshot each, and report layout-issue CANDIDATES: horizontal overflow, an "
          "interactive element running off the horizontal edge, or two interactive elements "
          "overlapping heavily. These are candidates, not verdicts — Read each screenshot, "
          "confirm what is actually visible, and call record_finding per real issue with that "
          "breakpoint's screenshot as evidence. The viewport is restored to its size before "
          "this call once it finishes.",
          {"type": "object",
           "properties": {"breakpoints": {
               "type": "array",
               "description": "Defaults to mobile/tablet/desktop if omitted.",
               "items": {"type": "object",
                         "properties": {"name": {"type": "string"},
                                        "width": {"type": "integer"},
                                        "height": {"type": "integer"}},
                         "required": ["name", "width", "height"],
                         "additionalProperties": False}}},
           "additionalProperties": False})
    async def check_responsive(args: dict[str, Any]) -> dict[str, Any]:
        d = await session.device()
        set_viewport = getattr(d, "set_viewport", None)
        responsive_issues = getattr(d, "responsive_issues", None)
        if set_viewport is None or responsive_issues is None:
            return _err("check_responsive is web-only; this session is not driving a browser.")

        raw = args.get("breakpoints") or [
            {"name": name, "width": w, "height": h}
            for name, (w, h) in config.WEB_BREAKPOINTS.items()]
        try:
            original = await session.run(lambda: d.window_size)
        except (DeviceError, asyncio.TimeoutError) as exc:
            return _err(f"Could not read the current viewport: {exc}")

        lines = []
        try:
            for bp in raw:
                if session.cancelled:
                    return _ok("Stopped mid-sweep. " + "\n".join(lines))
                name = str(bp.get("name") or "")
                width, height = int(bp["width"]), int(bp["height"])
                try:
                    await session.run(set_viewport, width, height)
                    # A resize re-flows the page but does not reload it — this is a layout
                    # settle, not a network wait, so a short fixed pause is enough rather than
                    # the text-polling `wait_for_ui` needs after a real navigation.
                    session.wait_cancellable(0.4)
                    path = await session.capture(note=f"responsive-{name}")
                    issues = await session.run(responsive_issues)
                except (DeviceError, asyncio.TimeoutError) as exc:
                    lines.append(f"- {name} ({width}x{height}): FAILED — {exc}")
                    continue
                session.actions_since_read += 1
                if issues:
                    lines.append(f"- {name} ({width}x{height}) — {path}:")
                    lines.extend(f"    {i}" for i in issues)
                else:
                    lines.append(f"- {name} ({width}x{height}) — {path}: "
                                 f"no overflow, off-screen or overlapping elements detected")
        finally:
            try:
                await session.run(set_viewport, *original)
            except (DeviceError, asyncio.TimeoutError):
                pass

        return _ok("Responsive sweep — candidates, not verdicts. Read each screenshot before "
                   "deciding anything is real, then record_finding per real issue:\n"
                   + "\n".join(lines))

    tools = [read_screen, screenshot, wait_for_text, wait_until_gone, check_crash,
             launch, tap_element, tap_text, tap_xy, type_text, use_credential,
             list_credentials, press, scroll, reset_app_data,
             journey_step, record_finding, add_note, link_finding, list_steps,
             list_findings, file_issue, search_issues, check_issue_status, learn_lesson,
             ask_user, propose_subprojects, check_responsive, select_option, note_put,
             note_get, set_test_account]
    if not can_file_findings:
        tools = [t for t in tools if t.name not in VERDICT_TOOLS]
    if session.resolved_platform != "web":
        tools = [t for t in tools if t.name not in ("check_responsive", "select_option")]
    return create_sdk_mcp_server(name="device", version="1.0.0", tools=tools)


#: Tools that write a verdict about a test case. Withheld from the manager module.
#:
#: The manager's prompt tells it plainly that it has no `record_finding`. That sentence has to
#: be true rather than aspirational, and the difference is not cosmetic: the manager walks the
#: app during recon and forms impressions — "the cart total looked stale" — which are exactly
#: what a verdict is not. A finding is one named case with a screenshot behind it, and once an
#: impression is in findings.json nothing downstream can separate the two: the project's bug
#: count becomes partly recon guesswork, and `project_report` totals it up as fact.
#:
#: Left in the tester's list untouched, and enforced by *absence* rather than by the PreToolUse
#: gate. The gate would deny the call, but the tool would still be in the manager's tool
#: definitions — so the model would see a tool its prompt says it does not have, reach for it
#: at the moment it most wanted to, and spend the turn discovering the refusal. This harness
#: has already paid for that lesson once with the cheap-tier tools (see prompts._cost_section).
#:
#: `file_issue` and `check_issue_status` don't write a verdict themselves — they publish one
#: that already exists out to Blackcode, or sync its status back — but both operate on
#: `session.package`/`session.slug`'s own findings, and the manager never has any (it has no
#: `record_finding`). Withheld for the same reason as the cheap-tier tools above: a tool that
#: would only ever answer "no finding to file/check" is not a capability, it's a dead end the
#: model has to discover the hard way. `search_issues` is left off this list on purpose — it
#: only reads Blackcode's own issues, never this module's findings, so the manager can use it
#: too (e.g. to check whether something it noticed during recon is already tracked).
VERDICT_TOOLS = ("record_finding", "file_issue", "check_issue_status")


def _device_tool_names(*, can_file_findings: bool = True, web: bool = False) -> list[str]:
    """The device tools as the agent sees them, for the allow-list in runtime.py.

    Derived from one list rather than written out twice: an allow-list that disagrees with what
    `build_device_server` registered is a tool the agent can see and cannot call, or one it can
    call that nobody meant it to have. `web=True` adds `check_responsive` and `select_option`,
    which `build_device_server` itself only registers for a web session (see its own filter at
    the end) — the two have to agree for the same reason.
    """
    short = ["read_screen", "screenshot", "wait_for_text", "wait_until_gone", "check_crash",
             "launch", "tap_element", "tap_text", "tap_xy", "type_text", "use_credential",
             "list_credentials", "press", "scroll", "reset_app_data",
             "journey_step", "record_finding", "add_note", "link_finding", "list_steps",
             "list_findings", "file_issue", "search_issues", "check_issue_status",
             "learn_lesson", "ask_user", "propose_subprojects",
             # The one channel out of this module another app's agent can read.
             "note_put", "note_get", "set_test_account"]
    if web:
        short = short + ["check_responsive", "select_option"]
    return [f"mcp__device__{name}" for name in short
            if can_file_findings or name not in VERDICT_TOOLS]


DEVICE_TOOL_NAMES = _device_tool_names()
WEB_DEVICE_TOOL_NAMES = _device_tool_names(web=True)
MANAGER_WEB_DEVICE_TOOL_NAMES = _device_tool_names(can_file_findings=False, web=True)

#: The manager module's device allow-list: everything except the verdict tools.
MANAGER_DEVICE_TOOL_NAMES = _device_tool_names(can_file_findings=False)
