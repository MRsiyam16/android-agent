"""Findings that turned out to be one defect.

`ecosystem.py` groups projects that are one product; this groups *findings* that are one
defect. Five apps on one backend means the same underlying fault gets filed once per app by
agents that cannot see each other — the first pass over Metaesthetics found 131 filed defects
that were 98 distinct ones, a quarter of the list duplicated. Worse, the duplicates are the
interesting ones: a search index that is not tokenised showed up seven times across three
clients, and nobody reading a single project's report could have said so.

A cluster is a **hypothesis with evidence attached**, not a verdict, which is why
`confidence` is stored and rendered everywhere the cluster is. "These three are one defect"
is a claim about someone else's backend, made from the outside; recording it as a plain fact
would be the same mistake as filing a finding from a dump without a screenshot.

Two things are deliberately *not* stored:

`scope` (cross-app vs single-app) is derived from the members' projects on every read. Storing
it would let it disagree with the membership it describes the first time a member moved.

The reverse link is a cache, not a record. `apply()` stamps each member finding with its
cluster id through `store.set_finding_tracking`, so a module agent sees "already known as part
of X" without loading the ecosystem — but this file stays authoritative, and re-running
`apply()` rebuilds the stamps from it. Anything that edits membership here must call it.

Storage sits beside `registry.json` and `last-opened.json` in the projects directory — state
that belongs to no single project. Read through the helpers; `DEFAULT_PROJECTS_DIR` is
patched by the tests.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import ecosystem
import project_paths
from agent import store

logger = logging.getLogger("clusters")

SCHEMA_VERSION = 1

#: How much weight to put on "these are one defect". Ordered strongest first.
#:
#: `confirmed` — the findings' own evidence says so (matching symptoms plus something that
#:   discriminates: a shared token, an identical error string, one client proving the rule
#:   another client breaks).
#: `likely`    — same mechanism and same area, no single piece of evidence nails it.
#: `tentative` — same shape, plausibly separate implementations. Group to fix together;
#:   do not file as one ticket without checking.
CONFIDENCE = ("confirmed", "likely", "tentative")

_LOCK = threading.RLock()


def _path() -> Path:
    return project_paths.DEFAULT_PROJECTS_DIR / "clusters.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _empty() -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "updated_at": _now(), "ecosystems": {}}


def _load() -> dict[str, Any]:
    path = _path()
    if not path.is_file():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Losing the grouping must not take the findings with it: every caller degrades to
        # "no clusters known", which is what the system did before this file existed.
        logger.warning("Could not read clusters (%s); treating as empty", exc)
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("schema", SCHEMA_VERSION)
    data.setdefault("ecosystems", {})
    return data


def _save(data: dict[str, Any]) -> None:
    data["updated_at"] = _now()
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".json.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Could not write clusters: %s", exc)


def _member_key(member: dict[str, Any]) -> tuple[str, str, str]:
    return (str(member.get("package") or ""), str(member.get("module") or ""),
            str(member.get("finding") or ""))


# -- reading --------------------------------------------------------------------------------
def _findings_index(name: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Every defect in the ecosystem, keyed the way a member references one."""
    return {(f["package"], f["module_slug"], f["id"]): f
            for f in ecosystem.findings(name, kinds=store.FINDING_KINDS)}


def _resolve(name: str, cluster: dict[str, Any],
             index: dict[tuple[str, str, str], dict[str, Any]]) -> dict[str, Any]:
    """Fill in what is derivable: each member's live finding, the roles spanned, the scope.

    A member whose finding is no longer on disk comes back as `orphan` rather than being
    dropped — a cluster quietly shrinking is how a wrong number becomes believable.
    """
    members, roles, orphans = [], set(), []
    for member in cluster.get("members", []):
        key = _member_key(member)
        finding = index.get(key)
        if finding is None:
            orphans.append({"package": key[0], "module": key[1], "finding": key[2]})
            continue
        roles.add(finding["role"])
        members.append({
            "package": finding["package"], "role": finding["role"],
            "module": finding["module_slug"], "module_title": finding.get("module_title", ""),
            "finding": finding["id"], "title": finding.get("title", ""),
            "kind": finding.get("kind"), "severity": finding.get("severity"),
            "resolved": bool(finding.get("resolved")),
            "issue_url": finding.get("issue_url"),
        })
    return {
        **cluster,
        "members": members,
        "orphans": orphans,
        "roles": sorted(roles),
        "scope": "cross-app" if len(roles) > 1 else "single-app",
        "size": len(members),
        # A cluster is done when every report of it is: one client fixed and another not is
        # exactly the partial-backend-fix case worth seeing.
        "resolved": bool(members) and all(m["resolved"] for m in members),
    }


