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
import llm_explorer
import memory
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
    parser.add_argument(
        "--llm-explore",
        action="store_true",
        help="Use Claude (fast model for taps, smart model for bug review) during exploration",
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="Persist per-app exploration memory (memory/<package>.json) to resume across runs "
             "and avoid re-flagging bugs already found",
    )
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


def run(
    package: str,
    max_steps: int,
    serial: str | None,
    server_url: str,
    llm_explore: bool = False,
    memory_flag: bool = False,
) -> None:
    if not package:
        raise SystemExit("--package is required (or set TARGET_PACKAGE in config.py)")

    use_llm = llm_explore or config.USE_LLM_EXPLORATION
    if use_llm:
        logger.info(
            "LLM-assisted exploration enabled (fast=%s, smart=%s, every %d new states)",
            config.LLM_FAST_MODEL, config.LLM_SMART_MODEL, config.LLM_SMART_ANALYSIS_INTERVAL,
        )

    use_memory = memory_flag or config.USE_MEMORY
    app_memory = memory.load(package) if use_memory else None
    if use_memory:
        logger.info("Persistent memory enabled for %s", package)

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
    new_states_since_analysis = 0
    # Carried forward from the previous iteration's post-click read (same screen this
    # iteration starts from) so we don't re-fetch the UI dump/app-info/screenshot from the
    # device a second time for the exact same screen — that redundant round-trip was the
    # single largest per-step cost. Left as None whenever the next screen is genuinely
    # unknown (after BACK, after an exhausted-state backtrack, after a restart) so those
    # paths still force a fresh read.
    pending: dict | None = None

    while step < max_steps:
        step += 1
        if pending is not None:
            width, height = pending["width"], pending["height"]
            xml = pending["xml"]
            current_package = pending["package"]
            current_activity = pending["activity"]
            carried_screenshot_b64 = pending["screenshot_b64"]
            pending = None
        else:
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
            carried_screenshot_b64 = None

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
        is_new_state = state_hash not in graph.nodes
        is_new_ever = is_new_state and not (use_memory and app_memory.is_known(state_hash))
        graph.upsert_node(state_hash, current_package, current_activity, actions)

        if use_memory and is_new_state:
            # First time this state is upserted into *this run's* graph — replay any
            # actions a previous run already tried here so they aren't re-tapped, and
            # record the state as known (idempotent if it already was).
            for prev_action_id in app_memory.previously_tried(state_hash):
                graph.mark_tried(state_hash, prev_action_id)
            app_memory.record_new_state(state_hash)
            memory.save(app_memory)

        if carried_screenshot_b64 is not None:
            # Same screen we just captured at the end of the previous iteration — reuse it
            # instead of taking a fresh screenshot of an unchanged screen.
            screenshot_b64 = carried_screenshot_b64
        elif is_new_ever or use_llm:
            # Only worth the capture cost for a screen the dashboard/memory hasn't seen
            # before, or when the LLM chooser needs an image to pick a tap regardless of
            # novelty. A blank screenshot_b64 on a known revisit is fine to post — server.py
            # backfills it from the state's previously-stored screenshot, and dashboard.js's
            # ingest() falls back the same way client-side.
            try:
                screenshot_b64 = device.screenshot_b64()
            except DeviceError as exc:
                logger.warning("[step %d] screenshot failed: %s", step, exc)
                screenshot_b64 = ""
        else:
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

        if use_llm and is_new_ever:
            # is_new_ever (not just is_new_state) so a screen already reviewed in a
            # previous run doesn't get re-analyzed/re-flagged every time we revisit it.
            new_states_since_analysis += 1
            if new_states_since_analysis >= config.LLM_SMART_ANALYSIS_INTERVAL:
                new_states_since_analysis = 0
                analysis = llm_explorer.analyze_screen(screenshot_b64, current_package, current_activity)
                if analysis:
                    level = "warning" if analysis["bug_suspected"] else "info"
                    telemetry.post_status(f"[AI review] {current_activity}: {analysis['summary']}", level=level)
                    if use_memory:
                        app_memory.record_analysis(state_hash, current_activity, analysis)
                        memory.save(app_memory)

        memory_hints = app_memory.outcome_hints(state_hash) if use_memory else None
        chooser = (
            (lambda cands: llm_explorer.pick_action(
                cands, screenshot_b64, current_package, current_activity, memory_hints=memory_hints,
            ))
            if use_llm else None
        )
        action = graph.pick_next_action(state_hash, chooser=chooser)

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

        # Adaptive settle: poll for the screen to actually change instead of always sleeping
        # the full ACTION_SETTLE_SECONDS ceiling. Most taps render well under that budget (a
        # calculator digit, a toggle) — this captures the resulting state as soon as it
        # differs from the pre-click state, and only burns the full wait on screens that are
        # genuinely slow to update. This same read is carried forward as next iteration's
        # starting state (see `pending` above) instead of being re-fetched a moment later.
        next_package, next_activity = current_package, current_activity
        next_width, next_height = width, height
        xml_after: str | None = None
        next_hash = state_hash
        deadline = time.monotonic() + config.ACTION_SETTLE_SECONDS
        while True:
            try:
                next_width, next_height = device.window_size
                xml_after = device.dump_xml()
                app_info_after = device.current_app()
                next_package = app_info_after.get("package", "")
                next_activity = app_info_after.get("activity", "")
                next_hash = compute_state_hash(next_package, next_activity, xml_after)
            except DeviceError as exc:
                logger.warning("[step %d] post-action read failed: %s", step, exc)
                next_hash = state_hash
                xml_after = None
                break
            if next_hash != state_hash or time.monotonic() >= deadline:
                break
            time.sleep(config.ACTION_SETTLE_POLL_SECONDS)

        next_is_new_ever = next_hash not in graph.nodes and not (use_memory and app_memory.is_known(next_hash))

        graph.record_edge(state_hash, next_hash, action["id"], f"click: {action['label']}")

        if use_memory:
            app_memory.record_tried(state_hash, action["id"], led_to_new_state=next_is_new_ever)
            memory.save(app_memory)

        # Only report the resulting screen to the dashboard/map if it's still in scope —
        # otherwise an action that briefly hands off to another app (a permission dialog,
        # a stray intent) leaks that other app's screen into the graph, even though the
        # loop's own in-scope check (top of the next iteration) will correctly BACK out of
        # it without exploring further.
        if in_scope(next_package):
            if next_is_new_ever or use_llm:
                try:
                    screenshot_after_b64 = device.screenshot_b64()
                except DeviceError:
                    screenshot_after_b64 = ""
            else:
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

            if xml_after is not None:
                pending = {
                    "width": next_width,
                    "height": next_height,
                    "xml": xml_after,
                    "package": next_package,
                    "activity": next_activity,
                    "screenshot_b64": screenshot_after_b64,
                }
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
    # post_state/post_status are fire-and-forget onto a background thread — make sure the
    # last few posts actually land before the process exits instead of being dropped with it.
    telemetry.flush(timeout=10.0)


def main() -> None:
    args = parse_args()
    run(
        args.package, args.steps, args.serial, args.server,
        llm_explore=args.llm_explore, memory_flag=args.memory,
    )


if __name__ == "__main__":
    main()
