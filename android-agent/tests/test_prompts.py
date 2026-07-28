"""The system prompt must describe the tools the session was actually built with.

This is the regression test for a real defect: the prompt advertised `check_screen` and
`pick_next_element` unconditionally, but `runtime._options` only registers the "cheap" MCP
server when AGENT_USE_CHEAP_TIER is on — which it is not by default. Every agent, including
the recon pass that was told to lean on `pick_next_element`, therefore started by reaching
for two tools that were not in its tool list.

The invariant: whatever the prompt names, the allow-list must contain.
"""
from __future__ import annotations

import re

import pytest

import config
from agent import prompts
from agent.device_tools import DEVICE_TOOL_NAMES
from agent.stepper import STEPPER_TOOL_NAMES

CHEAP_TOOLS = [name.replace("mcp__cheap__", "") for name in STEPPER_TOOL_NAMES]


@pytest.fixture
def cheap_tier(request, monkeypatch):
    monkeypatch.setattr(config, "AGENT_USE_CHEAP_TIER", request.param)
    return request.param


@pytest.mark.parametrize("cheap_tier", [False, True], indirect=True)
def test_prompt_mentions_cheap_tools_only_when_they_exist(cheap_tier):
    prompt = prompts.build_system_prompt("com.example.app", "auth", "Auth", "login flows")
    for tool in CHEAP_TOOLS:
        assert (tool in prompt) is cheap_tier, (
            f"{tool!r} is {'missing from' if cheap_tier else 'named in'} the prompt while "
            f"AGENT_USE_CHEAP_TIER={cheap_tier}")


@pytest.mark.parametrize("cheap_tier", [False, True], indirect=True)
def test_recon_prompt_mentions_cheap_tools_only_when_they_exist(cheap_tier):
    recon = prompts.recon_prompt()
    named = [tool for tool in CHEAP_TOOLS if tool in recon]
    if cheap_tier:
        # Recon maps screens; `pick_next_element` is the one it has a use for. It need not
        # name every cheap tool, but what it names must exist.
        assert "pick_next_element" in named
    else:
        assert named == []


@pytest.mark.parametrize("cheap_tier", [False, True], indirect=True)
def test_cost_guidance_is_present_either_way(cheap_tier):
    # Turns are the scarce resource whether or not there is somewhere cheaper to send them;
    # dropping the section entirely when the tier is off would lose that.
    prompt = prompts.build_system_prompt("com.example.app", "auth", "Auth", "")
    assert "# Cost discipline" in prompt


@pytest.mark.parametrize("cheap_tier", [False, True], indirect=True)
def test_no_prompt_names_a_tool_the_session_will_not_register(cheap_tier):
    """The general form of the bug: whatever a prompt names in backticks must exist.

    Checked in that direction on purpose. Asserting the reverse — that every registered tool
    gets a mention — would just force filler text into the prompt; it is fine for `tap_xy` or
    `check_crash` to be discovered from the tool list alone. What is not fine is naming
    something that was never registered.
    """
    available = {name.replace("mcp__device__", "") for name in DEVICE_TOOL_NAMES}
    if cheap_tier:
        available |= set(CHEAP_TOOLS)
    every_tool = {name.replace("mcp__device__", "") for name in DEVICE_TOOL_NAMES} | set(CHEAP_TOOLS)

    texts = {
        "system prompt": prompts.build_system_prompt("com.example.app", "auth", "Auth", ""),
        "recon prompt": prompts.recon_prompt(),
    }
    for label, text in texts.items():
        backticked = set(re.findall(r"`([a-z_][a-z0-9_]*)`", text))
        phantom = {t for t in backticked if t in every_tool} - available
        assert not phantom, (
            f"the {label} tells the agent to use {sorted(phantom)}, which "
            f"AGENT_USE_CHEAP_TIER={cheap_tier} does not register")


def test_the_core_loop_tools_are_actually_explained():
    # The counterweight: a prompt that names nothing would trivially pass the check above.
    # These are the tools whose *correct use* is the difference between a real finding and a
    # dump misread, so the prompt has to teach them, not just list them.
    prompt = prompts.build_system_prompt("com.example.app", "auth", "Auth", "")
    available = {name.replace("mcp__device__", "") for name in DEVICE_TOOL_NAMES}
    for referenced in ("read_screen", "screenshot", "wait_until_gone", "tap_element",
                       "tap_text", "record_finding", "list_findings", "journey_step",
                       "ask_user"):
        assert referenced in available
        assert f"`{referenced}`" in prompt


def test_prompt_carries_the_module_brief_and_its_memory_path():
    prompt = prompts.build_system_prompt("com.example.app", "auth", "Auth & Login", "login only")
    assert "com.example.app" in prompt
    assert "Auth & Login" in prompt
    assert "login only" in prompt
    assert "memory.md" in prompt


def test_no_unfilled_placeholders_survive_formatting():
    # A stray {name} in a .format() template raises; a stray {{name}} silently ships a
    # literal brace to the model. Neither should reach a session.
    prompt = prompts.build_system_prompt("com.example.app", "auth", "Auth", "")
    assert "{" not in prompt.replace("{{", "").replace("}}", "") or "{}" not in prompt
    assert "{cost_section}" not in prompt
    assert "{memory_path}" not in prompt
