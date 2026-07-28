"""The Stop button actually stopping.

`ClaudeSDKClient.interrupt()` ends the turn only once the tool in flight returns, and the
tools worth interrupting are precisely the slow ones — `wait_for_ui` can hold a turn for two
minutes on a cold Flutter start, `wait_for_text` and `wait_until_gone` for their full timeout.
Pressing Stop and then watching the agent keep going for another minute is indistinguishable
from a button that does nothing, which is how it was reported.

So Stop has two halves, and this file pins both: a flag the long waits poll, and the guarantee
that raising it does not poison the next turn.
"""
from __future__ import annotations

import threading
import time

import pytest

import project_paths
from adb_device import AdbDevice
from agent import store
from agent.device_tools import DeviceSession

PKG, SLUG = "com.example.app", "auth"


@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", root)
    monkeypatch.setattr(store, "PROJECTS_DIR", root)
    store.create_subproject(PKG, "Auth", "")


@pytest.fixture
def session():
    return DeviceSession(PKG, SLUG)


class TestCancelFlag:
    def test_a_fresh_session_is_not_cancelled(self, session):
        assert session.cancelled is False

    def test_cancel_raises_it_and_resume_clears_it(self, session):
        session.cancel()
        assert session.cancelled is True
        # Without this, one press of Stop would make every device tool in every *later* turn
        # return "stopped" immediately, and the agent would look permanently broken.
        session.resume()
        assert session.cancelled is False

    def test_a_pending_question_is_released_so_the_run_does_not_hang(self, session):
        import asyncio

        async def scenario():
            session._answer = asyncio.get_running_loop().create_future()
            session.pending_question = {"question": "which account?"}
            session.cancel()
            # A run parked on `ask_user` has nobody left to answer it after a Stop; leaving the
            # future pending would hold the turn open forever.
            assert session._answer.cancelled()

        asyncio.run(scenario())

    def test_the_sleep_helper_returns_early_instead_of_waiting_it_out(self, session):
        # Every second spent in a settle sleep is a second Stop cannot reclaim.
        threading.Timer(0.05, session.cancel).start()
        started = time.monotonic()
        cancelled = session.wait_cancellable(5.0)
        elapsed = time.monotonic() - started
        assert cancelled is True
        assert elapsed < 1.0, f"waited {elapsed:.2f}s for a cancel raised after 0.05s"

    def test_the_sleep_helper_still_sleeps_when_nothing_cancels(self, session):
        started = time.monotonic()
        assert session.wait_cancellable(0.15) is False
        assert time.monotonic() - started >= 0.14


class TestWaitForUiIsInterruptible:
    """`wait_for_ui` is the single longest block the agent can be inside."""

    def _device(self, monkeypatch):
        device = AdbDevice.__new__(AdbDevice)   # no phone, no uiautomator2 handshake
        device.serial = "test"
        # Never satisfies the readiness test, so the loop runs to its deadline unless cancelled.
        monkeypatch.setattr(device, "dump_xml", lambda: "", raising=False)
        return device

    def test_it_runs_to_the_deadline_when_nothing_cancels(self, monkeypatch):
        device = self._device(monkeypatch)
        started = time.monotonic()
        _, waited = device.wait_for_ui(PKG, timeout=0.4, poll=0.05)
        assert time.monotonic() - started >= 0.35
        assert waited >= 0.35

    def test_it_returns_promptly_when_cancelled(self, monkeypatch):
        device = self._device(monkeypatch)
        cancelled = threading.Event()
        threading.Timer(0.1, cancelled.set).start()
        started = time.monotonic()
        # A 30s budget that has to give up in a fraction of a second — this is the difference
        # between Stop working and Stop appearing to do nothing.
        device.wait_for_ui(PKG, timeout=30, poll=0.05, cancelled=cancelled)
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"took {elapsed:.2f}s to honour a cancel raised after 0.1s"

    def test_an_interrupted_wait_does_not_teach_system_memory_a_bogus_settle_time(
            self, monkeypatch):
        # The elapsed time of a cancelled wait says nothing about how long the app takes to
        # settle. Recording it would drag the learned budget toward whenever the user happened
        # to press Stop, and that budget is what later runs trust.
        import system_memory as sysmem

        observed = []
        monkeypatch.setattr(sysmem, "observe_launch",
                            lambda toolkit, secs: observed.append(secs))
        device = self._device(monkeypatch)
        cancelled = threading.Event()
        cancelled.set()
        device.wait_for_ui(PKG, timeout=30, poll=0.05, cancelled=cancelled)
        assert observed == []

    def test_the_signature_stays_compatible_with_callers_that_pass_no_flag(self, monkeypatch):
        # run_agent.py calls this positionally with no cancel flag.
        device = self._device(monkeypatch)
        xml, waited = device.wait_for_ui(PKG, timeout=0.2, poll=0.05)
        assert xml == ""
        assert isinstance(waited, float)
