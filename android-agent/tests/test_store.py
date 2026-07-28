"""On-disk state: sub-projects, transcripts, findings, credentials.

This module owns the only durable record of a test run. A lost finding or a clobbered
transcript is not recoverable from anywhere else, and the dashboard has already proved once
that a whole-file rewrite of a live document loses data when two writers race.
"""
from __future__ import annotations

import json
import threading

import pytest

import project_paths
from agent import store

PKG = "com.example.app"


@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    """Point the store at a temp tree so tests never touch real project data.

    Patched on `project_paths` rather than on `store`: since projects can be pointed at a
    folder anywhere, `store.project_dir` no longer builds a path from its own constant and
    asks `project_paths` instead. Patching `store.PROJECTS_DIR` still *worked* — it just
    stopped being read, so the whole suite wrote into the real projects folder while every
    assertion still passed on the first run and failed on the second.
    """
    root = tmp_path / "projects"
    monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", root)
    monkeypatch.setattr(store, "PROJECTS_DIR", root)
    return root


class TestSlugify:
    def test_titles_become_stable_folder_names(self):
        assert store.slugify("Auth & Login") == "auth-login"
        assert store.slugify("  Checkout  ") == "checkout"
        assert store.slugify("Cart/Checkout (v2)") == "cart-checkout-v2"

    def test_titles_with_no_usable_characters_still_yield_a_name(self):
        # A slug is a path segment; returning "" would put the module's files in its parent.
        assert store.slugify("???") == "module"
        assert store.slugify("") == "module"

    def test_package_names_survive_but_separators_do_not(self):
        # _safe_name must never let a title escape the projects tree.
        assert store._safe_name("com.example.app") == "com.example.app"
        assert "/" not in store._safe_name("../../etc/passwd")
        assert "\\" not in store._safe_name(r"..\..\windows")


class TestSubprojects:
    def test_create_then_read_back(self):
        store.create_subproject(PKG, "Auth & Login", "login, signup, reset")
        entry = store.get_subproject(PKG, "auth-login")
        assert entry["title"] == "Auth & Login"
        assert entry["status"] == "proposed"
        assert entry["finding_count"] == 0

    def test_recreating_updates_instead_of_duplicating(self):
        # Recon proposing a module that already exists must not leave the user two copies
        # to reconcile by hand.
        store.create_subproject(PKG, "Auth", "first scope")
        store.create_subproject(PKG, "Auth", "revised scope")
        modules = store.list_subprojects(PKG)
        assert len(modules) == 1
        assert modules[0]["scope"] == "revised scope"

    def test_update_is_a_merge_not_a_replace(self):
        store.create_subproject(PKG, "Auth", "login flows")
        store.update_subproject(PKG, "auth", status="tested")
        entry = store.get_subproject(PKG, "auth")
        assert entry["status"] == "tested"
        assert entry["scope"] == "login flows", "unrelated fields must survive an update"

    def test_deleting_removes_the_listing_but_keeps_the_evidence(self):
        store.create_subproject(PKG, "Auth", "")
        store.append_chat(PKG, "auth", {"role": "user", "text": "test login"})
        assert store.delete_subproject(PKG, "auth") is True
        assert store.get_subproject(PKG, "auth") is None
        # A mis-click must not be able to destroy a test history.
        assert (store.subproject_dir(PKG, "auth") / "chat.jsonl").is_file()

    def test_deleting_something_absent_reports_false(self):
        assert store.delete_subproject(PKG, "nope") is False

    def test_unknown_subproject_updates_report_none(self):
        assert store.update_subproject(PKG, "nope", status="tested") is None


class TestChatTranscript:
    def test_appends_survive_a_torn_final_line(self):
        # A hard kill mid-write leaves a partial line. Losing the whole transcript to it
        # would be far worse than losing the last entry.
        store.append_chat(PKG, "auth", {"role": "user", "text": "one"})
        store.append_chat(PKG, "auth", {"role": "agent", "text": "two"})
        path = store.subproject_dir(PKG, "auth") / "chat.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"role": "agent", "text": "tor')
        messages = store.read_chat(PKG, "auth")
        assert [m["text"] for m in messages] == ["one", "two"]

    def test_every_entry_is_timestamped(self):
        record = store.append_chat(PKG, "auth", {"role": "user", "text": "hi"})
        assert record["ts"].endswith("Z")

    def test_limit_returns_the_most_recent_entries(self):
        for i in range(10):
            store.append_chat(PKG, "auth", {"role": "user", "text": str(i)})
        assert [m["text"] for m in store.read_chat(PKG, "auth", limit=3)] == ["7", "8", "9"]

    def test_non_ascii_labels_round_trip(self):
        # App labels are not all Latin; the Windows console default encoding has taken a
        # whole run down over exactly this.
        store.append_chat(PKG, "auth", {"role": "user", "text": "検索 · Ünïcode"})
        assert store.read_chat(PKG, "auth")[-1]["text"] == "検索 · Ünïcode"

    def test_reading_a_module_with_no_transcript_is_empty_not_an_error(self):
        assert store.read_chat(PKG, "never-used") == []


