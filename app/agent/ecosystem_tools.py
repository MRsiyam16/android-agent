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
        from backend import agent_bridge

        member = await asyncio.to_thread(_resolve_app, str(args.get("app") or ""))
        if member is None:
            return _err(f"No app {args.get('app')!r} in {name!r}. The apps are: {_app_names()}.")
        slug = str(args.get("module") or "").strip()
        instruction = str(args.get("instruction") or "").strip()
        if not instruction:
            return _err("A run needs an instruction — say what it should establish.")

        try:
            started = agent_bridge.start_run(member["package"], slug, instruction)
        except agent_bridge.RunRefused as exc:
            return _err(str(exc))
        return _ok(f"Started {member['role']}/{slug} on {started['target']}. It runs in that "
                   f"project's own session — watch it there, or ask me again later and I will "
                   f"read what it filed.")

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

    return create_sdk_mcp_server(
        name="ecosystem",
        version="1.0.0",
        tools=[list_apps, read_app, read_finding, unclustered_defects,
               list_clusters, read_cluster, save_cluster, delete_cluster,
               ecosystem_report, create_module, update_module,
               search_issues, file_cluster, link_cluster, sync_issue_status,
               run_module, queue_retest, list_retests],
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
]


def ecosystem_tool_names() -> list[str]:
    """A copy, so a caller extending its own allow-list cannot append to this list."""
    return list(ECOSYSTEM_TOOL_NAMES)
