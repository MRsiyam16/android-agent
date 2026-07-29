"""When a verdict may not be trusted yet.

Both rules here were bought with a false defect. An "Authentication Error" modal made a
correct credential rejection read as "unknown credentials were accepted"; a "Creating your
account..." spinner made a correct duplicate-email refusal read as "a second account was
created with an address already in use". Both flipped to PASS purely by waiting.

The prompt has always carried this as prose. Prose is advice to a model sixty turns deep;
a tool that refuses is not.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:                      # avoids a cycle: device_tools imports this module
    from agent.device_tools import DeviceSession


# screen is what turned a correct credential rejection into "unknown credentials were
# accepted", and a correct duplicate-email refusal into "a second account was created".
_IN_FLIGHT_MARKERS = (
    "loading", "please wait", "signing in", "signing up", "logging in", "creating",
    "submitting", "processing", "uploading", "verifying", "authenticating", "connecting",
    "one moment", "just a moment",
)


def in_flight_text(texts: list[str]) -> Optional[str]:
    """The first on-screen string suggesting a request has not finished, if any."""
    for text in texts:
        lowered = text.lower()
        for marker in _IN_FLIGHT_MARKERS:
            if marker in lowered:
                return text
    return None


def finding_block_reason(session: "DeviceSession") -> Optional[str]:
    """Why the current screen state cannot support a verdict, or None if it can.

    Kept separate from the tool so the rule is a plain function: this is the timing rule that
    the prompt has always stated and that the harness has twice violated anyway, so it is
    worth being able to test it directly rather than only through an MCP round trip.
    """
    if session.actions_since_read:
        return (
            f"You have acted {session.actions_since_read} time(s) since the last read_screen, "
            f"so what you are describing is not what the screen currently shows. Call "
            f"read_screen, confirm the app is still in the state you are reporting, and file "
            f"it then. If the finding is about the screen *before* those actions, say so "
            f"explicitly in `actual` — but re-read first.")

    stalled = in_flight_text(session.last_texts)
    if stalled is not None:
        return (
            f"The last screen you read still showed {stalled!r}, which means the request had "
            f"not finished. A progress overlay hides the form underneath, so a verdict read "
            f"now is about the overlay, not the result — this is exactly how a correct "
            f"rejection has previously been filed as an acceptance. Call "
            f"wait_until_gone(text={stalled!r}), read the screen again, and judge that.")

    return None
