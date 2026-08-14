"""The ecosystem manager — the tier above every project manager.

`test_manager_module.py` exists because a manager that could file a finding would put recon
guesswork in the same list as verdicts. The equivalent load-bearing claim here is one step
stronger, and this file is what makes it true rather than aspirational:

**The ecosystem manager must have no device at all.** Not a restricted set — none. Its prompt
says so in as many words, and a prompt is only as good as the tool list beside it. Two things
make it real: the device server is never built for such a session, and the ecosystem server
registers exactly the tools the allow-list and the prompt name. Enforced by absence, for the
reason the harness already paid for once (see `prompts._cost_section`): a tool the model can
see and the prompt disclaims is an invitation to reach for it.

The reason it matters more here than one tier down is timing. There is still no lock across
sessions, and this is the session most likely to be open while some module is driving the one
phone — so it is the one that most needs to be incapable of touching it.

**A supervisor is not one of its own apps.** It lives in the ecosystem and has a project
folder like any other, so every listing built from `members()` would count it as an extra app
with no modules and no findings — an always-empty row, and a total that is one too many.
"""
from __future__ import annotations

import asyncio

import pytest

import clusters
import ecosystem
import project_paths
from agent import ecosystem_tools, prompts, store
from agent.device_tools import DeviceSession
from backend import projects as backend_projects

NAME = "metaesthetics"


@pytest.fixture
def eco(tmp_path, monkeypatch):
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
    for package, role in (("com.patient.android", "patient-android"),
                          ("clinic.example.com", "clinic-web")):
        backend_projects.write_meta(package, platform="android")
        ecosystem.tag(package, NAME, role)
        store.create_subproject(package, "Search")
        store.add_finding(package, "search",
                          {"title": "search misses surnames", "kind": "bug",
                           "expected": "surname matches", "actual": "no results"})
    ecosystem.create_supervisor(NAME)
    store.create_subproject(NAME, "Main")
    return tmp_path


def call(name: str, args: dict) -> str:
    """Invoke one ecosystem tool the way the SDK does, and return its text."""
    import mcp.types as mcp_types

    instance = ecosystem_tools.build_ecosystem_server(DeviceSession(NAME, "main"),
                                                      NAME)["instance"]
    handler = instance.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=args))
    result = asyncio.run(handler(request))
    return result.root.content[0].text


# ---------------------------------------------------------------------------------------
# It has no device
# ---------------------------------------------------------------------------------------
class TestItHasNoDevice:
    def test_a_supervisor_session_registers_no_device_server(self, eco):
        """The whole claim, in one assertion: not a restricted device server, no device
        server."""
        from agent.runtime import AgentSession

        async def noop(_e):
            pass

        options = AgentSession(NAME, "main", noop)._options()
        assert set(options.mcp_servers) == {"ecosystem"}
        assert not [t for t in options.allowed_tools if t.startswith("mcp__device__")]
        assert not [t for t in options.allowed_tools if t.startswith("mcp__manager__")]

    def test_an_app_session_is_unaffected(self, eco):
        """The branch must not have taken the device away from the sessions that need it."""
        from agent.runtime import AgentSession

        async def noop(_e):
            pass

        options = AgentSession("com.patient.android", "search", noop)._options()
        assert set(options.mcp_servers) == {"device"}
        assert not [t for t in options.allowed_tools if t.startswith("mcp__ecosystem__")]

    def test_the_ecosystem_server_registers_no_device_tool(self, eco):
        import mcp.types as mcp_types

        instance = ecosystem_tools.build_ecosystem_server(
            DeviceSession(NAME, "main"), NAME)["instance"]
        lister = instance.request_handlers[mcp_types.ListToolsRequest]
        listed = asyncio.run(lister(mcp_types.ListToolsRequest(method="tools/list")))
        names = {t.name for t in listed.root.tools}
        for forbidden in ("launch", "tap_element", "read_screen", "screenshot",
                          "record_finding", "journey_step"):
            assert forbidden not in names

    def test_the_allow_list_matches_what_the_server_registers(self, eco):
        """Two lists that disagree give the agent either a tool it can see and cannot call, or
        one nobody meant it to have."""
        import mcp.types as mcp_types

        instance = ecosystem_tools.build_ecosystem_server(
            DeviceSession(NAME, "main"), NAME)["instance"]
        lister = instance.request_handlers[mcp_types.ListToolsRequest]
        listed = asyncio.run(lister(mcp_types.ListToolsRequest(method="tools/list")))
        registered = {f"mcp__ecosystem__{t.name}" for t in listed.root.tools}
        assert registered == set(ecosystem_tools.ECOSYSTEM_TOOL_NAMES)

    def test_tool_names_hands_out_a_copy(self):
        ecosystem_tools.ecosystem_tool_names().append("mcp__ecosystem__nonsense")
        assert "mcp__ecosystem__nonsense" not in ecosystem_tools.ECOSYSTEM_TOOL_NAMES


