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
    # "android" | "ios" | "web" | "windows". Absent defaults to "android" for back-compat with
    # every existing caller. For a web project, `package` holds the target URL rather than a
    # package/bundle id; for windows, it holds the target executable's path — the same
    # field-sharing `device_tools.DeviceSession` already does between Android package names
    # and iOS bundle ids.
    platform: Optional[str] = None
    # Windows-only: the VirtualBox VM name (device.create_device's `serial`). Lets a Windows
    # project set its VM at creation instead of a separate pin-device call; a harmless no-op
    # for the other three platforms, which resolve their device some other way.
    device_serial: Optional[str] = None
    # Windows-only: which snapshot WindowsDevice.restore_snapshot() targets for this project.
    # Absent falls back to config.WINDOWS_DEFAULT_SNAPSHOT.
    snapshot_name: Optional[str] = None


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
    # Which module's live device to reuse, if one is already open. Not optional in practice
    # for a web project: each agent session owns one running browser, and resolving without
    # these would open a second, disconnected one instead of reaching the one on screen.
    package: Optional[str] = None
    slug: Optional[str] = None


class WindowsResetPayload(BaseModel):
    """Manual "Reset VM to clean snapshot" trigger — see `/device/windows/restore-snapshot`.

    Deliberately its own route rather than the agent's `reset_app_data` tool: a snapshot
    restore reboots the whole VM and routinely takes minutes, far past
    `config.AGENT_TOOL_TIMEOUT_SECONDS` — see `windows_device.py`'s module docstring.
    """
    package: Optional[str] = None
    slug: Optional[str] = None
    device_serial: Optional[str] = None
    snapshot_name: Optional[str] = None


class ViewportPayload(BaseModel):
    """Resize a web project's browser viewport — see `/device/viewport`."""
    package: Optional[str] = None
    slug: Optional[str] = None
    width: int
    height: int


class ResponsiveSweepPayload(BaseModel):
    """One-click breakpoint sweep from the dashboard — see `/device/responsive-sweep`."""
    package: Optional[str] = None
    slug: Optional[str] = None
    breakpoints: Optional[list[dict]] = None


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


class FindingTrackingPayload(BaseModel):
    """Where a finding is tracked externally, and whether it's resolved.

    All optional and applied with `exclude_unset` so a caller can update just one field
    (e.g. flip `resolved` after a Blackcode issue closes) without re-sending the others.
    """
    resolved: Optional[bool] = None
    issue_url: Optional[str] = None
    issue_id: Optional[int] = None
