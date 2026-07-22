#!/usr/bin/env python3
"""Smoke the coherent pre-sync backup command without business-row mutation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.warehouse_functional_runner as functional_runner  # noqa: E402
import packages.application.warehouse_functional_economics_backfill as economics_backfill  # noqa: E402
import packages.application.calculation_parameters as calculation_parameters  # noqa: E402
from packages.application.calculation_parameters import (  # noqa: E402
    CalculationParametersBlock,
    ensure_calculation_parameters_schema,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_sync_lock import (  # noqa: E402
    WarehouseSyncBusyError,
    warehouse_sync_lock,
)

run = functional_runner.run


def main() -> int:
    with TemporaryDirectory(prefix="warehouse-functional-backup-smoke-") as raw_dir:
        root = Path(raw_dir)
        runtime_dir = root / "runtime"
        runtime_dir.mkdir(parents=True)
        db_path = runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE primary_evidence(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO primary_evidence VALUES('source-1', 'unchanged')")
            conn.commit()
        run(
            argparse.Namespace(
                runtime_dir=str(runtime_dir),
                env_file="",
                command="readback",
            )
        )
        before = db_path.read_bytes()

        result = run(
            argparse.Namespace(
                runtime_dir=str(runtime_dir),
                env_file="",
                command="backup",
                backup_dir=str((root / "backups").resolve()),
            )
        )
        backup = dict(result.get("backup") or {})
        backup_path = Path(str(backup.get("path") or ""))
        if result.get("status") != "success" or result.get("mode") != "backup":
            raise AssertionError("backup command did not report success")
        if not backup_path.is_file() or backup.get("integrity_check") != "ok":
            raise AssertionError("backup command did not retain a coherent SQLite backup")
        if hashlib.sha256(backup_path.read_bytes()).hexdigest() != backup.get("sha256"):
            raise AssertionError("backup command SHA-256 does not match the retained file")
        if db_path.read_bytes() != before:
            raise AssertionError("backup command changed the live database")
        if backup_path.stat().st_mode & 0o777 != 0o600:
            raise AssertionError("backup command must retain mode 0600")
        try:
            run(
                argparse.Namespace(
                    runtime_dir=str(runtime_dir),
                    env_file="",
                    command="backup",
                    backup_dir="relative-backups-are-forbidden",
                )
            )
        except ValueError as exc:
            if "absolute" not in str(exc):
                raise AssertionError("relative backup directory failed for the wrong reason") from exc
        else:
            raise AssertionError("relative backup directory was unexpectedly accepted")
        with (
            mock.patch.object(
                functional_runner.RegistryUploadDbBackedRuntime,
                "backup_database",
                side_effect=ValueError("injected capacity gate"),
            ),
            mock.patch.object(
                functional_runner.WarehouseFunctionalBlock,
                "record_failed_sync",
            ) as failed_sync,
        ):
            try:
                run(
                    argparse.Namespace(
                        runtime_dir=str(runtime_dir),
                        env_file="",
                        command="manual-sync",
                        backup_dir=str((root / "manual-backups").resolve()),
                    )
                )
            except ValueError as exc:
                if "capacity" not in str(exc):
                    raise AssertionError("manual backup failed for the wrong reason") from exc
            else:
                raise AssertionError("manual sync continued after backup failure")
            failed_sync.assert_not_called()

        with (
            mock.patch.object(
                functional_runner.RegistryUploadDbBackedRuntime,
                "backup_database",
                side_effect=ValueError("injected hourly capacity gate"),
            ),
            mock.patch.object(functional_runner, "_refresh_official_supply_state") as refresh,
            mock.patch.object(
                functional_runner.WarehouseFunctionalBlock,
                "record_failed_sync",
            ) as failed_sync,
        ):
            try:
                run(
                    argparse.Namespace(
                        runtime_dir=str(runtime_dir),
                        env_file="",
                        command="hourly-sync",
                    )
                )
            except ValueError as exc:
                if "hourly capacity" not in str(exc):
                    raise AssertionError("hourly preflight failed for the wrong reason") from exc
            else:
                raise AssertionError("hourly sync continued after pre-mutation backup failure")
            refresh.assert_not_called()
            failed_sync.assert_called_once()

        with warehouse_sync_lock(runtime_dir):
            with warehouse_sync_lock(runtime_dir, blocking=False):
                pass
            concurrent_results: list[str] = []

            def try_concurrent_lock() -> None:
                try:
                    with warehouse_sync_lock(runtime_dir, blocking=False):
                        concurrent_results.append("acquired")
                except WarehouseSyncBusyError:
                    concurrent_results.append("busy")

            concurrent_thread = threading.Thread(target=try_concurrent_lock)
            concurrent_thread.start()
            concurrent_thread.join(timeout=5)
            if concurrent_thread.is_alive() or concurrent_results != ["busy"]:
                raise AssertionError(
                    f"parallel warehouse lock was not rejected: {concurrent_results}"
                )

        settings_lock_runtime_dir = root / "settings-lock-runtime"
        settings_lock_runtime_dir.mkdir()
        settings_parameters = CalculationParametersBlock(
            runtime=RegistryUploadDbBackedRuntime(runtime_dir=settings_lock_runtime_dir)
        )
        with warehouse_sync_lock(settings_lock_runtime_dir):
            settings_results: list[str] = []

            def try_settings_publication() -> None:
                try:
                    settings_parameters.create_version(
                        {},
                        preview_fingerprint="sha256:not-reached",
                        created_by="smoke",
                    )
                except WarehouseSyncBusyError:
                    settings_results.append("busy")
                else:
                    settings_results.append("created")

            settings_thread = threading.Thread(target=try_settings_publication)
            settings_thread.start()
            settings_thread.join(timeout=5)
            if settings_thread.is_alive() or settings_results != ["busy"]:
                raise AssertionError(
                    f"settings publication overlapped warehouse synchronization: {settings_results}"
                )

        wal_runtime_dir = root / "wal-runtime"
        wal_runtime_dir.mkdir()
        wal_runtime = RegistryUploadDbBackedRuntime(runtime_dir=wal_runtime_dir)
        wal_connection = sqlite3.connect(wal_runtime.db_path)
        try:
            wal_connection.execute("PRAGMA journal_mode=WAL")
            wal_connection.execute("PRAGMA wal_autocheckpoint=0")
            wal_connection.execute("CREATE TABLE wal_evidence(value BLOB NOT NULL)")
            wal_connection.execute("INSERT INTO wal_evidence(value) VALUES(zeroblob(2097152))")
            wal_connection.commit()
            wal_path = Path(str(wal_runtime.db_path) + "-wal")
            if not wal_path.is_file() or wal_path.stat().st_size <= 0:
                raise AssertionError("WAL capacity fixture was not created")
            coherent_size = wal_runtime.coherent_backup_size_bytes()
            if coherent_size < wal_runtime.db_path.stat().st_size + wal_path.stat().st_size:
                raise AssertionError("coherent backup capacity omitted committed WAL bytes")
            wal_backup = wal_runtime.backup_database(
                (root / "backups" / "wal-runtime.sqlite3").resolve()
            )
            with sqlite3.connect(str(wal_backup["path"])) as backup_conn:
                stored_bytes = int(
                    backup_conn.execute("SELECT length(value) FROM wal_evidence").fetchone()[0]
                )
            if stored_bytes != 2097152:
                raise AssertionError("coherent backup lost committed WAL content")
        finally:
            wal_connection.close()

        daily_runtime_dir = root / "daily-runtime"
        daily_runtime_dir.mkdir()
        daily_db = daily_runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(daily_db) as conn:
            conn.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO evidence(value) VALUES('daily-restore-point')")
            conn.commit()
        daily_runtime = RegistryUploadDbBackedRuntime(runtime_dir=daily_runtime_dir)
        parameters = CalculationParametersBlock(runtime=daily_runtime)
        daily_backup = parameters.prepare_functional_economics_backup()
        daily_path = Path(str(daily_backup.get("path") or ""))
        daily_manifest_path = Path(str(daily_backup.get("raw_manifest_path") or ""))
        if not daily_path.is_file() or daily_backup.get("integrity_check") != "ok":
            raise AssertionError("daily economics restore point was not created before mutation")
        if (
            not daily_manifest_path.is_file()
            or daily_manifest_path.stat().st_mode & 0o777 != 0o600
        ):
            raise AssertionError("daily economics restore point lacks private provenance")
        economics_backfill._validate_verified_backup(daily_backup)
        drift_path = (root / "backups" / "drifted-raw.sqlite3").resolve()
        declared_daily_backup = daily_runtime.backup_database(drift_path)
        drift_path.chmod(0o600)
        with sqlite3.connect(drift_path) as conn:
            conn.execute("INSERT INTO evidence(value) VALUES('unexpected-backup-drift')")
            conn.commit()
        try:
            economics_backfill._validate_verified_backup(declared_daily_backup)
        except economics_backfill.FunctionalEconomicsBackfillError as exc:
            if "declared fingerprint" not in str(exc):
                raise AssertionError("raw backup drift failed for the wrong reason") from exc
        else:
            raise AssertionError("modified raw backup was trusted")
        fake_plan = {"plan_fingerprint": "sha256:daily-fixture"}
        fake_result = {
            "status": "applied",
            "database_written": True,
            "backup": daily_backup,
        }
        with (
            mock.patch.object(economics_backfill, "build_functional_economics_backfill_plan", return_value=fake_plan),
            mock.patch.object(economics_backfill, "apply_functional_economics_backfill_plan", return_value=fake_result),
        ):
            publication = parameters.publish_current_functional_economics(
                verified_backup=daily_backup,
            )
        archive_path = Path(str((publication.get("backup_archive") or {}).get("archive_path") or ""))
        if daily_path.exists() or not archive_path.is_file():
            raise AssertionError("successful daily economics publication was not losslessly archived")
        if daily_manifest_path.exists():
            raise AssertionError("archived daily restore point retained a stale raw manifest")
        if (
            (publication.get("backup") or {}).get("archive_path") != str(archive_path)
            or not (publication.get("backup") or {}).get("source_removed")
            or (publication.get("backup") or {}).get("path")
        ):
            raise AssertionError("successful publication exposed deleted raw backup metadata")
        reused = parameters.prepare_functional_economics_backup()
        if not reused.get("reused") or reused.get("archive_path") != str(archive_path):
            raise AssertionError("hourly economics did not reuse the daily archived restore point")
        economics_backfill._validate_verified_backup(reused)
        archive_path.write_bytes(b"corrupted")
        try:
            parameters.prepare_functional_economics_backup()
        except ValueError as exc:
            if not any(token in str(exc) for token in ("SHA-256", "zstd", "provenance")):
                raise AssertionError("corrupt archive failed for the wrong reason") from exc
        else:
            raise AssertionError("corrupt daily archive was reused")

        operator_runtime_dir = root / "operator-settings-runtime"
        operator_runtime_dir.mkdir()
        with sqlite3.connect(operator_runtime_dir / "registry_upload_runtime.sqlite3") as conn:
            conn.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO evidence(value) VALUES('operator-settings')")
            conn.commit()
        operator_parameters = CalculationParametersBlock(
            runtime=RegistryUploadDbBackedRuntime(runtime_dir=operator_runtime_dir)
        )
        operator_backup_one = operator_parameters.prepare_operator_settings_backup(
            preview_fingerprint="sha256:operator-one",
        )
        operator_backup_two = operator_parameters.prepare_operator_settings_backup(
            preview_fingerprint="sha256:operator-two",
        )
        if (
            operator_backup_one.get("path") == operator_backup_two.get("path")
            or operator_backup_one.get("backup_scope") != "fresh_operator_settings"
            or operator_backup_two.get("backup_scope") != "fresh_operator_settings"
        ):
            raise AssertionError("operator settings did not receive fresh per-save restore points")
        operator_raw_manifest = Path(str(operator_backup_one.get("raw_manifest_path") or ""))
        operator_raw_evidence = json.loads(operator_raw_manifest.read_text())
        if (
            not operator_raw_manifest.is_file()
            or operator_raw_manifest.stat().st_mode & 0o777 != 0o600
            or operator_raw_evidence.get("settings_preview_fingerprint")
            != "sha256:operator-one"
            or operator_raw_evidence.get("backup_scope") != "fresh_operator_settings"
        ):
            raise AssertionError("operator settings raw restore point lacks durable preview lineage")
        operator_archive = operator_parameters._archive_functional_economics_backup(
            operator_backup_one,
        )
        if (
            operator_archive.get("settings_preview_fingerprint")
            != "sha256:operator-one"
            or operator_archive.get("backup_scope") != "fresh_operator_settings"
            or not str(operator_archive.get("lineage_fingerprint") or "").startswith(
                "sha256:"
            )
        ):
            raise AssertionError("operator settings archive lost exact preview lineage")
        if operator_raw_manifest.exists():
            raise AssertionError("operator settings archive retained a stale raw manifest")

        failed_settings_runtime_dir = root / "failed-settings-runtime"
        failed_settings_runtime_dir.mkdir()
        failed_settings_runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=failed_settings_runtime_dir
        )
        failed_settings = CalculationParametersBlock(runtime=failed_settings_runtime)
        failed_settings.ensure_initial_version(created_at="2026-07-21T00:00:00Z")
        with sqlite3.connect(failed_settings_runtime.db_path) as conn:
            conn.execute(
                """CREATE TRIGGER reject_operator_settings BEFORE INSERT
                   ON sheet_vitrina_v1_calculation_parameter_versions
                   WHEN NEW.source='operator_version'
                   BEGIN SELECT RAISE(ABORT,'injected settings failure'); END"""
            )
            conn.commit()
        failed_payload = {
            "effective_date": "2026-07-21",
            "buyout_rate": "0.9",
            "tax_rate": "0.06",
            "wb_agent_and_other_rate": "0.38",
            "acquiring_rate": "0",
            "wb_logistics_rate": "0",
            "wb_storage_rate": "0",
            "penalties_adjustments_rate": "0",
            "other_expense_rate": "0",
        }
        failed_preview = failed_settings.preview_version(failed_payload)
        try:
            failed_settings.create_version(
                failed_payload,
                preview_fingerprint=str(failed_preview["preview_fingerprint"]),
                created_by="smoke",
            )
        except sqlite3.IntegrityError as exc:
            if "injected settings failure" not in str(exc):
                raise AssertionError("settings abort failed for the wrong reason") from exc
        else:
            raise AssertionError("injected operator settings failure was accepted")
        failed_backup_root = (
            failed_settings_runtime_dir / "backups" / "calculation-parameters"
        )
        if list(failed_backup_root.glob("operator-settings-*.sqlite3")):
            raise AssertionError("failed settings save leaked a full-size raw backup")
        failed_archives = list(
            failed_backup_root.glob("operator-settings-*.sqlite3.zst")
        )
        if len(failed_archives) != 1:
            raise AssertionError("failed settings save did not retain one lossless archive")

        from apps.sqlite_backup_archive import apply_archive, build_plan

        retention_runtime_dir = root / "retention-runtime"
        retention_runtime_dir.mkdir()
        retention_runtime = RegistryUploadDbBackedRuntime(runtime_dir=retention_runtime_dir)
        with sqlite3.connect(retention_runtime.db_path) as conn:
            conn.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO evidence(value) VALUES('retention')")
            conn.commit()
        retention_parameters = CalculationParametersBlock(runtime=retention_runtime)
        retention_root = retention_runtime_dir / "backups" / "calculation-parameters"
        retention_root.mkdir(parents=True)
        for day in ("20260718", "20260719", "20260720", "20260721"):
            source = retention_root / f"functional-economics-daily-{day}.sqlite3"
            retention_runtime.backup_database(source)
            source.chmod(0o600)
            archive_plan = build_plan(source=source)
            apply_archive(
                source=source,
                archive=None,
                fingerprint=str(archive_plan["fingerprint"]),
            )
        retention = retention_parameters._prune_verified_functional_economics_archives(
            retention_root,
        )
        retained_archives = sorted(retention_root.glob("functional-economics-daily-*.sqlite3.zst"))
        if len(retained_archives) != 3 or len(retention.get("removed") or []) != 1:
            raise AssertionError(f"verified archive retention is not bounded: {retention}")
        retention_audit = retention_root / "functional-economics-archive-retention.jsonl"
        if not retention_audit.is_file() or retention_audit.stat().st_mode & 0o777 != 0o600:
            raise AssertionError("verified archive retention lacks a private durable audit")
        audit_rows = [json.loads(line) for line in retention_audit.read_text().splitlines() if line]
        if [row.get("status") for row in audit_rows] != ["intent", "completed"]:
            raise AssertionError(f"retention audit is not intent/completion durable: {audit_rows}")

        recovery_runtime_dir = root / "retention-recovery-runtime"
        recovery_runtime_dir.mkdir()
        recovery_runtime = RegistryUploadDbBackedRuntime(runtime_dir=recovery_runtime_dir)
        with sqlite3.connect(recovery_runtime.db_path) as conn:
            conn.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO evidence(value) VALUES('retention-recovery')")
            conn.commit()
        recovery_root = recovery_runtime_dir / "backups" / "calculation-parameters"
        recovery_root.mkdir(parents=True)
        recovery_source = recovery_root / "functional-economics-daily-20260718.sqlite3"
        recovery_runtime.backup_database(recovery_source)
        recovery_source.chmod(0o600)
        recovery_plan = build_plan(source=recovery_source)
        recovery_archive_result = apply_archive(
            source=recovery_source,
            archive=None,
            fingerprint=str(recovery_plan["fingerprint"]),
        )
        recovery_manifest_path = Path(str(recovery_archive_result["manifest_path"]))
        recovery_manifest = json.loads(recovery_manifest_path.read_text())
        recovery_archive_path = Path(str(recovery_manifest["archive_path"]))
        recovery_audit_path = (
            recovery_root / "functional-economics-archive-retention.jsonl"
        )
        legacy_row = {
            "contract_name": "functional_economics_archive_retention_v1",
            "archive_path": str(recovery_root / "already-removed.sqlite3.zst"),
            "archive_sha256": "sha256:legacy-archive",
            "source_sha256": "sha256:legacy-source",
            "source_size_bytes": 1,
            "removed_at": "2026-07-21T23:59:59Z",
        }
        recovery_audit_path.write_text(
            json.dumps(legacy_row, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        recovery_audit_path.chmod(0o600)
        calculation_parameters._append_retention_audit(
            recovery_audit_path,
            [
                {
                    "action_id": "interrupted-retention",
                    "status": "intent",
                    "archive_path": str(recovery_archive_path),
                    "manifest_path": str(recovery_manifest_path),
                    "archive_sha256": str(recovery_manifest["archive_sha256"]),
                    "source_sha256": str(recovery_manifest["source_sha256"]),
                    "source_size_bytes": int(recovery_manifest["source_size_bytes"]),
                    "backup_scope": "",
                    "settings_preview_fingerprint": "",
                    "intent_at": "2026-07-22T00:00:00Z",
                }
            ],
        )
        recovered = calculation_parameters._recover_retention_audit(recovery_root.resolve())
        if (
            len(recovered) != 1
            or not recovered[0].get("recovered")
            or recovery_archive_path.exists()
            or recovery_manifest_path.exists()
        ):
            raise AssertionError("interrupted archive retention was not resumed exactly")
        recovered_rows = [
            json.loads(line)
            for line in recovery_audit_path.read_text().splitlines()
            if line
        ]
        if [row.get("status") for row in recovered_rows] != [None, "intent", "completed"]:
            raise AssertionError("retention recovery did not preserve legacy history and completion")

        audit_before_failed_append = recovery_audit_path.read_bytes()
        with mock.patch(
            "packages.application.calculation_parameters.os.replace",
            side_effect=OSError("injected atomic audit failure"),
        ):
            try:
                calculation_parameters._append_retention_audit(
                    recovery_audit_path,
                    [{"action_id": "must-not-appear", "status": "intent"}],
                )
            except OSError as exc:
                if "injected atomic audit failure" not in str(exc):
                    raise
            else:
                raise AssertionError("failed atomic retention audit write was accepted")
        if recovery_audit_path.read_bytes() != audit_before_failed_append:
            raise AssertionError("failed retention audit append poisoned durable history")
        if list(recovery_root.glob("functional-economics-archive-retention.jsonl.tmp-*")):
            raise AssertionError("failed retention audit append leaked a temporary journal")

        newest_source = retention_root / "functional-economics-daily-20260722.sqlite3"
        retention_runtime.backup_database(newest_source)
        newest_source.chmod(0o600)
        newest_plan = build_plan(source=newest_source)
        apply_archive(
            source=newest_source,
            archive=None,
            fingerprint=str(newest_plan["fingerprint"]),
        )
        corrupt_retained = retention_root / "functional-economics-daily-20260720.sqlite3.zst"
        oldest_valid = retention_root / "functional-economics-daily-20260719.sqlite3.zst"
        corrupt_retained.write_bytes(b"corrupt retained archive")
        try:
            retention_parameters._prune_verified_functional_economics_archives(
                retention_root,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("corrupt retained archive was accepted by retention")
        if not oldest_valid.is_file():
            raise AssertionError("retention deleted an older valid archive before verifying all kept archives")

        preprune_runtime_dir = root / "preprune-runtime"
        preprune_runtime_dir.mkdir()
        preprune_runtime = RegistryUploadDbBackedRuntime(runtime_dir=preprune_runtime_dir)
        with sqlite3.connect(preprune_runtime.db_path) as conn:
            conn.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO evidence(value) VALUES('preprune')")
            conn.commit()
        preprune_parameters = CalculationParametersBlock(runtime=preprune_runtime)
        preprune_root = preprune_runtime_dir / "backups" / "calculation-parameters"
        preprune_root.mkdir(parents=True)
        for day in ("20260718", "20260719", "20260720"):
            source = preprune_root / f"functional-economics-daily-{day}.sqlite3"
            preprune_runtime.backup_database(source)
            source.chmod(0o600)
            plan = build_plan(source=source)
            apply_archive(
                source=source,
                archive=None,
                fingerprint=str(plan["fingerprint"]),
            )
        incoming_source = preprune_root / "functional-economics-daily-20260722.sqlite3"
        incoming_archive = Path(str(incoming_source) + ".zst")
        incoming_archive_manifest = incoming_archive.with_name(
            incoming_archive.name + ".manifest.json"
        )
        incoming_archive.mkdir()
        incoming_archive_manifest.mkdir()
        with mock.patch(
            "packages.application.calculation_parameters.current_business_date_iso",
            return_value="2026-07-22",
        ):
            try:
                preprune_parameters.prepare_functional_economics_backup()
            except ValueError as exc:
                if "archive is incomplete" not in str(exc):
                    raise AssertionError("non-file archive pair failed for the wrong reason") from exc
            else:
                raise AssertionError("non-file archive pair was accepted before backup preparation")
        if incoming_source.exists():
            raise AssertionError("non-file archive pair allowed a new raw backup")
        incoming_archive.rmdir()
        incoming_archive_manifest.rmdir()

        preprune_runtime.backup_database(incoming_source)
        incoming_source.chmod(0o600)
        incoming_raw_manifest = incoming_source.with_name(
            incoming_source.name + ".manifest.json"
        )
        incoming_raw_manifest.write_text("{}\n", encoding="utf-8")
        incoming_raw_manifest.chmod(0o600)
        archives_before_invalid_raw = sorted(preprune_root.glob("*.sqlite3.zst"))
        with mock.patch(
            "packages.application.calculation_parameters.current_business_date_iso",
            return_value="2026-07-22",
        ):
            try:
                preprune_parameters.prepare_functional_economics_backup()
            except ValueError as exc:
                if "provenance validation" not in str(exc):
                    raise AssertionError("invalid raw checkpoint failed for the wrong reason") from exc
            else:
                raise AssertionError("invalid raw checkpoint was accepted")
        if sorted(preprune_root.glob("*.sqlite3.zst")) != archives_before_invalid_raw:
            raise AssertionError("invalid raw checkpoint pruned a verified restore point")
        incoming_source.unlink()
        incoming_raw_manifest.unlink()

        with (
            mock.patch(
                "packages.application.calculation_parameters.current_business_date_iso",
                return_value="2026-07-22",
            ),
            mock.patch(
                "packages.application.calculation_parameters.shutil.disk_usage",
                return_value=SimpleNamespace(free=1),
            ),
        ):
            try:
                preprune_parameters.prepare_functional_economics_backup()
            except ValueError as exc:
                if "capacity" not in str(exc):
                    raise AssertionError("post-retention capacity failed for the wrong reason") from exc
            else:
                raise AssertionError("impossible post-retention capacity was accepted")
        if len(list(preprune_root.glob("functional-economics-daily-*.sqlite3.zst"))) != 2:
            raise AssertionError("daily retention slot was not reserved before capacity gate")

        for ordinal in range(3):
            source = preprune_root / (
                f"operator-settings-20260722T0{ordinal}0000Z-checkpoint.sqlite3"
            )
            preprune_runtime.backup_database(source)
            source.chmod(0o600)
            plan = build_plan(source=source)
            apply_archive(
                source=source,
                archive=None,
                fingerprint=str(plan["fingerprint"]),
            )
        with mock.patch(
            "packages.application.calculation_parameters.shutil.disk_usage",
            return_value=SimpleNamespace(free=1),
        ):
            try:
                preprune_parameters.prepare_operator_settings_backup(
                    preview_fingerprint="sha256:retention-slot-preview",
                )
            except ValueError as exc:
                if "capacity" not in str(exc):
                    raise AssertionError("settings post-retention capacity failed incorrectly") from exc
            else:
                raise AssertionError("impossible settings post-retention capacity was accepted")
        if len(list(preprune_root.glob("operator-settings-*.sqlite3.zst"))) != 2:
            raise AssertionError("settings retention slot was not reserved before capacity gate")

        manual_preprune_root = root / "backups" / "manual-preprune"
        manual_preprune_root.mkdir(parents=True)
        for ordinal in range(3):
            source = manual_preprune_root / (
                f"warehouse-functional-pre-sync-20260722T0{ordinal}0000Z.sqlite3"
            )
            preprune_runtime.backup_database(source)
            source.chmod(0o600)
            plan = build_plan(source=source)
            apply_archive(
                source=source,
                archive=None,
                fingerprint=str(plan["fingerprint"]),
            )
        with mock.patch(
            "packages.application.calculation_parameters.shutil.disk_usage",
            return_value=SimpleNamespace(free=1),
        ):
            try:
                preprune_parameters.preflight_fresh_economics_backup_capacity(
                    manual_preprune_root,
                )
            except ValueError as exc:
                if "capacity" not in str(exc):
                    raise AssertionError("manual post-retention capacity failed incorrectly") from exc
            else:
                raise AssertionError("impossible manual post-retention capacity was accepted")
        if len(list(manual_preprune_root.glob("warehouse-functional-pre-sync-*.sqlite3.zst"))) != 2:
            raise AssertionError("manual checkpoint retention slot was not reserved before capacity gate")

        capacity_runtime_dir = root / "capacity-runtime"
        capacity_runtime_dir.mkdir()
        capacity_db = capacity_runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(capacity_db) as conn:
            conn.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY)")
            conn.commit()
        capacity_parameters = CalculationParametersBlock(
            runtime=RegistryUploadDbBackedRuntime(runtime_dir=capacity_runtime_dir)
        )
        with (
            mock.patch(
                "packages.application.calculation_parameters.shutil.disk_usage",
                return_value=SimpleNamespace(free=1),
            ),
            mock.patch.object(
                capacity_parameters.runtime,
                "backup_database",
            ) as backup_database,
        ):
            try:
                capacity_parameters.prepare_functional_economics_backup()
            except ValueError as exc:
                if "daily backup and lossless archive" not in str(exc):
                    raise AssertionError("archive capacity failed for the wrong reason") from exc
            else:
                raise AssertionError("insufficient archive capacity was accepted")
            backup_database.assert_not_called()

        ten_gib = 10 * 1024 * 1024 * 1024
        with mock.patch(
            "packages.application.calculation_parameters.shutil.disk_usage",
            return_value=SimpleNamespace(free=100 * 1024 * 1024 * 1024),
        ):
            capacity = capacity_parameters._require_economics_backup_capacity(
                capacity_runtime_dir,
                source_size=ten_gib,
                raw_backup_exists=False,
            )
        if not capacity["same_filesystem"]:
            raise AssertionError("single-filesystem capacity fixture was not recognized")
        if capacity["required_free_bytes"] != (
            capacity["backup_required_free_bytes"]
            + capacity["runtime_required_free_bytes"]
        ):
            raise AssertionError("single-filesystem capacity did not combine backup and runtime needs")

        separate_backup_root = root / "separate-backup-mount"
        separate_backup_root.mkdir()

        def separate_disk_usage(path):
            free = 22 if Path(path).resolve() == separate_backup_root.resolve() else 5
            return SimpleNamespace(free=free * 1024 * 1024 * 1024)

        with (
            mock.patch(
                "packages.application.calculation_parameters.shutil.disk_usage",
                side_effect=separate_disk_usage,
            ),
            mock.patch(
                "packages.application.calculation_parameters._same_filesystem",
                return_value=False,
            ),
        ):
            separated = capacity_parameters._require_economics_backup_capacity(
                separate_backup_root,
                source_size=ten_gib,
                raw_backup_exists=False,
            )
        if separated["same_filesystem"]:
            raise AssertionError("split backup/runtime filesystems were collapsed")
        if separated["required_free_bytes"] != separated["backup_required_free_bytes"]:
            raise AssertionError("split filesystem gate double-counted runtime margin on backup mount")
        if separated["runtime_available_free_bytes"] <= separated["runtime_required_free_bytes"]:
            raise AssertionError("split runtime fixture does not prove its independent margin")

        def archived_disk_usage(path):
            free = 1 if Path(path).resolve() == separate_backup_root.resolve() else 5
            return SimpleNamespace(free=free * 1024 * 1024 * 1024)

        with (
            mock.patch(
                "packages.application.calculation_parameters.shutil.disk_usage",
                side_effect=archived_disk_usage,
            ),
            mock.patch(
                "packages.application.calculation_parameters._same_filesystem",
                return_value=False,
            ),
        ):
            archived_capacity = capacity_parameters._require_economics_backup_capacity(
                separate_backup_root,
                source_size=ten_gib,
                raw_backup_exists=False,
                archive_exists=True,
            )
        if archived_capacity["backup_required_free_bytes"] != 0:
            raise AssertionError("verified archive reuse reserved a second backup on its mount")
        if archived_capacity["runtime_required_free_bytes"] < 4 * 1024 * 1024 * 1024:
            raise AssertionError("verified archive reuse skipped the live-runtime margin")

        with (
            mock.patch.object(
                capacity_parameters.runtime,
                "coherent_backup_size_bytes",
                return_value=30 * 1024 * 1024 * 1024,
            ),
            mock.patch(
                "packages.application.calculation_parameters.shutil.disk_usage",
                side_effect=archived_disk_usage,
            ),
            mock.patch(
                "packages.application.calculation_parameters._same_filesystem",
                return_value=False,
            ),
        ):
            try:
                capacity_parameters._require_economics_backup_capacity(
                    separate_backup_root,
                    source_size=1 * 1024 * 1024 * 1024,
                    raw_backup_exists=False,
                    archive_exists=True,
                )
            except ValueError as exc:
                if "runtime-filesystem capacity" not in str(exc):
                    raise AssertionError("current runtime growth failed for the wrong reason") from exc
            else:
                raise AssertionError("stale morning archive understated current runtime capacity")

        queue_runtime_dir = root / "queue-runtime"
        queue_runtime_dir.mkdir()
        queue_db = queue_runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(queue_db) as conn:
            ensure_calculation_parameters_schema(conn)
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_proxy_targeted_recalc_queue(
                       request_id,effective_date,settings_version_id,status,created_at
                   ) VALUES('queue-1','2026-07-01','settings-1','pending','2026-07-01T00:00:00Z')"""
            )
            conn.commit()
        queue_parameters = CalculationParametersBlock(
            runtime=RegistryUploadDbBackedRuntime(runtime_dir=queue_runtime_dir)
        )
        archive_evidence = {"archive_path": "/backups/daily.sqlite3.zst", "zstd_test": "ok"}
        with mock.patch.object(
            queue_parameters,
            "publish_current_functional_economics",
            return_value={
                "plan_fingerprint": "sha256:queue",
                "changed_snapshot_count": 1,
                "database_written": True,
                "backup_archive": archive_evidence,
            },
        ):
            recalculation = queue_parameters.process_pending_targeted_recalculations(
                verified_backup={"integrity_check": "ok", "path": "/backups/daily.sqlite3"},
            )
        if recalculation.get("backup_archive") != archive_evidence:
            raise AssertionError("pending recalculation dropped archive evidence")

    print("warehouse_functional_backup_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
