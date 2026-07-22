#!/usr/bin/env python3
"""Smoke the coherent pre-sync backup command without business-row mutation."""

from __future__ import annotations

import argparse
import hashlib
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
