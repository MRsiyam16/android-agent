# Android App Testing Agent – Quick Start Guide for AI

## Overview
This is an autonomous Android UI exploration system that maps out an app's state space and user flows. An AI agent controls a real Android device via `uiautomator2`, executes intelligent exploration strategies, and publishes results to a real-time dashboard.

**Key outputs**: Flow graph visualization, state screenshots, transition map, and CSV export of discovered flows.

---

## What This Does (For AI Agents)

### Core Capability
Given an Android app package name, the agent:
1. Connects to a connected USB device (or emulator)
2. Wakes the screen, detects lock, waits for unlock
3. Launches the target app
4. Autonomously clicks through UI elements, discovering unique screens
5. Maps transitions between screens with interaction labels
6. Visualizes the complete flow graph on a local dashboard

### Why This Matters
- **Fast coverage**: Discovers 40+ unique states in minutes vs. hours of manual testing
- **Live feedback**: Real-time dashboard shows exploration progress
- **Structural deduplication**: Input text variations don't fragment the state space (only actual UI changes count)
- **Export-ready**: Save flows as JSON, import later, or share findings

---

## Setup (One-Time)

### Prerequisites
- **Python 3.8+**
- **USB device or emulator** with Developer Mode enabled and USB debugging allowed
- **ADB** (Android Debug Bridge) installed and in PATH
- **Dependencies**: Install with `pip install -r requirements.txt`

### Quick Install
```bash
cd android-agent
pip install -r requirements.txt
```

**On Windows**: ADB is auto-discovered from standard Android SDK paths. Set `ADB_PATH` environment variable if it's elsewhere.

**On Mac/Linux**: Install ADB via `brew install android-platform-tools` or download from Android SDK.

---

## Running for a Target App

### Command Line
```bash
python run_agent.py --package com.app.package.name --steps 50
```

