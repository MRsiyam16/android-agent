"""What a session writes down, and what it stops claiming once it is no longer true.

Two separate promises the runtime was breaking.

**Credentials.** `use_credential` tells the user, in the prompt it puts on screen, that the
value "will never be written into the transcript or the report". `store.secret_keys` says
values "are injected into a tool call server-side and never written into the prompt, the
transcript, or the report". The secrets endpoint repeats it. But a reply to a blocked
question was appended to `chat.jsonl` verbatim — so the password went to disk in plain
text and was replayed into the chat log by `GET /chat` every time the module was reopened.
`secrets.json` is singled out as gitignored precisely because it holds these; `chat.jsonl`
carries no such protection and was holding them too.

**Parked.** `parked_reason` was set when the subscription window ran out and never cleared.
It is what `/agent/status` and `GET /chat` report and what the browser renders as a sticky
"parked" badge, so a module that hit the window once claimed to be parked for the life of
the process — including after later runs that finished perfectly.
"""
from __future__ import annotations

import asyncio

import pytest

import project_paths
from agent import store
from agent.runtime import AgentSession, _transcript_text

PKG, SLUG = "com.example.app", "auth"


@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", root)
    monkeypatch.setattr(store, "PROJECTS_DIR", root)
    store.create_subproject(PKG, "Auth", "")
    return root


class TestTranscriptRedaction:
    def test_a_credential_answer_is_not_written_down(self):
        question = {"kind": "credential", "question": "I need a password",
                    "payload": {"name": "test_password"}}
        written = _transcript_text(question, "hunter2")
        assert "hunter2" not in written
        assert "test_password" in written   # which one was given is still worth knowing

    def test_a_credential_with_no_name_is_still_redacted(self):
        written = _transcript_text({"kind": "credential", "payload": {}}, "hunter2")
        assert "hunter2" not in written

    def test_an_approval_is_stored_as_typed(self):
        # Only credentials are secret. An approval or a "done" is the conversation, and
        # redacting it would make the transcript unreadable.
        question = {"kind": "approval", "payload": {}}
        assert _transcript_text(question, "Approved — go ahead.") == "Approved — go ahead."

    def test_an_ordinary_question_is_stored_as_typed(self):
        question = {"kind": "question", "payload": {}}
        assert _transcript_text(question, "Done, I unlocked it") == "Done, I unlocked it"

    def test_a_message_with_no_pending_question_is_stored_as_typed(self):
        assert _transcript_text(None, "test the login module") == "test the login module"


class TestCredentialNeverReachesTheTranscript:
    """The end-to-end version: drive `send` the way a reply to a blocked question does."""

    @pytest.fixture
    def session(self):
        async def emit(_event):
            return None

        return AgentSession(PKG, SLUG, emit)

    def test_answering_a_credential_prompt_leaves_no_password_on_disk(self, session,
                                                                     isolated_projects_dir):
        session.device.pending_question = {"kind": "credential", "question": "password?",
                                           "payload": {"name": "test_password"}}
        loop = asyncio.new_event_loop()
        try:
            # The future the parked `ask()` is waiting on, resolved by `answer()`.
            session.device._answer = loop.create_future()
            loop.run_until_complete(session.send("hunter2"))
        finally:
            loop.close()

        messages = store.read_chat(PKG, SLUG)
        assert messages, "the reply should still be recorded, just not verbatim"
        assert all("hunter2" not in (m.get("text") or "") for m in messages)

        # And not anywhere else in the module's files either.
        for path in (isolated_projects_dir / store._safe_name(PKG)).rglob("*"):
            if path.is_file() and path.suffix in (".jsonl", ".json", ".md"):
                assert "hunter2" not in path.read_text(encoding="utf-8"), path


class TestParkedReasonIsNotSticky:
    @pytest.fixture
    def session(self, monkeypatch):
        async def emit(_event):
            return None

        sess = AgentSession(PKG, SLUG, emit)

        class Client:
            async def query(self, _text):
                return None

        async def connect():
            sess._client = Client()

        async def pump():
            return None

        monkeypatch.setattr(sess, "connect", connect)
        monkeypatch.setattr(sess, "_pump", pump)
        return sess

    def test_a_new_turn_clears_a_previous_rate_limit_park(self, session):
        session.parked_reason = "rate_limit"
        asyncio.run(session.send("continue"))
        assert session.parked_reason is None

    def test_the_turn_still_runs_and_is_transcribed(self, session):
        asyncio.run(session.send("test the login module"))
        assert session.busy is False
        assert [m.get("text") for m in store.read_chat(PKG, SLUG)] == ["test the login module"]


class TestModuleStatusVocabulary:
    def test_the_status_written_on_completion_is_one_the_frontend_knows(self):
        # runtime writes "tested" at the end of a successful turn; the comment beside the
        # field used to document `running | done`, neither of which anything ever wrote.
        # css/agent.css styles one badge per status, and these three are what it has.
        assert "tested" in store.SUBPROJECT_STATUSES
        assert "proposed" in store.SUBPROJECT_STATUSES
        assert "approved" in store.SUBPROJECT_STATUSES

    def test_the_default_status_of_a_new_module_is_in_the_vocabulary(self):
        entry = store.create_subproject(PKG, "Checkout", "cart to receipt")
        assert entry["status"] in store.SUBPROJECT_STATUSES

    def test_no_dead_values_remain(self):
        # If a status is ever added, it needs a badge rule in css/agent.css to render.
        assert set(store.SUBPROJECT_STATUSES) == {"proposed", "approved", "tested"}
