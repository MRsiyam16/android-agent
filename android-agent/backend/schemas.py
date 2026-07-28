"""Request bodies for every endpoint, in one place.

Kept together rather than beside their routes because several are shared across route
modules, and because a mismatch between what the dashboard posts and what a model
declares is the failure this file exists to make easy to check — see AgentTriggerPayload.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ProjectCreatePayload(BaseModel):
    package: str
    # Where to put the project folder. Absent means the default `projects/<package>/`, which
    # is what telemetry from a bare `run_agent.py` still creates.
    root: Optional[str] = None


class TelemetryPayload(BaseModel):
    session_id: str
    device_serial: Optional[str] = None
    package_name: str
    # The app the run was pointed at. `package_name` is only whatever was on screen when the
    # frame was captured, and a run wanders out of its own app constantly — into the Play
    # Store, a browser, the permission controller. Filing by `package_name` is what scattered
    # screenshots into `com.android.vending` and `com.google.android.gms` folders and wrote a
    # deskclock board into Chrome's project. Optional so older clients still post.
    target_package: Optional[str] = None
    activity_name: str = ""
    state_hash: str
    parent_state_hash: Optional[str] = None
    screenshot_b64: str = ""
    available_elements: list[dict] = []
    executed_action: Optional[dict] = None
    # Scripted-journey extras. A journey posts one node per step (its `state_hash` is a
    # per-step id, not a structural hash) so the flow renders as the ordered chain the
    # test actually walked, instead of collapsing onto one self-looping screen.
    step_label: Optional[str] = None
    section: Optional[str] = None
    # The structural hash of the screen this step landed on, so a journey step can still be
    # correlated back to a screen discovered by autonomous exploration.
    state_hash_struct: Optional[str] = None


class CommandPayload(BaseModel):
    command: str
    device_serial: Optional[str] = None


class StatusPayload(BaseModel):
    session_id: Optional[str] = None
    message: str
    level: str = "info"
    popup: bool = False


class AgentMessagePayload(BaseModel):
    text: str
    device_serial: Optional[str] = None


class AttachmentPayload(BaseModel):
    """A reference image pasted or picked in the chat, as a base64 data URL."""
    data_url: str


class ModelPayload(BaseModel):
    """Which Claude model a module's session should run on. None means the CLI default."""
    model: Optional[str] = None


class AgentTriggerPayload(BaseModel):
    """For endpoints that start something rather than say something: /warm and /recon.

    They used to share `AgentMessagePayload`, whose `text` is required — so the `{}` the
    dashboard posts failed validation and both endpoints answered 422. Pre-warming swallowed
    it (it is an optimisation, and the client catches), which meant the advertised "your
    first message does not wait for the CLI to spawn" quietly never happened; Recon surfaced
    it as an error instead. Declaring what these actually accept fixes both.
    """
    device_serial: Optional[str] = None


class SubprojectPayload(BaseModel):
    title: str
    scope: str = ""


class SubprojectUpdatePayload(BaseModel):
    title: Optional[str] = None
    scope: Optional[str] = None
    status: Optional[str] = None


class SecretPayload(BaseModel):
    name: str
    value: str
