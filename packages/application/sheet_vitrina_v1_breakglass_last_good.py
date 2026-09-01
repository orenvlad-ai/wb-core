"""Guarded public last-good overlay for an incomplete FBS lifecycle.

The contour is deliberately read-side only.  It never changes warehouse,
inventory-history, WB, ready-snapshot, capital or economics source rows.  A
single immutable operation stores exact cells copied from a verified published
source; Web Vitrina uses them only while the corresponding public cell is
empty.  Any newer/non-empty value always wins.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from packages.contracts.web_vitrina_contract import WebVitrinaContractRow


CONTRACT_NAME = "sheet_vitrina_v1_breakglass_last_good_v1"
OPERATIONS_TABLE = "sheet_vitrina_v1_breakglass_last_good_operations"
CELLS_TABLE = "sheet_vitrina_v1_breakglass_last_good_cells"
REVOCATIONS_TABLE = "sheet_vitrina_v1_breakglass_last_good_revocations"
REVOCATION_AUDIT_TABLE = "sheet_vitrina_v1_breakglass_last_good_revocation_audit"


class BreakglassLastGoodError(RuntimeError):
    pass


def ensure_breakglass_last_good_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {OPERATIONS_TABLE}(
            operation_id TEXT PRIMARY KEY,
            contract_name TEXT NOT NULL CHECK(contract_name='{CONTRACT_NAME}'),
            manifest_sha256 TEXT NOT NULL UNIQUE,
            source_capture_id TEXT NOT NULL,
            source_capture_digest TEXT NOT NULL,
            source_checkpoint_operation_id TEXT NOT NULL,
            source_checkpoint_digest TEXT NOT NULL,
            source_ready_plan_digest TEXT NOT NULL,
            cell_count INTEGER NOT NULL CHECK(cell_count>0),
            target_digest TEXT NOT NULL,
            non_target_digest TEXT NOT NULL,
            applied_at TEXT NOT NULL
                CHECK(substr(applied_at,-1,1)='Z' AND julianday(applied_at) IS NOT NULL),
            metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json))
        );
        CREATE TABLE IF NOT EXISTS {CELLS_TABLE}(
            operation_id TEXT NOT NULL REFERENCES {OPERATIONS_TABLE}(operation_id),
            row_id TEXT NOT NULL,
            target_date_from TEXT NOT NULL
                CHECK(length(target_date_from)=10 AND date(target_date_from)=target_date_from),
            value_json TEXT NOT NULL CHECK(json_valid(value_json)),
            source_business_date TEXT NOT NULL
                CHECK(length(source_business_date)=10 AND date(source_business_date)=source_business_date),
            source_kind TEXT NOT NULL,
            source_identity TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json)),
            PRIMARY KEY(operation_id,row_id)
        );
        CREATE TABLE IF NOT EXISTS {REVOCATIONS_TABLE}(
            revocation_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL REFERENCES {OPERATIONS_TABLE}(operation_id),
            reason TEXT NOT NULL,
            revoked_at TEXT NOT NULL
                CHECK(substr(revoked_at,-1,1)='Z' AND julianday(revoked_at) IS NOT NULL),
            UNIQUE(operation_id)
        );
        CREATE TABLE IF NOT EXISTS {REVOCATION_AUDIT_TABLE}(
            revocation_id TEXT PRIMARY KEY REFERENCES {REVOCATIONS_TABLE}(revocation_id),
            operation_id TEXT NOT NULL REFERENCES {OPERATIONS_TABLE}(operation_id),
            manifest_sha256 TEXT NOT NULL,
            target_prestate_digest TEXT NOT NULL,
            non_target_digest TEXT NOT NULL,
            backup_digest TEXT NOT NULL,
            applied_readback_digest TEXT NOT NULL,
            metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
            UNIQUE(operation_id)
        );
        CREATE TRIGGER IF NOT EXISTS breakglass_last_good_operations_no_update
        BEFORE UPDATE ON {OPERATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'breakglass last-good operations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS breakglass_last_good_operations_no_delete
        BEFORE DELETE ON {OPERATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'breakglass last-good operations are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS breakglass_last_good_cells_no_update
        BEFORE UPDATE ON {CELLS_TABLE}
        BEGIN SELECT RAISE(ABORT,'breakglass last-good cells are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS breakglass_last_good_cells_no_delete
        BEFORE DELETE ON {CELLS_TABLE}
        BEGIN SELECT RAISE(ABORT,'breakglass last-good cells are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS breakglass_last_good_revocations_no_update
        BEFORE UPDATE ON {REVOCATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'breakglass last-good revocations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS breakglass_last_good_revocations_no_delete
        BEFORE DELETE ON {REVOCATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'breakglass last-good revocations are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS breakglass_last_good_revocation_audit_no_update
        BEFORE UPDATE ON {REVOCATION_AUDIT_TABLE}
        BEGIN SELECT RAISE(ABORT,'breakglass last-good revocation audit is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS breakglass_last_good_revocation_audit_no_delete
        BEFORE DELETE ON {REVOCATION_AUDIT_TABLE}
        BEGIN SELECT RAISE(ABORT,'breakglass last-good revocation audit is append-only'); END;
        """
    )


