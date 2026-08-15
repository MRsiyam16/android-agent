"""The master agent: bringing hardware up, running several apps at once, and moving files.

Three things are being protected here, and they are the three ways this tier can do damage
that the tiers below it cannot.

**Starting a run on a stack that is not up.** This is the failure that does not look like a
failure. A busy target refuses in a sentence; a missing WebDriverAgent refuses nothing, and
the module spends ten minutes reasoning about a broken app that was simply never reachable.
So `run_module` checks first, and the refusal names the fix.

**Driving the wrong device.** An iPad and an iPhone are both `ios` with identically-shaped
UDIDs. `start_app` pins automatically when there is one candidate and refuses outright when
there are two — guessing which one "doctor-ipad" meant is exactly the mistake pinning exists
to prevent, and a wrong guess files the iPhone's defects against the iPad.

**Losing files.** Nothing here deletes and nothing overwrites. Both are enforced in
`manager_fs`, not in the prompt, because a rule an agent can be talked out of is not a rule.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import ecosystem
import manager_fs
import project_paths
import stacks
from agent import ecosystem_tools, prompts, store
from agent.device_tools import DeviceSession
from backend import agent_bridge
from backend import projects as backend_projects

NAME = "metaesthetics"
IPAD = "ipad Test"
WEB = "clinic.example.com"
ANDROID = "com.patient.android"

IPAD_UDID = "00008027-000A15C40C1B002E"
IPHONE_UDID = "00008101-001C25D93C22001E"


def call(name: str, args: dict) -> str:
    import mcp.types as mcp_types

    instance = ecosystem_tools.build_ecosystem_server(DeviceSession(NAME, "main"),
                                                      NAME)["instance"]
    handler = instance.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=args))
    return asyncio.run(handler(request)).root.content[0].text


@pytest.fixture
def eco(tmp_path, monkeypatch):
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(agent_bridge, "attached", lambda: [])
    for package, role, platform in ((IPAD, "doctor-ipad", "ios"),
                                    (WEB, "clinic-web", "web"),
                                    (ANDROID, "patient-android", "android")):
        backend_projects.write_meta(package, platform=platform)
        ecosystem.tag(package, NAME, role)
        store.create_subproject(package, "Search")
    ecosystem.create_supervisor(NAME)
    store.create_subproject(NAME, "Main")
    return tmp_path


@pytest.fixture
def ready_stacks(monkeypatch):
    monkeypatch.setattr(stacks, "status", lambda platform: {
        "platform": platform, "ready": True, "detail": "stubbed ready", "fix": "",
        "devices": [], "starting": False})


def attach(monkeypatch, *devices):
    monkeypatch.setattr(agent_bridge, "attached", lambda: list(devices))


def ios(serial, model="iPad Pro"):
    return {"serial": serial, "platform": "ios", "model": model,
            "label": f"{model} (iOS 17.4)"}


# -- stacks -----------------------------------------------------------------------------------
class TestStacks:
    def test_an_unknown_platform_is_a_status_not_a_crash(self):
        """Everything in this module is called from a chat tool. A raised exception reaches
        the agent as a tool error with no next step in it."""
        row = stacks.status("blackberry")
        assert row["ready"] is False
        assert "android" in row["fix"]

    def test_the_web_stack_says_there_is_nothing_to_start(self, monkeypatch):
        """The honest answer, and the one most likely to be papered over. Reporting a web
        stack as 'started' would make a machine-setup problem look like progress."""
        monkeypatch.setattr(stacks, "_web_status", lambda: stacks._row(
            "web", False, "Playwright is not installed", fix="pip install playwright"))
        out = stacks.start("web")
        assert out["started"] is False
        assert "nothing to start" in out["note"].lower()
        assert "pip install playwright" in out["fix"]

    def test_a_ready_platform_is_not_started_again(self, monkeypatch):
        monkeypatch.setattr(stacks, "status", lambda p: stacks._row(p, True, "up"))
        out = stacks.start("ios")
        assert out["started"] is False
        assert "Already up" in out["note"]

    def test_the_ios_stack_is_not_started_when_no_device_is_attached(self, monkeypatch):
        monkeypatch.setattr(stacks, "status", lambda p: stacks._row(p, False, "nothing there"))
        spawned = []
        monkeypatch.setattr(stacks, "_spawn_ios_stack", lambda udid: spawned.append(udid))
        out = stacks.start("ios")
        assert out["started"] is False
        assert spawned == []

    def test_a_second_start_does_not_stack_a_second_uac_prompt(self, monkeypatch):
        """The tunnel takes up to 45 seconds to bind. An impatient second call would ask the
        user to approve Administrator again, for a process that then fails to bind the port."""
        monkeypatch.setattr(stacks, "status", lambda p: stacks._row(
            p, False, "not up", devices=[ios(IPAD_UDID)]))
        spawned = []
        monkeypatch.setattr(stacks, "_spawn_ios_stack", lambda udid: spawned.append(udid))
        stacks._ios_starting.clear()

        first = stacks.start("ios", IPAD_UDID)
        second = stacks.start("ios", IPAD_UDID)
        assert first["started"] is True
        assert second["started"] is False
        assert "still coming up" in second["note"]
        assert spawned == [IPAD_UDID]
        stacks._ios_starting.clear()


# -- files ------------------------------------------------------------------------------------
class TestManagerFs:
    @pytest.fixture(autouse=True)
    def sandbox(self, tmp_path, monkeypatch):
        monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path / "projects")
        monkeypatch.setattr(manager_fs, "HARNESS_ROOT", tmp_path / "work")
        monkeypatch.setattr(manager_fs, "APP_DIR", tmp_path / "work" / "app")
        (tmp_path / "work" / "app").mkdir(parents=True)
        (tmp_path / "projects").mkdir(parents=True)
        return tmp_path

    def test_a_path_outside_the_roots_is_refused_and_the_refusal_names_them(self, sandbox):
        outside = sandbox / "elsewhere"
        outside.mkdir()
        with pytest.raises(manager_fs.FsRefused) as exc:
            manager_fs.resolve(str(outside))
        assert "outside the folders" in str(exc.value)
        assert str(sandbox / "work") in str(exc.value)

    def test_dot_dot_cannot_walk_out_of_a_root(self, sandbox):
        """Resolved before the check, so the string form of the path is irrelevant."""
        with pytest.raises(manager_fs.FsRefused):
            manager_fs.resolve(str(sandbox / "work" / ".." / "elsewhere"), must_exist=False)

    def test_the_running_source_tree_cannot_be_moved(self, sandbox):
        """Not paternalism: this code is executing out of that folder, and an agent that moves
        it takes away the session it would need to undo the move."""
        with pytest.raises(manager_fs.FsRefused) as exc:
            manager_fs.move(str(sandbox / "work" / "app"), str(sandbox / "work" / "old"))
        assert "running this call" in str(exc.value)

    def test_a_folder_cannot_be_moved_into_itself(self, sandbox):
        src = sandbox / "work" / "suite"
        src.mkdir()
        with pytest.raises(manager_fs.FsRefused) as exc:
            manager_fs.move(str(src), str(src / "nested"))
        assert "into itself" in str(exc.value)

    def test_nothing_is_ever_overwritten(self, sandbox):
        src = sandbox / "work" / "a.txt"
        src.write_text("new")
        dest = sandbox / "work" / "b.txt"
        dest.write_text("old and irreplaceable")
        with pytest.raises(manager_fs.FsRefused):
            manager_fs.move(str(src), str(dest))
        assert dest.read_text() == "old and irreplaceable"
        assert src.exists()

    def test_moving_onto_an_existing_folder_means_into_it(self, sandbox):
        src = sandbox / "work" / "notes.txt"
        src.write_text("x")
        into = sandbox / "work" / "archive"
        into.mkdir()
        out = manager_fs.move(str(src), str(into))
        assert out["to"] == str(into / "notes.txt")
        assert (into / "notes.txt").exists()

    def test_trash_moves_rather_than_deletes(self, sandbox):
        doomed = sandbox / "work" / "old-suite"
        doomed.mkdir()
        (doomed / "findings.json").write_text("[]")
        out = manager_fs.trash(str(doomed))
        assert not doomed.exists()
        assert Path(out["to"], "findings.json").read_text() == "[]"
        assert manager_fs.trash_dir() in Path(out["to"]).parents

    def test_copying_a_folder_leaves_the_original(self, sandbox):
        src = sandbox / "work" / "shots"
        src.mkdir()
        (src / "one.jpg").write_bytes(b"x")
        manager_fs.copy(str(src), str(sandbox / "work" / "shots-copy"))
        assert (src / "one.jpg").exists()
        assert (sandbox / "work" / "shots-copy" / "one.jpg").exists()

    def test_make_dir_is_idempotent(self, sandbox):
        target = sandbox / "work" / "reports" / "2026"
        assert manager_fs.make_dir(str(target))["created"] == "yes"
        assert manager_fs.make_dir(str(target))["created"] == "already existed"


# -- the tools ----------------------------------------------------------------------------------
class TestHardwareTools:
    def test_a_run_is_refused_when_the_platform_stack_is_down(self, eco, monkeypatch):
        """The failure this whole tool exists to prevent. Refused up front with the fix in the
        message, rather than started and left to time out one device tool at a time."""
        monkeypatch.setattr(stacks, "status", lambda p: stacks._row(
            p, False, "WebDriverAgent is not answering.", fix="Start the stack."))
        monkeypatch.setattr(agent_bridge, "start_run",
                            lambda *a, **k: pytest.fail("must not start on a dead stack"))
        out = call("run_module", {"app": "doctor-ipad", "module": "search",
                                  "instruction": "check search"})
        assert "not up" in out
        assert "start_app" in out

    def test_start_app_refuses_to_guess_between_two_ios_devices(self, eco, monkeypatch):
        """The iPad and the iPhone are both `ios`. Picking one here would bring a stack up for
        the wrong device and file everything it saw against this app."""
        attach(monkeypatch, ios(IPAD_UDID), ios(IPHONE_UDID, "iPhone 15"))
        monkeypatch.setattr(stacks, "start",
                            lambda *a, **k: pytest.fail("must not start on a guess"))
        out = call("start_app", {"app": "doctor-ipad"})
        assert "not pinned" in out
        assert IPAD_UDID in out and IPHONE_UDID in out
        assert "pin_device" in out

    def test_start_app_pins_the_only_candidate_and_brings_the_stack_up(self, eco, monkeypatch):
        attach(monkeypatch, ios(IPAD_UDID))
        monkeypatch.setattr(stacks, "start", lambda platform, serial=None, **k: {
            **stacks._row(platform, True, "WebDriverAgent is answering"),
            "started": True, "note": "up", "serial": serial})
        out = call("start_app", {"app": "doctor-ipad"})
        assert "READY" in out
        assert (backend_projects.read_meta(IPAD) or {})["device_serial"] == IPAD_UDID
        # And it names what can now be run, which is the next thing anyone asks.
        assert "search" in out

    def test_the_only_ios_device_is_not_pinned_to_an_app_it_contradicts(self, eco, monkeypatch):
        """One iOS device attached is the case that makes the wrong pin *likely*, not unlikely:
        there is nothing to disambiguate against, so an iPhone would be pinned to `doctor-ipad`
        purely for being the only thing there. The role names the kind out loud; check it."""
        attach(monkeypatch, ios(IPHONE_UDID, "iPhone 15"))
        monkeypatch.setattr(stacks, "start",
                            lambda *a, **k: pytest.fail("must not start on a mismatch"))
        out = call("start_app", {"app": "doctor-ipad"})
        assert "iPhone 15" in out and "doctor-ipad" in out
        assert (backend_projects.read_meta(IPAD) or {}).get("device_serial") is None

    def test_start_app_on_the_web_does_not_claim_to_have_started_a_server(self, eco):
        """A browser is launched per run. Saying otherwise would make "start the clinic web
        project" sound like it did something it did not."""
        out = call("start_app", {"app": "clinic-web"})
        assert "clinic-web (web)" in out
        assert "per project" in out or "per run" in out or "Playwright" in out

    def test_pinning_to_a_device_that_is_not_attached_is_refused(self, eco, monkeypatch):
        attach(monkeypatch, ios(IPAD_UDID))
        out = call("pin_device", {"app": "doctor-ipad", "serial": IPHONE_UDID})
        assert "not attached" in out
        assert (backend_projects.read_meta(IPAD) or {}).get("device_serial") is None

    def test_pinning_across_platforms_is_refused(self, eco, monkeypatch):
        attach(monkeypatch, {"serial": "R5CR12GJAJY", "platform": "android", "model": "S21"})
        out = call("pin_device", {"app": "doctor-ipad", "serial": "R5CR12GJAJY"})
        assert "android" in out and "ios" in out

    def test_a_website_has_nothing_to_pin(self, eco):
        assert "nothing to pin" in call("pin_device", {"app": "clinic-web", "serial": "x"})

    def test_list_devices_says_which_app_would_take_which_device(self, eco, monkeypatch):
        attach(monkeypatch, ios(IPAD_UDID))
        out = call("list_devices", {})
        assert IPAD_UDID in out
        assert "doctor-ipad" in out
        assert "the target is the URL" in out      # clinic-web
        assert "nothing attached" in out           # patient-android

    def test_running_now_is_honest_when_nothing_is_running(self, eco):
        assert "Nothing is running" in call("running_now", {})

    def test_running_now_names_the_holder_of_a_taken_target(self, eco):
        import device_locks

        device_locks.reset()
        device_locks.acquire(device_locks.key_for("ios", IPAD_UDID, IPAD), IPAD, "search")
        out = call("running_now", {})
        assert IPAD_UDID in out
        assert "doctor-ipad/search" in out
        device_locks.reset()

    def test_stopping_something_that_is_not_running_is_not_an_error(self, eco):
        out = call("stop_module", {"app": "doctor-ipad", "module": "search"})
        assert "not running" in out


