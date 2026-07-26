#!/usr/bin/env python3
"""Deterministic smoke coverage for the unified warehouse recovery policy."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_functional import (  # noqa: E402
    enqueue_warehouse_targeted_recalculation,
)
from apps.warehouse_recovery_policy_canary import (  # noqa: E402
    plan_fingerprint as canary_fingerprint,
    run as run_canary,
)
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    BeforeImageQuery,
    OPERATION_POLICIES,
    RecoveryPolicyError,
    RecoverySelection,
    RecoveryState,
    RecoveryTier,
    T3_MIGRATION_ALLOWLIST,
    WarehouseRecoveryRegistry,
    capture_before_images,
    registered_policy_table,
    select_recovery_tier,
)


class WarehouseRecoveryPolicySmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime_dir = Path(self.temporary.name) / "state"
        self.runtime_dir.mkdir(parents=True)
        self.runtime = RegistryUploadDbBackedRuntime(runtime_dir=self.runtime_dir)
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE bounded_rows(
                    row_id TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    untouched TEXT NOT NULL
                );
                INSERT INTO bounded_rows VALUES('target','before','stable');
                INSERT INTO bounded_rows VALUES('other','same','stable');
                CREATE TABLE bounded_blobs(
                    row_id TEXT PRIMARY KEY,
                    payload BLOB NOT NULL
                );
                INSERT INTO bounded_blobs VALUES('blob-target',X'000102FF');
                CREATE TABLE sheet_vitrina_v1_warehouse_functional_versions(
                    version_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    source_blob BLOB
                );
                INSERT INTO sheet_vitrina_v1_warehouse_functional_versions
                VALUES('v1','2026-07-26T00:00:00Z',X'000102FF');
                CREATE TABLE sheet_vitrina_v1_calculation_parameter_versions(
                    version_id TEXT PRIMARY KEY,
                    rates_json TEXT NOT NULL
                );
                INSERT INTO sheet_vitrina_v1_calculation_parameter_versions
                VALUES('p1','{}');
                CREATE TABLE sheet_vitrina_v1_warehouse_wb_sync_status(
                    slot INTEGER PRIMARY KEY,
                    last_attempt_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT,
                    active_version_id TEXT,
                    updated_at TEXT
                );
                INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status
                VALUES(1,'2026-07-26T00:00:00Z','2026-07-26T00:00:00Z',NULL,'v1','2026-07-26T00:00:00Z');
                CREATE TABLE wb_finance_weekly_raw_rows(
                    raw_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );
                INSERT INTO wb_finance_weekly_raw_rows VALUES('raw-1','{"secret":"raw"}');
                """
            )
            conn.commit()
        self.registry = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime_dir,
            db_path=self.runtime.db_path,
            operational_reserve_bytes=0,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_policy_table_is_total_and_fail_closed(self) -> None:
        table = registered_policy_table()
        self.assertEqual(len(table), len(OPERATION_POLICIES))
        self.assertEqual(
            {item["mutation_kind"] for item in table},
            set(OPERATION_POLICIES),
        )
        for item in table:
            self.assertEqual(
                len(
                    {
                        select_recovery_tier(
                            mutation_kind=item["mutation_kind"],
                            closure_kind=item["closure_kinds"][0],
                            would_change=True,
                            migration_id=(
                                next(iter(T3_MIGRATION_ALLOWLIST))
                                if item["tier"] == "T3"
                                else ""
                            ),
                        ).tier
                    }
                )
                if item["enabled"]
                else 0,
                1 if item["enabled"] else 0,
            )
        with self.assertRaises(RecoveryPolicyError):
            select_recovery_tier(
                mutation_kind="new_unclassified_command",
                closure_kind="sku_date",
                would_change=True,
            )
        with self.assertRaises(RecoveryPolicyError):
            select_recovery_tier(
                mutation_kind="legacy_invoice_recovery",
                closure_kind="shipment",
                would_change=True,
            )

    def test_t0_noop_creates_zero_recovery_state_or_bytes(self) -> None:
        isolated = Path(self.temporary.name) / "noop-only"
        db = isolated / "registry_upload_runtime.sqlite3"
        policy = WarehouseRecoveryRegistry(
            runtime_dir=isolated,
            db_path=db,
            operational_reserve_bytes=0,
        )
        result = policy.plan_noop(
            mutation_kind="supplier_cost_queue_replay",
            closure_kind="shipment",
            plan_fingerprint="sha256:noop",
            scope={"shipment_ids": ["S1"]},
        )
        self.assertEqual(result["tier"], "T0")
        self.assertEqual(result["planned_bytes"], 0)
        self.assertEqual(result["actual_bytes"], 0)
        self.assertEqual(result["read_bytes"], 0)
        self.assertFalse(isolated.exists())

    def test_t1_exact_before_image_retain_and_idempotent_rollback(self) -> None:
        images, read_bytes = capture_before_images(
            self.runtime.db_path,
            [
                BeforeImageQuery(
                    table="bounded_rows",
                    query="SELECT * FROM bounded_rows WHERE row_id=?",
                    parameters=("target",),
                    key_columns=("row_id",),
                )
            ],
        )
        images[0]["after"] = {
            "row_id": "target",
            "value": "after",
            "untouched": "stable",
        }
        prepared = self.registry.prepare_t1(
            mutation_kind="supplier_cost_queue_replay",
            closure_kind="shipment",
            plan_fingerprint="sha256:t1",
            scope={"shipment_ids": ["S1"], "nm_ids": [1]},
            before_images=images,
            source_digest="sha256:source",
            non_target_digest="sha256:non-target",
            read_bytes=read_bytes,
        )
        self.assertEqual(prepared["tier"], "T1")
        self.assertEqual(prepared["lifecycle"], "verified")
        self.assertGreater(prepared["actual_bytes"], 0)
        self.assertFalse(
            list((self.runtime_dir / "warehouse-recovery").rglob("*"))
            if (self.runtime_dir / "warehouse-recovery").exists()
            else []
        )
        self.registry.begin_mutation(
            prepared["operation_id"],
            expected_source_digest="sha256:source",
        )
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            conn.execute(
                "UPDATE bounded_rows SET value='after' WHERE row_id='target'"
            )
            conn.commit()
        retained = self.registry.retain(
            prepared["operation_id"],
            after_digest="sha256:after",
            non_target_digest="sha256:non-target",
        )
        self.assertEqual(retained["lifecycle"], "retained")
        rolled_back = self.registry.rollback_t1(
            prepared["operation_id"],
            reason="smoke",
        )
        self.assertEqual(rolled_back["lifecycle"], "rolled_back")
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            target = conn.execute(
                "SELECT value,untouched FROM bounded_rows WHERE row_id='target'"
            ).fetchone()
            other = conn.execute(
                "SELECT value,untouched FROM bounded_rows WHERE row_id='other'"
            ).fetchone()
        self.assertEqual(target, ("before", "stable"))
        self.assertEqual(other, ("same", "stable"))

    def test_commission_queue_repeat_is_true_t0_noop(self) -> None:
        first = enqueue_warehouse_targeted_recalculation(
            runtime=self.runtime,
            stable_source_id="supplier_shipment:commission-regression",
            source_revision="sha256:commission-v1",
            effective_date="2026-07-26",
            affected_nm_ids=[1001],
            requested_at="2026-07-26T01:00:00Z",
        )
        self.assertEqual(first["recovery_policy"]["tier"], "T1")
        self.assertEqual(first["recovery_policy"]["lifecycle"], "retained")
        operation_count = len(self.registry.list_operations(limit=100))
        second = enqueue_warehouse_targeted_recalculation(
            runtime=self.runtime,
            stable_source_id="supplier_shipment:commission-regression",
            source_revision="sha256:commission-v1",
            effective_date="2026-07-26",
            affected_nm_ids=[1001],
            requested_at="2026-07-26T02:00:00Z",
        )
        self.assertEqual(second["recovery_policy"]["tier"], "T0")
        self.assertEqual(second["recovery_policy"]["actual_bytes"], 0)
        self.assertEqual(second["recovery_policy"]["read_bytes"], 0)
        self.assertEqual(second["requested_at"], "2026-07-26T01:00:00Z")
        self.assertEqual(
            len(self.registry.list_operations(limit=100)),
            operation_count,
        )

    def test_bounded_capture_rejects_finance_raw(self) -> None:
        with self.assertRaises(RecoveryPolicyError):
            capture_before_images(
                self.runtime.db_path,
                [
                    BeforeImageQuery(
                        table="wb_finance_weekly_raw_rows",
                        query="SELECT * FROM wb_finance_weekly_raw_rows",
                        key_columns=("raw_id",),
                    )
                ],
            )

    def test_t1_blob_before_image_is_byte_exact(self) -> None:
        images, _ = capture_before_images(
            self.runtime.db_path,
            [
                BeforeImageQuery(
                    table="bounded_blobs",
                    query="SELECT * FROM bounded_blobs WHERE row_id=?",
                    parameters=("blob-target",),
                    key_columns=("row_id",),
                )
            ],
        )
        images[0]["after"] = {
            "row_id": "blob-target",
            "payload": b"\x10\x11\x12",
        }
        operation = self.registry.prepare_t1(
            mutation_kind="ff_ledger_operation",
            closure_kind="document",
            plan_fingerprint="sha256:blob-undo",
            scope={"row_id": "blob-target"},
            before_images=images,
        )
        self.registry.begin_mutation(operation["operation_id"])
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            conn.execute(
                "UPDATE bounded_blobs SET payload=? WHERE row_id='blob-target'",
                (sqlite3.Binary(b"\x10\x11\x12"),),
            )
            conn.commit()
        self.registry.retain(
            operation["operation_id"],
            after_digest="sha256:blob-after",
        )
        self.registry.rollback_t1(operation["operation_id"], reason="blob-smoke")
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            restored = conn.execute(
                "SELECT payload FROM bounded_blobs WHERE row_id='blob-target'"
            ).fetchone()[0]
        self.assertEqual(restored, b"\x00\x01\x02\xff")

    def test_production_canary_is_business_safe_and_terminal(self) -> None:
        deployed_sha = "a" * 40
        legacy_backup = self.runtime_dir / "backups" / "legacy.sqlite3"
        legacy_backup.parent.mkdir(parents=True)
        legacy_backup.write_bytes(b"pre-policy-backup")
        os.utime(legacy_backup, (1, 1))
        before = self.registry.domain_content_digest()
        result = run_canary(
            runtime_dir=self.runtime_dir,
            deployed_sha=deployed_sha,
            apply=True,
            confirm=canary_fingerprint(deployed_sha),
        )
        after = self.registry.domain_content_digest()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["noop"]["tier"], "T0")
        self.assertEqual(result["bounded_replay"]["lifecycle"], "rolled_back")
        self.assertEqual(
            result["bounded_replay"]["scope"]["deployed_sha"],
            deployed_sha,
        )
        self.assertEqual(
            result["wide_domain_publication"]["lifecycle"],
            "retained",
        )
        self.assertEqual(
            result["wide_domain_publication"]["scope"]["deployed_sha"],
            deployed_sha,
        )
        self.assertEqual(result["orphan_scanner"]["status"], "clean")
        self.assertEqual(
            result["orphan_scanner"]["pre_policy_legacy_count"],
            1,
        )
        self.assertEqual(before["digest"], after["digest"])

    def test_t2_filtered_checkpoint_excludes_finance_raw(self) -> None:
        result = self.registry.prepare_t2(
            mutation_kind="hourly_warehouse_sync",
            plan_fingerprint="sha256:t2",
            scope={"warehouse_domain": "all"},
            source_digest="sha256:source",
            non_target_digest="sha256:non-target",
            source_watermarks={"functional_version_id": "v1", "event_cursor": 0},
            schema_revision="smoke-v1",
        )
        self.assertEqual(result["tier"], "T2")
        self.assertEqual(result["lifecycle"], "verified")
        self.assertGreaterEqual(result["planned_bytes"], result["actual_bytes"])
        checkpoint = next(
            Path(item["path"])
            for item in result["artifacts"]
            if item["artifact_kind"] == "domain_checkpoint"
        )
        with closing(
            sqlite3.connect(f"file:{checkpoint}?mode=ro", uri=True)
        ) as conn:
            conn.execute("PRAGMA query_only=ON")
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            metadata = conn.execute(
                "SELECT source_watermarks_json FROM recovery_checkpoint_metadata"
            ).fetchone()
        self.assertIn(
            "sheet_vitrina_v1_warehouse_functional_versions", tables
        )
        self.assertIn(
            "sheet_vitrina_v1_calculation_parameter_versions", tables
        )
        self.assertNotIn("wb_finance_weekly_raw_rows", tables)
        self.assertEqual(
            json.loads(metadata[0])["functional_version_id"],
            "v1",
        )

    def test_t2_rollback_restores_domain_and_removes_new_tables(self) -> None:
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            conn.execute(
                "CREATE INDEX t2_smoke_functional_created "
                "ON sheet_vitrina_v1_warehouse_functional_versions(created_at)"
            )
            conn.commit()
        prepared = self.registry.prepare_t2(
            mutation_kind="manual_warehouse_sync",
            plan_fingerprint="sha256:t2-rollback",
            scope={"warehouse_domain": "rollback"},
            source_digest="sha256:source",
            non_target_digest="sha256:non-target",
            source_watermarks={"functional_version_id": "v1"},
            schema_revision="smoke-v1",
        )
        self.registry.begin_mutation(prepared["operation_id"])
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_functional_versions "
                "SET created_at='changed' WHERE version_id='v1'"
            )
            conn.execute(
                "CREATE TABLE sheet_vitrina_v1_warehouse_created_by_publish("
                "id INTEGER PRIMARY KEY)"
            )
            conn.execute(
                "ALTER TABLE sheet_vitrina_v1_warehouse_functional_versions "
                "ADD COLUMN leaked_publication_column TEXT"
            )
            conn.execute("DROP INDEX t2_smoke_functional_created")
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_warehouse_created_by_publish VALUES(1)"
            )
            conn.commit()
        self.registry.fail_recoverable(
            prepared["operation_id"],
            error="synthetic post-write failure",
            next_action="rollback_t2",
        )
        rolled_back = self.registry.rollback_t2(
            prepared["operation_id"],
            reason="smoke",
        )
        self.assertEqual(rolled_back["lifecycle"], "rolled_back")
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT created_at FROM "
                    "sheet_vitrina_v1_warehouse_functional_versions "
                    "WHERE version_id='v1'"
                ).fetchone()[0],
                "2026-07-26T00:00:00Z",
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='sheet_vitrina_v1_warehouse_created_by_publish'"
                ).fetchone()
            )
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info("
                    "sheet_vitrina_v1_warehouse_functional_versions)"
                )
            }
            self.assertNotIn("leaked_publication_column", columns)
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='t2_smoke_functional_created'"
                ).fetchone()
            )

    def test_t3_requires_explicit_allowlist(self) -> None:
        with self.assertRaises(RecoveryPolicyError):
            select_recovery_tier(
                mutation_kind="schema_migration",
                closure_kind="full_store",
                would_change=True,
                migration_id="operator-free-form",
            )
        selected = select_recovery_tier(
            mutation_kind="schema_migration",
            closure_kind="full_store",
            would_change=True,
            migration_id="warehouse_recovery_registry_schema_v1",
        )
        self.assertEqual(selected.tier, RecoveryTier.T3)

        destination = (
            self.registry.recovery_root / "allowlisted-t3-smoke.sqlite3"
        )
        prepared = self.registry.prepare_t3(
            runtime=self.runtime,
            mutation_kind="schema_migration",
            migration_id="warehouse_recovery_registry_schema_v1",
            plan_fingerprint="sha256:t3-allowlisted",
            scope={"migration_id": "warehouse_recovery_registry_schema_v1"},
            destination=destination,
        )
        self.assertEqual(prepared["tier"], "T3")
        self.assertEqual(prepared["lifecycle"], "verified")
        self.assertTrue(destination.is_file())
        self.registry.begin_mutation(prepared["operation_id"])
        retained = self.registry.retain(
            prepared["operation_id"],
            after_digest="sha256:t3-smoke-after",
        )
        self.assertEqual(retained["lifecycle"], "retained")
        self.assertEqual(self.registry.scan_orphans()["status"], "clean")

    def test_fault_after_transition_restarts_to_verified(self) -> None:
        raised = {"done": False}

        def fail_once(_: str, boundary: str) -> None:
            if boundary == "after_transition:reserved" and not raised["done"]:
                raised["done"] = True
                raise RuntimeError("fault-after-reserved")

        images = [
            {
                "table": "bounded_rows",
                "key": {"row_id": "target"},
                "before": {
                    "row_id": "target",
                    "value": "before",
                    "untouched": "stable",
                },
                "after": {
                    "row_id": "target",
                    "value": "after",
                    "untouched": "stable",
                },
            }
        ]
        faulting = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime_dir,
            db_path=self.runtime.db_path,
            operational_reserve_bytes=0,
            fault_injector=fail_once,
        )
        with self.assertRaisesRegex(RuntimeError, "fault-after-reserved"):
            faulting.prepare_t1(
                mutation_kind="warehouse_archival_estimate",
                closure_kind="sku_date",
                plan_fingerprint="sha256:fault",
                scope={"nm_ids": [1]},
                before_images=images,
            )
        failed = faulting.list_operations(limit=10)[0]
        self.assertEqual(failed["lifecycle"], "failed_recoverable")
        resumed = self.registry.prepare_t1(
            mutation_kind="warehouse_archival_estimate",
            closure_kind="sku_date",
            plan_fingerprint="sha256:fault",
            scope={"nm_ids": [1]},
            before_images=images,
        )
        self.assertEqual(resumed["lifecycle"], "verified")
        repeated = self.registry.prepare_t1(
            mutation_kind="warehouse_archival_estimate",
            closure_kind="sku_date",
            plan_fingerprint="sha256:fault",
            scope={"nm_ids": [1]},
            before_images=images,
        )
        self.assertEqual(repeated["operation_id"], resumed["operation_id"])
        self.assertEqual(
            len(
                [
                    item
                    for item in repeated["artifacts"]
                    if item["artifact_kind"] == "undo"
                ]
            ),
            1,
        )

    def test_all_t1_and_t2_fault_boundaries_restart_to_terminal(self) -> None:
        t1_boundaries = (
            "after_transition:planned",
            "after_capacity_reservation",
            "after_transition:reserved",
            "after_transition:writing",
            "before_undo_write",
            "after_undo_write",
            "after_transition:verified",
            "before_business_mutation",
            "after_transition:mutation_running",
            "after_business_mutation",
            "after_transition:retained",
        )
        for index, boundary in enumerate(t1_boundaries):
            row_id = f"fault-{index}"
            with closing(sqlite3.connect(self.runtime.db_path)) as conn:
                conn.execute(
                    "INSERT INTO bounded_rows VALUES(?,?,?)",
                    (row_id, "before", "stable"),
                )
                conn.commit()
            raised = {"done": False}

            def fail_once(_: str, current: str) -> None:
                if current == boundary and not raised["done"]:
                    raised["done"] = True
                    raise RuntimeError("fault:" + boundary)

            faulting = WarehouseRecoveryRegistry(
                runtime_dir=self.runtime_dir,
                db_path=self.runtime.db_path,
                operational_reserve_bytes=0,
                fault_injector=fail_once,
            )
            fingerprint = f"sha256:t1-boundary-{index}"
            image = {
                "table": "bounded_rows",
                "key": {"row_id": row_id},
                "before": {
                    "row_id": row_id,
                    "value": "before",
                    "untouched": "stable",
                },
                "after": {
                    "row_id": row_id,
                    "value": "after",
                    "untouched": "stable",
                },
            }
            try:
                operation = faulting.prepare_t1(
                    mutation_kind="supplier_cost_queue_replay",
                    closure_kind="shipment",
                    plan_fingerprint=fingerprint,
                    scope={"row_id": row_id},
                    before_images=[image],
                )
                operation = faulting.begin_mutation(operation["operation_id"])
                with closing(sqlite3.connect(self.runtime.db_path)) as conn:
                    conn.execute(
                        "UPDATE bounded_rows SET value='after' WHERE row_id=?",
                        (row_id,),
                    )
                    conn.commit()
                faulting.retain(
                    operation["operation_id"],
                    after_digest="sha256:after",
                )
            except RuntimeError as exc:
                self.assertEqual(str(exc), "fault:" + boundary)
            self.assertTrue(raised["done"], boundary)

            resumed = self.registry.prepare_t1(
                mutation_kind="supplier_cost_queue_replay",
                closure_kind="shipment",
                plan_fingerprint=fingerprint,
                scope={"row_id": row_id},
                before_images=[image],
            )
            if resumed["lifecycle"] == RecoveryState.VERIFIED.value:
                resumed = self.registry.begin_mutation(resumed["operation_id"])
            if resumed["lifecycle"] == RecoveryState.MUTATION_RUNNING.value:
                with closing(sqlite3.connect(self.runtime.db_path)) as conn:
                    conn.execute(
                        "UPDATE bounded_rows SET value='after' WHERE row_id=?",
                        (row_id,),
                    )
                    conn.commit()
                resumed = self.registry.retain(
                    resumed["operation_id"],
                    after_digest="sha256:after",
                )
            self.assertEqual(resumed["lifecycle"], RecoveryState.RETAINED.value)

        t2_boundaries = (
            "after_transition:planned",
            "after_capacity_reservation",
            "after_transition:reserved",
            "after_transition:writing",
            "before_checkpoint_write",
            "after_checkpoint_fsync",
            "after_manifest_rename",
            "after_transition:verified",
        )
        for index, boundary in enumerate(t2_boundaries):
            raised = {"done": False}

            def fail_once(_: str, current: str) -> None:
                if current == boundary and not raised["done"]:
                    raised["done"] = True
                    raise RuntimeError("fault:" + boundary)

            fingerprint = f"sha256:t2-boundary-{index}"
            faulting = WarehouseRecoveryRegistry(
                runtime_dir=self.runtime_dir,
                db_path=self.runtime.db_path,
                operational_reserve_bytes=0,
                fault_injector=fail_once,
            )
            try:
                faulting.prepare_t2(
                    mutation_kind="hourly_warehouse_sync",
                    plan_fingerprint=fingerprint,
                    scope={"boundary": boundary},
                    source_digest="sha256:source",
                    non_target_digest="sha256:non-target",
                    source_watermarks={"boundary": boundary},
                    schema_revision="smoke-v1",
                )
            except RuntimeError as exc:
                self.assertEqual(str(exc), "fault:" + boundary)
            self.assertTrue(raised["done"], boundary)
            resumed = self.registry.prepare_t2(
                mutation_kind="hourly_warehouse_sync",
                plan_fingerprint=fingerprint,
                scope={"boundary": boundary},
                source_digest="sha256:source",
                non_target_digest="sha256:non-target",
                source_watermarks={"boundary": boundary},
                schema_revision="smoke-v1",
            )
            self.assertEqual(resumed["lifecycle"], RecoveryState.VERIFIED.value)
            checkpoint = next(
                Path(item["path"])
                for item in resumed["artifacts"]
                if item["artifact_kind"] == "domain_checkpoint"
            )
            self.assertFalse(Path(str(checkpoint) + ".tmp").exists())

    def test_capacity_reservation_is_compare_and_set(self) -> None:
        self.registry.ensure_schema()
        selection = RecoverySelection(
            mutation_kind="supplier_cost_queue_replay",
            closure_kind="shipment",
            tier=RecoveryTier.T1,
            would_change=True,
            migration_id="",
            reason="smoke",
        )
        for operation_id in ("capacity-a", "capacity-b"):
            self.registry._create_operation(  # noqa: SLF001 - contract smoke
                operation_id=operation_id,
                selection=selection,
                plan_fingerprint="sha256:" + operation_id,
                scope={"operation_id": operation_id},
                planned_bytes=700,
                source_digest="",
                non_target_digest="",
                rollback_expires_at="2026-07-27T00:00:00Z",
            )

        class DiskUsage:
            total = 10_000
            used = 9_000
            free = 1_000

        def reserve(operation_id: str) -> str:
            self.registry._reserve_capacity(  # noqa: SLF001 - contract smoke
                operation_id=operation_id,
                required_bytes=700,
                target_root=self.runtime_dir,
            )
            return operation_id

        with patch(
            "packages.application.warehouse_recovery_policy.shutil.disk_usage",
            return_value=DiskUsage(),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(reserve, operation_id)
                    for operation_id in ("capacity-a", "capacity-b")
                ]
                successes = []
                failures = []
                for future in futures:
                    try:
                        successes.append(future.result())
                    except RecoveryPolicyError as exc:
                        failures.append(str(exc))
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("capacity hard stop", failures[0])

    def test_capacity_expiry_low_watermark_and_post_write_stop(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        clock_value = {"now": start}
        policy = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime_dir,
            db_path=self.runtime.db_path,
            operational_reserve_bytes=500,
            clock=lambda: clock_value["now"],
        )
        policy.ensure_schema()
        selection = RecoverySelection(
            mutation_kind="supplier_cost_queue_replay",
            closure_kind="shipment",
            tier=RecoveryTier.T1,
            would_change=True,
            migration_id="",
            reason="smoke",
        )
        policy._create_operation(  # noqa: SLF001
            operation_id="expired-capacity",
            selection=selection,
            plan_fingerprint="sha256:expired-capacity",
            scope={"test": "expiry"},
            planned_bytes=10,
            source_digest="",
            non_target_digest="",
            rollback_expires_at="2026-07-02T00:00:00Z",
        )

        class HealthyDisk:
            total = 10_000
            used = 8_000
            free = 2_000

        with patch(
            "packages.application.warehouse_recovery_policy.shutil.disk_usage",
            return_value=HealthyDisk(),
        ):
            policy._reserve_capacity(  # noqa: SLF001
                operation_id="expired-capacity",
                required_bytes=10,
                target_root=self.runtime_dir,
            )
        clock_value["now"] = start + timedelta(hours=7)

        class DegradedDisk:
            total = 10_000
            used = 9_250
            free = 750

        with patch(
            "packages.application.warehouse_recovery_policy.shutil.disk_usage",
            return_value=DegradedDisk(),
        ):
            status = policy.capacity_status()
        self.assertTrue(status["degraded"])
        self.assertFalse(status["hard_stop"])
        self.assertEqual(status["reserved_bytes"], 0)
        self.assertEqual(status["expired_reservation_count"], 1)
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT state FROM "
                    "sheet_vitrina_v1_recovery_capacity_reservations "
                    "WHERE operation_id='expired-capacity'"
                ).fetchone()[0],
                "active",
            )

        class PostWriteLowDisk:
            total = 10_000
            used = 9_600
            free = 400

        with patch(
            "packages.application.warehouse_recovery_policy.shutil.disk_usage",
            side_effect=[HealthyDisk(), PostWriteLowDisk()],
        ):
            with self.assertRaisesRegex(
                RecoveryPolicyError,
                "post-write reserve",
            ):
                policy.prepare_t1(
                    mutation_kind="supplier_cost_queue_replay",
                    closure_kind="shipment",
                    plan_fingerprint="sha256:post-write-stop",
                    scope={"test": "post-write"},
                    before_images=[
                        {
                            "table": "bounded_rows",
                            "key": {"row_id": "target"},
                            "before": {
                                "row_id": "target",
                                "value": "before",
                                "untouched": "stable",
                            },
                            "after": {
                                "row_id": "target",
                                "value": "after",
                                "untouched": "stable",
                            },
                        }
                    ],
                )

    def test_orphan_scanner_covers_all_owned_file_families(self) -> None:
        root = self.runtime_dir / "warehouse-recovery" / "domain-checkpoints"
        root.mkdir(parents=True)
        suffixes = (
            ".sqlite3",
            ".sqlite3.zst",
            ".manifest.json",
            ".tmp",
            ".sqlite3-wal",
            ".sqlite3-shm",
            ".sqlite3-journal",
            ".undo.json",
        )
        for index, suffix in enumerate(suffixes):
            (root / f"orphan-{index}{suffix}").write_bytes(b"fixture")
        report = self.registry.scan_orphans()
        kinds = {item["kind"] for item in report["files"]}
        self.assertTrue(
            {"raw", "zst", "manifest", "temp", "wal", "shm", "journal", "undo"}
            <= kinds
        )
        self.assertEqual(report["orphan_count"], len(suffixes))
        foreign = root / "operator-note.txt"
        foreign.write_text("do not classify", encoding="utf-8")
        report = self.registry.scan_orphans()
        self.assertIn(str(foreign.resolve()), report["foreign_non_target_paths"])
        self.assertNotIn(str(foreign.resolve()), report["unclassified_paths"])

    def test_orphan_scanner_separates_pre_policy_backup_baseline(self) -> None:
        activation = datetime(2026, 7, 26, 18, 43, 50, tzinfo=timezone.utc)
        backups = self.runtime_dir / "backups"
        backups.mkdir()
        legacy = backups / "legacy-pre-policy.sqlite3"
        legacy.write_bytes(b"legacy")
        before_activation = (activation - timedelta(seconds=1)).timestamp()
        os.utime(legacy, (before_activation, before_activation))
        policy = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime_dir,
            db_path=self.runtime.db_path,
            operational_reserve_bytes=0,
            clock=lambda: activation,
        )
        policy.prepare_t1(
            mutation_kind="supplier_cost_queue_replay",
            closure_kind="shipment",
            plan_fingerprint="sha256:activation-boundary",
            scope={"shipment_id": "baseline"},
            before_images=[
                {
                    "table": "bounded_rows",
                    "key": {"row_id": "target"},
                    "before": {
                        "row_id": "target",
                        "value": "before",
                        "untouched": "stable",
                    },
                    "after": {
                        "row_id": "target",
                        "value": "after",
                        "untouched": "stable",
                    },
                }
            ],
        )
        report = policy.scan_orphans()
        self.assertEqual(report["status"], "clean")
        self.assertEqual(
            report["pre_policy_legacy_paths"],
            [str(legacy.resolve())],
        )
        self.assertEqual(
            report["policy_activation_at"],
            "2026-07-26T18:43:50.000000Z",
        )

        new_or_touched = backups / "post-policy.sqlite3-wal"
        new_or_touched.write_bytes(b"new")
        after_activation = (activation + timedelta(seconds=1)).timestamp()
        os.utime(new_or_touched, (after_activation, after_activation))
        report = policy.scan_orphans()
        self.assertEqual(report["status"], "attention_required")
        self.assertIn(
            str(new_or_touched.resolve()),
            report["unclassified_paths"],
        )

        os.utime(legacy, (after_activation, after_activation))
        report = policy.scan_orphans()
        self.assertIn(str(legacy.resolve()), report["unclassified_paths"])

    def test_scanner_detects_registered_corruption(self) -> None:
        operation = self.registry.prepare_t2(
            mutation_kind="manual_warehouse_sync",
            plan_fingerprint="sha256:corrupt-artifact",
            scope={"test": "corruption"},
            source_digest="sha256:source",
            non_target_digest="",
            source_watermarks={"test": "corruption"},
            schema_revision="smoke-v1",
        )
        checkpoint = next(
            Path(item["path"])
            for item in operation["artifacts"]
            if item["artifact_kind"] == "domain_checkpoint"
        )
        checkpoint.write_bytes(b"corrupt")
        report = self.registry.scan_orphans()
        self.assertEqual(report["status"], "attention_required")
        self.assertEqual(
            report["quarantine_candidates"][0]["operation_id"],
            operation["operation_id"],
        )

    def test_failed_pre_mutation_canary_releases_only_owned_temp(self) -> None:
        def fail_before_checkpoint(_: str, boundary: str) -> None:
            if boundary == "before_checkpoint_write":
                raise RuntimeError("synthetic canary checkpoint failure")

        faulting = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime_dir,
            db_path=self.runtime.db_path,
            operational_reserve_bytes=0,
            fault_injector=fail_before_checkpoint,
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "synthetic canary checkpoint failure",
        ):
            faulting.prepare_t2(
                mutation_kind="manual_warehouse_sync",
                plan_fingerprint="sha256:failed-canary-release",
                scope={"canary": True, "business_mutation": False},
                source_digest="sha256:source",
                non_target_digest="sha256:non-target",
                source_watermarks={"canary": True},
                schema_revision="smoke-v1",
            )
        failed = next(
            operation
            for operation in self.registry.list_operations(limit=100)
            if operation["plan_fingerprint"]
            == "sha256:failed-canary-release"
        )
        partial = (
            self.registry.checkpoint_root
            / f"{failed['operation_id']}.sqlite3.tmp"
        )
        partial.write_bytes(b"partial-canary-checkpoint")
        release = self.registry.release_failed_canary_pre_mutations()
        self.assertEqual(release["released_operation_ids"], [failed["operation_id"]])
        self.assertFalse(partial.exists())
        self.assertEqual(
            self.registry.get_operation(failed["operation_id"])["lifecycle"],
            RecoveryState.RELEASED.value,
        )
        self.assertEqual(self.registry.scan_orphans()["status"], "clean")

    def test_expired_retention_is_fingerprint_gated(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        clock_value = {"now": start}
        policy = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime_dir,
            db_path=self.runtime.db_path,
            operational_reserve_bytes=0,
            clock=lambda: clock_value["now"],
        )
        prepared = policy.prepare_t1(
            mutation_kind="calculation_parameters_update",
            closure_kind="date",
            plan_fingerprint="sha256:retention",
            scope={"effective_date": "2026-07-01"},
            before_images=[
                {
                    "table": "bounded_rows",
                    "key": {"row_id": "target"},
                    "before": {
                        "row_id": "target",
                        "value": "before",
                        "untouched": "stable",
                    },
                    "after": {
                        "row_id": "target",
                        "value": "before",
                        "untouched": "stable",
                    },
                }
            ],
            rollback_retention_days=1,
        )
        policy.begin_mutation(prepared["operation_id"])
        policy.retain(prepared["operation_id"], after_digest="sha256:same")
        rolled_back = policy.prepare_t2(
            mutation_kind="manual_warehouse_sync",
            plan_fingerprint="sha256:retention-rolled-back",
            scope={"test": "retention-rolled-back"},
            source_digest="sha256:source",
            non_target_digest="",
            source_watermarks={"test": "retention"},
            schema_revision="smoke-v1",
        )
        rolled_back_path = next(
            Path(item["path"])
            for item in rolled_back["artifacts"]
            if item["artifact_kind"] == "domain_checkpoint"
        )
        policy.begin_mutation(rolled_back["operation_id"])
        policy.fail_recoverable(
            rolled_back["operation_id"],
            error="synthetic rollback",
            next_action="rollback_t2",
        )
        policy.rollback_t2(rolled_back["operation_id"], reason="retention-smoke")
        failed = policy.prepare_t1(
            mutation_kind="warehouse_archival_estimate",
            closure_kind="sku_date",
            plan_fingerprint="sha256:retention-failed",
            scope={"test": "retention-failed"},
            before_images=[
                {
                    "table": "bounded_rows",
                    "key": {"row_id": "target"},
                    "before": {
                        "row_id": "target",
                        "value": "before",
                        "untouched": "stable",
                    },
                    "after": {
                        "row_id": "target",
                        "value": "before",
                        "untouched": "stable",
                    },
                }
            ],
            rollback_retention_days=1,
        )
        policy.fail_recoverable(
            failed["operation_id"],
            error="retain failed evidence",
            next_action="operator_retry",
        )
        clock_value["now"] = start + timedelta(days=15)
        plan = policy.release_expired()
        self.assertTrue(plan["would_change"])
        self.assertIn(rolled_back["operation_id"], plan["operation_ids"])
        self.assertNotIn(failed["operation_id"], plan["operation_ids"])
        with self.assertRaises(RecoveryPolicyError):
            policy.release_expired(
                apply=True,
                plan_fingerprint="sha256:wrong",
            )
        result = policy.release_expired(
            apply=True,
            plan_fingerprint=plan["fingerprint"],
        )
        self.assertEqual(result["status"], "applied")
        operation = policy.get_operation(prepared["operation_id"])
        self.assertEqual(operation["lifecycle"], RecoveryState.RELEASED.value)
        rolled_back_operation = policy.get_operation(rolled_back["operation_id"])
        self.assertEqual(
            rolled_back_operation["lifecycle"],
            RecoveryState.ROLLED_BACK.value,
        )
        self.assertFalse(rolled_back_path.exists())
        self.assertEqual(
            policy.get_operation(failed["operation_id"])["lifecycle"],
            RecoveryState.FAILED_RECOVERABLE.value,
        )
        self.assertEqual(policy.scan_orphans()["status"], "clean")


if __name__ == "__main__":
    unittest.main()
