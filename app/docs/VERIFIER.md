# The QA Verifier

A second instance of this harness, on port 8001, that answers one question for one caller:
**does this fix actually work on the device?**

Bugmaster (`D:\bugmaster`) fixes bugs on a VPS. It cannot reach the phone, the emulator or the
iPad on this desk, so before it merges a mobile fix it queues a *device job*, and a bridge
worker running on this PC picks it up, prepares the patched build, and asks the QA Verifier to
re-run the case. The contract between the two sides is `D:\bugmaster\docs\BRIDGE.md` — §5 is
the half implemented here.

## Why a second instance

The QA Master (port 8000) tests the staging build and reports on the product. The Verifier
re-runs one case against a build that exists only as a patch in a worktree on this machine.
They must not share a notebook, and the reason is not tidiness:

- a `bug` filed against an unmerged patch would appear on the product board as a defect in the
  shipped app, get clustered with real findings, and get filed to Blackcode;
- a `pass` recorded against a patch would read, later, as the staging build having been
  re-tested;
- the per-app learned memory would fill with facts about a build nobody can install.

`PROJECTS_DIR` is the whole of the separation. Set it and every path follows — project folders,
findings, transcripts, memory, `clusters.json`, `campaigns.json`, `retests.json`,
`verifications.json`, `scratchpad.json`, `ECOSYSTEM.md`, `_trash/`. The Verifier's notebook is
`app/verify-projects/` (gitignored, like `app/projects/`).

What is deliberately *not* separated is `SYSTEM_MEMORY.md` / `system_memory.json`. Those hold
knowledge about operating the harness — how long an iOS app takes to settle, which `pm clear`
variant this ROM needs — which is the same fact for both instances and about no app under test.

## Starting it

Double-click `QA Verifier (Bugmaster).bat`, or:

```bash
cd app
python start_verifier.py            # --no-browser to skip the tab
```

It sets `PROJECTS_DIR=verify-projects`, `SERVER_PORT=8001`, `AGENT_OPEN_MODULE_TABS=false`,
starts `server.py`, pre-warms the manager session and prints the fleet.

**It refuses to start while the QA Master holds a device lock.** It reads
`GET http://localhost:8000/agent/status` and stops if `device_locks` is non-empty. Both
instances reach the same phone through the same adb and the same single WebDriverAgent port,
and `device_locks` is per-process — it cannot see across a port. A master that is simply not
running is fine and expected.

The first run seeds the `metaesthetics-verify` ecosystem from `app/verify-seed.json`: a
supervisor plus `patient-android` (pinned to `emulator-5554`), `patient-ios` and `doctor-ipad`,
copying package and platform out of the corresponding real projects so the verifier tests the
same apps. It seeds *once* — after that the ecosystem is whatever it has become.

## The loop

```
worker                         QA Verifier (:8001)
  |  POST /agent/<supervisor>/main/message
  |    "Bugmaster verification job dj_… for Blackcode #612 …"
  |------------------------------> manager: run_journey, ONE step on the named role
  |                                         (module bm-612, created if missing)
  |                                step runs, files findings with screenshots
  |                                manager gets the review turn
  |                                manager: report_verification(job_id, verdict, finding_ids, note)
  |  GET /verifications/dj_…  (every 15 s; 404 until reported)
  |<------------------------------ { job_id, verdict, note, reported_at, package, module,
  |                                  campaign_id, findings: [ …, evidence: "D:\…\003-….jpg" ] }
  |  GET /agent/shot?path=<evidence>   → base64 into the result Bugmaster gates on
```

- `GET /verifications/{job_id}` — the verdict. **404 until reported**, and the worker's own
  45-minute timeout turns a long silence into `blocked`. Never a stub, never an empty 200.
- `GET /verifications?limit=20` — what this instance has answered, newest first.

Both are read-only. There is no `POST`: a verdict is written by the manager's
`report_verification` tool on the review turn of a run it watched, and an HTTP endpoint that
could record one would let anything on loopback answer a job nobody ran.

`verdict` is `pass`, `fail` or `blocked`. A `fail` is a useful answer — Bugmaster sends the
fixer round again with the findings attached. `blocked` means nobody checked (the run errored,
the agent asked a question, the device never came up) and goes to a human. **Nothing in this
protocol turns "could not check" into "checked".**

A job is answered once. `report_verification` refuses a second, different verdict, because the
pipeline has already read and acted on the first.

## The rule that matters

**A verification run never files a Blackcode issue.** The build under test is deployed nowhere,
so a ticket about it describes software no user can reach — and it would be filed against the
product as though the shipped app were broken. In a verification run a `bug` finding means one
thing: *the fix did not work*. Report `fail`; Bugmaster files and loops on its own.

That is stated in the ecosystem manager's system prompt and enforced in the tool:
`report_verification` refuses a `pass` that lists a `bug` finding.

## The emulator

Most fixes are screen logic and do not need the physical phone, so `patient-android` is pinned
to `emulator-5554`. `emulator.py` mirrors `vbox.py` — `is_running()`, `start_headless()`,
`wait_until_booted()`, `ensure_running()` — over `config.ANDROID_AVD_NAME` and
`config.EMULATOR_PATH`, always through `config.ADB_PATH` because adb is not on PATH here.

```bash
python emulator.py            # what adb can see
python emulator.py --ensure   # boot the AVD headless and wait for sys.boot_completed
```

Nothing starts it automatically. `stacks.status("android")` names it in the `fix` hint when no
Android device is attached, and the worker brings it up itself before sending a job
(BRIDGE.md §8). An emulator serial counts as a ready Android target everywhere in the harness,
but it cannot vouch for the camera, biometrics, push notifications or performance — Bugmaster
marks those jobs `needsRealDevice` and waits for the phone.
