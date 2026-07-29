# CLAUDE.md

Guidance for Claude Code working in this repository.

This file is loaded into **every** session before you type anything, so it holds only what
changes what you do: where things live, and the rules that were bought with a wrong bug
report. Everything explanatory is in `README.md` and `docs/`.

---

## What this is

**QA Tester AI** tests Android apps two ways, against a real device over ADB, with a shared
web dashboard on `localhost:8000`.

1. **Autonomous exploration** — `run_agent.py` clicks through an app and maps its state
   space. Screens are deduplicated by a *structural hash* of the UI tree that excludes
   input text, so typing "a" then "ab" is one screen, not two.
2. **The Agent tab** — you describe a test suite in English and a Claude Code CLI session
   (one per module, in-process via `claude-agent-sdk`) plans the cases, drives the phone
   and files findings with screenshot evidence.

A **project** is an app package. A **module** is a test suite for one part of it, with its
own transcript, memory, findings and session under `<project>/agent/<module>/`.

---

## Running it

```bash
python start.py                                    # server + device check + browser
python run_agent.py --package <pkg> --steps 30     # autonomous exploration
python -m pytest                                   # 163 tests, no device, a few seconds
```

`Start QA Tester AI.bat` in the repo root does the same as `start.py`.

---

## Where the code is

| Area | Files |
|---|---|
| Exploration loop | `run_agent.py`, `extractor.py` (state hashing), `graph.py` |
| Device control | `adb_device.py` (uiautomator2 wrapper), `journey.py` (scripted flows) |
| Server entry | `server.py` — a façade; the implementation is in `backend/` |
| HTTP endpoints | `backend/routes/{pages,telemetry,projects,device,agent}.py` |
| Server internals | `backend/{app,state,naming,projects,devices,schemas,paths}.py` |
| Chat agent | `agent/runtime.py` (one session per module), `agent/prompts.py`, `agent/store.py` |
| Agent's tools | `agent/device_tools.py` (MCP tools), `agent/screen.py` (dump reading), `agent/guards.py` (finding rules) |
| Frontend | `frontend/dashboard.html`, `frontend/static/js/*.js` (23 ES modules), `frontend/static/css/*.css` |
| Learned operating knowledge | `system_memory.py` → `SYSTEM_MEMORY.md` (generated) |

Frontend entry is `frontend/static/js/main.js`; `state.js` holds shared data, `render.js`
the canvas, `board.js` projects and saving, `chat.js`/`modules.js`/`transcript.js` the
Agent tab. Grep for a function name — the modules are small and named after what they own.

---

## Rules that are not negotiable

Each of these cost a false defect or destroyed data to learn.

**Never file a finding from a dump alone — screenshot it first.** `record_finding` refuses
without one. Every false defect this harness has produced was a dump misread.

**Never judge a submit while a request is in flight.** `dump_hierarchy()` returns *only the
topmost window*, so while a dialog or spinner is up the form underneath is simply absent —
a missing marker means *covered*, not *navigated*. An "Authentication Error" modal made a
correct rejection read as "credentials were accepted"; a "Creating your account…" spinner
made a correct duplicate-email refusal read as "a second account was created". Poll until
the loading text is gone, and judge a dialog by its **wording**.

**Never select an element by label alone.** One `content-desc` often serves both the app bar
and the primary button — `desc='Login'` matched the back header *and* submit, so a
first-match tap navigated back and looked like input rejection.

**Rank packages by node count to know which app is on screen.** `app_current()` is stale
right after launch and the dump's first `package` is the status bar. Exclude the IME:
Gboard adds ~180 nodes whenever a field has focus. A clickable-control count of **0** means
a floating overlay (Messenger chat heads, PiP) is eating taps.

**A board belongs to the project it was loaded from.** The server refuses a save aimed at
another. Four saved boards were destroyed before that check existed — see the header of
`tests/test_project_ownership.py`.

**Never set `ANTHROPIC_API_KEY`.** It overrides the Claude Code subscription profile and
silently bills every planner call per token. `start.py` warns if it finds one.

**Run scripts with `PYTHONIOENCODING=utf-8` on Windows.** The console is cp1252 and app
labels are not.

---

## Things that will waste your time

**A stale process on port 8000.** `server.py` runs with `reload=False`, so edits need a real
restart — and the old process keeps serving while the new one fails to bind. Check
`netstat -ano | grep :8000` before debugging code.

**Cached frontend modules.** After editing anything under `frontend/static/`, hard-reload
(ctrl+shift+R). Fetching the asset to confirm it changed proves nothing — that reads the
network copy, not the loaded one.

**Do not add `?v=` to the module `<script>` tag.** A module's identity is its URL, and
`board.js` imports `./main.js` unversioned — so a versioned entry loads a *second* copy of
main.js whose body runs mid-way through `board.js`, and the whole boot dies on a
temporal-dead-zone error.

**Never read `projects/**/flow-graph.json`.** They are megabytes of base64 JPEG — the
deskclock board is 6.5 MB, about 1.6M tokens. `.claude/settings.json` denies it. Use
`python tools/inspect_board.py <package>` instead.

**The `test_*.py` files in `scratch/`** are gitignored one-off drivers pointed
at one app on one day. The real suite is `tests/`, which `pytest.ini` restricts collection
to.

---

## System memory

`SYSTEM_MEMORY.md` is generated by `system_memory.py` at the end of every run
and holds learned wait times and environment facts, keyed by UI toolkit rather than by app.
**Read it before a device run**, not before every session.

```bash
python system_memory.py --show
python system_memory.py --learn <id> "<lesson>" --evidence "<how we know>"
```

After a run, record what was learned about *operating the harness* — not what an app under
test did, which belongs in a report. If a run produced a false defect, the fix is a lesson
here and an assertion in `tests/`, not just a corrected report.

---

## Git

One repo, rooted here, remote `MRsiyam16/android-agent`. It used to be a workspace with the
project nested under `android-agent/`, which is why the GitHub page opened on a folder rather
than the code; the tree was flattened so the repo root and the project root are the same
directory. Commit and push after a completed feature, not per file edit. Tag milestones
`v0.1`, `v0.2`, … — run `git tag -l` first and continue from the highest; `git push` does
not push tags. This is standing permission: no need to re-confirm each time. Never
`--no-verify`, never force-push to main without asking.

`prototype/` is the standalone ShopFlow canvas mockup the dashboard's visual language came
from. It is a static page with no build and nothing imports it — it lives here as a reference,
not as part of the app.

---

## Further reading

- `README.md` — the full guide
- `docs/ARCHITECTURE.md` — state hashing and exploration algorithms
- `docs/SETUP.md`, `docs/EXAMPLES.md`