def apply_breakglass_last_good_overlay(
    rows: Iterable[WebVitrinaContractRow],
    *,
    db_path: Path,
    date_columns: Sequence[str],
) -> list[WebVitrinaContractRow]:
    """Fill only blank cells from the latest non-revoked immutable operation."""

    source_rows = list(rows)
    payload = read_active_breakglass_last_good(db_path)
    if payload is None:
        return source_rows
    cells = {str(item["row_id"]): item for item in payload["cells"]}
    applied_at = str(payload["operation"]["applied_at"])
    operation_id = str(payload["operation"]["operation_id"])
    result: list[WebVitrinaContractRow] = []
    allowed_dates = [str(item) for item in date_columns]
    for row in source_rows:
        cell = cells.get(str(row.row_id))
        if cell is None:
            result.append(row)
            continue
        target_from = date.fromisoformat(str(cell["target_date_from"]))
        values = dict(row.values_by_date)
        presentation = dict(row.presentation_by_date)
        changed = False
        for business_date in allowed_dates:
            try:
                eligible = date.fromisoformat(business_date) >= target_from
            except ValueError:
                eligible = False
            if not eligible or values.get(business_date) not in {None, ""}:
                continue
            values[business_date] = cell["value"]
            presentation[business_date] = _provisional_presentation(
                operation_id=operation_id,
                source_business_date=str(cell["source_business_date"]),
                source_identity=str(cell["source_identity"]),
                source_digest=str(cell["source_digest"]),
            )
            changed = True
        result.append(
            replace(
                row,
                values_by_date=values,
                presentation_by_date=presentation,
                row_last_updated_at=max(str(row.row_last_updated_at or ""), applied_at),
            )
            if changed
            else row
        )
    return result


def read_active_breakglass_last_good(db_path: Path) -> dict[str, Any] | None:
    if not Path(db_path).is_file():
        return None
    with _connect_readonly(Path(db_path)) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if {OPERATIONS_TABLE, CELLS_TABLE, REVOCATIONS_TABLE} - tables:
            return None
        operation = conn.execute(
            f"""SELECT operation.* FROM {OPERATIONS_TABLE} operation
                LEFT JOIN {REVOCATIONS_TABLE} revoked
                  ON revoked.operation_id=operation.operation_id
                WHERE revoked.operation_id IS NULL
                ORDER BY operation.applied_at DESC,operation.operation_id DESC
                LIMIT 1"""
        ).fetchone()
        if operation is None:
            return None
        cells = conn.execute(
            f"""SELECT row_id,target_date_from,value_json,source_business_date,
                       source_kind,source_identity,source_digest,provenance_json
                FROM {CELLS_TABLE} WHERE operation_id=? ORDER BY row_id""",
            (str(operation["operation_id"]),),
        ).fetchall()
        if int(operation["cell_count"]) != len(cells):
            raise BreakglassLastGoodError(
                "active breakglass operation cell count is inconsistent"
            )
        normalized_cells = [
            {
                "row_id": str(item["row_id"]),
                "target_date_from": str(item["target_date_from"]),
                "value": json.loads(str(item["value_json"])),
                "source_business_date": str(item["source_business_date"]),
                "source_kind": str(item["source_kind"]),
                "source_identity": str(item["source_identity"]),
                "source_digest": str(item["source_digest"]),
                "provenance": json.loads(str(item["provenance_json"])),
            }
            for item in cells
        ]
        digest = _fingerprint(
            [[item["row_id"], item["target_date_from"], item["value"]] for item in normalized_cells]
        )
        if digest != str(operation["target_digest"]):
            raise BreakglassLastGoodError(
                "active breakglass operation target digest is inconsistent"
            )
        return {
            "operation": {key: operation[key] for key in operation.keys()},
            "cells": normalized_cells,
        }


