"""Projects that are really one product, and the view across them.

A project is one app. Metaesthetics is six of them — a patient app on Android and iOS, a
doctor app on iPad, a clinic admin portal, a marketing site — talking to one backend. Tested
one at a time they look like six unrelated apps, and a single defect in the shared backend
gets filed six times by six agents that cannot see each other.

Membership is **derived, never stored twice**: a project belongs to an ecosystem because its
own `meta.json` says so (`ecosystem`, `role`). `write_meta` takes arbitrary keys and every
reader defaults a missing one, so tagging needs no migration and no schema bump — the same
route `blackcode_project_id`, `model` and `cli_session_id` all took. A separate membership
file would be a second source of truth with its own way of going stale; there is nothing here
that `known_packages()` plus each project's meta cannot answer.

`ECOSYSTEM.md` is a *generated* digest, rewritten on every mutation the same way
`system_memory.py` rewrites `SYSTEM_MEMORY.md`, so the index and the projects cannot drift.
Read it; do not edit it.

Both files live in `projects/` beside `registry.json` and `last-opened.json` — the existing
home for state that belongs to no single project. That directory is gitignored, which is
what we want: this carries findings about apps under test, and those have never been in git.

Nothing here reads `flow-graph.json`. Those are megabytes of base64 JPEG (see CLAUDE.md);
a cross-project pass that opened them would blow up any caller's context.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import project_paths
from agent import store

logger = logging.getLogger("ecosystem")

#: Worst-first, matching `store.FINDING_KINDS` — a reader wants the defects before the passes.
DEFECT_KINDS = ("bug", "warning", "suggestion")

#: The role a supervisor project holds. It is a role like any other so that one key answers
#: "what is this project" for every project, rather than a second flag only some carry — but
#: it is the one role with no app behind it, which is what `supervises` exists to detect.
SUPERVISOR_ROLE = "supervisor"

_LOCK = threading.RLock()


def _index_path() -> Path:
    return project_paths.DEFAULT_PROJECTS_DIR / "ECOSYSTEM.md"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- membership -----------------------------------------------------------------------------
def _meta(package: str) -> dict[str, Any]:
    """A project's meta.json, or {} — imported lazily to keep this module importable from
    tools and tests without dragging in the whole backend package."""
    from backend import projects as backend_projects
    return backend_projects.read_meta(package) or {}


def tag(package: str, ecosystem: str, role: str) -> dict[str, Any]:
    """Put a project in an ecosystem under a role. Idempotent; safe to re-run."""
    from backend import projects as backend_projects
    with _LOCK:
        meta = backend_projects.write_meta(package, ecosystem=ecosystem, role=role)
    return meta


def untag(package: str) -> None:
    """Remove a project from its ecosystem, leaving everything else in its meta alone."""
    from backend import projects as backend_projects
    with _LOCK:
        meta = backend_projects.read_meta(package)
        if not meta:
            return
        meta.pop("ecosystem", None)
        meta.pop("role", None)
        path = backend_projects.meta_path(package)
        try:
            path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not untag %s: %s", package, exc)


def members(name: str) -> list[dict[str, Any]]:
    """Every project tagged into this ecosystem, with its location and platform.

    Sorted by role so the read order is stable across runs — `known_packages()` returns
    registry entries before folder scan results, which is an ordering no caller should
    inherit as if it meant something.
    """
    out: list[dict[str, Any]] = []
    for package in project_paths.known_packages():
        meta = _meta(package)
        if str(meta.get("ecosystem") or "") != name:
            continue
        # The supervisor is in the ecosystem but is not one of its apps — it has no package to
        # launch and files nothing. Counting it here would report six apps where there are
        # five, and put an always-empty row in every listing built from this.
        if str(meta.get("role") or "") == SUPERVISOR_ROLE:
            continue
        out.append({
            "package": package,
            "role": str(meta.get("role") or "untagged"),
            # Absent means android everywhere else in this codebase; keep that convention.
            "platform": str(meta.get("platform") or "android"),
            "root": str(project_paths.project_dir(package)),
            "last_run_at": meta.get("last_run_at"),
            "blackcode_project_id": meta.get("blackcode_project_id"),
        })
    return sorted(out, key=lambda m: m["role"])


def ecosystems() -> dict[str, list[dict[str, Any]]]:
    """Every ecosystem on disk to its members. Untagged projects are simply absent."""
    names: dict[str, None] = {}
    for package in project_paths.known_packages():
        name = str(_meta(package).get("ecosystem") or "")
        if name:
            names[name] = None
    return {name: members(name) for name in sorted(names)}


def role_of(package: str) -> Optional[str]:
    """This project's role, or None if it is not in an ecosystem."""
    meta = _meta(package)
    return str(meta["role"]) if meta.get("ecosystem") and meta.get("role") else None


