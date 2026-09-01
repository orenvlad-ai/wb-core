#!/usr/bin/env python3
"""One-submit break-glass publication of verified public last-good cells."""

from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sheet_vitrina_v1_breakglass_last_good import (  # noqa: E402
    CELLS_TABLE,
    CONTRACT_NAME,
    OPERATIONS_TABLE,
    REVOCATION_AUDIT_TABLE,
    REVOCATIONS_TABLE,
    BreakglassLastGoodError,
    persist_breakglass_last_good,
    read_active_breakglass_last_good,
    read_breakglass_last_good_revocation,
    revoke_breakglass_last_good,
    target_cells_digest,
)


CAPTURES_TABLE = "sheet_vitrina_v1_inventory_history_captures"
COMPONENTS_TABLE = "sheet_vitrina_v1_inventory_history_components"
CURRENT_CONFIG_TABLE = "registry_upload_current_state"
CONFIG_TABLE = "registry_upload_config_v2"
SOURCE_CAPTURE_KIND = "inventory_history_published_capture"
SOURCE_SEALED_ECONOMICS_KIND = "sealed_economics_before_plan_json"
WAC_KEYS = {
    "our_wb_unit_cost_rub",
    "total_our_wb_unit_cost_rub",
}
ECONOMICS_KEYS = {
    "proxy_profit_3_rub",
    "total_proxy_profit_3_rub",
    "proxy_margin_3_pct",
    "proxy_margin_3_pct_total",
    "proxy_profit_4_rub",
    "total_proxy_profit_4_rub",
    "proxy_margin_4_pct",
    "proxy_margin_4_pct_total",
    "proxy_margin_per_unit_rub",
    "proxy_margin_per_unit_rub_total",
}
NON_TARGET_TABLES = (
    "registry_upload_current_state",
    "registry_upload_config_v2",
    "sheet_vitrina_v1_warehouse_functional_active",
    "sheet_vitrina_v1_warehouse_wb_snapshots",
    "sheet_vitrina_v1_ff_pool_balances",
    "sheet_vitrina_v1_ff_pool_fbs_lifecycle_current",
    "sheet_vitrina_v1_inventory_history_captures",
    "sheet_vitrina_v1_inventory_history_components",
    "sheet_vitrina_v1_inventory_history_finalizations",
    "sheet_vitrina_v1_ready_snapshots",
)
NON_TARGET_TABLE_PREFIXES = (
    "sheet_vitrina_v1_own_capital_",
    "sheet_vitrina_v1_inventory_history_",
    "sheet_vitrina_v1_warehouse_",
    "sheet_vitrina_v1_ff_pool_",
)
PRODUCTION_CELL_COUNT = 303
PRODUCTION_FAMILY_COUNTS = {
    "functional_economics": 167,
    "functional_wac": 34,
    "inventory_combined_total": 34,
    "inventory_fbs_facility": 68,
}
PRODUCTION_SOURCE_EMPTY_IDENTITIES = {
    "SKU:497413772|proxy_margin_3_pct",
    "SKU:497413772|proxy_margin_4_pct",
    "SKU:497413772|proxy_margin_per_unit_rub",
}
PRODUCTION_INVENTORY_TOTALS = {
    "Orenburg": 25920,
    "Moscow": 72898,
    "FBS": 98818,
    "WB": 44428,
    "combined": 143246,
}
SQLITE_CANONICAL_VALUE_CONTRACT = "wbc0027_sqlite_scalar_canonical_json/v1"


class BreakglassRunnerError(RuntimeError):
    pass


def main() -> None:
    args = _parse_args()
    if args.mode == "plan":
        payload = build_manifest(
            db_path=args.db_path,
            operation_id=args.operation_id,
            source_capture_id=args.source_capture_id,
            economics_source_path=args.economics_source_path,
            expected_economics_source_sha256=args.expected_economics_source_sha256,
            expected_raw_plan_sha256=args.expected_raw_plan_sha256,
            economics_patch_index=args.economics_patch_index,
            economics_bundle_version=args.economics_bundle_version,
            economics_ready_as_of=args.economics_ready_as_of,
            economics_snapshot_id=args.economics_snapshot_id,
            economics_column_date=args.economics_column_date,
            expected_capture_sha256=args.expected_capture_sha256,
            expected_capture_sequence=args.expected_capture_sequence,
            expected_capture_captured_at=args.expected_capture_captured_at,
            public_date_columns=args.public_date_column,
            expected_cell_count=args.expected_cell_count,
            created_at=_utc_now(),
        )
        path = _write_immutable_json(args.evidence_dir / f"{args.operation_id}.manifest.json", payload)
        print(_canonical_json({"status": "planned", "path": str(path), "sha256": _file_sha256(path), "cell_count": len(payload["cells"]), "target_digest": payload["target_digest"], "non_target_digest": payload["non_target_digest"]}))
        return
    if args.mode == "apply":
        receipt = apply_manifest(
            db_path=args.db_path,
            manifest_path=args.manifest,
            expected_manifest_sha256=args.expected_manifest_sha256,
            operation_id=args.operation_id,
            evidence_dir=args.evidence_dir,
            writer_lock_path=args.writer_lock_path,
        )
        print(_canonical_json(receipt))
        return
    if args.mode == "readback":
        print(_canonical_json(readback_manifest(
            db_path=args.db_path,
            manifest_path=args.manifest,
            expected_manifest_sha256=args.expected_manifest_sha256,
            operation_id=args.operation_id,
        )))
        return
    if args.mode == "revoke":
        print(_canonical_json(revoke_manifest(
            db_path=args.db_path,
            manifest_path=args.manifest,
            expected_manifest_sha256=args.expected_manifest_sha256,
            operation_id=args.operation_id,
            revocation_id=args.revocation_id,
            reason=args.reason,
            evidence_dir=args.evidence_dir,
            writer_lock_path=args.writer_lock_path,
        )))
        return
    if args.mode == "revoke-readback":
        print(_canonical_json(revoke_readback_manifest(
            db_path=args.db_path,
            manifest_path=args.manifest,
            expected_manifest_sha256=args.expected_manifest_sha256,
            operation_id=args.operation_id,
            revocation_id=args.revocation_id,
        )))
        return
    raise BreakglassRunnerError(f"unsupported mode: {args.mode}")


