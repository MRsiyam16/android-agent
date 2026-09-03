"""The second instance: `python start_verifier.py` — QA on call for Bugmaster's fix pipeline.

`start_master.py` opens the tier above the projects: one product, one manager, one board. This
opens a *second copy of that whole thing* beside it, on port 8001, with its own notebook under
`app/verify-projects/`. It is not a mode of the first one. It is another harness.

**Why a second instance and not a second ecosystem inside the first.** The QA Master tests the
staging build and reports on the product. The Verifier re-runs one case against a build that
exists only as a patch in a worktree on this PC, at Bugmaster's request, so a merge gate can
read the answer. Those two must not share a notebook, because a shared one is wrong in both
directions and neither is visible after the fact:

* a `bug` filed against an unmerged patch would appear on the product board as a defect in
  the shipped app, get clustered with real findings, and get filed to Blackcode;
* a `pass` recorded on a patch would read, a week later, as the staging build having been
  re-tested;
* the learned per-app memory would fill with facts about a build nobody can install.

Two `PROJECTS_DIR`s is the whole of the separation, and everything follows it — findings,
transcripts, memory, clusters, the re-test queue, the verification log. See
`project_paths.py`'s header.

**Three things it does that the master does not.**

* **It refuses to start while the QA Master holds a device lock.** Both instances would drive
  the same phone through the same adb, and `device_locks` is per-process — it cannot see
  across a port. Two agents on one target interleave their taps, and each one's findings then
  describe a screen the other just changed. A master that is simply *off* is fine; it is a
  held lock that is the problem. This is the one check that is worth refusing over rather than
  warning about, because the damage it prevents is a false defect and those are expensive here.
* **It opens no watch tabs** (`AGENT_OPEN_MODULE_TABS=false`). Nobody is sitting in front of a
  verification run — the audience is a worker polling `GET /verifications/<job_id>` — and a
  browser tab per job on an unattended machine is just tabs.
* **It seeds its own fleet once**, from `verify-seed.json`, copying package and platform out of
  the real projects so the verifier tests the same apps and not a stale transcription.

Nothing here starts the emulator. `stacks.status("android")` says how (`python emulator.py
--ensure`) and Bugmaster's worker brings the device up itself before it sends the job — see
BRIDGE.md §8.

Environment is applied in `main()`, not at import, so importing this module in a test does not
silently repoint the whole harness's notebook.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent

#: The product this instance manages. A different name from `metaesthetics` on purpose: the two
#: notebooks never meet, and if one day they did, a shared name would silently merge them.
ECOSYSTEM = "metaesthetics-verify"

SEED_PATH = ROOT / "verify-seed.json"

#: Where the QA Master answers, and the only thing this launcher asks it.
MASTER_URL = "http://localhost:8000"

PORT = 8001
URL = f"http://localhost:{PORT}"

#: Applied to this process and inherited by the server subprocess. Set (not defaulted): a
#: leftover SERVER_PORT from a shell where somebody ran the master would otherwise put the
#: verifier on port 8000 and take the master's socket.
ENVIRONMENT: dict[str, str] = {
    "PROJECTS_DIR": "verify-projects",
    "SERVER_PORT": str(PORT),
    "SERVER_URL": URL,
    # Nobody is watching a verification run; the audience is a polling worker.
    "AGENT_OPEN_MODULE_TABS": "false",
    # So a "needs a human" toast from this instance says which instance it came from. Both
    # harnesses can be up at once and their notifications are otherwise identical.
    "QA_NOTIFY_TITLE": "QA Verifier",
}


def apply_environment(env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Put this instance's settings in `env` (the real environment by default).

    Called before anything imports `config` or `project_paths`: both read their values once, at
    import, and a harness half-configured for one notebook and half for the other would write
    findings to one place and read them from another.
    """
    target = os.environ if env is None else env
    target.update(ENVIRONMENT)
    return target  # type: ignore[return-value]