# ---------------------------------------------------------------------------------------
# A supervisor is not one of its apps
# ---------------------------------------------------------------------------------------
class TestTheSupervisorIsNotAnApp:
    def test_it_is_excluded_from_members(self, eco):
        roles = [m["role"] for m in ecosystem.members(NAME)]
        assert roles == ["clinic-web", "patient-android"]
        assert ecosystem.SUPERVISOR_ROLE not in roles

    def test_it_does_not_inflate_the_app_count(self, eco):
        assert ecosystem.summary(NAME)["apps"] == 2

    def test_supervises_answers_only_for_the_supervisor(self, eco):
        assert ecosystem.supervises(NAME) == NAME
        assert ecosystem.supervises("com.patient.android") is None

    def test_creating_it_twice_is_idempotent(self, eco):
        ecosystem.create_supervisor(NAME)
        assert ecosystem.supervises(NAME) == NAME
        assert ecosystem.summary(NAME)["apps"] == 2


# ---------------------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------------------
class TestTheTools:
    def test_list_apps_names_every_app_and_not_itself(self, eco):
        out = call("list_apps", {})
        assert "patient-android" in out and "clinic-web" in out
        assert "supervisor" not in out

    def test_read_app_accepts_a_role_or_a_package(self, eco):
        by_role = call("read_app", {"app": "patient-android"})
        by_package = call("read_app", {"app": "com.patient.android"})
        assert "search misses surnames" in by_role
        assert by_role == by_package

    def test_an_unknown_app_is_named_not_guessed_at(self, eco):
        out = call("read_app", {"app": "patient-windows-phone"})
        assert "patient-android" in out and "clinic-web" in out

    def test_read_finding_gives_the_text_a_grouping_decision_needs(self, eco):
        out = call("read_finding", {"app": "patient-android", "module": "search",
                                    "finding": "F001"})
        assert "surname matches" in out and "no results" in out

    def test_read_finding_lists_what_exists_when_the_id_is_wrong(self, eco):
        out = call("read_finding", {"app": "patient-android", "module": "search",
                                    "finding": "F999"})
        assert "F001" in out

    def test_save_cluster_groups_across_apps_and_stamps(self, eco):
        out = call("save_cluster", {
            "id": "search-prefix-only", "title": "Search is prefix-only",
            "confidence": "confirmed",
            "members": [{"app": "patient-android", "module": "search", "finding": "F001"},
                        {"app": "clinic-web", "module": "search", "finding": "F001"}]})
        assert "cross-app" in out and "confirmed" in out

        cluster = clusters.get(NAME, "search-prefix-only")
        assert cluster["size"] == 2
        # The stamp is applied by the tool, not left for a later call nobody makes.
        assert store.list_findings("com.patient.android", "search")[0]["cluster"] \
            == "search-prefix-only"

    def test_save_cluster_refuses_an_unknown_app_without_saving_anything(self, eco):
        """Half-saving a cluster would make the duplicate count look better than it is."""
        out = call("save_cluster", {
            "id": "half", "title": "t",
            "members": [{"app": "patient-android", "module": "search", "finding": "F001"},
                        {"app": "nope", "module": "search", "finding": "F001"}]})
        assert "nope" in out
        assert clusters.get(NAME, "half") is None

    def test_save_cluster_reports_members_that_matched_nothing(self, eco):
        out = call("save_cluster", {
            "id": "orphaned", "title": "t",
            "members": [{"app": "patient-android", "module": "search", "finding": "F001"},
                        {"app": "patient-android", "module": "search", "finding": "F404"}]})
        assert "orphan" in out.lower() and "F404" in out

    def test_unclustered_defects_is_the_working_queue(self, eco):
        assert "2 unclustered" in call("unclustered_defects", {})
        call("save_cluster", {"id": "c1", "title": "t",
                              "members": [{"app": "patient-android", "module": "search",
                                           "finding": "F001"}]})
        out = call("unclustered_defects", {})
        assert "1 unclustered" in out and "clinic-web" in out

    def test_delete_cluster_restores_the_distinct_count(self, eco):
        call("save_cluster", {"id": "c1", "title": "t",
                              "members": [{"app": "patient-android", "module": "search",
                                           "finding": "F001"},
                                          {"app": "clinic-web", "module": "search",
                                           "finding": "F001"}]})
        assert clusters.summary(NAME)["distinct"] == 1
        call("delete_cluster", {"id": "c1"})
        assert clusters.summary(NAME)["distinct"] == 2

    def test_ecosystem_report_says_filed_versus_distinct(self, eco):
        call("save_cluster", {"id": "c1", "title": "Search is prefix-only",
                              "confidence": "confirmed",
                              "members": [{"app": "patient-android", "module": "search",
                                           "finding": "F001"},
                                          {"app": "clinic-web", "module": "search",
                                           "finding": "F001"}]})
        out = call("ecosystem_report", {})
        assert "2 filed defects -> 1 distinct" in out
        assert "Cross-app defects (1)" in out

    def test_the_report_flags_modules_nobody_ran(self, eco):
        """A module with no defects may be one that works or one nobody opened."""
        store.create_subproject("com.patient.android", "Booking")
        assert "not yet tested" in call("ecosystem_report", {})

    def test_create_module_commissions_work_in_a_named_app(self, eco):
        out = call("create_module", {"app": "clinic-web", "title": "Practitioners",
                                     "scope": "check the claim token the patient app consumes"})
        assert "clinic-web" in out
        entry = store.get_subproject("clinic.example.com", "practitioners")
        assert entry is not None and entry["status"] == "proposed"

    def test_create_module_refuses_the_managers_own_slug(self, eco):
        out = call("create_module", {"app": "clinic-web", "title": "Main", "scope": "x"})
        assert "manager module" in out

    def test_update_module_retargets_scope(self, eco):
        call("update_module", {"app": "patient-android", "module": "search",
                               "scope": "narrowed to surname matching"})
        assert store.get_subproject("com.patient.android", "search")["scope"] \
            == "narrowed to surname matching"

    def test_update_module_cannot_touch_findings_or_memory(self, eco):
        """The powers it deliberately lacks — those were written by the agent that watched."""
        import mcp.types as mcp_types

        instance = ecosystem_tools.build_ecosystem_server(
            DeviceSession(NAME, "main"), NAME)["instance"]
        lister = instance.request_handlers[mcp_types.ListToolsRequest]
        listed = asyncio.run(lister(mcp_types.ListToolsRequest(method="tools/list")))
        schema = next(t for t in listed.root.tools if t.name == "update_module").inputSchema
        assert set(schema["properties"]) == {"app", "module", "scope", "title", "status"}

    def test_update_module_needs_something_to_change(self, eco):
        assert "Nothing to change" in call("update_module", {"app": "patient-android",
                                                             "module": "search"})


