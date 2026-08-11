"""One live Claude Code session per sub-project, streamed to the dashboard.

The planner is the Claude Code CLI, driven in-process through `claude-agent-sdk`. It picks up
the machine's subscription credentials, so planning and verdicts cost rate-limit window
rather than per-token spend. The device is reached through the SDK-MCP tools in
`device_tools`, which run in this same process.

Sessions are kept alive between chat messages on purpose. Re-spawning the CLI per message
would pay ~1-2s of startup each time and, worse, throw away the conversation — the agent
would forget which case it was on. `AgentSession` therefore holds one connected client per
(package, module) and feeds messages into it.

What this module refuses to do: silently degrade. If the subscription window is exhausted
mid-run, the run is parked with its session id preserved and the user is told, rather than
quietly finishing the test case on a different model and reporting the results as equivalent.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLINotFoundError,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import config
import system_memory as sysmem
from agent import device_tools, prompts, store
from agent.device_tools import DeviceSession, build_device_server
from agent.manager_tools import build_manager_server, manager_tool_names
from agent.stepper import Stepper, build_stepper_server, stepper_tool_names

logger = logging.getLogger("agent.runtime")

Emit = Callable[[dict[str, Any]], Awaitable[None]]

# File tools the agent legitimately needs: reading its own screenshots, and keeping its
# memory file and report up to date. Bash, web access and subagents are withheld — this agent
# tests a phone, and a shell would be an unnecessary blast radius inside a server process.
FILE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "TodoWrite"]
BLOCKED_TOOLS = ["Bash", "WebSearch", "WebFetch", "Task", "NotebookEdit", "KillShell",
                 "BashOutput", "SlashCommand"]


def _transcript_text(question: Optional[dict[str, Any]], text: str) -> str:
    """What a reply to a blocked question may be written into the transcript as.

    A credential is redacted, because three separate places promise it is: the tool tells
    the user it "will never be written into the transcript or the report"
    (device_tools.use_credential), `store.secret_keys` says values are injected server-side
    and never reach the transcript, and the secrets endpoint repeats it. The value still
    goes to `secrets.json`, which is the only place it was ever meant to live — but until
    this existed, the raw answer was appended to `chat.jsonl` verbatim and then replayed
    into the chat log by `GET /chat` every time the module was reopened. A password sitting
    in plain text in a file that, unlike secrets.json, carries no such protection.

    Everything else is stored as typed: an approval or a "done" is the conversation.
    """
    if (question or {}).get("kind") != "credential":
        return text
    name = str(((question or {}).get("payload") or {}).get("name") or "").strip()
    return f"(credential provided{f' for {name}' if name else ''} — value stored, not logged)"


def _tool_summary(name: str, payload: dict[str, Any]) -> str:
    """A one-line, human-readable version of a tool call for the chat log."""
    short = (name.replace("mcp__device__", "").replace("mcp__cheap__", "")
             .replace("mcp__manager__", ""))
    if short in ("read_screen", "list_findings", "list_credentials", "check_crash",
                 "list_modules", "project_report"):
        return short
    for key in ("id", "text", "label", "expectation", "goal", "question", "note", "key",
                "direction", "name", "title", "package", "slug"):
        if key in payload and isinstance(payload[key], (str, int)):
            value = str(payload[key])
            return f"{short}: {value[:90]}"
    if short == "propose_subprojects":
        mods = payload.get("modules") or []
        return f"{short}: {len(mods)} modules"
    return short


class AgentSession:
    """A conversation with one module's tester."""

    def __init__(self, package: str, slug: str, emit: Emit,
                 serial: Optional[str] = None, platform: Optional[str] = None):
        self.package = package
        self.slug = slug
        self.emit = emit
        self.stepper = Stepper()
        self.device = DeviceSession(package, slug, serial=serial, platform=platform, emit=emit)

        self._client: Optional[ClaudeSDKClient] = None
        self._lock = asyncio.Lock()
        self._resume_failed = False
        self.busy = False
        self.session_id: Optional[str] = None
        self.turns = 0
        self.parked_reason: Optional[str] = None
        # Reported by the CLI rather than assumed: the subscription's default model changes
        # over time, and a hardcoded label in the UI would quietly start lying.
        self.model: Optional[str] = None
        self.model_label: Optional[str] = None
        self.subscription: Optional[str] = None
        self.activity: Optional[str] = None
        # What the CLI says it can run, for the model picker. Populated at connect.
        self.available_models: list[dict[str, Any]] = []
        # An explicit override for this module, or None to take the CLI's default. Stored on
        # the sub-project so it survives a restart, and read back in `_options`.
        entry = store.get_subproject(package, slug) or {}
        self.requested_model: Optional[str] = entry.get("model") or None

    # -- lifecycle ---------------------------------------------------------------
    def _options(self) -> ClaudeAgentOptions:
        entry = store.get_subproject(self.package, self.slug) or {}
        title = entry.get("title", self.slug)
        scope = entry.get("scope", "")
        store.ensure_memory(self.package, self.slug, title)
        workdir = store.subproject_dir(self.package, self.slug)
        workdir.mkdir(parents=True, exist_ok=True)

        # The manager module is built differently from a tester in two matched ways: it gains
        # tools over the *project* (read any module's work, create a module) and it loses the
        # ones that file a verdict. It keeps the rest of the device tools because "does this
        # part of the app deserve a module" is a question you answer by looking at the app.
        #
        # Both halves are per-session rather than global, for the reason the cheap tier is (see
        # prompts._cost_section): the prompt is paired with a tool list, and a tool the prompt
        # describes but the session does not have costs a whole turn discovering it is absent.
        # The same is true in reverse — a tool the prompt says it does not have, sitting in the
        # tool definitions, is an invitation to reach for it.
        manager = store.is_main_slug(self.slug)
        is_web = self.device.resolved_platform == "web"
        if manager:
            names = (device_tools.MANAGER_WEB_DEVICE_TOOL_NAMES if is_web
                     else device_tools.MANAGER_DEVICE_TOOL_NAMES)
        else:
            names = (device_tools.WEB_DEVICE_TOOL_NAMES if is_web
                     else device_tools.DEVICE_TOOL_NAMES)
        allowed = names + FILE_TOOLS
        mcp_servers: dict[str, Any] = {
            "device": build_device_server(self.device, can_file_findings=not manager)}
        if config.AGENT_USE_CHEAP_TIER:
            mcp_servers["cheap"] = build_stepper_server(self.stepper, self.device)
            allowed += stepper_tool_names()
        if manager:
            mcp_servers["manager"] = build_manager_server(self.device)
            allowed += manager_tool_names()

        async def gate(payload: dict[str, Any], _tool_use_id: Optional[str],
                       _ctx: Any) -> dict[str, Any]:
            """Hard allow-list, enforced as a PreToolUse hook.

            It has to be a hook rather than `can_use_tool`: under `bypassPermissions` the SDK
            auto-approves before that callback is ever consulted, so a gate written there is
            silently dead code (the SDK warns about exactly this). `bypassPermissions` is
            still needed — there is no human at the CLI to answer a prompt, so anything that
            waits for approval would simply hang.
            """
            name = str(payload.get("tool_name") or "")
            if name in allowed:
                return {}
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason":
                    f"{name} is not available to the testing agent. Drive the phone with the "
                    f"device tools; use Read/Write only for screenshots, memory and the report.",
            }}

        options = ClaudeAgentOptions(
            system_prompt=prompts.build_system_prompt(
                self.package, self.slug, title, scope,
                platform=self.device.resolved_platform),
            mcp_servers=mcp_servers,
            allowed_tools=allowed,
            disallowed_tools=BLOCKED_TOOLS,
            hooks={"PreToolUse": [HookMatcher(hooks=[gate])]},
            permission_mode="bypassPermissions",
            cwd=str(workdir),
            # Don't inherit the repo's CLAUDE.md or settings: those describe how to *develop*
            # this harness, which would only distract an agent whose job is to test a phone.
            setting_sources=None,
            max_turns=config.AGENT_MAX_TURNS,
            effort=config.AGENT_PLANNER_EFFORT,  # type: ignore[arg-type]
        )
        # The picker wins over the configured default: it is the more specific, more recent
        # statement of intent, and it is per-module by design — recon can run on a cheaper
        # model than the suite that files defects.
        chosen = self.requested_model or config.AGENT_PLANNER_MODEL
        if chosen:
            options.model = chosen
        if config.AGENT_PLANNER_FALLBACK:
            options.fallback_model = config.AGENT_PLANNER_FALLBACK

        # Pick the conversation back up across a server restart. Without this, the chat log on
        # disk would still display but the agent itself would have forgotten which case it was
        # on — and "say continue once the window resets" would be an empty promise.
        prior = entry.get("cli_session_id")
        if prior and not self._resume_failed:
            options.resume = prior
        return options

    async def connect(self) -> None:
        if self._client is not None:
            return
        if os.environ.get("ANTHROPIC_API_KEY"):
            # Loud, because the failure is invisible otherwise: everything works, and the
            # subscription the user is paying for is bypassed in favour of metered billing.
            logger.warning(
                "ANTHROPIC_API_KEY is set in this process. It overrides the Claude Code "
                "subscription profile, so agent calls will be billed per token. Unset it "
                "unless that is what you want.")
        try:
            client = ClaudeSDKClient(options=self._options())
            await client.connect()
        except CLINotFoundError as exc:
            raise RuntimeError(
                "The Claude Code CLI was not found. Install it with "
                "`npm i -g @anthropic-ai/claude-code`, then run `claude` once to sign in "
                f"with your subscription. ({exc})") from exc
        except Exception as exc:  # noqa: BLE001
            # A stored session id can go stale (transcript pruned, different machine). Losing
            # the history is a nuisance; refusing to start at all would be worse.
            if self._resume_failed:
                raise
            logger.warning("Could not resume the previous conversation (%s) — starting a fresh "
                           "one for %s/%s", exc, self.package, self.slug)
            self._resume_failed = True
            client = ClaudeSDKClient(options=self._options())
            await client.connect()
        self._client = client
        await self._read_server_info(client)
        logger.info("agent session connected for %s/%s (model=%s, %s)",
                    self.package, self.slug, self.model, self.subscription or "auth unknown")
        await self.emit({"slug": self.slug, "package": self.package, "type": "agent_ready",
                         "model": self.model, "model_label": self.model_label,
                         "subscription": self.subscription, "warm": True})

    async def _read_server_info(self, client: ClaudeSDKClient) -> None:
        """Ask the CLI what model it will actually use, and how it is authenticated.

        Worth doing at connect time because the alternative is waiting for the first turn to
        report a model — the UI would sit on "connecting…" until you spoke to it. It also
        surfaces the subscription type, so the claim that this runs on the subscription rather
        than a metered key is something the UI can show rather than something I assert.
        """
        try:
            info = await client.get_server_info()
        except Exception as exc:  # noqa: BLE001 - informational only
            logger.debug("get_server_info failed: %s", exc)
            return
        if not isinstance(info, dict):
            return

        models = [m for m in (info.get("models") or []) if isinstance(m, dict)]
        # Kept so the picker offers exactly what this CLI can actually run, rather than a
        # hardcoded list that goes stale the moment a model is added or retired.
        self.available_models = [
            {"value": m.get("value"),
             "label": m.get("displayName") or m.get("description") or m.get("value"),
             "resolved": m.get("resolvedModel")}
            for m in models if m.get("value")
        ]
        wanted = self.requested_model or config.AGENT_PLANNER_MODEL or "default"
        entry = next((m for m in models if m.get("value") == wanted), None) \
            or next((m for m in models if m.get("value") == "default"), None) \
            or (models[0] if models else None)
        if entry:
            self.model = entry.get("resolvedModel") or entry.get("value")
            self.model_label = entry.get("description") or entry.get("displayName")

        account = info.get("account") if isinstance(info.get("account"), dict) else {}
        sub = account.get("subscriptionType")
        provider = account.get("apiProvider")
        if sub:
            self.subscription = f"{sub}" + (" (API key)" if provider and provider != "firstParty"
                                            else "")

    async def warm(self) -> dict[str, Any]:
        """Connect the CLI without sending anything.

        Startup and module-selection both call this so the first message does not pay the
        CLI's spawn cost. It is only worth doing per (package, module), because the system
        prompt, working directory and MCP servers are all fixed when the session connects —
        a generic pre-warmed session could not be re-pointed at a module afterwards.
        """
        await self.connect()
        return {"ready": True, "model": self.model, "model_label": self.model_label,
                "subscription": self.subscription, "session_id": self.session_id}

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("disconnect failed: %s", exc)
            self._client = None

    async def set_model(self, model: Optional[str]) -> dict[str, Any]:
        """Switch which model this module runs on.

        The model is fixed when `ClaudeSDKClient` connects, so this necessarily tears the
        session down and builds a new one. The conversation is not lost: `cli_session_id` is
        already persisted and `_options` resumes from it, so the new session opens knowing
        which case it was on. Refused mid-turn — swapping the model under a running test would
        finish it on a different model than it started on, which is the exact thing this
        harness refuses to do silently on a rate limit.
        """
        if self.busy:
            raise RuntimeError("The agent is mid-run. Press Stop before changing the model.")
        if (model or None) == self.requested_model:
            return {"ok": True, "model": self.model, "unchanged": True}
        self.requested_model = model or None
        await asyncio.to_thread(store.update_subproject, self.package, self.slug,
                                model=self.requested_model)
        await self.close()
        # A failed reconnect must not leave the module pointing at a model it could not start,
        # or every later message fails the same way with no clue why.
        try:
            await self.connect()
        except Exception:
            self.requested_model = None
            await asyncio.to_thread(store.update_subproject, self.package, self.slug,
                                    model=None)
            await self.connect()
            raise
        return {"ok": True, "model": self.model, "model_label": self.model_label}

    async def interrupt(self) -> bool:
        """Stop button.

        Two halves, because `interrupt()` alone was not a stop. It asks the CLI to end the
        turn *after* the tool in flight returns — and the tools that make Stop worth pressing
        are the slow ones: `wait_for_ui` blocks up to 120s, a settle poll runs for tens of
        seconds. Pressing Stop and watching the agent keep tapping for two minutes is why this
        read as broken.

        So the device session is cancelled first: that flips a flag the long-running device
        tools poll, and they return "cancelled" at their next check instead of running to
        term. Then the CLI is asked to end the turn, which it now can do promptly.
        """
        was_blocked = self.device.pending_question is not None
        if self._client is None or not self.busy:
            # Still worth cancelling the device: a tool can be mid-flight in the window
            # between the turn ending and `busy` clearing.
            self.device.cancel()
            return False
        self.device.cancel()
        if was_blocked:
            # Emitted here rather than waited for: the turn itself still has to unwind through
            # the CLI, which can take a moment, but the question Stop just voided should not
            # keep sitting in the browser looking like it is still waiting to be answered.
            await self.emit({"slug": self.slug, "type": "agent_unblocked"})
        try:
            await self._client.interrupt()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("interrupt failed: %s", exc)
            return False

    # -- the turn ----------------------------------------------------------------
    async def send(self, text: str) -> None:
        """Feed one user message in and stream everything that comes back."""
        # A reply to a blocked question resolves the waiting tool instead of starting a turn.
        # Checked before the busy guard below: the run is still `busy` for the entire time it
        # is parked on a question, so a busy-first check meant a reply here always hit "the
        # agent is still working" and `answer()` was never called — the parked `ask()` future
        # never resolved, and the run hung forever with no way to answer it.
        if self.device.pending_question is not None:
            # Captured before `answer()`, which is what lets the waiting `ask()` clear it.
            question = self.device.pending_question
            if self.device.answer(text):
                await asyncio.to_thread(
                    store.append_chat, self.package, self.slug,
                    {"role": "user", "text": _transcript_text(question, text)})
                await self.emit({"slug": self.slug, "type": "agent_unblocked"})
                return

        if self.busy:
            await self.emit({"slug": self.slug, "type": "agent_error",
                             "message": "The agent is still working. Press Stop first if you "
                                        "want to redirect it."})
            return

        await asyncio.to_thread(store.append_chat, self.package, self.slug,
                                {"role": "user", "text": text})
        async with self._lock:
            # A Stop from the previous turn leaves the cancel flag raised. Without clearing it
            # here, every device tool in the *next* turn would return "stopped" immediately and
            # the agent would look permanently broken after one press of the button.
            self.device.resume()
            # Cleared here rather than left to the next successful result: `parked_reason`
            # is what /agent/status and GET /chat report, and the browser renders it as a
            # sticky "parked" state. Set once on a rate limit and never reset, a module that
            # hit the window in the morning still claimed to be parked after runs that
            # finished perfectly — the state outlived the condition it described.
            self.parked_reason = None
            self.busy = True
            await self.emit({"slug": self.slug, "type": "agent_busy", "busy": True})
            try:
                await self.connect()
                assert self._client is not None
                await self._client.query(text)
                await self._pump()
            except Exception as exc:  # noqa: BLE001 - surface everything in the chat
                logger.exception("agent turn failed")
                await asyncio.to_thread(store.append_chat, self.package, self.slug,
                                        {"role": "error", "text": str(exc)})
                await self.emit({"slug": self.slug, "type": "agent_error", "message": str(exc)})
            finally:
                self.busy = False
                await self.emit({"slug": self.slug, "type": "agent_busy", "busy": False})

    async def _pump(self) -> None:
        """Translate the SDK's message stream into browser events + transcript entries.

        Every `store.*` call here goes through a worker thread. This is the hottest path in
        the server — one append per text block and one per tool call, each taking the store's
        process-wide lock — and `device_tools` already states the rule its own `capture()`
        follows: a blocking call on the event loop stalls the WebSocket feeding the browser,
        so the UI would freeze exactly when the agent is busy and there is most to show.
        """
        assert self._client is not None
        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                if message.model and message.model != self.model:
                    self.model = message.model
                    await self.emit({"slug": self.slug, "type": "agent_model",
                                     "model": self.model})
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        await asyncio.to_thread(store.append_chat, self.package, self.slug,
                                                {"role": "agent", "text": block.text})
                        await self.emit({"slug": self.slug, "type": "agent_text",
                                         "text": block.text})
                    elif isinstance(block, ThinkingBlock):
                        await self.emit({"slug": self.slug, "type": "agent_thinking"})
                    elif isinstance(block, ToolUseBlock):
                        summary = _tool_summary(block.name, block.input or {})
                        self.activity = summary
                        await asyncio.to_thread(store.append_chat, self.package, self.slug,
                                                {"role": "tool", "tool": block.name,
                                                 "summary": summary})
                        await self.emit({"slug": self.slug, "type": "agent_tool",
                                         "tool": block.name, "summary": summary})

            elif isinstance(message, UserMessage):
                # Tool results come back as user-role content; surface only failures, since
                # the successful ones are already implied by the agent's own narration.
                for block in message.content if isinstance(message.content, list) else []:
                    if isinstance(block, ToolResultBlock) and block.is_error:
                        detail = block.content
                        if isinstance(detail, list):
                            detail = " ".join(
                                b.get("text", "") for b in detail if isinstance(b, dict))
                        await self.emit({"slug": self.slug, "type": "agent_tool_error",
                                         "text": str(detail)[:400]})

            elif isinstance(message, SystemMessage):
                await self._handle_system(message)

            elif isinstance(message, ResultMessage):
                await self._remember_session(message.session_id)
                self.turns += message.num_turns or 0
                await self._handle_result(message)

    async def _remember_session(self, session_id: Optional[str]) -> None:
        if not session_id or session_id == self.session_id:
            return
        self.session_id = session_id
        await asyncio.to_thread(store.update_subproject, self.package, self.slug,
                                cli_session_id=session_id)

    async def _handle_system(self, message: SystemMessage) -> None:
        data = message.data if isinstance(message.data, dict) else {}
        if message.subtype == "init":
            await self._remember_session(data.get("session_id"))
            model = data.get("model")
            if model and model != self.model:
                self.model = model
                await self.emit({"slug": self.slug, "type": "agent_model", "model": self.model})
            return
        # Rate-limit notices arrive as system messages; shape varies by CLI version, so match
        # loosely rather than depend on one key.
        blob = str(data).lower()
        if "rate" in blob and "limit" in blob:
            await self.emit({"slug": self.slug, "type": "agent_notice",
                             "text": "Claude subscription rate-limit notice: "
                                     f"{str(data)[:300]}"})

    async def _handle_result(self, message: ResultMessage) -> None:
        reason = (message.stop_reason or "") + " " + (message.terminal_reason or "")
        blob = (reason + " " + str(message.errors or "") + " " +
                str(message.api_error_status or "")).lower()
        parked = "rate" in blob and "limit" in blob

        # One instruction carried to completion is one run, however it ended. This is the
        # Agent tab's counterpart to run_agent.py's `sysmem.run_session`, which cannot bracket
        # anything here — the work arrives as messages into a server that outlives it, so
        # without this `run_count` stayed at 0 no matter how much testing happened, and the
        # digest kept reporting that no run had ever been recorded. Threaded, like every other
        # store write on this path: it reads and rewrites two files.
        await asyncio.to_thread(
            sysmem.record_run, f"agent:{self.slug}",
            (message.duration_ms or 0) / 1000.0,
            not (parked or message.is_error),
            turns=message.num_turns or 0, taps=self.device.tap_count)

        if parked:
            self.parked_reason = "rate_limit"
            note = (
                "The Claude subscription window is exhausted, so I have stopped mid-run "
                "rather than finishing this on a different model and presenting the results "
                "as equivalent. The conversation is saved — say 'continue' once the window "
                "resets and I will pick up from here.")
            await asyncio.to_thread(store.append_chat, self.package, self.slug,
                                    {"role": "error", "text": note})
            await self.emit({"slug": self.slug, "type": "agent_parked", "reason": "rate_limit",
                             "text": note})
            return

        if message.is_error:
            detail = str(message.errors or message.result or reason).strip() or "unknown error"
            await asyncio.to_thread(store.append_chat, self.package, self.slug,
                                    {"role": "error", "text": detail})
            await self.emit({"slug": self.slug, "type": "agent_error", "message": detail[:500]})
            return

        await asyncio.to_thread(store.update_subproject, self.package, self.slug,
                                last_run_at=store.now(), status="tested")
        await self.emit({
            "slug": self.slug,
            "type": "agent_done",
            "turns": message.num_turns,
            "duration_ms": message.duration_ms,
            "taps": self.device.tap_count,
            "shots": self.device.shot_count,
            "stepper_calls": self.stepper.calls,
            "stepper_cost_usd": round(self.stepper.cost_usd, 4),
            "findings": len(await asyncio.to_thread(store.list_findings,
                                                    self.package, self.slug)),
        })


