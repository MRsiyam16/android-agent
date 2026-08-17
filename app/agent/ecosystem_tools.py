"""The ecosystem manager's tools: read across the apps, and say which of their findings are one defect.

`manager_tools.py` is this one tier down — it manages the modules of one project. This manages
the projects of one product. Registered only for a session whose project is a *supervisor*
(`ecosystem.supervises`), which is a project with no app behind it: no package to launch, no
device, nothing to tap.

What is absent is again the point, and here it is absent by a wider margin than the project
manager. That one keeps the device tools, because "does this part of the app deserve a module"
is a question you answer by looking at the app. This one has **no device server registered at
all** — not a restricted one, none — for two reasons:

* **It has no app.** A supervisor project is five apps at once; `launch` has nothing to
  launch and `read_screen` nothing to read. A device tool here would connect to whichever
  phone happened to be plugged in and report it as the product.
* **One phone, one driver** (see manager_tools' header). This tier exists to look at five
  projects whose modules run on shared hardware. It is the session most likely to be open
  while another is driving a device, so it is the one that most needs to be incapable of
  touching one.

It *can* now start a run — `run_module` — and the second point above is exactly why that is
safe rather than reckless. It does not drive the device; it hands an instruction to the module
that owns it, and `device_locks` refuses if something else is already there. Starting a run and
driving one are different powers, and this tier has only the first.

It can also bring the *hardware* up — `start_app`, `pin_device`, `list_devices` — which is the
same distinction one level lower. Launching WebDriverAgent for the iPad is not touching the
iPad; it is making it possible for the module that owns the iPad to touch it. Everything about
which device a suite lands on is decided here, because it is the only tier that can see both
the iPad and the iPhone at once and notice they are not the same device.

And it can move files around — `list_dir`, `move_path`, `copy_path`, `make_dir`, `trash_path`
over the closed set of roots in `manager_fs`. Not a shell: a shell's refusals do not explain
themselves, and nothing here deletes.

It also cannot file a finding. A finding is one named test case with a screenshot behind it,
filed by the agent that watched it happen; this tier has watched nothing. What it produces
instead is a *cluster* — the claim that several already-filed findings are one defect — which
carries its own confidence precisely because it is inference rather than observation.

**Blackcode is the one place this tier writes outside the harness.** The tester tier files one
issue per finding, which is correct for it and wrong for the product: a defect in a shared
backend, filed by five apps, becomes five tickets that five people close separately. Filing a
*cluster* is the one operation only this tier can do, because a cluster is the only object in
the system that spans apps. So it gets `file_cluster` (one ticket, every member stamped),
`link_cluster` (that defect is already tracked as #42), `sync_issue_status` (what got fixed,
across the whole product) and read-only `search_issues`.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from claude_agent_sdk import create_sdk_mcp_server, tool

import clusters as clusters_mod
import config
import ecosystem as ecosystem_mod
import retests as retests_mod
from agent import store
from agent.store import StoreWriteError

__all__ = ["build_ecosystem_server", "ECOSYSTEM_TOOL_NAMES", "ecosystem_tool_names"]

#: How many findings a listing prints in full before it says how many it held back. The counts
#: are always complete; this caps only the detail, the same bargain `project_report` strikes.
DETAIL_LIMIT = 60

#: Worst first — bugs before the things that worked. Mirrors store.FINDING_KINDS.
_KIND_ORDER = ("bug", "warning", "suggestion", "pass")


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    tally = {kind: 0 for kind in _KIND_ORDER}
    for finding in findings:
        kind = str(finding.get("kind") or "bug")
        tally[kind] = tally.get(kind, 0) + 1
    return tally


def _tally_line(tally: dict[str, int]) -> str:
    plural = {"bug": "bugs", "warning": "warnings", "suggestion": "suggestions",
              "pass": "passes"}
    parts = [f"{tally[k]} {k if tally[k] == 1 else plural[k]}"
             for k in _KIND_ORDER if tally.get(k)]
    return ", ".join(parts) if parts else "nothing filed"


#: Worst first. A cluster's issue takes the worst severity any of its reports carried — the
#: whole claim is that these are one defect, and one app rating it "low" does not make a
#: critical one less critical.
_SEVERITY_ORDER = ("critical", "high", "medium", "low", "none")


def _blackcode_missing() -> Optional[str]:
    """The error text when `bk` is not installed, or None when it is."""
    import blackcode
    if blackcode.is_available():
        return None
    return (f"The Blackcode CLI (`{config.BLACKCODE_CLI}`) is not installed or not on PATH. "
            f"Install it with `npm install -g @blackcode_sa/bc-issues`.")


def _worst_severity(members: list[dict[str, Any]]) -> str:
    for severity in _SEVERITY_ORDER:
        if any(str(m.get("severity") or "") == severity for m in members):
            return severity
    return "medium"


def _cluster_description(name: str, cluster: dict[str, Any],
                         full: list[tuple[dict[str, Any], dict[str, Any]]]) -> str:
    """Markdown body for an issue filed from a cluster.

    Every report is spelled out under its own heading — which app, which module, what that
    agent expected and saw. Whoever picks this ticket up needs all of them: the discriminator
    between "one backend fault" and "two client bugs that rhyme" is usually the difference
    between two apps' actuals, and summarising it away is deleting the evidence for the claim
    the ticket is making.
    """
    lines = [
        f"One defect, reported separately by {len(full)} "
        + ("test module" if len(full) == 1 else "test modules")
        + (f" across {len(cluster['roles'])} apps" if cluster["scope"] == "cross-app"
           else " in one app") + ".",
        "",
        f"**Confidence: {cluster.get('confidence', 'tentative')}** — this grouping is an "
        "inference made from the outside by the QA Tester AI ecosystem manager, not something "
        "any single test observed.",
        "",
    ]
    if cluster.get("root"):
        lines += ["## Root-cause hypothesis", cluster["root"], ""]

    lines.append("## Reports")
    for member, finding in full:
        lines += ["", f"### {member['role']} — {member.get('module_title') or member['module']} "
                      f"(`{member['finding']}`)",
                  f"*{finding.get('kind', 'bug')} / {finding.get('severity', '?')}* — "
                  f"{finding.get('title', '')}"]
        if finding.get("expected"):
            lines.append(f"- **Expected:** {finding['expected']}")
        if finding.get("actual"):
            lines.append(f"- **Actual:** {finding['actual']}")
        steps = finding.get("steps") or []
        if steps:
            lines.append("- **Steps:** " + " → ".join(str(s) for s in steps))
    lines.append("")
    lines.append(f"---\nFiled automatically by QA Tester AI — ecosystem `{name}`, cluster "
                 f"`{cluster['id']}`.")
    return "\n".join(lines)


def _finding_line(finding: dict[str, Any]) -> str:
    mark = f"  [cluster: {finding['cluster']}]" if finding.get("cluster") else ""
    return (f"{finding.get('role')}/{finding.get('module_slug')}/{finding.get('id')} "
            f"[{finding.get('kind', 'bug')}/{finding.get('severity', '?')}] "
            f"{finding.get('title', '')}{mark}")


def build_ecosystem_server(session: Any, name: str) -> dict[str, Any]:
    """The ecosystem manager's MCP server, bound to one ecosystem.

    `session` is here for its `_emit` only — unlike the project manager, this server never
    reads `session.package`, because the supervisor project is not one of the apps it reports
    on. The ecosystem is named explicitly instead.
    """

    def _resolve_app(ref: str) -> Optional[dict[str, Any]]:
        """An app by role or by package. Roles are what every listing prints, so they are what
        the agent will type back; packages are accepted because they are what is on disk."""
        ref = (ref or "").strip()
        members = ecosystem_mod.members(name)
        return next((m for m in members if m["role"] == ref or m["package"] == ref), None)

    def _app_names() -> str:
        return ", ".join(f"{m['role']} ({m['package']})" for m in ecosystem_mod.members(name)) \
            or "none tagged yet"

    @tool("list_apps",
          "Every app in this product, with its role, platform, how many modules it has and "
          "what they have filed. This is the state of the ecosystem — start here rather than "
          "reasoning from what you remember, because modules run between your turns.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def list_apps(_args: dict[str, Any]) -> dict[str, Any]:
        apps = await asyncio.to_thread(ecosystem_mod.app_index, name)
        if not apps:
            return _ok(f"No project is tagged into {name!r} yet.")
        return _ok("\n".join(
            f"{app['role']}  ({app['package']})  |  {app['platform']}  |  "
            f"{app['modules']} modules, {app['modules_tested']} tested  |  "
            f"{_tally_line(app['counts'])}" for app in apps))

    @tool("read_app",
          "One app's modules: status, scope, and what each has filed. Use it to see what an "
          "app covers and where its defects are. Titles only — use read_finding for the "
          "expected/actual of a specific one.",
          {"type": "object",
           "properties": {"app": {"type": "string",
                                  "description": "Role (e.g. patient-android) or package"}},
           "required": ["app"], "additionalProperties": False})
    async def read_app(args: dict[str, Any]) -> dict[str, Any]:
        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")

        index = await asyncio.to_thread(ecosystem_mod.module_index, name)
        mods = [m for m in index if m["package"] == member["package"]]
        findings = await asyncio.to_thread(store.list_all_findings, member["package"])

        out = [f"# {member['role']} — {member['package']}  ({member['platform']})", ""]
        never_run = []
        for mod in mods:
            tally = {k: 0 for k in _KIND_ORDER}
            tally.update(mod["counts"])
            out.append(f"{mod['slug']}  ({mod['title']})  |  status: {mod['status']}  |  "
                       f"{_tally_line(tally)}")
            if mod.get("scope"):
                out.append(f"      scope: {mod['scope']}")
            if not mod.get("last_run_at") and not store.is_main_slug(mod["slug"]):
                never_run.append(mod["slug"])
        if never_run:
            # The distinction worth protecting: a module with no bugs may be one that works,
            # or one nobody has run. Those are different and only one is good news.
            out += ["", "Never run — no outcomes because nothing tested them, not because "
                        "they passed: " + ", ".join(never_run)]

        defects = [f for f in findings if f.get("kind") in ("bug", "warning", "suggestion")]
        out += ["", f"## Defects ({len(defects)})"]
        for finding in sorted(defects, key=lambda f: _KIND_ORDER.index(
                str(f.get("kind") or "bug")) if str(f.get("kind") or "bug") in _KIND_ORDER
                else 99)[:DETAIL_LIMIT]:
            out.append(f"{finding.get('module_slug')}/{finding.get('id')} "
                       f"[{finding.get('kind')}/{finding.get('severity', '?')}] "
                       f"{finding.get('title', '')}"
                       + (f"  [cluster: {finding['cluster']}]" if finding.get("cluster") else ""))
        if len(defects) > DETAIL_LIMIT:
            out.append(f"[...{len(defects) - DETAIL_LIMIT} more not printed.]")
        return _ok("\n".join(out))

    @tool("read_finding",
          "One finding in full — expected, actual, steps, and whether it is already tracked or "
          "clustered. This is what you read before claiming two findings are the same defect: "
          "titles rhyme far more often than causes do.",
          {"type": "object",
           "properties": {
               "app": {"type": "string", "description": "Role or package"},
               "module": {"type": "string", "description": "Module slug"},
               "finding": {"type": "string", "description": "Finding id, e.g. F007"},
           },
           "required": ["app", "module", "finding"], "additionalProperties": False})
    async def read_finding(args: dict[str, Any]) -> dict[str, Any]:
        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")
        slug = str(args.get("module") or "").strip()
        wanted = str(args.get("finding") or "").strip()
        findings = await asyncio.to_thread(store.list_findings, member["package"], slug)
        finding = next((f for f in findings if f.get("id") == wanted), None)
        if finding is None:
            known = ", ".join(f["id"] for f in findings) or "none"
            return _err(f"No finding {wanted!r} in {member['role']}/{slug}. It has: {known}.")

        out = [f"{member['role']}/{slug}/{finding['id']}  "
               f"[{finding.get('kind')}/{finding.get('severity', '?')}]",
               finding.get("title", ""), ""]
        for label in ("expected", "actual"):
            if finding.get(label):
                out.append(f"{label}: {finding[label]}")
        if finding.get("steps"):
            out.append("steps: " + " -> ".join(finding["steps"]))
        if finding.get("cluster"):
            out.append(f"cluster: {finding['cluster']}")
        if finding.get("issue_url"):
            out.append(f"tracked: {finding['issue_url']}"
                       + ("  (resolved)" if finding.get("resolved") else ""))
        return _ok("\n".join(out))

    @tool("unclustered_defects",
          "Defects no cluster has claimed yet — the queue to work through when looking for "
          "duplicates. Optionally narrowed to one app. A defect being here means nobody has "
          "judged it, not that it is unique.",
          {"type": "object",
           "properties": {"app": {"type": "string",
                                  "description": "Role or package, to narrow to one app"}},
           "additionalProperties": False})
    async def unclustered_defects(args: dict[str, Any]) -> dict[str, Any]:
        found = await asyncio.to_thread(ecosystem_mod.findings, name)
        found = [f for f in found if not f.get("cluster")]
        if args.get("app"):
            member = await asyncio.to_thread(_resolve_app, str(args["app"]))
            if member is None:
                return _err(f"No app {args['app']!r} in {name!r}. The apps are: {_app_names()}.")
            found = [f for f in found if f["package"] == member["package"]]
        if not found:
            return _ok("Every defect is already in a cluster.")
        out = [f"{len(found)} unclustered defects:"]
        out += [_finding_line(f) for f in found[:DETAIL_LIMIT]]
        if len(found) > DETAIL_LIMIT:
            out.append(f"[...{len(found) - DETAIL_LIMIT} more. Narrow with `app`.]")
        return _ok("\n".join(out))

    @tool("list_clusters",
          "Findings that have been judged to be one defect, cross-app first. Each shows how "
          "many reports it absorbed, how confident that judgement is, and which apps filed "
          "them. Read this before proposing a new cluster, so you extend one rather than "
          "creating a second for the same defect.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def list_clusters(_args: dict[str, Any]) -> dict[str, Any]:
        rows = await asyncio.to_thread(clusters_mod.list_clusters, name)
        if not rows:
            return _ok("No clusters yet. Every filed defect is still being treated as distinct.")
        out = []
        for cluster in rows:
            out.append(f"{cluster['id']}  |  {cluster['size']}x  |  {cluster['scope']}  |  "
                       f"{cluster['confidence']}"
                       + ("  |  RESOLVED" if cluster["resolved"] else ""))
            out.append(f"      {cluster['title']}")
            out.append(f"      apps: {', '.join(cluster['roles']) or 'none'}")
            if cluster["orphans"]:
                out.append(f"      !! {len(cluster['orphans'])} member(s) point at findings "
                           f"that no longer exist")
        return _ok("\n".join(out))

    @tool("read_cluster",
          "One cluster with every member finding spelled out — which app filed it, in which "
          "module, and what it said. Use it to check a grouping still holds before acting on it.",
          {"type": "object",
           "properties": {"id": {"type": "string", "description": "Cluster id"}},
           "required": ["id"], "additionalProperties": False})
    async def read_cluster(args: dict[str, Any]) -> dict[str, Any]:
        cluster = await asyncio.to_thread(clusters_mod.get, name, str(args.get("id") or ""))
        if cluster is None:
            return _err(f"No cluster {args.get('id')!r} in {name!r}. Use list_clusters.")
        out = [f"# {cluster['id']} — {cluster['title']}",
               f"{cluster['size']} reports  |  {cluster['scope']}  |  "
               f"confidence: {cluster['confidence']}",
               f"apps: {', '.join(cluster['roles'])}", ""]
        if cluster.get("root"):
            out += ["Root-cause hypothesis:", cluster["root"], ""]
        for member in cluster["members"]:
            out.append(f"{member['role']}/{member['module']}/{member['finding']} "
                       f"[{member.get('kind')}/{member.get('severity', '?')}] {member['title']}"
                       + ("  (resolved)" if member.get("resolved") else ""))
        for orphan in cluster["orphans"]:
            out.append(f"!! {orphan['package']}/{orphan['module']}/{orphan['finding']} "
                       f"— no longer on disk")
        return _ok("\n".join(out))

    @tool("save_cluster",
          "Record that several filed findings are one defect, or update an existing grouping. "
          "Read each member with read_finding first — this claim is inference about someone "
          "else's backend, so `confidence` must say how much of it the evidence carries: "
          "'confirmed' only when something discriminates (a shared token, an identical error, "
          "one app proving the rule another breaks), 'likely' for same mechanism and area, "
          "'tentative' for same shape and possibly separate implementations. Replaces the "
          "member list wholesale, so pass every member you want kept.",
          {"type": "object",
           "properties": {
               "id": {"type": "string",
                      "description": "Short kebab-case id, e.g. search-prefix-only"},
               "title": {"type": "string", "description": "The one defect, in a line"},
               "root": {"type": "string",
                        "description": "Why you think these are one thing, and what the "
                                       "underlying fault is"},
               "confidence": {"type": "string", "enum": ["confirmed", "likely", "tentative"]},
               "members": {
                   "type": "array",
                   "description": "Every finding in this cluster",
                   "items": {"type": "object",
                             "properties": {
                                 "app": {"type": "string", "description": "Role or package"},
                                 "module": {"type": "string"},
                                 "finding": {"type": "string"}},
                             "required": ["app", "module", "finding"],
                             "additionalProperties": False}},
           },
           "required": ["id", "title", "members"], "additionalProperties": False})
    async def save_cluster(args: dict[str, Any]) -> dict[str, Any]:
        cluster_id = str(args.get("id") or "").strip()
        if not cluster_id:
            return _err("A cluster needs an id.")
        resolved, bad = [], []
        for raw in args.get("members") or []:
            member = await asyncio.to_thread(_resolve_app, str(raw.get("app") or ""))
            if member is None:
                bad.append(str(raw.get("app")))
                continue
            resolved.append({"package": member["package"],
                             "module": str(raw.get("module") or ""),
                             "finding": str(raw.get("finding") or "")})
        if bad:
            return _err(f"Unknown app(s): {', '.join(sorted(set(bad)))}. The apps are: "
                        f"{_app_names()}. Nothing was saved.")
        if not resolved:
            return _err("A cluster needs at least one member. To remove one entirely, use "
                        "delete_cluster.")

        cluster = await asyncio.to_thread(
            clusters_mod.save, name, cluster_id, title=str(args.get("title") or ""),
            root=str(args.get("root") or ""),
            confidence=str(args.get("confidence") or "tentative"), members=resolved)
        await asyncio.to_thread(clusters_mod.apply, name)

        # Reported back rather than assumed: a member whose finding is not on disk is dropped
        # to `orphans`, and a cluster that quietly saved four of five members would make the
        # duplicate count look better than it is.
        note = ""
        if cluster.get("orphans"):
            note = ("\n!! These members matched no finding on disk and are recorded as orphans: "
                    + "; ".join(f"{o['package']}/{o['module']}/{o['finding']}"
                                for o in cluster["orphans"]))
        return _ok(f"Saved {cluster_id!r}: {cluster['size']} reports, {cluster['scope']}, "
                   f"confidence {cluster['confidence']}. Apps: "
                   f"{', '.join(cluster['roles']) or 'none'}.{note}")

    @tool("delete_cluster",
          "Undo a grouping — its findings go back to being counted as distinct defects. Use "
          "when a cluster turns out to be wrong, not to tidy up after a fix.",
          {"type": "object",
           "properties": {"id": {"type": "string", "description": "Cluster id"}},
           "required": ["id"], "additionalProperties": False})
    async def delete_cluster(args: dict[str, Any]) -> dict[str, Any]:
        cluster_id = str(args.get("id") or "").strip()
        if not await asyncio.to_thread(clusters_mod.delete, name, cluster_id):
            return _err(f"No cluster {cluster_id!r} in {name!r}. Use list_clusters.")
        return _ok(f"Deleted {cluster_id!r}. Its findings are counted as distinct again.")

    @tool("ecosystem_report",
          "The whole product in one place: every app's outcomes, how many filed defects are "
          "actually distinct once duplicates are grouped, and the cross-app clusters worst "
          "first. Read this before saying anything about where the product stands — counting "
          "across five apps from memory is how a summary starts being wrong.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def ecosystem_report(_args: dict[str, Any]) -> dict[str, Any]:
        eco = await asyncio.to_thread(ecosystem_mod.summary, name)
        cl = await asyncio.to_thread(clusters_mod.summary, name)
        rows = await asyncio.to_thread(clusters_mod.list_clusters, name)

        out = [f"# {name} — the whole product", "",
               f"{eco['apps']} apps  |  {eco['modules']} modules "
               f"({eco['modules_tested']} tested, {eco['modules_untested']} not)",
               f"{cl['filed']} filed defects -> {cl['distinct']} distinct "
               f"({cl['absorbed']} absorbed by {cl['clusters']} clusters, "
               f"{cl['cross_app']} of them cross-app)", ""]
        if cl["orphans"]:
            out.append(f"!! {cl['orphans']} cluster member(s) point at findings that no longer "
                       f"exist — the distinct count may be wrong until they are fixed.")
            out.append("")

        apps = await asyncio.to_thread(ecosystem_mod.app_index, name)
        out.append("## Per app")
        for app in apps:
            untested = len(app["untested"])
            out.append(f"{app['role']}: {_tally_line(app['counts'])}"
                       + (f"  ({untested} module(s) not yet tested)" if untested else ""))

        cross = [c for c in rows if c["scope"] == "cross-app"]
        out += ["", f"## Cross-app defects ({len(cross)}) — no single project could see these"]
        if not cross:
            out.append("None found yet.")
        for cluster in cross:
            out.append(f"{cluster['size']}x [{cluster['confidence']}] {cluster['title']}")
            out.append(f"      {', '.join(cluster['roles'])}")
        return _ok("\n".join(out))

    @tool("create_module",
          "Create a module in one of the apps — how this tier commissions work. It does not "
          "run it: the module appears in that project's rail and waits for someone to open it. "
          "Say in `scope` what it should establish, including anything another app already "
          "found that it is checking the other half of.",
          {"type": "object",
           "properties": {
               "app": {"type": "string", "description": "Role or package"},
               "title": {"type": "string",
                         "description": "What the app calls this area, not a generic label"},
               "scope": {"type": "string", "description": "What testing this module covers"},
           },
           "required": ["app", "title", "scope"], "additionalProperties": False})
    async def create_module(args: dict[str, Any]) -> dict[str, Any]:
        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")
        title = str(args.get("title") or "").strip()
        if not title:
            return _err("A module needs a title.")
        slug = store.slugify(title)
        if store.is_main_slug(slug):
            return _err(f"{slug!r} is that project's manager module. Pick a name for the part "
                        f"of the app the module covers.")

        existed = await asyncio.to_thread(store.get_subproject, member["package"], slug)
        try:
            entry = await asyncio.to_thread(
                store.create_subproject, member["package"], title,
                str(args.get("scope") or ""), "proposed")
        except OSError as exc:
            return _err(f"The module was NOT created: {exc}")
        if existed:
            return _ok(f"{slug!r} already existed in {member['role']}, so its scope was updated "
                       f"rather than a second module being created. Tell the user it was an "
                       f"update, not a new module.")
        return _ok(f"Created {slug!r} in {member['role']}, status proposed — it is in that "
                   f"project's rail and has run nothing. Someone opens it and it runs there.")

    @tool("update_module",
          "Change a module's scope, title or status in one of the apps. Use it to retarget "
          "work — e.g. narrowing a scope once another app has established half of it. It "
          "cannot touch that module's findings or its memory: those were written by the agent "
          "that watched the run, and rewriting them from up here would replace observation "
          "with inference.",
          {"type": "object",
           "properties": {
               "app": {"type": "string", "description": "Role or package"},
               "module": {"type": "string", "description": "Module slug"},
               "scope": {"type": "string"},
               "title": {"type": "string"},
               "status": {"type": "string", "enum": ["proposed", "approved", "tested"]},
           },
           "required": ["app", "module"], "additionalProperties": False})
    async def update_module(args: dict[str, Any]) -> dict[str, Any]:
        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")
        slug = str(args.get("module") or "").strip()
        entry = await asyncio.to_thread(store.get_subproject, member["package"], slug)
        if entry is None:
            known = [str(s.get("slug")) for s in
                     await asyncio.to_thread(store.list_subprojects, member["package"])]
            return _err(f"No module {slug!r} in {member['role']}. It has: "
                        + (", ".join(known) if known else "none") + ".")

        updates = {k: str(args[k]) for k in ("scope", "title", "status") if args.get(k)}
        if not updates:
            return _err("Nothing to change — pass scope, title or status.")
        updated = await asyncio.to_thread(
            store.update_subproject, member["package"], slug, **updates)
        if updated is None:
            return _err(f"Could not update {slug!r} in {member['role']}.")
        return _ok(f"Updated {member['role']}/{slug}: "
                   + ", ".join(f"{k} -> {v!r}" for k, v in updates.items()))

    # -- Blackcode ------------------------------------------------------------------------
    def _members_with_findings(cluster: dict[str, Any]):
        """Each member paired with its full finding on disk. Members are already resolved
        against the store, so a miss here means it vanished between calls — treated as an
        orphan rather than filed as a member with no evidence."""
        out = []
        for member in cluster["members"]:
            findings = store.list_findings(member["package"], member["module"])
            finding = next((f for f in findings if f.get("id") == member["finding"]), None)
            if finding is not None:
                out.append((member, finding))
        return out

    def _project_for(cluster: dict[str, Any], explicit: Optional[str]) -> tuple[Any, str]:
        """(project id, how it was chosen) — or (None, message) when it cannot be decided.

        Order matters and is deliberately boring. An explicit argument wins. A single-app
        cluster otherwise goes to that app's own Blackcode project, because that is where its
        findings already go. A cross-app one goes to the ecosystem's, because it belongs to the
        product rather than to whichever app happened to notice first. Nothing is guessed: with
        neither remembered, the tool asks.
        """
        import blackcode
        if explicit:
            project_id = blackcode.resolve_project(explicit)
            # Remembered against the supervisor, so the *next* cluster does not have to be
            # told again — the same bargain file_issue strikes per project.
            blackcode.remember_project_id(name, project_id)
            return project_id, f"as given ({explicit})"

        if cluster["scope"] == "single-app" and cluster["members"]:
            package = cluster["members"][0]["package"]
            stored = blackcode.stored_project_id(package)
            if stored is not None:
                return stored, f"the {cluster['members'][0]['role']} project's own"

        stored = blackcode.stored_project_id(name)
        if stored is not None:
            return stored, f"the {name} product project"

        projects = blackcode.list_projects()
        names = ", ".join(f"{p['name']!r} (id {p['id']})" for p in projects) or "(none found)"
        return None, (f"No Blackcode project is set for {name!r} yet. Call again with "
                      f"`project` set to one of: {names}")

    @tool("search_issues",
          "Search or browse Blackcode issues across the workspace — what is already tracked, "
          "and what is still open. Read-only; touches nothing here. Use it before filing a "
          "cluster, so a defect a developer already raised does not get a second ticket.",
          {"type": "object",
           "properties": {
               "query": {"type": "string",
                         "description": "Text to search title/description, or an issue #number"},
               "project": {"type": "string", "description": "Blackcode project id or exact name"},
               "status": {"type": "string",
                          "description": "backlog/todo/in_progress/done/cancelled"},
           },
           "additionalProperties": False})
    async def search_issues(args: dict[str, Any]) -> dict[str, Any]:
        import blackcode
        missing = _blackcode_missing()
        if missing:
            return _err(missing)
        try:
            project_id = None
            if args.get("project"):
                project_id = await asyncio.to_thread(blackcode.resolve_project, args["project"])
            results = await asyncio.to_thread(
                blackcode.search_issues, args.get("query") or "", project_id, args.get("status"))
        except blackcode.BlackcodeError as exc:
            return _err(str(exc))
        if not results:
            return _ok("No matching issues.")
        return _ok("\n".join(
            f"#{r['number']} [{r['status']}] {r['title']} ({r['project_name']}) — {r['url']}"
            for r in results))

    @tool("file_cluster",
          "File a whole cluster as ONE Blackcode issue, and stamp that issue on every finding "
          "in it. This is the operation only this tier can do: the same backend fault filed by "
          "five apps is five tickets from five testers, and one ticket from here. The issue "
          "body spells out every report — which app, what it expected, what it saw — because "
          "that spread is the evidence for the claim that they are one defect. A visible "
          "action outside this dashboard (a real ticket a team will see), so file when the "
          "user asks you to, not on your own initiative because a cluster exists. `project` is "
          "needed only the first time; after that it is remembered.",
          {"type": "object",
           "properties": {
               "id": {"type": "string", "description": "Cluster id"},
               "project": {"type": "string",
                           "description": "Blackcode project id or exact name. Only needed "
                                          "the first time this product files an issue."},
           },
           "required": ["id"], "additionalProperties": False})
    async def file_cluster(args: dict[str, Any]) -> dict[str, Any]:
        import blackcode
        missing = _blackcode_missing()
        if missing:
            return _err(missing)

        cluster_id = str(args.get("id") or "").strip()
        cluster = await asyncio.to_thread(clusters_mod.get, name, cluster_id)
        if cluster is None:
            return _err(f"No cluster {cluster_id!r} in {name!r}. Use list_clusters.")
        if not cluster["members"]:
            return _err(f"{cluster_id!r} has no members whose findings are still on disk — "
                        f"nothing to file.")

        # Refused rather than duplicated. A member already tracked means a ticket for this
        # defect exists; a second one is exactly the outcome this tier exists to prevent, and
        # `link_cluster` is the operation for "it is already #42".
        filed = [m for m in cluster["members"] if m.get("issue_url")]
        if filed:
            already = "; ".join(f"{m['role']}/{m['module']}/{m['finding']} -> {m['issue_url']}"
                                for m in filed)
            return _err(
                f"{cluster_id!r} is already tracked, at least in part: {already}. Nothing was "
                f"filed. If that issue covers this defect, use link_cluster to attach the rest "
                f"of the members to it; if it does not, drop those members from the cluster "
                f"first.")

        full = await asyncio.to_thread(_members_with_findings, cluster)
        if not full:
            return _err(f"None of {cluster_id!r}'s findings could be read back — nothing filed.")

        try:
            project_id, how = await asyncio.to_thread(_project_for, cluster, args.get("project"))
        except blackcode.BlackcodeError as exc:
            return _err(str(exc))
        if project_id is None:
            return _err(how)

        # The first member's screenshot goes inline. One image, not all of them: the ticket
        # names every report, and whoever opens it can follow each back here.
        evidence = next((f.get("evidence") for _, f in full if f.get("evidence")), None)
        try:
            result = await asyncio.to_thread(
                blackcode.create_issue, project_id, cluster.get("title") or cluster_id,
                _cluster_description(name, cluster, full),
                _worst_severity([f for _, f in full]), evidence)
        except blackcode.BlackcodeError as exc:
            return _err(f"Could not file the issue: {exc}")

        # The issue exists from here on, so a stamping failure is reported, never swallowed —
        # a cluster that looks unfiled but has a live ticket is how the second one gets filed.
        stamped, failed = [], []
        for member, _finding in full:
            try:
                await asyncio.to_thread(
                    store.set_finding_tracking, member["package"], member["module"],
                    member["finding"], issue_id=result["number"], issue_url=result["url"])
                stamped.append(f"{member['role']}/{member['finding']}")
            except StoreWriteError as exc:
                failed.append(f"{member['role']}/{member['finding']} ({exc})")

        out = (f"Filed {cluster_id!r} as Blackcode issue #{result['number']} into {how} "
               f"project: {result['url']}\nStamped on {len(stamped)} finding(s): "
               f"{', '.join(stamped)}")
        if failed:
            out += (f"\n!! The issue WAS created, but these findings could not be marked as "
                    f"tracked and will still look unfiled: {'; '.join(failed)}")
        return _ok(out)

    @tool("link_cluster",
          "Attach an existing Blackcode issue to every finding in a cluster — for when the "
          "defect is already tracked, whether a tester filed one report of it or a developer "
          "raised it directly. Creates nothing; it records where this defect already lives so "
          "the rest of its reports stop looking untracked.",
          {"type": "object",
           "properties": {
               "id": {"type": "string", "description": "Cluster id"},
               "issue": {"type": "integer", "description": "Blackcode issue number"},
           },
           "required": ["id", "issue"], "additionalProperties": False})
    async def link_cluster(args: dict[str, Any]) -> dict[str, Any]:
        import blackcode
        missing = _blackcode_missing()
        if missing:
            return _err(missing)

        cluster_id = str(args.get("id") or "").strip()
        cluster = await asyncio.to_thread(clusters_mod.get, name, cluster_id)
        if cluster is None:
            return _err(f"No cluster {cluster_id!r} in {name!r}. Use list_clusters.")

        # Checked live before anything is written: a typo'd number would otherwise stamp every
        # member with a link to an issue that does not exist, which reads exactly like a
        # tracked defect.
        try:
            live = await asyncio.to_thread(blackcode.issue_status, int(args["issue"]))
        except blackcode.BlackcodeError as exc:
            return _err(f"Could not read issue #{args['issue']}: {exc}")

        stamped, failed = [], []
        for member in cluster["members"]:
            try:
                await asyncio.to_thread(
                    store.set_finding_tracking, member["package"], member["module"],
                    member["finding"], issue_id=live["number"], issue_url=live["url"],
                    resolved=live["resolved"])
                stamped.append(f"{member['role']}/{member['finding']}")
            except StoreWriteError as exc:
                failed.append(f"{member['role']}/{member['finding']} ({exc})")

        out = (f"Linked {cluster_id!r} to issue #{live['number']} [{live['status']}]: "
               f"{live['url']}\nStamped on {len(stamped)} finding(s): {', '.join(stamped)}")
        if failed:
            out += f"\n!! Could not stamp: {'; '.join(failed)}"
        return _ok(out)

    @tool("sync_issue_status",
          "Ask Blackcode what has actually been fixed, across every app in this product, and "
          "update each finding's resolved flag to match. This is how the board stops showing "
          "defects that shipped a fix weeks ago. Optionally narrowed to one app. Reports only "
          "what changed, plus anything it could not check.",
          {"type": "object",
           "properties": {"app": {"type": "string",
                                  "description": "Role or package, to narrow to one app"}},
           "additionalProperties": False})
    async def sync_issue_status(args: dict[str, Any]) -> dict[str, Any]:
        import blackcode
        missing = _blackcode_missing()
        if missing:
            return _err(missing)

        found = await asyncio.to_thread(ecosystem_mod.findings, name)
        if args.get("app"):
            member = await asyncio.to_thread(_resolve_app, str(args["app"]))
            if member is None:
                return _err(f"No app {args['app']!r} in {name!r}. The apps are: {_app_names()}.")
            found = [f for f in found if f["package"] == member["package"]]
        tracked = [f for f in found if f.get("issue_id")]
        if not tracked:
            return _ok("Nothing in this product has been filed to Blackcode yet, so there is "
                       "no status to sync.")

        # One `bk` call per distinct issue, not per finding. A cluster filed as one ticket has
        # every member pointing at the same number, and checking it five times is five
        # subprocesses for one answer.
        by_issue: dict[int, list[dict[str, Any]]] = {}
        for finding in tracked:
            by_issue.setdefault(int(finding["issue_id"]), []).append(finding)

        changed, unreachable, newly_queued, unchanged = [], [], [], 0
        for number, group in sorted(by_issue.items()):
            try:
                live = await asyncio.to_thread(blackcode.issue_status, number)
            except blackcode.BlackcodeError as exc:
                unreachable.append(f"#{number} ({exc})")
                continue
            for finding in group:
                if bool(finding.get("resolved")) == live["resolved"]:
                    unchanged += 1
                    continue
                try:
                    await asyncio.to_thread(
                        store.set_finding_tracking, finding["package"], finding["module_slug"],
                        finding["id"], resolved=live["resolved"])
                except StoreWriteError as exc:
                    unreachable.append(f"{finding['role']}/{finding['id']} (could not save: {exc})")
                    continue
                changed.append(f"{finding['role']}/{finding['module_slug']}/{finding['id']} "
                               f"(#{number}) -> {live['status']}"
                               + (" — now resolved" if live["resolved"] else " — reopened"))
                # A defect that just closed is a defect nobody has re-checked on the device.
                # Queued rather than run: from here it is impossible to tell whether the fix
                # is deployed to the environment under test, and "closed" covers fixed,
                # duplicate and will-not-do. Idempotent, so repeated syncs do not pile up.
                if live["resolved"]:
                    queued = await asyncio.to_thread(
                        retests_mod.queue, name, finding["package"], finding["module_slug"],
                        finding["id"], role=finding.get("role", ""),
                        title=str(finding.get("title") or ""),
                        reason=f"Issue #{number} was closed as {live['status']} — not yet "
                               f"re-checked on the device.",
                        issue_id=number, issue_url=str(live.get("url") or ""))
                    if queued is not None:
                        newly_queued.append(f"{finding['role']}/{finding['id']}")

        lines = [f"Checked {len(by_issue)} issue(s) covering {len(tracked)} finding(s)."]
        if changed:
            lines += [f"{len(changed)} changed:"] + changed
        else:
            lines.append("Nothing changed.")
        if unchanged:
            lines.append(f"{unchanged} already matched.")
        if newly_queued:
            lines.append(f"Queued {len(newly_queued)} re-test(s) for the user to approve — "
                         f"marked closed in Blackcode is not the same as checked on the "
                         f"device: {', '.join(newly_queued)}")
        if unreachable:
            lines.append("Could not check: " + "; ".join(unreachable))
        return _ok("\n".join(lines))

    # -- running things -------------------------------------------------------------------
    @tool("run_module",
          "Start a module running, in whichever app it lives in. This is how work you "
          "commission actually happens — create_module puts a module in an app's rail; this "
          "sends it its instruction and it begins. The run happens in that app's own session "
          "with its own device: you are starting it, not driving it, and you will not see its "
          "screen. Refused if something else is already driving that target, because two "
          "agents on one device interleave their actions. Say in `instruction` what to "
          "establish, including what another app already found — that context is the reason "
          "this is worth starting from up here rather than opening it by hand.",
          {"type": "object",
           "properties": {
               "app": {"type": "string", "description": "Role (e.g. patient-android) or package"},
               "module": {"type": "string", "description": "Module slug"},
               "instruction": {"type": "string",
                               "description": "What this run should establish"},
           },
           "required": ["app", "module", "instruction"], "additionalProperties": False})
    async def run_module(args: dict[str, Any]) -> dict[str, Any]:
        import stacks
        from backend import agent_bridge

        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")
        slug = str(args.get("module") or "").strip()
        instruction = str(args.get("instruction") or "").strip()
        if not instruction:
            return _err("A run needs an instruction — say what it should establish.")

        # Checked here and not inside `start_run`, because this is the only caller starting a
        # run blind. A browser's Send button is pressed by someone looking at the device; a
        # tool call is not, and a run started against a stack that was never brought up does
        # not fail — it times out one device tool at a time while the module reasons about a
        # broken app that is in fact simply unreachable.
        ready = await asyncio.to_thread(stacks.status, str(member.get("platform") or ""))
        if not ready["ready"]:
            return _err(f"{member['role']} cannot run yet — its {ready['platform']} stack is "
                        f"not up: {ready['detail']}"
                        + (f" Fix: {ready['fix']}" if ready.get("fix") else "")
                        + " Nothing was started. Use start_app first.")

        try:
            started = agent_bridge.start_run(member["package"], slug, instruction, watch=True)
        except agent_bridge.RunRefused as exc:
            return _err(str(exc))
        return _ok(f"Started {member['role']}/{slug} on {started['target']}. It runs in that "
                   f"project's own session, and "
                   + ("a browser tab is opening on it so the user can watch"
                      if started.get("watching")
                      else f"the user can watch it at {started['watch_url']}")
                   + ". You will not see its screen from here — ask me again later and I will "
                     "read what it filed.")

    @tool("queue_retest",
          "Ask for a finding to be re-tested, pending the user's approval. Use this for work "
          "a *fix* prompted — an issue closed in Blackcode, a developer saying something is "
          "done — rather than work you planned. It does not start anything: it goes on the "
          "queue in the manager dashboard for the user to approve or dismiss. The reason it "
          "waits is that you cannot see whether the fix is deployed to the environment under "
          "test, and 'closed' in a tracker covers fixed, duplicate and will-not-do.",
          {"type": "object",
           "properties": {
               "app": {"type": "string", "description": "Role or package"},
               "module": {"type": "string", "description": "Module slug"},
               "finding": {"type": "string", "description": "Finding id, e.g. F007"},
               "reason": {"type": "string",
                          "description": "Why this needs re-testing, in a line"},
               "instruction": {"type": "string",
                               "description": "What the re-test should check, if approved"},
           },
           "required": ["app", "module", "finding", "reason"], "additionalProperties": False})
    async def queue_retest(args: dict[str, Any]) -> dict[str, Any]:
        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")
        slug = str(args.get("module") or "").strip()
        finding_id = str(args.get("finding") or "").strip()
        findings = await asyncio.to_thread(store.list_findings, member["package"], slug)
        finding = next((f for f in findings if f.get("id") == finding_id), None)
        if finding is None:
            known = ", ".join(f["id"] for f in findings) or "none"
            return _err(f"No finding {finding_id!r} in {member['role']}/{slug}. It has: {known}.")

        entry = await asyncio.to_thread(
            retests_mod.queue, name, member["package"], slug, finding_id,
            role=member["role"], title=str(finding.get("title") or ""),
            reason=str(args.get("reason") or ""),
            issue_id=finding.get("issue_id"), issue_url=str(finding.get("issue_url") or ""),
            instruction=str(args.get("instruction") or ""))
        if entry is None:
            return _ok(f"{member['role']}/{slug}/{finding_id} is already on the re-test queue "
                       f"— not added twice.")
        return _ok(f"Queued a re-test of {member['role']}/{slug}/{finding_id} for approval. It "
                   f"has NOT started; it is waiting for the user in the manager dashboard.")

    @tool("list_retests",
          "The re-test queue: what is waiting for the user's approval, what they approved and "
          "what they dismissed. Read it before queueing, and before claiming a defect is "
          "confirmed fixed — 'closed in Blackcode' and 'checked on the device' are different "
          "claims, and only an approved re-test that ran turns one into the other.",
          {"type": "object",
           "properties": {"status": {"type": "string",
                                     "enum": ["pending", "approved", "dismissed"]}},
           "additionalProperties": False})
    async def list_retests(args: dict[str, Any]) -> dict[str, Any]:
        rows = await asyncio.to_thread(retests_mod.list_queued, name, args.get("status"))
        if not rows:
            return _ok("Nothing on the re-test queue.")
        return _ok("\n".join(
            f"[{r['status']}] {r['role']}/{r['module']}/{r['finding']} — {r['title']}"
            + (f"  (#{r['issue_id']})" if r.get("issue_id") else "")
            + f"\n      {r['reason']}" for r in rows))

    # -- the hardware under the runs --------------------------------------------------------
    #
    # `run_module` answers "may this run take the target?". These answer the question
    # underneath: is there a target at all? A missing stack does not refuse a run — the run
    # starts, the first device tool times out, and the transcript fills with an agent
    # reasoning about a broken app when WebDriverAgent was simply never launched.
    def _platform_devices(platform: str) -> list[dict[str, str]]:
        from backend import agent_bridge

        return [d for d in agent_bridge.attached()
                if (d.get("platform") or "").lower() == (platform or "").lower()]

    def _wrong_kind(role: str, device: dict[str, str]) -> Optional[str]:
        """Why this device contradicts what the role says it should be, or None.

        The roles in this product are named by the user — `doctor-ipad`, `patient-ios` — and
        when one of them names a device kind out loud, that is a fact worth checking against
        the hardware. It catches the specific accident that only one iOS device being attached
        makes likely: auto-pinning the iPad to the iPhone's project because it was the only
        thing there. A role that names no kind (`patient-ios`) is not second-guessed.
        """
        model = str(device.get("model") or device.get("label") or "").lower()
        for kind, other in (("ipad", "iphone"), ("iphone", "ipad")):
            if kind in role.lower() and other in model and kind not in model:
                return (f"{device['serial']} is {device.get('label') or device.get('model')}, "
                        f"and the app is called {role!r}. Nothing was pinned or started — "
                        f"pin_device will do it anyway if that really is the right device.")
        return None

    @tool("list_devices",
          "Every device attached to this machine right now, which app each is pinned to, and "
          "whether that platform's stack is up. Read it before starting anything: an app whose "
          "device is not attached cannot run, and an iPad and an iPhone are both `ios` with "
          "identically-shaped UDIDs, so an unpinned run on one can silently drive the other.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def list_devices(_args: dict[str, Any]) -> dict[str, Any]:
        import stacks
        from backend import agent_bridge
        from backend import projects as backend_projects

        found = await asyncio.to_thread(agent_bridge.attached)
        members = await asyncio.to_thread(ecosystem_mod.members, name)
        pins: dict[str, list[str]] = {}
        for member in members:
            serial = (backend_projects.read_meta(member["package"]) or {}).get("device_serial")
            if serial:
                pins.setdefault(str(serial), []).append(member["role"])

        out = ["## Attached devices"]
        if not found:
            out.append("None. Nothing on a device can run until one is plugged in, unlocked "
                       "and authorised.")
        for device in found:
            owners = pins.get(device["serial"], [])
            out.append(f"{device['serial']}  [{device.get('platform', '?')}]  "
                       f"{device.get('label') or device.get('model', '')}"
                       + (f"  -> pinned to {', '.join(owners)}" if owners
                          else "  -> not pinned to any app"))

        platforms = sorted({str(m.get("platform") or "").lower() for m in members
                            if m.get("platform")})
        out += ["", "## Stacks"]
        for row in await asyncio.to_thread(stacks.status_all, platforms):
            out.append(f"{row['platform']}: {'READY' if row['ready'] else 'not ready'} — "
                       f"{row['detail']}")
            if not row["ready"] and row["fix"]:
                out.append(f"      fix: {row['fix']}")

        out += ["", "## Apps and the device each would use"]
        for member in members:
            serial = (backend_projects.read_meta(member["package"]) or {}).get("device_serial")
            if (member.get("platform") or "").lower() == "web":
                where = "no device — the target is the URL"
            elif serial:
                attached_now = any(d["serial"] == serial for d in found)
                where = f"pinned to {serial}" + ("" if attached_now else "  (NOT attached)")
            else:
                same = _platform_devices(str(member.get("platform") or ""))
                where = (f"unpinned — would take {same[0]['serial']}" if len(same) == 1
                         else f"unpinned, {len(same)} {member.get('platform')} device(s) "
                              f"attached" if same else "unpinned and nothing attached")
            out.append(f"{member['role']}  ({member.get('platform')})  |  {where}")
        return _ok("\n".join(out))

    @tool("pin_device",
          "Pin one app to one device, so its runs always land on that hardware. Needed as soon "
          "as two devices of the same kind are attached: the iPad and the iPhone are both "
          "`ios`, and without a pin whichever the adapter finds first gets driven. Pass an "
          "empty serial to unpin.",
          {"type": "object",
           "properties": {
               "app": {"type": "string", "description": "Role or package"},
               "serial": {"type": "string",
                          "description": "Device serial/UDID from list_devices, or empty to "
                                         "unpin"},
           },
           "required": ["app"], "additionalProperties": False})
    async def pin_device(args: dict[str, Any]) -> dict[str, Any]:
        from backend import agent_bridge
        from backend import projects as backend_projects

        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")
        if (member.get("platform") or "").lower() == "web":
            return _err(f"{member['role']} is a website. Its target is its URL, not a device — "
                        f"there is nothing to pin.")

        serial = str(args.get("serial") or "").strip()
        if not serial:
            await asyncio.to_thread(backend_projects.write_meta, member["package"],
                                    device_serial=None)
            agent_bridge.forget_attached()
            return _ok(f"Unpinned {member['role']}. Its runs will take whichever "
                       f"{member.get('platform')} device is attached.")

        found = await asyncio.to_thread(agent_bridge.attached)
        match = next((d for d in found if d["serial"] == serial), None)
        if match is None:
            known = ", ".join(d["serial"] for d in found) or "none attached"
            return _err(f"{serial!r} is not attached. Attached: {known}. Pinning to a device "
                        f"that is not here would leave the app unable to run with no clue why.")
        if (match.get("platform") or "").lower() != (member.get("platform") or "").lower():
            return _err(f"{serial} is a {match.get('platform')} device and {member['role']} is "
                        f"{member.get('platform')}. Nothing was pinned.")

        await asyncio.to_thread(backend_projects.write_meta, member["package"],
                                device_serial=serial)
        agent_bridge.forget_attached()
        return _ok(f"Pinned {member['role']} to {serial} "
                   f"({match.get('label') or match.get('model', '')}). Every run of that app "
                   f"now lands on that device.")

    @tool("start_app",
          "Bring up everything one app needs before it can be tested: its platform's stack, "
          "and its device. This is what 'start the iPad app' means — on iOS it launches the "
          "tunnel, the WebDriverAgent runner and the port forward in their own windows (expect "
          "a UAC prompt, and 30-90 seconds), on Android it makes sure adb's daemon is up, and "
          "on the web it confirms Playwright and Chromium are installed since a browser is "
          "launched per run rather than started once. It starts no test: call run_module after "
          "it reports ready. Several apps can be up at once — that is the point — except that "
          "two iOS devices cannot, because there is one WebDriverAgent port.",
          {"type": "object",
           "properties": {
               "app": {"type": "string", "description": "Role (e.g. doctor-ipad) or package"},
           },
           "required": ["app"], "additionalProperties": False})
    async def start_app(args: dict[str, Any]) -> dict[str, Any]:
        import stacks
        from backend import agent_bridge
        from backend import projects as backend_projects

        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")
        platform = str(member.get("platform") or "").lower()
        pinned = (backend_projects.read_meta(member["package"]) or {}).get("device_serial")

        # Pin first, so the stack is started for the device the runs will actually use. Only
        # when it is unambiguous: with an iPad and an iPhone both attached, guessing which one
        # "doctor-ipad" meant is exactly the mistake pinning exists to prevent.
        note = ""
        if platform not in ("web", "") and not pinned:
            same = _platform_devices(platform)
            if len(same) == 1:
                mismatch = _wrong_kind(member["role"], same[0])
                if mismatch:
                    return _err(mismatch)
                await asyncio.to_thread(backend_projects.write_meta, member["package"],
                                        device_serial=same[0]["serial"])
                agent_bridge.forget_attached()
                pinned = same[0]["serial"]
                note = (f"\nPinned {member['role']} to the only {platform} device attached, "
                        f"{pinned} ({same[0].get('label') or ''}).")
            elif len(same) > 1:
                listing = "; ".join(f"{d['serial']} ({d.get('label') or d.get('model', '')})"
                                    for d in same)
                return _err(
                    f"{len(same)} {platform} devices are attached and {member['role']} is not "
                    f"pinned to one: {listing}. Nothing was started — starting a stack for the "
                    f"wrong one would drive the wrong device and report it as this app. Pin it "
                    f"with pin_device first.")

        result = await asyncio.to_thread(stacks.start, platform, pinned)

        head = (f"{member['role']} ({platform})"
                + (f" on {pinned}" if pinned else "")
                + f": {'READY' if result['ready'] else 'not ready yet'}")
        lines = [head, result["detail"], result.get("note", "")]
        if not result["ready"] and result.get("fix"):
            lines.append(f"Fix: {result['fix']}")
        if note:
            lines.append(note.strip())
        if result["ready"]:
            mods = await asyncio.to_thread(store.list_subprojects, member["package"])
            runnable = [str(m.get("slug")) for m in mods
                        if not store.is_main_slug(str(m.get("slug") or ""))]
            lines.append("Modules you can run now: "
                         + (", ".join(runnable) if runnable else
                            "none yet — create_module first."))
        elif result.get("starting"):
            lines.append("Ask me to check again in a minute (list_devices), or watch the "
                         "runner window. Do not start a run until it reports READY: it would "
                         "not fail cleanly, it would time out one tool at a time.")
        return _ok("\n".join(line for line in lines if line))

    @tool("running_now",
          "What is running this second: which modules have a turn in flight, and which targets "
          "are locked and by whom. Read it before starting a run and whenever the user asks "
          "what is happening — runs proceed between your turns, and a target that was free "
          "when you last looked may not be.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def running_now(_args: dict[str, Any]) -> dict[str, Any]:
        import device_locks
        from backend import agent_bridge

        live = [s for s in agent_bridge.sessions.status() if s.get("busy")]
        locks = device_locks.held()
        if not live and not locks:
            return _ok("Nothing is running. Every target is free.")

        out = []
        if live:
            out.append("## Running")
            for entry in live:
                role = ecosystem_mod.role_of(str(entry["package"])) or entry["package"]
                out.append(f"{role}/{entry['slug']}"
                           + (f"  — {entry['activity']}" if entry.get("activity") else "")
                           + ("  [waiting on a question from the user]"
                              if entry.get("blocked") else ""))
        if locks:
            out.append("")
            out.append("## Targets taken")
            for key, holder in sorted(locks.items()):
                role = ecosystem_mod.role_of(str(holder["package"])) or holder["package"]
                out.append(f"{key}  <- {role}/{holder['slug']}  since {holder['since']}")
        return _ok("\n".join(out))

    @tool("stop_module",
          "Stop a run. Use it for a run that is going the wrong way or one the user asks you to "
          "end — you started it, so you can end it. It stops the turn and frees the target; it "
          "does not undo anything the module already filed, and the transcript stays.",
          {"type": "object",
           "properties": {
               "app": {"type": "string", "description": "Role or package"},
               "module": {"type": "string", "description": "Module slug"},
           },
           "required": ["app", "module"], "additionalProperties": False})
    async def stop_module(args: dict[str, Any]) -> dict[str, Any]:
        from backend import agent_bridge

        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")
        slug = str(args.get("module") or "").strip()
        session = agent_bridge.sessions.peek(member["package"], slug)
        if session is None or not session.busy:
            return _ok(f"{member['role']}/{slug} was not running. Nothing to stop.")
        stopped = await session.interrupt()
        return _ok(f"{'Stopped' if stopped else 'Asked to stop'} {member['role']}/{slug}. It "
                   f"finishes the tool it is in and then ends the turn; whatever it already "
                   f"filed stays filed.")

    # -- jobs: a sweep of one app, or a journey across several -----------------------------
    #
    # One object underneath (`campaigns`), because a journey is a sweep whose steps stopped
    # agreeing about which app they are in, and everything hard — ordering, pausing, failure,
    # what the board shows — is the same for both.
    def _preflight(packages: list[str]) -> Optional[str]:
        """Every app this job will touch, checked before step one. None when all are ready.

        Up front rather than per step, because the failure it prevents is the expensive one:
        finding out at step three that the iPad was never reachable, with the booking already
        made and not repeatable.
        """
        import stacks

        members = {m["package"]: m for m in ecosystem_mod.members(name)}
        problems = []
        for package in packages:
            platform = str((members.get(package) or {}).get("platform") or "")
            row = stacks.status(platform)
            if not row["ready"]:
                role = (members.get(package) or {}).get("role", package)
                problems.append(f"{role} ({platform}): {row['detail']}"
                                + (f" Fix: {row['fix']}" if row.get("fix") else ""))
        if not problems:
            return None
        return ("Nothing was started — these have to be ready first, and a device that is "
                "asleep or locked looks exactly like a broken app once a run is under way:\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\n\nBring them up with start_app, and tell the user plainly which device "
                  "needs plugging in, waking or unlocking.")

    def _resolve_step(raw: dict[str, Any]) -> tuple[Optional[dict[str, Any]], str]:
        """One journey step, resolved against the store. (step, error)."""
        member = _resolve_app(str(raw.get("app") or ""))
        if member is None:
            return None, f"no app {raw.get('app')!r} — the apps are: {_app_names()}"
        slug = str(raw.get("module") or "").strip()
        entry = store.get_subproject(member["package"], slug)
        if entry is None:
            known = [str(s.get("slug")) for s in store.list_subprojects(member["package"])]
            return None, (f"{member['role']} has no module {slug!r} — it has: "
                          + (", ".join(known) or "none"))
        return {"package": member["package"], "role": member["role"], "module": slug,
                "title": str(entry.get("title") or slug),
                "scope": str(entry.get("scope") or ""),
                "expect": str(raw.get("expect") or "")}, ""

    @tool("test_app",
          "Sweep one app: every module, in order, without coming back to the user between "
          "them. This is the answer to \"test the clinic web\". You are handed a turn as each "
          "module ends — with what it filed and the shared scratchpad — and the next module "
          "starts when that turn finishes, so you are between every step without having to "
          "remember to be. For work that spans more than one app, use run_journey instead.",
          {"type": "object",
           "properties": {
               "app": {"type": "string", "description": "Role (e.g. clinic-web) or package"},
               "modules": {"type": "array", "items": {"type": "string"},
                           "description": "Module slugs, in the order to run them. Omit to "
                                          "take every module the app has, manager excluded."},
               "only_untested": {"type": "boolean",
                                 "description": "Skip modules already marked tested — for "
                                                "filling coverage gaps rather than redoing "
                                                "the app."},
               "goal": {"type": "string",
                        "description": "What this sweep is for, in a line — quoted to every "
                                       "module so each knows what it is part of"},
               "instruction": {"type": "string",
                               "description": "Anything every module should be told: an "
                                              "environment, a login, a defect to watch for"},
           },
           "required": ["app"], "additionalProperties": False})
    async def test_app(args: dict[str, Any]) -> dict[str, Any]:
        import campaigns as campaigns_mod
        from backend.campaign_runner import runner

        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")

        problem = await asyncio.to_thread(_preflight, [member["package"]])
        if problem:
            return _err(problem)

        entries = await asyncio.to_thread(store.list_subprojects, member["package"])
        by_slug = {str(e.get("slug")): e for e in entries
                   if not store.is_main_slug(str(e.get("slug") or ""))}

        wanted = [str(s).strip() for s in (args.get("modules") or []) if str(s).strip()]
        if wanted:
            missing = [s for s in wanted if s not in by_slug]
            if missing:
                return _err(f"{member['role']} has no module(s) {', '.join(missing)}. It has: "
                            f"{', '.join(by_slug) or 'none'}. Nothing was started.")
            chosen = wanted
        else:
            chosen = list(by_slug)
        if args.get("only_untested"):
            chosen = [s for s in chosen if by_slug[s].get("status") != "tested"]
        if not chosen:
            return _err(f"No modules to run for {member['role']}. Either it has none — create "
                        f"some with create_module, or run recon — or `only_untested` filtered "
                        f"them all out because every module is already marked tested.")

        steps = [{"package": member["package"], "role": member["role"], "module": s,
                  "title": str(by_slug[s].get("title") or s),
                  "scope": str(by_slug[s].get("scope") or ""), "expect": ""} for s in chosen]
        try:
            campaign = await asyncio.to_thread(
                campaigns_mod.create, name, steps, kind="sweep", role=member["role"],
                goal=str(args.get("goal") or ""),
                instruction=str(args.get("instruction") or ""))
        except ValueError as exc:
            return _err(str(exc))

        asyncio.create_task(runner.advance(campaign["id"]))
        return _ok(
            f"Started a sweep of {member['role']} — {len(steps)} modules, in this order: "
            f"{', '.join(chosen)}.\n\nStep one is starting now. You will be handed a turn as "
            f"each module ends; the next one begins when that turn finishes. Do not poll it.")

    @tool("run_journey",
          "Run a job whose steps are in DIFFERENT apps and only mean anything together — "
          "\"book on the patient app, then check it reached the iPad, then check the clinic "
          "web\". This is the thing no single project can do and no single module can verify.\n\n"
          "Plan it first, in this call: each step names an app, a module, and — the important "
          "part — what that step must ESTABLISH for the next one. Step one is told to write "
          "what it created into the shared scratchpad; step two is handed that verbatim, so it "
          "looks for the actual appointment rather than for any appointment. A journey without "
          "`expect` on each step is just several unrelated tests in a row.\n\n"
          "Every app it names is checked for a ready device before anything starts, and every "
          "one is held for the whole journey so a sweep cannot take one halfway through.",
          {"type": "object",
           "properties": {
               "goal": {"type": "string",
                        "description": "The one thing this journey proves or disproves"},
               "steps": {
                   "type": "array",
                   "description": "In order. Each is one module in one app.",
                   "items": {"type": "object",
                             "properties": {
                                 "app": {"type": "string", "description": "Role or package"},
                                 "module": {"type": "string", "description": "Module slug"},
                                 "expect": {"type": "string",
                                            "description": "What this step must establish, and "
                                                           "what it should write down for the "
                                                           "next one"}},
                             "required": ["app", "module", "expect"],
                             "additionalProperties": False}},
               "instruction": {"type": "string",
                               "description": "Anything every step should be told"},
           },
           "required": ["goal", "steps"], "additionalProperties": False})
    async def run_journey(args: dict[str, Any]) -> dict[str, Any]:
        import campaigns as campaigns_mod
        from backend.campaign_runner import runner

        raw_steps = args.get("steps") or []
        if len(raw_steps) < 2:
            return _err("A journey needs at least two steps in different apps — otherwise it "
                        "is a single module run, which is what run_module is for.")

        steps, errors = [], []
        for index, raw in enumerate(raw_steps, start=1):
            step, problem = await asyncio.to_thread(_resolve_step, raw)
            if problem:
                errors.append(f"step {index}: {problem}")
            else:
                steps.append(step)
        if errors:
            return _err("Nothing was started:\n" + "\n".join(f"- {e}" for e in errors))

        packages = list(dict.fromkeys(s["package"] for s in steps))
        problem = await asyncio.to_thread(_preflight, packages)
        if problem:
            return _err(problem)

        try:
            campaign = await asyncio.to_thread(
                campaigns_mod.create, name, steps, kind="journey",
                role=" -> ".join(dict.fromkeys(s["role"] for s in steps)),
                goal=str(args.get("goal") or ""),
                instruction=str(args.get("instruction") or ""))
        except ValueError as exc:
            return _err(str(exc))

        asyncio.create_task(runner.advance(campaign["id"]))
        plan = "\n".join(f"  {i}. {s['role']}/{s['module']} — {s['expect']}"
                         for i, s in enumerate(steps, start=1))
        return _ok(
            f"Journey planned and started: {args.get('goal')}\n\n{plan}\n\n"
            f"Apps held for the whole journey: {', '.join(packages)}. Step one is running. "
            f"Show the user this plan, then wait — you are handed a turn as each step ends, "
            f"with what it established and the scratchpad, and the next step starts when that "
            f"turn finishes.")

    @tool("campaign_status",
          "Where the jobs are up to: which step is running, in which app, what each filed, and "
          "anything a job is waiting on. Read it when the user asks how testing is going — not "
          "on a loop, since you are handed a turn when something changes.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def campaign_status(_args: dict[str, Any]) -> dict[str, Any]:
        import campaigns as campaigns_mod

        rows = await asyncio.to_thread(campaigns_mod.list_all, name)
        if not rows:
            return _ok("No job has run yet. `test_app` sweeps one app; `run_journey` spans "
                       "several.")
        out = []
        for campaign in rows[:8]:
            counts = campaigns_mod.progress(campaign)
            out.append(f"# {campaign.get('kind', 'sweep')}: "
                       f"{campaign.get('role') or ', '.join(counts['apps'])} — "
                       f"{campaign['status']}  ({counts['finished']}/{counts['total']} steps, "
                       f"{counts['findings']} findings)")
            if campaign.get("goal"):
                out.append(f"  goal: {campaign['goal']}")
            if campaign.get("blocked"):
                blocked = campaign["blocked"]
                out.append(f"  !! waiting: {blocked.get('reason')}"
                           + (f" — {blocked.get('module')}" if blocked.get("module") else ""))
                if blocked.get("question"):
                    out.append(f"     question: {blocked['question']}")
            for step in campaign["steps"]:
                mark = {"running": "->", "done": "ok", "failed": "!!",
                        "skipped": "--", "pending": "  "}.get(step["status"], "  ")
                out.append(f"  {mark} {step.get('role') or step['package']}/{step['module']}  "
                           f"[{step['status']}]"
                           + (f"  {step['findings']} findings" if step.get("findings") else "")
                           + (f"  ({step['note']})" if step.get("note") else ""))
                if step.get("reported"):
                    out.append(f"       said: {step['reported'][:200]}")
        return _ok("\n".join(out))

    @tool("control_campaign",
          "Change a running job: 'resume' after a pause, 'stop' to end it, 'skip' a step whose "
          "premise no longer holds, or 'retry' a step that failed for a reason you have since "
          "fixed. Retry is the one to reach for after bringing a stack back up — a failed step "
          "is not a dead job.",
          {"type": "object",
           "properties": {
               "app": {"type": "string",
                       "description": "Role or package of any app the job touches"},
               "action": {"type": "string", "enum": ["resume", "stop", "skip", "retry"]},
               "module": {"type": "string",
                          "description": "Which step. Required for skip and retry."},
           },
           "required": ["app", "action"], "additionalProperties": False})
    async def control_campaign(args: dict[str, Any]) -> dict[str, Any]:
        import campaigns as campaigns_mod
        from backend.campaign_runner import runner

        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")
        campaign = await asyncio.to_thread(campaigns_mod.active_for, member["package"])
        if campaign is None:
            return _err(f"No job is live on {member['role']}. `test_app` sweeps one app; "
                        f"`run_journey` spans several.")

        action = str(args.get("action") or "")
        slug = str(args.get("module") or "").strip()

        if action == "stop":
            await asyncio.to_thread(campaigns_mod.set_status, campaign["id"], "stopped")
            return _ok(f"Stopped the {campaign.get('kind', 'job')}. Whatever the steps already "
                       f"filed stays filed; nothing further starts. A step mid-run finishes "
                       f"its own turn — use stop_module to cut that short.")
        if action == "skip":
            if not slug:
                return _err("Say which step to skip.")
            await asyncio.to_thread(campaigns_mod.skip_step, campaign["id"], slug,
                                    "skipped by the manager")
            return _ok(f"`{slug}` will be skipped. The job carries on with the rest.")
        if action == "retry":
            if not slug:
                return _err("Say which step to retry.")
            # Checked *before* the mutation, not after. `retry_step` moves a failed step back
            # to pending, so "is it pending now" is also true of a step that was never run —
            # which reported a successful retry of something that had not happened yet.
            was = next((s for s in campaign["steps"] if s["module"] == slug), None)
            if was is None:
                return _err(f"`{slug}` is not a step of this job. It has: "
                            + ", ".join(s["module"] for s in campaign["steps"]) + ".")
            if was["status"] != "failed":
                return _err(f"`{slug}` is {was['status']}, not failed, so there is nothing to "
                            f"retry. Only a step that ran and failed can be put back.")
            await asyncio.to_thread(campaigns_mod.retry_step, campaign["id"], slug)
            asyncio.create_task(runner.advance(campaign["id"]))
            return _ok(f"`{slug}` is queued again and the job is moving. If the reason it "
                       f"failed is still true it will fail the same way — make sure you fixed "
                       f"it first.")

        asyncio.create_task(runner.advance(campaign["id"]))
        return _ok(f"Resuming. The next pending step starts as soon as its target is free.")

    @tool("set_step_brief",
          "Tell a step that has not run yet what to look for, now that you have read the one "
          "before it. This is how a journey actually becomes one job rather than several: step "
          "one reports a booking reference, and you write it into step two's brief before step "
          "two starts.",
          {"type": "object",
           "properties": {
               "app": {"type": "string", "description": "Role or package the job touches"},
               "module": {"type": "string", "description": "The step to redirect"},
               "expect": {"type": "string",
                          "description": "What that step must now establish, in full"},
           },
           "required": ["app", "module", "expect"], "additionalProperties": False})
    async def set_step_brief(args: dict[str, Any]) -> dict[str, Any]:
        import campaigns as campaigns_mod

        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")
        campaign = await asyncio.to_thread(campaigns_mod.active_for, member["package"])
        if campaign is None:
            return _err(f"No job is live on {member['role']}.")
        slug = str(args.get("module") or "").strip()
        updated = await asyncio.to_thread(campaigns_mod.set_step_brief, campaign["id"], slug,
                                          str(args.get("expect") or ""))
        if updated is None or not any(
                s["module"] == slug and s["expect"] == str(args.get("expect") or "")
                for s in updated["steps"]):
            return _err(f"`{slug}` is not a step of this job that is still waiting to run. A "
                        f"step that has already started cannot be re-briefed.")
        return _ok(f"`{slug}` will be told: {args.get('expect')}")

    # -- the shared scratchpad ---------------------------------------------------------------
    @tool("note_put",
          "Write a fact onto the product's shared scratchpad, where every app's agents can read "
          "it. The one thing that crosses between apps: a module in another project cannot see "
          "your chat, your findings or your screen, but it can read this. Use it for what is "
          "true right now — a booking reference, a test account, which environment is under "
          "test — not for findings, which belong to the module that observed them.",
          {"type": "object",
           "properties": {
               "key": {"type": "string",
                       "description": "Short kebab-case name, e.g. last-booking-ref"},
               "value": {"type": "string", "description": "The fact, in a line or two"},
               "note": {"type": "string", "description": "Why it is here, if not obvious"},
           },
           "required": ["key", "value"], "additionalProperties": False})
    async def note_put(args: dict[str, Any]) -> dict[str, Any]:
        import scratchpad as scratchpad_mod

        try:
            entry = await asyncio.to_thread(
                scratchpad_mod.put, name, str(args.get("key") or ""),
                str(args.get("value") or ""), author=f"{name}/manager",
                note=str(args.get("note") or ""))
        except ValueError as exc:
            return _err(str(exc))
        return _ok(f"Noted `{entry['key']}`. Every app's agents can read it now.")

    @tool("note_list",
          "Everything on the shared scratchpad, newest first — what the apps have written down "
          "for each other. Read it before starting a journey and before answering a question "
          "about what state the product is in.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def note_list(_args: dict[str, Any]) -> dict[str, Any]:
        import scratchpad as scratchpad_mod

        rows = await asyncio.to_thread(scratchpad_mod.list_all, name)
        if not rows:
            return _ok("The shared scratchpad is empty.")
        return _ok("\n".join(
            f"{r['key']}: {r['value']}"
            + (f"   [{r['author']}, {r['updated_at']}]" if r.get("author") else "")
            + (f"\n      {r['note']}" if r.get("note") else "") for r in rows))

    @tool("note_drop",
          "Remove a note that has stopped being true. Do this when a job ends — a stale "
          "booking reference that another agent reads next week is worse than no note at all, "
          "because it looks like a fact.",
          {"type": "object",
           "properties": {"key": {"type": "string", "description": "The note's key"}},
           "required": ["key"], "additionalProperties": False})
    async def note_drop(args: dict[str, Any]) -> dict[str, Any]:
        import scratchpad as scratchpad_mod

        gone = await asyncio.to_thread(scratchpad_mod.drop, name, str(args.get("key") or ""))
        return _ok(f"Dropped `{args.get('key')}`." if gone
                   else f"No note called `{args.get('key')}`.")

    # -- the files ---------------------------------------------------------------------------
    #
    # Named operations over a closed set of roots rather than a shell — see manager_fs's
    # header for why. Nothing here deletes: `trash_path` retires into `projects/_trash/`.
    def _fs(call, *call_args) -> dict[str, Any]:
        import manager_fs

        try:
            return {"ok": True, "result": call(*call_args)}
        except manager_fs.FsRefused as exc:
            return {"ok": False, "result": str(exc)}

    @tool("list_dir",
          "List a folder — what is in it, which entries are folders, and when each changed. "
          "The way to look around before moving anything. Restricted to the harness tree, the "
          "project roots, and anything named in QA_MANAGER_FS_ROOTS; a path outside them is "
          "refused and the refusal names the roots.",
          {"type": "object",
           "properties": {"path": {"type": "string", "description": "Absolute folder path"}},
           "required": ["path"], "additionalProperties": False})
    async def list_dir(args: dict[str, Any]) -> dict[str, Any]:
        import manager_fs

        outcome = await asyncio.to_thread(_fs, manager_fs.list_dir, str(args.get("path") or ""))
        if not outcome["ok"]:
            return _err(str(outcome["result"]))
        data = outcome["result"]
        if not data["is_dir"]:
            return _ok(f"{data['path']} is a file, {data['size']} bytes. Read it with Read.")
        if not data["entries"]:
            return _ok(f"{data['path']} is empty.")
        rows = [f"{'DIR ' if e['is_dir'] else '    '}{e['name']:<48} "
                f"{'' if e['is_dir'] else str(e['size']):>10}  {e['modified']}"
                for e in data["entries"]]
        return _ok(f"{data['path']}  ({len(rows)} entries)\n" + "\n".join(rows))

    @tool("make_dir",
          "Create a folder, and any parent folders it needs. Idempotent — an existing folder is "
          "reported as such rather than treated as an error.",
          {"type": "object",
           "properties": {"path": {"type": "string", "description": "Absolute folder path"}},
           "required": ["path"], "additionalProperties": False})
    async def make_dir(args: dict[str, Any]) -> dict[str, Any]:
        import manager_fs

        outcome = await asyncio.to_thread(_fs, manager_fs.make_dir, str(args.get("path") or ""))
        if not outcome["ok"]:
            return _err(str(outcome["result"]))
        data = outcome["result"]
        return _ok(f"{data['path']} — created: {data['created']}")

    @tool("move_path",
          "Move or rename a file or folder. A destination that is an existing folder means "
          "'into it'; a destination that already exists as a file is refused rather than "
          "overwritten, because that is the one outcome nobody can undo from this chat. The "
          "harness source and any git storage are refused too — moving the code this call is "
          "running out of takes away the session you are speaking through.",
          {"type": "object",
           "properties": {
               "source": {"type": "string", "description": "Absolute path to move"},
               "destination": {"type": "string",
                               "description": "Absolute destination path or folder"},
           },
           "required": ["source", "destination"], "additionalProperties": False})
    async def move_path(args: dict[str, Any]) -> dict[str, Any]:
        import manager_fs

        outcome = await asyncio.to_thread(_fs, manager_fs.move,
                                          str(args.get("source") or ""),
                                          str(args.get("destination") or ""))
        if not outcome["ok"]:
            return _err(str(outcome["result"]))
        return _ok(f"Moved {outcome['result']['from']} -> {outcome['result']['to']}")

    @tool("copy_path",
          "Copy a file or a whole folder. Same destination rules as move_path, and it never "
          "overwrites. Use it to hand someone a copy of evidence without taking it out of the "
          "project it belongs to.",
          {"type": "object",
           "properties": {
               "source": {"type": "string", "description": "Absolute path to copy"},
               "destination": {"type": "string",
                               "description": "Absolute destination path or folder"},
           },
           "required": ["source", "destination"], "additionalProperties": False})
    async def copy_path(args: dict[str, Any]) -> dict[str, Any]:
        import manager_fs

        outcome = await asyncio.to_thread(_fs, manager_fs.copy,
                                          str(args.get("source") or ""),
                                          str(args.get("destination") or ""))
        if not outcome["ok"]:
            return _err(str(outcome["result"]))
        return _ok(f"Copied {outcome['result']['from']} -> {outcome['result']['to']}")

    @tool("trash_path",
          "Retire a file or folder into projects/_trash/<timestamp>/. There is no delete here "
          "and there will not be: a test history that an agent removed at 2am because a folder "
          "looked empty must be recoverable by hand. Say 'moved to the trash folder', not "
          "'deleted', when you report it.",
          {"type": "object",
           "properties": {"path": {"type": "string", "description": "Absolute path to retire"}},
           "required": ["path"], "additionalProperties": False})
    async def trash_path(args: dict[str, Any]) -> dict[str, Any]:
        import manager_fs

        outcome = await asyncio.to_thread(_fs, manager_fs.trash, str(args.get("path") or ""))
        if not outcome["ok"]:
            return _err(str(outcome["result"]))
        return _ok(f"Moved {outcome['result']['from']} to the trash folder at "
                   f"{outcome['result']['to']}. Nothing was deleted — it can be moved back.")

    return create_sdk_mcp_server(
        name="ecosystem",
        version="1.0.0",
        tools=[list_apps, read_app, read_finding, unclustered_defects,
               list_clusters, read_cluster, save_cluster, delete_cluster,
               ecosystem_report, create_module, update_module,
               search_issues, file_cluster, link_cluster, sync_issue_status,
               run_module, queue_retest, list_retests,
               list_devices, pin_device, start_app, running_now, stop_module,
               test_app, run_journey, campaign_status, control_campaign,
               set_step_brief, note_put, note_list, note_drop,
               list_dir, make_dir, move_path, copy_path, trash_path],
    )


# Tool names as the agent sees them, for the allow-list in runtime.py. Kept in step with the
# `tools=[...]` list above and with the prompt section that describes them — a tool the prompt
# names but the session lacks costs a turn discovering it is absent, and one the session has
# but the prompt does not name is an invitation to reach for something undescribed.
ECOSYSTEM_TOOL_NAMES = [
    "mcp__ecosystem__list_apps",
    "mcp__ecosystem__read_app",
    "mcp__ecosystem__read_finding",
    "mcp__ecosystem__unclustered_defects",
    "mcp__ecosystem__list_clusters",
    "mcp__ecosystem__read_cluster",
    "mcp__ecosystem__save_cluster",
    "mcp__ecosystem__delete_cluster",
    "mcp__ecosystem__ecosystem_report",
    "mcp__ecosystem__create_module",
    "mcp__ecosystem__update_module",
    "mcp__ecosystem__search_issues",
    "mcp__ecosystem__file_cluster",
    "mcp__ecosystem__link_cluster",
    "mcp__ecosystem__sync_issue_status",
    "mcp__ecosystem__run_module",
    "mcp__ecosystem__queue_retest",
    "mcp__ecosystem__list_retests",
    "mcp__ecosystem__list_devices",
    "mcp__ecosystem__pin_device",
    "mcp__ecosystem__start_app",
    "mcp__ecosystem__running_now",
    "mcp__ecosystem__stop_module",
    "mcp__ecosystem__test_app",
    "mcp__ecosystem__run_journey",
    "mcp__ecosystem__campaign_status",
    "mcp__ecosystem__control_campaign",
    "mcp__ecosystem__set_step_brief",
    "mcp__ecosystem__note_put",
    "mcp__ecosystem__note_list",
    "mcp__ecosystem__note_drop",
    "mcp__ecosystem__list_dir",
    "mcp__ecosystem__make_dir",
    "mcp__ecosystem__move_path",
    "mcp__ecosystem__copy_path",
    "mcp__ecosystem__trash_path",
]


def ecosystem_tool_names() -> list[str]:
    """A copy, so a caller extending its own allow-list cannot append to this list."""
    return list(ECOSYSTEM_TOOL_NAMES)
