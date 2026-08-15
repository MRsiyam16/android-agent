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

# --- iOS / WebDriverAgent -----------------------------------------------------------
# The iOS adapter talks to WebDriverAgent over HTTP on a forwarded port, and shells out to
# the pymobiledevice3 CLI for the few things WDA does not cover (device list, installed
# apps, crash reports). Nothing here is used unless an iPhone is actually selected.
#
# WDA has to be running before any of it works, and it does not start itself. Three
# processes, each in its own terminal, device unlocked:
#   1. pymobiledevice3 remote tunneld                                  (as Administrator)
#   2. pymobiledevice3 developer dvt xcuitest <WDA_BUNDLE_ID> --tunnel <udid>
#   3. pymobiledevice3 usbmux forward 8100 8100 --udid <udid>
PYMOBILEDEVICE3_PATH: str = os.environ.get("PYMOBILEDEVICE3_PATH", "pymobiledevice3")
WDA_URL: str = os.environ.get("WDA_URL", "http://127.0.0.1:8100")

# The runner's bundle id carries the signing team suffix, because a free Apple ID cannot
# claim `com.facebook.WebDriverAgentRunner.xctrunner` itself — Sideloadly appends the team
# id to make it unique. Set WDA_BUNDLE_ID to whatever `pymobiledevice3 apps list` reports
# after re-signing; it changes if the app is ever signed by a different Apple ID.
WDA_BUNDLE_ID: str = os.environ.get(
    "WDA_BUNDLE_ID", "com.facebook.WebDriverAgentRunner.xctrunner")

# Listing installed apps costs a CLI round trip of several seconds, and it is asked once per
# launch to check the bundle id exists. Cached for this long.
IOS_APP_LIST_TTL_SECONDS: float = float(os.environ.get("IOS_APP_LIST_TTL_SECONDS", 120))

# --- Web / Playwright -----------------------------------------------------------
# The web adapter drives a real Chromium tab via Playwright, launched on first use. Nothing
# here is used unless a website target is actually selected, and importing `playwright` itself
# is deferred to `device.create_device()` — a checkout with no Playwright installed still
# starts fine for Android/iOS.
#
# Visible by default (not headless), matching this dashboard's own spirit of watching a run
# happen live rather than being told about it afterwards. Set WEB_HEADLESS=true for CI-style
# runs where nobody is watching.
WEB_HEADLESS: bool = os.environ.get("WEB_HEADLESS", "false").lower() in ("1", "true", "yes")
WEB_BROWSER_CHANNEL: str = os.environ.get("WEB_BROWSER_CHANNEL", "chromium")
WEB_DEFAULT_VIEWPORT: tuple[int, int] = (
    int(os.environ.get("WEB_VIEWPORT_W", 1280)), int(os.environ.get("WEB_VIEWPORT_H", 800)))

# A Playwright context with no `device_scale_factor` defaults to 1 — every screenshot comes
# back at native CSS-pixel resolution, the same as a non-Retina display. Text and fine UI
# edges are soft at that density even before anything downstream re-scales them for a
# thumbnail. 2 matches a standard Retina/high-DPI capture (a 1280x800 viewport screenshots
# at 2560x1600) and is what the flow-graph board and the detail modal both show. Coordinates
# are unaffected: Playwright's click/bounds/viewport APIs stay in CSS pixels regardless of
# this value, so taps and element rects do not need any matching downscale the way iOS's
# WDA screenshots do (see ios_device.screenshot_b64).
WEB_SCREENSHOT_SCALE: float = float(os.environ.get("WEB_SCREENSHOT_SCALE", 2))

# Breakpoints `check_responsive` sweeps when the agent doesn't name its own. Named after common
# device classes rather than exact devices — nothing here claims to be a specific iPhone or iPad.
WEB_BREAKPOINTS: dict[str, tuple[int, int]] = {
    "mobile": (375, 812),
    "tablet": (768, 1024),
    "desktop": (1440, 900),
}
WEB_NAV_TIMEOUT_SECONDS: float = float(os.environ.get("WEB_NAV_TIMEOUT_SECONDS", 30))

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

# --- Blackcode issue tracker (optional) -------------------------------------------------
# `bk` is the Blackcode CLI (https://issues.blackcode.ch) — the only supported interface to
# the platform; there is no public HTTP API to call directly (see blackcode.py's module
# docstring). Auth is `bk`'s own concern (`bk login`, stored under ~/.config/bk/) — this app
# never sees or stores a Blackcode token itself.
BLACKCODE_CLI: str = os.environ.get("BLACKCODE_CLI", "bk")
BLACKCODE_CLI_TIMEOUT_SECONDS: float = float(os.environ.get("BLACKCODE_CLI_TIMEOUT_SECONDS", 30))

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

# Ceiling on agent turns per instruction. Unset (None) by default, meaning no cap — the SDK
# only passes --max-turns to the CLI when this is truthy. Set AGENT_MAX_TURNS to re-enable one.
_agent_max_turns_raw = os.environ.get("AGENT_MAX_TURNS", "")
AGENT_MAX_TURNS: int | None = int(_agent_max_turns_raw) if _agent_max_turns_raw else None
# How long a single device tool call may block before it's reported as failed.
AGENT_TOOL_TIMEOUT_SECONDS: float = float(os.environ.get("AGENT_TOOL_TIMEOUT_SECONDS", 90))

# When the ecosystem manager starts a module — rather than you pressing Send — open that
# module in a browser tab so the run is visible instead of happening somewhere off-screen.
# A run you started yourself never opens one: you are already looking at it.
#
# On by default because an invisible run is the failure mode this whole tier introduced: the
# manager can start four suites across four apps, and without this the only evidence is a
# number changing on a board. Set AGENT_OPEN_MODULE_TABS=false if the tabs get in the way —
# nothing else depends on it, and /manager still lists what is running.
AGENT_OPEN_MODULE_TABS: bool = os.environ.get(
    "AGENT_OPEN_MODULE_TABS", "true").lower() in ("1", "true", "yes")

# Which browser those tabs open in: "chrome", "edge", "firefox", a full path to an
# executable, or "default" for whatever Windows has registered.
#
# "default" is not the default, because on this machine it was wrong in a way that is easy to
# miss: the registered handler is Edge, so every watch tab opened in a browser the user was
# not looking at while they worked in Chrome. A tab you have to go and find is barely better
# than no tab. Name the browser you actually use.
AGENT_BROWSER: str = os.environ.get("AGENT_BROWSER", "default").strip()

# --- Persistent per-app memory (optional) ---------------------------------------------------------
# Off by default — enable with --memory or USE_MEMORY=true. Purely local (memory/<package>.json);
# lets a run resume where a previous one left off and avoids re-flagging/re-reviewing screens
# already seen in an earlier run. Independent of USE_LLM_EXPLORATION.
USE_MEMORY: bool = os.environ.get("USE_MEMORY", "false").lower() in ("1", "true", "yes")
