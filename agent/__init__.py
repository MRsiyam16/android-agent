"""The chat agent behind the dashboard's Agent tab.

Layout:

    store.py         projects / sub-projects / chat log / memory / credentials on disk
    device_tools.py  the agent's hands: in-process MCP tools wrapping AdbDevice
    stepper.py       cheap-tier vision calls over OpenRouter (per-tap decisions)
    prompts.py       the QA-tester system prompt, incl. this harness's hard-won lessons
    runtime.py       one live Claude Code session per sub-project, streamed to the browser

The planner is the Claude Code CLI driven through claude-agent-sdk, so it runs on the
local Max subscription rather than a metered API key. See config.py for why the two
model tiers exist and what must never be put in the environment.
"""