class TestFindings:
    def test_findings_are_numbered_sequentially(self):
        store.create_subproject(PKG, "Auth", "")
        first = store.add_finding(PKG, "auth", {"title": "A", "severity": "high"})
        second = store.add_finding(PKG, "auth", {"title": "B", "severity": "low"})
        assert (first["id"], second["id"]) == ("F001", "F002")

    def test_filing_updates_the_modules_count(self):
        store.create_subproject(PKG, "Auth", "")
        store.add_finding(PKG, "auth", {"title": "A", "severity": "high"})
        assert store.get_subproject(PKG, "auth")["finding_count"] == 1

    def test_findings_are_readable_without_replaying_the_chat(self):
        # The transcript is what happened; findings are what was concluded. A report must be
        # rebuildable from the latter alone.
        store.create_subproject(PKG, "Auth", "")
        store.add_finding(PKG, "auth", {"title": "Empty submit accepted", "severity": "high"})
        raw = json.loads((store.subproject_dir(PKG, "auth") / "findings.json")
                         .read_text(encoding="utf-8"))
        assert raw[0]["title"] == "Empty submit accepted"


class TestFindingKinds:
    """The four buckets behind the top-bar pills.

    `kind` arrived after findings already existed on disk, and a project's whole history is in
    those files — so the default has to be the one that keeps old data showing what it always
    showed, not the one that reads best in the enum.
    """

    def test_a_finding_filed_before_kind_existed_reads_as_a_bug(self):
        store.create_subproject(PKG, "Auth", "")
        # Written the way the old tool wrote them: no `kind` at all.
        path = store.subproject_dir(PKG, "auth") / "findings.json"
        path.write_text(json.dumps([{"id": "F001", "title": "Old", "severity": "high"}]),
                        encoding="utf-8")
        assert store.list_findings(PKG, "auth")[0]["kind"] == "bug"

    def test_an_unrecognised_kind_falls_back_to_bug(self):
        # A hand-edited file or a future kind must not vanish from every bucket — a finding
        # that exists on disk but appears in no tab is worse than one filed under the wrong tab.
        store.create_subproject(PKG, "Auth", "")
        path = store.subproject_dir(PKG, "auth") / "findings.json"
        path.write_text(json.dumps([{"id": "F001", "title": "X", "kind": "nonsense"}]),
                        encoding="utf-8")
        assert store.list_findings(PKG, "auth")[0]["kind"] == "bug"

    def test_kinds_are_preserved_as_filed(self):
        store.create_subproject(PKG, "Auth", "")
        for kind in store.FINDING_KINDS:
            store.add_finding(PKG, "auth", {"title": kind, "kind": kind, "severity": "low"})
        assert [f["kind"] for f in store.list_findings(PKG, "auth")] == list(store.FINDING_KINDS)

    def test_all_findings_are_tagged_with_the_module_they_came_from(self):
        # The popups group by module, and a finding with no module attached would have to be
        # dropped or shown under a heading it does not belong to.
        store.create_subproject(PKG, "Auth", "")
        store.create_subproject(PKG, "Checkout", "")
        store.add_finding(PKG, "auth", {"title": "A", "kind": "bug"})
        store.add_finding(PKG, "checkout", {"title": "B", "kind": "pass"})
        rolled = {f["title"]: f for f in store.list_all_findings(PKG)}
        assert rolled["A"]["module_slug"] == "auth"
        assert rolled["A"]["module_title"] == "Auth"
        assert rolled["B"]["module_slug"] == "checkout"

    def test_a_module_with_no_findings_contributes_nothing(self):
        store.create_subproject(PKG, "Auth", "")
        store.create_subproject(PKG, "Empty", "")
        store.add_finding(PKG, "auth", {"title": "A", "kind": "bug"})
        assert len(store.list_all_findings(PKG)) == 1


class TestProjectLocation:
    """A project may be pointed at a folder anywhere on disk."""

    def test_an_unregistered_package_keeps_the_default_layout(self, isolated_projects_dir):
        # Every project that predates the registry has to go on working untouched.
        assert store.project_dir(PKG) == isolated_projects_dir / PKG

    def test_a_registered_package_moves_with_everything_under_it(self, tmp_path):
        elsewhere = tmp_path / "somewhere else"
        project_paths.register(PKG, elsewhere)
        store.create_subproject(PKG, "Auth", "")
        store.add_finding(PKG, "auth", {"title": "A", "kind": "bug", "evidence": "x"})
        store.set_secret(PKG, "test_password", "hunter2")
        # The whole tree follows the project, not just the board: transcripts, findings and
        # credentials all have to land in the folder the user chose.
        assert store.project_dir(PKG) == elsewhere
        assert (elsewhere / "secrets.json").is_file()
        assert (elsewhere / "agent" / "auth" / "findings.json").is_file()

    def test_the_registry_lists_moved_and_unmoved_projects_alike(self, tmp_path):
        elsewhere = tmp_path / "moved"
        project_paths.register("com.moved.app", elsewhere)
        (elsewhere / "meta.json").write_text('{"package": "com.moved.app"}', encoding="utf-8")
        default = store.project_dir(PKG)
        default.mkdir(parents=True, exist_ok=True)
        (default / "meta.json").write_text(f'{{"package": "{PKG}"}}', encoding="utf-8")
        assert set(project_paths.known_packages()) == {"com.moved.app", PKG}


