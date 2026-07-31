"""The Main module — the one module per project that manages the others.

It is built from a different prompt and a different tool list from a tester, and the two have
to agree. This file exists to keep three specific ways that can go wrong from going wrong.

**The manager must not be able to file a finding.** This is the load-bearing one. A finding is
a verdict about one named test case with a screenshot behind it — that is what makes the
outcome pills and `project_report` mean something. The manager walks the app during recon and
forms impressions: "the cart total looked stale". Those are not verdicts, and once one is in
`findings.json` nothing downstream can tell the two apart — the project's bug count becomes
partly recon guesswork and the report totals it as fact. The prompt says it has no
`record_finding`; these tests are what make that sentence true rather than aspirational.
Enforced by the tool being *absent*, not denied: a tool in the definitions that the prompt
disclaims is an invitation to reach for it, and the harness already paid for that lesson once
with the cheap-tier tools (see `prompts._cost_section`).

**The two prompts must not drift on how the device lies.** The manager reads the same dumps off
the same phone, so every trap in `_DEVICE_TRAPS` applies to it — with the difference that its
misreadings land in a breakdown that shapes every module after it. One constant, interpolated
into both.

**A module that was never run must never read as a module that passed.** `project_report` is
the tool the manager answers "where does this project stand" from, and "no defects in Checkout"
about a module nobody has opened is the most expensive wrong sentence it could help write.
"""
from __future__ import annotations

import asyncio

import pytest

import project_paths
from agent import device_tools, manager_tools, prompts, store
from agent.device_tools import DeviceSession, build_device_server


@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", root)
    monkeypatch.setattr(store, "PROJECTS_DIR", root)
    return root


PKG = "com.example.app"


@pytest.fixture
def project():
    """A project with a manager, a module that ran and found things, and one that never ran."""
    store.create_subproject(PKG, "Main", "manages this project", "approved")
    store.create_subproject(PKG, "Login", "auth flows", "approved")
    store.create_subproject(PKG, "Checkout", "cart and payment", "approved")
    store.add_finding(PKG, "login", {"title": "Empty submit accepted", "kind": "bug",
                                     "severity": "high", "expected": "refuse the submit",
                                     "actual": "landed on the home screen"})
    store.add_finding(PKG, "login", {"title": "Valid login works", "kind": "pass",
                                     "severity": "none"})
    store.add_note(PKG, "login", {"section": "login / empty submit", "kind": "bug",
                                  "text": "Submitted with both fields blank."})
    store.update_subproject(PKG, "login", last_run_at="2026-07-30T09:00:00Z")
    return PKG


# ---------------------------------------------------------------------------------------
# Which slug is the manager
# ---------------------------------------------------------------------------------------
class TestSlugResolution:
    def test_both_names_resolve_to_the_manager(self):
        """`onboarding` is what it was called before it was a manager. The slug is the folder
        name, so renaming would mean moving a transcript — both names stay valid instead."""
        assert store.is_main_slug("main")
        assert store.is_main_slug("onboarding")
        assert not store.is_main_slug("login")
        assert not store.is_main_slug("mainscreen"), "prefix matching would catch real modules"

    def test_a_project_with_no_manager_points_at_where_one_would_go(self):
        """"Find it" and "where would it go" are one call, so the endpoint that creates the
        manager and the code that opens it cannot disagree about the slug."""
        assert store.main_slug("com.nothing.here") == "main"

    def test_a_legacy_project_keeps_its_onboarding_folder(self):
        store.create_subproject(PKG, "Onboarding", "the interview", "approved")
        assert store.main_slug(PKG) == "onboarding"

    def test_main_wins_when_a_project_somehow_has_both(self):
        """`main` is the one current code creates and writes to, so a project that ended up
        with both must not have its live conversation shadowed by the old one."""
        store.create_subproject(PKG, "Onboarding", "the interview", "approved")
        store.create_subproject(PKG, "Main", "manages this project", "approved")
        assert store.main_slug(PKG) == "main"


