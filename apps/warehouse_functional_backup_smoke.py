#!/usr/bin/env python3
"""Smoke the coherent pre-sync backup command without business-row mutation."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.warehouse_functional_runner as functional_runner  # noqa: E402

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

    print("warehouse_functional_backup_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
