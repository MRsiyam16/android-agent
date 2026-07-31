"""The self-learning store, and the three ways it was not reaching anybody.

`system_memory.py` is the mechanism that is supposed to make run N+1 start smarter than run
N. All three of these were silent: the store kept accumulating correctly and what read it
got something stale, empty, or frozen.

* **The digest drifted.** `SYSTEM_MEMORY.md` was rewritten only on the way out of
  `run_session`, which brackets a whole `run_agent.py` process and is the only caller that
  can. Everything the Agent tab learns arrives through `learn`/`observe_*` inside a
  long-lived server, so measured timings and newer lessons sat in the JSON for days while
  the digest — the file CLAUDE.md tells you to read before a device run — still showed an
  earlier afternoon. It had no "Learned waits" table at all despite fifteen samples.
* **`run_count` never moved.** Only `run_session` incremented it, so it read 0 forever. Two
  things depend on it: the digest header, which claimed no run had ever been recorded, and
  the lesson-ageing window, which could never advance.
* **The prompt got names without rules.** `build_system_prompt` was handed `briefing()`,
  which returns ids and a count, under the heading "operating notes learned from previous
  runs" — so what reached the agent was `confirmed lessons: ui-never-settled` and nothing
  else. A label for a rule, with the rule missing, presented as fact to prefer over its own
  assumptions.

The store and digest paths are redirected at a tmp directory for every test by the autouse
`isolated_system_memory` fixture in conftest.py, so nothing here touches the tracked files.
"""
from __future__ import annotations

from pathlib import Path

import system_memory as sysmem


def digest() -> str:
    path = Path(sysmem.DIGEST_PATH)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


class TestDigestTracksTheStore:
    """Whatever changes the store rewrites the digest, so the two cannot disagree."""

    def test_a_lesson_reaches_the_digest_without_waiting_for_a_run_to_end(self):
        sysmem.learn("dump-shows-top-window-only",
                     "A dump shows only the topmost window, so a missing marker means "
                     "covered, not navigated.",
                     evidence="an auth-error modal read as an acceptance")
        assert "dump-shows-top-window-only" in digest()
        assert "only the topmost window" in digest()

    def test_a_measured_wait_reaches_the_digest_too(self):
        # The exact gap that was visible on disk: fifteen native samples in the store and no
        # Learned-waits table in the digest.
        for _ in range(3):
            sysmem.observe_launch("flutter", 12.0)
        assert "Learned waits" in digest()
        assert "launch_settle.flutter" in digest()

    def test_an_environment_fact_reaches_the_digest(self):
        sysmem.observe_environment("pm_clear_variant", "user0",
                                   evidence="the bare form returned Success and did nothing")
        assert "pm_clear_variant" in digest()

    def test_the_digest_is_rendered_from_the_state_just_persisted(self):
        # Rendering from a re-read would be a second load and a second chance to disagree.
        sysmem.learn("first", "The first rule.")
        sysmem.learn("second", "The second rule.")
        assert "The first rule." in digest() and "The second rule." in digest()


class TestRunCounting:
    def test_record_run_advances_the_counter_the_digest_reports(self):
        sysmem.record_run("agent:auth", 12.5, True, turns=4, taps=9)
        assert sysmem.load()["run_count"] == 1
        assert "Runs recorded: **1**" in digest()

    def test_a_run_is_summarised_with_whatever_the_caller_noted(self):
        sysmem.record_run("agent:auth", 3.0, True, turns=4)
        entry = sysmem.load()["runs"][-1]
        assert entry["tool"] == "agent:auth"
        assert entry["ok"] is True
        assert entry["turns"] == 4

    def test_a_failed_run_is_recorded_as_failed(self):
        sysmem.record_run("agent:auth", 1.0, False)
        assert sysmem.load()["runs"][-1]["ok"] is False

    def test_run_session_still_counts_exactly_one_run(self):
        with sysmem.run_session(tool="run_agent") as run:
            run.note(steps_requested=30)
        data = sysmem.load()
        assert data["run_count"] == 1
        assert data["runs"][-1]["tool"] == "run_agent"
        assert data["runs"][-1]["steps_requested"] == 30

    def test_a_raising_run_is_still_counted_and_marked_failed(self):
        try:
            with sysmem.run_session(tool="run_agent"):
                raise RuntimeError("device vanished")
        except RuntimeError:
            pass
        data = sysmem.load()
        assert data["run_count"] == 1
        assert data["runs"][-1]["ok"] is False