class TestFileTools:
    def test_a_path_outside_the_roots_is_refused_through_the_tool_too(self, eco, tmp_path):
        out = call("list_dir", {"path": str(tmp_path.parent)})
        assert "outside the folders" in out

    def test_trash_reports_a_move_not_a_deletion(self, eco, tmp_path, monkeypatch):
        monkeypatch.setattr(manager_fs, "HARNESS_ROOT", tmp_path)
        doomed = tmp_path / "stale"
        doomed.mkdir()
        out = call("trash_path", {"path": str(doomed)})
        assert "Nothing was deleted" in out
        assert not doomed.exists()


# -- watching a run somebody else started ----------------------------------------------------
class TestWatchTab:
    """A run the manager starts happens in a project nobody has open. Without a tab, the only
    evidence is a counter moving on a board — which is the failure this tier introduced."""

    def test_the_url_deep_links_to_the_module_and_escapes_the_package(self):
        """Packages here are URLs and app labels with spaces in them, not identifiers."""
        url = agent_bridge.watch_url("https://metaesthetics.net/en", "booking")
        assert "project=https%3A%2F%2Fmetaesthetics.net%2Fen" in url
        assert url.endswith("&module=booking")

    def test_the_manager_opens_a_tab_when_it_starts_a_run(self, eco, ready_stacks, monkeypatch):
        opened = []
        monkeypatch.setattr(agent_bridge, "open_watch_tab",
                            lambda p, s: opened.append((p, s)))
        monkeypatch.setattr(agent_bridge.sessions, "get",
                            lambda *a, **k: _IdleSession())
        out = call("run_module", {"app": "clinic-web", "module": "search",
                                  "instruction": "check the booking flow"})
        assert opened == [(WEB, "search")]
        assert "watch" in out.lower()

    def test_the_send_button_does_not_open_a_tab_onto_itself(self, eco, monkeypatch):
        """`watch` defaults off. The dashboard's own Send is pressed by someone already
        looking at the module; opening a second tab on it is pure noise."""
        monkeypatch.setattr(agent_bridge, "open_watch_tab",
                            lambda p, s: pytest.fail("Send must not open a tab"))
        monkeypatch.setattr(agent_bridge.sessions, "get", lambda *a, **k: _IdleSession())
        assert _start(WEB, "search", "hello")["watching"] is False

    def test_the_toggle_is_honoured(self, eco, monkeypatch):
        import config

        monkeypatch.setattr(config, "AGENT_OPEN_MODULE_TABS", False)
        monkeypatch.setattr(agent_bridge, "open_watch_tab",
                            lambda p, s: pytest.fail("the toggle is off"))
        monkeypatch.setattr(agent_bridge.sessions, "get", lambda *a, **k: _IdleSession())
        out = _start(WEB, "search", "hello", watch=True)
        assert out["watching"] is False
        # ...and the URL is still reported, so the agent can tell the user where to look.
        assert "module=search" in out["watch_url"]