def supervises(package: str) -> Optional[str]:
    """The ecosystem this project supervises, or None if it is an app like any other.

    A supervisor project has no package to launch, no device, nothing to tap — it is five
    apps at once. `runtime._options` reads this to build a session with **no device server
    registered at all**, which is what makes an ecosystem agent incapable of driving a phone
    rather than merely discouraged from it.
    """
    meta = _meta(package)
    name = str(meta.get("ecosystem") or "")
    return name if name and str(meta.get("role") or "") == SUPERVISOR_ROLE else None


def supervisor(name: str) -> Optional[str]:
    """The package of this ecosystem's supervisor project, or None if it has none.

    The inverse of `supervises`. `members` deliberately omits the supervisor, so anything that
    needs to *talk to* the manager — the manager dashboard, which opens its conversation —
    cannot find it from a members listing and would otherwise have to hardcode the convention
    that the package equals the ecosystem name.
    """
    for package in project_paths.known_packages():
        if supervises(package) == name:
            return package
    return None


def create_supervisor(name: str) -> str:
    """Create (or return) the supervisor project for an ecosystem.

    Its package is the ecosystem's own name. That keeps it addressable by every existing
    project path without a second concept — it is a project folder like the others, holding a
    conversation, a memory file and nothing else — while `supervises` keeps it out of the
    app listings that would otherwise count it as a sixth app.
    """
    from backend import projects as backend_projects
    with _LOCK:
        backend_projects.write_meta(name, ecosystem=name, role=SUPERVISOR_ROLE)
    return name


# -- the view across them -------------------------------------------------------------------
def findings(name: str, kinds: tuple[str, ...] = DEFECT_KINDS) -> list[dict[str, Any]]:
    """Every finding in the ecosystem, tagged with the app and role it came from.

    `store.list_all_findings` already stamps `module_slug`/`module_title`; this adds the two
    fields that only mean anything once more than one project is in view. Passes are excluded
    by default — 240 of the 367 findings on disk are passes, and a correlation pass looking
    for duplicated defects has no use for them.
    """
    out: list[dict[str, Any]] = []
    for member in members(name):
        package = member["package"]
        for finding in store.list_all_findings(package):
            if finding.get("kind") not in kinds:
                continue
            out.append({**finding, "package": package, "role": member["role"]})
    return out


def module_index(name: str) -> list[dict[str, Any]]:
    """Every module in the ecosystem with its status and outcome tally.

    The cheap half of the "map" a supervisor needs: what areas exist, which have been tested,
    and where the defects are — without opening a single findings file twice or going near a
    board.
    """
    out: list[dict[str, Any]] = []
    for member in members(name):
        package = member["package"]
        by_slug: dict[str, dict[str, int]] = {}
        for finding in store.list_all_findings(package):
            slug = str(finding.get("module_slug") or "")
            kind = str(finding.get("kind") or "bug")
            by_slug.setdefault(slug, {}).setdefault(kind, 0)
            by_slug[slug][kind] += 1
        for entry in store.list_subprojects(package):
            slug = str(entry.get("slug") or "")
            if not slug:
                continue
            out.append({
                "package": package,
                "role": member["role"],
                "slug": slug,
                "title": str(entry.get("title") or slug),
                "status": str(entry.get("status") or "proposed"),
                "scope": str(entry.get("scope") or ""),
                "last_run_at": entry.get("last_run_at"),
                "counts": by_slug.get(slug, {}),
            })
    return out


def app_index(name: str) -> list[dict[str, Any]]:
    """Every app with its module counts and outcome tally — one row per app.

    The per-app row that every listing of this ecosystem wants: the manager dashboard's app
    cards, the `list_apps` tool, and the totals in `summary` are all this, aggregated
    differently. Written once because two places counting the same findings is how two screens
    start disagreeing about how many bugs there are.

    `never_run` is carried separately from `modules_tested` because they answer different
    questions. A module that has filed nothing may be one that works or one nobody has opened,
    and only one of those is good news.
    """
    index = module_index(name)
    out: list[dict[str, Any]] = []
    for member in members(name):
        mods = [m for m in index if m["package"] == member["package"]]
        tally = {kind: 0 for kind in ("bug", "warning", "suggestion", "pass")}
        for mod in mods:
            for kind, n in mod["counts"].items():
                tally[kind] = tally.get(kind, 0) + n
        out.append({
            **member,
            "modules": len(mods),
            "modules_tested": sum(1 for m in mods if m["status"] == "tested"),
            # Both lists exclude the manager module: it is where you talk to the project, not
            # an area of the app, so it is never marked tested and never "runs" — counting it
            # would put one phantom gap in every app.
            "untested": [m["slug"] for m in mods
                         if m["status"] != "tested" and not store.is_main_slug(m["slug"])],
            "never_run": [m["slug"] for m in mods
                          if not m.get("last_run_at") and not store.is_main_slug(m["slug"])],
            "counts": tally,
            "defects": sum(tally[k] for k in DEFECT_KINDS),
        })
    return out


