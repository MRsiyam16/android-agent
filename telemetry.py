"""REST client the agent uses to push state telemetry to the FastAPI dashboard server."""
from __future__ import annotations

import logging
import queue
import threading

import requests

logger = logging.getLogger("telemetry")


class TelemetryClient:
    """`post_state`/`post_status` enqueue onto a single background worker thread instead of
    blocking the exploration loop on each HTTP round-trip — neither caller checks the return
    value, and callers never depended on the POST having landed before the next device action,
    so there's nothing lost by decoupling them. A single worker (not a pool) preserves send
    order, since server.py's screen numbering assumes telemetry arrives in the order it was
    produced. Call `flush()` before exiting to make sure queued posts aren't dropped."""

    def __init__(self, server_url: str, session_id: str, device_serial: str | None = None, timeout: float = 5.0):
        self.base_url = server_url.rstrip("/")
        self.session_id = session_id
        self.device_serial = device_serial
        self.timeout = timeout
        self._queue: queue.Queue = queue.Queue()
        self._session = requests.Session()
        self._worker = threading.Thread(target=self._drain_queue, daemon=True)
        self._worker.start()

    def _drain_queue(self) -> None:
        while True:
            path, json_payload = self._queue.get()
            try:
                resp = self._session.post(f"{self.base_url}{path}", json=json_payload, timeout=self.timeout)
                resp.raise_for_status()
            except requests.RequestException as exc:
                logger.warning("telemetry POST %s failed: %s", path, exc)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float | None = None) -> None:
        """Block until every queued post has been sent (or attempted). Call this before the
        process exits so the final steps' telemetry isn't silently dropped with the thread."""
        if timeout is None:
            self._queue.join()
            return
        # queue.Queue has no join(timeout=...); poll instead.
        import time as _time
        deadline = _time.monotonic() + timeout
        while not self._queue.empty() and _time.monotonic() < deadline:
            _time.sleep(0.05)

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
        self._queue.put(("/telemetry", payload))
        return True

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
        self._queue.put(("/status", {"session_id": self.session_id, "message": message, "level": level}))
        return True
