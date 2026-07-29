"""The history replayed to a browser on connect or reload.

This payload is the one place a long session can stall the dashboard: `ingest()` rebuilds
the entire graph from it, so it cannot simply be truncated, but sending a screen's JPEG once
per *visit* rather than once per *screen* multiplies it by however many times exploration
walked back through the same screen.
"""
from __future__ import annotations

import pytest

import config
import server

IMAGE = "/9j/4AAQSkZJRgABAQ" + "A" * 4000  # stand-in for a real base64 JPEG


@pytest.fixture(autouse=True)
def clean_server_state():
    server.state_store.clear()
    server.telemetry_history.clear()
    server._reset_screen_naming()
    yield
    server.state_store.clear()
    server.telemetry_history.clear()
    server._reset_screen_naming()


def post(state_hash: str, screenshot: str = "", parent: str | None = None) -> None:
    """Mimic what post_telemetry stores, including its blank-revisit backfill."""
    server._register_screen(state_hash, parent, None)
    record = {
        "session_id": "s1", "package_name": "com.example.app", "activity_name": ".Main",
        "state_hash": state_hash, "parent_state_hash": parent,
        "screenshot_b64": screenshot, "available_elements": [], "executed_action": None,
    }
    if not record["screenshot_b64"]:
        existing = server.state_store.get(state_hash)
        if existing and existing.get("screenshot_b64"):
            record["screenshot_b64"] = existing["screenshot_b64"]
    record["screen_name"] = server.screen_names[state_hash]
    record["screen_number"] = server.node_index[state_hash]
    server.state_store[state_hash] = record
    server.telemetry_history.append(record)


class TestReplayPayload:
    def test_every_record_still_reaches_the_browser(self):
        # The graph is rebuilt from these, so dropping any record loses a transition.
        post("aaa", IMAGE)
        for _ in range(20):
            post("aaa")
        assert len(server._history_for_replay()) == len(server.telemetry_history) == 21

    def test_a_screens_image_is_sent_once_not_once_per_visit(self):
        post("aaa", IMAGE)
        for _ in range(39):
            post("aaa")   # revisits, each backfilled to carry the image
        replay = server._history_for_replay()
        carrying = [r for r in replay if r["screenshot_b64"]]
        assert len(carrying) == 1, "the same JPEG is on the wire once per visit"
        assert carrying[0] is replay[0] or carrying[0]["screenshot_b64"] == IMAGE

    def test_the_first_record_carries_the_image_so_the_node_renders(self):
        # ingest() falls back to the screenshot it already has for a state, so the image has
        # to arrive on first sight or the node draws blank until the next capture.
        post("aaa", IMAGE)
        post("bbb", IMAGE)
        post("aaa")
        replay = server._history_for_replay()
        assert replay[0]["screenshot_b64"] == IMAGE
        assert replay[1]["screenshot_b64"] == IMAGE
        assert replay[2]["screenshot_b64"] == ""

    def test_an_image_arriving_late_is_backfilled_onto_the_first_sighting(self):
        # run_agent posts a blank for a state it has already reported; if the capture only
        # lands on a later post, first-sight must still be given it.
        post("aaa")                      # blank, nothing known yet
        server.state_store["aaa"]["screenshot_b64"] = IMAGE
        replay = server._history_for_replay()
        assert replay[0]["screenshot_b64"] == IMAGE

    def test_replay_does_not_mutate_the_stored_history(self):
        post("aaa", IMAGE)
        post("aaa")
        server._history_for_replay()
        assert server.telemetry_history[1]["screenshot_b64"] == IMAGE, \
            "stripping for the wire must not damage what the server holds"

    def test_payload_shrinks_by_the_duplication_factor(self):
        import json
        post("aaa", IMAGE)
        for _ in range(39):
            post("aaa")
        naive = len(json.dumps(list(server.telemetry_history)))
        deduped = len(json.dumps(server._history_for_replay()))
        assert deduped < naive / 10


class TestHistoryBound:
    def test_history_is_capped_rather_than_growing_forever(self):
        assert server.telemetry_history.maxlen == config.TELEMETRY_HISTORY_LIMIT

    def test_oldest_records_are_dropped_first(self, monkeypatch):
        from collections import deque
        monkeypatch.setattr(server, "telemetry_history", deque(maxlen=5))
        for i in range(8):
            post(f"h{i}", IMAGE)
        assert [r["state_hash"] for r in server.telemetry_history] == \
               ["h3", "h4", "h5", "h6", "h7"]
