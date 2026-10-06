#!/usr/bin/env python3
"""Offline real-process heavy ownership and Finance backup admission smoke."""
from __future__ import annotations

from contextlib import contextmanager
from contextlib import closing
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.finance_storage_backup_rotation_smoke import _fixture, _rotation, DEPLOYED_SHA
from packages.application.business_data_heavy_admission import (
    HeavyAdmissionBusy, HeavyAdmissionLease, LOCK_FILENAME, current_heavy_owner,
    heavy_admitted, heavy_admission_status,
)
from packages.application.business_data_procedure_admission import admission_idle, initialize_admission
from packages.application.finance_backup_admission import (
    STATE_FILENAME, BackupAdmissionStateError, backup_admission_priority,
)
from packages.application.finance_storage_backup_rotation import (
    FinanceBackupDeferred, FinanceStorageBackupRotationError, STRATEGY,
    TRANSACTION_CONTRACT, scheduled_rotation,
)
from packages.application.finance_storage_snapshot_retention import _atomic_write_json, _fingerprint


@contextmanager
def actor(runtime: Path):
    code = ("import sys; from pathlib import Path; "
            "from packages.application.business_data_heavy_admission import heavy_admitted\n"
            "with heavy_admitted(Path(sys.argv[1]), operation='cycle'):\n"
            " print('ready', flush=True)\n input()\n")
    process = subprocess.Popen([sys.executable, "-c", code, str(runtime)], cwd=ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        yield process
    finally:
        stdout, stderr = process.communicate("\n", timeout=10)
        if process.returncode:
            raise AssertionError(stderr or stdout)


def sign_write(path: Path, value: dict):
    value.pop("fingerprint", None)
    value["fingerprint"] = _fingerprint(value)
    _atomic_write_json(path, value)


def initial_backup(runtime: Path):
    raw, operational, _ = _fixture(runtime)
    initialize_admission(runtime)
    root = runtime / "backups" / "finance-storage-split-snapshots"
    root.mkdir(parents=True, mode=0o700)
    rotation = _rotation(runtime, root, minimum_replacement_interval_seconds=6 * 86400)
    plan = rotation.build_plan(force_replacement=True)
    result = rotation.apply(reviewed_plan=plan, expected_fingerprint=plan["fingerprint"],
                            approval_reference="offline-fixture-approved")
    return raw, operational, root, rotation, result


def make_due(root: Path):
    selector_path = root / "current.json"
    selector = json.loads(selector_path.read_text())
    manifest_path = root / "retained" / selector["backup_id"] / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["captured_at"] = (datetime.now(timezone.utc) - timedelta(days=6, hours=1)).isoformat()
    sign_write(manifest_path, manifest)
    selector["backup_manifest_fingerprint"] = manifest["fingerprint"]
    sign_write(selector_path, selector)


class HeavyAdmissionSmoke(unittest.TestCase):
    def test_status_read_does_not_provision_and_busy_releases_sh(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            self.assertFalse(heavy_admission_status(runtime)["ready"])
            self.assertEqual(list(runtime.iterdir()), [])
            initialize_admission(runtime)
            descriptor = os.open(runtime / LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(HeavyAdmissionBusy):
                    HeavyAdmissionLease(runtime, operation="backup")
                self.assertTrue(admission_idle(runtime)["idle"])
            finally:
                os.close(descriptor)

    def test_real_process_conflict_nested_owner_and_closed_context(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            initialize_admission(runtime)
            with actor(runtime):
                with self.assertRaises(HeavyAdmissionBusy):
                    HeavyAdmissionLease(runtime, operation="finance-backup")
            with heavy_admitted(runtime, operation="cycle") as root:
                with heavy_admitted(runtime, operation="warehouse-stage") as nested:
                    self.assertIs(root, nested)
                with self.assertRaises(HeavyAdmissionBusy):
                    HeavyAdmissionLease(runtime, operation="other-job", independent=True)
                self.assertIs(current_heavy_owner(runtime), root)
            self.assertIsNone(current_heavy_owner(runtime))
            with self.assertRaises(RuntimeError):
                with root.entered():
                    pass
            self.assertTrue(admission_idle(runtime)["idle"])

    def test_one_worker_transfer_and_copied_context_cannot_authorize_other_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            initialize_admission(runtime)
            lease = HeavyAdmissionLease(runtime, operation="cycle")
            entered, release = threading.Event(), threading.Event()
            errors = []
            def worker():
                try:
                    with lease.entered():
                        entered.set()
                        release.wait(5)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    lease.close()
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(entered.wait(5))
            with self.assertRaises(HeavyAdmissionBusy):
                with lease.entered():
                    pass
            self.assertFalse(lease.close_if_unstarted(thread))
            self.assertFalse(admission_idle(runtime)["idle"])
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            with heavy_admitted(runtime, operation="cycle"):
                context = copy_context()
                observed = []
                other = threading.Thread(target=lambda: context.run(
                    lambda: observed.append(current_heavy_owner(runtime))))
                other.start(); other.join(5)
                self.assertEqual(observed, [None])
            unused = HeavyAdmissionLease(runtime, operation="cycle")
            self.assertTrue(unused.close_if_unstarted(threading.Thread(target=lambda: None)))
            self.assertTrue(admission_idle(runtime)["idle"])

    @unittest.skipUnless(hasattr(os, "fork"), "fork ownership test")
    def test_fork_has_no_authority_and_close_does_not_unlock_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            initialize_admission(runtime)
            with heavy_admitted(runtime, operation="cycle") as lease:
                reader, writer = os.pipe()
                pid = os.fork()
                if pid == 0:
                    os.close(reader)
                    good = current_heavy_owner(runtime) is None
                    try:
                        with lease.entered():
                            good = False
                    except RuntimeError:
                        pass
                    lease.close()
                    os.write(writer, b"yes" if good else b"no")
                    os.close(writer)
                    os._exit(0)
                os.close(writer)
                self.assertEqual(os.read(reader, 3), b"yes")
                os.close(reader)
                os.waitpid(pid, 0)
                with self.assertRaises(HeavyAdmissionBusy):
                    HeavyAdmissionLease(runtime, operation="other", independent=True)
            self.assertTrue(heavy_admission_status(runtime)["idle"])

    def test_uncertain_thread_start_keeps_live_lease_until_worker_finally(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            lease = HeavyAdmissionLease(runtime, operation="cycle")
            entered, release = threading.Event(), threading.Event()
            def worker():
                try:
                    with lease.entered():
                        entered.set()
                        release.wait(5)
                finally:
                    lease.close()
            class UncertainThread(threading.Thread):
                def start(self):
                    super().start()
                    if not entered.wait(5):
                        raise AssertionError("worker did not enter")
                    raise RuntimeError("transport/start result uncertain")
            thread = UncertainThread(target=worker)
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                thread.start()
            self.assertFalse(lease.close_if_unstarted(thread))
            self.assertFalse(admission_idle(runtime)["idle"])
            release.set()
            thread.join(5)
            self.assertTrue(admission_idle(runtime)["idle"])


class BackupAdmissionSmoke(unittest.TestCase):
    def test_multiple_safe_started_manual_supersession_and_strict_scheduled_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime"
            _, _, root, rotation, _ = initial_backup(runtime)
            pending = []
            for producer in ("c" * 40, "d" * 40):
                old = _rotation(runtime, root, deployed_sha=producer)
                plan = old.build_plan(force_replacement=True)
                self.assertTrue(plan["apply_allowed_by_machine_preflight"])
                for key in ("destination_partial", "destination_final"):
                    self.assertFalse(Path(plan["replacement"][key]).exists())
                path = root / "transactions" / (plan["fingerprint"].removeprefix("sha256:") + ".json")
                transaction = {
                    "contract_version": TRANSACTION_CONTRACT, "strategy": STRATEGY,
                    "plan_fingerprint": plan["fingerprint"], "backup_id": plan["backup_id"],
                    "deployed_sha": producer, "approval_reference": "offline-review-approved",
                    "transaction_path": str(path.resolve()), "reviewed_plan": plan,
                    "phase": "started", "completed_deletions": [], "deletion_receipts": {},
                    "pending_deletion": "", "copy_proofs": {}, "updated_at": "2026-07-31T00:00:00Z",
                }
                _atomic_write_json(path, transaction)
                pending.append((path, transaction))
            before = {path: path.read_bytes() for path, _ in pending}
            priority = backup_admission_priority(runtime)
            self.assertFalse(priority["ready"])
            self.assertTrue(priority["priority"])
            with self.assertRaisesRegex(BackupAdmissionStateError, "multiple pending"):
                scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA,
                                   require_distinct_device=False, require_backup_mountpoint=False)
            self.assertEqual(before, {path: path.read_bytes() for path, _ in pending})

            unsafe_path, safe_transaction = pending[0]
            _atomic_write_json(unsafe_path, {**safe_transaction, "phase": "copied"})
            blocked = rotation.build_plan(force_replacement=True)
            self.assertFalse(blocked["apply_allowed_by_machine_preflight"])
            self.assertIn("non_terminal_transaction_requires_exact_resume",
                          {item["code"] for item in blocked["blockers"]})
            with self.assertRaises(FinanceStorageBackupRotationError):
                rotation.apply(reviewed_plan=blocked, expected_fingerprint=blocked["fingerprint"],
                               approval_reference="offline-review-approved")
            self.assertEqual(json.loads(unsafe_path.read_text())["phase"], "copied")
            _atomic_write_json(unsafe_path, safe_transaction)

            plan = rotation.build_plan(force_replacement=True)
            self.assertTrue(plan["apply_allowed_by_machine_preflight"])
            self.assertEqual(plan["blockers"], [])
            self.assertEqual({item["source_deployed_sha"] for item in
                              plan["pre_mutation_transaction_terminalizations"]}, {"c" * 40, "d" * 40})
            applied = rotation.apply(reviewed_plan=plan, expected_fingerprint=plan["fingerprint"],
                                     approval_reference="offline-review-approved")
            self.assertEqual(applied["status"], "completed")
            self.assertEqual(len(applied["superseded_pre_mutation_transactions"]), 2)
            for path, _ in pending:
                terminal = json.loads(path.read_text())
                self.assertEqual(terminal["phase"], "completed")
                self.assertEqual(terminal["terminal_status"], "superseded_before_mutation")
                self.assertEqual(terminal["copy_proofs"], {})
                self.assertEqual(terminal["completed_deletions"], [])
            readback = rotation.readback(reviewed_plan=plan, expected_fingerprint=plan["fingerprint"])
            self.assertEqual(len(readback["superseded_pre_mutation_transactions"]), 2)

    def test_bounded_priority_due_deadline_and_unknown_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime"
            raw, _, root, _, _ = initial_backup(runtime)
            selector = json.loads((root / "current.json").read_text())
            manifest = json.loads((root / "retained" / selector["backup_id"] / "backup_manifest.json").read_text())
            captured = datetime.fromisoformat(manifest["captured_at"].replace("Z", "+00:00"))
            with patch("packages.application.finance_storage_backup_rotation._sha256_file", side_effect=AssertionError("big hash")), \
                 patch("packages.application.finance_storage_backup_rotation._sqlite_readback", side_effect=AssertionError("DB scan")):
                unknown = backup_admission_priority(runtime, now=captured + timedelta(days=5))
                self.assertTrue(unknown["ready"])
                self.assertIsNone(unknown["latest_safe_start_at"])
                self.assertFalse(unknown["timing_proven"])
                reserved = backup_admission_priority(runtime, now=captured + timedelta(days=6, hours=20),
                                                      duration_budget_seconds=7200, next_actor_budget_seconds=10800)
                self.assertEqual(reserved["reason"], "latest_safe_start")
                deadline = backup_admission_priority(runtime, now=captured + timedelta(days=7))
                self.assertEqual(deadline["reason"], "rpo_deadline")
            with closing(sqlite3.connect(raw)) as connection:
                connection.execute("UPDATE backup_fixture_raw SET value='changed'")
                connection.commit()
            due = backup_admission_priority(runtime, now=captured + timedelta(days=6))
            self.assertEqual(due["reason"], "eligible_source_changed")
            self.assertEqual(due["minimum_interval_seconds"], 6 * 86400)
            self.assertEqual(due["rpo_seconds"], 7 * 86400)
            self.assertEqual(due["rto_seconds"], 4 * 3600)

    def test_canonical_build_apply_scheduled_busy_and_durable_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime"
            raw, _, root, rotation, _ = initial_backup(runtime)
            make_due(root)
            with closing(sqlite3.connect(raw)) as connection:
                connection.execute("UPDATE backup_fixture_raw SET value='due'")
                connection.commit()
            plan = rotation.build_plan()
            transactions_before = set((root / "transactions").iterdir())
            with actor(runtime):
                with patch.object(rotation, "_guard", side_effect=AssertionError("body admitted")):
                    with self.assertRaises(FinanceBackupDeferred):
                        rotation.build_plan()
                    with self.assertRaises(FinanceBackupDeferred):
                        rotation.apply(reviewed_plan=plan, expected_fingerprint=plan["fingerprint"],
                                       approval_reference="offline-fixture-approved")
                    with self.assertRaises(FinanceBackupDeferred):
                        rotation.readback(reviewed_plan=plan, expected_fingerprint=plan["fingerprint"])
                output = runtime / "reviewed-plan-sentinel.json"
                output.write_text('{"existing_reviewed_input": true}')
                cli = json.loads(subprocess.check_output([
                    sys.executable, str(ROOT / "apps/finance_storage_split.py"),
                    "snapshot-retention-plan", "--runtime-dir", str(runtime),
                    "--deployed-sha", DEPLOYED_SHA, "--output", str(output),
                ], cwd=ROOT))
                self.assertEqual(cli["status"], "deferred")
                self.assertEqual(output.read_text(), '{"existing_reviewed_input": true}')
                result = scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA,
                                            require_distinct_device=False, require_backup_mountpoint=False)
                self.assertEqual(result["status"], "deferred")
                self.assertTrue(result["intent_recorded"])
                again = scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA,
                                           require_distinct_device=False, require_backup_mountpoint=False)
                self.assertEqual(result["request_id"], again["request_id"])
            self.assertTrue(admission_idle(runtime)["idle"])
            self.assertEqual(transactions_before, set((root / "transactions").iterdir()))
            code = ("import json,sys;from pathlib import Path;"
                    "from packages.application.finance_backup_admission import backup_admission_priority;"
                    "print(json.dumps(backup_admission_priority(Path(sys.argv[1]))))")
            restarted = json.loads(subprocess.check_output([sys.executable, "-c", code, str(runtime)], cwd=ROOT))
            self.assertEqual(restarted["intent"]["request_id"], result["request_id"])
            self.assertEqual(restarted["reason"], "deferred_backup_request")
            completed = scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA,
                                           require_distinct_device=False, require_backup_mountpoint=False)
            self.assertEqual(completed["status"], "completed")
            intent = json.loads((runtime / STATE_FILENAME).read_text())
            self.assertEqual(intent["status"], "resolved")
            self.assertFalse(backup_admission_priority(runtime)["priority"])

    def test_exact_pending_unknown_outcome_reads_existing_copy_no_resend(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime"
            raw, _, root, rotation, _ = initial_backup(runtime)
            make_due(root)
            with closing(sqlite3.connect(raw)) as connection:
                connection.execute("UPDATE backup_fixture_raw SET value='pending'")
                connection.commit()
            plan = rotation.build_plan()
            with self.assertRaisesRegex(RuntimeError, "after current selection"):
                rotation.apply(reviewed_plan=plan, expected_fingerprint=plan["fingerprint"],
                               approval_reference="offline-fixture-approved", activate_policy=False,
                               fault_at="after_current_selected")
            state = backup_admission_priority(runtime)
            self.assertEqual(state["reason"], "pending_exact_transaction")
            self.assertEqual(state["pending_transaction"]["plan_fingerprint"], plan["fingerprint"])
            with self.assertRaisesRegex(FinanceStorageBackupRotationError, "another deployed SHA"):
                scheduled_rotation(runtime, deployed_sha="b" * 40,
                                   require_distinct_device=False, require_backup_mountpoint=False)
            with patch("packages.application.finance_storage_backup_rotation._copy_sqlite", side_effect=AssertionError("copy resend")):
                result = scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA,
                                            require_distinct_device=False, require_backup_mountpoint=False)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["plan_fingerprint"], plan["fingerprint"])

    def test_inert_read_and_invalid_metadata_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            self.assertFalse(backup_admission_priority(runtime)["priority"])
            self.assertEqual(list(runtime.iterdir()), [])
            self.assertEqual(scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA)["status"], "policy_inert")
            self.assertEqual(list(runtime.iterdir()), [])
            (runtime / STATE_FILENAME).write_text("{}")
            os.chmod(runtime / STATE_FILENAME, 0o600)
            bad = backup_admission_priority(runtime)
            self.assertFalse(bad["ready"])
            self.assertTrue(bad["priority"])
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime"
            _, _, _, rotation, _ = initial_backup(runtime)
            (runtime / STATE_FILENAME).write_text("{}")
            os.chmod(runtime / STATE_FILENAME, 0o600)
            with patch.object(rotation, "_guard", side_effect=AssertionError("invalid intent admitted")):
                with self.assertRaises(BackupAdmissionStateError):
                    rotation.build_plan()


if __name__ == "__main__":
    unittest.main()