def summary(name: str) -> dict[str, Any]:
    """Headline numbers for one ecosystem, aggregated from `app_index`."""
    apps = app_index(name)
    totals: dict[str, int] = {}
    for app in apps:
        for kind, n in app["counts"].items():
            # Only kinds actually filed, so an ecosystem with no suggestions does not report
            # `suggestion: 0` — absent and zero read differently in a tally.
            if n:
                totals[kind] = totals.get(kind, 0) + n
    modules = sum(a["modules"] for a in apps)
    tested = sum(a["modules_tested"] for a in apps)
    return {
        "ecosystem": name,
        "apps": len(apps),
        "modules": modules,
        "modules_tested": tested,
        "modules_untested": modules - tested,
        "counts": totals,
        "defects": sum(totals.get(k, 0) for k in DEFECT_KINDS),
    }


# -- the generated index --------------------------------------------------------------------
def _digest(data: dict[str, list[dict[str, Any]]]) -> str:
    lines = [
        "# Ecosystems",
        "",
        "Generated by `ecosystem.py` — **do not edit**; every write regenerates this file.",
        "Projects belong to an ecosystem because their own `meta.json` says so.",
        "",
        f"_Rebuilt {_now()}_",
        "",
    ]
    if not data:
        lines += ["No project is tagged into an ecosystem yet.",
                  "", "    python ecosystem.py --tag <package> <ecosystem> <role>", ""]
        return "\n".join(lines)

    for name, mems in data.items():
        s = summary(name)
        counts = ", ".join(f"{k} {v}" for k, v in sorted(s["counts"].items())) or "none yet"
        lines += [
            f"## {name}",
            "",
            f"{s['apps']} apps · {s['modules']} modules "
            f"({s['modules_tested']} tested, {s['modules_untested']} not) · "
            f"{s['defects']} defects — {counts}",
            "",
            "| Role | Package | Platform | Where it lives |",
            "|---|---|---|---|",
        ]
        for m in mems:
            lines.append(f"| `{m['role']}` | {m['package']} | {m['platform']} | `{m['root']}` |")
        lines += ["", "### Modules", "",
                  "| Role | Module | Status | Outcomes |", "|---|---|---|---|"]
        for mod in module_index(name):
            tally = ", ".join(f"{k} {v}" for k, v in sorted(mod["counts"].items())) or "—"
            lines.append(
                f"| `{mod['role']}` | {mod['title']} (`{mod['slug']}`) | {mod['status']} | {tally} |")
        lines.append("")
    return "\n".join(lines)


def write_index() -> Optional[str]:
    """Regenerate ECOSYSTEM.md from what is on disk. Returns its path, or None on failure.

    Written tmp+replace with pid/tid in the name, matching `store._write_json_atomic` — two
    modules running concurrently must not leave a half-written index behind.
    """
    path = _index_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".md.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(_digest(ecosystems()), encoding="utf-8")
        tmp.replace(path)
        return str(path)
    except OSError as exc:
        logger.warning("Could not write the ecosystem index: %s", exc)
        return None


def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Group projects that are one product.")
    parser.add_argument("--tag", nargs=3, metavar=("PACKAGE", "ECOSYSTEM", "ROLE"),
                        help="put a project in an ecosystem under a role")
    parser.add_argument("--untag", metavar="PACKAGE", help="remove a project from its ecosystem")
    parser.add_argument("--show", action="store_true", help="print the index")
    parser.add_argument("--rebuild", action="store_true", help="regenerate ECOSYSTEM.md")
    args = parser.parse_args()

    if args.tag:
        package, name, role = args.tag
        tag(package, name, role)
        print(f"tagged {package} -> {name}/{role}")
    if args.untag:
        untag(args.untag)
        print(f"untagged {args.untag}")
    if args.tag or args.untag or args.rebuild:
        print(f"wrote {write_index()}")
    if args.show or not (args.tag or args.untag or args.rebuild):
        print(_digest(ecosystems()))


if __name__ == "__main__":
    _cli()
