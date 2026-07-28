# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project Overview

**QA Tester AI** is an autonomous Android app testing platform with two main components:

1. **Web UI** (root directory): A Framer-like flow canvas (`index.html`, `app.js`, `styles.css`) for visualizing app testing flows—screens marked as working/broken/untested, interactive flow arrows, live recording timer
2. **Android Agent** (`android-agent/`): Python-based autonomous exploration system that maps out an app's complete state space by controlling a real/emulated Android device via ADB

**Core purpose**: Automatically discover all unique UI screens and transitions in an Android app, display them as an interactive graph, and enable QA teams to verify app behavior at scale.

---

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Frontend UI | HTML5, CSS3 (dark theme), vanilla JavaScript |
| Graph visualization | vis.js (physics-based node-link layout) |
| Backend API | FastAPI + uvicorn |
| Mobile control | uiautomator2 (ADB wrapper) |
| State communication | HTTP + WebSocket |
| Languages | Python 3.8+, JavaScript |

---

## Architecture

### Data Flow Pipeline

```
Android App
    ↓ (USB/ADB)
adb_device.py (screenshot, UI dump)
    ↓
extractor.py (state hash, clickable elements)
    ↓
graph.py (state graph management)
    ↓
telemetry.py (HTTP POST)
    ↓
server.py (FastAPI, WebSocket)
    ↓
dashboard.html (browser, vis.js graph)
```

### Key Modules

**`android-agent/run_agent.py`** (Main exploration loop)
- Connects to device via ADB
- Captures screenshots and XML UI dumps
- Computes state hash (SHA-256 of UI structure)
- Extracts clickable elements (actions)
- Implements greedy exploration: try unexplored actions first, backtrack on dead ends (max 6 consecutive backtracks, then restart app)
- Posts results to telemetry server

**`android-agent/extractor.py`** (State & action extraction)
- `compute_state_hash()`: Hashes UI structure—**key insight**: excludes input text but includes static labels, so "search 'a'" and "search 'ab'" are same state if UI layout identical
- `extract_actions()`: Filters clickable/focusable elements, deduplicates by coordinate, generates labels (text → content-desc → resource-id → class name)
- Filters by bounds, enabled state, package whitelist

**`android-agent/adb_device.py`** (Device control wrapper)
- Thin wrapper around uiautomator2 library
- Methods: `screenshot()`, `get_ui_dump()`, `click(x, y)`, `type(text)`, `press_back()`, etc.
- Handles preflight: screen on/off, device lock detection, app launch

**`android-agent/graph.py`** (State graph data structures)
- Stores nodes (unique states) and edges (transitions)
- Tracks state → screenshot, XML, timestamp, clickable elements
- Queries: `add_state()`, `add_edge()`, `get_unexplored_actions()`

**`android-agent/server.py`** (FastAPI telemetry server)
- `POST /telemetry`: Receive state logs from agent
- `POST /status`: Receive status messages
- `POST /command`: Send remote tap/type/back commands (future)
- `WebSocket /ws`: Broadcast real-time state updates to dashboard
- Stores data in memory (no persistent DB by default)

**`android-agent/config.py`** (Configuration hub)
- `MAX_STEPS`: Exploration limit (default 200)
- `ACTION_SETTLE_SECONDS`: Wait time after each click (default 0.9s; increase for slow apps)
- `SCREENSHOT_QUALITY`: JPEG quality 0–100 (default 90)
- `EXCLUDE_TOP_PCT`, `EXCLUDE_BOTTOM_PCT`: Ignore status/nav bars
- `BLOCKED_PACKAGES`: System UI to skip (keyboards, launchers)
- `ALLOWED_PACKAGES`: Whitelist (if app navigates outside main package)

**`android-agent/journey.py`** (Scripted-test flow mapping)
- For *scripted* tests, not exploration. Records an ordered chain of steps: each step gets
  its own node, chained parent → child in execution order, labelled with what the screen
  actually showed
- **Why it exists**: `compute_state_hash()` strips text from interactive elements, so in a
  calculator every keystroke hashes identically — a 21-tap run collapsed into one node with
  42 self-loops. Structural dedup answers "which screens exist?"; a scripted test asks "what
  sequence did we walk?"
- Sends `step_label` (used verbatim as the node name) and `section` (dashboard grouping);
  carries the structural hash along as `state_hash_struct` for correlation
- Exploration is unaffected — the server keys all of this off `step_label`, which
  `run_agent.py` never sends

**`android-agent/telemetry.py`** (HTTP client)
- Posts discovered states to server
- Includes step #, state hash, package, activity, action label, screenshot (base64 JPEG), timestamp

