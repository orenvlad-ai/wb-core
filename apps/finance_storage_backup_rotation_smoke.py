#!/usr/bin/env python3
"""Deterministic post-cutover Finance backup/retention safety smoke."""

from __future__ import annotations

from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.finance_raw_storage import (
    CONSUMER_ID,
    ensure_operational_schema,
    ensure_raw_schema,
    storage_health,
)
from packages.application.finance_storage_backup_rotation import (
    DEFAULT_COPY_OVERHEAD_BYTES,
    FinanceStorageBackupRotation,
    FinanceStorageBackupRotationError,
    RAW_BACKUP_FILENAME,
    backup_rotation_health,
    scheduled_rotation,
)
from packages.application.finance_storage_snapshot_retention import (
    ARCHIVE_CONTRACT,
    ARCHIVE_MANIFEST_FILENAME,
    TRANSACTION_CONTRACT as LEGACY_TRANSACTION_CONTRACT,
    TRANSACTION_FILENAME as LEGACY_TRANSACTION_FILENAME,
    _fingerprint,
)
from packages.application.storage_registry import (
    StoreRegistry,
    atomic_write_manifest,
    build_manifest,
)


DEPLOYED_SHA = "a" * 40


def _post_cutover_committed_event(raw: Path, operational: Path) -> None:
    event_id = "post-cutover-event-1"
    batch_id = "post-cutover-batch-1"
    observed_at = "2026-07-31T01:00:00Z"
    payload_sha256 = "sha256:" + "7" * 64
    with closing(sqlite3.connect(raw)) as conn:
        conn.execute(
            "INSERT INTO finance_raw_ingest_batches("
            "batch_id,source_identity,source_sha256,report_period,seller_id,"
            "week_start,week_end,row_count,rows_digest,status,created_at,committed_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                "fixture:post-cutover",
                "sha256:" + "6" * 64,
                "2026-W31",
                "fixture-seller",
                "2026-07-27",
                "2026-08-02",
                0,
                "sha256:" + "5" * 64,
                "committed",
                observed_at,
                observed_at,
            ),
        )
        conn.execute(
            "INSERT INTO finance_raw_outbox("
            "event_id,batch_id,sequence_no,event_type,payload_json,payload_sha256,"
            "created_at,published_at,attempt_count,last_error"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                batch_id,
                1,
                "finance_batch_committed",
                "{}",
                payload_sha256,
                observed_at,
                observed_at,
                1,
                None,
            ),
        )
        conn.execute(
            "UPDATE finance_raw_consumer_cursors SET last_sequence_no=1,"
            "last_event_id=?,updated_at=? WHERE consumer_id=?",
            (event_id, observed_at, CONSUMER_ID),
        )
        conn.commit()
    with closing(sqlite3.connect(operational)) as conn:
        conn.execute(
            "INSERT INTO finance_operational_inbox("
            "event_id,consumer_id,sequence_no,event_type,payload_sha256,received_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                event_id,
                CONSUMER_ID,
                1,
                "finance_batch_committed",
                payload_sha256,
                observed_at,
            ),
        )
        conn.execute(
            "INSERT INTO finance_operational_receipts("
            "consumer_id,event_id,sequence_no,source_revision,result_row_count,"
            "result_digest,applied_at) VALUES(?,?,?,?,?,?,?)",
            (
                CONSUMER_ID,
                event_id,
                1,
                payload_sha256,
                0,
                "sha256:" + "4" * 64,
                observed_at,
            ),
        )
        conn.execute(
            "UPDATE finance_operational_consumer_cursors SET last_sequence_no=1,"
            "last_event_id=?,source_revision=?,updated_at=? WHERE consumer_id=?",
            (event_id, payload_sha256, observed_at, CONSUMER_ID),
        )
        conn.commit()


