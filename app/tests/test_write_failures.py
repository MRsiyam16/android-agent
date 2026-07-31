"""A failed write is reported as a failure, never as success.

The harness enforces hard rules on what the *agent* may claim: no defect without a
screenshot, no verdict off a stale screen, no approximate link between a finding and a
node ("unlinked is honest; approximately linked is not"). It was much more relaxed about
what it claimed to the agent.

`_write_json_atomic` caught OSError, logged a warning and returned. Nothing checked it. So
a `record_finding` whose write failed still answered "Filed F003", still emitted the
finding to the browser, and still bumped `finding_count` — leaving the pill in the top bar
showing a verdict that `findings.json` did not contain, the model moving on to the next
case, and a `logger.warning` as the only trace. `add_note`, `record_step`, `link_finding`
and `set_secret` all shared the shape, and `Journey._post` did the same for the flow graph:
`journey_step` reported "Recorded step 3 … as node X" for a node the board never received.

Not a hypothetical failure: a project can live on any drive (see project_paths), so an
unplugged external disk or a Windows AV file lock lands exactly here.

These tests hold the honest behaviour in place: the store raises, and every tool that
persists something turns that into an error the agent can act on.
"""
from __future__ import annotations

import asyncio

import pytest

import mcp.types as mcp_types

import project_paths
from agent import store
from agent.device_tools import DeviceSession, build_device_server

PKG, SLUG = "com.example.app", "auth"


@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", root)
    monkeypatch.setattr(store, "PROJECTS_DIR", root)
    store.create_subproject(PKG, "Auth", "")
    return root


@pytest.fixture
def failing_writes(monkeypatch):
    """Every atomic write fails, as it would with the project's drive unplugged."""
    monkeypatch.setattr(store, "_write_json_atomic", lambda *_a, **_k: False)


@pytest.fixture
def evidence(tmp_path):
    path = tmp_path / "shot.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg")
    return str(path)


@pytest.fixture
def session():
    s = DeviceSession(PKG, SLUG)
    # A freshly read, settled screen: the finding guards are not what is under test here.
    s.last_texts = ["Sign in", "Welcome back"]
    s.actions_since_read = 0
    return s


@pytest.fixture
def call_tool(session):
    """Drive tools through the MCP server, the same path the agent takes."""
    server = build_device_server(session)["instance"]
    handler = server.request_handlers[mcp_types.CallToolRequest]

    def call(name, **args):
        request = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=args),
        )
        return asyncio.run(handler(request))

    return call


def text_of(result) -> str:
    return result.root.content[0].text


def is_error(result) -> bool:
    return bool(result.root.isError)


class TestStoreRaises:
    def test_a_finding_that_cannot_be_written_raises(self, failing_writes):
        with pytest.raises(store.StoreWriteError):
            store.add_finding(PKG, SLUG, {"title": "x", "kind": "bug"})

    def test_a_note_that_cannot_be_written_raises(self, failing_writes):
        with pytest.raises(store.StoreWriteError):
            store.add_note(PKG, SLUG, {"section": SLUG, "kind": "pass", "text": "x"})

    def test_a_step_that_cannot_be_written_raises(self, failing_writes):
        with pytest.raises(store.StoreWriteError):
            store.record_step(PKG, SLUG, "n1", "Open login", SLUG)

    def test_a_credential_that_cannot_be_written_raises(self, failing_writes):
        # Loud specifically because the value is kept nowhere else: the transcript now
        # stores a redaction, so a swallowed failure loses the credential outright.
        with pytest.raises(store.StoreWriteError):
            store.set_secret(PKG, "test_password", "hunter2")

    def test_the_finding_count_is_not_bumped_when_the_write_failed(self, failing_writes):
        # The count lives in a different file. Bumping it first is how the top bar ends up
        # advertising a verdict that findings.json does not contain.
        with pytest.raises(store.StoreWriteError):
            store.add_finding(PKG, SLUG, {"title": "x", "kind": "bug"})
        assert (store.get_subproject(PKG, SLUG) or {}).get("finding_count", 0) == 0

    def test_a_successful_write_still_returns_the_record(self):
        record = store.add_finding(PKG, SLUG, {"title": "Empty submit rejected", "kind": "pass"})
        assert record["id"] == "F001"
        assert len(store.list_findings(PKG, SLUG)) == 1


