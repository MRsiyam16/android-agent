"""Self-improving memory about *operating this harness* — never about any app under test.

`memory.py` remembers what a particular app did (screens, bugs, tried taps) and lives in
the gitignored `projects/` folder. This module remembers something different and longer
lived: **how to drive the system efficiently**. How long a Flutter first frame really takes
on this machine, whether `pm clear` needs `--user 0` on this ROM, that an empty UI dump
means "wait longer" rather than "the app is broken.

Two rules keep it useful:

1. **No test findings.** Nothing here records what an app did or whether it passed. A
   defect belongs in a report; only facts that change *how the harness should behave* go
   here. Timings are therefore keyed by UI toolkit, not by package name — "Flutter needs
   ~12s for its first frame" transfers to the next Flutter app, "com.example needs 12s"
   does not.
2. **Evidence or nothing.** A lesson is recorded with what produced it and a hit counter.
   Repeated observations raise confidence; a lesson never seen again ages out of the
   digest rather than being trusted forever.

The store is JSON at `android-agent/system_memory.json` and is **tracked in git** on
purpose: the whole point is that run N+1 starts smarter than run N, on any clone.

Usage from a run:

    import system_memory as sysmem

    with sysmem.run_session(tool="run_agent") as run:
        settle = sysmem.suggest_launch_settle("flutter")   # learned, not guessed
        ...
        sysmem.observe_launch("flutter", elapsed)          # feed the next run
        sysmem.learn("adb-pm-clear-user0",
                     "`pm clear` fails silently without --user 0 on multi-user ROMs",
                     evidence="observed on a Samsung ROM; plain form returned Success but kept the session")
        run.note(steps=50)

Nothing here is allowed to break a run: every public function swallows its own errors and
degrades to a sane default.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

logger = logging.getLogger("system_memory")

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.join(HERE, "system_memory.json")
DIGEST_PATH = os.path.join(HERE, "SYSTEM_MEMORY.md")

SCHEMA_VERSION = 1
MAX_SAMPLES = 40          # per timing key; enough for a stable p90 without unbounded growth
MAX_RUNS = 30             # rolling window of run summaries
STALE_LESSON_RUNS = 60    # lessons unseen for this many runs drop out of the digest


# --------------------------------------------------------------------------- store
def _empty() -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "updated_at": None,
        "run_count": 0,
        "environment": {},   # key -> {value, evidence, updated_at}
        "timings": {},       # key -> {samples: [...], unit: "s"}
        "lessons": {},       # id -> {text, evidence, hits, first_seen_run, last_seen_run}
        "runs": [],          # rolling summaries: {tool, started, seconds, ok, note}
    }


def load() -> dict[str, Any]:
    """Read the store, tolerating absence or corruption — a broken file must not stop a run."""
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "schema" not in data:
            raise ValueError("unrecognised system memory format")
        for key, default in _empty().items():
            data.setdefault(key, default)
        return data
    except FileNotFoundError:
        return _empty()
    except Exception as exc:  # noqa: BLE001
        logger.warning("system memory unreadable (%s); starting fresh in memory", exc)
        return _empty()


def save(data: dict[str, Any]) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = STORE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, STORE_PATH)      # atomic: a killed run cannot leave a half-file
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not persist system memory: %s", exc)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _mutate(fn) -> Any:
    data = load()
    result = fn(data)
    save(data)
    return result


# --------------------------------------------------------------------------- timings
def observe_launch(toolkit: str, seconds: float) -> None:
    """Record how long a first usable UI dump actually took for this UI toolkit."""
    observe_timing(f"launch_settle.{toolkit or 'unknown'}", seconds)


def observe_timing(key: str, seconds: float) -> None:
    if seconds is None or seconds < 0 or seconds > 600:
        return                                   # nonsense sample: drop rather than skew
    def apply(data):
        entry = data["timings"].setdefault(key, {"samples": [], "unit": "s"})
        entry["samples"].append(round(float(seconds), 2))
        entry["samples"] = entry["samples"][-MAX_SAMPLES:]
    try:
        _mutate(apply)
    except Exception as exc:  # noqa: BLE001
        logger.debug("observe_timing failed: %s", exc)


def suggest_timing(key: str, default: float, floor: float = 0.0,
                   ceiling: float = 120.0, headroom: float = 1.25) -> float:
    """A learned wait: the slowest of the recent samples plus headroom.

    Deliberately pessimistic. Waiting a second too long costs a second; waiting too
    little costs a dump of the wrong screen and, downstream, a false defect — the two
    errors are not symmetrical, so this tunes toward the slow tail rather than the mean.
    Falls back to `default` until there is enough evidence to beat a guess.
    """
    try:
        samples = load()["timings"].get(key, {}).get("samples", [])
        if len(samples) < 3:
            return max(floor, default)
        ordered = sorted(samples)
        idx = max(0, int(len(ordered) * 0.9) - 1)         # ~p90, min() of the slow tail
        value = max(ordered[idx], statistics.median(ordered))
        return round(min(ceiling, max(floor, value * headroom)), 2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("suggest_timing failed: %s", exc)
        return max(floor, default)


def suggest_launch_settle(toolkit: str, default: float = 6.0) -> float:
    return suggest_timing(f"launch_settle.{toolkit or 'unknown'}",
                          default=default, floor=2.0, ceiling=45.0)


# --------------------------------------------------------------------------- facts
def observe_environment(key: str, value: Any, evidence: str = "") -> None:
    """Record a stable fact about this machine/ROM/toolchain (adb path, ROM quirks…)."""
    def apply(data):
        prev = data["environment"].get(key, {})
        if prev.get("value") == value:
            return
        data["environment"][key] = {
            "value": value,
            "evidence": evidence or prev.get("evidence", ""),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    try:
        _mutate(apply)
    except Exception as exc:  # noqa: BLE001
        logger.debug("observe_environment failed: %s", exc)


def environment(key: str, default: Any = None) -> Any:
    try:
        return load()["environment"].get(key, {}).get("value", default)
    except Exception:  # noqa: BLE001
        return default


def learn(lesson_id: str, text: str, evidence: str = "") -> None:
    """Upsert an operational lesson. Re-learning the same id raises its confidence."""
    def apply(data):
        run_no = data.get("run_count", 0)
        entry = data["lessons"].get(lesson_id)
        if entry:
            entry["hits"] = entry.get("hits", 1) + 1
            entry["last_seen_run"] = run_no
            entry["text"] = text or entry["text"]
            if evidence:
                entry["evidence"] = evidence
        else:
            data["lessons"][lesson_id] = {
                "text": text, "evidence": evidence, "hits": 1,
                "first_seen_run": run_no, "last_seen_run": run_no,
            }
    try:
        _mutate(apply)
    except Exception as exc:  # noqa: BLE001
        logger.debug("learn failed: %s", exc)


def lessons(min_hits: int = 1) -> list[dict[str, Any]]:
    try:
        data = load()
        run_no = data.get("run_count", 0)
        out = []
        for lid, entry in data["lessons"].items():
            if entry.get("hits", 0) < min_hits:
                continue
            if run_no - entry.get("last_seen_run", 0) > STALE_LESSON_RUNS:
                continue
            out.append({"id": lid, **entry})
        return sorted(out, key=lambda e: (-e.get("hits", 0), e["id"]))
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- runs
class RunHandle:
    """Handed to the caller inside `run_session` so a run can annotate itself."""

    def __init__(self, tool: str):
        self.tool = tool
        self.started = time.monotonic()
        self.ok = True
        self.notes: dict[str, Any] = {}

    def note(self, **kwargs: Any) -> None:
        self.notes.update(kwargs)


@contextmanager
def run_session(tool: str) -> Iterator[RunHandle]:
    """Bracket a run so it is counted, timed, and folded into the digest on exit.

    The digest is rewritten on the way out, which is what makes the system *self*-
    learning rather than merely instrumented: every run leaves the next one a better
    briefing without anyone remembering to update a document.
    """
    handle = RunHandle(tool)
    try:
        yield handle
    except BaseException:
        handle.ok = False
        raise
    finally:
        elapsed = round(time.monotonic() - handle.started, 1)

        def apply(data):
            data["run_count"] = data.get("run_count", 0) + 1
            data["runs"].append({
                "tool": handle.tool,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "seconds": elapsed,
                "ok": handle.ok,
                **handle.notes,
            })
            data["runs"] = data["runs"][-MAX_RUNS:]
        try:
            _mutate(apply)
            write_digest()
        except Exception as exc:  # noqa: BLE001
            logger.debug("run_session bookkeeping failed: %s", exc)


# --------------------------------------------------------------------------- digest
def write_digest() -> Optional[str]:
    """Regenerate the human/agent-readable briefing from the store."""
    try:
        data = load()
        lines = [
            "# System memory — how to drive this harness",
            "",
            "<!-- GENERATED by system_memory.py after every run. Do not hand-edit: your",
            "     changes are overwritten. Add knowledge with system_memory.learn(...)",
            "     or `python system_memory.py --learn <id> \"<text>\"`. -->",
            "",
            "Operational knowledge only — how to *run* the system, never what any app under",
            "test did. Test findings belong in a report, not here.",
            "",
            f"Runs recorded: **{data.get('run_count', 0)}** · "
            f"last updated: {data.get('updated_at') or 'never'}",
            "",
        ]

        env = data.get("environment", {})
        if env:
            lines += ["## Environment", "", "| Fact | Value | Why we know |", "|---|---|---|"]
            for key in sorted(env):
                e = env[key]
                lines.append(f"| `{key}` | `{e.get('value')}` | {e.get('evidence', '') or '—'} |")
            lines.append("")

        timings = data.get("timings", {})
        if timings:
            lines += ["## Learned waits", "",
                      "Tuned to the slow tail on purpose: waiting too little yields a dump of",
                      "the wrong screen, which downstream becomes a false defect.", "",
                      "| Key | Samples | Median | Slowest | Recommended |", "|---|---|---|---|---|"]
            for key in sorted(timings):
                s = timings[key].get("samples", [])
                if not s:
                    continue
                lines.append(
                    f"| `{key}` | {len(s)} | {statistics.median(s):.1f}s | {max(s):.1f}s | "
                    f"**{suggest_timing(key, default=max(s)):.1f}s** |")
            lines.append("")

        ls = lessons()
        if ls:
            lines += ["## Operating lessons", ""]
            for e in ls:
                conf = "confirmed" if e.get("hits", 0) >= 3 else (
                    "seen twice" if e.get("hits", 0) == 2 else "seen once")
                lines.append(f"- **{e['id']}** ({conf}) — {e['text']}")
                if e.get("evidence"):
                    lines.append(f"  - _evidence:_ {e['evidence']}")
            lines.append("")

        runs = data.get("runs", [])
        if runs:
            ok = sum(1 for r in runs if r.get("ok"))
            lines += ["## Recent runs", "",
                      f"{ok}/{len(runs)} of the last runs completed without raising.", ""]
            for r in reversed(runs[-8:]):
                mark = "ok" if r.get("ok") else "FAILED"
                extra = " ".join(f"{k}={v}" for k, v in r.items()
                                 if k not in ("tool", "at", "seconds", "ok"))
                lines.append(f"- `{r.get('at')}` {r.get('tool')} — {r.get('seconds')}s, {mark}"
                             + (f" ({extra})" if extra else ""))
            lines.append("")

        text = "\n".join(lines)
        with open(DIGEST_PATH, "w", encoding="utf-8") as fh:
            fh.write(text)
        return DIGEST_PATH
    except Exception as exc:  # noqa: BLE001
        logger.debug("write_digest failed: %s", exc)
        return None


def briefing(max_lessons: int = 8) -> str:
    """One-screen summary for logging at the start of a run."""
    data = load()
    parts = [f"system memory: {data.get('run_count', 0)} runs recorded"]
    ls = lessons(min_hits=2)[:max_lessons]
    if ls:
        parts.append("confirmed lessons: " + "; ".join(e["id"] for e in ls))
    return " | ".join(parts)


# --------------------------------------------------------------------------- CLI
def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Inspect or extend the harness's system memory.")
    ap.add_argument("--show", action="store_true", help="print the digest")
    ap.add_argument("--learn", nargs=2, metavar=("ID", "TEXT"), help="record an operating lesson")
    ap.add_argument("--evidence", default="", help="evidence for --learn")
    ap.add_argument("--forget", metavar="ID", help="drop a lesson by id")
    ap.add_argument("--rebuild", action="store_true", help="regenerate SYSTEM_MEMORY.md")
    args = ap.parse_args()

    if args.learn:
        learn(args.learn[0], args.learn[1], args.evidence)
        print(f"learned: {args.learn[0]}")
    if args.forget:
        _mutate(lambda d: d["lessons"].pop(args.forget, None))
        print(f"forgot: {args.forget}")
    if args.learn or args.forget or args.rebuild:
        write_digest()
    if args.show or not any([args.learn, args.forget, args.rebuild]):
        path = write_digest()
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                print(fh.read())
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