# -- the one refusal -------------------------------------------------------------------------
def master_status(url: str = MASTER_URL, timeout: float = 3.0) -> Optional[dict[str, Any]]:
    """The QA Master's `/agent/status`, or None if nothing is answering on :8000.

    None is not an error. The normal case for an unattended machine is that only the verifier
    is up, and a launcher that refused to start because the *other* harness was off would make
    the bridge depend on somebody having opened a dashboard.
    """
    try:
        with urllib.request.urlopen(f"{url}/agent/status", timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def refusal(status: Optional[dict[str, Any]]) -> Optional[str]:
    """Why the verifier must not start right now, or None if it may.

    Split from the HTTP call so the rule can be tested without a server, and because the rule
    is the part worth being sure about: **any** held lock refuses, whoever holds it. The two
    instances share one phone, one adb and one WebDriverAgent port, and neither one's
    `device_locks` can see the other's — the file is not the lock, the process is. So this is
    the only place the collision can be caught at all.
    """
    if status is None:
        return None
    locks = status.get("device_locks") or {}
    if not locks:
        return None
    holders = []
    for key, holder in (locks.items() if isinstance(locks, dict) else []):
        who = (f"{holder.get('package')}/{holder.get('slug')}"
               if isinstance(holder, dict) else str(holder))
        since = holder.get("since") if isinstance(holder, dict) else None
        holders.append(f"    {key} — {who}" + (f" (since {since})" if since else ""))
    return ("The QA Master on :8000 is driving a device:\n"
            + "\n".join(holders or [f"    {k}" for k in locks])
            + "\n\n  Both instances reach the same phone through the same adb, and a device "
              "lock\n  cannot be seen across a port — two agents on one target interleave "
              "their\n  taps, and each one's findings then describe a screen the other just "
              "changed.\n  Wait for that run to finish, or stop it from the master's board, "
              "then start\n  this again.")


# -- the fleet -------------------------------------------------------------------------------
def _source_meta(source_dir: Path, folder: str) -> dict[str, Any]:
    """One project's meta.json out of the QA Master's notebook, or {}.

    Read as a file rather than through `backend.projects`, deliberately: by the time this runs
    the harness is pointed at `verify-projects/`, and asking it for a package would look in the
    wrong notebook and find nothing. This is the one place the verifier reads the master's
    disk, it reads exactly three files, and it never writes there.
    """
    path = source_dir / folder / "meta.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def seed_members(spec: dict[str, Any], source_dir: Path) -> list[dict[str, Any]]:
    """Turn the seed file into the meta each verifier project should be created with.

    Pure, so the copying rules can be tested against a fixture directory. A member with no
    package after the copy is dropped rather than created as an empty shell: a project whose
    package is "" is one the manager can list, try to run, and fail on with no useful error.
    """
    out: list[dict[str, Any]] = []
    for raw in spec.get("members") or []:
        member = {k: v for k, v in raw.items() if not k.startswith("_")}
        source = _source_meta(source_dir, str(member.pop("from_project", "") or ""))
        for field in member.pop("copy", []) or []:
            if source.get(field) is not None and field not in member:
                member[field] = source[field]
        if not str(member.get("package") or "").strip():
            continue
        member.setdefault("platform", "android")
        out.append(member)
    return out


def ensure_fleet(name: str = ECOSYSTEM, seed_path: Path = SEED_PATH,
                 source_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """Create the verifier's ecosystem the first time, and return what it now holds.

    Seeded once: if the ecosystem already exists on disk, its projects are whatever somebody
    has since made them, and re-applying the seed would quietly undo a pin or a role that was
    corrected by hand. Everything is created through the same functions the dashboard's own
    routes use — `backend.projects.write_meta` and `ecosystem.tag`, which is exactly what
    `POST /projects/{package}/ecosystem` does — rather than by writing meta.json here. A second
    way to make a project is a second set of defaults to keep in step.
    """
    import ecosystem as ecosystem_mod
    import start_master
    from backend import projects as backend_projects

    if name in ecosystem_mod.ecosystems():
        return ecosystem_mod.members(name)

    try:
        spec = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  seed   : could not read {seed_path.name} ({exc}) — nothing was created.")
        return []

    # The QA Master's notebook, which is where the packages and platforms are copied from —
    # relative to `app/` and never to PROJECTS_DIR, which by now points at the verifier's own.
    source = source_dir or ROOT / str(spec.get("source_dir") or "projects")

    start_master.ensure_supervisor(name)
    for member in seed_members(spec, source):
        package = str(member.pop("package"))
        role = str(member.pop("role", "") or "untagged")
        backend_projects.write_meta(package, **member)
        ecosystem_mod.tag(package, name, role)
    ecosystem_mod.write_index()
    return ecosystem_mod.members(name)


# -- the server ------------------------------------------------------------------------------
def start_server() -> Optional[subprocess.Popen]:
    """Start `server.py` on :8001, or return None if one is already answering there.

    Same shape as `start_master.start_server` and for the same reasons — attaching to a running
    server rather than refusing the port, and naming the PID when the port is held by something
    that is not answering. The environment is already this instance's, so the child inherits
    the verifier's notebook without being told about it separately.
    """
    from start import port_owner, process_name, server_responds

    if server_responds():
        pid = port_owner(PORT)
        print(f"  server : already up at {URL}"
              + (f"  (pid {pid}, {process_name(pid)})" if pid else "") + " — attaching to it")
        return None

    pid = port_owner(PORT)
    if pid:
        print(f"  server : port {PORT} is held by PID {pid} ({process_name(pid)}) and is NOT "
              f"answering.")
        print("           Stop it, then run this again.")
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
    """Every verifier app, where it would run, and whether that platform can take a run."""
    import ecosystem as ecosystem_mod
    import stacks

    members = ecosystem_mod.members(name)
    print(f"\n  {name} — {len(members)} apps\n")
    for member in members:
        from backend import projects as backend_projects

        pin = (backend_projects.read_meta(member["package"]) or {}).get("device_serial")
        where = f"pinned to {pin}" if pin else "unpinned"
        print(f"    {member['role']:<18} {str(member.get('platform')):<9} {where}")

    print("\n  Stacks:")
    platforms = sorted({str(m.get("platform") or "").lower()
                        for m in members if m.get("platform")})
    for row in stacks.status_all(platforms):
        print(f"    {row['platform']:<9} {'ready' if row['ready'] else 'NOT READY':<10} "
              f"{row['detail']}")
        if not row["ready"] and row["fix"]:
            print(f"              {row['fix']}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Open the QA Verifier — the QA side of Bugmaster's device bridge.")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    ap.add_argument("--ecosystem", default=ECOSYSTEM,
                    help=f"which product to verify for (default {ECOSYSTEM})")
    args = ap.parse_args()

    # Before every harness import below. See `apply_environment`.
    apply_environment()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    print("\nQA Verifier - the device bridge for Bugmaster\n")

    stop = refusal(master_status())
    if stop:
        print("  REFUSED to start.\n")
        print("  " + stop + "\n")
        return 1

    import project_paths

    print(f"  notebook: {project_paths.DEFAULT_PROJECTS_DIR}")

    name = str(args.ecosystem)
    members = ensure_fleet(name)
    if not members:
        print(f"  fleet  : nothing is tagged into {name!r} and the seed created nothing.")
        print(f"           Check {SEED_PATH.name} and the projects it copies from.")
        return 1

    package, slug = _supervisor(name)
    print(f"  product: {name}  (supervisor project {package!r}, module {slug!r})")

    proc = start_server()
    from start import server_responds

    if proc is None and not server_responds():
        return 1

    try:
        info = _post(f"/agent/{package}/{slug}/warm")
        model = info.get("model_label") or info.get("model") or "model unknown"
        print(f"  agent  : ready  ({model})")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"  agent  : could not pre-warm the manager session — {exc}")
        print("           The page still works; the first job will be slower.")

    report_fleet(name)

    print(f"\n  Board  : {URL}/manager")
    print(f"  Bridge : {URL}/verifications        (what has been answered)")
    print(f"           {URL}/verifications/<job>  (404 until reported)")
    print("\n  The worker sends a message beginning `Bugmaster verification job` to")
    print(f"  POST {URL}/agent/{package}/{slug}/message, the manager runs one step and")
    print("  calls report_verification, and the worker polls the URL above for the verdict.")

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


def _supervisor(name: str) -> tuple[str, str]:
    import start_master

    return start_master.ensure_supervisor(name)


def _post(path: str, timeout: float = 120.0) -> dict:
    request = urllib.request.Request(f"{URL}{path}", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.load(resp)


if __name__ == "__main__":
    sys.exit(main())
