"""`record_finding` refuses a verdict read off a stale or mid-flight screen.

Both of this harness's worst false defects — "unknown credentials were accepted" and "a
second account was created with an address already in use" — were correct app behaviour
judged at the wrong moment: once through an "Authentication Error" modal, once through a
"Creating your account…" spinner. The prompt has always warned about it. These tests cover
the part that does not depend on the model remembering.
"""
from __future__ import annotations

import asyncio

import pytest

import mcp.types as mcp_types

import project_paths
from agent import store
from agent.device_tools import (
    DeviceSession,
    build_device_server,
    finding_block_reason,
    in_flight_text,
)

PKG, SLUG = "com.example.app", "auth"


@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    # Patched on project_paths, which is what store.project_dir actually consults now that a
    # project can live outside the repo. See the same fixture in test_store.py.
    root = tmp_path / "projects"
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", root)
    monkeypatch.setattr(store, "PROJECTS_DIR", root)
    store.create_subproject(PKG, "Auth", "")
    return tmp_path


@pytest.fixture
def evidence(tmp_path):
    path = tmp_path / "shot.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg")
    return str(path)


@pytest.fixture
def session():
    return DeviceSession(PKG, SLUG)


@pytest.fixture
def file_finding(session):
    """Call the real record_finding tool through the MCP server, as the agent does.

    Driven via the server's CallToolRequest handler rather than the local closure, so the
    guard is exercised on the path that actually runs — a guard that only holds when called
    directly would be no guard at all.
    """
    server = build_device_server(session)["instance"]
    handler = server.request_handlers[mcp_types.CallToolRequest]

    def call(**overrides):
        args = {"title": "Empty submit accepted", "severity": "high",
                "expected": "the form rejects an empty email",
                "actual": "the form submitted and landed on the home screen"}
        args.update(overrides)
        request = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name="record_finding", arguments=args),
        )
        return asyncio.run(handler(request))

    return call


def text_of(result) -> str:
    return result.root.content[0].text


def is_error(result) -> bool:
    return bool(result.root.isError)


class TestInFlightDetection:
    @pytest.mark.parametrize("marker", [
        "Creating your account…", "Signing in", "Please wait", "Loading",
        "Verifying your details", "One moment", "Authenticating…",
    ])
    def test_common_progress_wording_is_recognised(self, marker):
        assert in_flight_text(["Sign up", marker, "Cancel"]) == marker

    def test_a_settled_screen_reports_nothing(self):
        assert in_flight_text(["Sign in", "Invalid credentials", "Forgot password?"]) is None

    def test_matching_is_case_insensitive_and_substring(self):
        assert in_flight_text(["CREATING YOUR ACCOUNT"]) == "CREATING YOUR ACCOUNT"

    def test_an_empty_screen_reports_nothing(self):
        assert in_flight_text([]) is None


class TestRecordFindingGuards:
    def test_missing_evidence_is_refused(self, file_finding):
        result = file_finding(evidence="")
        assert is_error(result)
        assert "screenshot" in text_of(result).lower()

    def test_nonexistent_evidence_path_is_refused(self, file_finding, tmp_path):
        result = file_finding(evidence=str(tmp_path / "never-captured.jpg"))
        assert is_error(result)

    def test_a_finding_after_an_unread_action_is_refused(self, session, file_finding, evidence):
        # The exact shape of the two real incidents: tap submit, then file a verdict about a
        # screen that has since changed.
        session.last_texts = ["Sign in", "Email is required"]
        session.actions_since_read = 1
        result = file_finding(evidence=evidence)
        assert is_error(result)
        assert "read_screen" in text_of(result)
        assert store.list_findings(PKG, SLUG) == []

    def test_a_finding_while_a_request_is_in_flight_is_refused(self, session, file_finding,
                                                               evidence):
        session.last_texts = ["Sign up", "Creating your account…"]
        session.actions_since_read = 0
        result = file_finding(evidence=evidence)
        assert is_error(result)
        assert "wait_until_gone" in text_of(result)
        assert "Creating your account…" in text_of(result)
        assert store.list_findings(PKG, SLUG) == []

    def test_a_finding_off_a_freshly_read_settled_screen_is_filed(self, session, file_finding,
                                                                  evidence):
        session.last_texts = ["Sign in", "Home", "Welcome back"]
        session.actions_since_read = 0
        result = file_finding(evidence=evidence)
        assert not is_error(result)
        findings = store.list_findings(PKG, SLUG)
        assert len(findings) == 1
        assert findings[0]["title"] == "Empty submit accepted"
        assert findings[0]["evidence"] == evidence

    def test_the_guard_is_about_timing_not_content(self, session, file_finding, evidence):
        # A finding *about* a stuck spinner is legitimate — but only once the agent has
        # waited it out and re-read, which is what clears both counters.
        session.last_texts = ["Creating your account…"]
        assert is_error(file_finding(evidence=evidence))
        session.last_texts = ["Creating your account…", "This is taking longer than usual"]
        session.actions_since_read = 0
        # Still refused while the marker is up: wait_until_gone reports the timeout instead,
        # and that report is what a hung-request finding should be built on.
        assert is_error(file_finding(evidence=evidence))
        session.last_texts = ["Sign up", "Request timed out"]
        assert not is_error(file_finding(evidence=evidence))


