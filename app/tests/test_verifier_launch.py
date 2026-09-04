"""The second instance: its own notebook, its emulator, and the one thing it refuses over.

Three separate mistakes are guarded here, and each of them is silent when it happens.

**A shared notebook.** `PROJECTS_DIR` is the whole of the separation between the QA Master and
the QA Verifier. If it stopped being honoured, findings about an unmerged patch would land on
the product board looking exactly like findings about the shipped app — and nothing downstream
could tell them apart afterwards.

**An emulator that does not count.** `adb devices` does not distinguish an AVD from a cable,
and neither does any adapter in this codebase. Reporting Android as "not ready" because no
phone is plugged in would refuse a run on a device that is already up, with a fix telling
somebody to find a USB cable.

**Two harnesses on one phone.** `device_locks` is per-process. It cannot see across a port, so
the *only* place a collision between the two instances can be caught is at launch, by asking
the master what it is holding.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import config
import emulator
import project_paths
import stacks
import start_verifier


# -- the second notebook -----------------------------------------------------------------------
class TestProjectsDirOverride:
    def test_an_unset_variable_keeps_the_folder_beside_the_code(self, monkeypatch):
        monkeypatch.delenv("PROJECTS_DIR", raising=False)
        assert project_paths._configured_projects_dir() == project_paths.BASE_DIR / "projects"

    def test_a_relative_value_is_resolved_against_app_not_the_cwd(self, monkeypatch):
        """The same reason the registry stores absolute paths: `app/` is a fixed point and the
        working directory is not the same on a double-clicked .bat as it is in a terminal."""
        monkeypatch.setenv("PROJECTS_DIR", "verify-projects")
        assert (project_paths._configured_projects_dir()
                == project_paths.BASE_DIR / "verify-projects")

    def test_an_absolute_value_is_used_as_given(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "elsewhere"))
        assert project_paths._configured_projects_dir() == tmp_path / "elsewhere"

    def test_blank_and_quoted_values_do_not_create_a_folder_called_nothing(self, monkeypatch):
        """A .bat that writes `set PROJECTS_DIR=` leaves an empty string, and one that quotes
        the value leaves the quotes in. Both would otherwise become a folder name."""
        monkeypatch.setenv("PROJECTS_DIR", "   ")
        assert project_paths._configured_projects_dir() == project_paths.BASE_DIR / "projects"
        monkeypatch.setenv("PROJECTS_DIR", '"verify-projects"')
        assert (project_paths._configured_projects_dir()
                == project_paths.BASE_DIR / "verify-projects")

    def test_every_cross_project_file_moves_with_it(self, monkeypatch, tmp_path):
        """One setting, one notebook. A per-file override would leave the verification log in
        one instance's folder and the findings it points at in the other's."""
        import accounts
        import campaigns
        import clusters
        import ecosystem
        import retests
        import scratchpad
        import verifications
        from agent import store

        monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path)
        for path in (accounts._path(), campaigns._path(), clusters._path(), retests._path(),
                     scratchpad._path(), verifications._path(), ecosystem._index_path(),
                     project_paths._registry_path(), store._last_opened_path()):
            assert Path(path).parent == tmp_path, path

    def test_the_launcher_points_the_second_instance_at_its_own_notebook(self):
        env: dict[str, str] = {}
        start_verifier.apply_environment(env)
        assert env["PROJECTS_DIR"] == "verify-projects"
        assert env["SERVER_PORT"] == "8001"
        assert env["SERVER_URL"].endswith(":8001")
        # Nobody is watching a verification run; the audience is a polling worker.
        assert env["AGENT_OPEN_MODULE_TABS"] == "false"

    def test_importing_the_launcher_does_not_repoint_the_running_harness(self):
        """The environment is applied in `main()`, not at import. A test or a tool that merely
        imports this module must not silently move every path in the process."""
        source = Path(start_verifier.__file__).read_text(encoding="utf-8")
        body = source.split("def apply_environment", 1)[0]
        assert "os.environ.update" not in body
        assert "os.environ[" not in body

    def test_the_master_is_untouched_by_any_of_this(self, monkeypatch):
        """The everyday harness is the one thing that must not change. With no PROJECTS_DIR
        set — which is every existing shortcut, .bat and terminal — it reads `app/projects`
        and answers on :8000, exactly as before."""
        import importlib

        monkeypatch.delenv("PROJECTS_DIR", raising=False)
        monkeypatch.delenv("SERVER_PORT", raising=False)
        monkeypatch.delenv("QA_NOTIFY_TITLE", raising=False)
        assert project_paths._configured_projects_dir().name == "projects"
        fresh = importlib.reload(config)
        try:
            assert fresh.SERVER_PORT == 8000
            assert fresh.SERVER_URL == "http://localhost:8000"
        finally:
            importlib.reload(config)

    def test_a_notification_says_which_instance_it_came_from(self, monkeypatch):
        """Both harnesses can be up at once, and a 2am "a job needs a human" that does not say
        which one costs a round trip to find out — which is most of what it was for."""
        import notify

        monkeypatch.delenv("QA_NOTIFY_TITLE", raising=False)
        assert notify._default_title() == "QA Tester AI"
        monkeypatch.setenv("QA_NOTIFY_TITLE", start_verifier.ENVIRONMENT["QA_NOTIFY_TITLE"])
        assert notify._default_title() == "QA Verifier"


# -- the refusal ----------------------------------------------------------------------------
class TestMasterLockRefusal:
    def test_a_master_that_is_not_running_is_fine(self):
        """The normal case on an unattended machine is that only the verifier is up. A launcher
        that needed the other harness would make the bridge depend on somebody opening a page."""
        assert start_verifier.refusal(None) is None

    def test_an_idle_master_is_fine(self):
        assert start_verifier.refusal({"device_locks": {}}) is None
        assert start_verifier.refusal({"sessions": []}) is None

    def test_any_held_lock_refuses_and_names_the_holder(self):
        """Two agents on one target interleave their taps, and each one's findings then
        describe a screen the other just changed. That is a false defect, which is the most
        expensive thing this harness can produce."""
        stop = start_verifier.refusal({"device_locks": {
            "android:R5CR12GJAJY": {"package": "com.patient.android", "slug": "checkout",
                                    "since": "2026-09-03T10:02:00Z"}}})
        assert stop is not None
        assert "R5CR12GJAJY" in stop
        assert "com.patient.android/checkout" in stop
        assert "10:02" in stop

    def test_a_lock_on_a_different_platform_still_refuses(self, ):
        """Deliberately not clever. The verifier does not yet know which device the next job
        will want, and "the master is only holding the iPad" is exactly the reasoning that ends
        with two agents on one phone an hour later."""
        assert start_verifier.refusal({"device_locks": {"web:clinic.example.com": {}}}) is not None

    def test_the_status_probe_survives_a_dead_port(self):
        assert start_verifier.master_status("http://127.0.0.1:9", timeout=0.5) is None


# -- the fleet it seeds ------------------------------------------------------------------------
class TestSeed:
    @pytest.fixture
    def source(self, tmp_path) -> Path:
        """A stand-in for the QA Master's notebook, with the two iOS projects in it."""
        root = tmp_path / "projects"
        for folder, meta in (
            ("com.metaestetics.mobile_clientapp",
             {"package": "com.metaestetics.mobile_clientapp", "platform": "android",
              "device_serial": "R5CR12GJAJY", "blackcode_project_id": 5}),
            ("Metaesthetics_iphone_Test",
             {"package": "Metaesthetics iphone Test", "platform": "ios"}),
            ("ipad_Test",
             {"package": "ipad Test", "platform": "ios",
              "device_serial": "00008030-00061D0E22A3C02E", "blackcode_project_id": 6}),
        ):
            (root / folder).mkdir(parents=True)
            (root / folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return root

    def test_the_shipped_seed_names_the_three_roles_the_gate_uses(self):
        spec = json.loads(start_verifier.SEED_PATH.read_text(encoding="utf-8"))
        roles = [m["role"] for m in spec["members"]]
        assert roles == ["patient-android", "patient-ios", "doctor-ipad"]

    def test_the_ios_roles_take_their_package_from_the_real_project(self, source):
        members = start_verifier.seed_members(
            json.loads(start_verifier.SEED_PATH.read_text(encoding="utf-8")), source)
        by_role = {m["role"]: m for m in members}
        assert by_role["patient-ios"]["package"] == "Metaesthetics iphone Test"
        assert by_role["doctor-ipad"]["package"] == "ipad Test"
        assert by_role["doctor-ipad"]["platform"] == "ios"

    def test_no_ios_role_inherits_the_real_devices_pin(self, source):
        """A verifier project pinned to the real iPad would send a verification run at the
        device the QA Master is testing on, which is the collision the launcher refuses over."""
        members = start_verifier.seed_members(
            json.loads(start_verifier.SEED_PATH.read_text(encoding="utf-8")), source)
        for member in members:
            if member["role"] != "patient-android":
                assert "device_serial" not in member

    def test_the_android_role_is_pinned_to_the_emulator_and_keeps_the_project_id(self, source):
        members = start_verifier.seed_members(
            json.loads(start_verifier.SEED_PATH.read_text(encoding="utf-8")), source)
        android = next(m for m in members if m["role"] == "patient-android")
        assert android["device_serial"] == "emulator-5554"
        assert android["blackcode_project_id"] == 5

    def test_a_missing_source_project_drops_the_member_rather_than_creating_a_shell(self,
                                                                                    tmp_path):
        """A project whose package is "" is one the manager can list, try to run, and fail on
        with no useful error."""
        members = start_verifier.seed_members(
            json.loads(start_verifier.SEED_PATH.read_text(encoding="utf-8")),
            tmp_path / "nothing-here")
        assert [m["role"] for m in members] == ["patient-android"]

    def test_seeding_happens_once(self, tmp_path, monkeypatch, source):
        """Re-applying the seed would quietly undo a pin or a role corrected by hand."""
        import ecosystem

        monkeypatch.setattr(project_paths, "DEFAULT_PROJECTS_DIR", tmp_path / "verify")
        made = start_verifier.ensure_fleet("metaesthetics-verify", source_dir=source)
        assert {m["role"] for m in made} == {"patient-android", "patient-ios", "doctor-ipad"}
        assert ecosystem.supervisor("metaesthetics-verify") == "metaesthetics-verify"

        ecosystem.tag("com.metaestetics.mobile_clientapp", "metaesthetics-verify", "renamed")
        again = start_verifier.ensure_fleet("metaesthetics-verify", source_dir=source)
        assert "renamed" in {m["role"] for m in again}


# -- the emulator ------------------------------------------------------------------------------
class TestEmulatorParsing:
    def test_the_daemon_chatter_and_the_header_are_not_devices(self):
        """Every one of these lines has at some point been mistaken for a device by a naive
        split, and each mistake reads downstream as a phone that will not answer."""
        out = ("* daemon not running; starting now at tcp:5037\n"
               "* daemon started successfully\n"
               "List of devices attached\n"
               "emulator-5554\tdevice\n"
               "R5CR12GJAJY\tdevice\n"
               "\n")
        assert emulator.parse_devices(out) == [("emulator-5554", "device"),
                                               ("R5CR12GJAJY", "device")]

    def test_an_emulator_is_recognised_by_its_serial(self):
        assert emulator.is_emulator("emulator-5554")
        assert emulator.is_emulator("emulator-5556")
        assert not emulator.is_emulator("R5CR12GJAJY")
        assert not emulator.is_emulator("00008030-00061D0E22A3C02E")
        assert not emulator.is_emulator("")

    def test_only_a_device_state_counts_as_running(self, monkeypatch):
        """`offline` is what an AVD reports for the first several seconds of a boot. Treating
        it as up is how an install lands on a device that is not there yet."""
        monkeypatch.setattr(emulator, "_adb",
                            lambda *a, **k: "List of devices attached\nemulator-5554\toffline\n")
        assert emulator.is_running() is None
        monkeypatch.setattr(emulator, "_adb",
                            lambda *a, **k: "List of devices attached\nemulator-5554\tdevice\n")
        assert emulator.is_running() == "emulator-5554"

    def test_a_plugged_in_phone_is_not_reported_as_the_emulator(self, monkeypatch):
        monkeypatch.setattr(emulator, "_adb",
                            lambda *a, **k: "List of devices attached\nR5CR12GJAJY\tdevice\n")
        assert emulator.is_running() is None

    def test_it_never_shells_out_to_a_bare_adb(self, monkeypatch):
        """adb is not on PATH on this machine. A bare "adb" works in a developer's shell and
        fails inside the server, which is the worst of both."""
        seen = []

        def fake_run(args, **kwargs):
            seen.append(args)
            return subprocess.CompletedProcess(args, 0, "List of devices attached\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        emulator.is_running()
        assert seen and seen[0][0] == config.ADB_PATH

    def test_the_headless_flags_are_the_ones_that_make_it_unattended(self, monkeypatch):
        launched = []
        monkeypatch.setattr(emulator, "_emulator_path", lambda: __file__)
        monkeypatch.setattr(subprocess, "Popen",
                            lambda args, **kwargs: launched.append((args, kwargs)))
        emulator.start_headless("Medium_Phone_API_36.1")
        args, kwargs = launched[0]
        assert args[1:] == ["-avd", "Medium_Phone_API_36.1", "-no-window", "-no-audio",
                            "-no-boot-anim"]
        # Detached, or a Ctrl-C meant for the server takes the device down mid-run.
        assert kwargs.get("creationflags") or kwargs.get("start_new_session")

    def test_a_missing_binary_says_where_to_point_it(self, monkeypatch, tmp_path):
        from adb_device import DeviceError

        monkeypatch.setattr(emulator, "_emulator_path", lambda: str(tmp_path / "nope.exe"))
        monkeypatch.setattr(subprocess, "Popen",
                            lambda *a, **k: pytest.fail("must not launch a missing binary"))
        with pytest.raises(DeviceError) as exc:
            emulator.start_headless()
        assert "EMULATOR_PATH" in str(exc.value)

    def test_waiting_stops_at_boot_completed_and_not_before(self, monkeypatch):
        replies = iter(["", "0", "1"])
        monkeypatch.setattr(emulator, "is_running", lambda: "emulator-5554")
        monkeypatch.setattr(emulator, "_adb", lambda *a, **k: next(replies))
        monkeypatch.setattr(emulator.time, "sleep", lambda _s: None)
        assert emulator.wait_until_booted(timeout=30) == "emulator-5554"

    def test_ensure_does_not_launch_a_second_avd_over_a_running_one(self, monkeypatch):
        """A second `emulator -avd` on the same AVD fails on a lock file rather than booting a
        second device, so the check is not a nicety."""
        monkeypatch.setattr(emulator, "is_running", lambda: "emulator-5554")
        monkeypatch.setattr(emulator, "start_headless",
                            lambda *a, **k: pytest.fail("one is already up"))
        monkeypatch.setattr(emulator, "wait_until_booted",
                            lambda timeout=0, serial=None: serial or "emulator-5554")
        assert emulator.ensure_running() == "emulator-5554"


# -- the emulator, as the rest of the harness sees it -------------------------------------------
class TestAndroidStatusWithAnEmulator:
    @pytest.fixture(autouse=True)
    def no_getprop(self, monkeypatch):
        """`describe_serial` shells out twice per device; the question here is the verdict."""
        import adb_device

        monkeypatch.setattr(adb_device, "describe_serial",
                            lambda serial: {"serial": serial, "label": serial})

    def test_an_emulator_is_a_ready_android_target(self, monkeypatch):
        """The whole point. Every adapter here reaches an AVD through the same `-s <serial>` a
        cable gives, so refusing a run because no phone is plugged in refuses a run on a device
        that is already up."""
        import adb_device

        monkeypatch.setattr(adb_device, "list_serials", lambda: ["emulator-5554"])
        row = stacks.status("android")
        assert row["ready"] is True
        assert row["devices"][0]["emulator"] is True
        assert "emulator-5554" in row["detail"]

    def test_an_emulator_only_fleet_says_what_it_cannot_vouch_for(self, monkeypatch):
        import adb_device

        monkeypatch.setattr(adb_device, "list_serials", lambda: ["emulator-5554"])
        detail = stacks.status("android")["detail"]
        assert "camera" in detail and "biometrics" in detail

    def test_a_real_phone_is_not_labelled_an_emulator(self, monkeypatch):
        import adb_device

        monkeypatch.setattr(adb_device, "list_serials", lambda: ["R5CR12GJAJY"])
        row = stacks.status("android")
        assert row["ready"] is True
        assert row["devices"][0]["emulator"] is False
        assert "[emulator]" not in row["detail"]

    def test_with_nothing_attached_the_fix_offers_the_emulator(self, monkeypatch):
        """The fix hint is the only place this capability is advertised — nothing starts an
        AVD automatically, because a two-minute boot as a side effect of opening a dashboard is
        how a feature gets turned off."""
        import adb_device

        monkeypatch.setattr(adb_device, "list_serials", lambda: [])
        row = stacks.status("android")
        assert row["ready"] is False
        assert "emulator.py --ensure" in row["fix"]
        assert config.ANDROID_AVD_NAME in row["fix"]