# ---------------------------------------------------------------------------------------
# The tool list
# ---------------------------------------------------------------------------------------
class TestItCannotFileFindings:
    def test_record_finding_is_absent_from_the_managers_allow_list(self):
        assert "mcp__device__record_finding" in device_tools.DEVICE_TOOL_NAMES
        assert "mcp__device__record_finding" not in device_tools.MANAGER_DEVICE_TOOL_NAMES

    def test_the_lists_differ_by_exactly_the_verdict_tools(self):
        """Withholding more than intended would quietly cripple recon — the manager still has
        to launch the app, read screens and navigate."""
        withheld = set(device_tools.DEVICE_TOOL_NAMES) - set(
            device_tools.MANAGER_DEVICE_TOOL_NAMES)
        assert withheld == {f"mcp__device__{name}" for name in device_tools.VERDICT_TOOLS}

    def test_the_manager_keeps_the_tools_recon_needs(self):
        for name in ("launch", "read_screen", "tap_element", "scroll", "ask_user",
                     "propose_subprojects", "journey_step"):
            assert f"mcp__device__{name}" in device_tools.MANAGER_DEVICE_TOOL_NAMES

    def test_the_tool_is_not_registered_at_all_not_merely_denied(self):
        """A tool in the definitions that the prompt disclaims is worse than useless: the model
        sees it, reaches for it at the moment it most wanted to, and spends a turn discovering
        the refusal. It has to be missing from the server, not blocked at the gate."""
        import mcp.types as mcp_types

        session = DeviceSession(PKG, "main")
        for can_file, expected in ((True, True), (False, False)):
            instance = build_device_server(session, can_file_findings=can_file)["instance"]
            lister = instance.request_handlers[mcp_types.ListToolsRequest]
            listed = asyncio.run(lister(mcp_types.ListToolsRequest(method="tools/list")))
            names = {t.name for t in listed.root.tools}
            assert ("record_finding" in names) is expected

    def test_calling_it_on_a_manager_server_is_not_found(self):
        import mcp.types as mcp_types

        session = DeviceSession(PKG, "main")
        instance = build_device_server(session, can_file_findings=False)["instance"]
        handler = instance.request_handlers[mcp_types.CallToolRequest]
        request = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name="record_finding",
                arguments={"title": "looked stale", "kind": "bug", "expected": "a",
                           "actual": "b"}))
        result = asyncio.run(handler(request))
        assert result.root.isError
        assert "not found" in result.root.content[0].text.lower()

    def test_the_allow_list_matches_what_the_server_registers(self):
        """Two lists that disagree give the agent either a tool it can see and cannot call, or
        one nobody meant it to have."""
        import mcp.types as mcp_types

        session = DeviceSession(PKG, "main")
        for can_file, allow in ((True, device_tools.DEVICE_TOOL_NAMES),
                                (False, device_tools.MANAGER_DEVICE_TOOL_NAMES)):
            instance = build_device_server(session, can_file_findings=can_file)["instance"]
            lister = instance.request_handlers[mcp_types.ListToolsRequest]
            listed = asyncio.run(lister(mcp_types.ListToolsRequest(method="tools/list")))
            registered = {f"mcp__device__{t.name}" for t in listed.root.tools}
            assert registered == set(allow)

    def test_the_manager_tool_names_match_the_manager_server(self):
        import mcp.types as mcp_types

        instance = manager_tools.build_manager_server(DeviceSession(PKG, "main"))["instance"]
        lister = instance.request_handlers[mcp_types.ListToolsRequest]
        listed = asyncio.run(lister(mcp_types.ListToolsRequest(method="tools/list")))
        assert {f"mcp__manager__{t.name}" for t in listed.root.tools} == set(
            manager_tools.MANAGER_TOOL_NAMES)

    def test_manager_tool_names_hands_out_a_copy(self):
        """runtime._options builds its allow-list with `+=`, and an earlier version of that
        pattern appending to the module-level list would grow it on every session."""
        manager_tools.manager_tool_names().append("mcp__manager__nonsense")
        assert "mcp__manager__nonsense" not in manager_tools.MANAGER_TOOL_NAMES


