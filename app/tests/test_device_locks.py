"""One driver per target — the rule that makes concurrent runs safe rather than plausible.

The failure this prevents is not a crash. Two sessions on one phone each build their own
`Device`, connect happily, and interleave taps on one screen; every finding either files is a
verdict about a screen the other agent had just changed. Confident, evidenced, wrong, and
filed under two different modules.

The other half matters as much: an iPad suite and a website suite must *not* block each other.
A lock that serialises everything would be safe and useless.
"""
from __future__ import annotations

import asyncio

import pytest

import device_locks
import project_paths
from agent import store
from backend import projects as backend_projects
from backend.app import create_app


@pytest.fixture(autouse=True)
def clean_locks():
    device_locks.reset()
    yield
    device_locks.reset()


# -- keys ---------------------------------------------------------------------------------
class TestWhatCounts:
    def test_two_projects_on_one_phone_share_a_key(self):
        """The serial is the target, not the project. Two suites on one phone conflict."""
        assert (device_locks.key_for("android", "R5CT30", "com.a")
                == device_locks.key_for("android", "R5CT30", "com.b"))

    def test_one_project_on_two_phones_does_not(self):
        assert (device_locks.key_for("ios", "udid-ipad", "app")
                != device_locks.key_for("ios", "udid-iphone", "app"))

    def test_an_ipad_and_a_website_never_collide(self):
        """The case this whole module exists to permit."""
        assert (device_locks.key_for("ios", "udid-ipad", "ipad Test")
                != device_locks.key_for("web", None, "https://example.com"))

    def test_web_keys_on_the_site_because_that_is_what_its_runs_share(self):
        """Browsers are cheap; one account's session state is not. Two suites signed into the
        same site would log each other out and file the result as a defect."""
        assert (device_locks.key_for("web", None, "https://example.com")
                == device_locks.key_for("web", "ignored", "https://example.com"))
        assert device_locks.key_for("web", None, "https://a.com").startswith("web:")

    def test_an_unknown_serial_over_locks_rather_than_under_locks(self):
        """Safe direction: a project with no identified device contends only with itself,
        instead of two runs landing on a phone nobody established the identity of."""
        key = device_locks.key_for("android", None, "com.a")
        assert key != device_locks.key_for("android", None, "com.b")
        assert "com.a" in key


# -- exclusion ----------------------------------------------------------------------------
class TestExclusion:
    def test_a_second_module_on_the_same_device_is_refused(self):
        device_locks.acquire("R5CT30", "com.a", "search")
        with pytest.raises(device_locks.DeviceBusy) as caught:
            device_locks.acquire("R5CT30", "com.a", "checkout")
        # Naming the holder is the difference between a refusal you can act on and a mystery.
        assert "com.a / search" in str(caught.value)
        assert caught.value.holder["slug"] == "search"

    def test_releasing_hands_the_device_on(self):
        device_locks.acquire("R5CT30", "com.a", "search")
        device_locks.release("R5CT30", "com.a", "search")
        device_locks.acquire("R5CT30", "com.b", "login")     # no raise
        assert device_locks.holder("R5CT30")["package"] == "com.b"

    def test_different_targets_run_at_once(self):
        device_locks.acquire("udid-ipad", "ipad Test", "calendar")
        device_locks.acquire("web:https://clinic.example", "https://clinic.example", "booking")
        assert len(device_locks.held()) == 2

    def test_the_same_module_can_take_its_own_device_twice(self):
        """Re-entrant, so a session that already holds its target does not deadlock itself."""
        device_locks.acquire("R5CT30", "com.a", "search")
        device_locks.acquire("R5CT30", "com.a", "search")
        device_locks.release("R5CT30", "com.a", "search")
        assert device_locks.holder("R5CT30") is not None    # still held by the outer take
        device_locks.release("R5CT30", "com.a", "search")
        assert device_locks.holder("R5CT30") is None

    def test_releasing_something_you_do_not_hold_is_not_an_error(self):
        """This runs in `finally`, where raising would mask whatever actually went wrong."""
        device_locks.acquire("R5CT30", "com.a", "search")
        device_locks.release("R5CT30", "com.b", "other")     # no raise
        assert device_locks.holder("R5CT30")["package"] == "com.a"

    def test_closing_a_session_drops_everything_it_held(self):
        device_locks.acquire("R5CT30", "com.a", "search")
        device_locks.acquire("web:site", "com.a", "search")
        device_locks.release_all("com.a", "search")
        assert device_locks.held() == {}