---

## Core Concepts

### State Identification (Hash)
A state is uniquely identified by a SHA-256 hash computed from:
- Package name, activity name
- For each UI element: `class:resource_id:clickable:text_if_static:content_desc_if_static`
- **Critical**: Interactive element text is excluded (prevents input variations from fragmenting state space)
- **Dynamic patterns** (times, percentages, dates) are also excluded

**Example**: 
- "Search 'a'" and "Search 'ab'" → same state hash (text excluded on interactive SearchBox)
- "Login" button and "Logout" button on same screen → different states (static label conveys meaning)

### Exploration Strategy
1. **Take screenshot** → extract XML, compute state hash
2. **New state?** → Add to graph, extract clickable elements
3. **Pick next action**: Prioritize unexplored elements (depth-first), then known elements
4. **Click** → observe result
5. **No actions left?** → Press BACK (up to 6 backtracks), then restart app, reset counter
6. **Stop when**: Step limit reached OR exhausted all paths

### Telemetry Flow
1. Agent discovers state → posts JSON to server
2. Server stores in memory, broadcasts via WebSocket
3. Dashboard receives updates → adds node, edge, screenshot to graph
4. Browser runs vis.js physics simulation → positions nodes automatically

---

## Common Development Tasks

### Setup
```bash
cd android-agent
pip install -r requirements.txt
```

### Run Exploration
```bash
# Terminal 1: Start server
python server.py

# Terminal 2: Run agent
python run_agent.py --package com.example.app --steps 50

# Browser: http://localhost:8000
```