def _fixture(runtime: Path) -> tuple[Path, Path, Path]:
    generation = runtime / "generations" / ("1" * 20)
    generation.mkdir(parents=True)
    raw = generation / "finance_raw.sqlite3"
    operational = generation / "operational.sqlite3"
    with closing(sqlite3.connect(raw)) as conn:
        ensure_raw_schema(conn)
        conn.execute(
            "INSERT INTO finance_raw_consumer_cursors"
            "(consumer_id,last_sequence_no,last_event_id,updated_at) "
            "VALUES(?,0,'','2026-07-31T00:00:00Z')",
            (CONSUMER_ID,),
        )
        conn.execute(
            "CREATE TABLE backup_fixture_raw(id INTEGER PRIMARY KEY,value TEXT)"
        )
        conn.execute("INSERT INTO backup_fixture_raw VALUES(1,'first')")
        conn.commit()
    with closing(sqlite3.connect(operational)) as conn:
        ensure_operational_schema(conn)
        conn.execute(
            "INSERT INTO finance_operational_consumer_cursors"
            "(consumer_id,last_sequence_no,last_event_id,source_revision,updated_at) "
            "VALUES(?,0,'','','2026-07-31T00:00:00Z')",
            (CONSUMER_ID,),
        )
        conn.execute(
            "CREATE TABLE backup_fixture_operational(id INTEGER PRIMARY KEY,value TEXT)"
        )
        conn.execute("INSERT INTO backup_fixture_operational VALUES(1,'first')")
        conn.commit()
    manifest = build_manifest(
        state="cutover",
        canonical_source="split",
        generation_epoch="1" * 20,
        raw_generation_id="raw-" + "1" * 16,
        raw_relative_path=str(raw.relative_to(runtime)),
        raw_watermark="raw-watermark-1",
        operational_generation_id="operational-" + "1" * 8,
        operational_relative_path=str(operational.relative_to(runtime)),
        operational_watermark="operational-watermark-1",
        rollback_generation_id="monolith",
        source_fingerprint="sha256:" + "b" * 64,
        created_at="2026-07-31T00:00:00Z",
    )
    with closing(sqlite3.connect(raw)) as conn:
        conn.execute(
            "UPDATE finance_raw_schema_meta SET generation_id=?,"
            "generation_epoch=?,source_fingerprint=? WHERE singleton=1",
            (
                manifest.raw.generation_id,
                manifest.generation_epoch,
                manifest.source_fingerprint,
            ),
        )
        conn.commit()
    with closing(sqlite3.connect(operational)) as conn:
        conn.execute(
            "UPDATE finance_operational_schema_meta SET generation_id=?,"
            "generation_epoch=?,source_fingerprint=? WHERE singleton=1",
            (
                manifest.operational.generation_id,
                manifest.generation_epoch,
                manifest.source_fingerprint,
            ),
        )
        conn.commit()
    atomic_write_manifest(runtime / "storage_generation_manifest.json", manifest)
    monolith = runtime / "registry_upload_runtime.sqlite3"
    monolith.write_bytes(b"protected-original-monolith")
    return raw, operational, monolith


def _legacy_root_snapshot(
    runtime: Path,
    identity: str,
    *,
    unknown: bool = False,
    deployed_sha: str = "b" * 40,
) -> Path:
    snapshot_id = "finance-split-" + identity * 20
    root = runtime / "finance-storage-split-snapshots" / snapshot_id
    root.mkdir(parents=True)
    database = root / "monolith.sqlite3"
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("CREATE TABLE legacy(id INTEGER PRIMARY KEY,value TEXT)")
        conn.execute("INSERT INTO legacy VALUES(1,'superseded')")
        conn.commit()
    os.chmod(database, 0o600)
    manifest = {
        "contract_version": "wb_core_finance_storage_coherent_snapshot_v1",
        "status": "integrity_verified",
        "snapshot_id": snapshot_id,
        "deployed_sha": deployed_sha,
        "database_path": str(database),
        "captured_at": "2026-07-30T00:00:00Z",
    }
    manifest["evidence_fingerprint"] = _fingerprint(manifest)
    manifest_path = root / "snapshot_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    if unknown:
        (root / "foreign.bin").write_bytes(b"do-not-delete")
    return root