# -- the turn -----------------------------------------------------------------------------
class TestTheTurn:
    """Held for the length of a turn, not the length of a session — several modules of one app
    are legitimately open at once; you just cannot run two of them together."""

    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
        backend_projects.write_meta("com.a", platform="android")
        store.create_subproject("com.a", "Search")
        store.create_subproject("com.a", "Checkout")
        return tmp_path

    def _session(self, slug, serial="R5CT30"):
        from agent.runtime import AgentSession

        async def noop(_e):
            pass

        session = AgentSession("com.a", slug, noop, serial=serial, platform="android")
        return session

    def test_a_busy_device_refuses_the_turn_with_a_message_naming_the_holder(self, project):
        events = []

        async def collect(event):
            events.append(event)

        from agent.runtime import AgentSession
        second = AgentSession("com.a", "checkout", collect, serial="R5CT30",
                              platform="android")
        device_locks.acquire("R5CT30", "com.a", "search")

        asyncio.run(second.send("go"))
        assert [e["type"] for e in events] == ["agent_error"]
        assert "com.a / search" in events[0]["message"]

    def test_a_refused_turn_leaves_nothing_in_the_transcript(self, project):
        """Checked before the message is recorded: a user line implying a run happened, on a
        turn that never started, is a transcript that lies about what was tried."""
        from agent.runtime import AgentSession

        async def noop(_e):
            pass

        device_locks.acquire("R5CT30", "com.a", "search")
        asyncio.run(AgentSession("com.a", "checkout", noop, serial="R5CT30",
                                 platform="android").send("go"))
        assert store.read_chat("com.a", "checkout") == []

    def test_the_ecosystem_manager_is_not_subject_to_a_device_lock(self, tmp_path, monkeypatch):
        """It has no device. Blocking it because a phone is busy would apply a rule about
        hardware to the one tier that cannot touch any."""
        import ecosystem
        monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
        backend_projects.write_meta("com.a", platform="android")
        ecosystem.tag("com.a", "product", "app")
        ecosystem.create_supervisor("product")
        store.create_subproject("product", "Main")

        from agent.runtime import AgentSession

        async def noop(_e):
            pass

        session = AgentSession("product", "main", noop)
        # Every plausible target already taken by someone else.
        device_locks.acquire("unknown-device:product", "com.a", "search")
        device_locks.acquire("web:product", "com.a", "search")

        # It gets past the lock check; the CLI connection is what fails in a test process,
        # and that failure arrives as an agent_error *after* a user line was recorded — which
        # a lock refusal never does.
        asyncio.run(session.send("where does the product stand"))
        assert store.read_chat("product", "main") != []


# -- which device a project runs on --------------------------------------------------------
class TestDeviceRouting:
    """Every session used to inherit the serial of whatever device last posted telemetry.

    With one phone that is right by accident. With an iPad and an iPhone both in play it is
    wrong half the time — same adapter, identically-shaped UDIDs — so a run on the iPad suite
    would quietly drive the iPhone and file the results under `doctor-ipad`.
    """

    @pytest.fixture
    def projects_on_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
        backend_projects.write_meta("ipad Test", platform="ios")
        backend_projects.write_meta("iphone Test", platform="ios")
        backend_projects.write_meta("com.droid", platform="android")
        backend_projects.write_meta("https://site.example", platform="web")
        return tmp_path

    @pytest.fixture
    def attached(self, monkeypatch):
        """Two iOS devices and one Android, as if all three were plugged in."""
        from backend import agent_bridge

        devices = [{"serial": "udid-ipad", "platform": "ios"},
                   {"serial": "udid-iphone", "platform": "ios"},
                   {"serial": "R5CT30", "platform": "android"}]
        monkeypatch.setattr(agent_bridge, "attached", lambda: devices)
        return devices

    def _device_for(self, package):
        from backend.agent_bridge import device_for
        return device_for(package)

    def test_a_pinned_project_always_gets_its_own_device(self, projects_on_disk, attached,
                                                         monkeypatch):
        from backend import state
        monkeypatch.setattr(state, "device_serial", lambda: "udid-iphone")
        backend_projects.write_meta("ipad Test", device_serial="udid-ipad")
        assert self._device_for("ipad Test") == ("udid-ipad", "ios")

    def test_the_live_serial_is_never_borrowed_across_platforms(self, projects_on_disk,
                                                               attached, monkeypatch):
        """An Android phone posting telemetry must not become the iOS project's device."""
        from backend import state
        monkeypatch.setattr(state, "device_serial", lambda: "R5CT30")
        serial, platform = self._device_for("ipad Test")
        assert platform == "ios"
        assert serial != "R5CT30"

    def test_with_one_device_of_the_right_kind_it_is_chosen(self, projects_on_disk, monkeypatch):
        from backend import agent_bridge, state
        monkeypatch.setattr(agent_bridge, "attached",
                            lambda: [{"serial": "udid-ipad", "platform": "ios"}])
        monkeypatch.setattr(state, "device_serial", lambda: None)
        assert self._device_for("ipad Test") == ("udid-ipad", "ios")

    def test_two_of_the_same_kind_and_no_pin_picks_neither(self, projects_on_disk, attached,
                                                           monkeypatch):
        """Being confidently wrong about which iPad you meant is worse than asking."""
        from backend import state
        monkeypatch.setattr(state, "device_serial", lambda: None)
        assert self._device_for("ipad Test") == (None, "ios")

    def test_a_web_project_has_no_device_at_all(self, projects_on_disk, attached, monkeypatch):
        from backend import state
        monkeypatch.setattr(state, "device_serial", lambda: "udid-ipad")
        assert self._device_for("https://site.example") == (None, "web")

    def test_pinning_round_trips_through_the_route(self, projects_on_disk, attached):
        from fastapi.testclient import TestClient
        client = TestClient(create_app())

        client.post("/projects/ipad Test/device", json={"device_serial": "udid-ipad"})
        assert self._device_for("ipad Test")[0] == "udid-ipad"

        rows = {d["serial"]: d for d in client.get("/devices").json()}
        assert rows["udid-ipad"]["pinned_to"] == ["ipad Test"]

        client.post("/projects/ipad Test/device", json={})
        assert backend_projects.read_meta("ipad Test").get("device_serial") is None


# -- the status endpoint ------------------------------------------------------------------
def test_status_reports_who_is_holding_what(tmp_path, monkeypatch):
    """Concurrent runs are the normal case now, so "why will this one not start" needs an
    answer that is visible rather than inferred from a failed turn."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    device_locks.acquire("udid-ipad", "ipad Test", "calendar")
    body = TestClient(create_app()).get("/agent/status").json()
    assert body["device_locks"]["udid-ipad"]["package"] == "ipad Test"
