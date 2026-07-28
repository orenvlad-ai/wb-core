#!/usr/bin/env python3
"""Deterministic storage registry/outbox/migration safety smoke."""

from __future__ import annotations

from contextlib import closing
from datetime import date
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.finance_storage_sqlite_open_inventory import inventory
from packages.application.finance_raw_storage import (
    FinanceOutboxConsumer,
    FinanceRawLiveTailBridge,
    FinanceRawIngestor,
    FinanceStorageError,
    InjectedFinanceStorageFault,
    ensure_operational_schema,
    ensure_raw_schema,
    shadow_compare_week,
    storage_health,
)
from packages.application.finance_storage_migration import (
    FinanceStorageCandidateBuilder,
    FinanceStorageCoherentSnapshot,
    FinanceStorageCutover,
    FinanceStorageMigrationError,
    FinanceStorageMigrationPlanner,
    FinanceStorageRollback,
    FinanceStorageShadowRunner,
    FinanceStorageShadowVerifier,
    InjectedMigrationFault,
    _accessible_fd_paths,
    _unknown_snapshot_writers,
)
from packages.application.partner_report import PartnerReportBlock
from packages.application.business_data_write_barrier import (
    acquire_barrier,
    confirm_barrier_hold,
    mark_barrier_restoring,
    release_barrier,
)
from packages.application.storage_registry import (
    StoreRegistry,
    StorageRegistryError,
    atomic_write_manifest,
    build_manifest,
    manifest_payload,
    parse_manifest,
)


DEPLOYED_SHA = "a" * 40


def _raw_row(report: int, rrd: int, *, week: int = 1) -> dict[str, object]:
    return {
        "reportId": report,
        "rrdId": rrd,
        "reportType": 1,
        "nmId": 100000 + rrd,
        "vendorCode": f"SKU-{rrd}",
        "sku": f"BAR-{rrd}",
        "docTypeName": "Продажа",
        "sellerOperName": "Логистика",
        "quantity": 1,
        "forPay": 10 + week,
    }


def _create_monolith(runtime_dir: Path, *, rows: int = 5) -> Path:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / "registry_upload_runtime.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE wb_finance_weekly_raw_rows (
                seller_id TEXT NOT NULL,
                report_id TEXT NOT NULL,
                rrd_id TEXT NOT NULL,
                report_type INTEGER,
                week_start TEXT NOT NULL,
                week_end TEXT NOT NULL,
                nm_id TEXT,
                vendor_code TEXT,
                barcode TEXT,
                doc_type_name TEXT,
                seller_oper_name TEXT,
                row_hash TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(seller_id,report_id,rrd_id)
            );
            CREATE INDEX wb_finance_raw_by_week
            ON wb_finance_weekly_raw_rows(seller_id,week_start,week_end);
            CREATE INDEX wb_finance_raw_by_sku_week
            ON wb_finance_weekly_raw_rows(seller_id,nm_id,week_start,week_end);
            CREATE TABLE wb_finance_weekly_reports (
                seller_id TEXT NOT NULL,
                report_id TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                PRIMARY KEY(seller_id,report_id)
            );
            CREATE TABLE wb_finance_weekly_sync (
                seller_id TEXT NOT NULL,
                week_start TEXT NOT NULL,
                week_end TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY(seller_id,week_start,week_end)
            );
            CREATE TABLE wb_finance_weekly_aggregates (
                seller_id TEXT NOT NULL,
                week_start TEXT NOT NULL,
                week_end TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                PRIMARY KEY(seller_id,week_start,week_end)
            );
            CREATE TABLE sheet_vitrina_v1_warehouse_functional_balances (
                version_id TEXT NOT NULL,
                warehouse_key TEXT NOT NULL,
                nm_id INTEGER NOT NULL,
                quantity_text TEXT NOT NULL,
                PRIMARY KEY(version_id,warehouse_key,nm_id)
            );
            CREATE TABLE unrelated_runtime_state (
                state_key TEXT PRIMARY KEY,
                payload BLOB NOT NULL
            );
            """
        )
        for index in range(1, rows + 1):
            raw = _raw_row(10 + (index // 3), 1000 + index)
            raw_json = json.dumps(
                raw,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            import hashlib

            row_hash = hashlib.sha256(raw_json.encode()).hexdigest()
            conn.execute(
                """INSERT INTO wb_finance_weekly_raw_rows VALUES(
                   'canonical',?,?,?,?,?,?,?,?,?,?,?,?,?,?
                   )""",
                (
                    str(raw["reportId"]),
                    str(raw["rrdId"]),
                    1,
                    "2026-01-05",
                    "2026-01-11",
                    str(raw["nmId"]),
                    str(raw["vendorCode"]),
                    str(raw["sku"]),
                    str(raw["docTypeName"]),
                    str(raw["sellerOperName"]),
                    row_hash,
                    raw_json,
                    "2026-01-12T00:00:00Z",
                    "2026-01-12T00:00:00Z",
                ),
            )
        conn.execute(
            "INSERT INTO wb_finance_weekly_reports VALUES('canonical','10',?)", (rows,)
        )
        conn.execute(
            """INSERT INTO wb_finance_weekly_sync
               VALUES('canonical','2026-01-05','2026-01-11','completed')"""
        )
        conn.execute(
            """INSERT INTO wb_finance_weekly_aggregates
               VALUES('canonical','2026-01-05','2026-01-11','{"sales":5}')"""
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances
               VALUES('v1','wb','101','5.0000')"""
        )
        conn.execute(
            "INSERT INTO unrelated_runtime_state VALUES('keep',?)",
            (sqlite3.Binary(b"\x00\x01do-not-change"),),
        )
        conn.commit()
    return path


