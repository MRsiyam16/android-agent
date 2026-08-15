"""One command to open the master agent: `python start_master.py --ecosystem metaesthetics`

The other launchers each bring up *one* stack and open the cockpit at `/`: a phone, an app, a
flow graph. This one opens the tier above them — the ecosystem manager at `/manager`, which has
no device of its own and instead commissions, starts and stops runs across every app in the
product.

So the shape is different in three ways, and each of them is the point:

* **It starts no device stack.** Deciding that the iPad needs its tunnel up is the master
  agent's job now (`start_app`), and it can only make that decision once it knows which apps
  the user actually wants tested today. A launcher that brought up every platform on principle
  would ask for a UAC prompt to test a website.
* **It attaches to a server that is already running** rather than refusing the port. Opening
  the master agent while an Android suite is mid-run is a completely normal thing to want, and
  the old "re-run with --force" would have killed the run to show a dashboard.
* **It pre-warms the manager's own session**, not a module's. The first thing anyone types
  here is a question about the whole product, and that answer starts with several tool calls;
  paying the CLI spawn cost first means the answer starts arriving instead of the spinner.

It ends by printing the fleet: every app, the device it would use, and whether that platform
can accept a run at all. That listing is the thing worth reading before typing "start the iPad
app" — it is also exactly what the agent sees when it calls `list_devices`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import config
from start import port_owner, process_name, server_responds

ROOT = Path(__file__).resolve().parent
URL = f"http://localhost:{config.SERVER_PORT}"


def _post(path: str, timeout: float = 120.0) -> dict:
    request = urllib.request.Request(f"{URL}{path}", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.load(resp)


def _get(path: str, timeout: float = 20.0):
    with urllib.request.urlopen(f"{URL}{path}", timeout=timeout) as resp:
        return json.load(resp)


def resolve_ecosystem(explicit: str | None) -> str | None:
    """The product to manage: the one named, or the only one there is."""
    import ecosystem

    found = ecosystem.ecosystems()
    if explicit:
        if explicit in found:
            return explicit
        print(f"  There is no product called {explicit!r}.")
        print(f"  Known: {', '.join(sorted(found)) or 'none — tag some projects first'}")
        return None
    if len(found) == 1:
        return next(iter(found))
    if not found:
        print("  No product exists yet. Tag projects into one from the dashboard, or pass")
        print("  --ecosystem NAME to create the supervisor for a new one.")
        return None
    print(f"  Several products exist — name one with --ecosystem: {', '.join(sorted(found))}")
    return None


def ensure_supervisor(name: str) -> tuple[str, str]:
    """(package, slug) of the manager module, creating the supervisor project if needed."""
    import ecosystem
    from agent import store

    package = ecosystem.supervisor(name) or ecosystem.create_supervisor(name)
    slug = store.main_slug(package)
    if store.get_subproject(package, slug) is None:
        store.create_subproject(
            package, "Main",
            f"manages the {name} product: which apps are tested, which defects are one defect, "
            f"and which runs happen next",
            status="approved")
    return package, slug


def start_server() -> subprocess.Popen | None:
    """Start `server.py`, or return None if one is already answering."""
    if server_responds():
        pid = port_owner(config.SERVER_PORT)
        print(f"  server : already up at {URL}"
              + (f"  (pid {pid}, {process_name(pid)})" if pid else "")
              + " — attaching to it")
        return None

    pid = port_owner(config.SERVER_PORT)
    if pid:
        # Bound but not answering: a stale socket. Said plainly, because uvicorn's
        # SO_REUSEADDR would otherwise let a second server look like it started while the
        # dead one keeps the port.
        print(f"  server : port {config.SERVER_PORT} is held by PID {pid} "
              f"({process_name(pid)}) and is NOT answering.")
        print("           Stop it, or run `python start.py --force`.")
        return None

    print(f"  server : starting on {URL} ...")
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT)
    for _ in range(60):
        if server_responds():
            print(f"           up  (pid {proc.pid})")
            return proc
        if proc.poll() is not None:
            print("           exited during startup. Run `python server.py` to see the error.")
            return None
        time.sleep(0.5)
    print("           did not come up in 30s.")
    proc.terminate()
    return None


def report_fleet(name: str) -> None:
    """Every app, the device it would use, and whether that platform can take a run.

    Deliberately the same picture the agent gets from `list_devices`: the user reading this
    window and the agent reading its tool output should never be looking at two different
    accounts of what is plugged in.
    """
    import ecosystem
    import stacks

    members = ecosystem.members(name)
    try:
        attached = _get("/devices")
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        attached = []

    print(f"\n  {name} — {len(members)} apps\n")
    for member in members:
        platform = str(member.get("platform") or "?")
        serial = None
        for device in attached:
            if member["package"] in (device.get("pinned_to") or []):
                serial = device["serial"]
        if platform == "web":
            where = member["package"]
        elif serial:
            where = f"pinned to {serial}"
        else:
            same = [d for d in attached if d.get("platform") == platform]
            where = (f"unpinned, would take {same[0]['serial']}" if len(same) == 1
                     else f"unpinned, {len(same)} {platform} device(s) attached" if same
                     else "no device attached")
        print(f"    {member['role']:<18} {platform:<9} {where}")

    print("\n  Stacks:")
    platforms = sorted({str(m.get("platform") or "").lower()
                        for m in members if m.get("platform")})
    for row in stacks.status_all(platforms):
        print(f"    {row['platform']:<9} {'ready' if row['ready'] else 'NOT READY':<10} "
              f"{row['detail']}")
        if not row["ready"] and row["fix"]:
            print(f"              {row['fix']}")

    try:
        board = _get(f"/ecosystems/{name}/board")
        pending = (board.get("retests") or {}).get("pending", 0)
        if pending:
            print(f"\n  {pending} re-test(s) are waiting for your approval on the board.")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Open the QA Tester AI master agent for one product.")
    ap.add_argument("--ecosystem", help="which product to manage (default: the only one)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()

    # The server subprocess shares this console and logs to stderr, which is unbuffered. Without
    # this, our own stdout sits in a block buffer and the fleet report appears *after* pages of
    # uvicorn logging — or, when the output is piped rather than a console, not until exit.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    print("\nQA Tester AI - master agent\n")

    name = resolve_ecosystem(args.ecosystem)
    if not name:
        return 1
    package, slug = ensure_supervisor(name)
    print(f"  product: {name}  (supervisor project {package!r}, module {slug!r})")

    proc = start_server()
    if proc is None and not server_responds():
        return 1

    # Pre-warm the manager's own session. Not fatal: the page works without it, the first
    # message just pays the CLI's spawn cost. But a failure here is usually "the Claude Code
    # CLI is not installed", which is worth saying now rather than after the user types.
    try:
        info = _post(f"/agent/{package}/{slug}/warm")
        model = info.get("model_label") or info.get("model") or "model unknown"
        subscription = f", {info['subscription']}" if info.get("subscription") else ""
        print(f"  agent  : ready  ({model}{subscription})")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "read", None)
        print(f"  agent  : could not pre-warm the manager session — {exc}")
        if detail:
            print(f"           {detail()[:300]!r}")
        print("           The page still works; the first message will be slower. If this is")
        print("           'CLI not found', install it: npm i -g @anthropic-ai/claude-code")

    report_fleet(name)

    print(f"\n  Board  : {URL}/manager")
    print(f"  Cockpit: {URL}/   (one app, one device, the flow graph)")
    print("\n  Ask the master agent things like:")
    print('    "start the iPad app"            - brings up the tunnel + WebDriverAgent')
    print('    "start the clinic web project"  - checks the browser stack')
    print('    "run the booking module on clinic-web and the search module on the iPad"')
    print('    "what is running right now?"')

    if not args.no_browser:
        webbrowser.open(f"{URL}/manager")

    if proc is None:
        print("\n  This window is not the server — it attached to one that was already up.")
        print("  Closing it changes nothing.\n")
        return 0

    print("\n  Keep this window open: it is the server. Ctrl+C to stop.\n")
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nStopping server ...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
