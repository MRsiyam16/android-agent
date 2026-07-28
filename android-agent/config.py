"""Central configuration for the Android App Testing Agent and telemetry server."""
import os
import shutil

try:  # Local secrets (OPENROUTER_API_KEY) live in .env, which is gitignored.
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:  # python-dotenv is optional; env vars still work if set by hand.
    pass


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
# Loopback by default, deliberately. /command is unauthenticated remote control of the
# phone — arbitrary taps, launches and screenshots — so binding it to 0.0.0.0 hands anyone
# on the same Wi-Fi (a cafe, a shared office) a remote for your device and a view of its
# screen. Set SERVER_HOST=0.0.0.0 explicitly if you really do need to reach the dashboard
# from another machine, and only on a network you trust.
SERVER_HOST: str = os.environ.get("SERVER_HOST", "127.0.0.1")
SERVER_PORT: int = int(os.environ.get("SERVER_PORT", 8000))
SERVER_URL: str = os.environ.get("SERVER_URL", f"http://localhost:{SERVER_PORT}")

# --- Exploration limits ---------------------------------------------------------
MAX_STEPS: int = int(os.environ.get("MAX_STEPS", 200))
ACTION_SETTLE_SECONDS: float = float(os.environ.get("ACTION_SETTLE_SECONDS", 0.9))
# Ceiling for the adaptive post-click wait in run_agent.py: it polls the UI and stops as
# soon as the screen actually changes, instead of always sleeping the full amount. This is
# the poll interval between checks; ACTION_SETTLE_SECONDS above is the max total budget.
ACTION_SETTLE_POLL_SECONDS: float = float(os.environ.get("ACTION_SETTLE_POLL_SECONDS", 0.25))
MAX_CONSECUTIVE_BACKTRACKS: int = 6

# How many telemetry records the server keeps for replaying the graph to a browser that
# connects or reloads. A backstop, not a normal-path limit: a 200-step run posts ~400
# records, so the default holds many runs' worth. Old records are dropped oldest-first once
# it is reached, which costs the earliest transitions on a *reload* — the live graph in an
# already-open tab is unaffected, and a saved project is unaffected either way.
TELEMETRY_HISTORY_LIMIT: int = int(os.environ.get("TELEMETRY_HISTORY_LIMIT", 20000))

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

# --- Chat agent (Agent tab) ---------------------------------------------------------
# One model runs everything: the Claude Code CLI, driven in-process via claude-agent-sdk. It
# authenticates with the local Max subscription profile, so the cost is a *rate limit* (rolling
# 5-hour + weekly windows) rather than a per-token charge — and it reads screenshots itself, so
# no second vision model is needed.
#
# An optional cheap OpenRouter tier can take over the high-volume mechanical calls (which
# element to tap, has the screen settled) to spend less of the subscription window. It is OFF
# by default: one model means one set of judgement, no second opinion to reconcile, and nothing
# billed per token. Turn it on with AGENT_USE_CHEAP_TIER=true if a long run starts hitting the
# rate-limit window.
#
# CRITICAL: never set ANTHROPIC_API_KEY in this process's environment. It takes precedence over
# the subscription profile, silently moving every call onto metered API billing.
AGENT_USE_CHEAP_TIER: bool = os.environ.get(
    "AGENT_USE_CHEAP_TIER", "false").lower() in ("1", "true", "yes")
AGENT_PLANNER_MODEL: str = os.environ.get("AGENT_PLANNER_MODEL", "")  # "" = CLI default (subscription)
AGENT_PLANNER_EFFORT: str = os.environ.get("AGENT_PLANNER_EFFORT", "high")

OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL: str = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Vision-capable and cheap. Alternatives worth trying: qwen/qwen3-vl-8b-instruct, or
# bytedance/ui-tars-1.5-7b (trained specifically for GUI grounding).
AGENT_STEPPER_MODEL: str = os.environ.get("AGENT_STEPPER_MODEL", "google/gemini-2.5-flash-lite")
# Passed to the CLI as --fallback-model: a *Claude* model to retry on when the default one is
# overloaded. Empty by default. Note this does not rescue an exhausted subscription window —
# that parks the run instead (see runtime.AgentSession._handle_result), because finishing a
# test case on a different tier and reporting it as equivalent would be dishonest.
AGENT_PLANNER_FALLBACK: str = os.environ.get("AGENT_PLANNER_FALLBACK", "")

# The chat agent reads the screen with a much tighter edge exclusion than exploration does.
# EXCLUDE_BOTTOM_PCT of 0.08 exists to stop autonomous exploration from mashing the gesture
# nav bar, but on a 2400px phone it hides the bottom 192px — which is where a calculator's
# entire bottom keypad row lives, `=` included. A tester needs to reach those; it only has to
# clear the nav bar itself, so 2% is enough.
AGENT_EXCLUDE_TOP_PCT: float = float(os.environ.get("AGENT_EXCLUDE_TOP_PCT", 0.0))
AGENT_EXCLUDE_BOTTOM_PCT: float = float(os.environ.get("AGENT_EXCLUDE_BOTTOM_PCT", 0.02))

# Hard ceiling on agent turns per instruction, so a confused loop can't run forever.
AGENT_MAX_TURNS: int = int(os.environ.get("AGENT_MAX_TURNS", 300))
# How long a single device tool call may block before it's reported as failed.
AGENT_TOOL_TIMEOUT_SECONDS: float = float(os.environ.get("AGENT_TOOL_TIMEOUT_SECONDS", 90))

# --- Persistent per-app memory (optional) ---------------------------------------------------------
# Off by default — enable with --memory or USE_MEMORY=true. Purely local (memory/<package>.json);
# lets a run resume where a previous one left off and avoids re-flagging/re-reviewing screens
# already seen in an earlier run. Independent of USE_LLM_EXPLORATION.
USE_MEMORY: bool = os.environ.get("USE_MEMORY", "false").lower() in ("1", "true", "yes")