# ---------------------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------------------
class TestThePrompt:
    def test_the_manager_gets_the_manager_core(self):
        text = prompts.build_system_prompt(PKG, "main", "Main", "manages this project")
        assert "manager module inside QA Tester AI" in text
        assert "you do not file findings" in text

    def test_a_tester_does_not(self):
        text = prompts.build_system_prompt(PKG, "login", "Login", "auth flows")
        assert "manager module inside QA Tester AI" not in text
        assert "record_finding" in text

    def test_a_legacy_onboarding_module_is_a_manager_too(self):
        text = prompts.build_system_prompt(PKG, "onboarding", "Onboarding", "")
        assert "manager module inside QA Tester AI" in text

    def test_both_prompts_carry_the_device_traps(self):
        """The manager reads the same dumps off the same phone. A copy of this section that
        drifted would put it back on the exact misreadings the tester is protected from —
        landing in a breakdown that shapes every module after it."""
        manager = prompts.build_system_prompt(PKG, "main", "Main", "")
        tester = prompts.build_system_prompt(PKG, "login", "Login", "")
        for marker in ("only the topmost window",
                       "Never judge a submit while a request is in flight",
                       "Do not select by label alone",
                       "Forms validate reactively as you type"):
            assert marker in manager, f"manager lost: {marker}"
            assert marker in tester, f"tester lost: {marker}"

    def test_neither_prompt_leaves_an_unfilled_placeholder(self):
        """Both are `.format`ed with a different set of keys. A section added to one and not
        interpolated ships a literal `{device_traps}` to the model."""
        for slug in ("main", "login"):
            text = prompts.build_system_prompt(PKG, slug, "T", "")
            for placeholder in ("{device_traps}", "{cost_section}", "{memory_path}"):
                assert placeholder not in text, f"{slug} has an unfilled {placeholder}"

    def test_the_manager_prompt_names_the_tools_it_actually_has(self):
        text = prompts.build_system_prompt(PKG, "main", "Main", "")
        for name in ("list_modules", "read_module", "create_module", "project_report"):
            assert name in text
            assert f"mcp__manager__{name}" in manager_tools.MANAGER_TOOL_NAMES

    def test_the_manager_prompt_does_not_offer_a_tool_it_lacks(self):
        """The prompt says plainly that it has no `record_finding`. If a later edit re-granted
        the tool, that sentence would become a lie the model reads sixty turns deep."""
        text = prompts.build_system_prompt(PKG, "main", "Main", "")
        assert "you do not file findings" in text
        assert "mcp__device__record_finding" not in device_tools.MANAGER_DEVICE_TOOL_NAMES

    def test_the_interview_says_it_stays_open_afterwards(self):
        """Without this the module goes quiet after proposing a breakdown, which is what it did
        before it was a manager — and the tools it grew would never be reached for."""
        text = prompts.onboarding_prompt(PKG)
        assert "manager" in text
        assert "list_modules" in text and "project_report" in text