**Options**:
- `--package <pkg>`: Target app (required)
- `--steps N`: Max exploration steps (default 200, use 50 for quick tests)
- `--serial <id>`: Device serial (auto-detected if one device connected)
- `--server <url>`: Telemetry server URL (default http://localhost:8000)

### Full Example
```bash
python run_agent.py --package com.samsung.android.app.notes --steps 50
```

This will:
1. Connect to device
2. Check screen, unlock if needed
3. Launch the Notes app
4. Run 50 exploration steps
5. Output: `50 steps, 43 unique states, 50 edges`

---

## Dashboard (Live Visualization)

### Start the Server
In a separate terminal:
```bash
python server.py
```
The server runs on `http://localhost:8000`.

### Open in Browser
Navigate to `http://localhost:8000` to see:
- **Flow Graph**: Interactive node-link diagram of states and transitions
- **State Screenshots**: Full app UI snapshots
- **Section Organization**: Auto-grouped by UI activity
- **Live Updates**: Refreshes as agent explores

### Controls
- **Pan/Zoom**: Click and drag graph, scroll to zoom
- **Toolbar**: Tool buttons (pan, select, text annotation, comments)
- **Live Preview**: Toggle to see current device screen
- **Settings** (gear icon):
  - Toggle tap markers (show interaction points)
  - Toggle section headings
  - Toggle background grid
  - Auto-fit on new state (camera follows exploration)
  - Reset layout

### Save & Import
- **Save**: Downloads the entire flow graph as JSON
- **Import**: Reload a previously saved JSON to pick up where you left off

---

## The Agent Tab — testing by conversation

Everything above describes *autonomous exploration*: point `run_agent.py` at a package and it
maps the state space. The **Agent** tab does something different — you tell an agent in plain
English what to test, and it plans the cases, drives the phone, and reports what it observed.

```bash
python start.py      # one command: server, device check, agent readiness, browser
```

Then open the **Agent** tab: the phone's live screen is on the left, the chat in the middle,
the module list on the right.

```
you › test the login module: empty submit, wrong password, valid login, session persistence

agent › Plan: 4 cases. Starting with empty submit.
  ▸ launch com.example.app                        ✓
  ▸ tap_element id=540_612  'Login'               ✓
  ▸ submit empty → expect inline validation
      ✓ "Email is required" is on screen
  ▸ wrong password → expect rejection
      ⚠ "Signing in…" still up, waiting
      ✓ "Invalid credentials", still on the form
  ▸ needs a valid test account
  ⏸ blocked — which credentials should I use?
```

### Setup

1. **Claude Code CLI**, installed and signed in once. This is the planner, and signing in with
   a Pro/Max subscription is what keeps it off metered API billing:
   ```bash
   npm i -g @anthropic-ai/claude-code
   claude          # once, to authenticate
   ```
2. **`OPENROUTER_API_KEY`** in `android-agent/.env` (gitignored) for the cheap tier. Optional —
   without it the agent still works, but every routine screen check spends subscription quota
   instead of a fraction of a cent.
3. `pip install -r requirements.txt`

**Do not set `ANTHROPIC_API_KEY`.** It overrides the subscription profile and silently bills
planner calls per token. `start.py` warns if it finds one.

### Modules

A project is an app package; a module is a test suite for one part of it. On a new project,
press **Recon**: the agent explores the app and proposes a breakdown (Auth, Catalog, Cart,
Checkout…) for you to approve, rename or merge. It does not test anything until you approve.
Each module keeps its own chat transcript, memory file, findings and screenshots under
`projects/<package>/agent/<module>/`.

Each test case also draws itself onto the **Flow Graph** as an ordered chain of named steps,
grouped by module — so the graph shows the path a test walked, not just which screens exist.

### What it will and will not do

- It runs an instruction to completion rather than asking after each step, and streams every
  action into the chat. **Stop** halts it after the step in flight.
- It pauses only when genuinely blocked — a credential it does not have, an OTP, a paywall, or
  a spec ambiguity where the two readings imply different tests.
- It **cannot file a defect without a screenshot**, and is told to look at the image itself
  before doing so. Every false defect this harness has produced was a dump misread.
- It has no shell, no web access and no subagents; the phone plus its own notes are the whole
  of its world.
- If the subscription window runs out mid-run it **stops and says so**, keeping the session so
  you can say "continue" later — rather than finishing on a different model and presenting the
  results as equivalent.

### Cost, in practice

A ~40-tap module test is roughly 40 cheap-tier calls (a fraction of a cent, shown live in the
UI) plus a handful of planner turns against the subscription window. The constraint is the
window, not money — which is exactly why the mechanical work is delegated.

---

## How the Agent Explores (What's Happening Under the Hood)

### State Identification
Each unique screen is identified by a **structural hash** of the UI tree:
- Package name + Activity
- Element structure (classes, IDs, bounds)
- **Excludes**: Input text (so "search 'a'" and "search 'ab'" = same state)
- **Includes**: Static labels and layout structure

This prevents input variations from exploding the state space.

### Exploration Strategy
1. **Take screenshot** → extract XML dump
2. **Compute state hash** → is this new or seen before?
3. **If new**: Add to graph, extract clickable elements
4. **If seen**: Skip to next action
5. **Pick next action**: Try unexplored elements first, then known elements
6. **Backtrack**: If stuck, press BACK (max 6 backtracks, then restart app)
7. **Stop when**: Hit step limit OR exhausted all clickable paths

### Preflight Checks
Before exploring, the agent:
- Checks if screen is on (wakes if needed)
- Detects if device is locked
- Waits up to 120s for unlock (human-in-loop)
- Launches target app
- Posts status messages to dashboard

---

## Configuration (Fine-Tuning)

Edit `config.py` to adjust:

```python
MAX_STEPS = 200                    # Exploration limit
ACTION_SETTLE_SECONDS = 0.9        # Wait after each click (increase if app is slow)
SCREENSHOT_QUALITY = 90            # JPEG quality (lower = faster)
EXCLUDE_TOP_PCT = 0.05             # Ignore top 5% (status bar)
EXCLUDE_BOTTOM_PCT = 0.08          # Ignore bottom 8% (nav bar)
BLOCKED_PACKAGES = {...}           # System UI to skip (keyboards, launchers)
```

### Target a Specific Package
If the app navigates outside its main package, set `ALLOWED_PACKAGES`:
```bash
ALLOWED_PACKAGES=com.app.main,com.app.lib python run_agent.py --package com.app.main --steps 50
```

---

## Output & Results

### Console Output
```
2026-07-22 15:08:30 run_agent INFO Exploration finished: 50 steps, 43 unique states, 50 edges
```

Key numbers:
- **Steps**: Interactions executed
- **Unique states**: Distinct UI screens found
- **Edges**: State-to-state transitions

### Files Generated
- **No persistent files by default** — all data lives in the telemetry server's in-memory database
- **Save from dashboard**: Click "Save" to download `flow-graph.json`
- **Re-import later**: Click "Import" and select the JSON file

### Dashboard Export (JSON Format)
```json
{
  "nodes": [
    {"id": "c25453e3", "label": "Notes Main", "image": "data:image/jpeg;base64,..."},
    ...
  ],
  "edges": [
    {"from": "c25453e3", "to": "9fdbdcdb", "label": "click 'New item'", "x": 84, "y": 871},
    ...
  ]
}
```

---

## Troubleshooting

### "Device not found"
```bash
adb devices
```
If empty, check USB cable, enable USB debugging, and approve the RSA key on the device.

### "App failed to launch"
- Verify package name: `adb shell pm list packages | grep <keyword>`
- Check app is installed: `adb install <apk-path>`
- Check permissions: App may need to be manually granted permissions on first launch

### "Exploration stuck (repeating same state)"
Increase `ACTION_SETTLE_SECONDS` in `config.py` — the app may be slow to render.

### "Tap markers not showing"
Open dashboard Settings → toggle "Tap markers" on (it's in the Display section).

### "Screenshots are blurry"
Increase `SCREENSHOT_QUALITY` in `config.py` (values 70–100, default 90).

### "The dump says the screen changed, but the app is fine" (Flutter targets)
`dump_hierarchy()` returns **only the topmost window**. While a dialog or a blocking
progress overlay is up, the form underneath is absent from the dump — so a missing screen
marker means *something is covering the screen*, not that navigation happened. Scripting a
Flutter app against that assumption produces confident false defects:

- An "Authentication Error" modal made a correct credential rejection read as
  *"unknown credentials were accepted"*.
- A "Creating your account…" spinner made a correct duplicate-email refusal read as
  *"a second account was created with an address already in use"*. It flipped to PASS purely
  by polling until the overlay cleared.

Rules that follow:

1. **Never judge a submit while a request is in flight.** Poll until the loading text is gone.
2. Treat *dialog present* as still-on-form, and judge the dialog by its **wording** — the same
   widget carries both confirmations and errors.
3. **Wait for text, not for nodes.** A dump 5s after launch held only `com.android.systemui`;
   a cold start after `pm clear` sits on a splash publishing ~15 nodes with no text.
4. **Don't select by label alone.** One `content-desc` often serves both the app bar and the
   primary button (`desc='Login'` matched the back header *and* the submit button), so a
   first-match tap navigates back and looks like input rejection. Constrain body taps below
   the app bar, and never use a button label as a screen-identity marker.
5. **Forms validate reactively as you type**, so a before/after diff around the submit tap
   misses errors that are already on screen. Baseline the pristine form on arrival and diff
   against that, excluding `EditText` values.
6. **A successful registration persists a session**, so later cases open on profile onboarding.
   Reset with `pm clear --user 0 <pkg>` — the bare form fails *silently* on multi-user Samsung
   ROMs. Expect a `com.android.permissioncontroller` permission prompt on the next launch,
   which a package-filtered dump cannot see.

Before reporting any negative finding from a dump, screenshot it.

---

## Best Practices for Fast & Effective Exploration

### 1. **Start with a tight step limit**
```bash
python run_agent.py --package com.example.app --steps 30
```
Map the main flows first. Expand later if needed.

### 2. **Use Preflight to Avoid Delays**
The agent auto-checks lock and screen. Let it handle device setup — don't interrupt.

### 3. **Target Single Packages**
If the app navigates to other apps (Camera, Gallery), use `BLOCKED_PACKAGES` to auto-backtrack:
```python
BLOCKED_PACKAGES.add("com.android.camera")
```

### 4. **Monitor the Dashboard Live**
Open the browser dashboard in parallel with the agent running. Watch the graph grow in real-time. If you see repetition, the agent is likely exhausted — safe to stop.

### 5. **Save & Annotate**
Once exploration finishes, click "Save" on the dashboard. Use the **text tool** and **comment pins** to annotate findings:
- Text labels for key flows
- Comment pins on screenshots to mark bugs or edge cases

### 6. **Re-run with Settings Tweaks**
- App too slow? Increase `ACTION_SETTLE_SECONDS` to 1.5 or 2.0
- App crashes on certain clicks? Add those elements to a skip list
- Too many trivial variations? Adjust state hash logic to ignore certain attributes

### 7. **Export & Share**
Click "Save" → share the JSON file. Other agents can import it and pick up exploration from the last state.

---

## For AI Agents Using This Tool

### Integration Pattern
```python
import subprocess
import time

# 1. Start server
subprocess.Popen(["python", "server.py"], cwd="android-agent")
time.sleep(2)

# 2. Run exploration
result = subprocess.run([
    "python", "run_agent.py",
    "--package", "com.target.app",
    "--steps", "50"
], cwd="android-agent", capture_output=True, text=True)

# 3. Parse output
if "Exploration finished" in result.stdout:
    # Extract counts: "50 steps, 43 unique states, 50 edges"
    pass

# 4. Query dashboard JSON via browser or script
# GET http://localhost:8000/api/states -> list of discovered states
# GET http://localhost:8000/api/edges -> list of transitions
```

### Key Endpoints (Future Expansion)
- `GET /telemetry` → List all state logs
- `POST /command` → Send remote tap/type/back commands
- `GET /screenshot` → Current device screenshot

### When to Use
- **Regression testing**: Compare new builds against baseline flows
- **Coverage analysis**: Identify unvisited states
- **Bug hunting**: Interact with unusual state combinations
- **Compliance**: Document all reachable app states for auditing

---

## Performance Expectations

| Metric | Typical | Notes |
|--------|---------|-------|
| Time per step | 7–10s | Includes ~0.9s settle, screenshot, hash, element extraction |
| States discovered (50 steps) | 20–50 | Depends on app complexity and connectivity |
| Memory overhead | ~50–200 MB | Dashboard stores all screenshots in memory |
| Screenshot size | 50–100 KB | JPEG at 90% quality, 1080p device |

For faster exploration:
- Lower `SCREENSHOT_QUALITY` to 70
- Reduce `ACTION_SETTLE_SECONDS` to 0.5 (risky if app is slow)
- Use `--steps 20` for quick surveys

---

## Advanced Topics

### Custom State Hashing
Edit `extractor.py::compute_state_hash()` to customize what counts as "same state":
- Exclude dynamic attributes (timestamps, counters)
- Weight certain UI elements differently
- Preserve input text in specific fields

### Simulating User Journeys
After exploration, manually curate a sequence of transitions to test a user flow (e.g., "create note" → "edit note" → "save note"). The dashboard's comment tool helps document expected vs. actual behavior.

### Parallel Exploration
Run multiple instances with different apps or step limits. Use separate device serials:
```bash
python run_agent.py --package app1 --serial ABC123 --steps 50 &
python run_agent.py --package app2 --serial XYZ789 --steps 50 &
```

Each needs its own server port (edit `config.py` `SERVER_PORT`).

---

## File Structure
```
android-agent/
├── start.py              # One command to bring the whole system up
├── run_agent.py          # Autonomous exploration loop
├── server.py             # FastAPI telemetry + agent server
├── config.py             # Configuration (ADB, server, limits, agent tiers)
├── extractor.py          # XML parsing, state hashing, action extraction
├── adb_device.py         # uiautomator2 wrapper
├── graph.py              # Graph data structures
├── journey.py            # Scripted-flow mapping (one node per step)
├── telemetry.py          # HTTP client for posting results
├── system_memory.py      # Self-updating briefing on how to drive this harness
├── agent/                # The chat agent behind the Agent tab
│   ├── runtime.py        #   one live Claude Code session per module
│   ├── device_tools.py   #   in-process MCP tools that drive the phone
│   ├── stepper.py        #   cheap OpenRouter tier for high-volume calls
│   ├── prompts.py        #   the QA system prompt + learned lessons
│   └── store.py          #   modules, transcripts, memory, findings, credentials
├── templates/
│   └── dashboard.html    # Frontend (vis.js graph, agent chat)
├── static/
│   ├── dashboard.js      # Graph + agent UI logic
│   └── dashboard.css     # Styling
├── .env                  # OPENROUTER_API_KEY — gitignored, never committed
├── requirements.txt      # Python dependencies
└── README.md             # This file
```

---

## Support & Issues

**Common questions:**
- *Can I test offline?* No, the server and device connection are required.
- *Does this work with emulators?* Yes, if emulator is running and ADB can detect it.
- *Can I test a web app in a WebView?* Yes, as long as the Android app itself is the target. The agent sees the WebView's structure but not raw HTML.
- *How do I test app crashes?* The agent logs crashes to console and moves on. Review the flow graph to identify crash-prone transitions.

**Next steps:**
- Read `ARCHITECTURE.md` for deep-dive on state hashing and exploration algorithms
- Check `EXAMPLES.md` for sample runs and output interpretation
- Open an issue on GitHub for bugs or feature requests

---

**Happy testing! 🚀**
