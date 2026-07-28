"""Facebook -> Marketplace -> search 'chair', mapping the user flow to the telemetry dashboard."""
import re
import sys
import time
import uuid
from xml.etree import ElementTree as ET

import requests

import config
from adb_device import AdbDevice
from extractor import compute_state_hash, extract_actions

SERVER = config.SERVER_URL
SESSION_ID = str(uuid.uuid4())
PACKAGE = "com.facebook.katana"

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def log(msg):
    print(f"[chair-search] {msg}", flush=True)


def find_center(xml, predicate):
    """Return (cx, cy) of the first element matching predicate(attrib), else None."""
    root = ET.fromstring(xml)
    for node in root.iter():
        attrib = node.attrib
        if predicate(attrib):
            m = _BOUNDS_RE.match(attrib.get("bounds", ""))
            if m:
                x1, y1, x2, y2 = (int(g) for g in m.groups())
                return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def capture(device, activity, parent_hash=None, executed_action=None):
    xml = device.dump_xml()
    w, h = device.window_size
    state_hash = compute_state_hash(PACKAGE, activity, xml)
    actions = extract_actions(xml, w, h)
    screenshot_b64 = device.screenshot_b64()

    payload = {
        "session_id": SESSION_ID,
        "device_serial": device.serial,
        "package_name": PACKAGE,
        "activity_name": activity,
        "state_hash": state_hash,
        "parent_state_hash": parent_hash,
        "screenshot_b64": screenshot_b64,
        "available_elements": actions,
        "executed_action": executed_action,
    }
    try:
        res = requests.post(f"{SERVER}/telemetry", json=payload, timeout=10)
        log(f"  [telemetry] state {state_hash[:8]} -> {res.status_code}")
    except Exception as exc:
        log(f"  [telemetry] post failed: {exc}")

    return state_hash, actions, xml


def main():
    device = AdbDevice()
    log(f"Connected to device {device.serial}")

    if device.is_locked():
        log("Waking screen and unlocking...")
        device.wake_screen()
        time.sleep(0.5)

    # Bring Facebook to the front (already installed/logged in on this device)
    log("Opening Facebook...")
    device.start_app(PACKAGE)
    time.sleep(3.0)

    activity = "com.facebook.katana.LoginActivity"
    h0, _, _ = capture(device, activity, None, None)
    log(f"Captured Facebook Home ({h0[:8]}).")

    # Facebook's bottom tab bar is custom-drawn and not exposed in the accessibility tree,
    # so the Marketplace tab (4th icon, shop/storefront glyph) is targeted by fixed
    # coordinates confirmed via screenshot on this device (720x1600).
    coords = (420, 1447)
    log(f"Tapping Marketplace tab at {coords}...")
    device.click(*coords)
    time.sleep(3.0)

    h1, _, _ = capture(device, activity, h0, {"label": "Marketplace", "x": coords[0], "y": coords[1]})
    log(f"Captured Marketplace Home ({h1[:8]}).")

    # Search icon (magnifying glass), top-right of the Marketplace header — confirmed via
    # screenshot on this device (720x1600); not reliably exposed in the a11y tree.
    coords = (580, 119)
    log(f"Tapping Search at {coords}...")
    device.click(*coords)
    time.sleep(2.0)

    h2, _, _ = capture(device, activity, h1, {"label": "Search Marketplace", "x": coords[0], "y": coords[1]})
    log(f"Captured Search input screen ({h2[:8]}). Typing 'chair'...")

    device.send_keys("chair", clear=False)
    time.sleep(1.0)
    device.press("enter")
    time.sleep(4.0)

    h3, _, _ = capture(device, activity, h2, {"label": "chair", "x": None, "y": None})
    log(f"Captured 'chair' search results ({h3[:8]}).")

    log(f"\nDone. Flow mapped to {SERVER} (session {SESSION_ID}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
