"""Central configuration for the Android App Testing Agent and telemetry server."""
import os
import shutil


def _discover_adb_path() -> str:
    """Resolve an adb executable: explicit env var > PATH > common Windows SDK locations."""
    env_path = os.environ.get("ADB_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    which_path = shutil.which("adb")
    if which_path:
        return which_path

    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        os.path.expanduser(r"~\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
        r"C:\Android\platform-tools\adb.exe",
        r"C:\platform-tools\adb.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    # Fall back to bare "adb" and let the caller surface a clear error if it's missing.
    return "adb"


# --- ADB / device -----------------------------------------------------------
ADB_PATH: str = _discover_adb_path()

# --- Telemetry server ---------------------------------------------------------
SERVER_HOST: str = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT: int = int(os.environ.get("SERVER_PORT", 8000))
SERVER_URL: str = os.environ.get("SERVER_URL", f"http://localhost:{SERVER_PORT}")

# --- Exploration limits ---------------------------------------------------------
MAX_STEPS: int = int(os.environ.get("MAX_STEPS", 200))
ACTION_SETTLE_SECONDS: float = float(os.environ.get("ACTION_SETTLE_SECONDS", 0.9))
MAX_CONSECUTIVE_BACKTRACKS: int = 6

# --- Target / package filtering ---------------------------------------------------------
# Leave TARGET_PACKAGE empty and pass --package on the CLI to run_agent.py.
TARGET_PACKAGE: str = os.environ.get("TARGET_PACKAGE", "")

# If ALLOWED_PACKAGES is non-empty, only those packages are considered "in scope" —
# the agent backtracks (BACK) whenever it lands outside this whitelist.
ALLOWED_PACKAGES: set[str] = set(filter(None, os.environ.get("ALLOWED_PACKAGES", "").split(",")))

# Actions whose label or resource-id suggests they hand off to another app entirely
# (camera, gallery picker, voice recorder, share sheet, file export/print) — these almost
# always leave the target app's scope, so the agent skips clicking them rather than
# discover that the hard way via an out-of-scope backtrack loop.
BLOCKED_ACTION_KEYWORDS: set[str] = set(filter(None, os.environ.get(
    "BLOCKED_ACTION_KEYWORDS",
    "camera,photo,gallery,voice,audio,record,share,export,print",
).lower().split(",")))

# Packages the agent will never treat as explorable app state (system chrome, launchers,
# soft keyboards) — landing on one of these always triggers an immediate BACK.
BLOCKED_PACKAGES: set[str] = {
    "com.android.systemui",
    "com.android.launcher",
    "com.android.launcher3",
    "com.google.android.apps.nexuslauncher",
    "com.sec.android.app.launcher",
    "com.samsung.android.app.launcher",
    "com.sec.android.inputmethod",
    "com.google.android.inputmethod.latin",
    "com.google.android.googlequicksearchbox",
    "com.android.settings",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.systemui.ImageWallpaper",
}

# --- Screen boundary exclusion ---------------------------------------------------------
# Fraction of screen height, from each edge, treated as system chrome (status bar / gesture
# nav bar) — clickable elements whose center falls in these bands are ignored.
EXCLUDE_TOP_PCT: float = 0.05
EXCLUDE_BOTTOM_PCT: float = 0.08

# --- Screenshot capture ---------------------------------------------------------
SCREENSHOT_FORMAT: str = "jpeg"
SCREENSHOT_QUALITY: int = 90

# --- LLM-assisted exploration (optional) ---------------------------------------------------------
# Off by default — enable with --llm-explore or USE_LLM_EXPLORATION=true. Requires the `anthropic`
# package and an ANTHROPIC_API_KEY (or `ant auth login` profile); any API failure falls back to the
# existing heuristic exploration, it never blocks a run.
USE_LLM_EXPLORATION: bool = os.environ.get("USE_LLM_EXPLORATION", "false").lower() in ("1", "true", "yes")
LLM_FAST_MODEL: str = os.environ.get("LLM_FAST_MODEL", "claude-haiku-4-5")
LLM_SMART_MODEL: str = os.environ.get("LLM_SMART_MODEL", "claude-opus-4-8")
LLM_SMART_ANALYSIS_INTERVAL: int = int(os.environ.get("LLM_SMART_ANALYSIS_INTERVAL", 5))
LLM_MAX_ACTIONS_TO_MODEL: int = int(os.environ.get("LLM_MAX_ACTIONS_TO_MODEL", 40))

# --- Persistent per-app memory (optional) ---------------------------------------------------------
# Off by default — enable with --memory or USE_MEMORY=true. Purely local (memory/<package>.json);
# lets a run resume where a previous one left off and avoids re-flagging/re-reviewing screens
# already seen in an earlier run. Independent of USE_LLM_EXPLORATION.
USE_MEMORY: bool = os.environ.get("USE_MEMORY", "false").lower() in ("1", "true", "yes")