class TestLessonAgeing:
    """Only a single sighting ever expires.

    The window was written while `run_count` was frozen at 0, so it had never been tested
    against a moving counter. Now that agent instructions advance it too, an unguarded window
    would start dropping rules that each cost a wrong bug report to learn — and forgetting one
    is the expensive direction to be wrong in.
    """

    def _store_with_lessons(self, run_count: int, lessons: dict) -> None:
        data = sysmem.load()
        data["run_count"] = run_count
        data["lessons"] = lessons
        sysmem.save(data)

    def test_a_recent_single_sighting_is_kept(self):
        self._store_with_lessons(100, {
            "recent": {"text": "t", "hits": 1, "last_seen_run": 100}})
        assert [e["id"] for e in sysmem.lessons()] == ["recent"]

    def test_an_old_single_sighting_ages_out(self):
        self._store_with_lessons(sysmem.STALE_LESSON_RUNS + 5, {
            "long-ago": {"text": "t", "hits": 1, "last_seen_run": 0}})
        assert sysmem.lessons() == []

    def test_an_old_lesson_seen_twice_is_never_dropped(self):
        self._store_with_lessons(sysmem.STALE_LESSON_RUNS + 500, {
            "twice": {"text": "t", "hits": 2, "last_seen_run": 0},
            "confirmed": {"text": "t", "hits": 9, "last_seen_run": 0},
            "once": {"text": "t", "hits": 1, "last_seen_run": 0},
        })
        assert {e["id"] for e in sysmem.lessons()} == {"twice", "confirmed"}

    def test_the_most_confident_lesson_comes_first(self):
        self._store_with_lessons(0, {
            "seen-once": {"text": "t", "hits": 1, "last_seen_run": 0},
            "seen-often": {"text": "t", "hits": 7, "last_seen_run": 0},
        })
        assert [e["id"] for e in sysmem.lessons()][0] == "seen-often"


class TestOperatingNotes:
    def test_the_notes_carry_the_rule_and_not_just_its_name(self):
        sysmem.learn("dump-shows-top-window-only",
                     "A dump shows only the topmost window.", evidence="x")
        notes = sysmem.operating_notes()
        assert "dump-shows-top-window-only" in notes
        assert "A dump shows only the topmost window." in notes

    def test_a_lesson_seen_only_once_still_reaches_the_notes(self):
        # `briefing` filters to hits>=2 because it is a one-line log summary. The prompt must
        # not: a failure recorded once is still the only record that it happened, and most
        # lessons in the real store sit at exactly one sighting.
        sysmem.learn("seen-once", "Something that happened once.")
        assert "seen-once" in sysmem.operating_notes()
        assert "seen-once" not in sysmem.briefing()

    def test_confidence_is_stated_so_the_reader_can_weigh_it(self):
        sysmem.learn("once", "One sighting.")
        for _ in range(3):
            sysmem.learn("thrice", "Three sightings.")
        notes = sysmem.operating_notes()
        assert "(seen once)" in notes
        assert "(confirmed)" in notes

    def test_an_empty_store_yields_no_text_rather_than_a_bare_heading(self):
        # build_system_prompt appends a heading only if this is non-empty; `briefing` always
        # returned its run count, so the section was always added and often said nothing.
        assert sysmem.operating_notes() == ""

    def test_the_notes_are_bounded(self):
        for i in range(30):
            sysmem.learn(f"lesson-{i:02d}", f"Rule {i}.")
        assert len(sysmem.operating_notes(max_lessons=10).splitlines()) == 10


class TestTheAgentPromptReceivesThem:
    def test_the_lesson_text_reaches_the_system_prompt(self, tmp_path, monkeypatch):
        import project_paths
        from agent import prompts

        monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path / "projects")
        sysmem.learn("dump-shows-top-window-only",
                     "A dump shows only the topmost window.", evidence="x")
        prompt = prompts.build_system_prompt("com.example.app", "auth", "Auth", "")
        assert "A dump shows only the topmost window." in prompt

    def test_a_store_with_nothing_learned_adds_no_empty_section(self, tmp_path, monkeypatch):
        import project_paths
        from agent import prompts

        monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path / "projects")
        prompt = prompts.build_system_prompt("com.example.app", "auth", "Auth", "")
        assert "Operating notes learned from previous runs" not in prompt


class TestFailuresNeverBreakARun:
    """Every public function swallows its own errors and degrades to a sane default —
    the module's own stated contract, and the reason a run can trust it."""

    def test_an_unwritable_store_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(sysmem, "STORE_PATH", str(Path("/nonexistent-dir") / "s.json"))
        monkeypatch.setattr(sysmem, "DIGEST_PATH", str(Path("/nonexistent-dir") / "d.md"))
        sysmem.learn("anything", "text")           # must not raise
        sysmem.record_run("agent:auth", 1.0)       # must not raise
        assert sysmem.operating_notes() == ""

    def test_a_corrupt_store_reads_as_empty_rather_than_raising(self):
        Path(sysmem.STORE_PATH).write_text("{not json", encoding="utf-8")
        assert sysmem.load()["run_count"] == 0
        assert sysmem.lessons() == []

    def test_a_nonsense_timing_sample_is_dropped_rather_than_skewing_the_budget(self):
        sysmem.observe_timing("launch_settle.native", -5)
        sysmem.observe_timing("launch_settle.native", 10_000)
        assert sysmem.load()["timings"] == {}