def build_manifest(
    *,
    db_path: Path,
    operation_id: str,
    source_capture_id: str,
    economics_source_path: Path,
    expected_economics_source_sha256: str,
    expected_raw_plan_sha256: str,
    economics_patch_index: int,
    economics_bundle_version: str,
    economics_ready_as_of: str,
    economics_snapshot_id: str,
    economics_column_date: str,
    expected_capture_sha256: str,
    expected_capture_sequence: int,
    expected_capture_captured_at: str,
    public_date_columns: Sequence[str],
    expected_cell_count: int,
    created_at: str,
) -> dict[str, Any]:
    db_path = Path(db_path).resolve()
    economics_source_path = Path(economics_source_path).resolve()
    if not db_path.is_file() or not economics_source_path.is_file():
        raise BreakglassRunnerError("source database/sealed economics JSON is absent")
    with _connect_readonly(db_path) as conn:
        enabled_nm_ids = _enabled_nm_ids(conn)
        capture, inventory_cells = _inventory_cells(
            conn,
            capture_id=source_capture_id,
            enabled_nm_ids=enabled_nm_ids,
        )
        _validate_capture_binding(
            capture,
            expected_digest=expected_capture_sha256,
            expected_sequence=expected_capture_sequence,
            expected_captured_at=expected_capture_captured_at,
        )
        non_target_digest = _non_target_digest(conn)
        target_prestate = _target_prestate(conn)
    ready_source, dependent_cells, source_empty_identities = _sealed_dependent_cells(
        economics_source_path,
        expected_file_sha256=expected_economics_source_sha256,
        expected_raw_plan_sha256=expected_raw_plan_sha256,
        patch_index=economics_patch_index,
        bundle_version=economics_bundle_version,
        ready_as_of=economics_ready_as_of,
        snapshot_id=economics_snapshot_id,
        column_date=economics_column_date,
        enabled_nm_ids=enabled_nm_ids,
    )
    cells = sorted([*inventory_cells, *dependent_cells], key=lambda item: item["row_id"])
    if len(cells) != expected_cell_count:
        raise BreakglassRunnerError(
            f"breakglass cell count changed: expected={expected_cell_count}, actual={len(cells)}"
        )
    family_counts: dict[str, int] = {}
    for cell in cells:
        family = str((cell.get("provenance") or {}).get("family") or "")
        family_counts[family] = family_counts.get(family, 0) + 1
    eligible_presentation_count = _eligible_presentation_count(
        cells,
        date_columns=public_date_columns,
    )
    inventory_totals = _inventory_total_summary(capture, inventory_cells)
    if expected_cell_count == PRODUCTION_CELL_COUNT:
        if family_counts != PRODUCTION_FAMILY_COUNTS:
            raise BreakglassRunnerError(
                f"production family counts changed: {family_counts}"
            )
        if set(source_empty_identities) != PRODUCTION_SOURCE_EMPTY_IDENTITIES:
            raise BreakglassRunnerError(
                f"production source-empty economics identities changed: {source_empty_identities}"
            )
        if inventory_totals != PRODUCTION_INVENTORY_TOTALS:
            raise BreakglassRunnerError(
                f"production inventory totals changed: {inventory_totals}"
            )
        if eligible_presentation_count != 606:
            raise BreakglassRunnerError(
                "production eligible public presentation count changed"
            )
    target_digest = target_cells_digest(cells)
    return {
        "contract_name": CONTRACT_NAME,
        "mode": "breakglass_last_good",
        "operation_id": operation_id,
        "created_at": created_at,
        "db_path": str(db_path),
        "source": {
            "capture_id": source_capture_id,
            "capture_sequence": int(capture["capture_sequence"]),
            "capture_business_date": str(capture["business_date"]),
            "capture_source_digest": str(capture["source_digest"]),
            "capture_captured_at": str(capture["captured_at"]),
            "economics_source_path": str(economics_source_path),
            "economics_source_digest": str(ready_source["file_digest"]),
            "economics_patch_index": economics_patch_index,
            "economics_identity": list(ready_source["identity"]),
            "economics_identity_digest": str(ready_source["identity_digest"]),
            "ready_snapshot_id": str(ready_source["snapshot_id"]),
            "ready_plan_digest": str(ready_source["raw_plan_digest"]),
            "ready_source_date": economics_column_date,
            "date_column_digest": str(ready_source["date_column_digest"]),
            "source_empty_identities": source_empty_identities,
        },
        "scope": {
            "enabled_nm_ids": enabled_nm_ids,
            "enabled_nm_id_count": len(enabled_nm_ids),
            "cell_count": len(cells),
            "family_counts": family_counts,
            "inventory_totals": inventory_totals,
            "public_date_columns": list(public_date_columns),
            "eligible_presentation_count": eligible_presentation_count,
            "write_boundary": "new_breakglass_tables_only",
            "public_overlay_rule": "fill_blank_only_nonempty_wins",
            "quality": "last_good_provisional",
            "wb_writes": 0,
            "fbo_writes": 0,
            "warehouse_writes": 0,
            "history_writes": 0,
            "ready_snapshot_writes": 0,
            "source_writes": 0,
            "capital_writes": 0,
            "non_target_writes": 0,
        },
        "target_prestate": target_prestate,
        "target_prestate_digest": _fingerprint(target_prestate),
        "target_digest": target_digest,
        "non_target_digest": non_target_digest,
        "cells": cells,
    }