def persist_breakglass_last_good(
    conn: sqlite3.Connection,
    *,
    operation: Mapping[str, Any],
    cells: Sequence[Mapping[str, Any]],
) -> None:
    ensure_breakglass_last_good_schema(conn)
    operation_id = str(operation["operation_id"])
    if conn.execute(
        f"SELECT 1 FROM {OPERATIONS_TABLE} WHERE operation_id=?",
        (operation_id,),
    ).fetchone() is not None:
        raise BreakglassLastGoodError("breakglass operation identity already exists")
    if not cells:
        raise BreakglassLastGoodError("breakglass operation must contain cells")
    normalized = normalize_manifest_cells(cells)
    target_digest = _fingerprint(
        [[item["row_id"], item["target_date_from"], item["value"]] for item in normalized]
    )
    if target_digest != str(operation["target_digest"]):
        raise BreakglassLastGoodError("breakglass manifest target digest changed")
    conn.execute(
        f"""INSERT INTO {OPERATIONS_TABLE}(
                operation_id,contract_name,manifest_sha256,source_capture_id,
                source_capture_digest,source_checkpoint_operation_id,
                source_checkpoint_digest,source_ready_plan_digest,cell_count,
                target_digest,non_target_digest,applied_at,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            CONTRACT_NAME,
            str(operation["manifest_sha256"]),
            str(operation["source_capture_id"]),
            str(operation["source_capture_digest"]),
            str(operation["source_checkpoint_operation_id"]),
            str(operation["source_checkpoint_digest"]),
            str(operation["source_ready_plan_digest"]),
            len(normalized),
            target_digest,
            str(operation["non_target_digest"]),
            str(operation["applied_at"]),
            _canonical_json(dict(operation.get("metadata") or {})),
        ),
    )
    conn.executemany(
        f"""INSERT INTO {CELLS_TABLE}(
                operation_id,row_id,target_date_from,value_json,
                source_business_date,source_kind,source_identity,source_digest,
                provenance_json
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
        [
            (
                operation_id,
                item["row_id"],
                item["target_date_from"],
                _canonical_json(item["value"]),
                item["source_business_date"],
                item["source_kind"],
                item["source_identity"],
                item["source_digest"],
                _canonical_json(item["provenance"]),
            )
            for item in normalized
        ],
    )


def revoke_breakglass_last_good(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    revocation_id: str,
    reason: str,
    revoked_at: str,
    manifest_sha256: str,
    target_prestate_digest: str,
    non_target_digest: str,
    backup_digest: str,
    applied_readback_digest: str,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    ensure_breakglass_last_good_schema(conn)
    if conn.execute(
        f"SELECT 1 FROM {OPERATIONS_TABLE} WHERE operation_id=?",
        (operation_id,),
    ).fetchone() is None:
        raise BreakglassLastGoodError("breakglass operation is absent")
    operation = conn.execute(
        f"SELECT manifest_sha256 FROM {OPERATIONS_TABLE} WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if str(operation[0]) != str(manifest_sha256):
        raise BreakglassLastGoodError("breakglass revocation manifest binding changed")
    conn.execute(
        f"""INSERT INTO {REVOCATIONS_TABLE}(
                revocation_id,operation_id,reason,revoked_at
            ) VALUES(?,?,?,?)""",
        (revocation_id, operation_id, str(reason), revoked_at),
    )
    conn.execute(
        f"""INSERT INTO {REVOCATION_AUDIT_TABLE}(
                revocation_id,operation_id,manifest_sha256,target_prestate_digest,
                non_target_digest,backup_digest,applied_readback_digest,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            revocation_id,
            operation_id,
            str(manifest_sha256),
            str(target_prestate_digest),
            str(non_target_digest),
            str(backup_digest),
            str(applied_readback_digest),
            _canonical_json(dict(metadata or {})),
        ),
    )


def read_breakglass_last_good_revocation(
    db_path: Path,
    *,
    operation_id: str,
) -> dict[str, Any] | None:
    """Read one exact append-only revocation without opening a write handle."""

    if not Path(db_path).is_file():
        return None
    with _connect_readonly(Path(db_path)) as conn:
        if {REVOCATIONS_TABLE, REVOCATION_AUDIT_TABLE} - {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }:
            return None
        row = conn.execute(
            f"""SELECT revocation.revocation_id,revocation.operation_id,
                       revocation.reason,revocation.revoked_at,
                       audit.manifest_sha256,audit.target_prestate_digest,
                       audit.non_target_digest,audit.backup_digest,
                       audit.applied_readback_digest,audit.metadata_json
                FROM {REVOCATIONS_TABLE} revocation
                JOIN {REVOCATION_AUDIT_TABLE} audit
                  ON audit.revocation_id=revocation.revocation_id
                WHERE revocation.operation_id=?""",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "revocation_id": str(row["revocation_id"]),
            "operation_id": str(row["operation_id"]),
            "reason": str(row["reason"]),
            "revoked_at": str(row["revoked_at"]),
            "manifest_sha256": str(row["manifest_sha256"]),
            "target_prestate_digest": str(row["target_prestate_digest"]),
            "non_target_digest": str(row["non_target_digest"]),
            "backup_digest": str(row["backup_digest"]),
            "applied_readback_digest": str(row["applied_readback_digest"]),
            "metadata": json.loads(str(row["metadata_json"])),
        }


def normalize_manifest_cells(cells: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in sorted(cells, key=lambda item: str(item.get("row_id") or "")):
        row_id = str(raw.get("row_id") or "").strip()
        if not row_id or row_id in seen:
            raise BreakglassLastGoodError("breakglass row identities must be unique")
        seen.add(row_id)
        value = raw.get("value")
        if value is None or value == "":
            raise BreakglassLastGoodError("breakglass source values must be non-empty")
        target_date_from = str(raw.get("target_date_from") or "")
        source_business_date = str(raw.get("source_business_date") or "")
        try:
            date.fromisoformat(target_date_from)
            date.fromisoformat(source_business_date)
        except ValueError as exc:
            raise BreakglassLastGoodError("breakglass business date is invalid") from exc
        source_kind = str(raw.get("source_kind") or "").strip()
        source_identity = str(raw.get("source_identity") or "").strip()
        source_digest = str(raw.get("source_digest") or "").strip()
        if not source_kind or not source_identity or not source_digest.startswith("sha256:"):
            raise BreakglassLastGoodError("breakglass source provenance is incomplete")
        result.append(
            {
                "row_id": row_id,
                "target_date_from": target_date_from,
                "value": value,
                "source_business_date": source_business_date,
                "source_kind": source_kind,
                "source_identity": source_identity,
                "source_digest": source_digest,
                "provenance": dict(raw.get("provenance") or {}),
            }
        )
    return result


def target_cells_digest(cells: Sequence[Mapping[str, Any]]) -> str:
    normalized = normalize_manifest_cells(cells)
    return _fingerprint(
        [[item["row_id"], item["target_date_from"], item["value"]] for item in normalized]
    )


def _provisional_presentation(
    *,
    operation_id: str,
    source_business_date: str,
    source_identity: str,
    source_digest: str,
) -> dict[str, str]:
    reason = (
        "Последнее подтверждённое опубликованное значение сохранено временно: "
        f"текущая FBS lifecycle неполна; источник {source_business_date}."
    )
    return {
        "state": "",
        "tone": "neutral",
        "reason": reason,
        "source": CONTRACT_NAME,
        "quality_state": "last_good_provisional",
        "quality_label": "Последнее подтверждённое",
        "quality_reason": reason,
        "operation_id": operation_id,
        "source_identity": source_identity,
        "source_digest": source_digest,
    }


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
