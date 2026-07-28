"""The one SessionRegistry, and the wire it pushes agent events down.

Sessions live in this process: the planner is the Claude Code CLI driven through
claude-agent-sdk, and the device tools are in-process MCP tools, so an agent's every step
can be pushed straight out over the dashboard's WebSocket.

Separate from routes/agent.py because the startup pre-warm and the shutdown close both
need the registry without pulling in the route module.
"""
from __future__ import annotations

from typing import Any

from agent import runtime as agent_runtime

from .state import manager


async def _emit(event: dict[str, Any]) -> None:
    await manager.broadcast(event)


sessions = agent_runtime.SessionRegistry(_emit)