def apply_manifest(
    *,
    db_path: Path,
    manifest_path: Path,
    expected_manifest_sha256: str,
    operation_id: str,
    evidence_dir: Path,
    writer_lock_path: Path,
) -> dict[str, Any]:
    manifest_path = Path(manifest_path).resolve()
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise BreakglassRunnerError("manifest digest changed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("operation_id") or "") != operation_id:
        raise BreakglassRunnerError("operation identity changed")
    if str(manifest.get("db_path") or "") != str(Path(db_path).resolve()):
        raise BreakglassRunnerError("target database binding changed")
    for boundary in (
        "wb_writes",
        "fbo_writes",
        "warehouse_writes",
        "history_writes",
        "ready_snapshot_writes",
        "source_writes",
        "capital_writes",
        "non_target_writes",
    ):
        if (manifest.get("scope") or {}).get(boundary) != 0:
            raise BreakglassRunnerError(f"manifest {boundary} boundary changed")
    evidence_dir = Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    backup_path = evidence_dir / f"{operation_id}.before.json"
    receipt_path = evidence_dir / f"{operation_id}.receipt.json"
    if backup_path.exists() or receipt_path.exists():
        raise BreakglassRunnerError("operation artifacts already exist; blind retry forbidden")
    _revalidate_manifest_sources(manifest)
    lock_path = Path(writer_lock_path).resolve()
    if not lock_path.is_file():
        raise BreakglassRunnerError("canonical warehouse writer lock path is absent")
    with lock_path.open("r+b") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BreakglassRunnerError("warehouse writer lock is busy") from exc
        with _connect_readonly(Path(db_path)) as read_conn:
            if _non_target_digest(read_conn) != str(manifest["non_target_digest"]):
                raise BreakglassRunnerError("non-target/source CAS changed before apply")
            prestate = _target_prestate(read_conn)
        if _fingerprint(prestate) != str(manifest["target_prestate_digest"]):
            raise BreakglassRunnerError("breakglass target prestate changed before apply")
        backup = {
            "contract_name": CONTRACT_NAME,
            "operation_id": operation_id,
            "captured_at": _utc_now(),
            "target_prestate": prestate,
            "target_prestate_digest": _fingerprint(prestate),
            "rollback": {
                "kind": "append_only_revocation",
                "operation_id": operation_id,
                "restores_public_overlay_prestate": True,
                "preserves_append_only_audit": True,
            },
        }
        _write_immutable_json(backup_path, backup)
        applied_at = _utc_now()
        conn = sqlite3.connect(Path(db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            conn.set_authorizer(_breakglass_only_authorizer)
            if _non_target_digest(conn) != str(manifest["non_target_digest"]):
                raise BreakglassRunnerError("non-target/source CAS changed inside transaction")
            persist_breakglass_last_good(
                conn,
                operation={
                    "operation_id": operation_id,
                    "manifest_sha256": expected_manifest_sha256,
                    "source_capture_id": str(manifest["source"]["capture_id"]),
                    "source_capture_digest": str(manifest["source"]["capture_source_digest"]),
                    "source_checkpoint_operation_id": _fingerprint(
                        manifest["source"]["economics_identity"]
                    ),
                    "source_checkpoint_digest": str(manifest["source"]["economics_source_digest"]),
                    "source_ready_plan_digest": str(manifest["source"]["ready_plan_digest"]),
                    "target_digest": str(manifest["target_digest"]),
                    "non_target_digest": str(manifest["non_target_digest"]),
                    "applied_at": applied_at,
                    "metadata": {
                        "backup_path": str(backup_path),
                        "backup_digest": _file_sha256(backup_path),
                        "family_counts": dict(manifest["scope"]["family_counts"]),
                        "quality": "last_good_provisional",
                    },
                },
                cells=list(manifest["cells"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.set_authorizer(None)
            conn.close()
        readback = readback_manifest(
            db_path=Path(db_path),
            manifest_path=manifest_path,
            expected_manifest_sha256=expected_manifest_sha256,
            operation_id=operation_id,
        )
        receipt = {
            "contract_name": CONTRACT_NAME,
            "status": "applied",
            "operation_id": operation_id,
            "manifest_sha256": expected_manifest_sha256,
            "applied_at": applied_at,
            "production_mutation_submit_count": 1,
            "transaction_count": 1,
            "cell_insert_count": len(manifest["cells"]),
            "wb_write_count": 0,
            "fbo_write_count": 0,
            "warehouse_write_count": 0,
            "history_write_count": 0,
            "ready_snapshot_write_count": 0,
            "source_write_count": 0,
            "capital_write_count": 0,
            "non_target_write_count": 0,
            "sqlite_authorizer": "breakglass_target_tables_only",
            "backup_path": str(backup_path),
            "backup_digest": _file_sha256(backup_path),
            "readback": readback,
        }
        _write_immutable_json(receipt_path, receipt)
        return {**receipt, "receipt_path": str(receipt_path), "receipt_digest": _file_sha256(receipt_path)}


def revoke_manifest(
    *,
    db_path: Path,
    manifest_path: Path,
    expected_manifest_sha256: str,
    operation_id: str,
    revocation_id: str,
    reason: str,
    evidence_dir: Path,
    writer_lock_path: Path,
) -> dict[str, Any]:
    """Append one manifest-bound revocation after proving the applied state."""

    manifest_path = Path(manifest_path).resolve()
    if _file_sha256(manifest_path) != expected_manifest_sha256:
        raise BreakglassRunnerError("revocation manifest digest changed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("operation_id") or "") != operation_id:
        raise BreakglassRunnerError("revocation operation identity changed")
    if not revocation_id or not reason.strip():
        raise BreakglassRunnerError("revocation identity/reason is required")
    _revalidate_manifest_sources(manifest)
    applied_readback = readback_manifest(
        db_path=Path(db_path),
        manifest_path=manifest_path,
        expected_manifest_sha256=expected_manifest_sha256,
        operation_id=operation_id,
    )
    applied_readback_digest = _fingerprint(applied_readback)
    evidence_dir = Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    apply_backup_path = evidence_dir / f"{operation_id}.before.json"
    if not apply_backup_path.is_file():
        raise BreakglassRunnerError("apply before-image artifact is absent")
    backup = json.loads(apply_backup_path.read_text(encoding="utf-8"))
    if (
        str(backup.get("operation_id") or "") != operation_id
        or str(backup.get("target_prestate_digest") or "")
        != str(manifest["target_prestate_digest"])
    ):
        raise BreakglassRunnerError("apply before-image binding changed")
    backup_digest = _file_sha256(apply_backup_path)
    revocation_plan_path = evidence_dir / f"{revocation_id}.plan.json"
    receipt_path = evidence_dir / f"{revocation_id}.receipt.json"
    if revocation_plan_path.exists() or receipt_path.exists():
        raise BreakglassRunnerError("revocation artifacts already exist; blind retry forbidden")
    with _connect_readonly(Path(db_path)) as before_conn:
        before_non_target_digest = _non_target_digest(before_conn)
    if before_non_target_digest != str(manifest["non_target_digest"]):
        raise BreakglassRunnerError("revocation non-target CAS changed")
    revoked_at = _utc_now()
    revocation_plan = {
        "contract_name": CONTRACT_NAME,
        "mode": "append_only_revocation",
        "operation_id": operation_id,
        "revocation_id": revocation_id,
        "manifest_sha256": expected_manifest_sha256,
        "target_prestate_digest": str(manifest["target_prestate_digest"]),
        "non_target_digest": before_non_target_digest,
        "backup_path": str(apply_backup_path),
        "backup_digest": backup_digest,
        "applied_readback": applied_readback,
        "applied_readback_digest": applied_readback_digest,
        "reason": reason.strip(),
        "created_at": revoked_at,
    }
    _write_immutable_json(revocation_plan_path, revocation_plan)
    lock_path = Path(writer_lock_path).resolve()
    if not lock_path.is_file():
        raise BreakglassRunnerError("canonical warehouse writer lock path is absent")
    with lock_path.open("r+b") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BreakglassRunnerError("warehouse writer lock is busy") from exc
        conn = sqlite3.connect(Path(db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            conn.set_authorizer(_breakglass_only_authorizer)
            if _non_target_digest(conn) != before_non_target_digest:
                raise BreakglassRunnerError(
                    "revocation non-target CAS changed inside transaction"
                )
            revoke_breakglass_last_good(
                conn,
                operation_id=operation_id,
                revocation_id=revocation_id,
                reason=reason.strip(),
                revoked_at=revoked_at,
                manifest_sha256=expected_manifest_sha256,
                target_prestate_digest=str(manifest["target_prestate_digest"]),
                non_target_digest=before_non_target_digest,
                backup_digest=backup_digest,
                applied_readback_digest=applied_readback_digest,
                metadata={
                    "revocation_plan_path": str(revocation_plan_path),
                    "revocation_plan_digest": _file_sha256(revocation_plan_path),
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.set_authorizer(None)
            conn.close()
    readback = revoke_readback_manifest(
        db_path=Path(db_path),
        manifest_path=manifest_path,
        expected_manifest_sha256=expected_manifest_sha256,
        operation_id=operation_id,
        revocation_id=revocation_id,
    )
    receipt = {
        "contract_name": CONTRACT_NAME,
        "status": "revoked",
        "operation_id": operation_id,
        "revocation_id": revocation_id,
        "manifest_sha256": expected_manifest_sha256,
        "production_mutation_submit_count": 1,
        "transaction_count": 1,
        "source_write_count": 0,
        "non_target_write_count": 0,
        "sqlite_authorizer": "breakglass_target_tables_only",
        "backup_digest": backup_digest,
        "revocation_plan_path": str(revocation_plan_path),
        "revocation_plan_digest": _file_sha256(revocation_plan_path),
        "readback": readback,
    }
    _write_immutable_json(receipt_path, receipt)
    return {
        **receipt,
        "receipt_path": str(receipt_path),
        "receipt_digest": _file_sha256(receipt_path),
    }


def revoke_readback_manifest(
    *,
    db_path: Path,
    manifest_path: Path,
    expected_manifest_sha256: str,
    operation_id: str,
    revocation_id: str,
) -> dict[str, Any]:
    if _file_sha256(Path(manifest_path)) != expected_manifest_sha256:
        raise BreakglassRunnerError("revocation readback manifest digest changed")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if str(manifest.get("operation_id") or "") != operation_id:
        raise BreakglassRunnerError("revocation readback operation identity changed")
    revocation = read_breakglass_last_good_revocation(
        Path(db_path), operation_id=operation_id
    )
    if revocation is None or revocation["revocation_id"] != revocation_id:
        raise BreakglassRunnerError("exact breakglass revocation is absent")
    if revocation["manifest_sha256"] != expected_manifest_sha256:
        raise BreakglassRunnerError("revoked manifest binding changed")
    if revocation["target_prestate_digest"] != str(
        manifest["target_prestate_digest"]
    ):
        raise BreakglassRunnerError("revoked prestate binding changed")
    if read_active_breakglass_last_good(Path(db_path)) is not None:
        raise BreakglassRunnerError("revoked operation remains active")
    with _connect_readonly(Path(db_path)) as conn:
        non_target_digest = _non_target_digest(conn)
        operation = conn.execute(
            f"""SELECT manifest_sha256,cell_count,target_digest,metadata_json
                FROM {OPERATIONS_TABLE} WHERE operation_id=?""",
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise BreakglassRunnerError("revoked operation audit disappeared")
        stored_cells = conn.execute(
            f"""SELECT row_id,target_date_from,value_json FROM {CELLS_TABLE}
                WHERE operation_id=? ORDER BY row_id""",
            (operation_id,),
        ).fetchall()
    if non_target_digest != str(manifest["non_target_digest"]):
        raise BreakglassRunnerError("non-target digest changed after revocation")
    stored_target_digest = _fingerprint(
        [
            [str(item["row_id"]), str(item["target_date_from"]), json.loads(str(item["value_json"]))]
            for item in stored_cells
        ]
    )
    if (
        str(operation["manifest_sha256"]) != expected_manifest_sha256
        or int(operation["cell_count"]) != len(manifest["cells"])
        or len(stored_cells) != len(manifest["cells"])
        or str(operation["target_digest"]) != str(manifest["target_digest"])
        or stored_target_digest != str(manifest["target_digest"])
    ):
        raise BreakglassRunnerError("revoked operation/cell audit changed")
    operation_metadata = json.loads(str(operation["metadata_json"]))
    backup_path = Path(str(operation_metadata.get("backup_path") or ""))
    if (
        not backup_path.is_file()
        or _file_sha256(backup_path) != revocation["backup_digest"]
        or str(operation_metadata.get("backup_digest") or "")
        != revocation["backup_digest"]
    ):
        raise BreakglassRunnerError("revoked before-image artifact changed")
    return {
        "status": "verified_revoked",
        "operation_id": operation_id,
        "revocation_id": revocation_id,
        "manifest_sha256": expected_manifest_sha256,
        "target_prestate_digest": revocation["target_prestate_digest"],
        "non_target_digest": non_target_digest,
        "active": False,
        "audit_preserved": True,
        "cell_count": len(stored_cells),
        "target_digest": stored_target_digest,
        "backup_digest": revocation["backup_digest"],
    }


def readback_manifest(
    *,
    db_path: Path,
    manifest_path: Path,
    expected_manifest_sha256: str,
    operation_id: str,
) -> dict[str, Any]:
    if _file_sha256(Path(manifest_path)) != expected_manifest_sha256:
        raise BreakglassRunnerError("readback manifest digest changed")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if str(manifest.get("operation_id") or "") != operation_id:
        raise BreakglassRunnerError("readback operation identity changed")
    payload = read_active_breakglass_last_good(Path(db_path))
    if payload is None or str(payload["operation"]["operation_id"]) != operation_id:
        raise BreakglassRunnerError("applied breakglass operation is not active")
    if str(payload["operation"]["manifest_sha256"]) != expected_manifest_sha256:
        raise BreakglassRunnerError("applied manifest binding changed")
    if len(payload["cells"]) != len(manifest["cells"]):
        raise BreakglassRunnerError("applied cell count changed")
    with _connect_readonly(Path(db_path)) as conn:
        non_target_digest = _non_target_digest(conn)
    if non_target_digest != str(manifest["non_target_digest"]):
        raise BreakglassRunnerError("non-target digest changed after apply")
    return {
        "status": "verified",
        "operation_id": operation_id,
        "cell_count": len(payload["cells"]),
        "target_digest": str(payload["operation"]["target_digest"]),
        "non_target_digest": non_target_digest,
        "active": True,
    }


def _inventory_cells(
    conn: sqlite3.Connection,
    *,
    capture_id: str,
    enabled_nm_ids: Sequence[int],
) -> tuple[sqlite3.Row, list[dict[str, Any]]]:
    capture = conn.execute(
        f"SELECT * FROM {CAPTURES_TABLE} WHERE capture_id=?",
        (capture_id,),
    ).fetchone()
    if capture is None or str(capture["capture_kind"]) != "accepted_refresh":
        raise BreakglassRunnerError("published source capture is absent")
    business_date = str(capture["business_date"])
    scopes = [("TOTAL", "TOTAL", None), *[("SKU", f"SKU:{nm_id}", nm_id) for nm_id in enabled_nm_ids]]
    cells: list[dict[str, Any]] = []
    for scope_kind, scope_key, nm_id in scopes:
        components = conn.execute(
            f"""SELECT * FROM {COMPONENTS_TABLE}
                WHERE capture_id=? AND scope_kind=? AND scope_key=?
                ORDER BY component_kind,component_id""",
            (capture_id, scope_kind, scope_key),
        ).fetchall()
        wb = [item for item in components if str(item["component_kind"]) == "WB"]
        fbs = [item for item in components if str(item["component_kind"]) == "FBS_FACILITY"]
        if len(wb) != 1 or len(fbs) != 2:
            raise BreakglassRunnerError(f"published inventory scope is incomplete: {scope_key}")
        if any(str(item["state"]) not in {"exact", "exact_zero"} for item in [*wb, *fbs]):
            raise BreakglassRunnerError(f"published inventory scope is not exact: {scope_key}")
        prefix = "total_" if scope_kind == "TOTAL" else ""
        for item in fbs:
            facility_id = str(item["component_id"])
            cells.append(
                {
                    "row_id": f"{scope_key}|{prefix}inventory_fbs_facility_available_qty_v1:{facility_id}",
                    "target_date_from": business_date,
                    "value": int(item["quantity"]),
                    "source_business_date": business_date,
                    "source_kind": SOURCE_CAPTURE_KIND,
                    "source_identity": capture_id,
                    "source_digest": str(capture["source_digest"]),
                    "provenance": {
                        "family": "inventory_fbs_facility",
                        "component_id": facility_id,
                        "component_label": str(item["component_label"]),
                        "component_source_digest": str(item["source_digest"]),
                        "component_source_revision": str(item["source_revision"]),
                        "component_source_watermark": str(item["source_watermark"]),
                        "capture_sequence": int(capture["capture_sequence"]),
                    },
                }
            )
        combined = int(wb[0]["quantity"]) + sum(int(item["quantity"]) for item in fbs)
        cells.append(
            {
                "row_id": f"{scope_key}|{prefix}stock_total",
                "target_date_from": business_date,
                "value": combined,
                "source_business_date": business_date,
                "source_kind": SOURCE_CAPTURE_KIND,
                "source_identity": capture_id,
                "source_digest": str(capture["source_digest"]),
                "provenance": {
                    "family": "inventory_combined_total",
                    "formula": "WB + SUM(active FBS facilities)",
                    "wb_quantity": int(wb[0]["quantity"]),
                    "fbs_quantity": sum(int(item["quantity"]) for item in fbs),
                    "capture_sequence": int(capture["capture_sequence"]),
                },
            }
        )
    return capture, cells


def _sealed_dependent_cells(
    source_path: Path,
    *,
    expected_file_sha256: str,
    expected_raw_plan_sha256: str,
    patch_index: int,
    bundle_version: str,
    ready_as_of: str,
    snapshot_id: str,
    column_date: str,
    enabled_nm_ids: Sequence[int],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Read the exact retained economics JSON directly, never through SQLite."""

    source_path = Path(source_path).resolve()
    file_digest = _file_sha256(source_path)
    if file_digest != expected_file_sha256:
        raise BreakglassRunnerError("sealed economics JSON digest changed")
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    patches = ((payload.get("functional_economics") or {}).get("patches") or [])
    if not isinstance(patches, list) or patch_index < 0 or patch_index >= len(patches):
        raise BreakglassRunnerError("sealed economics patch is absent")
    patch = patches[patch_index]
    if not isinstance(patch, Mapping):
        raise BreakglassRunnerError("sealed economics patch is invalid")
    identity = [str(item) for item in list(patch.get("identity") or [])]
    expected_identity = [bundle_version, ready_as_of, snapshot_id]
    if identity != expected_identity:
        raise BreakglassRunnerError("sealed economics identity changed")
    raw_plan = patch.get("before_plan_json")
    if not isinstance(raw_plan, str):
        raise BreakglassRunnerError("sealed economics raw before-plan is absent")
    raw_plan_digest = "sha256:" + hashlib.sha256(raw_plan.encode("utf-8")).hexdigest()
    if raw_plan_digest != expected_raw_plan_sha256:
        raise BreakglassRunnerError("sealed economics raw before-plan digest changed")
    cells, source_empty, column_index = _dependent_cells_from_raw_plan(
        raw_plan,
        column_date=column_date,
        enabled_nm_ids=enabled_nm_ids,
        source_identity=snapshot_id,
        source_digest=raw_plan_digest,
    )
    identity_digest = _fingerprint(identity)
    date_column_digest = _fingerprint(
        {
            "identity_digest": identity_digest,
            "column_date": column_date,
            "column_index": column_index,
        }
    )
    return (
        {
            "file_digest": file_digest,
            "identity": identity,
            "identity_digest": identity_digest,
            "snapshot_id": snapshot_id,
            "raw_plan_digest": raw_plan_digest,
            "date_column_digest": date_column_digest,
        },
        cells,
        source_empty,
    )


def _dependent_cells_from_raw_plan(
    raw_plan: str,
    *,
    column_date: str,
    enabled_nm_ids: Sequence[int],
    source_identity: str,
    source_digest: str,
) -> tuple[list[dict[str, Any]], list[str], int]:
    plan = json.loads(raw_plan)
    dates = [str(item) for item in list(plan.get("date_columns") or [])]
    if column_date not in dates:
        raise BreakglassRunnerError("sealed economics source date is absent")
    column_index = dates.index(column_date) + 2
    sheet = next((item for item in list(plan.get("sheets") or []) if item.get("sheet_name") == "DATA_VITRINA"), None)
    if not isinstance(sheet, Mapping):
        raise BreakglassRunnerError("sealed economics DATA_VITRINA is absent")
    allowed_scope_keys = {"TOTAL", *{f"SKU:{nm_id}" for nm_id in enabled_nm_ids}}
    cells: list[dict[str, Any]] = []
    source_empty: list[str] = []
    seen: set[str] = set()
    for raw in list(sheet.get("rows") or []):
        if not isinstance(raw, list) or len(raw) <= column_index:
            continue
        row_id = str(raw[1] or "")
        scope_key, _, metric_key = row_id.partition("|")
        if scope_key not in allowed_scope_keys or metric_key not in WAC_KEYS | ECONOMICS_KEYS:
            continue
        if row_id in seen:
            raise BreakglassRunnerError("sealed economics row identity is duplicated")
        seen.add(row_id)
        value = raw[column_index]
        if value in {None, ""}:
            source_empty.append(row_id)
            continue
        family = "functional_wac" if metric_key in WAC_KEYS else "functional_economics"
        cells.append(
            {
                "row_id": row_id,
                "target_date_from": column_date,
                "value": value,
                "source_business_date": column_date,
                "source_kind": SOURCE_SEALED_ECONOMICS_KIND,
                "source_identity": source_identity,
                "source_digest": source_digest,
                "provenance": {
                    "family": family,
                    "source_contract": "functional_economics.patches[].before_plan_json",
                    "metric_key": metric_key,
                },
            }
        )
    if not cells or not any(item["row_id"] == "TOTAL|total_our_wb_unit_cost_rub" for item in cells):
        raise BreakglassRunnerError("sealed economics dependent publication is incomplete")
    return cells, sorted(source_empty), column_index


def _revalidate_manifest_sources(manifest: Mapping[str, Any]) -> None:
    source = manifest.get("source") or {}
    source_path = Path(str(source.get("economics_source_path") or "")).resolve()
    if not source_path.is_file():
        raise BreakglassRunnerError("sealed economics source disappeared")
    if _file_sha256(source_path) != str(source.get("economics_source_digest") or ""):
        raise BreakglassRunnerError("sealed economics source changed before mutation")
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    patches = ((payload.get("functional_economics") or {}).get("patches") or [])
    index = int(source.get("economics_patch_index"))
    if index < 0 or index >= len(patches) or not isinstance(patches[index], Mapping):
        raise BreakglassRunnerError("sealed economics patch disappeared")
    patch = patches[index]
    identity = [str(item) for item in list(patch.get("identity") or [])]
    if identity != list(source.get("economics_identity") or []):
        raise BreakglassRunnerError("sealed economics identity changed before mutation")
    if _fingerprint(identity) != str(source.get("economics_identity_digest") or ""):
        raise BreakglassRunnerError("sealed economics identity digest changed")
    raw_plan = patch.get("before_plan_json")
    if not isinstance(raw_plan, str):
        raise BreakglassRunnerError("sealed economics raw before-plan disappeared")
    raw_digest = "sha256:" + hashlib.sha256(raw_plan.encode("utf-8")).hexdigest()
    if raw_digest != str(source.get("ready_plan_digest") or ""):
        raise BreakglassRunnerError("sealed economics raw before-plan changed")
    plan = json.loads(raw_plan)
    dates = [str(item) for item in list(plan.get("date_columns") or [])]
    source_date = str(source.get("ready_source_date") or "")
    if source_date not in dates:
        raise BreakglassRunnerError("sealed economics date column disappeared")
    observed_column_digest = _fingerprint(
        {
            "identity_digest": str(source["economics_identity_digest"]),
            "column_date": source_date,
            "column_index": dates.index(source_date) + 2,
        }
    )
    if observed_column_digest != str(source.get("date_column_digest") or ""):
        raise BreakglassRunnerError("sealed economics date-column binding changed")


def _validate_capture_binding(
    capture: sqlite3.Row,
    *,
    expected_digest: str,
    expected_sequence: int,
    expected_captured_at: str,
) -> None:
    observed = {
        "digest": str(capture["source_digest"]),
        "sequence": int(capture["capture_sequence"]),
        "captured_at": str(capture["captured_at"]),
    }
    expected = {
        "digest": expected_digest,
        "sequence": int(expected_sequence),
        "captured_at": expected_captured_at,
    }
    if observed != expected:
        raise BreakglassRunnerError(
            f"published inventory capture binding changed: {observed}"
        )


def _inventory_total_summary(
    capture: sqlite3.Row,
    cells: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    del capture
    total_facilities = [
        item
        for item in cells
        if str(item["row_id"]).startswith("TOTAL|")
        and (item.get("provenance") or {}).get("family") == "inventory_fbs_facility"
    ]
    facilities: dict[str, int] = {}
    for item in total_facilities:
        label = str((item.get("provenance") or {}).get("component_label") or "")
        lowered = label.casefold()
        if "орен" in lowered or "oren" in lowered:
            key = "Orenburg"
        elif "моск" in lowered or "mosc" in lowered or "mosk" in lowered:
            key = "Moscow"
        else:
            key = label or str((item.get("provenance") or {}).get("component_id") or "")
        facilities[key] = int(item["value"])
    combined = next(
        (
            item
            for item in cells
            if item["row_id"] == "TOTAL|total_stock_total"
        ),
        None,
    )
    if combined is None:
        raise BreakglassRunnerError("published inventory total row is absent")
    provenance = combined.get("provenance") or {}
    return {
        **facilities,
        "FBS": int(provenance["fbs_quantity"]),
        "WB": int(provenance["wb_quantity"]),
        "combined": int(combined["value"]),
    }


def _eligible_presentation_count(
    cells: Sequence[Mapping[str, Any]],
    *,
    date_columns: Sequence[str],
) -> int:
    parsed_dates = [date.fromisoformat(str(item)) for item in date_columns]
    return sum(
        1
        for cell in cells
        for public_date in parsed_dates
        if public_date >= date.fromisoformat(str(cell["target_date_from"]))
    )


def _enabled_nm_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        f"""SELECT config.nm_id FROM {CURRENT_CONFIG_TABLE} current_state
            JOIN {CONFIG_TABLE} config ON config.bundle_version=current_state.bundle_version
            WHERE current_state.slot=1 AND config.enabled=1
            ORDER BY config.display_order,config.nm_id"""
    ).fetchall()
    result = [int(item[0]) for item in rows]
    if not result:
        raise BreakglassRunnerError("enabled public SKU scope is empty")
    return result


def _target_prestate(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    if {OPERATIONS_TABLE, CELLS_TABLE, REVOCATIONS_TABLE} - tables:
        return {
            "schema_present": False,
            "operations": [],
            "cells": [],
            "revocations": [],
            "revocation_audit": [],
        }
    return {
        "schema_present": True,
        "operations": _table_rows(conn, OPERATIONS_TABLE),
        "cells": _table_rows(conn, CELLS_TABLE),
        "revocations": _table_rows(conn, REVOCATIONS_TABLE),
        "revocation_audit": (
            _table_rows(conn, REVOCATION_AUDIT_TABLE)
            if REVOCATION_AUDIT_TABLE in tables
            else []
        ),
    }


def _non_target_digest(conn: sqlite3.Connection) -> str:
    tables = _tables(conn)
    selected = sorted(
        table
        for table in tables
        if table in NON_TARGET_TABLES
        or table.startswith(NON_TARGET_TABLE_PREFIXES)
    )
    payload = {
        table: _table_rows(conn, table)
        for table in selected
    }
    return _fingerprint(payload)


def _breakglass_only_authorizer(
    action: int,
    arg1: str | None,
    _arg2: str | None,
    database: str | None,
    _trigger: str | None,
) -> int:
    if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
        allowed = {
            OPERATIONS_TABLE,
            CELLS_TABLE,
            REVOCATIONS_TABLE,
            REVOCATION_AUDIT_TABLE,
            "sqlite_master",
        }
        if str(database or "") == "main" and str(arg1 or "") not in allowed:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _table_rows(conn: sqlite3.Connection, table: str) -> list[list[Any]]:
    columns = [str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})")]
    if not columns:
        return []
    order = ",".join(f'"{item}"' for item in columns)
    return [
        [_canonicalize_sqlite_scalar(value) for value in item]
        for item in conn.execute(f'SELECT {order} FROM "{table}" ORDER BY {order}')
    ]


def _canonicalize_sqlite_scalar(value: Any) -> Any:
    """Return the v1 collision-free JSON representation of one SQLite scalar."""

    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return {
            "__sqlite_value_type__": "blob",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BreakglassRunnerError(
                f"unsupported SQLite scalar for {SQLITE_CANONICAL_VALUE_CONTRACT}: non-finite float"
            )
        return value
    raise BreakglassRunnerError(
        f"unsupported SQLite scalar for {SQLITE_CANONICAL_VALUE_CONTRACT}: "
        f"{type(value).__name__}"
    )


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(item[0]) for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _connect_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?{query}", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (_canonical_json(payload) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", choices=("plan", "apply", "readback", "revoke", "revoke-readback")
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--source-capture-id", default="")
    parser.add_argument("--expected-capture-sha256", default="")
    parser.add_argument("--expected-capture-sequence", type=int, default=0)
    parser.add_argument("--expected-capture-captured-at", default="")
    parser.add_argument("--economics-source-path", type=Path)
    parser.add_argument("--expected-economics-source-sha256", default="")
    parser.add_argument("--expected-raw-plan-sha256", default="")
    parser.add_argument("--economics-patch-index", type=int, default=2)
    parser.add_argument("--economics-bundle-version", default="")
    parser.add_argument("--economics-ready-as-of", default="")
    parser.add_argument("--economics-snapshot-id", default="")
    parser.add_argument("--economics-column-date", default="")
    parser.add_argument("--public-date-column", action="append", default=[])
    parser.add_argument("--expected-cell-count", type=int, default=303)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expected-manifest-sha256", default="")
    parser.add_argument("--revocation-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument(
        "--writer-lock-path",
        type=Path,
        default=Path("/opt/wb-core-runtime/state/.warehouse-functional-sync.lock"),
    )
    args = parser.parse_args()
    if args.mode == "plan" and not all(
        (
            args.source_capture_id,
            args.expected_capture_sha256,
            args.expected_capture_sequence,
            args.expected_capture_captured_at,
            args.economics_source_path,
            args.expected_economics_source_sha256,
            args.expected_raw_plan_sha256,
            args.economics_bundle_version,
            args.economics_ready_as_of,
            args.economics_snapshot_id,
            args.economics_column_date,
            args.public_date_column,
        )
    ):
        parser.error("plan source arguments are required")
    if args.mode in {"apply", "readback", "revoke", "revoke-readback"} and not all(
        (args.manifest, args.expected_manifest_sha256)
    ):
        parser.error("manifest arguments are required")
    if args.mode in {"revoke", "revoke-readback"} and not args.revocation_id:
        parser.error("revocation identity is required")
    if args.mode == "revoke" and not args.reason.strip():
        parser.error("revocation reason is required")
    return args


if __name__ == "__main__":
    try:
        main()
    except (BreakglassRunnerError, BreakglassLastGoodError, OSError, sqlite3.Error, ValueError) as exc:
        print(_canonical_json({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
