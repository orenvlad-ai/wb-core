#!/usr/bin/env python3
"""Bounded recovery for CNY supplier payment №10 (26GN582 → 26GN583)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.cny_ledger import CnyLedgerBlock  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


DOCUMENT_ID = "cnydoc_40e1467235af48549f98e9f1ba93ac9f"
FILE_SHA256 = "f8fae608914935c70f66eeddc73849ca520f307174160a4a265b5829f5374880"
DOCUMENT_NUMBER = "10"
AMOUNT_CNY = "76646"
OLD_SHIPMENT_ID = "sup_eb8f1541b9594d168d689d5cff7e81d0"
OLD_INVOICE_NO = "26GN582"
TARGET_SHIPMENT_ID = "sup_35a64348998a47de895ea225a6aeed71"
TARGET_INVOICE_NO = "26GN583"
AUDIT_TABLE = "sheet_vitrina_v1_supplier_cny_payment_10_recovery_audit"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args(argv)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    plan = build_plan(runtime.db_path)
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if str(args.fingerprint or "") != str(plan["fingerprint"]):
        raise ValueError("apply requires the exact current dry-run fingerprint")
    backup_root = (
        Path(args.backup_dir)
        if args.backup_dir
        else runtime.runtime_dir / "backups" / "supplier-cny-payment-10-recovery"
    )
    result = apply_plan(runtime, plan, backup_root=backup_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def build_plan(db_path: Path) -> dict[str, Any]:
    with _connect(db_path, read_only=True) as conn:
        state = _target_state(conn)
        non_target_digest = _non_target_digest(conn)
    _validate_state(state)
    document = state["document"]
    operation = state["operations"][0] if state["operations"] else {}
    layer = state["capital_layers"][0] if state["capital_layers"] else {}
    target_complete = (
        str(document.get("status") or "") == "posted"
        and str(document.get("source_order_id") or "") == TARGET_SHIPMENT_ID
        and str(document.get("context_order_id") or "") == TARGET_SHIPMENT_ID
        and len(state["operations"]) == 1
        and str(operation.get("source_order_id") or "") == TARGET_SHIPMENT_ID
        and len(state["capital_layers"]) == 1
        and str(layer.get("shipment_id") or "") == TARGET_SHIPMENT_ID
    )
    changes = (
        []
        if target_complete
        else [
            {
                "action": (
                    "restore_and_relink"
                    if str(document.get("status") or "") == "excluded"
                    else "relink"
                ),
                "document_id": DOCUMENT_ID,
                "old_shipment_id": str(document.get("source_order_id") or ""),
                "target_shipment_id": TARGET_SHIPMENT_ID,
            }
        ]
    )
    material = {
        "contract_name": "supplier_cny_payment_10_recovery_plan_v1",
        "scope": {
            "document_id": DOCUMENT_ID,
            "file_sha256": FILE_SHA256,
            "document_number": DOCUMENT_NUMBER,
            "amount_cny": AMOUNT_CNY,
            "old_shipment_id": OLD_SHIPMENT_ID,
            "old_invoice_no": OLD_INVOICE_NO,
            "target_shipment_id": TARGET_SHIPMENT_ID,
            "target_invoice_no": TARGET_INVOICE_NO,
        },
        "target_before_digest": "sha256:" + _hash(state),
        "non_target_before_digest": non_target_digest,
        "expected_affected_rows": len(changes),
        "changes": changes,
    }
    return {
        **material,
        "mode": "dry_run",
        "would_change": bool(changes),
        "fingerprint": "sha256:" + _hash(material),
        "readback": _readback(state),
    }


def apply_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    backup_root: Path,
) -> dict[str, Any]:
    if not plan.get("would_change"):
        return {
            **dict(plan),
            "mode": "apply",
            "applied": False,
            "backup": None,
            "post_apply": {"idempotent": True, "changed_rows": 0},
        }
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_root / f"registry_upload.cny-payment-10.{timestamp}.sqlite3"
    _sqlite_backup(runtime.db_path, backup_path)
    applied_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    with _connect(runtime.db_path, read_only=True) as conn:
        current = _target_state(conn)
        current_non_target = _non_target_digest(conn)
    if "sha256:" + _hash(current) != str(plan["target_before_digest"]):
        raise ValueError("target source fingerprint changed after dry-run")
    if current_non_target != str(plan["non_target_before_digest"]):
        raise ValueError("non-target source fingerprint changed after dry-run")

    block = CnyLedgerBlock(runtime=runtime, timestamp_factory=lambda: applied_at)
    result = block.relink_document(
        DOCUMENT_ID,
        target_shipment_id=TARGET_SHIPMENT_ID,
    )
    if str(result.get("outcome") or "") not in {"relinked", "restored"}:
        raise ValueError(f"unexpected correction outcome: {result.get('outcome')}")

    with _connect(runtime.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {AUDIT_TABLE}(
                recovery_id TEXT PRIMARY KEY,
                plan_fingerprint TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL,
                backup_path TEXT NOT NULL,
                changed_rows INTEGER NOT NULL,
                report_json TEXT NOT NULL
            )
            """
        )
        recovery_id = "scny10_" + str(plan["fingerprint"]).split(":", 1)[-1][:24]
        conn.execute(
            f"""
            INSERT INTO {AUDIT_TABLE}(
                recovery_id,plan_fingerprint,applied_at,backup_path,
                changed_rows,report_json
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                recovery_id,
                plan["fingerprint"],
                applied_at,
                str(backup_path),
                int(plan["expected_affected_rows"]),
                json.dumps(dict(plan), ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()

    post = build_plan(runtime.db_path)
    if post["would_change"]:
        raise ValueError("post-apply readback is not idempotent")
    if post["non_target_before_digest"] != plan["non_target_before_digest"]:
        raise ValueError("non-target invariant changed after correction")
    readback = post["readback"]
    if (
        readback["active_document_count"] != 1
        or readback["operation_count"] != 1
        or readback["capital_layer_count"] != 1
        or readback["source_order_id"] != TARGET_SHIPMENT_ID
        or readback["operation_shipment_id"] != TARGET_SHIPMENT_ID
        or readback["capital_shipment_id"] != TARGET_SHIPMENT_ID
    ):
        raise ValueError("post-apply CNY chain conservation failed")
    return {
        **dict(plan),
        "mode": "apply",
        "applied": True,
        "backup": {
            "path": str(backup_path),
            "sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
        },
        "correction": result,
        "post_apply": {
            "idempotent": True,
            "changed_rows": int(plan["expected_affected_rows"]),
            "readback": readback,
            "non_target_digest": post["non_target_before_digest"],
        },
    }


def _target_state(conn: sqlite3.Connection) -> dict[str, Any]:
    document_row = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_cny_documents WHERE document_id=?",
        (DOCUMENT_ID,),
    ).fetchone()
    if document_row is None:
        raise ValueError(f"target CNY document is missing: {DOCUMENT_ID}")
    shipments = {
        str(row["shipment_id"]): dict(row)
        for row in conn.execute(
            """
            SELECT shipment_id,invoice_no,expenses_complete,cny_paid_amount,
                   cny_payment_currency_rub_cost,cny_calculation_status,updated_at
            FROM sheet_vitrina_v1_supplier_shipments
            WHERE shipment_id IN (?,?)
            ORDER BY shipment_id
            """,
            (OLD_SHIPMENT_ID, TARGET_SHIPMENT_ID),
        ).fetchall()
    }
    operations = [
        dict(row)
        for row in conn.execute(
            """
            SELECT operation_id,source_document_id,source_order_id,cny_delta,
                   rub_value_delta,status,error_reason
            FROM sheet_vitrina_v1_cny_ledger_operations
            WHERE source_document_id=?
            ORDER BY operation_id
            """,
            (DOCUMENT_ID,),
        ).fetchall()
    ]
    layers = [
        dict(row)
        for row in conn.execute(
            """
            SELECT payment_id,shipment_id,paid_cny,paid_rub,
                   incremental_paid_share,cumulative_paid_share,fingerprint
            FROM sheet_vitrina_v1_own_capital_payment_layers
            WHERE payment_id=?
            ORDER BY payment_id
            """,
            (DOCUMENT_ID,),
        ).fetchall()
    ]
    same_sha = [
        dict(row)
        for row in conn.execute(
            """
            SELECT document_id,source_order_id,status,file_sha256
            FROM sheet_vitrina_v1_cny_documents
            WHERE file_sha256=?
            ORDER BY document_id
            """,
            (FILE_SHA256,),
        ).fetchall()
    ]
    expense_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM sheet_vitrina_v1_supplier_financial_expense_lines
            WHERE financial_document_id=?
            """,
            (DOCUMENT_ID,),
        ).fetchone()[0]
    )
    return {
        "document": dict(document_row),
        "shipments": shipments,
        "operations": operations,
        "capital_layers": layers,
        "same_sha_documents": same_sha,
        "expense_line_count": expense_count,
    }


