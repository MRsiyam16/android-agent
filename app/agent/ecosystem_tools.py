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
  touching one. Until there is a lock across sessions, an ecosystem agent that could start a
  run would be two agents on one screen by design rather than by accident.

It also cannot file a finding. A finding is one named test case with a screenshot behind it,
filed by the agent that watched it happen; this tier has watched nothing. What it produces
instead is a *cluster* — the claim that several already-filed findings are one defect — which
carries its own confidence precisely because it is inference rather than observation.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from claude_agent_sdk import create_sdk_mcp_server, tool

import clusters as clusters_mod
import ecosystem as ecosystem_mod
from agent import store

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
        members = await asyncio.to_thread(ecosystem_mod.members, name)
        if not members:
            return _ok(f"No project is tagged into {name!r} yet.")
        index = await asyncio.to_thread(ecosystem_mod.module_index, name)
        lines = []
        for member in members:
            mods = [m for m in index if m["package"] == member["package"]]
            tested = sum(1 for m in mods if m["status"] == "tested")
            tally = {k: 0 for k in _KIND_ORDER}
            for mod in mods:
                for kind, n in mod["counts"].items():
                    tally[kind] = tally.get(kind, 0) + n
            lines.append(
                f"{member['role']}  ({member['package']})  |  {member['platform']}  |  "
                f"{len(mods)} modules, {tested} tested  |  {_tally_line(tally)}")
        return _ok("\n".join(lines))

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

        members = await asyncio.to_thread(ecosystem_mod.members, name)
        index = await asyncio.to_thread(ecosystem_mod.module_index, name)
        out.append("## Per app")
        for member in members:
            mods = [m for m in index if m["package"] == member["package"]]
            tally = {k: 0 for k in _KIND_ORDER}
            for mod in mods:
                for kind, n in mod["counts"].items():
                    tally[kind] = tally.get(kind, 0) + n
            untested = [m["slug"] for m in mods
                        if m["status"] != "tested" and not store.is_main_slug(m["slug"])]
            out.append(f"{member['role']}: {_tally_line(tally)}"
                       + (f"  ({len(untested)} module(s) not yet tested)" if untested else ""))

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

    return create_sdk_mcp_server(
        name="ecosystem",
        version="1.0.0",
        tools=[list_apps, read_app, read_finding, unclustered_defects,
               list_clusters, read_cluster, save_cluster, delete_cluster,
               ecosystem_report, create_module, update_module],
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
]


def ecosystem_tool_names() -> list[str]:
    """A copy, so a caller extending its own allow-list cannot append to this list."""
    return list(ECOSYSTEM_TOOL_NAMES)