def _start(*args, **kwargs) -> dict:
    """`start_run` inside a loop, with the turn it queues allowed to run to completion.

    It hands the turn to `asyncio.create_task`, so calling it from a sync test raises before
    it reaches anything worth asserting on.
    """
    async def go():
        out = agent_bridge.start_run(*args, **kwargs)
        await asyncio.sleep(0)
        return out

    return asyncio.run(go())


class _IdleSession:
    """Enough of an AgentSession for start_run: a free target and a no-op turn."""

    class _Device:
        resolved_platform = "web"
        serial = None

    busy = False
    device = _Device()

    async def send(self, text):  # noqa: D102 - never awaited; the task is cancelled at teardown
        return None


# -- the prompt ----------------------------------------------------------------------------------
def test_the_prompt_names_every_tool_and_the_rules_the_tools_enforce(eco):
    """A tool the prompt does not name is one the agent will not reach for; a rule stated only
    in code is one it will argue with. Both halves have to be here."""
    prompt = prompts.build_system_prompt(NAME, "main", "Main", "")
    for name in ecosystem_tools.ECOSYSTEM_TOOL_NAMES:
        assert name.rsplit("__", 1)[-1] in prompt, name
    assert "two iOS devices cannot" in prompt
    assert "_trash" in prompt
    assert "{" not in prompt.replace("{ecosystem}", "")


