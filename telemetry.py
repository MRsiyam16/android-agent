"""REST client the agent uses to push state telemetry to the FastAPI dashboard server."""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger("telemetry")


class TelemetryClient:
    def __init__(self, server_url: str, session_id: str, device_serial: str | None = None, timeout: float = 5.0):
        self.base_url = server_url.rstrip("/")
        self.session_id = session_id
        self.device_serial = device_serial
        self.timeout = timeout

    def post_state(
        self,
        package_name: str,
        activity_name: str,
        state_hash: str,
        screenshot_b64: str,
        available_elements: list[dict],
        executed_action: dict | None = None,
        parent_state_hash: str | None = None,
    ) -> bool:
        payload = {
            "session_id": self.session_id,
            "device_serial": self.device_serial,
            "package_name": package_name,
            "activity_name": activity_name,
            "state_hash": state_hash,
            "parent_state_hash": parent_state_hash,
            "screenshot_b64": screenshot_b64,
            "available_elements": available_elements,
            "executed_action": executed_action,
        }
        try:
            resp = requests.post(f"{self.base_url}/telemetry", json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning("telemetry POST failed: %s", exc)
            return False

    def clear(self) -> bool:
        try:
            resp = requests.post(f"{self.base_url}/clear", timeout=self.timeout)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning("telemetry clear failed: %s", exc)
            return False

    def post_status(self, message: str, level: str = "info") -> bool:
        """Push a preflight/progress banner message the dashboard can display."""
        try:
            resp = requests.post(
                f"{self.base_url}/status",
                json={"session_id": self.session_id, "message": message, "level": level},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning("status POST failed: %s", exc)
            return False
