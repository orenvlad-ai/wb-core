#!/usr/bin/env python3
"""Smoke the policy-managed pre-sync domain checkpoint contract."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.warehouse_functional_runner import run  # noqa: E402
from packages.application.calculation_parameters import (  # noqa: E402
    CalculationParametersBlock,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


def main() -> int:
    with TemporaryDirectory(prefix="warehouse-functional-backup-smoke-") as raw_dir:
        root = Path(raw_dir)
        runtime_dir = root / "runtime"
        runtime_dir.mkdir()
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "CREATE TABLE sheet_vitrina_v1_warehouse_smoke("
                "id TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_warehouse_smoke VALUES('source-1','unchanged')"
            )
            conn.execute(
                "CREATE TABLE wb_finance_weekly_raw_rows("
                "id TEXT PRIMARY KEY,payload_json TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO wb_finance_weekly_raw_rows VALUES('raw-1','secret-raw')"
            )
            conn.commit()

        result = run(
            argparse.Namespace(
                runtime_dir=str(runtime_dir),
                env_file="",
                command="backup",
                backup_dir=str((root / "backups").resolve()),
            )
        )
        recovery = dict(result.get("backup") or {})
        _assert(result.get("status") == "success", "backup command reports success")
        _assert(
            recovery.get("tier") == "T2"
            and recovery.get("lifecycle") == "retained",
            "pre-sync recovery is a retained T2 domain checkpoint",
        )
        checkpoint = next(
            artifact
            for artifact in recovery.get("artifacts") or []
            if artifact.get("artifact_kind") == "domain_checkpoint"
        )
        checkpoint_path = Path(str(checkpoint["path"]))
        _assert(checkpoint_path.is_file(), "domain checkpoint exists")
        _assert(
            checkpoint_path.stat().st_mode & 0o777 == 0o600,
            "domain checkpoint mode is 0600",
        )
        with sqlite3.connect(checkpoint_path) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            stored = conn.execute(
                "SELECT value FROM sheet_vitrina_v1_warehouse_smoke WHERE id='source-1'"
            ).fetchone()
        _assert(stored == ("unchanged",), "domain checkpoint retains warehouse data")
        _assert(
            "wb_finance_weekly_raw_rows" not in tables,
            "domain checkpoint excludes Finance raw",
        )
        with sqlite3.connect(runtime.db_path) as conn:
            live_domain = conn.execute(
                "SELECT value FROM sheet_vitrina_v1_warehouse_smoke WHERE id='source-1'"
            ).fetchone()
            live_raw = conn.execute(
                "SELECT payload_json FROM wb_finance_weekly_raw_rows WHERE id='raw-1'"
            ).fetchone()
        _assert(
            live_domain == ("unchanged",) and live_raw == ("secret-raw",),
            "checkpoint command changes no business row",
        )

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
            _assert("absolute" in str(exc), "relative path fails for the right reason")
        else:
            raise AssertionError("relative backup directory was unexpectedly accepted")

        parameters = CalculationParametersBlock(runtime=runtime)
        before_artifacts = sorted(
            str(path)
            for path in (runtime_dir / "warehouse-recovery").rglob("*")
            if path.is_file()
        )
        economics = parameters.prepare_functional_economics_backup()
        settings = parameters.prepare_operator_settings_backup(
            preview_fingerprint="sha256:smoke-preview"
        )
        after_artifacts = sorted(
            str(path)
            for path in (runtime_dir / "warehouse-recovery").rglob("*")
            if path.is_file()
        )
        _assert(
            economics.get("full_database_copy") is False
            and economics.get("copy_bytes") == 0
            and settings.get("full_database_copy") is False
            and settings.get("copy_bytes") == 0,
            "bounded compatibility descriptors never request a full backup",
        )
        _assert(
            before_artifacts == after_artifacts,
            "bounded compatibility descriptors create no recovery bytes",
        )

    print("warehouse_functional_backup_smoke: T2 domain checkpoint ok")
    return 0


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
