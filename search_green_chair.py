"""Search for 'green chair' on Facebook Marketplace and map user flow to telemetry server."""
import subprocess
import sys
import time
import uuid
import requests

import config
from adb_device import AdbDevice
from extractor import compute_state_hash, extract_actions

SERVER = config.SERVER_URL
SESSION_ID = str(uuid.uuid4())
PACKAGE = "com.facebook.katana"
ACTIVITY = "com.facebook.katana.LoginActivity"


def log(msg):
    clean_msg = str(msg).encode("ascii", "replace").decode("ascii")
    print(f"[fb-test] {clean_msg}", flush=True)


def capture(device, parent_hash=None, executed_action=None):
    xml = device.dump_xml()
    w, h = device.window_size
    state_hash = compute_state_hash(PACKAGE, ACTIVITY, xml)
    actions = extract_actions(xml, w, h)
    screenshot_b64 = device.screenshot_b64()

    payload = {
        "session_id": SESSION_ID,
        "device_serial": device.serial,
        "package_name": PACKAGE,
        "activity_name": ACTIVITY,
        "state_hash": state_hash,
        "parent_state_hash": parent_hash,
        "screenshot_b64": screenshot_b64,
        "available_elements": actions,
        "executed_action": executed_action,
    }
    try:
        res = requests.post(f"{SERVER}/telemetry", json=payload, timeout=10)
        if res.status_code == 200:
            log(f"  [telemetry] state {state_hash[:8]} posted successfully")
    except Exception as exc:
        log(f"  [telemetry] post failed: {exc}")

    return state_hash, actions, xml


def main():
    device = AdbDevice()
    log(f"Connected to device {device.serial}")

    # Wake screen if off
    if device.is_locked():
        log("Waking screen and unlocking...")
        device.wake_screen()
        time.sleep(0.5)

    # Launch Facebook Marketplace directly via deep link
    log("Opening Facebook Marketplace...")
    try:
        subprocess.run(
            [config.ADB_PATH, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "fb://marketplace"],
            timeout=10,
            capture_output=True,
            check=False,
        )
    except Exception as exc:
        log(f"Launch failed: {exc}")
        return 1

    time.sleep(4.0)

    # Step 1: Marketplace Home
    h1, actions1, _ = capture(device, None, None)
    log("Captured Facebook Marketplace Home.")

    # Find search icon (top right, usually near x=580, y=119 or 668, y=119)
    # Let's tap the search button directly using coords from previous checks
    log("Tapping search button...")
    device.click(580, 119)
    time.sleep(2.0)

    # Step 2: Search Input Screen
    h2, actions2, _ = capture(device, h1, {"label": "Search Marketplace", "resource_id": "com.facebook.katana:id/search_button"})
    log("Captured Search input screen. Typing 'green chair'...")

    # Type "green chair" (spaces replaced by %s in ADB input text command)
    try:
        subprocess.run(
            [config.ADB_PATH, "shell", "input", "text", "green%schair"],
            timeout=10,
            capture_output=True,
            check=False,
        )
        time.sleep(1.0)
        # Press Enter
        subprocess.run(
            [config.ADB_PATH, "shell", "input", "keyevent", "66"],
            timeout=10,
            capture_output=True,
            check=False,
        )
    except Exception as exc:
        log(f"Typing failed: {exc}")
        return 1

    time.sleep(4.0)

    # Step 3: Search Results Screen
    h3, actions3, _ = capture(device, h2, {"label": "green chair", "resource_id": "com.facebook.katana:id/search_edit_text"})
    log("Captured green chair search results.")

    log("\n🎉 Search complete and user flow mapped to http://localhost:8000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