class TestSecrets:
    def test_values_are_stored_but_only_names_are_listed(self):
        store.set_secret(PKG, "test_password", "hunter2")
        assert store.secret_keys(PKG) == ["test_password"]
        assert store.get_secrets(PKG)["test_password"] == "hunter2"

    def test_setting_a_second_secret_keeps_the_first(self):
        store.set_secret(PKG, "test_email", "a@b.c")
        store.set_secret(PKG, "test_password", "hunter2")
        assert store.secret_keys(PKG) == ["test_email", "test_password"]

    def test_overwriting_a_secret_replaces_only_that_one(self):
        store.set_secret(PKG, "test_email", "old@b.c")
        store.set_secret(PKG, "test_password", "hunter2")
        store.set_secret(PKG, "test_email", "new@b.c")
        secrets = store.get_secrets(PKG)
        assert secrets == {"test_email": "new@b.c", "test_password": "hunter2"}

    def test_secrets_live_outside_the_agent_folder_that_gets_shared(self):
        # secrets.json sits at the project root and is gitignored there; putting it under
        # agent/<slug>/ would place credentials next to the evidence people copy around.
        store.set_secret(PKG, "test_password", "hunter2")
        assert (store.project_dir(PKG) / "secrets.json").is_file()


class TestConcurrentWriters:
    """The callers are concurrent: each chat turn is its own task, modules run in parallel,
    and the device tools reach this module through asyncio.to_thread. Read-modify-write
    without a lock loses data — these tests fail if the lock is removed."""

    def test_findings_filed_in_parallel_all_survive_with_unique_ids(self):
        store.create_subproject(PKG, "Auth", "")
        barrier = threading.Barrier(8)

        def file_one(i: int) -> None:
            barrier.wait()  # maximise the overlap on the read-modify-write
            store.add_finding(PKG, "auth", {"title": f"defect {i}", "severity": "low"})

        threads = [threading.Thread(target=file_one, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        findings = store.list_findings(PKG, "auth")
        assert len(findings) == 8, "a finding was lost to a concurrent write"
        assert len({f["id"] for f in findings}) == 8, "two findings share an id"
        assert store.get_subproject(PKG, "auth")["finding_count"] == 8

    def test_parallel_module_creation_does_not_drop_modules(self):
        barrier = threading.Barrier(6)

        def create(i: int) -> None:
            barrier.wait()
            store.create_subproject(PKG, f"Module {i}", f"scope {i}")

        threads = [threading.Thread(target=create, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(store.list_subprojects(PKG)) == 6

    def test_parallel_secrets_do_not_overwrite_each_other(self):
        barrier = threading.Barrier(6)

        def store_one(i: int) -> None:
            barrier.wait()
            store.set_secret(PKG, f"cred_{i}", f"value_{i}")

        threads = [threading.Thread(target=store_one, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert store.secret_keys(PKG) == [f"cred_{i}" for i in range(6)]

    def test_parallel_chat_appends_produce_no_torn_lines(self):
        # Multi-KB entries: past the size where one write lands indivisibly, so interleaved
        # appends would corrupt a line and read_chat would silently skip it.
        barrier = threading.Barrier(8)
        body = "x" * 4096

        def append(i: int) -> None:
            barrier.wait()
            store.append_chat(PKG, "auth", {"role": "agent", "text": f"{i}:{body}"})

        threads = [threading.Thread(target=append, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        messages = store.read_chat(PKG, "auth")
        assert len(messages) == 8, "an entry was lost or corrupted by an interleaved write"
        assert {m["text"].split(":")[0] for m in messages} == {str(i) for i in range(8)}


class TestAtomicWrites:
    def test_a_crash_mid_write_cannot_truncate_the_original(self, monkeypatch):
        store.create_subproject(PKG, "Auth", "login flows")

        real_replace = store.Path.replace

        def explode(self, target):  # noqa: ANN001
            raise OSError("simulated crash between write and replace")

        monkeypatch.setattr(store.Path, "replace", explode)
        store.update_subproject(PKG, "auth", status="tested")   # must not raise
        monkeypatch.setattr(store.Path, "replace", real_replace)

        # The old content is still intact and readable.
        assert store.get_subproject(PKG, "auth")["scope"] == "login flows"