def _legacy_backup_archive(runtime: Path, backup_root: Path, identity: str) -> Path:
    source = _legacy_root_snapshot(runtime, identity)
    with closing(sqlite3.connect(source / "monolith.sqlite3")) as conn:
        conn.execute("CREATE TABLE padding(value BLOB)")
        conn.execute("INSERT INTO padding VALUES(zeroblob(5242880))")
        conn.commit()
    manifest_path = source / "snapshot_manifest.json"
    snapshot_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    snapshot_manifest.pop("evidence_fingerprint", None)
    snapshot_manifest["evidence_fingerprint"] = _fingerprint(snapshot_manifest)
    manifest_path.write_text(
        json.dumps(snapshot_manifest, sort_keys=True), encoding="utf-8"
    )
    archive = backup_root / source.name
    archive.mkdir(parents=True)
    for name in ("monolith.sqlite3", "snapshot_manifest.json"):
        destination = archive / name
        destination.write_bytes((source / name).read_bytes())
        os.chmod(destination, 0o600)
    plan_fingerprint = "sha256:" + identity * 64
    declared = []
    for name in ("monolith.sqlite3", "snapshot_manifest.json"):
        path = archive / name
        declared.append(
            {
                "name": name,
                "size_bytes": path.stat().st_size,
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    archive_manifest = {
        "contract_version": ARCHIVE_CONTRACT,
        "status": "archive_verified",
        "snapshot_id": source.name,
        "snapshot_status": "integrity_verified",
        "snapshot_deployed_sha": "b" * 40,
        "snapshot_evidence_fingerprint": snapshot_manifest["evidence_fingerprint"],
        "archived_by_deployed_sha": DEPLOYED_SHA,
        "plan_fingerprint": plan_fingerprint,
        "approval_reference": "archive-smoke",
        "source_path": str(source),
        "archive_path": str(archive),
        "files": declared,
        "verified_at": f"2026-07-{int(identity):02d}T00:00:00Z",
        "source_release_completed": True,
        "source_released_at": "2026-07-31T00:00:00Z",
    }
    archive_manifest["fingerprint"] = _fingerprint(archive_manifest)
    archive_manifest_path = archive / ARCHIVE_MANIFEST_FILENAME
    archive_manifest_path.write_text(
        json.dumps(archive_manifest, sort_keys=True), encoding="utf-8"
    )
    os.chmod(archive_manifest_path, 0o600)
    transaction = {
        "contract_version": LEGACY_TRANSACTION_CONTRACT,
        "snapshot_id": source.name,
        "plan_fingerprint": plan_fingerprint,
        "approval_reference": "archive-smoke",
        "deployed_sha": DEPLOYED_SHA,
        "source_path": str(source),
        "archive_path": str(archive),
        "phase": "source_released",
        "archive_manifest_fingerprint": archive_manifest["fingerprint"],
        "updated_at": "2026-07-31T00:00:00Z",
    }
    transaction_path = archive / LEGACY_TRANSACTION_FILENAME
    transaction_path.write_text(
        json.dumps(transaction, sort_keys=True), encoding="utf-8"
    )
    os.chmod(transaction_path, 0o600)
    for path in source.iterdir():
        path.unlink()
    source.rmdir()
    return archive


def _rotation(
    runtime: Path, backup_root: Path, **overrides: object
) -> FinanceStorageBackupRotation:
    options = {
        "backup_root": backup_root,
        "require_distinct_device": False,
        "require_backup_mountpoint": False,
        "root_target_bytes": 0,
        "backup_target_bytes": 0,
        "hard_reserve_bytes": 0,
        "degraded_available_bytes": 0,
        "max_set_bytes": 1024**3,
        "max_age_seconds": 7 * 24 * 60 * 60,
        "minimum_replacement_interval_seconds": 0,
    }
    options.update(overrides)
    return FinanceStorageBackupRotation(runtime, deployed_sha=DEPLOYED_SHA, **options)


class FinanceStorageBackupRotationSmoke(unittest.TestCase):
    def test_two_cycles_crash_resume_one_current_and_non_targets(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runtime = Path(raw_tmp) / "runtime"
            raw, operational, monolith = _fixture(runtime)
            backup_root = runtime / "backups" / "finance-storage-split-snapshots"
            backup_root.parent.mkdir(parents=True)
            stale = _legacy_root_snapshot(runtime, "2")
            generation_before = sorted(
                str(path.relative_to(runtime))
                for path in (runtime / "generations").rglob("*")
            )
            monolith_before = monolith.read_bytes()
            rotation = _rotation(runtime, backup_root)
            self.assertEqual(
                scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA)["status"],
                "policy_inert",
            )
            first = rotation.build_plan(force_replacement=True)
            self.assertTrue(first["apply_allowed_by_machine_preflight"])
            self.assertEqual(first["policy"]["retained_count_cap"], 1)
            self.assertEqual(first["policy"]["temporary_count_cap"], 2)
            with self.assertRaisesRegex(RuntimeError, "operational copy"):
                rotation.apply(
                    reviewed_plan=first,
                    expected_fingerprint=first["fingerprint"],
                    approval_reference="smoke-human-gate",
                    fault_at="after_operational_copy",
                )
            self.assertTrue(stale.is_dir())
            applied = rotation.apply(
                reviewed_plan=first,
                expected_fingerprint=first["fingerprint"],
                approval_reference="smoke-human-gate",
            )
            self.assertTrue(applied["replacement_verified"])
            self.assertEqual(applied["retained_backup_count"], 1)
            self.assertFalse(stale.exists())
            first_id = applied["retained_backup_id"]
            transaction = json.loads(
                (
                    backup_root
                    / "transactions"
                    / f"{first['fingerprint'].removeprefix('sha256:')}.json"
                ).read_text(encoding="utf-8")
            )
            retained_manifest = json.loads(
                (
                    backup_root / "retained" / first_id / "backup_manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                retained_manifest["captured_at"],
                min(
                    item["captured_at"] for item in transaction["copy_proofs"].values()
                ),
            )
            for key, value in retained_manifest["source_identity"][
                "watermarks"
            ].items():
                self.assertEqual(retained_manifest["restore_drill"][key], value)
            first_readback = rotation.readback(
                reviewed_plan=first,
                expected_fingerprint=first["fingerprint"],
            )
            self.assertTrue(first_readback["restore_drill_verified"])
            self.assertEqual(first_readback["projected_90_day_growth_bytes"], 0)
            selector_before_repeat = (backup_root / "current.json").read_bytes()
            repeated = rotation.apply(
                reviewed_plan=first,
                expected_fingerprint=first["fingerprint"],
                approval_reference="smoke-human-gate",
            )
            self.assertEqual(repeated["fingerprint"], applied["fingerprint"])
            self.assertEqual(
                (backup_root / "current.json").read_bytes(),
                selector_before_repeat,
            )
            audit_events = [
                json.loads(line)
                for line in (backup_root / "retention_audit.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line
            ]
            self.assertEqual(
                sum(
                    item.get("plan_fingerprint") == first["fingerprint"]
                    for item in audit_events
                ),
                1,
            )

            with closing(sqlite3.connect(raw)) as conn:
                conn.execute("INSERT INTO backup_fixture_raw VALUES(2,'second')")
                conn.commit()
            with closing(sqlite3.connect(operational)) as conn:
                conn.execute(
                    "INSERT INTO backup_fixture_operational VALUES(2,'second')"
                )
                conn.commit()
            _post_cutover_committed_event(raw, operational)
            post_cutover_health = storage_health(StoreRegistry(runtime))
            self.assertEqual(post_cutover_health["consumer_lag_events"], 0)
            self.assertEqual(post_cutover_health["live_tail_cursor"], 0)
            self.assertFalse(post_cutover_health["live_tail_applicable"])
            self.assertEqual(post_cutover_health["live_tail_lag_events"], 0)
            second = rotation.build_plan(force_replacement=True)
            applied_second = rotation.apply(
                reviewed_plan=second,
                expected_fingerprint=second["fingerprint"],
                approval_reference="smoke-human-gate",
            )
            self.assertNotEqual(first_id, applied_second["retained_backup_id"])
            retained_second_manifest = json.loads(
                (
                    backup_root
                    / "retained"
                    / applied_second["retained_backup_id"]
                    / "backup_manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertFalse(
                retained_second_manifest["restore_drill"]["live_tail_applicable"]
            )
            self.assertEqual(
                retained_second_manifest["restore_drill"]["live_tail_lag_events"],
                0,
            )
            retained = [
                path.name
                for path in (backup_root / "retained").iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ]
            self.assertEqual(retained, [applied_second["retained_backup_id"]])
            with closing(sqlite3.connect(raw)) as conn:
                conn.execute(
                    "INSERT INTO backup_fixture_raw VALUES(3,'scheduled-third')"
                )
                conn.commit()
            scheduled_third = scheduled_rotation(
                runtime,
                deployed_sha=DEPLOYED_SHA,
                require_distinct_device=False,
                require_backup_mountpoint=False,
            )
            self.assertEqual(scheduled_third["status"], "completed")
            self.assertNotEqual(
                scheduled_third["retained_backup_id"],
                applied_second["retained_backup_id"],
            )
            scheduled_not_due = scheduled_rotation(
                runtime,
                deployed_sha=DEPLOYED_SHA,
                require_distinct_device=False,
                require_backup_mountpoint=False,
            )
            self.assertEqual(scheduled_not_due["status"], "not_due")
            retained = [
                path.name
                for path in (backup_root / "retained").iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ]
            self.assertEqual(retained, [scheduled_third["retained_backup_id"]])
            self.assertEqual(monolith.read_bytes(), monolith_before)
            self.assertEqual(
                sorted(
                    str(path.relative_to(runtime))
                    for path in (runtime / "generations").rglob("*")
                ),
                generation_before,
            )
            health = backup_rotation_health(runtime)
            self.assertEqual(health["status"], "healthy")
            self.assertTrue(health["next_replacement_capacity"])
            foreign = backup_root / "retained" / "foreign"
            foreign.mkdir()
            degraded = backup_rotation_health(runtime)
            self.assertEqual(degraded["status"], "degraded")
            self.assertIn(
                "retained inventory contains partial, foreign or unsafe entries",
                degraded["blockers"],
            )

    def test_corrupted_replacement_preserves_selected_current(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runtime = Path(raw_tmp) / "runtime"
            raw, _operational, _monolith = _fixture(runtime)
            backup_root = runtime / "backups" / "finance-storage-split-snapshots"
            backup_root.parent.mkdir(parents=True)
            rotation = _rotation(runtime, backup_root)
            first = rotation.build_plan(force_replacement=True)
            first_result = rotation.apply(
                reviewed_plan=first,
                expected_fingerprint=first["fingerprint"],
                approval_reference="smoke-human-gate",
            )
            with closing(sqlite3.connect(raw)) as conn:
                conn.execute("INSERT INTO backup_fixture_raw VALUES(2,'changed')")
                conn.commit()
            second = rotation.build_plan(force_replacement=True)
            with self.assertRaisesRegex(RuntimeError, "raw copy"):
                rotation.apply(
                    reviewed_plan=second,
                    expected_fingerprint=second["fingerprint"],
                    approval_reference="smoke-human-gate",
                    fault_at="after_raw_copy",
                )
            partial = Path(second["replacement"]["destination_partial"])
            (partial / RAW_BACKUP_FILENAME).write_bytes(b"corrupt")
            with self.assertRaises(FinanceStorageBackupRotationError):
                rotation.apply(
                    reviewed_plan=second,
                    expected_fingerprint=second["fingerprint"],
                    approval_reference="smoke-human-gate",
                )
            selected = json.loads(
                (backup_root / "current.json").read_text(encoding="utf-8")
            )
            self.assertEqual(selected["backup_id"], first_result["retained_backup_id"])
            self.assertTrue(
                (backup_root / "retained" / first_result["retained_backup_id"]).is_dir()
            )

    def test_unknown_artifact_is_protected_and_capacity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runtime = Path(raw_tmp) / "runtime"
            _fixture(runtime)
            backup_root = runtime / "backups" / "finance-storage-split-snapshots"
            backup_root.parent.mkdir(parents=True)
            unknown = _legacy_root_snapshot(runtime, "3", unknown=True)
            active_sha_snapshot = _legacy_root_snapshot(
                runtime,
                "e",
                deployed_sha=DEPLOYED_SHA,
            )
            rotation = _rotation(runtime, backup_root)
            plan = rotation.build_plan(force_replacement=True)
            self.assertTrue(plan["apply_allowed_by_machine_preflight"])
            protected_paths = {item["path"] for item in plan["inventory"]["protected"]}
            self.assertEqual(
                protected_paths,
                {str(unknown.resolve()), str(active_sha_snapshot.resolve())},
            )
            result = rotation.apply(
                reviewed_plan=plan,
                expected_fingerprint=plan["fingerprint"],
                approval_reference="smoke-human-gate",
            )
            self.assertTrue(result["replacement_verified"])
            self.assertTrue((unknown / "foreign.bin").is_file())
            self.assertTrue(active_sha_snapshot.is_dir())

            changed_plan = rotation.build_plan(force_replacement=True)
            (unknown / "foreign.bin").write_bytes(b"changed")
            with self.assertRaisesRegex(
                FinanceStorageBackupRotationError,
                "protected Finance snapshot inventory CAS drifted",
            ):
                rotation.apply(
                    reviewed_plan=changed_plan,
                    expected_fingerprint=changed_plan["fingerprint"],
                    approval_reference="smoke-human-gate",
                )

            impossible = _rotation(
                runtime,
                backup_root,
                hard_reserve_bytes=os.statvfs(backup_root).f_bavail
                * os.statvfs(backup_root).f_frsize
                + 1,
            ).build_plan(force_replacement=True)
            self.assertFalse(impossible["apply_allowed_by_machine_preflight"])
            self.assertIn(
                "replacement_capacity_shortfall",
                {item["code"] for item in impossible["blockers"]},
            )

    def test_due_policy_mount_fallback_concurrency_and_digest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runtime = Path(raw_tmp) / "runtime"
            raw, _operational, _monolith = _fixture(runtime)
            backup_root = runtime / "backups" / "finance-storage-split-snapshots"
            backup_root.parent.mkdir(parents=True)
            with self.assertRaisesRegex(
                FinanceStorageBackupRotationError, "root fallback"
            ):
                FinanceStorageBackupRotation(
                    runtime,
                    deployed_sha=DEPLOYED_SHA,
                    backup_root=backup_root,
                    require_distinct_device=False,
                    root_target_bytes=0,
                    backup_target_bytes=0,
                ).build_plan(force_replacement=True)

            stale = _legacy_root_snapshot(runtime, "4")
            rotation = _rotation(runtime, backup_root)
            first = rotation.build_plan(force_replacement=True)
            unowned_partial = Path(first["replacement"]["destination_partial"])
            unowned_partial.mkdir(parents=True)
            (unowned_partial / "foreign.bin").write_bytes(b"foreign")
            with self.assertRaisesRegex(
                FinanceStorageBackupRotationError,
                "unowned replacement path",
            ):
                rotation.apply(
                    reviewed_plan=first,
                    expected_fingerprint=first["fingerprint"],
                    approval_reference="smoke-human-gate",
                )
            self.assertFalse((backup_root / "current.json").exists())
            (unowned_partial / "foreign.bin").unlink()
            unowned_partial.rmdir()
            lock_path = runtime / ".finance-storage-snapshot-retention.lock"
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(
                    FinanceStorageBackupRotationError, "another Finance backup worker"
                ):
                    rotation.apply(
                        reviewed_plan=first,
                        expected_fingerprint=first["fingerprint"],
                        approval_reference="smoke-human-gate",
                    )
            finally:
                os.close(descriptor)

            with closing(sqlite3.connect(stale / "monolith.sqlite3")) as conn:
                conn.execute("UPDATE legacy SET value='drifted'")
                conn.commit()
            with self.assertRaisesRegex(
                FinanceStorageBackupRotationError, "deletion candidate drifted"
            ):
                rotation.apply(
                    reviewed_plan=first,
                    expected_fingerprint=first["fingerprint"],
                    approval_reference="smoke-human-gate",
                )
            self.assertFalse((backup_root / "current.json").exists())

            stale = _legacy_root_snapshot(runtime, "5")
            clean = rotation.build_plan(force_replacement=True)
            result = rotation.apply(
                reviewed_plan=clean,
                expected_fingerprint=clean["fingerprint"],
                approval_reference="smoke-human-gate",
            )
            self.assertTrue(result["replacement_verified"])
            with closing(sqlite3.connect(raw)) as conn:
                conn.execute("INSERT INTO backup_fixture_raw VALUES(2,'not-yet-due')")
                conn.commit()
            not_due_rotation = _rotation(
                runtime,
                backup_root,
                minimum_replacement_interval_seconds=10**9,
                max_age_seconds=10**9,
            )
            not_due = not_due_rotation.build_plan(cleanup_legacy=False)
            self.assertFalse(not_due["replacement"]["due"])
            age_due_rotation = _rotation(
                runtime,
                backup_root,
                minimum_replacement_interval_seconds=10**9,
                max_age_seconds=0,
            )
            age_due = age_due_rotation.build_plan(cleanup_legacy=False)
            self.assertTrue(age_due["replacement"]["due"])
            with closing(sqlite3.connect(raw)) as conn:
                conn.execute(
                    "UPDATE finance_raw_schema_meta SET generation_id='drifted' "
                    "WHERE singleton=1"
                )
                conn.commit()
            with self.assertRaisesRegex(
                FinanceStorageBackupRotationError, "coherent zero-lag"
            ):
                rotation.build_plan(force_replacement=True)

    def test_crash_matrix_resumes_without_losing_fallback(self) -> None:
        for fault in (
            "after_pre_publish_gc",
            "after_operational_copy",
            "after_raw_copy",
            "after_restore_manifest",
            "after_backup_manifest",
            "after_replacement_verified",
            "after_current_selected",
            "during_candidate_delete",
            "after_post_publish_gc",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw_tmp:
                runtime = Path(raw_tmp) / "runtime"
                _fixture(runtime)
                backup_root = runtime / "backups" / "finance-storage-split-snapshots"
                backup_root.parent.mkdir(parents=True)
                fallback = _legacy_root_snapshot(runtime, "6")
                rotation = _rotation(runtime, backup_root)
                plan = rotation.build_plan(force_replacement=True)
                with self.assertRaises(RuntimeError):
                    rotation.apply(
                        reviewed_plan=plan,
                        expected_fingerprint=plan["fingerprint"],
                        approval_reference="smoke-human-gate",
                        fault_at=fault,
                    )
                if fault == "after_raw_copy":
                    pending_manifest = (
                        Path(plan["replacement"]["destination_partial"])
                        / "storage_generation_manifest.json.pending"
                    )
                    pending_manifest.write_bytes(b'{"incomplete":')
                    os.chmod(pending_manifest, 0o600)
                if fault == "after_restore_manifest":
                    pending_backup_manifest = (
                        Path(plan["replacement"]["destination_partial"])
                        / "backup_manifest.json.pending"
                    )
                    pending_backup_manifest.write_bytes(b'{"incomplete":')
                    os.chmod(pending_backup_manifest, 0o600)
                if fault != "after_post_publish_gc":
                    self.assertTrue(fallback.is_dir())
                completed = rotation.apply(
                    reviewed_plan=plan,
                    expected_fingerprint=plan["fingerprint"],
                    approval_reference="smoke-human-gate",
                )
                self.assertTrue(completed["replacement_verified"])
                self.assertFalse(fallback.exists())
                self.assertEqual(completed["retained_backup_count"], 1)
                self.assertEqual(completed["removed_artifact_count"], 1)
                self.assertEqual(
                    completed["removed_artifacts"][0]["artifact_id"],
                    fallback.name,
                )
                if fault == "after_raw_copy":
                    transaction = json.loads(
                        (
                            backup_root
                            / "transactions"
                            / f"{plan['fingerprint'].removeprefix('sha256:')}.json"
                        ).read_text(encoding="utf-8")
                    )
                    self.assertTrue(transaction["manifest_recovery_evidence"])
                if fault == "after_restore_manifest":
                    transaction = json.loads(
                        (
                            backup_root
                            / "transactions"
                            / f"{plan['fingerprint'].removeprefix('sha256:')}.json"
                        ).read_text(encoding="utf-8")
                    )
                    self.assertTrue(transaction["backup_manifest_recovery_evidence"])

    def test_capacity_pre_gc_keeps_last_fallback_until_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runtime = Path(raw_tmp) / "runtime"
            _fixture(runtime)
            backup_root = runtime / "backups" / "finance-storage-split-snapshots"
            backup_root.mkdir(parents=True)
            oldest = _legacy_backup_archive(runtime, backup_root, "7")
            newest = _legacy_backup_archive(runtime, backup_root, "8")
            source_copy_bytes = (
                sum(
                    path.stat().st_size
                    for path in (runtime / "generations" / ("1" * 20)).glob("*.sqlite3")
                )
                + DEFAULT_COPY_OVERHEAD_BYTES
            )
            available = (
                os.statvfs(backup_root).f_bavail * os.statvfs(backup_root).f_frsize
            )
            hard_reserve = available - source_copy_bytes + 1024**2
            rotation = _rotation(runtime, backup_root, hard_reserve_bytes=hard_reserve)
            plan = rotation.build_plan(force_replacement=True)
            self.assertTrue(plan["apply_allowed_by_machine_preflight"])
            self.assertEqual(
                [item["artifact_id"] for item in plan["pre_publish_deletions"]],
                [oldest.name],
            )
            self.assertIn(
                newest.name,
                [item["artifact_id"] for item in plan["post_publish_deletions"]],
            )
            result = rotation.apply(
                reviewed_plan=plan,
                expected_fingerprint=plan["fingerprint"],
                approval_reference="smoke-human-gate",
            )
            self.assertTrue(result["replacement_verified"])
            self.assertFalse(oldest.exists())
            self.assertFalse(newest.exists())

    def test_shadow_state_must_remain_inactive_and_cas_stable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runtime = Path(raw_tmp) / "runtime"
            _fixture(runtime)
            backup_root = runtime / "backups" / "finance-storage-split-snapshots"
            backup_root.mkdir(parents=True)
            shadow_path = runtime / ".finance-storage-shadow-ingest.json"
            shadow_path.write_text(
                json.dumps(
                    {
                        "contract_version": "wb_core_finance_shadow_ingest_state_v1",
                        "enabled": False,
                        "status": "inactive_after_cutover",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.chmod(shadow_path, 0o600)
            rotation = _rotation(runtime, backup_root)
            plan = rotation.build_plan(force_replacement=True)
            self.assertFalse(plan["canonical_guard"]["shadow_state"]["enabled"])
            shadow_path.write_text(
                json.dumps(
                    {
                        "contract_version": "wb_core_finance_shadow_ingest_state_v1",
                        "enabled": False,
                        "status": "drifted_after_plan",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                FinanceStorageBackupRotationError, "shadow state CAS drifted"
            ):
                rotation.apply(
                    reviewed_plan=plan,
                    expected_fingerprint=plan["fingerprint"],
                    approval_reference="smoke-human-gate",
                )
            shadow_path.write_text(
                json.dumps(
                    {
                        "contract_version": "wb_core_finance_shadow_ingest_state_v1",
                        "enabled": True,
                        "status": "unexpected_active_shadow",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                FinanceStorageBackupRotationError, "inactive shadow ingest"
            ):
                rotation.build_plan(force_replacement=True)


if __name__ == "__main__":
    unittest.main()