def list_clusters(name: str) -> list[dict[str, Any]]:
    """Every cluster in this ecosystem, resolved, cross-app first then largest first."""
    index = _findings_index(name)
    raw = _load()["ecosystems"].get(name, {})
    out = [_resolve(name, {**c, "id": cid}, index) for cid, c in raw.items()]
    return sorted(out, key=lambda c: (c["scope"] != "cross-app", -c["size"], c["id"]))


def get(name: str, cluster_id: str) -> Optional[dict[str, Any]]:
    raw = _load()["ecosystems"].get(name, {}).get(cluster_id)
    if raw is None:
        return None
    return _resolve(name, {**raw, "id": cluster_id}, _findings_index(name))


def cluster_of(name: str, package: str, module: str, finding_id: str) -> Optional[str]:
    """Which cluster claims this finding, read from this file rather than the stamp."""
    key = (package, module, finding_id)
    for cid, cluster in _load()["ecosystems"].get(name, {}).items():
        if any(_member_key(m) == key for m in cluster.get("members", [])):
            return cid
    return None


def summary(name: str) -> dict[str, Any]:
    """Filed vs distinct, and how much of the list was duplication.

    `distinct` counts each cluster once plus every unclustered defect. Orphaned members are
    excluded from `absorbed` so a deleted finding cannot inflate the saving.
    """
    defects = ecosystem.findings(name)
    rows = list_clusters(name)
    absorbed = sum(max(0, c["size"] - 1) for c in rows)
    total = len(defects)
    return {
        "ecosystem": name,
        "filed": total,
        "clusters": len(rows),
        "cross_app": sum(1 for c in rows if c["scope"] == "cross-app"),
        "clustered": sum(c["size"] for c in rows),
        "absorbed": absorbed,
        "distinct": total - absorbed,
        "resolved_clusters": sum(1 for c in rows if c["resolved"]),
        "orphans": sum(len(c["orphans"]) for c in rows),
    }


# -- writing --------------------------------------------------------------------------------
def save(name: str, cluster_id: str, *, title: str, root: str = "",
         confidence: str = "tentative",
         members: Optional[list[dict[str, Any]]] = None, **extra: Any) -> dict[str, Any]:
    """Create or update a cluster. Unknown confidence falls back to the weakest.

    Defaulting rather than raising on a bad confidence is deliberate: the caller is usually an
    agent, and the cost of a mislabelled hypothesis is that someone reads it more sceptically,
    while the cost of an exception mid-run is a lost analysis.
    """
    if confidence not in CONFIDENCE:
        logger.warning("Unknown confidence %r for cluster %s; using 'tentative'",
                       confidence, cluster_id)
        confidence = "tentative"
    with _LOCK:
        data = _load()
        bucket = data["ecosystems"].setdefault(name, {})
        existing = bucket.get(cluster_id, {})
        bucket[cluster_id] = {
            **existing, **extra,
            "title": title, "root": root, "confidence": confidence,
            "members": [{"package": str(m.get("package") or ""),
                         "module": str(m.get("module") or ""),
                         "finding": str(m.get("finding") or "")}
                        for m in (members if members is not None
                                  else existing.get("members", []))],
            "created_at": existing.get("created_at", _now()),
            "updated_at": _now(),
        }
        _save(data)
    return get(name, cluster_id) or {}


def delete(name: str, cluster_id: str) -> bool:
    """Forget a cluster and clear the stamp from every finding that carried it."""
    with _LOCK:
        data = _load()
        bucket = data["ecosystems"].get(name, {})
        cluster = bucket.pop(cluster_id, None)
        if cluster is None:
            return False
        _save(data)
    for member in cluster.get("members", []):
        package, module, finding_id = _member_key(member)
        try:
            store.set_finding_tracking(package, module, finding_id, cluster="")
        except store.StoreWriteError as exc:
            logger.warning("Could not clear cluster stamp on %s/%s/%s: %s",
                           package, module, finding_id, exc)
    return True