def _validate_state(state: Mapping[str, Any]) -> None:
    document = dict(state.get("document") or {})
    shipments = dict(state.get("shipments") or {})
    if str((shipments.get(OLD_SHIPMENT_ID) or {}).get("invoice_no") or "") != OLD_INVOICE_NO:
        raise ValueError("old shipment identity mismatch")
    if str((shipments.get(TARGET_SHIPMENT_ID) or {}).get("invoice_no") or "") != TARGET_INVOICE_NO:
        raise ValueError("target shipment identity mismatch")
    if (
        str(document.get("document_type") or "") != "supplier_cny_payment"
        or str(document.get("document_number") or "") != DOCUMENT_NUMBER
        or str(document.get("file_sha256") or "") != FILE_SHA256
        or _decimal_text(document.get("cny_amount")) != AMOUNT_CNY
    ):
        raise ValueError("CNY payment identity mismatch")
    if str(document.get("source_order_id") or "") not in {
        OLD_SHIPMENT_ID,
        TARGET_SHIPMENT_ID,
    }:
        raise ValueError("CNY payment has an unexpected shipment binding")
    if str(document.get("status") or "") not in {"posted", "excluded"}:
        raise ValueError("CNY payment has an unsupported status")
    same_sha = list(state.get("same_sha_documents") or [])
    if len(same_sha) != 1 or str(same_sha[0].get("document_id") or "") != DOCUMENT_ID:
        raise ValueError("same-SHA document cardinality mismatch")
    if int(state.get("expense_line_count") or 0) != 0:
        raise ValueError("CNY payment unexpectedly owns financial expense lines")
    operations = list(state.get("operations") or [])
    layers = list(state.get("capital_layers") or [])
    if str(document.get("status") or "") == "posted":
        if len(operations) != 1 or len(layers) != 1:
            raise ValueError("active CNY payment chain cardinality mismatch")
        if _decimal_text(abs(Decimal(str(operations[0].get("cny_delta") or 0)))) != AMOUNT_CNY:
            raise ValueError("CNY ledger amount mismatch")
        if _decimal_text(layers[0].get("paid_cny")) != AMOUNT_CNY:
            raise ValueError("capital layer amount mismatch")


