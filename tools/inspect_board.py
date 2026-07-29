"""Print a flow-graph.json's structure without its screenshots.

A saved board is mostly base64 JPEG: the deskclock board is 6.5 MB, of which the
nodes, edges and positions are about 40 KB. Reading one directly costs roughly
1.6M tokens and tells you nothing you came for, so `.claude/settings.json` denies
it outright. This is the way in instead.

    python tools/inspect_board.py com.google.android.deskclock
    python tools/inspect_board.py --path projects/com.smoke.test/flow-graph.json
    python tools/inspect_board.py com.google.android.keep --edges
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project_paths  # noqa: E402


def _strip(value):
    """Replace any data: URI with a size marker, at any depth."""
    if isinstance(value, str):
        if value.startswith("data:"):
            return f"<{len(value) // 1024} KB {value[5:value.find(';')] or 'data'}>"
        return value
    if isinstance(value, list):
        return [_strip(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items()}
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package", nargs="?", help="package whose project board to read")
    ap.add_argument("--path", help="explicit path to a flow-graph.json")
    ap.add_argument("--nodes", action="store_true", help="list every node")
    ap.add_argument("--edges", action="store_true", help="list every edge")
    args = ap.parse_args()

    if args.path:
        board = Path(args.path)
    elif args.package:
        board = Path(project_paths.project_dir(args.package)) / "flow-graph.json"
    else:
        ap.error("give a package name or --path")

    if not board.exists():
        print(f"no board at {board}", file=sys.stderr)
        return 1

    size = board.stat().st_size
    data = json.loads(board.read_text(encoding="utf-8"))

    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    print(f"{board}")
    print(f"  on disk    {size / 1_048_576:.1f} MB")
    print(f"  {'package':<14} {data.get('package', '<none - pre-ownership board>')}")
    print(f"  {'nodes':<14} {len(nodes)}")
    print(f"  {'edges':<14} {len(edges)}")
    for key in ("nodePositions", "notes", "comments", "nodeStatus"):
        print(f"  {key:<14} {len(data.get(key, []))}")

    if args.nodes:
        print("\nnodes:")
        for n in nodes:
            print(f"  {n.get('hash', '?'):<12} {n.get('screenName', '')}")
    if args.edges:
        print("\nedges:")
        for e in edges:
            print(f"  {e.get('from', '?')} -> {e.get('to', '?'):<12} {e.get('fullLabel', '')}")

    if not (args.nodes or args.edges):
        if nodes:
            print("\nfirst node, screenshots stripped:")
            print(json.dumps(_strip(nodes[0]), indent=2)[:800])
        print("\n(--nodes / --edges to list them all)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
