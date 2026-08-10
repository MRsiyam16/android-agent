"""Flow mapping for *scripted* tests, as opposed to autonomous exploration.

`run_agent.py` explores an unknown app and asks "which distinct screens exist?" — there,
`extractor.compute_state_hash` is exactly right: it strips text from interactive/dynamic
elements so typing "a" then "ab" doesn't invent two screens.

A scripted test asks a different question: "what sequence of steps did we walk, and what
did the app show after each one?" Structural hashing destroys that. In a calculator every
keystroke leaves the UI structure identical, so a 21-tap run collapses onto a single node
with 21 self-loops — technically true, useless as a flow map.

A Journey therefore gives every *step* its own node, chained parent -> child in execution
order, labelled with what the screen actually showed. The result reads as the flow the
test walked. The structural hash is still computed and carried along (`state_hash_struct`)
so a step can be correlated back to an explored screen.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any, Optional

import requests

import config
from extractor import compute_state_hash, extract_actions


def _short_session_code(session_id: str) -> str:
    """A short, collision-resistant node-id prefix for this session.

    Used to be `session_id[:8]`. `device_tools.py` builds a module's Journey session id as
    `f"agent-{slug}"`, so two module slugs sharing a start truncate to the same 8 chars —
    "auth" and "auth-login-password-reset" both give "agent-au", and
    "settings-permissions-biometrics-account" and "search-doctors-clinics-filters" both give
    "agent-se". Two Journeys sharing a prefix mint the same node ids from step 1, and
    `state_store[state_hash] = record` in the telemetry route has no notion of ownership, so
    the second module's steps silently overwrite the first's in server memory — discovered
    when the Settings module's board turned out to be showing the Search module's screens
    under borrowed ids, with no error anywhere to say so. Hashing spreads different session
    ids across the id space instead of collapsing every slug with a shared start onto the
    same short prefix.
    """
    return hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:8]


class Journey:
    """Records an ordered chain of steps to the telemetry server."""

    def __init__(self, device, package: str, activity: str = "",
                 server: str = config.SERVER_URL, session_id: Optional[str] = None):
        self.device = device
        self.package = package
        self.activity = activity
        self.server = server.rstrip("/")
        self.session_id = session_id or str(uuid.uuid4())
        self.step_count = 0
        self.section: Optional[str] = None
        self._prev_node: Optional[str] = None
        #: Whether the most recent `step()` actually reached the board. Exposed rather than
        #: raised: autonomous exploration must not die because telemetry is down (see
        #: `_post`), but the agent's `journey_step` tool needs to stop telling the model a
        #: screen is on the flow graph when it is not.
        self.last_step_posted: bool = True

    def start_section(self, title: str) -> None:
        """Group the steps that follow under a heading on the dashboard."""
        self.section = title

    def status(self, message: str, level: str = "info") -> None:
        self._post("/status", {"session_id": self.session_id, "message": message, "level": level})

    def step(self, label: str, executed_action: Optional[dict] = None,
             xml: Optional[str] = None) -> str:
        """Capture the current screen as the next step in the flow.

        `label` is what the step means to a human reading the map, e.g. "Tap 5 -> 7+5".
        `executed_action` is the tap that produced this screen; it becomes the edge label.
        Returns the node id, so a caller can branch from an earlier step if it needs to.
        """
        self.step_count += 1
        node_id = f"{_short_session_code(self.session_id)}-{self.step_count:03d}"

        if xml is None:
            xml = self.device.dump_xml()
        w, h = self.device.window_size

        self.last_step_posted = self._post("/telemetry", {
            "session_id": self.session_id,
            "device_serial": self.device.serial,
            "package_name": self.package,
            "activity_name": self.activity,
            "state_hash": node_id,
            "parent_state_hash": self._prev_node,
            "screenshot_b64": self.device.screenshot_b64(),
            "available_elements": extract_actions(xml, w, h),
            "executed_action": executed_action,
            "step_label": f"{self.step_count}. {label}",
            "section": self.section,
            "state_hash_struct": compute_state_hash(self.package, self.activity, xml),
        })

        self._prev_node = node_id
        return node_id

    def _post(self, path: str, payload: dict[str, Any]) -> bool:
        """Post one record. Returns whether it landed; never raises.

        Telemetry is observability, never the thing under test — a server that's down must
        not fail the run or mask the app's real result. The return value is how a caller
        that *does* care (the agent's journey_step tool, which promises the model a screen
        is now on the board) can tell the difference.

        A non-2xx is a failure too: the run is posting to its own server, so a 422 from a
        payload the schema rejected drops the step just as completely as a refused
        connection, and used to look identical to success.
        """
        try:
            response = requests.post(f"{self.server}{path}", json=payload, timeout=15)
        except requests.RequestException as exc:
            print(f"[journey] telemetry post to {path} failed: {exc}", flush=True)
            return False
        if response.status_code >= 400:
            print(f"[journey] telemetry post to {path} rejected: "
                  f"{response.status_code} {response.text[:200]}", flush=True)
            return False
        return True
