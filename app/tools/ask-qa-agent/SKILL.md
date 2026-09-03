---
name: ask-qa-agent
description: |
  Query the QA Tester AI harness (an autonomous Android/iOS/web app-testing agent running on
  this machine) for what it has already learned about an app — filed bugs, warnings, passes,
  and per-module notes — without driving the phone or repeating the testing yourself. Use this
  BEFORE starting new QA/testing work on an app, filing a bug report, or answering "has this
  been tested" / "what bugs does X have" / "is login already covered" / "what does the QA agent
  know about <feature>". Also use to list what projects and test modules already exist. Do not
  use this to run new tests, control a device, or change any finding — it is read-only.
---

# Asking the QA Tester AI agent what it knows

## What this is

QA Tester AI is a separate harness on this machine that tests apps (Android over ADB, plus
iOS/web/desktop targets) and keeps everything it learns on disk per project: filed findings
(bug / warning / suggestion / pass), and per-module memory notes. `ask_qa_agent.py` is a
read-mostly CLI into that state, built specifically so another agent (you) can ask it
questions instead of guessing, re-testing something already covered, or filing a duplicate
bug report.

## No server required

The dashboard (`start.py` / `server.py`, normally on `localhost:8000`) does **not** need to be
running. This tool never talks to it — it reads project files straight off disk
(`findings.json`, `memory.md`, `subprojects.json`), and its `ask` subcommand spawns its own
short-lived Claude Code CLI call independent of the dashboard. It works identically whether the
dashboard is up, down, or mid-test-run.

## Where it lives

```
D:\qa tester AI\app\tools\ask_qa_agent.py
```

Run it with `py` (not bare `python` — on this machine `python` resolves to a 3.11 install with
none of this project's dependencies installed; `py` resolves to the 3.13 install that has
them). Always set `PYTHONIOENCODING=utf-8` — findings and memory routinely contain non-ASCII
text (curly quotes, non-English UI strings) and the default Windows console codepage will
crash on it otherwise.

```bash
PYTHONIOENCODING=utf-8 py "D:\qa tester AI\app\tools\ask_qa_agent.py" <command> ...
```

(In PowerShell: `$env:PYTHONIOENCODING="utf-8"; py "D:\qa tester AI\app\tools\ask_qa_agent.py" <command> ...`)

## Commands

**`projects`** — every project (app) this harness has ever tested, with module and finding
counts. Run this first if you don't already know the exact project name — it is not always a
literal package id: this harness also tests iOS/web/desktop targets, so a "package" can be a
full URL (`https://metaesthetics.net/en`) or a free-text label with spaces (`ipad Test`). Quote
it exactly as listed here in every later command.

```bash
py tools/ask_qa_agent.py projects
py tools/ask_qa_agent.py projects --json
```

**`modules <package>`** — the test modules (test suites) inside one project, each with its
status and finding count.

```bash
py tools/ask_qa_agent.py modules com.example.app
```

**`findings <package> [--module SLUG] [--kind bug|warning|suggestion|pass] [--json]`** — the
raw, exact list of filed outcomes. This is a plain read off `findings.json`: no LLM, no cost,
instant. Use this whenever a filter/lookup answers the question, e.g. "does F016 still exist",
"list every open bug in the auth module".

```bash
py tools/ask_qa_agent.py findings com.example.app --kind bug --json
```

**`ask <package> "<question>" [--module SLUG] [--json] [--model M] [--effort E] [--chat N]`**
— a question in plain English, answered by handing the project's (or one module's) memory and
findings to a one-shot Claude Code call that synthesizes a real answer, citing finding ids
(e.g. `F016`). Use this for genuine reasoning — "why does checkout look flaky", "what's the
overall health of this app", "has anything like this been seen before" — not for things a
filter already answers.

```bash
py tools/ask_qa_agent.py ask com.example.app "has the login flow been tested, and were there any bugs?" --json
py tools/ask_qa_agent.py ask com.example.app "any known issues with the checkout flow?" --module checkout
```

Omitting `--module` pulls in every module's memory and findings for that project, which is
the right default for a broad question but the more expensive call — scope to `--module` when
you already know which part of the app you care about.

## Cost

`findings` / `modules` / `projects` are free and instant — plain file reads. `ask` spends one
call against the *same* Claude Code subscription rate-limit window the QA harness's own
dashboard uses for live testing runs — typically 10–60 seconds, no per-token API billing, but
it is shared capacity. Prefer the read-only commands whenever a filter would do; reach for
`ask` only when the question genuinely needs judgment over the notes rather than a lookup.

## What it will never do

Read-only by construction: it cannot start a test, drive a device, or write a finding, note, or
memory entry. Asking it a question cannot change anything the QA agent knows. If you need new
testing done, that is a separate ask to the QA harness itself (its dashboard, or whoever owns
this machine) — not something this tool can trigger.

## Errors worth knowing

- Unknown package/module names cause an immediate, listed error naming what packages/modules
  *do* exist — read that list rather than guessing at spelling.
- `ask` on a project with many modules can produce a large context; the tool caps and truncates
  it automatically, so this is never something you need to work around.
