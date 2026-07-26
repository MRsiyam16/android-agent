"""One command to bring the system up: `python start.py`

Handles the things that have actually gone wrong when restarting by hand:

* A stale server still holding the port. uvicorn binds with SO_REUSEADDR, so a second
  `python server.py` can look like it started while the *old* process keeps serving the
  old code. This checks first and tells you who owns the port instead of failing quietly.
* Not knowing whether the device is ready until a run fails halfway through.
* The dashboard coming up empty after a restart — telemetry lives in memory, so a
  previous run's flow only returns via the Projects tab. The saved projects are listed
  here so it is obvious what to open.
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

ROOT = Path(__file__).resolve().parent
URL = f"http://localhost:{config.SERVER_PORT}"


def port_owner(port: int) -> str | None:
    """PID currently listening on `port`, if any."""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{port}"):
                return parts[4]
    return None


def process_name(pid: str) -> str:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return out.split(",")[0].strip('"') if out and "No tasks" not in out else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def server_responds(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/projects", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def check_device() -> None:
    """Print device readiness. Never fatal — the dashboard is useful without a device."""
    try:
        out = subprocess.run(
            [config.ADB_PATH, "devices", "-l"], capture_output=True, text=True, timeout=20
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  device : adb not usable ({exc})")
        return

    lines = [l for l in out.splitlines()[1:] if l.strip() and "\t" in l]
    if not lines:
        print("  device : none attached (check the cable / USB debugging)")
        return

    for line in lines:
        serial, rest = line.split("\t", 1)
        model = next((p.split(":", 1)[1] for p in rest.split() if p.startswith("model:")), "?")
        state = rest.split()[0]
        note = ""
        if state == "device":
            try:
                dump = subprocess.run(
                    [config.ADB_PATH, "-s", serial, "shell", "dumpsys", "window"],
                    capture_output=True, text=True, timeout=20,
                ).stdout
                locked = any(
                    "mDreamingLockscreen=true" in l or "isStatusBarKeyguard=true" in l
                    for l in dump.splitlines()
                )
                note = "  LOCKED - unlock before running a test" if locked else "  ready"
            except (OSError, subprocess.SubprocessError):
                note = "  (lock state unknown)"
        print(f"  device : {serial}  {model}  [{state}]{note}")


def list_projects() -> None:
    try:
        with urllib.request.urlopen(f"{URL}/projects", timeout=10) as resp:
            projects = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return
    if not projects:
        return
    print("\nSaved projects (open one from the Projects tab; a restart clears the live graph):")
    for p in projects[:8]:
        print(f"  {p['package']:<45} {p['state_count']:>4} states  {p['edge_count']:>4} transitions")


def main() -> int:
    ap = argparse.ArgumentParser(description="Start the Android Agent dashboard.")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    ap.add_argument("--force", action="store_true", help="kill whatever already holds the port")
    args = ap.parse_args()

    pid = port_owner(config.SERVER_PORT)
    if pid:
        alive = server_responds()
        print(f"Port {config.SERVER_PORT} is held by PID {pid} ({process_name(pid)}), "
              f"{'responding' if alive else 'NOT responding (stale socket)'}.")
        if not args.force:
            if alive:
                print("A server is already up. Re-run with --force to restart it "
                      "(needed after editing server.py; it does not auto-reload).")
                if not args.no_browser:
                    webbrowser.open(URL)
                list_projects()
                return 0
            print("Re-run with --force to clear it.")
            return 1
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, timeout=20)
        for _ in range(20):
            if not port_owner(config.SERVER_PORT):
                break
            time.sleep(0.25)
        print(f"Stopped PID {pid}.")

    print(f"Starting server on {URL} ...")
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT)

    for _ in range(60):
        if server_responds():
            break
        if proc.poll() is not None:
            print("Server exited during startup. Run `python server.py` to see the error.")
            return 1
        time.sleep(0.5)
    else:
        print("Server did not come up in 30s.")
        proc.terminate()
        return 1

    print(f"\n  server : {URL}  (pid {proc.pid})")
    check_device()
    list_projects()
    print("\nCtrl+C to stop.\n")

    if not args.no_browser:
        webbrowser.open(URL)

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
