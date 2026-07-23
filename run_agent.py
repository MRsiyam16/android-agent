"""CLI entry point: autonomously explores an Android app and streams telemetry to server.py.

Usage:
    python run_agent.py --package com.example.app --steps 100 --serial R5CR12GJAJY
"""
from __future__ import annotations

import argparse
import logging
import time
import uuid

import config
from adb_device import AdbDevice, DeviceError, list_serials
from extractor import compute_state_hash, extract_actions
from graph import ExplorationGraph
from telemetry import TelemetryClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("run_agent")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autonomous Android app exploration agent")
    parser.add_argument("--package", default=config.TARGET_PACKAGE, help="Target app package name")
    parser.add_argument("--steps", type=int, default=config.MAX_STEPS, help="Max exploration steps")
    parser.add_argument("--serial", default=None, help="ADB device serial (auto-detected if omitted)")
    parser.add_argument("--server", default=config.SERVER_URL, help="Telemetry server base URL")
    return parser.parse_args()


def resolve_serial(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        serials = list_serials()
    except DeviceError as exc:
        logger.warning("Could not enumerate adb devices (%s); falling back to default connect()", exc)
        return None
    if not serials:
        logger.warning("No 'device'-state serials found via adb; falling back to default connect()")
        return None
    if len(serials) > 1:
        logger.info("Multiple devices attached (%s); using the first one: %s", serials, serials[0])
    return serials[0]


def in_scope(package: str) -> bool:
    if package in config.BLOCKED_PACKAGES:
        return False
    if config.ALLOWED_PACKAGES:
        return package in config.ALLOWED_PACKAGES
    return True


def ensure_device_ready(device: AdbDevice, telemetry: TelemetryClient, timeout: float = 120.0) -> None:
    """Preflight checklist: screen must be on and unlocked before exploration can begin.

    We can wake the display, but we will never attempt to enter a PIN/pattern/password —
    that's the user's device credential. If it's locked, we wait (with a visible prompt on
    both the CLI and the dashboard) for the user to unlock it themselves.
    """
    try:
        if not device.is_screen_on():
            logger.info("Screen is off — waking it...")
            device.wake_screen()
            time.sleep(1.0)
    except DeviceError as exc:
        logger.warning("Could not check/wake screen: %s", exc)

    deadline = time.monotonic() + timeout
    warned = False
    while True:
        try:
            locked = device.is_locked()
        except DeviceError as exc:
            logger.warning("Could not check lock state: %s", exc)
            break

        if not locked:
            if warned:
                logger.info("Device unlocked — continuing.")
                telemetry.post_status("Device unlocked — starting exploration.", level="ok")
            break

        if not warned:
            logger.warning("🔒 Device is locked. Please unlock your phone to start testing.")
            telemetry.post_status("🔒 Please unlock your phone to start testing.", level="warning")
            warned = True

        if time.monotonic() >= deadline:
            telemetry.post_status("Timed out waiting for device unlock.", level="error")
            raise SystemExit("Timed out waiting for the device to be unlocked. Unlock your phone and try again.")

        time.sleep(2.0)


def run(package: str, max_steps: int, serial: str | None, server_url: str) -> None:
    if not package:
        raise SystemExit("--package is required (or set TARGET_PACKAGE in config.py)")

    resolved_serial = resolve_serial(serial)
    logger.info("Connecting to device%s...", f" ({resolved_serial})" if resolved_serial else "")
    device = AdbDevice(resolved_serial)
    logger.info("Connected: serial=%s", device.serial)

    session_id = uuid.uuid4().hex[:12]
    telemetry = TelemetryClient(server_url, session_id, device_serial=device.serial)
    graph = ExplorationGraph()

    telemetry.post_status("Running preflight checks (screen on, device unlocked)...", level="info")
    ensure_device_ready(device, telemetry)

    logger.info("Starting app: %s", package)
    telemetry.post_status(f"Launching {package}...", level="info")
    device.start_app(package)
    time.sleep(1.5)

    prev_state_hash: str | None = None
    step = 0

    while step < max_steps:
        step += 1
        try:
            width, height = device.window_size
            xml = device.dump_xml()
            app_info = device.current_app()
            current_package = app_info.get("package", "")
            current_activity = app_info.get("activity", "")
        except DeviceError as exc:
            logger.error("[step %d] device read failed: %s — retrying shortly", step, exc)
            time.sleep(1.0)
            continue

        if not in_scope(current_package):
            logger.info("[step %d] out-of-scope package '%s' — pressing BACK", step, current_package)
            try:
                device.press("back")
            except DeviceError as exc:
                logger.warning("[step %d] BACK failed: %s", step, exc)
            graph.note_backtrack()
            time.sleep(config.ACTION_SETTLE_SECONDS)
            if graph.should_force_stop():
                logger.warning("Max backtracks reached out-of-scope — restarting target app '%s'", package)
                try:
                    device.start_app(package)
                    graph.note_progress()
                except DeviceError:
                    break
            continue

        state_hash = compute_state_hash(current_package, current_activity, xml)
        actions = extract_actions(xml, width, height)
        graph.upsert_node(state_hash, current_package, current_activity, actions)

        try:
            screenshot_b64 = device.screenshot_b64()
        except DeviceError as exc:
            logger.warning("[step %d] screenshot failed: %s", step, exc)
            screenshot_b64 = ""

        telemetry.post_state(
            package_name=current_package,
            activity_name=current_activity,
            state_hash=state_hash,
            screenshot_b64=screenshot_b64,
            available_elements=actions,
            executed_action=None,
            parent_state_hash=prev_state_hash,
        )

        action = graph.pick_next_action(state_hash)

        if action is None:
            logger.info("[step %d] state %s exhausted (%d actions tried) — BACK", step, state_hash[:8], len(actions))
            try:
                device.press("back")
            except DeviceError as exc:
                logger.warning("[step %d] BACK failed: %s", step, exc)
            graph.note_backtrack()
            if graph.should_force_stop():
                logger.warning("Max backtracks reached — restarting target app '%s'", package)
                try:
                    device.start_app(package)
                    graph.note_progress()
                except DeviceError:
                    break
            prev_state_hash = state_hash
            time.sleep(config.ACTION_SETTLE_SECONDS)
            continue

        graph.note_progress()
        logger.info("[step %d] state %s -> click '%s' @ (%d,%d)",
                    step, state_hash[:8], action["label"], action["x"], action["y"])

        try:
            device.click(action["x"], action["y"])
        except DeviceError as exc:
            logger.warning("[step %d] click failed: %s", step, exc)
            graph.mark_tried(state_hash, action["id"])
            time.sleep(config.ACTION_SETTLE_SECONDS)
            continue

        time.sleep(config.ACTION_SETTLE_SECONDS)

        # Capture the resulting state and report the edge that produced it.
        next_package, next_activity = current_package, current_activity
        try:
            xml_after = device.dump_xml()
            app_info_after = device.current_app()
            next_package = app_info_after.get("package", "")
            next_activity = app_info_after.get("activity", "")
            next_hash = compute_state_hash(next_package, next_activity, xml_after)
        except DeviceError as exc:
            logger.warning("[step %d] post-action read failed: %s", step, exc)
            next_hash = state_hash

        graph.record_edge(state_hash, next_hash, action["id"], f"click: {action['label']}")

        # Only report the resulting screen to the dashboard/map if it's still in scope —
        # otherwise an action that briefly hands off to another app (a permission dialog,
        # a stray intent) leaks that other app's screen into the graph, even though the
        # loop's own in-scope check (top of the next iteration) will correctly BACK out of
        # it without exploring further.
        if in_scope(next_package):
            try:
                screenshot_after_b64 = device.screenshot_b64()
            except DeviceError:
                screenshot_after_b64 = ""

            telemetry.post_state(
                package_name=next_package,
                activity_name=next_activity,
                state_hash=next_hash,
                screenshot_b64=screenshot_after_b64,
                available_elements=[],
                executed_action={"label": action["label"], "x": action["x"], "y": action["y"], "from_state": state_hash},
                parent_state_hash=state_hash,
            )
        else:
            logger.info("[step %d] resulting state is out-of-scope (%s) — not reporting to dashboard", step, next_package)

        prev_state_hash = next_hash

    logger.info(
        "Exploration finished: %d steps, %d unique states, %d edges",
        step, graph.node_count, graph.edge_count,
    )
    telemetry.post_status(
        f"Exploration finished — {graph.node_count} states, {graph.edge_count} transitions.",
        level="ok",
    )


def main() -> None:
    args = parse_args()
    run(args.package, args.steps, args.serial, args.server)


if __name__ == "__main__":
    main()