def add_member(name: str, cluster_id: str, package: str, module: str,
               finding_id: str) -> Optional[dict[str, Any]]:
    """Add one finding to a cluster. Idempotent; None if the cluster does not exist."""
    with _LOCK:
        data = _load()
        cluster = data["ecosystems"].get(name, {}).get(cluster_id)
        if cluster is None:
            return None
        key = (package, module, finding_id)
        if not any(_member_key(m) == key for m in cluster.get("members", [])):
            cluster.setdefault("members", []).append(
                {"package": package, "module": module, "finding": finding_id})
            cluster["updated_at"] = _now()
            _save(data)
    _stamp(package, module, finding_id, cluster_id)
    return get(name, cluster_id)


def remove_member(name: str, cluster_id: str, package: str, module: str,
                  finding_id: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        data = _load()
        cluster = data["ecosystems"].get(name, {}).get(cluster_id)
        if cluster is None:
            return None
        key = (package, module, finding_id)
        kept = [m for m in cluster.get("members", []) if _member_key(m) != key]
        if len(kept) != len(cluster.get("members", [])):
            cluster["members"] = kept
            cluster["updated_at"] = _now()
            _save(data)
    _stamp(package, module, finding_id, "")
    return get(name, cluster_id)


def _stamp(package: str, module: str, finding_id: str, cluster_id: str) -> bool:
    try:
        return store.set_finding_tracking(
            package, module, finding_id, cluster=cluster_id) is not None
    except store.StoreWriteError as exc:
        logger.warning("Could not stamp cluster on %s/%s/%s: %s",
                       package, module, finding_id, exc)
        return False


def apply(name: str) -> dict[str, int]:
    """Rewrite every member finding's `cluster` stamp from this file.

    Idempotent and safe to re-run — this file is authoritative, so a stamp that disagrees is
    simply overwritten. Findings that no cluster claims have theirs cleared, which is what
    makes splitting a cluster leave nothing behind.
    """
    claimed: dict[tuple[str, str, str], str] = {}
    for cid, cluster in _load()["ecosystems"].get(name, {}).items():
        for member in cluster.get("members", []):
            claimed[_member_key(member)] = cid

    stamped = cleared = missing = 0
    for finding in ecosystem.findings(name, kinds=store.FINDING_KINDS):
        key = (finding["package"], finding["module_slug"], finding["id"])
        want = claimed.get(key, "")
        if str(finding.get("cluster") or "") == want:
            continue
        if _stamp(*key, want):
            stamped += 1 if want else 0
            cleared += 0 if want else 1
        else:
            missing += 1
    return {"stamped": stamped, "cleared": cleared, "failed": missing}


def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Findings that are one defect.")
    parser.add_argument("ecosystem", nargs="?", default="metaesthetics")
    parser.add_argument("--apply", action="store_true",
                        help="rewrite every finding's cluster stamp from the store")
    parser.add_argument("--json", action="store_true", help="dump clusters as JSON")
    args = parser.parse_args()

    if args.apply:
        print(apply(args.ecosystem))
        return
    if args.json:
        print(json.dumps(list_clusters(args.ecosystem), indent=2))
        return

    s = summary(args.ecosystem)
    print(f"{s['filed']} filed defects -> {s['distinct']} distinct "
          f"({s['absorbed']} absorbed by {s['clusters']} clusters, "
          f"{s['cross_app']} of them cross-app)")
    if s["orphans"]:
        print(f"!! {s['orphans']} cluster member(s) point at findings that no longer exist")
    print()
    for cluster in list_clusters(args.ecosystem):
        mark = " [RESOLVED]" if cluster["resolved"] else ""
        print(f"{cluster['size']}x [{cluster['confidence']:9}] {cluster['scope']:10} "
              f"{cluster['title']}{mark}")
        print(f"      {', '.join(cluster['roles'])}")


if __name__ == "__main__":
    _cli()
