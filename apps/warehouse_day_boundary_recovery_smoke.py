#!/usr/bin/env python3
"""Disposable real-checkpoint/SQLite contention regressions for midnight aborts."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.warehouse_functional import WarehouseFunctionalBlock, WarehouseBusinessDateBoundaryError, _fingerprint
from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry, RecoveryPolicyError
from packages.application.warehouse_recovery_sync_retention import run_bounded_recovery_retention
from packages.application.warehouse_update_journal import ensure_warehouse_update_journal_schema
from apps import warehouse_hourly_rollback_reconciliation as reconciliation
from apps.production_apply_contract import AdapterError
from apps.production_apply_launcher import execute

BEFORE = "2026-11-01T18:59:59Z"
AFTER = "2026-11-01T19:00:01Z"


class DayBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(self.temp.name) / "state")
        self.clock = BEFORE
        self.block = WarehouseFunctionalBlock(runtime=self.runtime, timestamp_factory=lambda: self.clock)
        self.block._local_source_digest = lambda **_: "sha256:local"
        self.block._wb_supply_source_digest = lambda **_: "sha256:supply"
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,'base',?)", (BEFORE,))
            conn.execute("CREATE TABLE unrelated_supplier_expenses(id TEXT PRIMARY KEY,amount TEXT)")
            conn.execute("INSERT INTO unrelated_supplier_expenses VALUES('preserve','123')")
        self.plan = {
            "kind": "hourly_wb_sync", "captured_at": BEFORE,
            "effective_date": "2026-11-01", "base_active_version_id": "base",
            "wb_snapshot": {"snapshot_date": "2026-11-01"}, "lines": [],
            "local_source_digest": "sha256:local", "wb_supply_source_digest": "sha256:supply",
        }
        self.plan["plan_fingerprint"] = _fingerprint(self.plan)
        self.registry = WarehouseRecoveryRegistry(runtime_dir=self.runtime.runtime_dir, db_path=self.runtime.db_path)

    def apply(self):
        return self.block.apply_plan(self.plan, confirm_fingerprint=self.plan["plan_fingerprint"])

    def assert_safely_aborted(self):
        operation = self.registry.list_operations(limit=1)[0]
        self.assertEqual(operation["lifecycle"], "rolled_back")
        self.assertIn("canonical business-date boundary", operation["last_error"])
        self.assertEqual(operation["after_digest"], "")
        for artifact in operation["artifacts"]:
            self.assertTrue(Path(artifact["path"]).is_file())
            self.assertEqual(artifact["state"], "verified")
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active").fetchall(), [("base",)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_functional_versions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT * FROM unrelated_supplier_expenses").fetchall(), [("preserve", "123")])
        result = run_bounded_recovery_retention(self.runtime)
        self.assertIn(result["status"], ("no_change", "applied"))
        # The old coherent capture stays expired; it is never silently applied.
        with self.assertRaises(WarehouseBusinessDateBoundaryError):
            self.apply()
        self.assertEqual(len(self.registry.list_operations()), 1)
        # A new day's coherent capture can obtain its own native checkpoint;
        # the expired attempt no longer poisons recovery admission globally.
        fresh_fingerprint = _fingerprint({"captured_at": AFTER, "effective_date": "2026-11-02"})
        fresh = self.registry.prepare_t2(mutation_kind="hourly_warehouse_sync", plan_fingerprint=fresh_fingerprint,
            scope={"effective_date": "2026-11-02", "base_active_version_id": "base"}, source_digest="sha256:local",
            non_target_digest="", source_watermarks={"effective_date": "2026-11-02"}, schema_revision="v2")
        self.assertEqual(fresh["lifecycle"], "verified")
        self.assertEqual(self.registry.get_operation(operation["operation_id"])["lifecycle"], "rolled_back")

    def test_checkpoint_creation_crosses_business_midnight(self):
        original = WarehouseRecoveryRegistry.prepare_t2
        def prepare(registry, **kwargs):
            result = original(registry, **kwargs)
            self.clock = AFTER
            return result
        with patch.object(WarehouseRecoveryRegistry, "prepare_t2", prepare):
            with self.assertRaisesRegex(WarehouseBusinessDateBoundaryError, "acquiring apply lock"):
                self.apply()
        self.assert_safely_aborted()

    def test_waiting_for_sqlite_apply_lock_crosses_business_midnight(self):
        ready, release, began = Event(), Event(), Event()
        failures = []
        def holder():
            try:
                with closing(sqlite3.connect(self.runtime.db_path)) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    ready.set()
                    if not began.wait(5):
                        raise AssertionError("apply never attempted SQLite lock")
                    self.clock = AFTER
                    release.wait(0.05)
                    conn.rollback()
            except BaseException as exc:
                failures.append(exc)
                ready.set()
        original_connect = __import__("packages.application.warehouse_functional", fromlist=["_connect"])._connect
        def connect(path):
            conn = original_connect(path)
            def trace(sql):
                if sql == "BEGIN IMMEDIATE":
                    worker.start()
                    self.assertTrue(ready.wait(5))
                    began.set()
            conn.set_trace_callback(trace)
            return conn
        worker = Thread(target=holder)
        try:
            with patch("packages.application.warehouse_functional._connect", connect):
                with self.assertRaisesRegex(WarehouseBusinessDateBoundaryError, "acquiring apply lock"):
                    self.apply()
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assert_safely_aborted()

    def test_other_failure_remains_protected(self):
        self.block._local_source_digest = lambda **kwargs: "sha256:drift" if "connection" in kwargs else "sha256:local"
        with self.assertRaisesRegex(Exception, "local sources drifted while"):
            self.apply()
        self.assertEqual(self.registry.list_operations(limit=1)[0]["lifecycle"], "failed_recoverable")
        with self.assertRaisesRegex(RuntimeError, "unresolved protected T2"):
            run_bounded_recovery_retention(self.runtime)


class LegacyReconciliationTests(unittest.TestCase):
    def setUp(self):
        DayBoundaryTests.setUp(self)
        root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(reconciliation, "ROOT", root).start()
        (root / ".wb-core-runtime-sha").write_text("a" * 40)
        self.run_id, self.cycle_id = "whur_exact_guard_failure", "b" * 32
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            ensure_warehouse_update_journal_schema(conn)
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_update_runs("
                "run_id,trigger_source,status,scheduled_for,started_at,active_phase,last_error,result_json,"
                "functional_version_id,business_date,created_at,updated_at) "
                "VALUES(?,'cycle','running',?,?,'functional_publication','','{}','','2026-11-01',?,?)",
                (self.run_id, BEFORE, "2026-11-01T18:45:00Z", BEFORE, BEFORE))
            for phase, status, details in (
                ("official_complete_wb_stocks", "success", {"plan_fingerprint": self.plan["plan_fingerprint"]}),
                ("functional_publication", "running", {})):
                conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_update_phases("
                    "run_id,phase_key,status,started_at,item_count,last_error,details_json) VALUES(?,?,?,'2026-11-01T18:54:00Z',0,'',?)",
                    (self.run_id, phase, status, json.dumps(details)))
        moment = [datetime(2026, 11, 1, 18, 56, tzinfo=timezone.utc)]
        def inject(op, stage):
            if stage == "after_checkpoint_fsync":
                moment[0] = datetime(2026, 11, 1, 19, 2, tzinfo=timezone.utc)
        registry = WarehouseRecoveryRegistry(runtime_dir=self.runtime.runtime_dir, db_path=self.runtime.db_path,
                                             clock=lambda: moment[0], fault_injector=inject)
        op = registry.prepare_t2(mutation_kind="hourly_warehouse_sync", plan_fingerprint=self.plan["plan_fingerprint"],
            scope={"effective_date": "2026-11-01", "base_active_version_id": "base"}, source_digest="sha256:local",
            non_target_digest="", source_watermarks={"effective_date": "2026-11-01", "base_active_version_id": "base", "local_source_digest": "sha256:local"}, schema_revision="v2")
        self.operation_id = op["operation_id"]
        registry.begin_mutation(self.operation_id)
        registry.fail_recoverable(self.operation_id, error="hourly_wb_sync transaction failed", next_action="resume_or_rollback_hourly_wb_sync")
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_update_runs SET status='failed',finished_at='2026-11-01T19:03:00Z',last_error=? WHERE run_id=?",
                         ("functional_publication: " + reconciliation.ERROR, self.run_id))
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_update_phases SET status='failed',finished_at='2026-11-01T19:03:00Z',last_error=? WHERE run_id=? AND phase_key='functional_publication'",
                         (reconciliation.ERROR, self.run_id))
            # A later source edit is intentionally outside publication proof.
            conn.execute("INSERT INTO unrelated_supplier_expenses VALUES('later','456')")
        cycle_dir = self.runtime.runtime_dir / "sheet-vitrina-cycles"
        cycle_dir.mkdir()
        (cycle_dir / (self.cycle_id + ".json")).write_text(json.dumps({"cycle_id": self.cycle_id,
            "business_date": "2026-11-01", "stages": [{"stage": "warehouse", "durable_ref": self.run_id, "status": "failed"}]}))
        self.request = {"runtime_dir": str(self.runtime.runtime_dir), "deployed_sha": "a" * 40,
            "recovery_operation_id": self.operation_id, "cycle_id": self.cycle_id, "warehouse_run_id": self.run_id}
        self.registry.clock = lambda: datetime(2026, 11, 2, tzinfo=timezone.utc)
        self.adapter = reconciliation.WarehouseHourlyRollbackReconciliationAdapter()
        self.launcher_op = "hourly-rollback-fixture-20261102"

    def preview(self):
        return self.adapter.preview(self.request, self.launcher_op)

    def update_operation(self, assignment, parameters=()):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_recovery_operations SET " + assignment + " WHERE operation_id=?", (*parameters, self.operation_id))

    def test_query_only_preview_and_launcher_submit_preserve_later_sources(self):
        before = self.runtime.db_path.read_bytes()
        files = sorted(str(path) for path in self.runtime.runtime_dir.rglob("*"))
        preview = self.preview()
        self.assertEqual(self.runtime.db_path.read_bytes(), before)
        self.assertEqual(sorted(str(path) for path in self.runtime.runtime_dir.rglob("*")), files)
        receipt = execute(action="apply", adapter_name="fixture", operation_id=self.launcher_op,
            request=self.request, expected_prestate=preview["prestate_sha256"], expected_candidate=preview["candidate_sha256"],
            adapters={"fixture": self.adapter})
        self.assertEqual(receipt["state"], "applied")
        operation = self.registry.get_operation(self.operation_id)
        self.assertEqual(operation["lifecycle"], "rolled_back")
        self.assertEqual(operation["last_error"], "hourly_wb_sync transaction failed")
        self.assertTrue(all(Path(artifact["path"]).is_file() for artifact in operation["artifacts"]))
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT * FROM unrelated_supplier_expenses ORDER BY id").fetchall(), [("later", "456"), ("preserve", "123")])
            self.assertEqual(conn.execute("SELECT status FROM sheet_vitrina_v1_warehouse_update_runs WHERE run_id=?", (self.run_id,)).fetchone()[0], "failed")
        self.assertEqual(self.adapter.readback(self.request, self.launcher_op)["state"], "applied")
        (Path(self.temp.name) / ".wb-core-runtime-sha").write_text("c" * 40)
        self.assertEqual(self.adapter.readback(self.request, self.launcher_op)["state"], "applied")
        self.assertNotEqual(self.adapter.readback(self.request, "another-operation")["state"], "applied")
        # Native retention no longer blocks and does not delete the checkpoint.
        with patch("packages.application.warehouse_recovery_sync_retention.WarehouseRecoveryRegistry", return_value=self.registry):
            self.assertEqual(run_bounded_recovery_retention(self.runtime)["status"], "no_change")
        self.assertTrue(all(Path(artifact["path"]).is_file() for artifact in operation["artifacts"]))

    def test_wrong_canonical_error_is_rejected(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_update_phases SET last_error='other failure' WHERE phase_key='functional_publication'")
        with self.assertRaisesRegex(AdapterError, "canonical-phase-plan"):
            self.preview()

    def test_wrong_cycle_run_link_is_rejected(self):
        self.request["warehouse_run_id"] = "other-run"
        with self.assertRaisesRegex(AdapterError, "canonical-run-error"):
            self.preview()

    def test_changed_publication_operand_is_rejected(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_active SET updated_at='drift'")
        with self.assertRaisesRegex(AdapterError, "publication-drift:functional_active"):
            self.preview()

    def test_existing_after_digest_is_rejected(self):
        self.update_operation("after_digest='sha256:committed'")
        with self.assertRaisesRegex(AdapterError, "operation-not-exact"):
            self.preview()

    def test_active_base_drift_is_rejected(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_active SET version_id='newer'")
        with self.assertRaisesRegex(AdapterError, "active-version-drift"):
            self.preview()

    def test_candidate_version_presence_is_rejected(self):
        candidate = "whfv_" + self.plan["plan_fingerprint"].removeprefix("sha256:")[:24]
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_versions("
                "version_id,cutover_id,version_kind,effective_at,status,plan_fingerprint,local_source_digest,source_watermarks_json,created_at) "
                "VALUES(?,'fixture','hourly_wb_sync',?,'good',?,'local','{}',?)", (candidate, BEFORE, self.plan["plan_fingerprint"], BEFORE))
        with self.assertRaisesRegex(AdapterError, "candidate-already-published"):
            self.preview()

    def test_reviewed_cas_drift_is_rejected_without_lifecycle_write(self):
        preview = self.preview()
        self.update_operation("state_version=state_version+1")
        with self.assertRaisesRegex(AdapterError, "reviewed-prestate-drift"):
            self.adapter.apply(self.request, self.launcher_op, preview)
        self.assertEqual(self.registry.get_operation(self.operation_id)["lifecycle"], "failed_recoverable")

    def test_native_terminal_transition_rejects_stale_state_version(self):
        operation = self.registry.get_operation(self.operation_id)
        with self.assertRaisesRegex(RecoveryPolicyError, "proof or CAS drifted"):
            self.registry.complete_hourly_transaction_rollback(self.operation_id,
                expected_state_version=operation["state_version"] - 1,
                plan_fingerprint=operation["plan_fingerprint"], base_active_version_id="base",
                evidence={"contract": "hourly_business_date_rollback_v1", "sqlite_rollback_completed": True,
                          "planned_business_date": "2026-11-01", "error": reconciliation.ERROR})
        self.assertEqual(self.registry.get_operation(self.operation_id)["lifecycle"], "failed_recoverable")


if __name__ == "__main__":
    unittest.main()