def _readback(state: Mapping[str, Any]) -> dict[str, Any]:
    document = dict(state.get("document") or {})
    operations = list(state.get("operations") or [])
    layers = list(state.get("capital_layers") or [])
    return {
        "document_id": str(document.get("document_id") or ""),
        "file_sha256": str(document.get("file_sha256") or ""),
        "status": str(document.get("status") or ""),
        "source_order_id": str(document.get("source_order_id") or ""),
        "context_order_id": str(document.get("context_order_id") or ""),
        "amount_cny": _decimal_text(document.get("cny_amount")),
        "same_sha_document_count": len(state.get("same_sha_documents") or []),
        "active_document_count": (
            1 if str(document.get("status") or "") != "excluded" else 0
        ),
        "operation_count": len(operations),
        "operation_shipment_id": (
            str(operations[0].get("source_order_id") or "") if operations else ""
        ),
        "operation_cny_delta": (
            _decimal_text(operations[0].get("cny_delta")) if operations else ""
        ),
        "capital_layer_count": len(layers),
        "capital_shipment_id": (
            str(layers[0].get("shipment_id") or "") if layers else ""
        ),
        "capital_paid_cny": (
            _decimal_text(layers[0].get("paid_cny")) if layers else ""
        ),
        "expense_line_count": int(state.get("expense_line_count") or 0),
    }


def _non_target_digest(conn: sqlite3.Connection) -> str:
    documents = [
        dict(row)
        for row in conn.execute(
            """
            SELECT document_id,document_type,source_order_id,context_order_id,
                   status,file_sha256,natural_key,cny_amount,rub_amount
            FROM sheet_vitrina_v1_cny_documents
            WHERE document_id<>?
            ORDER BY document_id
            """,
            (DOCUMENT_ID,),
        ).fetchall()
    ]
    operations = [
        dict(row)
        for row in conn.execute(
            """
            SELECT operation_id,operation_type,source_document_id,source_order_id,
                   operation_date,cny_delta,rub_value_delta,status,error_reason
            FROM sheet_vitrina_v1_cny_ledger_operations
            WHERE source_document_id<>?
            ORDER BY operation_id
            """,
            (DOCUMENT_ID,),
        ).fetchall()
    ]
    return "sha256:" + _hash(
        {"cny_documents": documents, "cny_operations": operations}
    )


def _sqlite_backup(source: Path, target: Path) -> None:
    if target.exists():
        raise ValueError(f"backup target already exists: {target}")
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)
    if target.stat().st_size <= 0:
        raise ValueError("SQLite backup is empty")


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _decimal_text(value: Any) -> str:
    from decimal import Decimal

    text = format(Decimal(str(value or 0)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
