#!/usr/bin/env python3
"""Versioned rollback export from the isolated Autoanswers store.

The normal runtime never dual-writes the registry database.  If an older
release must be restored, this runner is executed while the registry service
and both Autoanswers timers/services are quiesced.  It snapshots the legacy
tables first, then replaces only the Autoanswers table set in one SQLite
transaction and proves per-table readback.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any, Mapping, Sequence
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sqlite_contention import connect_sqlite  # noqa: E402
from packages.application.wb_autoanswers_runtime import (  # noqa: E402
    AUTOANSWERS_DB_FILENAME,
    AUTOANSWERS_STORE_COPY_BATCH_SIZE,
    LEGACY_RUNTIME_DB_FILENAME,
    _autoanswers_schema_table_names,
    _copy_legacy_autoanswers_snapshot,
    _fsync_directory,
    _fsync_path,
    _quote_identifier,
    _sqlite_table_columns,
    _sqlite_table_exists,
    _update_row_digest,
)


CONTRACT_NAME = "wb_autoanswers_store_rollback_v1"
MIN_HEADROOM_BYTES = 256 * 1024 * 1024
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    _fsync_path(temporary)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _table_evidence(
    path: Path,
    table_names: Sequence[str],
) -> dict[str, dict[str, Any]]:
    uri = f"file:{path.resolve()}?mode=ro"
    evidence: dict[str, dict[str, Any]] = {}
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        try:
            for table_name in table_names:
                if not _sqlite_table_exists(conn, table_name):
                    continue
                columns = _sqlite_table_columns(conn, table_name)
                quoted_columns = ",".join(
                    _quote_identifier(column) for column in columns
                )
                digest = hashlib.sha256()
                row_count = 0
                cursor = conn.execute(
                    f"SELECT {quoted_columns} FROM "
                    f"{_quote_identifier(table_name)} ORDER BY rowid"
                )
                while True:
                    rows = cursor.fetchmany(AUTOANSWERS_STORE_COPY_BATCH_SIZE)
                    if not rows:
                        break
                    for row in rows:
                        _update_row_digest(
                            digest,
                            tuple(row[column] for column in columns),
                        )
                    row_count += len(rows)
                evidence[table_name] = {
                    "row_count": row_count,
                    "sha256": digest.hexdigest(),
                    "columns": columns,
                }
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return evidence


def _fingerprint(
    source_evidence: Mapping[str, Any],
    legacy_evidence: Mapping[str, Any],
) -> str:
    material = {
        "contract_name": CONTRACT_NAME,
        "source": source_evidence,
        "legacy": legacy_evidence,
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def build_plan(runtime_dir: Path) -> dict[str, Any]:
    runtime_dir = Path(runtime_dir)
    isolated_path = runtime_dir / AUTOANSWERS_DB_FILENAME
    legacy_path = runtime_dir / LEGACY_RUNTIME_DB_FILENAME
    if not isolated_path.is_file() or not legacy_path.is_file():
        raise RuntimeError(
            "rollback export requires both isolated and legacy SQLite stores"
        )
    table_names = _autoanswers_schema_table_names()
    source_evidence = _table_evidence(isolated_path, table_names)
    legacy_evidence = _table_evidence(legacy_path, table_names)
    missing = sorted(set(source_evidence) - set(legacy_evidence))
    if missing:
        raise RuntimeError(
            "legacy store is missing Autoanswers tables: " + ",".join(missing)
        )
    available_bytes = shutil.disk_usage(runtime_dir).free
    required_bytes = isolated_path.stat().st_size * 2 + MIN_HEADROOM_BYTES
    return {
        "contract_name": CONTRACT_NAME,
        "status": "planned",
        "source_database": AUTOANSWERS_DB_FILENAME,
        "target_database": LEGACY_RUNTIME_DB_FILENAME,
        "table_count": len(source_evidence),
        "source_evidence": source_evidence,
        "legacy_evidence": legacy_evidence,
        "fingerprint": _fingerprint(source_evidence, legacy_evidence),
        "available_bytes": available_bytes,
        "required_bytes": required_bytes,
        "capacity_ok": available_bytes >= required_bytes,
        "changed_tables": sorted(
            table_name
            for table_name, source in source_evidence.items()
            if int(source.get("row_count") or 0)
            != int((legacy_evidence.get(table_name) or {}).get("row_count") or 0)
            or str(source.get("sha256") or "")
            != str((legacy_evidence.get(table_name) or {}).get("sha256") or "")
        ),
        "non_target_scope": "all non-Autoanswers registry tables remain untouched",
    }


def _replace_autoanswers_tables(
    *,
    source_path: Path,
    target_path: Path,
    table_names: Sequence[str],
    expected_source: Mapping[str, Any],
) -> dict[str, Any]:
    source_uri = f"file:{source_path.resolve()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=30, isolation_level=None)
    target = connect_sqlite(
        target_path,
        timeout_ms=300_000,
        priority="background",
        isolation_level=None,
    )
    source.row_factory = sqlite3.Row
    target.row_factory = sqlite3.Row
    try:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        target.execute("PRAGMA foreign_keys=OFF")
        target.execute("BEGIN IMMEDIATE")
        try:
            for table_name in reversed(table_names):
                if table_name in expected_source:
                    target.execute(
                        f"DELETE FROM {_quote_identifier(table_name)}"
                    )
            target_evidence: dict[str, dict[str, Any]] = {}
            for table_name in table_names:
                if table_name not in expected_source:
                    continue
                source_columns = _sqlite_table_columns(source, table_name)
                target_columns = _sqlite_table_columns(target, table_name)
                columns = [
                    column
                    for column in source_columns
                    if column in target_columns
                ]
                if columns != list(
                    (expected_source.get(table_name) or {}).get("columns") or []
                ):
                    raise RuntimeError(
                        f"rollback schema mismatch for {table_name}"
                    )
                quoted_columns = ",".join(
                    _quote_identifier(column) for column in columns
                )
                placeholders = ",".join("?" for _ in columns)
                digest = hashlib.sha256()
                row_count = 0
                cursor = source.execute(
                    f"SELECT {quoted_columns} FROM "
                    f"{_quote_identifier(table_name)} ORDER BY rowid"
                )
                while True:
                    rows = cursor.fetchmany(AUTOANSWERS_STORE_COPY_BATCH_SIZE)
                    if not rows:
                        break
                    values = [
                        tuple(row[column] for column in columns) for row in rows
                    ]
                    for row in values:
                        _update_row_digest(digest, row)
                    target.executemany(
                        f"INSERT INTO {_quote_identifier(table_name)}"
                        f"({quoted_columns}) VALUES({placeholders})",
                        values,
                    )
                    row_count += len(values)
                expected = dict(expected_source.get(table_name) or {})
                if (
                    row_count != int(expected.get("row_count") or 0)
                    or digest.hexdigest() != str(expected.get("sha256") or "")
                ):
                    raise RuntimeError(
                        f"rollback source changed for {table_name}"
                    )
                target_digest = hashlib.sha256()
                target_count = 0
                target_cursor = target.execute(
                    f"SELECT {quoted_columns} FROM "
                    f"{_quote_identifier(table_name)} ORDER BY rowid"
                )
                while True:
                    rows = target_cursor.fetchmany(
                        AUTOANSWERS_STORE_COPY_BATCH_SIZE
                    )
                    if not rows:
                        break
                    for row in rows:
                        _update_row_digest(
                            target_digest,
                            tuple(row[column] for column in columns),
                        )
                    target_count += len(rows)
                if (
                    target_count != row_count
                    or target_digest.hexdigest() != digest.hexdigest()
                ):
                    raise RuntimeError(
                        f"rollback target readback failed for {table_name}"
                    )
                target_evidence[table_name] = {
                    "row_count": target_count,
                    "sha256": target_digest.hexdigest(),
                    "matching": True,
                }
            foreign_key_rows = []
            for table_name in expected_source:
                foreign_key_rows.extend(
                    target.execute(
                        "PRAGMA foreign_key_check("
                        + _quote_identifier(table_name)
                        + ")"
                    ).fetchall()
                )
            if foreign_key_rows:
                raise RuntimeError(
                    "rollback target foreign-key readback failed"
                )
            target.commit()
            source.commit()
        except Exception:
            target.rollback()
            source.rollback()
            raise
        return {
            "table_evidence": target_evidence,
            "foreign_key_check_rows": len(foreign_key_rows),
        }
    finally:
        source.close()
        target.close()


def apply_rollback_export(
    runtime_dir: Path,
    *,
    expected_fingerprint: str,
) -> dict[str, Any]:
    if not _truthy(os.environ.get("WB_AUTOANSWERS_FORCE_OFF")):
        raise RuntimeError("rollback export requires WB_AUTOANSWERS_FORCE_OFF=true")
    if not _truthy(
        os.environ.get("WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE")
    ):
        raise RuntimeError(
            "rollback export requires the deployment service quiet window"
        )
    plan = build_plan(runtime_dir)
    if str(expected_fingerprint or "") != str(plan["fingerprint"]):
        raise RuntimeError("rollback export fingerprint changed; request a new plan")
    if not bool(plan["capacity_ok"]):
        raise RuntimeError("insufficient capacity for rollback snapshot")

    runtime_dir = Path(runtime_dir)
    isolated_path = runtime_dir / AUTOANSWERS_DB_FILENAME
    legacy_path = runtime_dir / LEGACY_RUNTIME_DB_FILENAME
    backup_dir = runtime_dir / "backups" / "wb_autoanswers_store_rollback_v1"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (
        datetime.now(timezone.utc)
        .strftime("%Y%m%dT%H%M%SZ")
    )
    backup_path = backup_dir / (
        f"legacy-autoanswers-{stamp}-{uuid4().hex[:12]}.sqlite3"
    )
    backup_manifest_path = backup_path.with_suffix(".manifest.json")
    backup = _copy_legacy_autoanswers_snapshot(
        legacy_path=legacy_path,
        candidate_path=backup_path,
        table_names=_autoanswers_schema_table_names(),
    )
    os.chmod(backup_path, 0o600)
    _fsync_path(backup_path)
    backup_manifest = {
        "contract_name": CONTRACT_NAME,
        "status": "backup_verified",
        "created_at": _now(),
        "backup_filename": backup_path.name,
        "backup_size_bytes": backup_path.stat().st_size,
        "rollback_fingerprint": plan["fingerprint"],
        "table_evidence": backup["table_evidence"],
        "integrity_check": backup["integrity_check"],
        "foreign_key_check_rows": backup["foreign_key_check_rows"],
    }
    _write_private_json(backup_manifest_path, backup_manifest)

    readback = _replace_autoanswers_tables(
        source_path=isolated_path,
        target_path=legacy_path,
        table_names=_autoanswers_schema_table_names(),
        expected_source=dict(plan["source_evidence"]),
    )
    final_evidence = _table_evidence(
        legacy_path,
        _autoanswers_schema_table_names(),
    )
    if final_evidence != plan["source_evidence"]:
        raise RuntimeError("rollback export post-commit reconciliation failed")
    result = {
        "contract_name": CONTRACT_NAME,
        "status": "applied",
        "applied_at": _now(),
        "fingerprint": plan["fingerprint"],
        "source_database": AUTOANSWERS_DB_FILENAME,
        "target_database": LEGACY_RUNTIME_DB_FILENAME,
        "changed_tables": plan["changed_tables"],
        "backup": {
            "filename": backup_path.name,
            "manifest_filename": backup_manifest_path.name,
            "size_bytes": backup_path.stat().st_size,
            "integrity_check": backup["integrity_check"],
        },
        "readback": readback,
        "source_preserved": True,
        "non_target_scope": plan["non_target_scope"],
    }
    evidence_path = (
        runtime_dir / "wb_autoanswers_store_rollback_v1.latest.json"
    )
    _write_private_json(evidence_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--fingerprint", default="")
    args = parser.parse_args()
    runtime_dir = args.runtime_dir.expanduser().resolve()
    lock_path = runtime_dir / ".wb_autoanswers_store_rollback.lock"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            result = (
                build_plan(runtime_dir)
                if args.action == "plan"
                else apply_rollback_export(
                    runtime_dir,
                    expected_fingerprint=str(args.fingerprint or ""),
                )
            )
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
