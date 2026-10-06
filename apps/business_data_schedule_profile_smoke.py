#!/usr/bin/env python3
"""Fixed-profile, exact target, process interruption and rollback tests offline."""
from __future__ import annotations
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.business_data_maintenance_pause_smoke import FakeSystemd
from apps.business_data_maintenance import POLICY_FILENAME, POLICY_SCHEMA_VERSION, maintenance_prepare, maintenance_restore
from apps import business_data_maintenance as master_cli
from apps import business_data_maintenance_pause as pause_cli
from apps.hosted_runtime_deploy_barrier import reconcile
from packages.application import business_data_maintenance_pause as pause
from packages.application import business_data_schedule_profile as profile
from packages.application import business_data_schedule_transition as transition
from packages.application.business_data_procedure_admission import initialize_admission
from packages.application.business_data_write_barrier import barrier_status, release_barrier, BusinessDataWriteBarrierError

SHA = "a" * 40


class FileSystemd(FakeSystemd):
    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        directory.mkdir()
        self.loaded = []
        for unit, value in self.states.items():
            fragment = directory / unit
            source = ROOT / "artifacts/registry_upload_http_entrypoint/systemd" / unit
            fragment.write_bytes(source.read_bytes() if source.exists() else b"[Unit]\nDescription=offline fixture\n")
            value["properties"].update(FragmentPath=str(fragment), DropInPaths="")

    def unit_state(self, unit):
        value = super().unit_state(unit)
        files = [value["properties"]["FragmentPath"]]
        if unit == profile.WAREHOUSE_TIMER:
            value["properties"]["DropInPaths"] = shlex.join(self.loaded)
            files += self.loaded
        content = []
        for name in files:
            path = Path(name)
            if not path.exists():
                raise RuntimeError("systemd unit content cannot be safely proven: " + unit)
            content.append({"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        value["properties"]["UnitContentDigest"] = profile.fingerprint(content)
        return value

    def _run(self, args):
        if args[0] == "show":
            value = self.states[args[1]]
            payload = {"UnitFileState": value["is_enabled"], "ActiveState": value["is_active"],
                       "FragmentPath": value["properties"]["FragmentPath"],
                       "DropInPaths": shlex.join(self.loaded) if args[1] == profile.WAREHOUSE_TIMER else ""}
            return subprocess.CompletedProcess(args, 0, "\n".join(key + "=" + item for key, item in payload.items()))
        if args == ["daemon-reload"]:
            self.calls.append(tuple(args))
            path = self.directory / profile.DROPIN_RELATIVE
            self.loaded = [str(path)] if path.exists() else []
            return subprocess.CompletedProcess(args, 0)
        return super()._run(args)


@contextmanager
def fixture(*, existing=False, master=True):
    with tempfile.TemporaryDirectory() as temporary, patch.object(pause, "_cron_entries", return_value=[]):
        root = Path(temporary)
        runtime = root / "runtime"; runtime.mkdir()
        initialize_admission(runtime)
        policy = {"schema_version": POLICY_SCHEMA_VERSION, "master_desired": master,
                  "processes": {key: {"desired": True} for key in
                                ("warehouse_functional", "wb_finance_weekly", "vitrina_refresh")}}
        (runtime / POLICY_FILENAME).write_text(json.dumps(policy)); (runtime / POLICY_FILENAME).chmod(0o600)
        systemd = FileSystemd(root / "units")
        if existing:
            path = systemd.directory / profile.DROPIN_RELATIVE
            path.parent.mkdir(); path.write_bytes(profile.DROPIN_BYTES); path.chmod(0o644)
            systemd._run(["daemon-reload"])
        def activity():
            return {"contract_name": "business_data_maintenance_activity_v1", "admission_ready": True,
                    "complete": True, "runtime_dir": str(runtime.resolve()), "jobs": [],
                    "feature_intent": {"web_vitrina": {"schedules": [{"enabled": True, "time": "10:00"}]}}}
        proc = root / "proc"; proc.mkdir()
        options = dict(systemd=systemd, activity_reader=activity, proc_root=proc)
        pause.pause(runtime, window_id="profile-window-001", actor="test", reason="offline fixture", **options)
        yield runtime, systemd, options


def reviewed(runtime, systemd, options):
    return transition.preview(runtime, operation_id="profile-operation-001", window_id="profile-window-001",
                              deployed_sha=SHA, unit_directory=systemd.directory, **options)


def apply(runtime, systemd, options, plan, **extra):
    return transition.apply(runtime, reviewed_plan=plan, expected_fingerprint=plan["fingerprint"], deployed_sha=SHA,
                            actor="test", reason="offline target", unit_directory=systemd.directory, **options, **extra)


READY = {"ready": True, "contract": "offline_fixture_only", "blockers": []}


class ScheduleProfileSmoke(unittest.TestCase):
    def test_legacy_noop_and_exact_fixed_slots(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            self.assertIsNone(profile.load_selector(runtime))
            profile.require_legacy_master(runtime)
            self.assertEqual(list(runtime.iterdir()), [])
            raw = {"schedules": [{"enabled": False}]}
            self.assertIs(profile.projected_schedule(runtime, raw)["raw_feature_intent"], raw)
            self.assertEqual(list(runtime.iterdir()), [])
        self.assertEqual(profile.PROFILE["slots"], [f"{hour:02d}:00" for hour in range(0, 24, 3)])
        self.assertIn(b"OnCalendar=\nOnCalendar=*-*-* 00,03,06,09,12,15,18,21:00:00 Asia/Yekaterinburg", profile.DROPIN_BYTES)
        self.assertTrue(profile.PROFILE["fbs_collect_every_cycle"])
        self.assertEqual(profile.PROFILE["fbs_max_age_seconds"], 9 * 3600)

    def test_dormant_readiness_and_raw_disabled_phase_block_before_effects(self):
        with fixture() as (runtime, systemd, options):
            plan = reviewed(runtime, systemd, options)
            self.assertFalse(plan["activation_readiness"]["ready"])
            calls = list(systemd.calls)
            files = {p: p.read_bytes() for p in runtime.iterdir() if p.is_file()}
            with self.assertRaisesRegex(RuntimeError, "dependencies"):
                apply(runtime, systemd, options, plan)
            self.assertEqual(calls, systemd.calls)
            self.assertEqual(files, {p: p.read_bytes() for p in runtime.iterdir() if p.is_file()})
            self.assertFalse((runtime / profile.TRANSITIONS_DIRECTORY).exists())
            policy = json.loads((runtime / POLICY_FILENAME).read_text())
            policy["processes"]["warehouse_functional"]["desired"] = False
            (runtime / POLICY_FILENAME).write_text(json.dumps(policy))
            with patch.object(profile, "activation_dependencies", return_value=READY):
                readiness = profile.activation_readiness(runtime)
                self.assertFalse(readiness["ready"])
                self.assertIn("required_phase_owner_disabled_or_unknown:warehouse_functional", readiness["blockers"])
                with self.assertRaisesRegex(RuntimeError, "mandatory"):
                    profile.effective_core(runtime)

    def test_unknown_overrides_and_plain_resume_drift_refuse(self):
        with fixture() as (runtime, systemd, options), patch.object(profile, "activation_dependencies", return_value=READY):
            path = systemd.directory / profile.DROPIN_RELATIVE
            path.parent.mkdir(); path.write_bytes(b"[Timer]\nOnCalendar=daily\n")
            systemd._run(["daemon-reload"])
            calls = list(systemd.calls)
            with self.assertRaises(RuntimeError):
                reviewed(runtime, systemd, options)
            with self.assertRaisesRegex(RuntimeError, "configuration changed"):
                pause.resume(runtime, window_id="profile-window-001", actor="test", reason="no bypass", **options)
            self.assertEqual(calls, systemd.calls)
            self.assertTrue(barrier_status(runtime)["active"])

    def test_every_target_step_interruption_restart_identity_no_resend(self):
        points = ["prepared", "dropin_written", "dropin_installed", "reloaded", "selector_written", "target_verified", "committed", "barrier_released"]
        points += ["timer:" + timer for timer in pause.TIMERS]
        for point in points:
            with self.subTest(point=point), fixture() as (runtime, systemd, options), \
                    patch.object(profile, "activation_dependencies", return_value=READY):
                plan = reviewed(runtime, systemd, options)
                baseline = deepcopy(pause.load_state(runtime)["baseline"])
                def fault(here):
                    if here == point:
                        raise RuntimeError("offline interruption")
                with self.assertRaisesRegex(RuntimeError, "offline interruption"):
                    apply(runtime, systemd, options, plan, _fault=fault)
                # Re-read durable state exactly as a replacement CLI process.
                state = profile.load_transition(runtime, plan["operation_id"])
                self.assertEqual(state["plan"], plan)
                code = ("import json,sys;from pathlib import Path;"
                        "from packages.application.business_data_schedule_profile import load_transition;"
                        "print(json.dumps(load_transition(Path(sys.argv[1]),sys.argv[2])))")
                replaced_process = json.loads(subprocess.check_output(
                    [sys.executable, "-c", code, str(runtime), plan["operation_id"]], cwd=ROOT))
                self.assertEqual(replaced_process, state)
                self.assertEqual(pause.load_state(runtime)["baseline"], baseline)
                if point != "barrier_released":
                    self.assertTrue(barrier_status(runtime)["active"])
                bad = deepcopy(plan); bad["operation_id"] = "other-operation-001"
                bad.pop("fingerprint"); bad["fingerprint"] = profile.fingerprint(bad)
                with self.assertRaises(RuntimeError):
                    apply(runtime, systemd, options, bad)
                receipt = apply(runtime, systemd, options, plan)
                self.assertTrue(receipt["exact_target_state_restored"])
                self.assertFalse(receipt["exact_prior_state_restored"])
                self.assertFalse(barrier_status(runtime)["active"])
                self.assertEqual(pause.load_state(runtime)["baseline"], baseline)
                before_calls = list(systemd.calls)
                self.assertEqual(apply(runtime, systemd, options, plan), receipt)
                self.assertEqual(before_calls, systemd.calls)
                self.assertEqual(systemd.calls.count(("daemon-reload",)), 1)
                for timer, pair in plan["target_timer_states"].items():
                    state = systemd.unit_state(timer)
                    self.assertEqual([state["is_enabled"], state["is_active"]], pair)
                with self.assertRaisesRegex(RuntimeError, "configuration changed"):
                    pause.resume(runtime, window_id="profile-window-001", actor="test", reason="no plain bypass", **options)
                with self.assertRaises(BusinessDataWriteBarrierError):
                    release_barrier(runtime, window_id=plan["window_id"], plan_fingerprint=plan["baseline_fingerprint"],
                                    actor="test", reason="no false prior", restore_readback=receipt)

    def test_partial_pre_reload_ordinary_resume_and_both_release_clis_cannot_escape(self):
        for point in ("prepared", "dropin_written", "dropin_installed"):
            for recover in ("target", "rollback"):
                with self.subTest(point=point, recover=recover), fixture() as (runtime, systemd, options), \
                        patch.object(profile, "activation_dependencies", return_value=READY):
                    plan = reviewed(runtime, systemd, options)
                    def fault(here):
                        if here == point:
                            raise RuntimeError("pre reload interruption")
                    with self.assertRaisesRegex(RuntimeError, "pre reload interruption"):
                        apply(runtime, systemd, options, plan, _fault=fault)
                    # Loaded configuration still equals the original baseline.
                    for unit, old in plan["baseline"]["units"].items():
                        self.assertEqual(pause._unit_fingerprint(systemd.unit_state(unit)), pause._unit_fingerprint(old))
                    before = {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()}
                    calls = list(systemd.calls)
                    with self.assertRaisesRegex(RuntimeError, "same-operation"):
                        pause.resume(runtime, window_id=plan["window_id"], actor="test", reason="ordinary resume", **options)
                    exact_prior = {"schema_version": pause.SCHEMA, "status": "restored", "exact_prior_state_restored": True,
                                   "baseline_fingerprint": plan["baseline_fingerprint"], "units": plan["baseline"]["units"]}
                    with self.assertRaisesRegex(RuntimeError, "same-operation"):
                        release_barrier(runtime, window_id=plan["window_id"], plan_fingerprint=plan["baseline_fingerprint"],
                                        actor="test", reason="ordinary direct release", restore_readback=exact_prior)
                    with patch.object(pause_cli, "_read_env_file", return_value={}), \
                         patch.object(pause_cli, "_build_web_auth_cookie", return_value="offline"), \
                         patch.object(pause_cli, "SystemdClient", return_value=systemd), \
                         patch.object(pause_cli, "RuntimeScheduleClient"), patch("builtins.print"):
                        result = pause_cli.main(["resume", "--runtime-dir", str(runtime), "--env-file", "offline.env",
                                                 "--window-id", plan["window_id"], "--actor", "test", "--reason", "CLI resume"])
                        self.assertEqual(result, 1)
                    with patch.object(master_cli, "_load_json_object", return_value={
                            "phase": "restored", "exact_prior_state_restored": True, "restore_readback": exact_prior}):
                        with self.assertRaisesRegex(RuntimeError, "same-operation"):
                            master_cli.main(["barrier-release", "--runtime-dir", str(runtime), "--env-file", "offline.env",
                                             "--window-id", plan["window_id"], "--plan-fingerprint", plan["baseline_fingerprint"],
                                             "--actor", "test", "--reason", "CLI release"])
                    self.assertEqual(calls, systemd.calls)
                    self.assertEqual(before, {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()})
                    self.assertTrue(barrier_status(runtime)["active"])
                    if recover == "target":
                        self.assertTrue(apply(runtime, systemd, options, plan)["exact_target_state_restored"])
                    else:
                        transition.rollback(runtime, operation_id=plan["operation_id"], actor="test", reason="controlled rollback", **options)
                        self.assertIsNone(transition._image(Path(plan["dropin_path"])))
                        self.assertTrue(pause.resume(runtime, window_id=plan["window_id"], actor="test", reason="exact old", **options)["exact_prior_state_restored"])
                    self.assertFalse(barrier_status(runtime)["active"])

    def test_restored_files_before_durable_rollback_cannot_be_ordinarily_released(self):
        with fixture() as (runtime, systemd, options), patch.object(profile, "activation_dependencies", return_value=READY):
            plan = reviewed(runtime, systemd, options)
            def stop(here):
                if here == "target_verified":
                    raise RuntimeError("before commit")
            with self.assertRaises(RuntimeError):
                apply(runtime, systemd, options, plan, _fault=stop)
            def interrupted(here):
                if here == "rollback_reloaded":
                    raise RuntimeError("before durable rolled back")
            with self.assertRaisesRegex(RuntimeError, "before durable"):
                transition.rollback(runtime, operation_id=plan["operation_id"], actor="test", reason="rollback", **options, _fault=interrupted)
            self.assertEqual(profile.load_transition(runtime, plan["operation_id"])["phase"], "rolling_back")
            before = {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()}
            calls = list(systemd.calls)
            with self.assertRaisesRegex(RuntimeError, "same-operation"):
                pause.resume(runtime, window_id=plan["window_id"], actor="test", reason="premature", **options)
            with self.assertRaisesRegex(RuntimeError, "same-operation"):
                release_barrier(runtime, window_id=plan["window_id"], plan_fingerprint=plan["baseline_fingerprint"],
                                actor="test", reason="premature", restore_readback={"status": "restored", "exact_prior_state_restored": True})
            self.assertEqual(calls, systemd.calls)
            self.assertEqual(before, {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()})
            transition.rollback(runtime, operation_id=plan["operation_id"], actor="test", reason="same operation", **options)
            self.assertTrue(pause.resume(runtime, window_id=plan["window_id"], actor="test", reason="exact old", **options)["exact_prior_state_restored"])

    def test_first_prepared_record_and_competing_release_share_barrier_authority(self):
        with fixture() as (runtime, systemd, options), patch.object(profile, "activation_dependencies", return_value=READY):
            plan = reviewed(runtime, systemd, options)
            code = """import sys
from pathlib import Path
from packages.application import business_data_schedule_profile as profile
from packages.application.business_data_write_barrier import release_barrier
original = profile.assert_no_partial_transition
checks = 0
def observed(runtime):
    global checks
    original(runtime)
    checks += 1
    if checks == 1:
        print('prechecked', flush=True)
profile.assert_no_partial_transition = observed
try:
    release_barrier(Path(sys.argv[1]), window_id=sys.argv[2], plan_fingerprint=sys.argv[3],
                    actor='offline-child', reason='competing ordinary release',
                    restore_readback={'status':'restored','exact_prior_state_restored':True})
except RuntimeError as exc:
    print('refused:' + str(exc), flush=True)
else:
    raise AssertionError('partial transition escaped')
"""
            child = None
            original_save = transition._save
            def save_with_contender(runtime, state, event):
                nonlocal child
                if event == "prepared":
                    child = subprocess.Popen([sys.executable, "-c", code, str(runtime),
                                              plan["window_id"], plan["baseline_fingerprint"]], cwd=ROOT,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    self.assertEqual(child.stdout.readline().strip(), "prechecked")
                    # First check saw no intent. Actual release is blocked on
                    # the barrier lock owned around this first journal write.
                    self.assertIsNone(child.poll())
                original_save(runtime, state, event)
            def stopped(here):
                if here == "prepared":
                    raise RuntimeError("prepared interruption")
            try:
                with patch.object(transition, "_save", side_effect=save_with_contender):
                    with self.assertRaisesRegex(RuntimeError, "prepared interruption"):
                        apply(runtime, systemd, options, plan, _fault=stopped)
                output, error = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, error)
                self.assertIn("refused:schedule transition requires exact same-operation recovery", output)
            finally:
                if child is not None and child.poll() is None:
                    child.kill(); child.communicate()
            self.assertTrue(barrier_status(runtime)["active"])
            self.assertEqual(profile.load_transition(runtime, plan["operation_id"])["phase"], "prepared")
            self.assertTrue(apply(runtime, systemd, options, plan)["exact_target_state_restored"])

    def test_unknown_transition_inventory_blocks_ordinary_authority_without_mutation(self):
        with fixture() as (runtime, systemd, options):
            root = runtime / profile.TRANSITIONS_DIRECTORY
            root.mkdir(mode=0o700)
            unknown = root / "unknown-record.txt"
            unknown.write_text("unproven inventory")
            before = {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()}
            calls = list(systemd.calls)
            state = pause.load_state(runtime)
            with self.assertRaisesRegex(RuntimeError, "inventory"):
                pause.resume(runtime, window_id=state["window_id"], actor="test", reason="unknown", **options)
            with self.assertRaisesRegex(RuntimeError, "inventory"):
                release_barrier(runtime, window_id=state["window_id"], plan_fingerprint=state["baseline_fingerprint"],
                                actor="test", reason="unknown", restore_readback={"status": "restored", "exact_prior_state_restored": True})
            self.assertEqual(calls, systemd.calls)
            self.assertEqual(before, {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()})
            self.assertTrue(barrier_status(runtime)["active"])
            unknown.unlink()
            self.assertTrue(pause.resume(runtime, window_id=state["window_id"], actor="test", reason="legacy exact", **options)["exact_prior_state_restored"])

    def test_ambiguous_systemd_submit_reads_back_without_resend(self):
        for action in ("enable", "start"):
            with self.subTest(action=action), fixture() as (runtime, systemd, options), \
                    patch.object(profile, "activation_dependencies", return_value=READY):
                plan = reviewed(runtime, systemd, options)
                systemd.fail_once = (action, profile.WAREHOUSE_TIMER)
                with self.assertRaisesRegex(RuntimeError, "ambiguous restore"):
                    apply(runtime, systemd, options, plan)
                self.assertTrue(barrier_status(runtime)["active"])
                apply(runtime, systemd, options, plan)
                self.assertEqual(systemd.calls.count((action, profile.WAREHOUSE_TIMER)), 1)

    def test_partial_deploy_and_master_block_and_committed_drift_keeps_barrier(self):
        with fixture() as (runtime, systemd, options), patch.object(profile, "activation_dependencies", return_value=READY):
            plan = reviewed(runtime, systemd, options)
            def stop(here):
                if here == "prepared":
                    raise RuntimeError("offline interruption")
            with self.assertRaises(RuntimeError):
                apply(runtime, systemd, options, plan, _fault=stop)
            with patch("apps.hosted_runtime_deploy_barrier.subprocess.run") as mutate:
                with self.assertRaisesRegex(RuntimeError, "same-operation"):
                    reconcile(runtime_dir=runtime, enable=[profile.WAREHOUSE_TIMER], restart=[profile.WAREHOUSE_TIMER])
                mutate.assert_not_called()
            calls = list(systemd.calls)
            with self.assertRaisesRegex(RuntimeError, "same-operation"):
                maintenance_prepare(runtime, systemd=systemd, schedules=None)
            self.assertEqual(calls, systemd.calls)
            def committed(here):
                if here == "committed":
                    raise RuntimeError("offline interruption")
            with self.assertRaises(RuntimeError):
                apply(runtime, systemd, options, plan, _fault=committed)
            foreign = systemd.directory / pause.TIMERS[0]
            foreign.write_bytes(foreign.read_bytes() + b"# foreign drift\n")
            with self.assertRaisesRegex(RuntimeError, "configuration drift"):
                apply(runtime, systemd, options, plan)
            self.assertTrue(barrier_status(runtime)["active"])
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                transition.rollback(runtime, operation_id=plan["operation_id"], actor="test", reason="refuse", **options)

    def test_rollback_existing_and_absent_dropin_each_step(self):
        points = ["rollback_dropin", "rollback_selector", "rollback_reloaded"]
        points += ["rollback_timer:" + timer for timer in pause.TIMERS]
        for existing in (False, True):
            for point in points:
                with self.subTest(existing=existing, point=point), fixture(existing=existing) as (runtime, systemd, options), \
                        patch.object(profile, "activation_dependencies", return_value=READY):
                    plan = reviewed(runtime, systemd, options)
                    baseline = deepcopy(pause.load_state(runtime)["baseline"])
                    def stop_before_commit(here):
                        if here == "target_verified":
                            raise RuntimeError("before commit")
                    with self.assertRaisesRegex(RuntimeError, "before commit"):
                        apply(runtime, systemd, options, plan, _fault=stop_before_commit)
                    def stop_rollback(here):
                        if here == point:
                            raise RuntimeError("rollback interrupted")
                    with self.assertRaisesRegex(RuntimeError, "rollback interrupted"):
                        transition.rollback(runtime, operation_id=plan["operation_id"], actor="test", reason="offline", **options, _fault=stop_rollback)
                    self.assertTrue(barrier_status(runtime)["active"])
                    result = transition.rollback(runtime, operation_id=plan["operation_id"], actor="test", reason="offline", **options)
                    self.assertTrue(result["original_configuration_restored"])
                    self.assertEqual(pause.load_state(runtime)["baseline"], baseline)
                    self.assertIsNone(profile.load_selector(runtime))
                    self.assertEqual(transition._image(Path(plan["dropin_path"])), plan["dropin_before"])
                    self.assertTrue(pause.resume(runtime, window_id=plan["window_id"], actor="test", reason="exact old resume", **options)["exact_prior_state_restored"])

    def test_profile_deploy_projection_paused_guard_master_refusal_before_mutation(self):
        with fixture() as (runtime, systemd, options), patch.object(profile, "activation_dependencies", return_value=READY):
            plan = reviewed(runtime, systemd, options)
            apply(runtime, systemd, options, plan)
            policy_before = (runtime / POLICY_FILENAME).read_bytes()
            before_calls = list(systemd.calls)
            for call in (maintenance_prepare, maintenance_restore):
                with self.assertRaisesRegex(RuntimeError, "legacy master"):
                    call(runtime, systemd=systemd, schedules=None)
            self.assertEqual(before_calls, systemd.calls)
            self.assertEqual(policy_before, (runtime / POLICY_FILENAME).read_bytes())
            with patch.object(profile, "prove_preset") as prove, patch("apps.hosted_runtime_deploy_barrier.subprocess.run") as mutate:
                projected = reconcile(runtime_dir=runtime, enable=list(profile.RETIRED_TIMERS), restart=list(profile.RETIRED_TIMERS), mutate=False)
                self.assertEqual(projected["enabled_units"], [profile.WAREHOUSE_TIMER])
                self.assertEqual(projected["restarted_units"], [profile.WAREHOUSE_TIMER])
                self.assertEqual(set(projected["profile_disabled_timers"]), set(profile.RETIRED_TIMERS))
                mutate.assert_not_called(); prove.assert_called_once()
            pause.pause(runtime, window_id="profile-next-window", actor="test", reason="next held pause", **options)
            with patch.object(profile, "prove_preset"), \
                 patch("apps.hosted_runtime_deploy_barrier.unit_state", return_value={"UnitFileState": "disabled", "ActiveState": "inactive"}), \
                 patch("apps.hosted_runtime_deploy_barrier.subprocess.run") as mutate:
                projected = reconcile(runtime_dir=runtime, enable=list(profile.CORE_TIMERS), restart=list(profile.CORE_TIMERS), mutate=False)
                self.assertEqual(projected["enabled_units"], [])
                self.assertEqual(projected["restarted_units"], [])
                mutate.assert_not_called()

    def test_master_off_and_missing_selector_never_reopens_legacy(self):
        with fixture(master=False) as (runtime, systemd, options), patch.object(profile, "activation_dependencies", return_value=READY):
            plan = reviewed(runtime, systemd, options)
            self.assertEqual(plan["target_timer_states"][profile.WAREHOUSE_TIMER], ["disabled", "inactive"])
            apply(runtime, systemd, options, plan)
            for unit in profile.CORE_TIMERS:
                value = systemd.unit_state(unit)
                self.assertEqual((value["is_enabled"], value["is_active"]), ("disabled", "inactive"))
            (runtime / profile.SELECTOR_FILENAME).unlink()
            with patch("apps.hosted_runtime_deploy_barrier.subprocess.run") as mutate:
                with self.assertRaisesRegex(RuntimeError, "legacy fallback refused"):
                    reconcile(runtime_dir=runtime, enable=list(profile.RETIRED_TIMERS), restart=list(profile.RETIRED_TIMERS))
                mutate.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "legacy fallback refused"):
                profile.load_selector(runtime)


if __name__ == "__main__":
    unittest.main()