class TestBlockReason:
    """The rule as a plain function, independent of the tool wiring."""

    def test_a_settled_freshly_read_screen_permits_a_verdict(self, session):
        session.last_texts = ["Sign in", "Invalid credentials"]
        session.actions_since_read = 0
        assert finding_block_reason(session) is None

    def test_an_unread_action_blocks(self, session):
        session.last_texts = ["Sign in"]
        session.actions_since_read = 2
        assert "read_screen" in (finding_block_reason(session) or "")

    def test_an_unread_action_is_reported_before_the_spinner(self, session):
        # Both wrong at once: the stale read is the more fundamental problem, and re-reading
        # is what would reveal the spinner anyway.
        session.last_texts = ["Creating your account…"]
        session.actions_since_read = 1
        assert "read_screen" in (finding_block_reason(session) or "")


class TestActionCounter:
    """The counter must be driven by the tools themselves, not by the caller remembering."""

    @pytest.fixture
    def fake_device(self, session, monkeypatch):
        """Stand in for a phone: a fixed dump, a fixed size, taps that do nothing."""
        dump = ('<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
                '<node class="android.widget.TextView" package="com.example.app" text="Sign in"'
                ' content-desc="" resource-id="" clickable="false" focusable="false"'
                ' enabled="true" bounds="[100,400][980,470]" />'
                '<node class="android.widget.Button" package="com.example.app" text="Submit"'
                ' content-desc="" resource-id="" clickable="true" focusable="false"'
                ' enabled="true" bounds="[100,960][980,1080]" /></hierarchy>')

        class FakeDevice:
            window_size = (1080, 2400)

            def dump_xml(self):
                return dump

            def click(self, x, y):
                return None

        device = FakeDevice()

        async def get_device():
            return device

        async def run(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(session, "device", get_device)
        monkeypatch.setattr(session, "run", run)
        return device

    @pytest.fixture
    def call_tool(self, session):
        server = build_device_server(session)["instance"]
        handler = server.request_handlers[mcp_types.CallToolRequest]

        def call(name: str, **arguments):
            request = mcp_types.CallToolRequest(
                method="tools/call",
                params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
            )
            return asyncio.run(handler(request))

        return call

    def test_a_fresh_session_starts_settled(self, session):
        assert session.actions_since_read == 0
        assert session.last_texts == []

    def test_a_tap_marks_the_screen_stale(self, session, fake_device, call_tool):
        call_tool("read_screen")
        assert session.actions_since_read == 0
        call_tool("tap_element", id="540_1020", why="submit the form")
        assert session.actions_since_read == 1, "the tap did not mark the read as stale"
        assert finding_block_reason(session) is not None

    def test_reading_the_screen_clears_it_again(self, session, fake_device, call_tool):
        call_tool("read_screen")
        call_tool("tap_element", id="540_1020", why="submit the form")
        call_tool("read_screen")
        assert session.actions_since_read == 0
        assert finding_block_reason(session) is None

    def test_read_screen_captures_the_text_the_guard_reads(self, session, fake_device, call_tool):
        call_tool("read_screen")
        assert "Sign in" in session.last_texts

    def test_a_failed_tap_does_not_mark_the_screen_stale(self, session, fake_device, call_tool):
        call_tool("read_screen")
        result = call_tool("tap_element", id="0_0", why="an id that is not on screen")
        assert is_error(result)
        # Nothing happened to the phone, so nothing about the read is stale — blocking a
        # verdict here would be a false alarm.
        assert session.actions_since_read == 0


class TestFindingScreenLink:
    """Which screen on the board a verdict points at.

    The board outlines the screen a finding was filed against — red for a bug, amber for a
    warning or suggestion. That outline is a claim about one specific screen, so the link
    has to be stated, never inferred.

    The tempting shortcut is the journey's most recently posted node. Read a real transcript
    and it does not hold: the agent files the verdict *before* recording the step that shows
    it about as often as after, so "most recent" lands on the previous test case. A red badge
    on a screen that is fine is the same category of mistake as the dump misreads the
    screenshot rule exists to stop — and it is worse than no badge, because a reader who
    trusts the outline goes and looks at the wrong screen.
    """

    def test_the_named_step_is_what_gets_recorded(self, file_finding, evidence):
        file_finding(evidence=evidence, kind="bug", step="agent-au-014")
        assert store.list_findings(PKG, SLUG)[0]["node"] == "agent-au-014"

    def test_a_finding_with_no_step_is_left_unlinked(self, file_finding, evidence):
        file_finding(evidence=evidence, kind="bug")
        assert store.list_findings(PKG, SLUG)[0]["node"] is None, (
            "an unlinked finding must stay unlinked — the board skips it, which is the "
            "honest outcome")

    def test_the_last_posted_node_is_not_used_as_a_fallback(self, session, file_finding,
                                                            evidence):
        """The shortcut this class exists to rule out."""
        from journey import Journey

        session._journey = Journey.__new__(Journey)
        session._journey._prev_node = "agent-au-003"
        file_finding(evidence=evidence, kind="bug")
        assert store.list_findings(PKG, SLUG)[0]["node"] is None, (
            "record_finding fell back to the journey's last node; that node is whatever step "
            "happened to be posted most recently, which is routinely a different test case")

    def test_a_blank_step_is_treated_as_absent(self, file_finding, evidence):
        file_finding(evidence=evidence, kind="bug", step="   ")
        assert store.list_findings(PKG, SLUG)[0]["node"] is None