def _create_maintenance_hold(runtime_dir: Path) -> None:
    path = runtime_dir / ".business-data-maintenance.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "business_data_maintenance_v1",
                "phase": "held",
                "held_at": "2026-01-01T00:00:00Z",
                "hold_readback": {"quiet": True},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _create_verified_snapshot(runtime_dir: Path) -> Path:
    snapshot = FinanceStorageCoherentSnapshot(
        runtime_dir,
        deployed_sha=DEPLOYED_SHA,
        repo_root=ROOT,
    )
    plan = snapshot.build_plan()
    if not plan["snapshot_allowed_by_machine_preflight"]:
        raise AssertionError(f"snapshot fixture plan is blocked: {plan['blockers']}")
    window_id = str(plan["target_snapshot"]["window_id"])
    fingerprint = str(plan["fingerprint"])
    acquire_barrier(
        runtime_dir,
        window_id=window_id,
        window_kind="snapshot",
        plan_fingerprint=fingerprint,
        approval_reference="fixture-program-authorization",
        actor="smoke",
        reason="coherent snapshot fixture",
    )
    _create_maintenance_hold(runtime_dir)
    maintenance_state = json.loads(
        (runtime_dir / ".business-data-maintenance.json").read_text(
            encoding="utf-8"
        )
    )
    confirm_barrier_hold(
        runtime_dir,
        window_id=window_id,
        plan_fingerprint=fingerprint,
        maintenance_state=maintenance_state,
    )
    created = snapshot.create(
        reviewed_plan=plan,
        expected_fingerprint=fingerprint,
        approval_reference="fixture-program-authorization",
    )
    manifest_path = Path(str(created["snapshot_manifest_path"]))
    mark_barrier_restoring(
        runtime_dir,
        window_id=window_id,
        plan_fingerprint=fingerprint,
    )
    release_barrier(
        runtime_dir,
        window_id=window_id,
        plan_fingerprint=fingerprint,
        actor="smoke",
        reason="fixture controls restored",
        restore_readback={
            "status": "restored",
            "captured_at": "2026-01-01T00:00:01Z",
            "exact_prior_state_restored": True,
            "control_signature": "sha256:" + ("c" * 64),
            "auto_updates": {
                "revision": 2,
                "master_desired": True,
            },
        },
    )
    verified = snapshot.verify_integrity(manifest_path)
    if verified["status"] != "integrity_verified":
        raise AssertionError("coherent snapshot fixture integrity did not verify")
    return manifest_path


class StoreRegistrySmoke(unittest.TestCase):
    def test_inert_default_and_fail_closed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw)
            monolith = _create_monolith(runtime, rows=1)
            registry = StoreRegistry(runtime)
            manifest = registry.load(require_files=True)
            self.assertTrue(manifest.implicit)
            self.assertEqual(registry.resolve("finance_raw"), monolith.resolve())
            self.assertEqual(registry.resolve("operational"), monolith.resolve())
            self.assertFalse(registry.manifest_path.exists())
            with registry.session(
                "finance_raw", mode="ro", operation="registry_smoke"
            ) as conn:
                self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertGreaterEqual(registry.status()["open_observation_count"], 1)

            split = build_manifest(
                state="shadow",
                canonical_source="monolith",
                generation_epoch="epoch-1",
                raw_generation_id="raw-1",
                raw_relative_path="generations/epoch-1/raw.sqlite3",
                raw_watermark="10",
                operational_generation_id="op-1",
                operational_relative_path="generations/epoch-1/op.sqlite3",
                operational_watermark="digest",
                rollback_generation_id="monolith",
                source_fingerprint="sha256:" + "b" * 64,
                created_at="2026-01-01T00:00:00Z",
            )
            atomic_write_manifest(registry.manifest_path, split)
            self.assertEqual(registry.load().manifest_sha256, split.manifest_sha256)
            payload = manifest_payload(split)
            payload["operational"]["generation_epoch"] = "mixed"
            payload["manifest_sha256"] = "sha256:" + "0" * 64
            with self.assertRaises(StorageRegistryError):
                parse_manifest(payload)


