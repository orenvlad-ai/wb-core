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
    FinanceStorageMigrationError,
    FinanceStorageMigrationPlanner,
    InjectedMigrationFault,
    _accessible_fd_paths,
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
            raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
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


class MigrationSmoke(unittest.TestCase):
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
                "maintenance hold",
            ):
                FinanceStorageCandidateBuilder(
                    planner,
                    expected_fingerprint=first["fingerprint"],
                    approval_reference="fixture-human-gate",
                ).apply()
            self.assertFalse((runtime / "generations").exists())
            _create_maintenance_hold(runtime)

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
            self.assertTrue(result["old_monolith_unchanged"])
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse((runtime / "storage_generation_manifest.json").exists())
            candidate_manifest = parse_manifest(
                json.loads(Path(result["candidate_manifest_path"]).read_text())
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
            planner = FinanceStorageMigrationPlanner(
                runtime,
                chunk_size=1,
                deployed_sha=DEPLOYED_SHA,
                repo_root=ROOT,
                require_exact_allocations=False,
            )
            real = planner.build_plan()
            _create_maintenance_hold(runtime)
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
            with self.assertRaisesRegex(
                FinanceStorageMigrationError,
                "reviewed Finance storage plan is stale",
            ):
                FinanceStorageCandidateBuilder(
                    planner,
                    expected_fingerprint=real["fingerprint"],
                    approval_reference="fixture-human-gate",
                ).apply()
            self.assertFalse((runtime / "generations").exists())


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