# ---------------------------------------------------------------------------------------
# The prompt, paired with the tool list
# ---------------------------------------------------------------------------------------
class TestThePrompt:
    def test_the_supervisor_gets_the_ecosystem_core(self, eco):
        prompt = prompts.build_system_prompt(NAME, "main", "Main", "")
        assert "ecosystem manager" in prompt
        assert "You are the manager module" not in prompt

    def test_an_app_manager_still_gets_the_manager_core(self, eco):
        prompt = prompts.build_system_prompt("com.patient.android", "main", "Main", "")
        assert "You are the manager module" in prompt
        assert "ecosystem manager" not in prompt

    def test_it_names_every_tool_it_actually_has(self, eco):
        prompt = prompts.build_system_prompt(NAME, "main", "Main", "")
        for name in ecosystem_tools.ECOSYSTEM_TOOL_NAMES:
            assert name.rsplit("__", 1)[-1] in prompt

    def test_it_offers_no_tool_it_lacks(self, eco):
        """The pairing rule: a prompt that describes an absent tool costs a whole turn
        discovering it is absent, and reads as permission to go looking for it.

        The device lessons in system memory are the live case — every one of them names a
        device tool, and they used to be appended to every prompt including this one.
        """
        prompt = prompts.build_system_prompt(NAME, "main", "Main", "")
        for absent in ("read_screen", "tap_element", "record_finding", "journey_step",
                       "use_credential", "propose_subprojects"):
            assert absent not in prompt

    def test_it_does_not_claim_an_app_under_test(self, eco):
        prompt = prompts.build_system_prompt(NAME, "main", "Main", "")
        assert "App under test" not in prompt
        assert "Stored test credentials" not in prompt
        assert f"Product: `{NAME}`" in prompt

    def test_it_leaves_no_unfilled_placeholder(self, eco):
        prompt = prompts.build_system_prompt(NAME, "main", "Main", "")
        assert "{" not in prompt.replace("{ecosystem}", "")