class OutboxSmoke(unittest.TestCase):
    def test_ingest_fault_matrix(self) -> None:
        for fault_at in (
            "before_transaction",
            "after_rows_before_outbox",
            "before_raw_commit",
            "after_raw_commit",
        ):
            with self.subTest(fault_at=fault_at), tempfile.TemporaryDirectory() as raw:
                runtime = Path(raw)
                _create_monolith(runtime, rows=1)
                registry = StoreRegistry(runtime)
                ingestor = FinanceRawIngestor(registry)
                with self.assertRaises(InjectedFinanceStorageFault):
                    ingestor.ingest_batch(
                        [_raw_row(19, 1901)],
                        source_identity=f"fixture:{fault_at}",
                        source_sha256="sha256:" + "9" * 64,
                        week_start="2025-12-29",
                        week_end="2026-01-04",
                        fault_at=fault_at,
                    )
                with registry.session(
                    "finance_raw",
                    mode="ro",
                    operation="ingest_fault_matrix_read",
                ) as conn:
                    committed = int(
                        conn.execute(
                            """SELECT COUNT(*) FROM finance_raw_ingest_batches
                               WHERE status='committed'"""
                        ).fetchone()[0]
                    )
                    outbox = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM finance_raw_outbox"
                        ).fetchone()[0]
                    )
                expected = 1 if fault_at == "after_raw_commit" else 0
                self.assertEqual((committed, outbox), (expected, expected))
                if expected:
                    retry = ingestor.ingest_batch(
                        [_raw_row(19, 1901)],
                        source_identity=f"fixture:{fault_at}",
                        source_sha256="sha256:" + "9" * 64,
                        week_start="2025-12-29",
                        week_end="2026-01-04",
                    )
                    self.assertEqual(retry.status, "no_op")

    def test_atomic_ingest_at_least_once_and_poison_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw)
            _create_monolith(runtime, rows=1)
            registry = StoreRegistry(runtime)
            ingestor = FinanceRawIngestor(registry)
            result = ingestor.ingest_batch(
                [_raw_row(20, 2001), _raw_row(20, 2002)],
                source_identity="fixture:week-1",
                source_sha256="sha256:" + "c" * 64,
                week_start=date(2026, 1, 5),
                week_end=date(2026, 1, 11),
            )
            self.assertEqual(result.status, "committed")
            retry = ingestor.ingest_batch(
                [_raw_row(20, 2001), _raw_row(20, 2002)],
                source_identity="fixture:week-1",
                source_sha256="sha256:" + "c" * 64,
                week_start="2026-01-05",
                week_end="2026-01-11",
            )
            self.assertEqual(retry.status, "no_op")

            with self.assertRaises(InjectedFinanceStorageFault):
                ingestor.ingest_batch(
                    [_raw_row(21, 2101)],
                    source_identity="fixture:week-2",
                    source_sha256="sha256:" + "d" * 64,
                    week_start="2026-01-12",
                    week_end="2026-01-18",
                    fault_at="before_raw_commit",
                )
            with registry.session(
                "finance_raw", mode="ro", operation="outbox_smoke_read"
            ) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM finance_raw_ingest_batches"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM finance_raw_outbox").fetchone()[0],
                    1,
                )

            def apply_event(
                conn: sqlite3.Connection, payload: dict[str, object]
            ) -> tuple[int, str]:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS derived_fixture(event_id TEXT PRIMARY KEY)"
                )
                conn.execute(
                    "INSERT INTO derived_fixture(event_id) VALUES(?)",
                    (str(payload["event_id"]),),
                )
                return int(payload["row_count"]), str(payload["rows_digest"])

            consumer = FinanceOutboxConsumer(registry, apply_event=apply_event)
            for fault_at in (
                "after_outbox_read",
                "after_inbox_before_apply",
                "after_apply_before_receipt",
                "before_operational_commit",
            ):
                with self.subTest(fault_at=fault_at):
                    with self.assertRaises(InjectedFinanceStorageFault):
                        consumer.consume_next(fault_at=fault_at)
                    with registry.session(
                        "operational",
                        mode="ro",
                        operation="consumer_fault_matrix_read",
                    ) as conn:
                        tables = {
                            str(row[0])
                            for row in conn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            ).fetchall()
                        }
                        receipts = (
                            int(
                                conn.execute(
                                    """SELECT COUNT(*) FROM
                                       finance_operational_receipts"""
                                ).fetchone()[0]
                            )
                            if "finance_operational_receipts" in tables
                            else 0
                        )
                        self.assertEqual(receipts, 0)
            with self.assertRaises(InjectedFinanceStorageFault):
                consumer.consume_next(fault_at="after_operational_commit_before_ack")
            consumed = consumer.consume_next()
            self.assertIsNotNone(consumed)
            assert consumed is not None
            self.assertTrue(consumed.duplicate)
            with registry.session(
                "operational", mode="ro", operation="outbox_smoke_operational"
            ) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM derived_fixture").fetchone()[0], 1
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM finance_operational_receipts"
                    ).fetchone()[0],
                    1,
                )
            health = storage_health(registry)
            self.assertEqual(health["consumer_lag_events"], 0)
            self.assertTrue(health["rollback_ready"])

            second = ingestor.ingest_batch(
                [_raw_row(22, 2201)],
                source_identity="fixture:week-3",
                source_sha256="sha256:" + "e" * 64,
                week_start="2026-01-19",
                week_end="2026-01-25",
            )
            with self.assertRaises(InjectedFinanceStorageFault):
                consumer.consume_next(fault_at="before_outbox_ack_commit")
            ack_retry = consumer.consume_next()
            self.assertIsNotNone(ack_retry)
            assert ack_retry is not None
            self.assertTrue(ack_retry.duplicate)
            self.assertEqual(ack_retry.event_id, second.event_id)
            poison_event = ingestor.ingest_batch(
                [_raw_row(23, 2301)],
                source_identity="fixture:week-4",
                source_sha256="sha256:" + "8" * 64,
                week_start="2026-01-26",
                week_end="2026-02-01",
            )

            def poison(
                _conn: sqlite3.Connection, _payload: dict[str, object]
            ) -> tuple[int, str]:
                raise RuntimeError("fixture poison")

            poison_consumer = FinanceOutboxConsumer(
                registry, apply_event=poison, poison_threshold=3
            )
            for _attempt in range(3):
                with self.assertRaises(RuntimeError):
                    poison_consumer.consume_next()
            with registry.session(
                "operational", mode="ro", operation="outbox_smoke_dead_letter"
            ) as conn:
                row = conn.execute(
                    """SELECT status,attempt_count FROM finance_operational_dead_letters
                       WHERE event_id=?""",
                    (poison_event.event_id,),
                ).fetchone()
                self.assertEqual(dict(row), {"status": "action_required", "attempt_count": 3})

    def test_live_tail_bridge_restart_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "source"
            _create_monolith(runtime, rows=1)
            registry = StoreRegistry(runtime)
            ingested = FinanceRawIngestor(registry).ingest_batch(
                [_raw_row(30, 3001), _raw_row(30, 3002)],
                source_identity="fixture:tail-1",
                source_sha256="sha256:" + "f" * 64,
                week_start="2026-02-02",
                week_end="2026-02-08",
            )
            candidate_path = Path(raw) / "candidate.sqlite3"
            with closing(
                sqlite3.connect(candidate_path, isolation_level=None)
            ) as destination:
                destination.row_factory = sqlite3.Row
                ensure_raw_schema(destination)
                destination.commit()
                bridge = FinanceRawLiveTailBridge()
                with registry.session(
                    "finance_raw",
                    mode="rw",
                    operation="live_tail_gap_fixture",
                ) as source_write:
                    source_write.execute(
                        "UPDATE finance_raw_outbox SET sequence_no=2 WHERE event_id=?",
                        (ingested.event_id,),
                    )
                    source_write.commit()
                with registry.session(
                    "finance_raw",
                    mode="ro",
                    operation="live_tail_gap_smoke",
                ) as source:
                    with self.assertRaisesRegex(
                        FinanceStorageError,
                        "gap/reorder",
                    ):
                        bridge.plan_next(
                            source=source,
                            destination=destination,
                        )
                with registry.session(
                    "finance_raw",
                    mode="rw",
                    operation="live_tail_gap_restore_fixture",
                ) as source_write:
                    source_write.execute(
                        "UPDATE finance_raw_outbox SET sequence_no=1 WHERE event_id=?",
                        (ingested.event_id,),
                    )
                    source_write.commit()
                with registry.session(
                    "finance_raw",
                    mode="ro",
                    operation="live_tail_smoke_plan",
                ) as source:
                    plan = bridge.plan_next(
                        source=source,
                        destination=destination,
                    )
                    self.assertEqual(plan["event_id"], ingested.event_id)
                    self.assertEqual(plan["row_count"], 2)
                    with self.assertRaises(InjectedFinanceStorageFault):
                        bridge.apply_next(
                            source=source,
                            destination=destination,
                            fault_at="after_rows_before_outbox",
                        )
                    self.assertEqual(
                        destination.execute(
                            "SELECT COUNT(*) FROM finance_raw_rows"
                        ).fetchone()[0],
                        0,
                    )
                    with self.assertRaises(InjectedFinanceStorageFault):
                        bridge.apply_next(
                            source=source,
                            destination=destination,
                            fault_at="after_destination_commit",
                        )
                    self.assertIsNone(
                        bridge.apply_next(
                            source=source,
                            destination=destination,
                        )
                    )
                self.assertEqual(
                    destination.execute(
                        "SELECT COUNT(*) FROM finance_raw_rows"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    destination.execute(
                        "SELECT last_sequence_no FROM finance_raw_bridge_cursors"
                    ).fetchone()[0],
                    ingested.sequence_no,
                )
                repeated = FinanceRawIngestor(registry).ingest_batch(
                    [_raw_row(30, 3001), _raw_row(30, 3002)],
                    source_identity="fixture:tail-2-same-rows",
                    source_sha256="sha256:" + "1" * 64,
                    week_start="2026-02-02",
                    week_end="2026-02-08",
                )
                with registry.session(
                    "finance_raw",
                    mode="ro",
                    operation="live_tail_reused_rows_smoke",
                ) as source:
                    replay = bridge.apply_next(
                        source=source,
                        destination=destination,
                    )
                    self.assertIsNotNone(replay)
                    self.assertEqual(
                        replay["sequence_no"],
                        repeated.sequence_no,
                    )
                self.assertEqual(
                    destination.execute(
                        "SELECT COUNT(*) FROM finance_raw_rows"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    destination.execute(
                        "SELECT COUNT(*) FROM finance_raw_batch_rows"
                    ).fetchone()[0],
                    4,
                )
                self.assertEqual(
                    destination.execute(
                        "SELECT COUNT(*) FROM finance_raw_current_rows"
                    ).fetchone()[0],
                    2,
                )


class MigrationSmoke(unittest.TestCase):
    def test_snapshot_plan_blocks_active_business_writer_service(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            _create_monolith(runtime, rows=1)
            active_service = {
                "unit": "wb-core-sheet-vitrina-closure-retry.service",
                "return_code": 0,
                "load_state": "loaded",
                "active_state": "activating",
                "sub_state": "start",
                "unit_file_state": "static",
                "main_pid": 4242,
                "result": "success",
                "exec_main_status": "0",
                "last_trigger": "",
                "next_trigger": "",
            }
            with mock.patch(
                "packages.application.finance_storage_migration._systemd_inventory",
                return_value=[active_service],
            ):
                plan = FinanceStorageCoherentSnapshot(
                    runtime,
                    deployed_sha=DEPLOYED_SHA,
                    repo_root=ROOT,
                ).build_plan()
            self.assertFalse(
                plan["snapshot_allowed_by_machine_preflight"]
            )
            blocker = next(
                row
                for row in plan["blockers"]
                if row["code"] == "active_business_writer_service"
            )
            self.assertEqual(blocker["services"], [active_service])

    def test_snapshot_plan_allows_exact_drainable_autoanswers_oneshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            _create_monolith(runtime, rows=1)
            service = {
                "unit": "wb-core-autoanswers-worker.service",
                "return_code": 0,
                "load_state": "loaded",
                "active_state": "activating",
                "sub_state": "start",
                "unit_file_state": "static",
                "main_pid": 4242,
                "result": "success",
                "exec_main_status": "0",
                "last_trigger": "",
                "next_trigger": "",
            }
            timer = {
                "unit": "wb-core-autoanswers-worker.timer",
                "return_code": 0,
                "load_state": "loaded",
                "active_state": "active",
                "sub_state": "running",
                "unit_file_state": "enabled",
                "main_pid": 0,
                "result": "success",
                "exec_main_status": "",
                "last_trigger": "now",
                "next_trigger": "",
            }
            readonly_service = {
                **service,
                "unit": "wb-core-autoanswers-readonly-sync.service",
                "main_pid": 4243,
            }
            readonly_timer = {
                **timer,
                "unit": "wb-core-autoanswers-readonly-sync.timer",
            }
            with mock.patch(
                "packages.application.finance_storage_migration._systemd_inventory",
                return_value=[
                    service,
                    timer,
                    readonly_service,
                    readonly_timer,
                ],
            ):
                plan = FinanceStorageCoherentSnapshot(
                    runtime,
                    deployed_sha=DEPLOYED_SHA,
                    repo_root=ROOT,
                ).build_plan()
            self.assertTrue(plan["snapshot_allowed_by_machine_preflight"])
            self.assertEqual(plan["blockers"], [])
            drainable = plan["writers_and_timers"][
                "drainable_active_services"
            ]
            self.assertEqual(
                {
                    (
                        item["unit"],
                        item["paired_timer"]["unit"],
                    )
                    for item in drainable
                },
                {
                    (service["unit"], timer["unit"]),
                    (readonly_service["unit"], readonly_timer["unit"]),
                },
            )
            self.assertEqual(
                plan["writers_and_timers"]["blocking_active_services"],
                [],
            )

            mismatched_timer = {
                **timer,
                "unit_file_state": "disabled",
            }
            with mock.patch(
                "packages.application.finance_storage_migration._systemd_inventory",
                return_value=[service, mismatched_timer],
            ):
                blocked = FinanceStorageCoherentSnapshot(
                    runtime,
                    deployed_sha=DEPLOYED_SHA,
                    repo_root=ROOT,
                ).build_plan()
            self.assertFalse(
                blocked["snapshot_allowed_by_machine_preflight"]
            )
            blocker = next(
                row
                for row in blocked["blockers"]
                if row["code"] == "active_business_writer_service"
            )
            self.assertEqual(blocker["services"], [service])

    def test_candidate_plan_ignores_transient_systemd_execution_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            _create_monolith(runtime, rows=2)
            snapshot_manifest = _create_verified_snapshot(runtime)
            planner = FinanceStorageMigrationPlanner(
                runtime,
                chunk_size=1,
                deployed_sha=DEPLOYED_SHA,
                repo_root=ROOT,
                require_exact_allocations=False,
                source_snapshot_manifest=snapshot_manifest,
            )
            unit = {
                "unit": "wb-core-wb-finance-weekly.service",
                "return_code": 0,
                "load_state": "loaded",
                "active_state": "activating",
                "sub_state": "start",
                "unit_file_state": "static",
                "main_pid": 4242,
                "result": "success",
                "exec_main_status": "0",
                "last_trigger": "now",
                "next_trigger": "",
            }
            idle_unit = {
                **unit,
                "active_state": "inactive",
                "sub_state": "dead",
                "main_pid": 0,
                "result": "exit-code",
                "exec_main_status": "1",
                "last_trigger": "later",
            }
            with mock.patch(
                "packages.application.finance_storage_migration._systemd_inventory",
                return_value=[unit],
            ):
                active_plan = planner.build_plan()
            with mock.patch(
                "packages.application.finance_storage_migration._systemd_inventory",
                return_value=[idle_unit],
            ):
                idle_plan = planner.build_plan()
            self.assertEqual(
                active_plan["fingerprint"],
                idle_plan["fingerprint"],
            )
            self.assertNotEqual(
                active_plan["writers_and_timers"]["systemd_units"],
                idle_plan["writers_and_timers"]["systemd_units"],
            )
            self.assertEqual(
                active_plan["fingerprint_contract"]["version"],
                "wb_core_finance_storage_split_plan_fingerprint_v2",
            )

            disabled_unit = {
                **idle_unit,
                "unit_file_state": "disabled",
            }
            with mock.patch(
                "packages.application.finance_storage_migration._systemd_inventory",
                return_value=[disabled_unit],
            ):
                disabled_plan = planner.build_plan()
            self.assertNotEqual(
                active_plan["fingerprint"],
                disabled_plan["fingerprint"],
            )

            with mock.patch(
                "packages.application.finance_storage_migration._systemd_inventory",
                return_value=[idle_unit],
            ):
                result = FinanceStorageCandidateBuilder(
                    planner,
                    expected_fingerprint=active_plan["fingerprint"],
                    approval_reference="fixture-human-gate",
                ).apply()
            self.assertEqual(result["status"], "candidate_ready")

    def test_dry_run_idempotent_resume_and_non_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            source = _create_monolith(runtime, rows=5)
            before = source.read_bytes()
            planner = FinanceStorageMigrationPlanner(
                runtime,
                chunk_size=2,
                deployed_sha=DEPLOYED_SHA,
                repo_root=ROOT,
                require_exact_allocations=False,
            )
            first = planner.build_plan()
            second = planner.build_plan()
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            self.assertEqual(first["raw"]["row_count"], 5)
            self.assertEqual(first["chunks"]["chunk_count"], 3)
            self.assertEqual(
                first["query_only_contract"]["destination_bytes_created"], 0
            )
            self.assertFalse((runtime / "generations").exists())
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(first["direct_sqlite_open_inventory"]["status"], "ok")
            self.assertEqual(
                first["source"]["identity"]["path"],
                str(source.resolve()),
            )
            self.assertFalse(
                first["target_generation"]["destination_preflight"]["raw"][
                    "exists"
                ]
            )
            self.assertTrue(
                any(
                    "sqlite_autoindex_wb_finance_weekly_raw_rows_1" in detail
                    for detail in first["raw"]["query_plan"]["primary_identity_lookup"]
                )
            )
            self.assertTrue(
                any(
                    "wb_finance_raw_by_week" in detail
                    for detail in first["raw"]["query_plan"]["week_lookup"]
                )
            )
            with self.assertRaisesRegex(
                FinanceStorageMigrationError,
                "verified immutable coherent snapshot",
            ):
                FinanceStorageCandidateBuilder(
                    planner,
                    expected_fingerprint=first["fingerprint"],
                    approval_reference="fixture-human-gate",
                ).apply()
            self.assertFalse((runtime / "generations").exists())
            snapshot_manifest = _create_verified_snapshot(runtime)
            planner = FinanceStorageMigrationPlanner(
                runtime,
                chunk_size=2,
                deployed_sha=DEPLOYED_SHA,
                repo_root=ROOT,
                require_exact_allocations=False,
                source_snapshot_manifest=snapshot_manifest,
            )
            first = planner.build_plan()
            second = planner.build_plan()
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            self.assertEqual(
                first["source"]["logical_store"],
                "coherent_snapshot",
            )
            self.assertEqual(
                first["source"]["full_integrity_check"]["status"],
                "ok",
            )

            with self.assertRaises(InjectedMigrationFault):
                FinanceStorageCandidateBuilder(
                    planner,
                    expected_fingerprint=first["fingerprint"],
                    approval_reference="fixture-human-gate",
                    fault_after_chunks=1,
                ).apply()
            result = FinanceStorageCandidateBuilder(
                planner,
                expected_fingerprint=first["fingerprint"],
                approval_reference="fixture-human-gate",
            ).apply()
            self.assertEqual(result["status"], "candidate_ready")
            self.assertFalse(result["global_manifest_switched"])
            self.assertTrue(result["old_monolith_retained"])
            self.assertFalse(
                result["business_data_maintenance_hold_required_for_backfill"]
            )
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse((runtime / "storage_generation_manifest.json").exists())
            candidate_manifest_path = Path(result["candidate_manifest_path"])
            candidate_manifest = parse_manifest(
                json.loads(candidate_manifest_path.read_text())
            )
            self.assertEqual(candidate_manifest.state, "shadow")
            self.assertEqual(candidate_manifest.canonical_source, "monolith")
            op_path = runtime / candidate_manifest.operational.relative_path
            with closing(sqlite3.connect(op_path)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT payload FROM unrelated_runtime_state WHERE state_key='keep'"
                    ).fetchone()[0],
                    b"\x00\x01do-not-change",
                )
                self.assertEqual(
                    conn.execute(
                        """SELECT COUNT(*) FROM finance_storage_migration_chunks
                           WHERE status='verified'"""
                    ).fetchone()[0],
                    8,
                )
            raw_path = runtime / candidate_manifest.raw.relative_path
            with (
                closing(
                    sqlite3.connect(
                        f"file:{source.resolve()}?mode=ro",
                        uri=True,
                    )
                ) as source_shadow,
                closing(
                    sqlite3.connect(
                        f"file:{raw_path.resolve()}?mode=ro",
                        uri=True,
                    )
                ) as candidate_shadow,
            ):
                source_shadow.execute("PRAGMA query_only=ON")
                candidate_shadow.execute("PRAGMA query_only=ON")
                comparison = shadow_compare_week(
                    source_conn=source_shadow,
                    shadow_conn=candidate_shadow,
                    seller_id="canonical",
                    week_start="2026-01-05",
                    week_end="2026-01-11",
                )
                self.assertEqual(comparison["status"], "match")
                self.assertEqual(comparison["source_row_count"], 5)
                self.assertEqual(comparison["shadow_row_count"], 5)
                self.assertTrue(comparison["source_query_plan"])
                self.assertTrue(comparison["shadow_query_plan"])
            shadow_runner = FinanceStorageShadowRunner(
                runtime,
                candidate_manifest_path=candidate_manifest_path,
                plan_fingerprint=first["fingerprint"],
                approval_reference="fixture-human-gate",
            )
            activated = shadow_runner.activate()
            self.assertTrue(activated["enabled"])
            reconciled = shadow_runner.reconcile_legacy_current(
                chunk_size=2
            )
            self.assertEqual(reconciled["missing_current_rows"], 0)
            self.assertEqual(reconciled["source_row_count"], 5)
            self.assertEqual(reconciled["reused_count"], 5)
            event = FinanceRawIngestor(StoreRegistry(runtime)).ingest_batch(
                [_raw_row(31, 3101)],
                source_identity="fixture:shadow-tail",
                source_sha256="sha256:" + "7" * 64,
                week_start="2026-02-09",
                week_end="2026-02-15",
            )
            tail = shadow_runner.apply_live_tail(max_events=10)
            self.assertEqual(tail["status"], "caught_up")
            self.assertEqual(tail["destination_cursor"], event.sequence_no)
            self.assertEqual(tail["lag_events"], 0)
            self.assertEqual(tail["duplicate_event_ids"], 0)
            self.assertEqual(tail["duplicate_sequences"], 0)
            retry_tail = shadow_runner.apply_live_tail(max_events=10)
            self.assertEqual(retry_tail["applied_events"], 0)
            with closing(sqlite3.connect(raw_path)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM finance_raw_rows"
                    ).fetchone()[0],
                    6,
                )
            deactivated = shadow_runner.deactivate(
                reason="shadow smoke completed"
            )
            self.assertFalse(deactivated["enabled"])
            with closing(sqlite3.connect(raw_path)) as conn:
                conn.execute(
                    "DELETE FROM finance_raw_rows WHERE batch_sequence_no=1"
                )
                conn.commit()
            with self.assertRaisesRegex(
                FinanceStorageMigrationError,
                "verified raw chunk drifted",
            ):
                FinanceStorageCandidateBuilder(
                    planner,
                    expected_fingerprint=first["fingerprint"],
                    approval_reference="fixture-human-gate",
                ).apply()
            atomic_write_manifest(
                runtime / "storage_generation_manifest.json",
                candidate_manifest,
            )
            selected = StoreRegistry(runtime)
            with selected.session(
                "finance_raw",
                mode="ro",
                operation="candidate_identity_smoke",
            ) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT generation_id FROM finance_raw_schema_meta WHERE singleton=1"
                    ).fetchone()[0],
                    candidate_manifest.raw.generation_id,
                )
            with closing(sqlite3.connect(raw_path)) as conn:
                conn.execute(
                    """UPDATE finance_raw_schema_meta
                       SET generation_epoch='mixed-generation' WHERE singleton=1"""
                )
                conn.commit()
            with self.assertRaises(StorageRegistryError):
                selected.connect(
                    "finance_raw",
                    mode="ro",
                    operation="candidate_identity_mismatch_smoke",
                )

    def test_capacity_shortfall_happens_before_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            _create_monolith(runtime, rows=2)
            snapshot_manifest = _create_verified_snapshot(runtime)
            planner = FinanceStorageMigrationPlanner(
                runtime,
                chunk_size=1,
                deployed_sha=DEPLOYED_SHA,
                repo_root=ROOT,
                require_exact_allocations=False,
                source_snapshot_manifest=snapshot_manifest,
            )
            real = planner.build_plan()
            race_plan = planner.build_plan()
            real_vfs = os.statvfs(runtime)
            fake_vfs = type(
                "FakeVfs",
                (),
                {"f_bavail": 1, "f_frsize": 4096},
            )()
            with mock.patch(
                "packages.application.finance_storage_migration.os.statvfs",
                return_value=fake_vfs,
            ):
                blocked = planner.build_plan()
                self.assertFalse(blocked["capacity"]["sufficient"])
                with self.assertRaises(FinanceStorageMigrationError):
                    FinanceStorageCandidateBuilder(
                        planner,
                        expected_fingerprint=blocked["fingerprint"],
                        approval_reference="fixture-human-gate",
                    ).apply()
            self.assertFalse((runtime / "generations").exists())
            self.assertTrue(real["capacity"]["checked_before_destination_creation"])
            with mock.patch(
                "packages.application.finance_storage_migration.os.statvfs",
                side_effect=[real_vfs, fake_vfs],
            ):
                with self.assertRaisesRegex(
                    FinanceStorageMigrationError,
                    "capacity reservation raced",
                ):
                    FinanceStorageCandidateBuilder(
                        planner,
                        expected_fingerprint=race_plan["fingerprint"],
                        approval_reference="fixture-human-gate",
                    ).apply()
            self.assertFalse((runtime / "generations").exists())
            source_path = runtime / "registry_upload_runtime.sqlite3"
            with closing(sqlite3.connect(source_path)) as conn:
                conn.execute(
                    """UPDATE wb_finance_weekly_raw_rows
                       SET raw_json='{"source":"drift"}' WHERE rowid=1"""
                )
                conn.commit()
            after_live_drift = planner.build_plan()
            self.assertEqual(
                after_live_drift["fingerprint"],
                real["fingerprint"],
            )
            snapshot_database = Path(
                json.loads(snapshot_manifest.read_text(encoding="utf-8"))[
                    "database_path"
                ]
            )
            with closing(sqlite3.connect(snapshot_database)) as conn:
                conn.execute(
                    """UPDATE wb_finance_weekly_raw_rows
                       SET raw_json='{"snapshot":"drift"}' WHERE rowid=1"""
                )
                conn.commit()
            with self.assertRaisesRegex(
                FinanceStorageMigrationError,
                "verified immutable coherent snapshot",
            ):
                planner.build_plan()
            self.assertFalse((runtime / "generations").exists())

    def test_final_cutover_recopy_outbox_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            source_path = _create_monolith(runtime, rows=2)
            snapshot_manifest = _create_verified_snapshot(runtime)
            planner = FinanceStorageMigrationPlanner(
                runtime,
                chunk_size=1,
                deployed_sha=DEPLOYED_SHA,
                repo_root=ROOT,
                require_exact_allocations=False,
                source_snapshot_manifest=snapshot_manifest,
            )
            candidate_plan = planner.build_plan()
            candidate_result = FinanceStorageCandidateBuilder(
                planner,
                expected_fingerprint=candidate_plan["fingerprint"],
                approval_reference="fixture-human-gate",
            ).apply()
            candidate_manifest_path = Path(
                candidate_result["candidate_manifest_path"]
            )
            candidate_manifest = parse_manifest(
                json.loads(candidate_manifest_path.read_text())
            )
            shadow = FinanceStorageShadowRunner(
                runtime,
                candidate_manifest_path=candidate_manifest_path,
                plan_fingerprint=candidate_plan["fingerprint"],
                approval_reference="fixture-human-gate",
            )
            shadow.activate()
            shadow.reconcile_legacy_current(chunk_size=1)
            event = FinanceRawIngestor(StoreRegistry(runtime)).ingest_batch(
                [_raw_row(10, 1001), _raw_row(10, 1002)],
                source_identity="fixture:cutover-tail",
                source_sha256="sha256:" + "6" * 64,
                week_start="2026-01-05",
                week_end="2026-01-11",
            )
            self.assertEqual(
                shadow.apply_live_tail(max_events=10)["destination_cursor"],
                event.sequence_no,
            )
            verification = FinanceStorageShadowVerifier(
                runtime,
                candidate_manifest_path=candidate_manifest_path,
                candidate_plan_fingerprint=candidate_plan["fingerprint"],
                minimum_observation_seconds=0,
            ).verify()
            self.assertEqual(
                verification["status"],
                "ready",
                verification,
            )
            self.assertEqual(verification["mismatch_count"], 0)
            cutover = FinanceStorageCutover(
                runtime,
                candidate_manifest_path=candidate_manifest_path,
                candidate_plan_fingerprint=candidate_plan["fingerprint"],
                deployed_sha=DEPLOYED_SHA,
            )
            generous_vfs = type(
                "GenerousVfs",
                (),
                {"f_bavail": 10 * 1024 * 1024, "f_frsize": 4096},
            )()
            with mock.patch(
                "packages.application.finance_storage_migration.os.statvfs",
                return_value=generous_vfs,
            ):
                cutover_plan = cutover.build_plan()
            self.assertTrue(
                cutover_plan["apply_allowed_by_machine_preflight"],
                cutover_plan["blockers"],
            )
            tampered_plan = json.loads(json.dumps(cutover_plan))
            tampered_plan["candidate"]["bridge_cursor"] = 999
            with self.assertRaisesRegex(
                FinanceStorageMigrationError,
                "reviewed Finance cutover plan",
            ):
                cutover.apply(
                    reviewed_plan=tampered_plan,
                    expected_fingerprint=cutover_plan["fingerprint"],
                    approval_reference="fixture-human-gate",
                )
            fingerprint = str(cutover_plan["fingerprint"])
            window_id = (
                "final-cutover-"
                + fingerprint.removeprefix("sha256:")[:20]
            )
            acquire_barrier(
                runtime,
                window_id=window_id,
                window_kind="final_cutover",
                plan_fingerprint=fingerprint,
                approval_reference="fixture-human-gate",
                actor="smoke",
                reason="final cutover smoke",
            )
            _create_maintenance_hold(runtime)
            maintenance_state = json.loads(
                (runtime / ".business-data-maintenance.json").read_text()
            )
            confirm_barrier_hold(
                runtime,
                window_id=window_id,
                plan_fingerprint=fingerprint,
                maintenance_state=maintenance_state,
            )
            cutover_result = cutover.apply(
                reviewed_plan=cutover_plan,
                expected_fingerprint=fingerprint,
                approval_reference="fixture-human-gate",
            )
            self.assertTrue(cutover_result["global_manifest_switched"])
            self.assertTrue(cutover_result["old_monolith_retained"])
            self.assertEqual(
                cutover_result["outbox_reconciliation"]["lag_events"],
                0,
            )
            retry = cutover.apply(
                reviewed_plan=cutover_plan,
                expected_fingerprint=fingerprint,
                approval_reference="fixture-human-gate",
            )
            self.assertTrue(retry["idempotent"])
            selected = StoreRegistry(runtime)
            manifest = selected.load(require_files=True)
            self.assertEqual(manifest.state, "cutover")
            self.assertEqual(manifest.canonical_source, "split")
            self.assertTrue(source_path.is_file())
            with selected.session(
                "finance_raw",
                mode="ro",
                operation="cutover_raw_readback",
            ) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM finance_raw_rows"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM finance_raw_outbox"
                    ).fetchone()[0],
                    1,
                )
            with selected.session(
                "operational",
                mode="ro",
                operation="cutover_operational_readback",
            ) as conn:
                self.assertEqual(
                    conn.execute(
                        """SELECT payload FROM unrelated_runtime_state
                           WHERE state_key='keep'"""
                    ).fetchone()[0],
                    b"\x00\x01do-not-change",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM finance_operational_receipts"
                    ).fetchone()[0],
                    1,
                )
                self.assertIsNone(
                    conn.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type='table'
                             AND name='wb_finance_weekly_raw_rows'"""
                    ).fetchone()
                )
            partner = PartnerReportBlock(
                runtime,
                seller_id="seller-1",
            )
            with partner._connect() as conn:  # noqa: SLF001
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM wb_finance_weekly_raw_rows"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    conn.execute(
                        """SELECT COUNT(*) FROM finance_raw_store.finance_raw_rows"""
                    ).fetchone()[0],
                    2,
                )
            mark_barrier_restoring(
                runtime,
                window_id=window_id,
                plan_fingerprint=fingerprint,
            )
            release_barrier(
                runtime,
                window_id=window_id,
                plan_fingerprint=fingerprint,
                actor="smoke",
                reason="fixture controls restored",
                restore_readback={
                    "status": "restored",
                    "captured_at": "2026-01-01T00:00:02Z",
                    "exact_prior_state_restored": True,
                    "control_signature": "sha256:" + ("d" * 64),
                    "auto_updates": {
                        "revision": 3,
                        "master_desired": True,
                    },
                },
            )
            rollback = FinanceStorageRollback(
                runtime,
                deployed_sha=DEPLOYED_SHA,
            )
            with mock.patch(
                "packages.application.finance_storage_migration.os.statvfs",
                return_value=generous_vfs,
            ):
                rollback_plan = rollback.build_plan()
            self.assertTrue(
                rollback_plan["prepare_allowed_by_machine_preflight"],
                rollback_plan["blockers"],
            )
            rollback_candidate = rollback.prepare(
                reviewed_plan=rollback_plan,
                expected_fingerprint=rollback_plan["fingerprint"],
                approval_reference="fixture-human-gate",
            )
            post_cutover = FinanceRawIngestor(
                StoreRegistry(runtime)
            ).ingest_batch(
                [_raw_row(50, 5001)],
                source_identity="fixture:post-cutover-write",
                source_sha256="sha256:" + "5" * 64,
                week_start="2026-03-02",
                week_end="2026-03-08",
            )
            self.assertEqual(post_cutover.sequence_no, 2)
            with selected.session(
                "operational",
                mode="rw",
                operation="post_cutover_operational_mutation",
            ) as conn:
                conn.execute(
                    """UPDATE unrelated_runtime_state
                       SET payload=? WHERE state_key='keep'""",
                    (sqlite3.Binary(b"\x02post-cutover"),),
                )
                conn.commit()
            rollback_fingerprint = str(
                rollback_plan["fingerprint"]
            )
            rollback_window = (
                "rollback-"
                + rollback_fingerprint.removeprefix("sha256:")[:20]
            )
            acquire_barrier(
                runtime,
                window_id=rollback_window,
                window_kind="rollback_drill",
                plan_fingerprint=rollback_fingerprint,
                approval_reference="fixture-human-gate",
                actor="smoke",
                reason="rollback drill smoke",
            )
            _create_maintenance_hold(runtime)
            confirm_barrier_hold(
                runtime,
                window_id=rollback_window,
                plan_fingerprint=rollback_fingerprint,
                maintenance_state=json.loads(
                    (
                        runtime / ".business-data-maintenance.json"
                    ).read_text()
                ),
            )
            rollback_result = rollback.apply(
                reviewed_plan=rollback_plan,
                expected_fingerprint=rollback_fingerprint,
                approval_reference="fixture-human-gate",
                candidate_evidence_path=Path(
                    rollback_candidate["candidate_evidence_path"]
                ),
            )
            self.assertEqual(
                rollback_result["status"],
                "rollback_complete",
            )
            self.assertEqual(
                rollback_result["raw_replay"]["latest_sequence"],
                2,
            )
            rolled_back = StoreRegistry(runtime)
            rollback_manifest = rolled_back.load(require_files=True)
            self.assertEqual(rollback_manifest.state, "monolith")
            self.assertEqual(
                rollback_manifest.raw.relative_path,
                rollback_manifest.operational.relative_path,
            )
            with rolled_back.session(
                "operational",
                mode="ro",
                operation="rollback_readback",
            ) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM wb_finance_weekly_raw_rows"
                    ).fetchone()[0],
                    3,
                )
                self.assertEqual(
                    conn.execute(
                        """SELECT payload FROM unrelated_runtime_state
                           WHERE state_key='keep'"""
                    ).fetchone()[0],
                    b"\x02post-cutover",
                )
            mark_barrier_restoring(
                runtime,
                window_id=rollback_window,
                plan_fingerprint=rollback_fingerprint,
            )
            release_barrier(
                runtime,
                window_id=rollback_window,
                plan_fingerprint=rollback_fingerprint,
                actor="smoke",
                reason="rollback controls restored",
                restore_readback={
                    "status": "restored",
                    "captured_at": "2026-01-01T00:00:03Z",
                    "exact_prior_state_restored": True,
                    "control_signature": "sha256:" + ("e" * 64),
                    "auto_updates": {
                        "revision": 4,
                        "master_desired": True,
                    },
                },
            )


class InventorySmoke(unittest.TestCase):
    def test_migrated_modules_use_registry(self) -> None:
        payload = inventory(ROOT)
        self.assertEqual(payload["parse_errors"], [])
        self.assertEqual(payload["violations"], [])
        self.assertEqual(payload["status"], "ok")

    def test_inaccessible_proc_fd_is_skipped(self) -> None:
        class DeniedFdDirectory:
            @staticmethod
            def iterdir() -> list[Path]:
                raise PermissionError("fixture denied")

        self.assertEqual(_accessible_fd_paths(DeniedFdDirectory()), [])

    def test_snapshot_writer_ownership_is_fail_closed(self) -> None:
        openers = [
            {"pid": 100, "fd": 5, "access_mode": "read_write", "comm": "http"},
            {"pid": 200, "fd": 7, "access_mode": "read_write", "comm": "sync"},
            {"pid": 300, "fd": 9, "access_mode": "read_write", "comm": "unknown"},
            {"pid": 400, "fd": 11, "access_mode": "read_only", "comm": "reader"},
        ]
        units = [
            {"unit": "wb-core-registry-http.service", "main_pid": 100},
            {"unit": "wb-core-finance-weekly-sync.service", "main_pid": 200},
        ]
        self.assertEqual(
            [
                item["pid"]
                for item in _unknown_snapshot_writers(
                    openers,
                    units,
                    hold_confirmed=False,
                )
            ],
            [300],
        )
        self.assertEqual(
            [
                item["pid"]
                for item in _unknown_snapshot_writers(
                    openers,
                    units,
                    hold_confirmed=True,
                )
            ],
            [200, 300],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
