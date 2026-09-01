#!/usr/bin/env python3
"""One-submit break-glass publication of verified public last-good cells."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
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
    REVOCATIONS_TABLE,
    BreakglassLastGoodError,
    persist_breakglass_last_good,
    read_active_breakglass_last_good,
    target_cells_digest,
)


CAPTURES_TABLE = "sheet_vitrina_v1_inventory_history_captures"
COMPONENTS_TABLE = "sheet_vitrina_v1_inventory_history_components"
READY_TABLE = "sheet_vitrina_v1_ready_snapshots"
CURRENT_CONFIG_TABLE = "registry_upload_current_state"
CONFIG_TABLE = "registry_upload_config_v2"
SOURCE_CAPTURE_KIND = "inventory_history_published_capture"
SOURCE_CHECKPOINT_KIND = "verified_ready_snapshot_checkpoint"
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


class BreakglassRunnerError(RuntimeError):
    pass


def main() -> None:
    args = _parse_args()
    if args.mode == "plan":
        payload = build_manifest(
            db_path=args.db_path,
            operation_id=args.operation_id,
            source_capture_id=args.source_capture_id,
            checkpoint_path=args.checkpoint_path,
            expected_checkpoint_sha256=args.expected_checkpoint_sha256,
            checkpoint_operation_id=args.checkpoint_operation_id,
            checkpoint_ready_as_of=args.checkpoint_ready_as_of,
            checkpoint_column_date=args.checkpoint_column_date,
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
    raise BreakglassRunnerError(f"unsupported mode: {args.mode}")


def build_manifest(
    *,
    db_path: Path,
    operation_id: str,
    source_capture_id: str,
    checkpoint_path: Path,
    expected_checkpoint_sha256: str,
    checkpoint_operation_id: str,
    checkpoint_ready_as_of: str,
    checkpoint_column_date: str,
    expected_cell_count: int,
    created_at: str,
) -> dict[str, Any]:
    db_path = Path(db_path).resolve()
    checkpoint_path = Path(checkpoint_path).resolve()
    if not db_path.is_file() or not checkpoint_path.is_file():
        raise BreakglassRunnerError("source database/checkpoint is absent")
    checkpoint_sha256 = _file_sha256(checkpoint_path)
    if checkpoint_sha256 != expected_checkpoint_sha256:
        raise BreakglassRunnerError("checkpoint digest changed")
    with _connect_readonly(db_path) as conn:
        enabled_nm_ids = _enabled_nm_ids(conn)
        capture, inventory_cells = _inventory_cells(
            conn,
            capture_id=source_capture_id,
            enabled_nm_ids=enabled_nm_ids,
        )
        non_target_digest = _non_target_digest(conn)
        target_prestate = _target_prestate(conn)
    with _connect_readonly(checkpoint_path, immutable=True) as checkpoint:
        ready_source, dependent_cells = _dependent_cells(
            checkpoint,
            ready_as_of=checkpoint_ready_as_of,
            column_date=checkpoint_column_date,
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
            "checkpoint_operation_id": checkpoint_operation_id,
            "checkpoint_path": str(checkpoint_path),
            "checkpoint_digest": checkpoint_sha256,
            "ready_snapshot_id": str(ready_source["snapshot_id"]),
            "ready_refreshed_at": str(ready_source["refreshed_at"]),
            "ready_plan_digest": str(ready_source["plan_digest"]),
            "ready_source_date": checkpoint_column_date,
        },
        "scope": {
            "enabled_nm_ids": enabled_nm_ids,
            "enabled_nm_id_count": len(enabled_nm_ids),
            "cell_count": len(cells),
            "family_counts": family_counts,
            "write_boundary": "new_breakglass_tables_only",
            "public_overlay_rule": "fill_blank_only_nonempty_wins",
            "quality": "last_good_provisional",
            "wb_writes": 0,
            "warehouse_writes": 0,
            "ready_snapshot_writes": 0,
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
    if (manifest.get("scope") or {}).get("wb_writes") != 0:
        raise BreakglassRunnerError("manifest WB boundary changed")
    evidence_dir = Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    backup_path = evidence_dir / f"{operation_id}.before.json"
    receipt_path = evidence_dir / f"{operation_id}.receipt.json"
    if backup_path.exists() or receipt_path.exists():
        raise BreakglassRunnerError("operation artifacts already exist; blind retry forbidden")
    checkpoint_path = Path(str(manifest["source"]["checkpoint_path"])).resolve()
    if _file_sha256(checkpoint_path) != str(manifest["source"]["checkpoint_digest"]):
        raise BreakglassRunnerError("source checkpoint changed before apply")
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
                "restores_target_prestate": True,
            },
        }
        _write_immutable_json(backup_path, backup)
        applied_at = _utc_now()
        conn = sqlite3.connect(Path(db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            if _non_target_digest(conn) != str(manifest["non_target_digest"]):
                raise BreakglassRunnerError("non-target/source CAS changed inside transaction")
            persist_breakglass_last_good(
                conn,
                operation={
                    "operation_id": operation_id,
                    "manifest_sha256": expected_manifest_sha256,
                    "source_capture_id": str(manifest["source"]["capture_id"]),
                    "source_capture_digest": str(manifest["source"]["capture_source_digest"]),
                    "source_checkpoint_operation_id": str(manifest["source"]["checkpoint_operation_id"]),
                    "source_checkpoint_digest": str(manifest["source"]["checkpoint_digest"]),
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
            "warehouse_write_count": 0,
            "ready_snapshot_write_count": 0,
            "backup_path": str(backup_path),
            "backup_digest": _file_sha256(backup_path),
            "readback": readback,
        }
        _write_immutable_json(receipt_path, receipt)
        return {**receipt, "receipt_path": str(receipt_path), "receipt_digest": _file_sha256(receipt_path)}


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


def _dependent_cells(
    conn: sqlite3.Connection,
    *,
    ready_as_of: str,
    column_date: str,
    enabled_nm_ids: Sequence[int],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = conn.execute(
        f"SELECT snapshot_id,refreshed_at,plan_json FROM {READY_TABLE} WHERE as_of_date=?",
        (ready_as_of,),
    ).fetchone()
    if source is None:
        raise BreakglassRunnerError("checkpoint ready snapshot is absent")
    raw_plan = str(source["plan_json"])
    plan_digest = "sha256:" + hashlib.sha256(raw_plan.encode("utf-8")).hexdigest()
    plan = json.loads(raw_plan)
    dates = [str(item) for item in list(plan.get("date_columns") or [])]
    if column_date not in dates:
        raise BreakglassRunnerError("checkpoint source date is absent")
    column_index = dates.index(column_date) + 2
    sheet = next((item for item in list(plan.get("sheets") or []) if item.get("sheet_name") == "DATA_VITRINA"), None)
    if not isinstance(sheet, Mapping):
        raise BreakglassRunnerError("checkpoint DATA_VITRINA is absent")
    allowed_scope_keys = {"TOTAL", *{f"SKU:{nm_id}" for nm_id in enabled_nm_ids}}
    cells: list[dict[str, Any]] = []
    for raw in list(sheet.get("rows") or []):
        if not isinstance(raw, list) or len(raw) <= column_index:
            continue
        row_id = str(raw[1] or "")
        scope_key, _, metric_key = row_id.partition("|")
        if scope_key not in allowed_scope_keys or metric_key not in WAC_KEYS | ECONOMICS_KEYS:
            continue
        value = raw[column_index]
        if value in {None, ""}:
            continue
        family = "functional_wac" if metric_key in WAC_KEYS else "functional_economics"
        cells.append(
            {
                "row_id": row_id,
                "target_date_from": column_date,
                "value": value,
                "source_business_date": column_date,
                "source_kind": SOURCE_CHECKPOINT_KIND,
                "source_identity": str(source["snapshot_id"]),
                "source_digest": plan_digest,
                "provenance": {
                    "family": family,
                    "ready_as_of_date": ready_as_of,
                    "ready_refreshed_at": str(source["refreshed_at"]),
                    "metric_key": metric_key,
                },
            }
        )
    if not cells or not any(item["row_id"] == "TOTAL|total_our_wb_unit_cost_rub" for item in cells):
        raise BreakglassRunnerError("checkpoint dependent publication is incomplete")
    return {
        "snapshot_id": str(source["snapshot_id"]),
        "refreshed_at": str(source["refreshed_at"]),
        "plan_digest": plan_digest,
    }, cells


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
        return {"schema_present": False, "operations": [], "cells": [], "revocations": []}
    return {
        "schema_present": True,
        "operations": _table_rows(conn, OPERATIONS_TABLE),
        "cells": _table_rows(conn, CELLS_TABLE),
        "revocations": _table_rows(conn, REVOCATIONS_TABLE),
    }


def _non_target_digest(conn: sqlite3.Connection) -> str:
    tables = _tables(conn)
    payload = {
        table: _table_rows(conn, table)
        for table in NON_TARGET_TABLES
        if table in tables
    }
    return _fingerprint(payload)


def _table_rows(conn: sqlite3.Connection, table: str) -> list[list[Any]]:
    columns = [str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})")]
    if not columns:
        return []
    order = ",".join(f'"{item}"' for item in columns)
    return [list(item) for item in conn.execute(f'SELECT {order} FROM "{table}" ORDER BY {order}')]


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
    parser.add_argument("mode", choices=("plan", "apply", "readback"))
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--source-capture-id", default="")
    parser.add_argument("--checkpoint-path", type=Path)
    parser.add_argument("--expected-checkpoint-sha256", default="")
    parser.add_argument("--checkpoint-operation-id", default="")
    parser.add_argument("--checkpoint-ready-as-of", default="")
    parser.add_argument("--checkpoint-column-date", default="")
    parser.add_argument("--expected-cell-count", type=int, default=303)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expected-manifest-sha256", default="")
    parser.add_argument(
        "--writer-lock-path",
        type=Path,
        default=Path("/opt/wb-core-runtime/state/.warehouse-functional-sync.lock"),
    )
    args = parser.parse_args()
    if args.mode == "plan" and not all((args.source_capture_id, args.checkpoint_path, args.expected_checkpoint_sha256, args.checkpoint_operation_id, args.checkpoint_ready_as_of, args.checkpoint_column_date)):
        parser.error("plan source arguments are required")
    if args.mode in {"apply", "readback"} and not all((args.manifest, args.expected_manifest_sha256)):
        parser.error("manifest arguments are required")
    return args


if __name__ == "__main__":
    try:
        main()
    except (BreakglassRunnerError, BreakglassLastGoodError, OSError, sqlite3.Error, ValueError) as exc:
        print(_canonical_json({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