### Options
- `--package <pkg>`: Target app (required)
- `--steps N`: Max steps (default 200; use 50 for quick tests)
- `--serial <id>`: Device serial (auto-detected if one device)
- `--server <url>`: Telemetry server URL (default http://localhost:8000)

### Configuration Tweaks (in `config.py`)
| Issue | Fix |
|-------|-----|
| App is slow | `ACTION_SETTLE_SECONDS = 2.0` |
| Need faster screenshots | `SCREENSHOT_QUALITY = 70` |
| Exploration repeating same state | Increase settle time or app is fully explored |
| Device not found | `adb devices` — check USB cable, enable USB debugging |
| Port 8000 in use | Change `SERVER_PORT` in config.py |

### Verify Setup
```bash
adb devices                                    # Check device connected
python -c "import uiautomator2; print('✓')"  # Check dependencies
python server.py                               # Check server starts
```

---

## Performance Expectations

| Metric | Typical |
|--------|---------|
| Time per step | 7–10s (screenshot + hash + extraction) |
| States discovered (50 steps) | 20–50 (depends on app) |
| Memory overhead | 50–200 MB |
| Screenshot size | 50–100 KB (JPEG @ 90%) |

**For faster runs**: Lower `SCREENSHOT_QUALITY` to 70, reduce `ACTION_SETTLE_SECONDS` to 0.5 (risky if app slow), use `--steps 20`.

---

## File Structure

```
D:\QA Tester AI/
├── index.html                    # Flow canvas UI (Framer-like)
├── app.js                        # Canvas interaction logic
├── styles.css                    # Dark theme styling
├── android-agent/
│   ├── run_agent.py             # Main exploration loop (entry point)
│   ├── server.py                # FastAPI telemetry server
│   ├── config.py                # Configuration (edit here for tweaks)
│   ├── adb_device.py            # Device control wrapper
│   ├── extractor.py             # State hashing & action extraction
│   ├── graph.py                 # State graph structures
│   ├── telemetry.py             # HTTP client
│   ├── templates/
│   │   └── dashboard.html       # Web UI (FastAPI serves)
│   ├── static/
│   │   ├── dashboard.js         # Graph interaction, vis.js
│   │   └── dashboard.css        # Styling
│   ├── requirements.txt         # Python dependencies
│   ├── README.md                # Full guide (read this first)
│   ├── ARCHITECTURE.md          # Deep-dive on algorithms
│   ├── QUICK_REFERENCE.md       # Cheat sheet
│   └── SETUP.md                 # Installation guide
└── .claude/                     # Claude Code config
```

---

## Extension Points

### Custom State Hashing
Edit `extractor.py::compute_state_hash()` to:
- Ignore certain element types
- Weight elements by importance
- Use ML for semantic equivalence

### Custom Exploration Strategy
Modify `run_agent.py::pick_action()` to:
- Prioritize certain elements (menu first, settings last)
- Avoid known-bad paths
- Test specific user journeys first

### Custom Telemetry
Subclass `telemetry.py::TelemetryClient` to:
- Send to different server
- Add custom metadata (performance, crashes)
- Integrate with CI/CD

---

## Dashboard Controls

| Control | Action |
|---------|--------|
| Click & drag | Pan graph |
| Scroll | Zoom in/out |
| Click node | Show state details |
| Pan/Select/Text/Comment tools | Left toolbar (fixed position) |
| Settings (gear) | Tap markers, headings, grid, auto-fit, reset |
| Save | Download flow-graph.json |
| Import | Load previously saved JSON |
| Live preview | View current device screen (right dock) |

---

## The chat agent (Agent tab)

The dashboard's second tab is **Agent**: the phone's live screen on the left, a chat box in
the middle, and the module list on the right. You give it an instruction in plain English
("test the login module: empty submit, wrong password, valid login, session persistence") and
it plans the cases, drives the phone, and reports what it observed — streaming each step into
the chat and drawing the path onto the Flow Graph as it goes.

**Double-click `Start QA Tester AI.bat`** in the project root, or from a terminal:

```bash
python start.py          # server + device check + agent readiness, then open localhost:8000
```

Either way the server also **pre-warms a Claude Code session** for the module you used last
(`projects/last-opened.json`), so the first thing you type does not wait for the CLI to spawn.
Selecting a module in the UI warms that one too, via `POST /agent/{pkg}/{slug}/warm`.

### One model, by default

Everything runs on the local **Claude Code CLI**, driven in-process by `claude-agent-sdk`: it
plans, drives the phone, reads its own screenshots, and writes the verdicts. It authenticates
with the machine's Pro/Max subscription, so the cost is a **rate-limit window** rather than
per-token billing — and the Agent tab shows the model and subscription it is actually using,
read from the CLI (`get_server_info()`) rather than assumed.

An optional cheap OpenRouter tier (`AGENT_USE_CHEAP_TIER=true`) can take over the
high-volume mechanical calls — which element to tap, has the screen settled — to spend less of
the window. **Off by default:** one model means one set of judgement with nothing to reconcile
and nothing billed per token. Turn it on only if long runs start hitting the rate limit.

> **Never set `ANTHROPIC_API_KEY` in the server's environment.** It takes precedence over the
> Claude Code subscription profile, silently moving every planner call onto metered API
> billing. `start.py` warns if it finds one set.

Requires the CLI installed and signed in once: `npm i -g @anthropic-ai/claude-code`, then
`claude`. `OPENROUTER_API_KEY` goes in `android-agent/.env` (gitignored).

### Modules (sub-projects)

A project is an app package; a **module** is a test suite for one part of it (Auth, Checkout,
Feed). Press **Recon** on a new project and the agent explores the app, then proposes a
breakdown for you to approve, rename or merge — approval is required before it tests anything.
Modules can also be added by hand. Each module owns its own chat transcript, memory file,
findings and evidence:

```
projects/<package>/
├── secrets.json                 test credentials — gitignored, write-only over the API
└── agent/
    ├── subprojects.json         the module list + per-module status
    └── <module>/
        ├── chat.jsonl           append-only transcript (append, never rewrite — a
        │                        whole-file rewrite is how the dashboard lost notes)
        ├── memory.md            what the agent learned about this module
        ├── findings.json        confirmed defects
        └── shots/               screenshots captured as evidence
```

### Module map

| File | Role |
|---|---|
| `agent/runtime.py` | one live `ClaudeSDKClient` per module, kept alive between messages; translates the SDK stream into WebSocket events |
| `agent/device_tools.py` | 20 in-process MCP tools wrapping `AdbDevice`, with this harness's failure modes built in as guardrails |
| `agent/stepper.py` | the OpenRouter cheap tier + the two tools that expose it |
| `agent/prompts.py` | the system prompt; folds in the live `system_memory.briefing()` |
| `agent/store.py` | modules, transcripts, memory, findings, credentials on disk |

### Design decisions worth not re-litigating

- **Sessions are kept alive between messages.** Re-spawning the CLI per message costs ~1-2s
  and, worse, discards the conversation — the agent would forget which case it was on.
- **The tool allow-list is a `PreToolUse` hook, not `can_use_tool`.** Under
  `bypassPermissions` the SDK auto-approves *before* `can_use_tool` is consulted, so a gate
  written there is silently dead code (the SDK warns about exactly this). `bypassPermissions`
  is still required: there is no human at the CLI to answer a prompt, so anything awaiting
  approval would hang. Bash, web access and subagents are withheld — this agent tests a
  phone, and a shell inside the server process is needless blast radius.
- **`setting_sources=None`.** The agent deliberately does *not* load this repo's `CLAUDE.md`
  or settings: those describe how to develop the harness, which would only distract an agent
  whose job is to test a phone.
- **An exhausted subscription window parks the run** with its session id preserved, rather
  than finishing the case on a different model and presenting the results as equivalent. Say
  "continue" once the window resets.
- **`record_finding` refuses a defect without a screenshot.** Every false defect this harness
  has produced was a dump misread that a screenshot would have caught.
- **Device tools all run in worker threads.** `uiautomator2` is synchronous; a single
  `dump_hierarchy()` on the event loop would freeze the WebSocket — and therefore the UI —
  exactly when the agent is busiest.

---

## System Memory (self-learning) — read this first

`android-agent/SYSTEM_MEMORY.md` is a **generated briefing on how to drive this harness**,
rebuilt automatically at the end of every run by `android-agent/system_memory.py`.

**Read it before starting work.** It carries learned wait times, environment facts (which
`pm clear` form this ROM needs, how adb resolves, console encoding) and operating lessons
with evidence and a confidence counter. It exists so run N+1 starts smarter than run N.

Two hard rules:

1. **Operational knowledge only.** Nothing about what an app under test did, whether it
   passed, or what defects were found — those belong in a report. System memory answers
   "how do I run this thing correctly", nothing else.
2. **Timings are keyed by UI toolkit, not by package.** "Flutter's first frame takes ~12s
   on this machine" transfers to the next Flutter app; "com.example takes 12s" is a fact
   about one test target and does not belong here.

### How it learns

- `AdbDevice.wait_for_ui()` waits for *text*, not node count, and records how long the
  first usable dump actually took — keyed by `detect_toolkit()`. `run_agent.py` uses that
  learned value instead of a fixed sleep.
- `AdbDevice.clear_app_data()` tries both `pm clear` forms, verifies the result, and
  remembers which one this ROM honours.
- `run_agent.main()` wraps the run in `system_memory.run_session(...)`, which counts and
  times it and regenerates the digest on exit.
- Anything notable calls `system_memory.learn(id, text, evidence=...)`. Re-learning the
  same id raises its confidence; a lesson not seen for 60 runs ages out of the digest.

### Adding to it by hand

```bash
python system_memory.py --show                       # print the current briefing
python system_memory.py --learn <id> "<lesson>" --evidence "<how we know>"
python system_memory.py --forget <id>                # retract something that proved wrong
```

`system_memory.json` and `SYSTEM_MEMORY.md` are **tracked in git** deliberately — the
learning is meant to survive a clone, unlike the per-app `projects/` memory. Do not
hand-edit the `.md`; it is overwritten on every run.

**After each run, add what was newly learned about operating the system**, so the next run
is faster and makes fewer false calls. If a run produced a false defect, the fix is a
lesson here, not just a corrected report.

---

## Debugging & Troubleshooting

**"Device not found"**
```bash
adb devices
# Check: USB cable, Developer Mode enabled, USB Debugging on, RSA key approved
```

**"Exploration stuck (repeating same state)"**
- Increase `ACTION_SETTLE_SECONDS` in `config.py` (app may be slow to render)
- Or app is fully explored (check dashboard graph)

**"Screenshots blurry"**
```python
# config.py
SCREENSHOT_QUALITY = 100
```

**"Agent crashes on certain click"**
- Check device logs: `adb logcat | grep com.app.package`
- Add element to skip list in `extractor.py` if needed

**"Port 8000 in use"**
```python
# config.py
SERVER_PORT = 8001
```

**"I edited the server/dashboard but nothing changed"**
`server.py` runs with `reload=False`, so edits need a real restart — and a stale process
still holding port 8000 makes the new one fail to bind while the *old* code keeps serving.
Check who owns the port and when it started before debugging the code:
```bash
netstat -ano | grep :8000
```
For dashboard JS/CSS, hard-reload the browser (ctrl+shift+R). Fetching the asset to confirm
it contains your change proves nothing — that reads the network copy, not the loaded script.
A server-side count disagreeing with the UI count is the tell.

**"The dump says the app is broken, but the app is fine"**
Another app's *floating overlay* — Messenger chat heads, bubbles, picture-in-picture —
appears as nodes in the UI dump and intercepts taps. Once one tap lands on the bubble the
run continues against the wrong screen and every downstream assertion produces a confident
false defect (a YouTube pass reported missing player controls, comments and fullscreen;
all three were the home feed being dumped instead of a watch page).

Tells: a clickable-control inventory of **0**, or an identical node count across dumps that
should be different screens. Guard by ranking packages by node count and refusing any dump
whose top package isn't the app under test — but exclude the IME from that ranking, since
Gboard contributes ~180 nodes whenever a text field has focus and would otherwise trip the
guard on every search flow. Force-stop bubble-capable apps before a run.

**"My script dies printing app text on Windows"**
`UnicodeEncodeError: 'charmap' codec can't encode…` — the console is cp1252 and app labels
are not. Run with `PYTHONIOENCODING=utf-8`; it is not optional for any app with non-Latin
content.

**"Did the app actually launch?"**
Don't trust `app_current()` (returns a stale package right after launch) or the first
`package` attribute in the UI dump (that's the `com.android.systemui` status bar). Count
nodes per package across the dump and take the most common. When a device check reports an
unexpected state, screenshot the device before believing it.

---

## Best Practices

1. **Start with tight step limits**: `--steps 30` to map core flows, expand later
2. **Monitor dashboard in parallel**: Watch graph grow in real-time, stop early if complete
3. **Target single packages**: Use `BLOCKED_PACKAGES` to skip system apps (camera, gallery)
4. **Increase settle time for slow apps**: `ACTION_SETTLE_SECONDS = 1.5–2.0`
5. **Save after each run**: Click "Save" on dashboard to download JSON
6. **Annotate findings**: Use dashboard text & comment tools to mark bugs/edge cases
7. **Re-run with tweaks**: Adjust config after first run (settle time, quality, step limit)

---

## Git Workflow (android-agent/)

`android-agent/` is its own git repo with a GitHub remote (`MRsiyam16/android-agent`);
the project root (this file, the flow-canvas UI) is **not** currently a git repo.

- **Commit + push after each completed task/feature**, not after every individual
  file edit. One logical unit of work (e.g. "add per-app persistent memory") = one
  commit, pushed once it's verified working.
- **Tag a new version at each feature milestone**, not on every commit. Use
  lightweight tags: `v0.1`, `v0.2`, `v0.3`, … (bump the minor number per
  milestone). Run `git tag -l` first and continue from the highest existing tag —
  the sequence is well past the examples above. Push tags explicitly
  (`git push origin <tag>` or `git push --tags`) — a normal `git push` does not
  push tags, so check for unpushed ones.
- **`.gitignore` excludes `test_*.py` and `*_test.py`.** Scripted test files are
  never committed, so anything learned while writing one (device quirks, assertion
  tricks) must be recorded in code that *is* tracked, or in these docs — otherwise
  it's lost.
- This is a standing instruction — apply it automatically without re-confirming
  each time, following the repo's existing "Committing changes with git" safety
  rules (new commits, not amends; no `--no-verify`; never `git push --force` to
  main without asking).

---

## Key Design Decisions

1. **State hash excludes input text**: Prevents input variations (search "a" vs "ab") from exploding state space while preserving static label differences (Login vs Logout)
2. **Greedy exploration with backtracking**: Tries unexplored paths first, backtracks on dead ends—balances depth-first coverage with breadth-first completeness
3. **In-memory telemetry**: No persistent DB by default—data lives in server memory. Save from dashboard when done.
4. **WebSocket for live updates**: Dashboard updates in real-time as agent explores without polling
5. **Structural deduplication**: Only UI layout changes count as new state; input text, times, counters ignored

---

## References

- **Full Guide**: `android-agent/README.md` (read first)
- **Deep Architecture**: `android-agent/ARCHITECTURE.md` (state hashing, exploration algorithms)
- **Quick Reference**: `android-agent/QUICK_REFERENCE.md` (cheat sheet)
- **Setup Guide**: `android-agent/SETUP.md` (installation)

---

## When to Edit Which Files

| File | Edit When |
|------|-----------|
| `config.py` | Adjusting performance (settle time, screenshot quality, step limit) |
| `extractor.py` | Changing what counts as "same state" or customizing action labels |
| `run_agent.py` | Changing exploration strategy (e.g., action prioritization) |
| `adb_device.py` | Adding device control methods (e.g., long press, scroll) |
| `server.py` | Adding API endpoints or changing telemetry format |
| `graph.py` | Extending state/edge metadata or adding graph algorithms |
| `app.js` | Changing canvas UI interaction (Framer-like tools) |
| `dashboard.js` | Changing graph visualization (vis.js options, layouts) |

---

## Summary

This is a **mobile QA automation framework** designed to autonomously explore Android apps and visualize their state space. The agent discovers all reachable UI screens, maps transitions, and reports findings in real-time to a web dashboard. Core innovation: **state deduplication via structural hashing**—input variations don't fragment the state space, only actual UI changes count.

For questions or deep dives: check the `android-agent/` docs—they're comprehensive and well-organized.