class TestToolsReportTheFailure:
    def test_record_finding_does_not_claim_to_have_filed(self, call_tool, evidence,
                                                         failing_writes):
        result = call_tool("record_finding", title="Empty submit accepted",
                           expected="rejected", actual="accepted", evidence=evidence)
        assert is_error(result)
        assert "NOT saved" in text_of(result)
        # The word that used to be here regardless of what happened on disk.
        assert "Filed F001" not in text_of(result)
        assert store.list_findings(PKG, SLUG) == []

    def test_the_agent_is_told_a_retry_is_safe(self, call_tool, evidence, failing_writes):
        # It matters that this is retryable: the write is all-or-nothing, so nothing was
        # half-filed and no id was consumed. An agent told only "failed" may reasonably
        # decide not to risk a double-file and drop the verdict instead.
        assert "again" in text_of(call_tool(
            "record_finding", title="x", expected="a", actual="b", evidence=evidence)).lower()

    def test_add_note_does_not_claim_to_have_pinned(self, call_tool, session, failing_writes):
        session._section = SLUG
        result = call_tool("add_note", text="Looks right", kind="pass", section=SLUG)
        assert is_error(result)
        assert "NOT saved" in text_of(result)

    def test_link_finding_does_not_claim_to_have_linked(self, call_tool, evidence,
                                                        monkeypatch):
        # Filed while writes work, linked after they stop — the finding exists, the link
        # is what fails.
        call_tool("record_finding", title="Broken", expected="a", actual="b",
                  evidence=evidence)
        monkeypatch.setattr(store, "_write_json_atomic", lambda *_a, **_k: False)
        result = call_tool("link_finding", id="F001", step="agent-au-001")
        assert is_error(result)
        assert "NOT saved" in text_of(result)
        assert store.list_findings(PKG, SLUG)[0].get("node") is None

    def test_a_working_store_still_files_normally(self, call_tool, evidence):
        result = call_tool("record_finding", title="Empty submit rejected",
                           expected="rejected", actual="rejected", evidence=evidence,
                           kind="pass")
        assert not is_error(result)
        assert "Filed F001" in text_of(result)


class TestJourneyPostFailures:
    def test_a_refused_connection_is_reported_as_not_posted(self, monkeypatch):
        import journey as journey_mod

        def boom(*_a, **_k):
            raise journey_mod.requests.RequestException("connection refused")

        monkeypatch.setattr(journey_mod.requests, "post", boom)
        j = journey_mod.Journey.__new__(journey_mod.Journey)
        j.server = "http://localhost:8000"
        assert j._post("/telemetry", {}) is False

    def test_a_rejected_payload_is_reported_as_not_posted(self, monkeypatch):
        # A 422 drops the step just as completely as a refused connection, and used to be
        # indistinguishable from success — the run is posting to its own server, so a
        # schema mismatch is a realistic way for a step to vanish.
        import journey as journey_mod

        class Response:
            status_code = 422
            text = "unprocessable"

        monkeypatch.setattr(journey_mod.requests, "post", lambda *_a, **_k: Response())
        j = journey_mod.Journey.__new__(journey_mod.Journey)
        j.server = "http://localhost:8000"
        assert j._post("/telemetry", {}) is False

    def test_a_2xx_is_reported_as_posted(self, monkeypatch):
        import journey as journey_mod

        class Response:
            status_code = 200
            text = "{}"

        monkeypatch.setattr(journey_mod.requests, "post", lambda *_a, **_k: Response())
        j = journey_mod.Journey.__new__(journey_mod.Journey)
        j.server = "http://localhost:8000"
        assert j._post("/telemetry", {}) is True


class TestJourneyStepTellsTheTruth:
    @pytest.fixture
    def stub_journey(self, session, monkeypatch):
        """A Journey that records steps without a device or a server."""
        class Stub:
            def __init__(self):
                self.step_count = 0
                self.section = None
                self._prev_node = None
                self.last_step_posted = True

            def start_section(self, title):
                self.section = title

            def step(self, label, _action=None, _xml=None):
                self.step_count += 1
                return f"stub-{self.step_count:03d}"

        stub = Stub()
        monkeypatch.setattr(session, "journey", lambda: stub)
        monkeypatch.setattr(session, "device", lambda: asyncio.sleep(0))
        return stub

    def test_a_posted_step_reads_as_recorded(self, call_tool, stub_journey):
        result = call_tool("journey_step", label="Open login", case="empty submit")
        assert not is_error(result)
        assert "Recorded step 1" in text_of(result)
        assert "WARNING" not in text_of(result)

    def test_a_step_the_board_never_received_says_so(self, call_tool, stub_journey):
        stub_journey.last_step_posted = False
        text = text_of(call_tool("journey_step", label="Open login", case="empty submit"))
        assert "WARNING" in text
        assert "did NOT receive" in text
        # Not an error, and explicitly non-retryable: every call mints a fresh node id, so
        # re-stepping the same screen would duplicate it on the board rather than repair it.
        assert "Do not call journey_step again" in text

    def test_the_node_id_is_still_usable_after_a_failed_post(self, call_tool, stub_journey):
        # The local index did land, so list_steps and link_finding still work on it —
        # the only false claim would be that the board has it.
        stub_journey.last_step_posted = False
        assert "stub-001" in text_of(call_tool("journey_step", label="Open login"))
        assert [s["node"] for s in store.list_steps(PKG, SLUG)] == ["stub-001"]