def test_the_deep_link_is_applied_before_the_project_list_loads():
    """A source-order assertion, because the bug it guards was pure ordering and invisible.

    `loadAgentProjects` keeps `agent.package` when it names a real project and otherwise takes
    `projects[0]` — and it resolves a fetch, so it lands *after* anything the boot block wrote
    synchronously. Wired up the natural way (deep link last, next to the `last_opened`
    fallback it replaces) the link was honoured and then silently overwritten a moment later:
    the tab opened on the wrong project, with no error anywhere.

    There is no browser in this suite, so what can be checked is that the two statements are
    still in the order that makes it work.
    """
    source = (Path(__file__).resolve().parents[1]
              / "frontend" / "static" / "js" / "main.js").read_text(encoding="utf-8")
    assert "URLSearchParams(location.search)" in source
    assert source.index("agent.package = linkedProject") < source.index("initAgent();"), (
        "the deep link must be applied before initAgent(), or loadAgentProjects overwrites it")
    assert source.index("ui.pendingModule = linkedModule") < source.index("initAgent();")


def test_the_launcher_finds_the_product_and_its_manager_module(eco):
    import start_master

    assert start_master.resolve_ecosystem(NAME) == NAME
    assert start_master.resolve_ecosystem("nope") is None
    package, slug = start_master.ensure_supervisor(NAME)
    assert ecosystem.supervises(package) == NAME
    assert store.get_subproject(package, slug) is not None