def test_a_finding_without_a_severity_does_not_break_the_prompt(eco):
    """`record_finding` always sets a severity; `store.add_finding` does not require one.

    A finding written any other way used to raise KeyError while the *system prompt* was
    being assembled — so the symptom was a module that could not open at all, a long way
    from the finding that caused it.
    """
    store.add_finding("com.patient.android", "search",
                      {"title": "no severity on this one", "kind": "bug",
                       "expected": "x", "actual": "y"})
    prompt = prompts.build_system_prompt("com.patient.android", "search", "Search", "")
    assert "no severity on this one" in prompt
    assert "[?]" in prompt


class TestToolSearchIsReachable:
    """The CLI may hand an MCP server's tools over *deferred* — names advertised, schemas not
    loaded — and `ToolSearch` is the only way to load them.

    This is not a hypothetical. On the ecosystem manager's first real run every
    `mcp__ecosystem__*` tool was unreachable: the gate denied `ToolSearch`, the agent could
    not fetch a single schema, and it fell back to reading the projects' JSON off disk. It
    said so in its reply, which is the only reason it was noticed rather than looking like a
    model that simply preferred `Read`.

    Allowing it withholds nothing: `ToolSearch` only fetches schemas, and the gate still
    decides what may actually be called.
    """

    def test_tool_search_is_allowed_for_every_tier(self, eco):
        from agent.runtime import AgentSession

        async def noop(_e):
            pass

        for package, slug in ((NAME, "main"),                      # ecosystem manager
                              ("com.patient.android", "main"),     # project manager
                              ("com.patient.android", "search")):  # tester
            allowed = AgentSession(package, slug, noop)._options().allowed_tools
            assert "ToolSearch" in allowed, f"{package}/{slug} cannot load a deferred schema"

    def test_tool_search_is_not_in_the_blocked_list(self):
        from agent import runtime

        assert "ToolSearch" not in runtime.BLOCKED_TOOLS

    def test_the_refusal_does_not_send_a_device_less_tier_looking_for_a_phone(self, eco):
        """The tester's refusal names the device tools. Said to the ecosystem manager it is an
        instruction it cannot follow, about tools it does not have."""
        import asyncio as _asyncio

        from agent.runtime import AgentSession

        async def noop(_e):
            pass

        options = AgentSession(NAME, "main", noop)._options()
        hook = options.hooks["PreToolUse"][0].hooks[0]
        decision = _asyncio.run(hook({"tool_name": "Bash"}, None, None))
        reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
        assert "ecosystem manager" in reason
        assert "phone" not in reason.lower()