class SessionRegistry:
    """All live agent sessions, keyed by package + module."""

    def __init__(self, emit: Emit):
        self.emit = emit
        self._sessions: dict[tuple[str, str], AgentSession] = {}

    def get(self, package: str, slug: str, serial: Optional[str] = None,
            platform: Optional[str] = None) -> AgentSession:
        key = (package, slug)
        if key not in self._sessions:
            self._sessions[key] = AgentSession(
                package, slug, self.emit, serial=serial, platform=platform)
        return self._sessions[key]

    def peek(self, package: str, slug: str) -> Optional[AgentSession]:
        return self._sessions.get((package, slug))

    async def close(self, package: str, slug: str) -> None:
        session = self._sessions.pop((package, slug), None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()

    def status(self) -> list[dict[str, Any]]:
        return [{"package": p, "slug": s, "busy": sess.busy,
                 "blocked": sess.device.pending_question,
                 "parked": sess.parked_reason,
                 "model": sess.model,
                 "model_label": sess.model_label,
                 "subscription": sess.subscription,
                 "activity": sess.activity,
                 "stepper_cost_usd": round(sess.stepper.cost_usd, 4)}
                for (p, s), sess in self._sessions.items()]

    async def warm(self, package: str, slug: str, serial: Optional[str] = None,
                   platform: Optional[str] = None) -> dict[str, Any]:
        return await self.get(package, slug, serial=serial, platform=platform).warm()
