#!/usr/bin/env python3
"""Deterministic storage registry/outbox/migration safety smoke."""

from __future__ import annotations

from contextlib import closing
from datetime import date
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import finance_storage_split as finance_storage_cli
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
from packages.application.finance_generation_filesystem import (
    CONTRACT_VERSION as GENERATION_FILESYSTEM_CONTRACT,
    FinanceGenerationFilesystemError,
    inspect_generation_filesystem,
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
    _atomic_write_json,
    _accessible_fd_paths,
    _plan_fingerprint,
    _unknown_snapshot_writers,
)
from packages.application.finance_storage_recovery_contract import (
    EXPECTED_RUNNER_CONTRACTS,
    FinanceStorageRecoveryContractError,
    recovery_contract,
    validate_recovery_preflight,
)
from packages.application.finance_storage_snapshot_retention import (
    ARCHIVE_MANIFEST_FILENAME,
    FinanceStorageSnapshotRetention,
    FinanceStorageSnapshotRetentionError,
    TRANSACTION_FILENAME,
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


def _digest_json(value: object) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _create_retention_snapshot(
    runtime_dir: Path,
    *,
    snapshot_id: str,
    deployed_sha: str,
    status: str = "integrity_verified",
) -> Path:
    root = (
        runtime_dir
        / "finance-storage-split-snapshots"
        / snapshot_id
    )
    root.mkdir(parents=True)
    database = root / "monolith.sqlite3"
    database.write_bytes((snapshot_id + "\n").encode("utf-8") * 32)
    os.chmod(database, 0o600)
    manifest = {
        "contract_version": (
            "wb_core_finance_storage_coherent_snapshot_v1"
        ),
        "status": status,
        "snapshot_id": snapshot_id,
        "deployed_sha": deployed_sha,
        "approval_reference": "retention-smoke",
        "database_path": str(database),
        "candidate_build_allowed": status == "integrity_verified",
    }
    manifest["evidence_fingerprint"] = _digest_json(manifest)
    manifest_path = root / "snapshot_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o600)
    return root


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
    def test_generation_filesystem_contract_binds_mount_uuid_and_options(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            runtime = base / "runtime"
            generations = runtime / "generations"
            generations.mkdir(parents=True)
            source = base / "sdc1"
            source.touch()
            by_uuid = base / "by-uuid"
            by_label = base / "by-label"
            by_uuid.mkdir()
            by_label.mkdir()
            filesystem_uuid = "284b3362-b890-431d-a7da-7f0fcd2ee0a6"
            filesystem_label = "wb-finance-gen"
            (by_uuid / filesystem_uuid).symlink_to(source)
            (by_label / filesystem_label).symlink_to(source)
            runtime_resolved = os.path.realpath(str(runtime))
            generations_resolved = os.path.realpath(str(generations))
            source_resolved = os.path.realpath(str(source))
            mountinfo = base / "mountinfo"
            mountinfo.write_text(
                "36 25 8:33 / "
                + generations_resolved
                + " rw,nosuid,nodev,noexec,noatime - ext4 "
                + source_resolved
                + " rw,errors=remount-ro\n",
                encoding="utf-8",
            )
            contract = {
                "contract_version": GENERATION_FILESYSTEM_CONTRACT,
                "path": str(generations),
                "filesystem_uuid": filesystem_uuid,
                "filesystem_label": filesystem_label,
                "filesystem_type": "ext4",
                "required_mount_options": [
                    "rw",
                    "noatime",
                    "nodev",
                    "nosuid",
                    "noexec",
                ],
                "require_distinct_device": True,
            }
            real_stat = Path.stat

            def fake_stat(
                path: Path,
                *args: object,
                **kwargs: object,
            ) -> object:
                current = real_stat(path, *args, **kwargs)
                resolved = os.path.realpath(str(path))
                payload = {
                    key: getattr(current, key)
                    for key in (
                        "st_mode",
                        "st_ino",
                        "st_dev",
                        "st_nlink",
                        "st_uid",
                        "st_gid",
                        "st_size",
                        "st_atime",
                        "st_mtime",
                        "st_ctime",
                    )
                }
                payload["st_rdev"] = int(
                    getattr(current, "st_rdev", 0)
                )
                if resolved == runtime_resolved:
                    payload["st_dev"] = 2049
                elif resolved == generations_resolved:
                    payload["st_dev"] = 2081
                elif resolved == source_resolved:
                    payload["st_mode"] = stat.S_IFBLK | 0o660
                    payload["st_rdev"] = os.makedev(8, 33)
                return SimpleNamespace(**payload)

            with (
                mock.patch(
                    "packages.application.finance_generation_filesystem."
                    "os.path.ismount",
                    return_value=True,
                ),
                mock.patch.object(
                    Path,
                    "stat",
                    autospec=True,
                    side_effect=fake_stat,
                ),
            ):
                identity = inspect_generation_filesystem(
                    runtime,
                    contract,
                    mountinfo_path=mountinfo,
                    by_uuid_root=by_uuid,
                    by_label_root=by_label,
                )
            self.assertEqual(identity["status"], "ready")
            self.assertEqual(identity["device"], 2081)
            self.assertEqual(identity["runtime_device"], 2049)
            self.assertEqual(identity["filesystem_uuid"], filesystem_uuid)
            self.assertTrue(identity["mountpoint_proven"])
            self.assertTrue(identity["distinct_device"])
            self.assertTrue(
                set(contract["required_mount_options"]).issubset(
                    identity["mount_options"]
                )
            )

            drifted = {
                **contract,
                "filesystem_uuid": (
                    "11111111-1111-4111-8111-111111111111"
                ),
            }
            with (
                mock.patch(
                    "packages.application.finance_generation_filesystem."
                    "os.path.ismount",
                    return_value=True,
                ),
                mock.patch.object(
                    Path,
                    "stat",
                    autospec=True,
                    side_effect=fake_stat,
                ),
                self.assertRaisesRegex(
                    FinanceGenerationFilesystemError,
                    "UUID/label/source identity drifted",
                ),
            ):
                inspect_generation_filesystem(
                    runtime,
                    drifted,
                    mountinfo_path=mountinfo,
                    by_uuid_root=by_uuid,
                    by_label_root=by_label,
                )

    def test_candidate_capacity_uses_generation_mount_and_mount_loss_blocks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            _create_monolith(runtime, rows=2)
            snapshot_manifest = _create_verified_snapshot(runtime)
            generations = runtime / "generations"
            generations.mkdir()
            filesystem_identity = {
                "contract_version": GENERATION_FILESYSTEM_CONTRACT,
                "path": str(generations.resolve()),
                "filesystem_uuid": (
                    "284b3362-b890-431d-a7da-7f0fcd2ee0a6"
                ),
                "filesystem_label": "wb-finance-gen",
                "filesystem_type": "ext4",
                "required_mount_options": [
                    "noatime",
                    "nodev",
                    "noexec",
                    "nosuid",
                    "rw",
                ],
                "require_distinct_device": True,
                "status": "ready",
                "source": "/dev/sdc1",
                "device": int(generations.stat().st_dev),
                "major_minor": "8:33",
                "mount_root": "/",
                "mount_options": [
                    "errors=remount-ro",
                    "noatime",
                    "nodev",
                    "noexec",
                    "nosuid",
                    "rw",
                ],
                "filesystem_block_size": 4096,
                "total_bytes": 105_087_164_416,
                "available_bytes": 103_996_661_760,
                "runtime_device": int(runtime.stat().st_dev),
                "distinct_device": True,
                "mountpoint_proven": True,
            }
            fake_vfs = SimpleNamespace(
                f_bavail=25_389_810,
                f_frsize=4096,
                f_blocks=25_656_045,
            )
            planner = FinanceStorageMigrationPlanner(
                runtime,
                chunk_size=1,
                deployed_sha=DEPLOYED_SHA,
                repo_root=ROOT,
                require_exact_allocations=False,
                source_snapshot_manifest=snapshot_manifest,
                generation_filesystem_contract={
                    "configured": True,
                },
            )
            with (
                mock.patch(
                    "packages.application.finance_storage_migration."
                    "inspect_generation_filesystem",
                    return_value=filesystem_identity,
                ),
                mock.patch(
                    "packages.application.finance_storage_migration."
                    "os.statvfs",
                    return_value=fake_vfs,
                ),
            ):
                plan = planner.build_plan()
            self.assertEqual(
                plan["capacity"]["available_bytes"],
                103_996_661_760,
            )
            self.assertEqual(
                plan["capacity"]["generation_filesystem"]["path"],
                str(generations.resolve()),
            )
            self.assertTrue(plan["capacity"]["sufficient"])
            with (
                mock.patch(
                    "packages.application.finance_storage_migration."
                    "inspect_generation_filesystem",
                    side_effect=FinanceGenerationFilesystemError(
                        "fixture mount lost"
                    ),
                ),
                self.assertRaisesRegex(
                    FinanceStorageMigrationError,
                    "fixture mount lost",
                ),
            ):
                FinanceStorageCandidateBuilder(
                    planner,
                    expected_fingerprint=plan["fingerprint"],
                    approval_reference="fixture-human-gate",
                ).apply()
            self.assertEqual(list(generations.iterdir()), [])

    def test_streamed_snapshot_plan_is_parsed_once_and_reused(self) -> None:
        fingerprint = "sha256:" + ("7" * 64)
        reviewed_plan = {
            "contract_version": (
                "wb_core_finance_storage_snapshot_plan_v1"
            ),
            "mode": "snapshot_dry_run",
            "fingerprint": fingerprint,
            "snapshot_allowed_by_machine_preflight": True,
            "target_snapshot": {
                "window_id": "snapshot-single-read-smoke",
            },
        }
        serialized_plan = json.dumps(reviewed_plan)
        plan_reads: list[str] = []
        original_read_text = Path.read_text

        def read_text_once(
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> str:
            if str(path) == "/dev/stdin":
                plan_reads.append(str(path))
                if len(plan_reads) > 1:
                    raise AssertionError(
                        "streamed reviewed plan was read more than once"
                    )
                return serialized_plan
            return original_read_text(path, *args, **kwargs)

        normalized_lease = {
            "contract_version": (
                "wb_core_finance_migration_deploy_lease_readback_v1"
            ),
            "policy": "finance_migration_global_deploy_hold_v1",
            "lease": {
                "deployed_sha": DEPLOYED_SHA,
                "task_id": "finance-single-read-smoke",
                "lease_id": "finance-single-read-smoke",
                "window_id": "finance-single-read-smoke",
                "phase": "offline-rehearsal",
                "revision": 1,
            },
            "fingerprint": "sha256:" + ("8" * 64),
        }
        snapshot = mock.Mock()
        snapshot.create.return_value = {
            "status": "captured_unverified",
        }
        with tempfile.TemporaryDirectory() as raw:
            with (
                mock.patch.object(
                    Path,
                    "read_text",
                    new=read_text_once,
                ),
                mock.patch.object(
                    finance_storage_cli,
                    "validate_finance_migration_deploy_lease",
                    return_value=normalized_lease,
                ),
                mock.patch.object(
                    finance_storage_cli,
                    "validate_generation_filesystem_contract",
                    return_value={"contract_version": "smoke"},
                ),
                mock.patch.object(
                    finance_storage_cli,
                    "inspect_generation_filesystem",
                    return_value={"status": "ready"},
                ),
                mock.patch.object(
                    finance_storage_cli,
                    "validate_recovery_preflight",
                    return_value={
                        "status": "ready",
                        "action": "snapshot-create",
                        "phase": "mutation",
                    },
                ) as recovery_preflight,
                mock.patch.object(
                    finance_storage_cli,
                    "FinanceStorageCoherentSnapshot",
                    return_value=snapshot,
                ),
                mock.patch.object(finance_storage_cli, "_emit"),
            ):
                result = finance_storage_cli.main(
                    [
                        "snapshot-create",
                        "--runtime-dir",
                        raw,
                        "--repo-root",
                        str(ROOT),
                        "--deployed-sha",
                        DEPLOYED_SHA,
                        "--snapshot-plan-file",
                        "/dev/stdin",
                        "--confirm-fingerprint",
                        fingerprint,
                        "--approval-reference",
                        "single-read-regression-smoke",
                        "--deploy-lease-json",
                        "{}",
                        "--generation-filesystem-contract-json",
                        "{}",
                    ]
                )
        self.assertEqual(result, 0)
        self.assertEqual(plan_reads, ["/dev/stdin"])
        self.assertEqual(
            recovery_preflight.call_args.kwargs["reviewed_plan"],
            reviewed_plan,
        )
        self.assertEqual(
            snapshot.create.call_args.kwargs["reviewed_plan"],
            reviewed_plan,
        )

    def test_streamed_candidate_plan_is_parsed_once_for_preflight(self) -> None:
        fingerprint = "sha256:" + ("6" * 64)
        reviewed_plan = {
            "contract_version": (
                "wb_core_finance_storage_split_plan_v1"
            ),
            "mode": "dry_run",
            "deployed_sha": DEPLOYED_SHA,
            "fingerprint": fingerprint,
            "apply_allowed_by_machine_preflight": True,
            "deploy_lease": {"transport": "reviewed-separately"},
        }
        serialized_plan = json.dumps(reviewed_plan)
        plan_reads: list[str] = []
        original_read_text = Path.read_text

        def read_text_once(
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> str:
            if str(path) == "/dev/stdin":
                plan_reads.append(str(path))
                if len(plan_reads) > 1:
                    raise AssertionError(
                        "streamed candidate plan was read more than once"
                    )
                return serialized_plan
            return original_read_text(path, *args, **kwargs)

        normalized_lease = {
            "contract_version": (
                "wb_core_finance_migration_deploy_lease_readback_v1"
            ),
            "policy": "finance_migration_global_deploy_hold_v1",
            "lease": {
                "deployed_sha": DEPLOYED_SHA,
                "task_id": "finance-candidate-read-smoke",
                "lease_id": "finance-candidate-read-smoke",
                "window_id": "finance-candidate-read-smoke",
                "phase": "offline-rehearsal",
                "revision": 1,
            },
            "fingerprint": "sha256:" + ("5" * 64),
        }
        candidate = mock.Mock()
        candidate.apply.return_value = {"status": "candidate_ready"}
        with tempfile.TemporaryDirectory() as raw:
            with (
                mock.patch.object(
                    Path,
                    "read_text",
                    new=read_text_once,
                ),
                mock.patch.object(
                    finance_storage_cli,
                    "validate_finance_migration_deploy_lease",
                    return_value=normalized_lease,
                ),
                mock.patch.object(
                    finance_storage_cli,
                    "validate_generation_filesystem_contract",
                    return_value={"contract_version": "smoke"},
                ),
                mock.patch.object(
                    finance_storage_cli,
                    "inspect_generation_filesystem",
                    return_value={"status": "ready"},
                ),
                mock.patch.object(
                    finance_storage_cli,
                    "validate_recovery_preflight",
                    return_value={
                        "status": "ready",
                        "action": "apply",
                        "phase": "mutation",
                    },
                ) as recovery_preflight,
                mock.patch.object(
                    finance_storage_cli,
                    "FinanceStorageMigrationPlanner",
                ),
                mock.patch.object(
                    finance_storage_cli,
                    "FinanceStorageCandidateBuilder",
                    return_value=candidate,
                ),
                mock.patch.object(finance_storage_cli, "_emit"),
            ):
                result = finance_storage_cli.main(
                    [
                        "apply",
                        "--runtime-dir",
                        raw,
                        "--repo-root",
                        str(ROOT),
                        "--deployed-sha",
                        DEPLOYED_SHA,
                        "--migration-plan-file",
                        "/dev/stdin",
                        "--confirm-fingerprint",
                        fingerprint,
                        "--approval-reference",
                        "candidate-single-read-smoke",
                        "--deploy-lease-json",
                        "{}",
                        "--generation-filesystem-contract-json",
                        "{}",
                    ]
                )
        self.assertEqual(result, 0)
        self.assertEqual(plan_reads, ["/dev/stdin"])
        self.assertEqual(
            recovery_preflight.call_args.kwargs["reviewed_plan"],
            reviewed_plan,
        )
        candidate.apply.assert_called_once_with()

    @staticmethod
    def _recovery_lease() -> dict[str, object]:
        return {
            "lease": {
                "lease_id": "finance-split-recovery-smoke",
                "task_id": "finance-recovery-hardening-smoke",
                "deployed_sha": DEPLOYED_SHA,
                "window_id": "finance-recovery-hardening-window",
                "phase": "offline-rehearsal",
                "revision": 1,
            }
        }

    @staticmethod
    def _recovery_capabilities() -> dict[str, bool]:
        return {
            "maintenance_restore": True,
            "barrier_release": True,
            "durable_restore_submit_status": True,
            "durable_restore_inventory": True,
            "durable_restore_resume": True,
            "restore_systemd_template": True,
        }

    def test_recovery_contract_and_pre_barrier_fail_closed_matrix(
        self,
    ) -> None:
        contract = recovery_contract(
            runner_contracts=EXPECTED_RUNNER_CONTRACTS,
            restore_job_contract=(
                "business_data_maintenance_restore_job_v1"
            ),
            restore_max_resume_sequence=3,
            downstream_capabilities=self._recovery_capabilities(),
        )
        transitions = [
            str(item["transition"]) for item in contract["transitions"]
        ]
        self.assertEqual(len(transitions), 21)
        self.assertEqual(len(set(transitions)), 21)
        self.assertIn("candidate.abort", transitions)
        self.assertTrue(contract["fail_closed_default"])
        self.assertFalse(contract["second_restore_job_allowed"])
        self.assertTrue(
            str(contract["fingerprint"]).startswith("sha256:")
        )
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            _create_monolith(runtime, rows=2)
            snapshot = FinanceStorageCoherentSnapshot(
                runtime,
                deployed_sha=DEPLOYED_SHA,
                repo_root=ROOT,
            )
            plan = snapshot.build_plan()
            plan["deploy_lease"] = self._recovery_lease()
            barrier_path = runtime / ".business-data-write-barrier.json"
            common = {
                "runtime_dir": runtime,
                "action": "snapshot-create",
                "phase": "pre_barrier",
                "deployed_sha": DEPLOYED_SHA,
                "approval_reference": "program-authorization-smoke",
                "expected_fingerprint": str(plan["fingerprint"]),
                "runner_contracts": EXPECTED_RUNNER_CONTRACTS,
                "restore_job_contract": (
                    "business_data_maintenance_restore_job_v1"
                ),
                "restore_max_resume_sequence": 3,
                "reviewed_plan": plan,
            }
            with self.assertRaisesRegex(
                FinanceStorageRecoveryContractError,
                "deploy lease",
            ):
                validate_recovery_preflight(
                    **common,
                    deploy_lease=None,
                    downstream_capabilities=(
                        self._recovery_capabilities()
                    ),
                )
            missing = self._recovery_capabilities()
            missing["durable_restore_resume"] = False
            with self.assertRaisesRegex(
                FinanceStorageRecoveryContractError,
                "durable_restore_resume",
            ):
                validate_recovery_preflight(
                    **common,
                    deploy_lease=self._recovery_lease(),
                    downstream_capabilities=missing,
                )
            with self.assertRaisesRegex(
                FinanceStorageRecoveryContractError,
                "approval reference",
            ):
                validate_recovery_preflight(
                    **{**common, "approval_reference": ""},
                    deploy_lease=self._recovery_lease(),
                    downstream_capabilities=(
                        self._recovery_capabilities()
                    ),
                )
            self.assertFalse(barrier_path.exists())
            tampered_plan = json.loads(json.dumps(plan))
            tampered_plan["target_snapshot"]["snapshot_id"] = (
                "finance-split-" + ("f" * 20)
            )
            with self.assertRaisesRegex(
                FinanceStorageRecoveryContractError,
                "deterministic fingerprint is stale",
            ):
                validate_recovery_preflight(
                    **{**common, "reviewed_plan": tampered_plan},
                    deploy_lease=self._recovery_lease(),
                    downstream_capabilities=(
                        self._recovery_capabilities()
                    ),
                )
            with self.assertRaisesRegex(
                FinanceStorageMigrationError,
                "reviewed coherent snapshot plan",
            ):
                snapshot.create(
                    reviewed_plan=tampered_plan,
                    expected_fingerprint=str(plan["fingerprint"]),
                    approval_reference="program-authorization-smoke",
                )
            self.assertFalse(barrier_path.exists())
            fresh = validate_recovery_preflight(
                **common,
                deploy_lease=self._recovery_lease(),
                downstream_capabilities=self._recovery_capabilities(),
            )
            self.assertEqual(
                fresh["boundary_classification"],
                "fresh_acquire",
            )
            self.assertFalse(barrier_path.exists())
            window_id = str(plan["target_snapshot"]["window_id"])
            acquire_barrier(
                runtime,
                window_id=window_id,
                window_kind="snapshot",
                plan_fingerprint=str(plan["fingerprint"]),
                approval_reference="program-authorization-smoke",
                actor="smoke",
                reason="preflight continuity matrix",
            )
            acquiring = validate_recovery_preflight(
                **common,
                deploy_lease=self._recovery_lease(),
                downstream_capabilities=self._recovery_capabilities(),
            )
            self.assertEqual(
                acquiring["boundary_classification"],
                "exact_idempotent_resume",
            )
            _create_maintenance_hold(runtime)
            confirm_barrier_hold(
                runtime,
                window_id=window_id,
                plan_fingerprint=str(plan["fingerprint"]),
                maintenance_state=json.loads(
                    (
                        runtime / ".business-data-maintenance.json"
                    ).read_text()
                ),
            )
            held = validate_recovery_preflight(
                **{**common, "phase": "mutation"},
                deploy_lease=self._recovery_lease(),
                downstream_capabilities=self._recovery_capabilities(),
            )
            self.assertEqual(
                held["boundary_classification"],
                "held_and_recoverable",
            )
            mark_barrier_restoring(
                runtime,
                window_id=window_id,
                plan_fingerprint=str(plan["fingerprint"]),
            )
            restoring = validate_recovery_preflight(
                **common,
                deploy_lease=self._recovery_lease(),
                downstream_capabilities=(
                    self._recovery_capabilities()
                ),
            )
            self.assertEqual(
                restoring["boundary_classification"],
                "exact_restore_release_resume",
            )

    def test_snapshot_retention_archive_first_crash_resume_and_readback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            _create_monolith(runtime, rows=2)
            backup_root = (
                runtime
                / "backups"
                / "finance-storage-split-snapshots"
            )
            backup_root.parent.mkdir(parents=True)
            stale_id = "finance-split-" + ("1" * 20)
            current_id = "finance-split-" + ("2" * 20)
            stale = _create_retention_snapshot(
                runtime,
                snapshot_id=stale_id,
                deployed_sha="b" * 40,
            )
            current = _create_retention_snapshot(
                runtime,
                snapshot_id=current_id,
                deployed_sha=DEPLOYED_SHA,
            )
            retention = FinanceStorageSnapshotRetention(
                runtime,
                deployed_sha=DEPLOYED_SHA,
                backup_root=backup_root,
                backup_reserve_bytes=0,
                minimum_root_free_bytes=0,
                require_distinct_device=False,
            )
            plan = retention.build_plan()
            self.assertTrue(plan["apply_allowed_by_machine_preflight"])
            plan["deploy_lease"] = self._recovery_lease()
            self.assertEqual(
                [
                    item["snapshot_id"]
                    for item in plan["selected_snapshots"]
                ],
                [stale_id],
            )
            self.assertEqual(
                [
                    item["snapshot_id"]
                    for item in plan["protected_snapshots"]
                ],
                [current_id],
            )
            tampered_plan = json.loads(json.dumps(plan))
            tampered_plan["unreviewed_transport"] = {"accepted": False}
            with self.assertRaisesRegex(
                FinanceStorageSnapshotRetentionError,
                "fingerprint is stale",
            ):
                retention.apply(
                    reviewed_plan=tampered_plan,
                    expected_fingerprint=str(plan["fingerprint"]),
                    approval_reference="retention-crash-smoke",
                )
            with self.assertRaisesRegex(
                FinanceStorageRecoveryContractError,
                "deterministic fingerprint is stale",
            ):
                validate_recovery_preflight(
                    runtime,
                    action="snapshot-retention-apply",
                    phase="pre_barrier",
                    deployed_sha=DEPLOYED_SHA,
                    approval_reference="retention-crash-smoke",
                    expected_fingerprint=str(plan["fingerprint"]),
                    deploy_lease=self._recovery_lease(),
                    runner_contracts=EXPECTED_RUNNER_CONTRACTS,
                    restore_job_contract=(
                        "business_data_maintenance_restore_job_v1"
                    ),
                    restore_max_resume_sequence=3,
                    downstream_capabilities=(
                        self._recovery_capabilities()
                    ),
                    reviewed_plan=tampered_plan,
                )
            recovery = validate_recovery_preflight(
                runtime,
                action="snapshot-retention-apply",
                phase="pre_barrier",
                deployed_sha=DEPLOYED_SHA,
                approval_reference="retention-crash-smoke",
                expected_fingerprint=str(plan["fingerprint"]),
                deploy_lease=self._recovery_lease(),
                runner_contracts=EXPECTED_RUNNER_CONTRACTS,
                restore_job_contract=(
                    "business_data_maintenance_restore_job_v1"
                ),
                restore_max_resume_sequence=3,
                downstream_capabilities=self._recovery_capabilities(),
                reviewed_plan=plan,
            )
            self.assertEqual(recovery["boundary_classification"], "not_required")
            self.assertEqual(
                recovery["relevant_transitions"],
                [
                    "snapshot_retention.archive",
                    "snapshot_retention.release",
                ],
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "injected fault after archive verification",
            ):
                retention.apply(
                    reviewed_plan=plan,
                    expected_fingerprint=str(plan["fingerprint"]),
                    approval_reference="retention-crash-smoke",
                    fault_after_archive_verified=True,
                )
            archive = backup_root / stale_id
            self.assertTrue(stale.is_dir())
            self.assertTrue(
                (archive / ARCHIVE_MANIFEST_FILENAME).is_file()
            )
            transaction = json.loads(
                (archive / TRANSACTION_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(transaction["phase"], "archive_verified")
            with self.assertRaisesRegex(
                RuntimeError,
                "injected fault after source removal",
            ):
                retention.apply(
                    reviewed_plan=plan,
                    expected_fingerprint=str(plan["fingerprint"]),
                    approval_reference="retention-crash-smoke",
                    fault_after_source_removed=True,
                )
            self.assertFalse(stale.exists())
            transaction = json.loads(
                (archive / TRANSACTION_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(transaction["phase"], "partial_source_release")
            result = retention.apply(
                reviewed_plan=plan,
                expected_fingerprint=str(plan["fingerprint"]),
                approval_reference="retention-crash-smoke",
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["archived_snapshot_count"], 1)
            self.assertEqual(
                result["snapshots"][0]["continuity"],
                "post_source_removal_finalized",
            )
            self.assertFalse(stale.exists())
            self.assertTrue(current.is_dir())
            self.assertTrue(
                (
                    runtime / "registry_upload_runtime.sqlite3"
                ).is_file()
            )
            readback = retention.readback(
                reviewed_plan=plan,
                expected_fingerprint=str(plan["fingerprint"]),
            )
            self.assertEqual(readback["status"], "readback_verified")
            self.assertTrue(readback["capacity_sufficient"])
            self.assertFalse(readback["live_monolith_touched"])
            self.assertFalse(readback["split_generation_touched"])
            transaction = json.loads(
                (archive / TRANSACTION_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(transaction["phase"], "source_released")
            repeated = retention.apply(
                reviewed_plan=plan,
                expected_fingerprint=str(plan["fingerprint"]),
                approval_reference="retention-crash-smoke",
            )
            self.assertTrue(repeated["snapshots"][0]["idempotent"])
            self.assertFalse(stale.exists())
            self.assertTrue(retention.audit_path.is_file())

    def test_snapshot_retention_blocks_unknown_files_and_same_device(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            _create_monolith(runtime, rows=1)
            backup_root = (
                runtime
                / "backups"
                / "finance-storage-split-snapshots"
            )
            backup_root.parent.mkdir(parents=True)
            snapshot = _create_retention_snapshot(
                runtime,
                snapshot_id="finance-split-" + ("3" * 20),
                deployed_sha="b" * 40,
            )
            with self.assertRaisesRegex(
                FinanceStorageSnapshotRetentionError,
                "distinct backup device",
            ):
                FinanceStorageSnapshotRetention(
                    runtime,
                    deployed_sha=DEPLOYED_SHA,
                    backup_root=backup_root,
                ).build_plan()
            (snapshot / "unexpected.bin").write_bytes(b"unsafe")
            retention = FinanceStorageSnapshotRetention(
                runtime,
                deployed_sha=DEPLOYED_SHA,
                backup_root=backup_root,
                backup_reserve_bytes=0,
                minimum_root_free_bytes=0,
                require_distinct_device=False,
            )
            with self.assertRaisesRegex(
                FinanceStorageSnapshotRetentionError,
                "unknown files",
            ):
                retention.build_plan()

    def test_snapshot_retention_blocks_post_plan_unknown_file_before_release(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            _create_monolith(runtime, rows=1)
            backup_root = (
                runtime
                / "backups"
                / "finance-storage-split-snapshots"
            )
            backup_root.parent.mkdir(parents=True)
            snapshot = _create_retention_snapshot(
                runtime,
                snapshot_id="finance-split-" + ("4" * 20),
                deployed_sha="b" * 40,
            )
            retention = FinanceStorageSnapshotRetention(
                runtime,
                deployed_sha=DEPLOYED_SHA,
                backup_root=backup_root,
                backup_reserve_bytes=0,
                minimum_root_free_bytes=0,
                require_distinct_device=False,
            )
            plan = retention.build_plan()
            (snapshot / "appeared-after-plan.bin").write_bytes(b"unsafe")
            with self.assertRaisesRegex(
                FinanceStorageSnapshotRetentionError,
                "unknown files before release",
            ):
                retention.apply(
                    reviewed_plan=plan,
                    expected_fingerprint=str(plan["fingerprint"]),
                    approval_reference="retention-post-plan-drift-smoke",
                )
            self.assertTrue(snapshot.is_dir())
            self.assertTrue(
                (snapshot / "snapshot_manifest.json").is_file()
            )
            self.assertTrue((snapshot / "monolith.sqlite3").is_file())

    def test_snapshot_recaptures_bounded_data_drift_after_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            source_path = _create_monolith(runtime, rows=2)
            snapshot = FinanceStorageCoherentSnapshot(
                runtime,
                deployed_sha=DEPLOYED_SHA,
                repo_root=ROOT,
            )
            plan = snapshot.build_plan()
            plan["deploy_lease"] = self._recovery_lease()
            planned_identity = dict(plan["source"]["identity"])
            with closing(sqlite3.connect(source_path)) as conn:
                conn.execute(
                    "UPDATE unrelated_runtime_state SET payload=? "
                    "WHERE state_key='keep'",
                    (sqlite3.Binary(b"\x00\x01bounded-plan-drift"),),
                )
                conn.commit()
            fingerprint = str(plan["fingerprint"])
            window_id = str(plan["target_snapshot"]["window_id"])
            acquire_barrier(
                runtime,
                window_id=window_id,
                window_kind="snapshot",
                plan_fingerprint=fingerprint,
                approval_reference="program-authorization-smoke",
                actor="smoke",
                reason="held source recapture",
            )
            _create_maintenance_hold(runtime)
            confirm_barrier_hold(
                runtime,
                window_id=window_id,
                plan_fingerprint=fingerprint,
                maintenance_state=json.loads(
                    (
                        runtime / ".business-data-maintenance.json"
                    ).read_text()
                ),
            )
            created = snapshot.create(
                reviewed_plan=plan,
                expected_fingerprint=fingerprint,
                approval_reference="program-authorization-smoke",
            )
            actual_identity = dict(
                created["snapshot"]["source_identity"]
            )
            self.assertNotEqual(actual_identity, planned_identity)
            self.assertEqual(
                created["snapshot"]["planned_source_identity"],
                planned_identity,
            )
            self.assertEqual(
                created["snapshot"]["held_source_recapture"][
                    "classification"
                ],
                "bounded_data_drift_recaptured",
            )
            self.assertTrue(
                created["snapshot"]["held_source_recapture"][
                    "capacity_sufficient"
                ]
            )
            status = snapshot.read_status(
                reviewed_plan=plan,
                expected_fingerprint=fingerprint,
                approval_reference="program-authorization-smoke",
            )
            self.assertTrue(status["idempotent"])
            self.assertEqual(
                status["snapshot"]["source_identity"],
                actual_identity,
            )

    def test_snapshot_rejects_schema_drift_after_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            source_path = _create_monolith(runtime, rows=2)
            snapshot = FinanceStorageCoherentSnapshot(
                runtime,
                deployed_sha=DEPLOYED_SHA,
                repo_root=ROOT,
            )
            plan = snapshot.build_plan()
            with closing(sqlite3.connect(source_path)) as conn:
                conn.execute(
                    "ALTER TABLE unrelated_runtime_state "
                    "ADD COLUMN unexpected_schema TEXT"
                )
                conn.commit()
            fingerprint = str(plan["fingerprint"])
            window_id = str(plan["target_snapshot"]["window_id"])
            acquire_barrier(
                runtime,
                window_id=window_id,
                window_kind="snapshot",
                plan_fingerprint=fingerprint,
                approval_reference="program-authorization-smoke",
                actor="smoke",
                reason="schema drift rejection",
            )
            _create_maintenance_hold(runtime)
            confirm_barrier_hold(
                runtime,
                window_id=window_id,
                plan_fingerprint=fingerprint,
                maintenance_state=json.loads(
                    (
                        runtime / ".business-data-maintenance.json"
                    ).read_text()
                ),
            )
            with self.assertRaisesRegex(
                FinanceStorageMigrationError,
                "stable identity drifted.*schema_digest",
            ):
                snapshot.create(
                    reviewed_plan=plan,
                    expected_fingerprint=fingerprint,
                    approval_reference="program-authorization-smoke",
                )

    def test_snapshot_copy_crash_continuity(self) -> None:
        for fault_boundary in (
            "partial_copy",
            "database_without_manifest",
        ):
            with self.subTest(
                fault_boundary=fault_boundary
            ), tempfile.TemporaryDirectory() as raw:
                runtime = Path(raw) / "runtime"
                _create_monolith(runtime, rows=2)
                snapshot = FinanceStorageCoherentSnapshot(
                    runtime,
                    deployed_sha=DEPLOYED_SHA,
                    repo_root=ROOT,
                )
                plan = snapshot.build_plan()
                fingerprint = str(plan["fingerprint"])
                window_id = str(
                    plan["target_snapshot"]["window_id"]
                )
                acquire_barrier(
                    runtime,
                    window_id=window_id,
                    window_kind="snapshot",
                    plan_fingerprint=fingerprint,
                    approval_reference="program-authorization-smoke",
                    actor="smoke",
                    reason="snapshot copy crash rehearsal",
                )
                _create_maintenance_hold(runtime)
                confirm_barrier_hold(
                    runtime,
                    window_id=window_id,
                    plan_fingerprint=fingerprint,
                    maintenance_state=json.loads(
                        (
                            runtime
                            / ".business-data-maintenance.json"
                        ).read_text()
                    ),
                )
                if fault_boundary == "partial_copy":
                    real_replace = os.replace

                    def fault_side_effect(
                        source: object,
                        destination: object,
                    ) -> None:
                        if Path(destination).name == "monolith.sqlite3":
                            raise InjectedMigrationFault(
                                "disconnect at partial_copy"
                            )
                        real_replace(source, destination)

                    patch_target = (
                        "packages.application.finance_storage_migration."
                        "os.replace"
                    )
                else:

                    def fault_side_effect(
                        destination: object,
                        payload: object,
                    ) -> None:
                        if (
                            Path(destination).name
                            == "snapshot_manifest.json"
                        ):
                            raise InjectedMigrationFault(
                                "disconnect at database_without_manifest"
                            )
                        _atomic_write_json(destination, payload)

                    patch_target = (
                        "packages.application.finance_storage_migration."
                        "_atomic_write_json"
                    )
                with mock.patch(
                    patch_target,
                    side_effect=fault_side_effect,
                ):
                    with self.assertRaises(InjectedMigrationFault):
                        snapshot.create(
                            reviewed_plan=plan,
                            expected_fingerprint=fingerprint,
                            approval_reference=(
                                "program-authorization-smoke"
                            ),
                        )
                resumed = snapshot.create(
                    reviewed_plan=plan,
                    expected_fingerprint=fingerprint,
                    approval_reference="program-authorization-smoke",
                )
                self.assertEqual(
                    (
                        resumed["snapshot"].get("continuity") or {}
                    ).get("classification"),
                    (
                        "partial_rebuilt"
                        if fault_boundary == "partial_copy"
                        else "database_without_manifest_validated"
                    ),
                )
                self.assertFalse(
                    (
                        Path(
                            str(
                                plan["target_snapshot"][
                                    "snapshot_root"
                                ]
                            )
                        )
                        / ".monolith.sqlite3.partial"
                    ).exists()
                )

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
                "wb_core_finance_storage_split_plan_fingerprint_v3",
            )
            self.assertEqual(
                active_plan["fingerprint_contract"][
                    "transport_fields_rechecked_not_hashed"
                ],
                ["deploy_lease"],
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

    def test_candidate_orders_foreign_key_parents_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            source = _create_monolith(runtime, rows=3)
            with closing(sqlite3.connect(source)) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(
                    """
                    CREATE TABLE z_dependency_parent (
                        parent_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL
                    );
                    CREATE TABLE a_dependency_child (
                        child_id TEXT PRIMARY KEY,
                        parent_id TEXT NOT NULL REFERENCES
                            z_dependency_parent(parent_id),
                        payload TEXT NOT NULL
                    );
                    INSERT INTO z_dependency_parent
                    VALUES('parent-1','parent-payload');
                    INSERT INTO a_dependency_child
                    VALUES('child-1','parent-1','child-payload');
                    """
                )
                conn.commit()
            snapshot_manifest = _create_verified_snapshot(runtime)
            planner = FinanceStorageMigrationPlanner(
                runtime,
                chunk_size=2,
                deployed_sha=DEPLOYED_SHA,
                repo_root=ROOT,
                require_exact_allocations=False,
                source_snapshot_manifest=snapshot_manifest,
            )
            plan = planner.build_plan()
            table_order = plan["operational_copy"]["table_order"]
            self.assertLess(
                table_order.index("z_dependency_parent"),
                table_order.index("a_dependency_child"),
            )
            fault_after_parent = (
                table_order.index("z_dependency_parent") + 1
            )
            with self.assertRaisesRegex(
                InjectedMigrationFault,
                "after_verified_operational_tables",
            ):
                FinanceStorageCandidateBuilder(
                    planner,
                    expected_fingerprint=plan["fingerprint"],
                    approval_reference="fixture-human-gate",
                    fault_after_operational_tables=fault_after_parent,
                ).apply()
            generation_root = (
                runtime
                / plan["target_generation"]["generation_directory"]
            )
            self.assertFalse(
                (
                    generation_root
                    / "candidate_generation_manifest.json"
                ).exists()
            )
            self.assertFalse(
                (runtime / "storage_generation_manifest.json").exists()
            )
            operational_path = generation_root / "operational.sqlite3"
            with closing(sqlite3.connect(operational_path)) as conn:
                verified = {
                    str(row[0])
                    for row in conn.execute(
                        """SELECT chunk_id
                           FROM finance_storage_migration_chunks
                           WHERE store_name='operational'
                             AND status='verified'"""
                    ).fetchall()
                }
                self.assertIn(
                    "table:z_dependency_parent",
                    verified,
                )
                self.assertNotIn(
                    "table:a_dependency_child",
                    verified,
                )

            result = FinanceStorageCandidateBuilder(
                planner,
                expected_fingerprint=plan["fingerprint"],
                approval_reference="fixture-human-gate",
            ).apply()
            self.assertEqual(result["status"], "candidate_ready")
            self.assertEqual(
                result["operational_foreign_key_check"],
                "ok",
            )
            with closing(sqlite3.connect(operational_path)) as conn:
                self.assertEqual(
                    conn.execute(
                        """SELECT child.child_id,parent.payload
                           FROM a_dependency_child AS child
                           JOIN z_dependency_parent AS parent
                             ON parent.parent_id=child.parent_id"""
                    ).fetchone(),
                    ("child-1", "parent-payload"),
                )
                self.assertEqual(
                    conn.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall(),
                    [],
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
            reviewed_candidate_plan = json.loads(
                json.dumps(candidate_plan)
            )
            reviewed_candidate_plan["deploy_lease"] = (
                self._recovery_lease()
            )
            self.assertEqual(
                _plan_fingerprint(reviewed_candidate_plan),
                candidate_plan["fingerprint"],
            )
            with self.assertRaisesRegex(
                FinanceStorageRecoveryContractError,
                "requires an exact reviewed plan",
            ):
                validate_recovery_preflight(
                    runtime,
                    action="apply",
                    phase="mutation",
                    deployed_sha=DEPLOYED_SHA,
                    approval_reference="fixture-human-gate",
                    expected_fingerprint=candidate_plan["fingerprint"],
                    deploy_lease=self._recovery_lease(),
                    runner_contracts=EXPECTED_RUNNER_CONTRACTS,
                    restore_job_contract=(
                        "business_data_maintenance_restore_job_v1"
                    ),
                    restore_max_resume_sequence=3,
                    downstream_capabilities=(
                        self._recovery_capabilities()
                    ),
                    source_snapshot_manifest=snapshot_manifest,
                )
            tampered_candidate_plan = json.loads(
                json.dumps(reviewed_candidate_plan)
            )
            tampered_candidate_plan["rollback_plan"][
                "old_monolith_generation_id"
            ] = "tampered"
            with self.assertRaisesRegex(
                FinanceStorageRecoveryContractError,
                "deterministic fingerprint is stale",
            ):
                validate_recovery_preflight(
                    runtime,
                    action="apply",
                    phase="mutation",
                    deployed_sha=DEPLOYED_SHA,
                    approval_reference="fixture-human-gate",
                    expected_fingerprint=candidate_plan["fingerprint"],
                    deploy_lease=self._recovery_lease(),
                    runner_contracts=EXPECTED_RUNNER_CONTRACTS,
                    restore_job_contract=(
                        "business_data_maintenance_restore_job_v1"
                    ),
                    restore_max_resume_sequence=3,
                    downstream_capabilities=(
                        self._recovery_capabilities()
                    ),
                    reviewed_plan=tampered_candidate_plan,
                    source_snapshot_manifest=snapshot_manifest,
                )
            candidate_preflight = validate_recovery_preflight(
                runtime,
                action="apply",
                phase="mutation",
                deployed_sha=DEPLOYED_SHA,
                approval_reference="fixture-human-gate",
                expected_fingerprint=candidate_plan["fingerprint"],
                deploy_lease=self._recovery_lease(),
                runner_contracts=EXPECTED_RUNNER_CONTRACTS,
                restore_job_contract=(
                    "business_data_maintenance_restore_job_v1"
                ),
                restore_max_resume_sequence=3,
                downstream_capabilities=(
                    self._recovery_capabilities()
                ),
                reviewed_plan=reviewed_candidate_plan,
                source_snapshot_manifest=snapshot_manifest,
            )
            self.assertEqual(candidate_preflight["status"], "ready")
            candidate_builder = FinanceStorageCandidateBuilder(
                planner,
                expected_fingerprint=candidate_plan["fingerprint"],
                approval_reference="fixture-human-gate",
            )

            def candidate_manifest_then_disconnect(
                path: Path,
                manifest: object,
            ) -> None:
                atomic_write_manifest(path, manifest)
                raise KeyboardInterrupt(
                    "candidate client disconnected after manifest"
                )

            with mock.patch(
                "packages.application.finance_storage_migration."
                "atomic_write_manifest",
                side_effect=candidate_manifest_then_disconnect,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    candidate_builder.apply()
            candidate_result = candidate_builder.apply()
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
            shadow_preflight = validate_recovery_preflight(
                runtime,
                action="shadow-activate",
                phase="mutation",
                deployed_sha=DEPLOYED_SHA,
                approval_reference="fixture-human-gate",
                expected_fingerprint=candidate_plan["fingerprint"],
                deploy_lease=self._recovery_lease(),
                runner_contracts=EXPECTED_RUNNER_CONTRACTS,
                restore_job_contract=(
                    "business_data_maintenance_restore_job_v1"
                ),
                restore_max_resume_sequence=3,
                downstream_capabilities=(
                    self._recovery_capabilities()
                ),
                candidate_manifest_path=candidate_manifest_path,
            )
            self.assertEqual(shadow_preflight["status"], "ready")
            def shadow_state_then_disconnect(
                path: Path,
                payload: object,
            ) -> None:
                _atomic_write_json(path, payload)
                raise KeyboardInterrupt(
                    "shadow client disconnected after state"
                )

            with mock.patch(
                "packages.application.finance_storage_migration."
                "_atomic_write_json",
                side_effect=shadow_state_then_disconnect,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    shadow.activate()
            self.assertTrue(shadow.activate()["idempotent"])
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
            verifier = FinanceStorageShadowVerifier(
                runtime,
                candidate_manifest_path=candidate_manifest_path,
                candidate_plan_fingerprint=candidate_plan["fingerprint"],
                minimum_observation_seconds=0,
            )

            def soak_evidence_then_disconnect(
                path: Path,
                payload: object,
            ) -> None:
                _atomic_write_json(path, payload)
                raise KeyboardInterrupt(
                    "soak client disconnected after evidence"
                )

            with mock.patch(
                "packages.application.finance_storage_migration."
                "_atomic_write_json",
                side_effect=soak_evidence_then_disconnect,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    verifier.verify()
            verification = verifier.verify()
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
            cutover_plan["deploy_lease"] = self._recovery_lease()
            self.assertTrue(
                cutover_plan["apply_allowed_by_machine_preflight"],
                cutover_plan["blockers"],
            )
            tampered_plan = json.loads(json.dumps(cutover_plan))
            tampered_plan["candidate"]["bridge_cursor"] = 999
            with self.assertRaisesRegex(
                FinanceStorageRecoveryContractError,
                "deterministic fingerprint is stale",
            ):
                validate_recovery_preflight(
                    runtime,
                    action="cutover-apply",
                    phase="pre_barrier",
                    deployed_sha=DEPLOYED_SHA,
                    approval_reference="fixture-human-gate",
                    expected_fingerprint=cutover_plan["fingerprint"],
                    deploy_lease=self._recovery_lease(),
                    runner_contracts=EXPECTED_RUNNER_CONTRACTS,
                    restore_job_contract=(
                        "business_data_maintenance_restore_job_v1"
                    ),
                    restore_max_resume_sequence=3,
                    downstream_capabilities=(
                        self._recovery_capabilities()
                    ),
                    reviewed_plan=tampered_plan,
                    candidate_manifest_path=candidate_manifest_path,
                )
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
            cutover_preflight = validate_recovery_preflight(
                runtime,
                action="cutover-apply",
                phase="mutation",
                deployed_sha=DEPLOYED_SHA,
                approval_reference="fixture-human-gate",
                expected_fingerprint=fingerprint,
                deploy_lease=self._recovery_lease(),
                runner_contracts=EXPECTED_RUNNER_CONTRACTS,
                restore_job_contract=(
                    "business_data_maintenance_restore_job_v1"
                ),
                restore_max_resume_sequence=3,
                downstream_capabilities=(
                    self._recovery_capabilities()
                ),
                reviewed_plan=cutover_plan,
                candidate_manifest_path=candidate_manifest_path,
            )
            self.assertEqual(
                cutover_preflight["boundary_classification"],
                "held_and_recoverable",
            )
            def cutover_write_then_disconnect(
                path: Path,
                manifest: object,
            ) -> None:
                atomic_write_manifest(path, manifest)
                raise KeyboardInterrupt(
                    "submitting cutover client disconnected"
                )

            with mock.patch(
                "packages.application.finance_storage_migration."
                "atomic_write_manifest",
                side_effect=cutover_write_then_disconnect,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    cutover.apply(
                        reviewed_plan=cutover_plan,
                        expected_fingerprint=fingerprint,
                        approval_reference="fixture-human-gate",
                    )
            cutover_result = cutover.apply(
                reviewed_plan=cutover_plan,
                expected_fingerprint=fingerprint,
                approval_reference="fixture-human-gate",
            )
            self.assertTrue(cutover_result["global_manifest_switched"])
            self.assertTrue(cutover_result["old_monolith_retained"])
            self.assertEqual(
                cutover_result["continuity_classification"],
                "exact_post_manifest_readback",
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
            rollback_plan["deploy_lease"] = self._recovery_lease()
            self.assertTrue(
                rollback_plan["prepare_allowed_by_machine_preflight"],
                rollback_plan["blockers"],
            )
            tampered_rollback_plan = json.loads(
                json.dumps(rollback_plan)
            )
            tampered_rollback_plan["unreviewed_transport"] = {
                "accepted": False
            }
            with self.assertRaisesRegex(
                FinanceStorageRecoveryContractError,
                "deterministic fingerprint is stale",
            ):
                validate_recovery_preflight(
                    runtime,
                    action="rollback-prepare",
                    phase="pre_barrier",
                    deployed_sha=DEPLOYED_SHA,
                    approval_reference="fixture-human-gate",
                    expected_fingerprint=rollback_plan["fingerprint"],
                    deploy_lease=self._recovery_lease(),
                    runner_contracts=EXPECTED_RUNNER_CONTRACTS,
                    restore_job_contract=(
                        "business_data_maintenance_restore_job_v1"
                    ),
                    restore_max_resume_sequence=3,
                    downstream_capabilities=(
                        self._recovery_capabilities()
                    ),
                    reviewed_plan=tampered_rollback_plan,
                )
            with self.assertRaisesRegex(
                FinanceStorageMigrationError,
                "reviewed Finance rollback plan",
            ):
                rollback.prepare(
                    reviewed_plan=tampered_rollback_plan,
                    expected_fingerprint=rollback_plan["fingerprint"],
                    approval_reference="fixture-human-gate",
                )
            rollback_prepare_preflight = validate_recovery_preflight(
                runtime,
                action="rollback-prepare",
                phase="mutation",
                deployed_sha=DEPLOYED_SHA,
                approval_reference="fixture-human-gate",
                expected_fingerprint=rollback_plan["fingerprint"],
                deploy_lease=self._recovery_lease(),
                runner_contracts=EXPECTED_RUNNER_CONTRACTS,
                restore_job_contract=(
                    "business_data_maintenance_restore_job_v1"
                ),
                restore_max_resume_sequence=3,
                downstream_capabilities=(
                    self._recovery_capabilities()
                ),
                reviewed_plan=rollback_plan,
            )
            self.assertEqual(
                rollback_prepare_preflight["status"],
                "ready",
            )
            def rollback_candidate_then_disconnect(
                path: Path,
                payload: object,
            ) -> None:
                _atomic_write_json(path, payload)
                raise KeyboardInterrupt(
                    "rollback prepare client disconnected after evidence"
                )

            with mock.patch(
                "packages.application.finance_storage_migration."
                "_atomic_write_json",
                side_effect=rollback_candidate_then_disconnect,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    rollback.prepare(
                        reviewed_plan=rollback_plan,
                        expected_fingerprint=rollback_plan[
                            "fingerprint"
                        ],
                        approval_reference="fixture-human-gate",
                    )
            rollback_candidate = rollback.prepare(
                reviewed_plan=rollback_plan,
                expected_fingerprint=rollback_plan["fingerprint"],
                approval_reference="fixture-human-gate",
            )
            self.assertTrue(rollback_candidate["idempotent"])
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
            rollback_apply_preflight = validate_recovery_preflight(
                runtime,
                action="rollback-apply",
                phase="mutation",
                deployed_sha=DEPLOYED_SHA,
                approval_reference="fixture-human-gate",
                expected_fingerprint=rollback_fingerprint,
                deploy_lease=self._recovery_lease(),
                runner_contracts=EXPECTED_RUNNER_CONTRACTS,
                restore_job_contract=(
                    "business_data_maintenance_restore_job_v1"
                ),
                restore_max_resume_sequence=3,
                downstream_capabilities=(
                    self._recovery_capabilities()
                ),
                reviewed_plan=rollback_plan,
                rollback_candidate_evidence_path=Path(
                    rollback_candidate["candidate_evidence_path"]
                ),
            )
            self.assertEqual(
                rollback_apply_preflight[
                    "boundary_classification"
                ],
                "held_and_recoverable",
            )
            def rollback_write_then_disconnect(
                path: Path,
                manifest: object,
            ) -> None:
                atomic_write_manifest(path, manifest)
                raise KeyboardInterrupt(
                    "submitting rollback client disconnected"
                )

            with mock.patch(
                "packages.application.finance_storage_migration."
                "atomic_write_manifest",
                side_effect=rollback_write_then_disconnect,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    rollback.apply(
                        reviewed_plan=rollback_plan,
                        expected_fingerprint=rollback_fingerprint,
                        approval_reference="fixture-human-gate",
                        candidate_evidence_path=Path(
                            rollback_candidate[
                                "candidate_evidence_path"
                            ]
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
            self.assertTrue(rollback_result["idempotent"])
            self.assertEqual(
                rollback_result["continuity_classification"],
                "exact_post_manifest_result_recovered",
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