# ---------------------------------------------------------------------------------------
# The tools, as the agent reaches them
# ---------------------------------------------------------------------------------------
class TestManagerToolsThroughTheServer:
    """Driven through the MCP CallToolRequest handler, for the reason test_board_notes gives:
    a tool that only works when called as a local closure is not the tool the agent has."""

    @pytest.fixture
    def call(self):
        import mcp.types as mcp_types

        session = DeviceSession(PKG, "main")
        instance = manager_tools.build_manager_server(session)["instance"]
        handler = instance.request_handlers[mcp_types.CallToolRequest]

        def invoke(name, **arguments):
            request = mcp_types.CallToolRequest(
                method="tools/call",
                params=mcp_types.CallToolRequestParams(name=name, arguments=arguments))
            result = asyncio.run(handler(request))
            return result.root.content[0].text, bool(result.root.isError)

        return invoke

    # -- list_modules ------------------------------------------------------------------
    def test_list_modules_shows_every_module_with_its_counts(self, call, project):
        text, errored = call("list_modules")
        assert not errored
        assert "login" in text and "checkout" in text and "main" in text
        assert "1 bug, 1 pass" in text

    def test_list_modules_marks_which_one_is_speaking(self, call, project):
        text, _ = call("list_modules")
        assert "<- you" in text

    def test_list_modules_on_an_empty_project_says_what_to_do(self, call):
        text, errored = call("list_modules")
        assert not errored
        assert "propose_subprojects" in text and "create_module" in text

    def test_list_modules_distinguishes_never_run_from_nothing_found(self, call, project):
        text, _ = call("list_modules")
        assert "never run" in text, "a module nobody opened must not read as one that passed"

    # -- read_module -------------------------------------------------------------------
    def test_read_module_returns_findings_notes_and_memory(self, call, project):
        text, errored = call("read_module", slug="login")
        assert not errored
        assert "Empty submit accepted" in text
        assert "expected: refuse the submit" in text
        assert "Submitted with both fields blank." in text
        assert "## Its memory file" in text

    def test_read_module_refuses_an_unknown_slug_and_names_the_real_ones(self, call, project):
        """Silently reading the wrong module would attribute one suite's defects to another,
        and the summary that came out would look perfectly reasonable."""
        text, errored = call("read_module", slug="chekout")
        assert errored
        assert "checkout" in text, "the refusal should show the spelling that works"

    def test_read_module_says_a_module_filed_nothing_without_implying_it_passed(
            self, call, project):
        text, errored = call("read_module", slug="checkout")
        assert not errored
        assert "not run" in text and "different" in text

    def test_read_module_labels_a_truncated_memory_file(self, call, project):
        """An agent that thinks it read a whole file will answer "there is nothing about X in
        Checkout's memory" when the answer was past the cut."""
        long_memory = "- a confirmed behaviour\n" * 400
        store.memory_path(PKG, "login").parent.mkdir(parents=True, exist_ok=True)
        store.memory_path(PKG, "login").write_text(long_memory, encoding="utf-8")
        text, _ = call("read_module", slug="login")
        assert "truncated" in text
        assert "Do not conclude something is absent" in text

    # -- create_module -----------------------------------------------------------------
    def test_create_module_adds_an_approved_module(self, call, project):
        text, errored = call("create_module", title="Search", scope="search and filters")
        assert not errored
        entry = store.get_subproject(PKG, "search")
        assert entry is not None and entry["status"] == "approved"

    def test_create_module_says_it_is_waiting_rather_than_tested(self, call, project):
        """Creating a module is not testing it. An agent that reported otherwise would have the
        user believing an area was covered by a module that has never been opened."""
        text, _ = call("create_module", title="Search", scope="search and filters")
        assert "run nothing" in text or "has run nothing" in text

    def test_recreating_a_module_reports_an_update_not_a_creation(self, call, project):
        """`create_subproject` is idempotent on slug. Saying "created" for an update is the
        harness claiming something it did not do, and the user would go looking in the rail
        for a module that is not there."""
        call("create_module", title="Search", scope="search and filters")
        text, errored = call("create_module", title="Search", scope="search, filters and sort")
        assert not errored
        assert "already existed" in text and "updated" in text
        assert len([s for s in store.list_subprojects(PKG) if s["slug"] == "search"]) == 1

    def test_create_module_refuses_the_managers_own_slug(self, call, project):
        """A second module on `main` would either collide with this conversation's folder or
        become a tester holding the manager's prompt."""
        for title in ("Main", "main", "Onboarding"):
            text, errored = call("create_module", title=title, scope="x")
            assert errored, title
            assert "manager module" in text

    def test_create_module_refuses_an_empty_title(self, call, project):
        text, errored = call("create_module", title="   ", scope="something")
        assert errored

    # -- project_report ----------------------------------------------------------------
    def test_project_report_totals_every_module(self, call, project):
        text, errored = call("project_report")
        assert not errored
        assert "1 bug, 1 pass" in text
        assert "login/F001" in text

    def test_project_report_calls_out_modules_nobody_ran(self, call, project):
        """The sentence this tool exists to stop the manager writing: "no defects in Checkout"
        about a module that has never been opened."""
        text, _ = call("project_report")
        assert "Never run" in text
        assert "checkout" in text
        assert "not because they passed" in text

    def test_project_report_leaves_the_manager_out_of_the_per_module_rows(self, call, project):
        """It files nothing by construction, so a zero row for it reads as a gap."""
        text, _ = call("project_report")
        rows = [line for line in text.splitlines() if line.startswith("main:")]
        assert not rows

    def test_project_report_on_an_empty_project_says_so(self, call):
        text, errored = call("project_report")
        assert not errored
        assert "No modules" in text

    def test_project_report_caps_the_detail_and_says_it_capped_it(self, call, project):
        """Truncation that reads as completeness is how a report claims to have covered
        everything it did not. The counts stay complete; only the detail is cut."""
        for i in range(manager_tools.REPORT_DETAIL_LIMIT + 5):
            store.add_finding(PKG, "checkout", {"title": f"case {i}", "kind": "bug",
                                                "severity": "low"})
        text, _ = call("project_report")
        assert "more not printed" in text
        assert "counts above are complete" in text
