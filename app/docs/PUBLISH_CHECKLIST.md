# Release checklist

What to check before tagging a release, and why each item is here.

This file used to be a frozen inventory: a file-by-file manifest with `Ready ✓` beside every
entry, a line count, and a `READY FOR PUBLICATION` banner. All of it went stale — it still
listed `INDEX.md`, `QUICK_REFERENCE.md`, `templates/dashboard.html` and
`static/dashboard.js`, none of which exist any more, claimed "~940 lines" of a codebase many
times that size, and recorded "Test Coverage: Manual testing on 2 apps" next to a suite of
353 assertions. A checklist that describes the repo is a second copy of the repo, and the
copy is always the one that rots. So this one only lists **actions**, and points at the files
that are actually maintained for anything structural.

For what the project *is* and where things live, read `README.md` (repo root) and
`CLAUDE.md`. For the internals, `docs/ARCHITECTURE.md`.

---

## Before tagging

Run from `app/`, which is where `pytest.ini` lives — from the repo root pytest collects
nothing and exits clean, which looks exactly like a passing suite.

```bash
cd app
python -m pytest                  # no device needed, a few seconds
```

- [ ] **The suite is green.** Every test in `tests/` pins an incident that cost a wrong bug
      report to learn; a failure there is a regression in the harness's judgement, not a
      broken assertion to update. `tests/conftest.py` holds the UI dumps that caused them.
- [ ] **`git status` is clean.** In particular `app/system_memory.json` and
      `app/SYSTEM_MEMORY.md`, which are tracked on purpose so a clone starts with what this
      machine has learned. The suite redirects both at a tmp directory
      (`isolated_system_memory` in `tests/conftest.py`), so a test run must leave them
      untouched — if it does not, something is writing to the real store.
- [ ] **`git ls-files tests/` lists every test file.** `app/.gitignore` ignores `test_*.py`
      as a backstop against ad-hoc drivers and negates it for `tests/test_*.py`. The
      negation works, but a new test file still has to be added, and an untracked one exists
      only on this machine while the suite looks complete here.
- [ ] **No secrets committed.** `projects/` is gitignored in full, which covers
      `secrets.json`, evidence screenshots and the boards. `.env` (the optional
      `OPENROUTER_API_KEY`) is ignored separately. Check `git ls-files | grep -i -E "secret|\.env"`
      comes back empty.
- [ ] **No project board in the diff.** A `flow-graph.json` is multi-MB of base64 JPEG. If
      one is staged, the ignore rules have been bypassed.

## Check by hand, on a device

The suite is deliberately device-free, so these are the parts nothing can assert:

- [ ] `python start.py` binds, reports the device and agent state, and opens the dashboard.
      It refuses to double-bind port 8000 rather than letting a stale process keep serving
      old code.
- [ ] A short exploration run finishes and draws on the board:
      `python run_agent.py --package <pkg> --steps 10`.
- [ ] The Agent tab answers one instruction end to end, and **Stop** cancels the step in
      flight rather than landing a minute later.
- [ ] Reload the dashboard with a project open: the board comes back with its notes, comment
      pins and saved screen positions. A reload rebuilding the graph from telemetry and then
      autosaving a note-less blob over the saved project is how boards have been destroyed
      before.

## Things that are easy to ship broken

- [ ] **`ANTHROPIC_API_KEY` unset.** It overrides the Claude Code subscription profile and
      silently bills every planner call per token. `start.py` warns; `runtime.connect` warns
      again.
- [ ] **`SERVER_HOST` still loopback.** `/command` is unauthenticated remote control of the
      phone — taps, launches, screenshots. Bound to `0.0.0.0` it hands anyone on the network
      a remote for the device and a view of its screen.
- [ ] **Frontend edits hard-reloaded before you believe them.** Modules are served without a
      cache-busting query on purpose (a versioned entry point loads a second copy of
      `main.js` and the boot dies on a temporal-dead-zone error), so ctrl+shift+R is the only
      way to be sure you are looking at the new code.
- [ ] **The docs still describe reality.** `README.md`, `CLAUDE.md` and `docs/` are the four
      places that make claims about structure. This file used to be a fifth, which is why it
      no longer does.

## Cutting the release

Tags are `v0.1`, `v0.2`, … — list what exists first and continue from the highest, because
`git tag -l` sorts lexically and a gap is easy to create by guessing.

```bash
git tag -l                        # continue from the highest
git tag vX.Y
git push && git push --tags       # `git push` alone does not push tags
```

Never `--no-verify`, and never force-push to main without asking.
