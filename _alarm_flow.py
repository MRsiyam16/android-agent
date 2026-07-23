"""One-off driver: open Clock app, create a 5 PM alarm, streaming telemetry to the
running dashboard server so the flow gets mapped live. Not part of the reusable agent."""
import sys
import time
import uuid

import requests

import config
from adb_device import AdbDevice
from extractor import compute_state_hash, extract_actions

SERVER = config.SERVER_URL
SESSION_ID = str(uuid.uuid4())


def log(msg):
    print(f"[flow] {msg}".encode("ascii", "replace").decode("ascii"), flush=True)


PACKAGE = "com.google.android.deskclock"


def capture(device, parent_hash=None, executed_action=None):
    xml = device.dump_xml()
    # current_app() is unreliable on this device (reports a stale foreground app), but
    # the UI dump itself is ground truth and every node below is stamped deskclock, so
    # the package is hardcoded for this flow rather than trusted from current_app().
    w, h = device.window_size
    state_hash = compute_state_hash(PACKAGE, "", xml)
    actions = extract_actions(xml, w, h)
    screenshot_b64 = device.screenshot_b64()

    payload = {
        "session_id": SESSION_ID,
        "device_serial": device.serial,
        "package_name": PACKAGE,
        "activity_name": "",
        "state_hash": state_hash,
        "parent_state_hash": parent_hash,
        "screenshot_b64": screenshot_b64,
        "available_elements": actions,
        "executed_action": executed_action,
    }
    try:
        requests.post(f"{SERVER}/telemetry", json=payload, timeout=10)
    except Exception as exc:  # noqa: BLE001
        log(f"telemetry post failed: {exc}")

    return state_hash, actions, xml


def find(actions, *needles):
    needles = [n.lower() for n in needles]
    for a in actions:
        label = (a.get("label") or "").lower()
        rid = (a.get("resource_id") or "").lower()
        if any(n in label or n in rid for n in needles):
            return a
    return None


def dump_labels(actions, tag):
    log(f"-- elements on screen ({tag}) --")
    for a in actions:
        log(f"   '{a['label']}' rid={a['resource_id']} clickable={a['clickable']} @({a['x']},{a['y']})")


def main():
    device = AdbDevice()
    log(f"connected to {device.serial}")

    if device.is_locked():
        log("device appears locked; waking screen")
        device.wake_screen()

    device.start_app("com.google.android.deskclock")
    time.sleep(1.5)

    h1, actions1, _ = capture(device, None, None)
    dump_labels(actions1, "clock app launched (Alarm tab)")

    fab = find(actions1, "add alarm", "fab")
    if not fab:
        log("could not find Add alarm FAB; aborting")
        return 1
    log(f"clicking '{fab['label']}' @ ({fab['x']},{fab['y']})")
    device.click(fab["x"], fab["y"])
    time.sleep(1.0)

    h2, actions2, xml2 = capture(device, h1, {"label": fab["label"], "resource_id": fab["resource_id"]})
    dump_labels(actions2, "after tapping Add alarm")

    hour5 = next((a for a in actions2 if a["label"] == "5" and not a["resource_id"]), None)
    if not hour5:
        log("could not find hour '5' on clock face; aborting")
        return 1
    log(f"clicking hour '5' @ ({hour5['x']},{hour5['y']})")
    device.click(hour5["x"], hour5["y"])
    time.sleep(0.8)

    h3, actions3, _ = capture(device, h2, {"label": "hour 5", "resource_id": ""})
    dump_labels(actions3, "after selecting hour 5 (should be minute mode now)")

    minute00 = next(
        (a for a in actions3 if a["label"] == "00" and "minute" in (a["resource_id"] or "")),
        None,
    ) or next((a for a in actions3 if a["label"] == "0" and not a["resource_id"]), None)
    if minute00:
        log(f"clicking minute '00' @ ({minute00['x']},{minute00['y']})")
        device.click(minute00["x"], minute00["y"])
        time.sleep(0.6)
    else:
        log("minute already shows 00 (or no explicit minute element found); leaving as-is")

    h4, actions4, _ = capture(device, h3, {"label": "minute 00", "resource_id": ""})
    dump_labels(actions4, "after confirming minute 00")

    pm = find(actions4, "pm")
    if not pm:
        log("could not find PM toggle; aborting")
        return 1
    log(f"clicking 'PM' @ ({pm['x']},{pm['y']})")
    device.click(pm["x"], pm["y"])
    time.sleep(0.6)

    h5, actions5, _ = capture(device, h4, {"label": pm["label"], "resource_id": pm["resource_id"]})
    dump_labels(actions5, "after selecting PM")

    ok = find(actions5, "ok")
    if not ok:
        log("could not find OK button; aborting")
        return 1
    log(f"clicking 'OK' @ ({ok['x']},{ok['y']})")
    device.click(ok["x"], ok["y"])
    time.sleep(1.2)

    h6, actions6, _ = capture(device, h5, {"label": ok["label"], "resource_id": ok["resource_id"]})
    dump_labels(actions6, "final: back on Alarm list with new 5:00 PM alarm")

    log("done — flow map streamed to dashboard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
