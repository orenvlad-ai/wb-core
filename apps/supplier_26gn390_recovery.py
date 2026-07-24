#!/usr/bin/env python3
"""Bounded recovery for supplier shipment 26GN390 and logistics invoice 136."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.application.own_product_capital import (  # noqa: E402
    OwnProductCapitalBlock,
)
from packages.application.warehouse_functional import (  # noqa: E402
    enqueue_warehouse_targeted_recalculation,
)


SHIPMENT_ID = "sup_b3070385b00b4eb680bd805d751d65be"
INVOICE_NO = "26GN390"
DOCUMENT_NUMBER = "136"
ACTIVE_DOCUMENT_ID = "fdoc_b53824642d3e41bd8afa60785bdb6ed2"
ARCHIVED_DOCUMENT_ID = "fdoc_883d0528332c4900aa92348a45163b48"
EXPECTED_FILE_SHA256 = (
    "e9358919df6b1de9ebb75943e2d2d05dc2a522df8c16328c05559a07c3837136"
)
EXPECTED_AMOUNT_RUB = "1075030.00"
TARGET_FF_ACCEPTANCE_DATE = "2026-07-21"
TARGET_DOCUMENT_IDS = {ACTIVE_DOCUMENT_ID, ARCHIVED_DOCUMENT_ID}
AUDIT_TABLE = "sheet_vitrina_v1_supplier_26gn390_recovery_audit"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args(argv)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    report = build_plan(runtime.db_path)
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if str(args.fingerprint or "") != str(report["fingerprint"]):
        raise ValueError("apply requires the exact current dry-run fingerprint")
    backup_root = (
        Path(args.backup_dir)
        if args.backup_dir
        else runtime.runtime_dir / "backups" / "supplier-26gn390-recovery"
    )
    result = apply_plan(runtime, report, backup_root=backup_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def build_plan(db_path: Path) -> dict[str, Any]:
    with _connect(db_path, read_only=True) as conn:
        shipment = _shipment(conn)
        documents = _documents(conn)
        side_effects = _side_effects(conn)
        target_digest = _target_digest(shipment, documents)
        non_target_digest = _non_target_digest(conn)
    _validate_identity(shipment, documents)
    active = next(
        item for item in documents if item["document_id"] == ACTIVE_DOCUMENT_ID
    )
    archive = next(
        item for item in documents if item["document_id"] == ARCHIVED_DOCUMENT_ID
    )
    changes: list[dict[str, Any]] = []
    if str(active["parse_status"]) == "excluded":
        changes.append(
            {
                "action": "restore_active",
                "document_id": ACTIVE_DOCUMENT_ID,
                "new_status": "parsed",
            }
        )
    if str(archive["parse_status"]) != "excluded":
        changes.append(
            {
                "action": "archive_duplicate",
                "document_id": ARCHIVED_DOCUMENT_ID,
                "new_status": "excluded",
            }
        )
    extra_active = [
        item
        for item in documents
        if item["document_id"] not in TARGET_DOCUMENT_IDS
        and _is_invoice_136(item)
        and str(item["parse_status"]) != "excluded"
    ]
    for item in extra_active:
        changes.append(
            {
                "action": "archive_unexpected_duplicate",
                "document_id": item["document_id"],
                "new_status": "excluded",
            }
        )
    actual_ff_acceptance_date = str(
        shipment.get("actual_ff_acceptance_date") or ""
    )
    if actual_ff_acceptance_date not in {"", TARGET_FF_ACCEPTANCE_DATE}:
        raise ValueError(
            "target shipment has an unexpected actual FF acceptance date"
        )
    if actual_ff_acceptance_date != TARGET_FF_ACCEPTANCE_DATE:
        changes.append(
            {
                "action": "confirm_ff_acceptance_date",
                "shipment_id": SHIPMENT_ID,
                "old_value": actual_ff_acceptance_date,
                "new_value": TARGET_FF_ACCEPTANCE_DATE,
            }
        )
    if (
        int(side_effects.get("archived_financial_capital_event_count") or 0) > 0
        or int(side_effects.get("active_financial_capital_event_count") or 0) == 0
    ):
        changes.append(
            {
                "action": "reconcile_financial_capital_chain",
                "active_document_id": ACTIVE_DOCUMENT_ID,
                "archived_document_id": ARCHIVED_DOCUMENT_ID,
            }
        )
    direct_changes = [
        item
        for item in changes
        if item["action"]
        in {
            "restore_active",
            "archive_duplicate",
            "archive_unexpected_duplicate",
        }
    ]
    plan_material = {
        "contract_name": "supplier_26gn390_recovery_plan_v1",
        "scope": {
            "shipment_id": SHIPMENT_ID,
            "invoice_no": INVOICE_NO,
            "document_number": DOCUMENT_NUMBER,
            "active_document_id": ACTIVE_DOCUMENT_ID,
            "archived_document_id": ARCHIVED_DOCUMENT_ID,
            "file_sha256": EXPECTED_FILE_SHA256,
            "amount_rub": EXPECTED_AMOUNT_RUB,
        },
        "target_before_digest": target_digest,
        "non_target_before_digest": non_target_digest,
        "expected_affected_rows": len(changes),
        "expected_direct_rows": len(direct_changes),
        "changes": changes,
    }
    fingerprint = "sha256:" + _hash(plan_material)
    return {
        **plan_material,
        "mode": "dry_run",
        "would_change": bool(changes),
        "fingerprint": fingerprint,
        "readback": _readback_projection(shipment, documents, side_effects),
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
    backup_path = backup_root / f"registry_upload.26gn390.{timestamp}.sqlite3"
    _sqlite_backup(runtime.db_path, backup_path)
    applied_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    with _connect(runtime.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            shipment = _shipment(conn)
            documents = _documents(conn)
            _validate_identity(shipment, documents)
            if _target_digest(shipment, documents) != plan["target_before_digest"]:
                raise ValueError("target source fingerprint changed after dry-run")
            if _non_target_digest(conn) != plan["non_target_before_digest"]:
                raise ValueError("non-target source fingerprint changed after dry-run")
            changed_rows = 0
            direct_changes = [
                item
                for item in plan.get("changes") or []
                if item["action"]
                in {
                    "restore_active",
                    "archive_duplicate",
                    "archive_unexpected_duplicate",
                }
            ]
            for change in direct_changes:
                cursor = conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_supplier_financial_documents
                    SET parse_status=?,updated_at=?
                    WHERE supplier_order_id=? AND document_id=?
                      AND parse_status<>?
                    """,
                    (
                        change["new_status"],
                        applied_at,
                        SHIPMENT_ID,
                        change["document_id"],
                        change["new_status"],
                    ),
                )
                changed_rows += int(cursor.rowcount or 0)
                if change["new_status"] == "excluded":
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_cny_documents
                        SET status='excluded',updated_at=?
                        WHERE linked_financial_document_id=?
                          AND status<>'excluded'
                        """,
                        (applied_at, change["document_id"]),
                    )
            if changed_rows != int(plan["expected_direct_rows"]):
                raise ValueError(
                    "affected row count differs from approved dry-run plan"
                )
            if direct_changes:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_supplier_shipments
                    SET expenses_complete=0,updated_at=?
                    WHERE shipment_id=?
                    """,
                    (applied_at, SHIPMENT_ID),
                )
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
            recovery_id = "s26r_" + str(plan["fingerprint"]).split(":", 1)[-1][:24]
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
                    changed_rows,
                    json.dumps(dict(plan), ensure_ascii=False, sort_keys=True),
                ),
            )
            if _non_target_digest(conn) != plan["non_target_before_digest"]:
                raise ValueError("non-target invariant changed inside transaction")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    shipment = runtime.load_supplier_shipment(SHIPMENT_ID) or {}
    capital_reconciliation: dict[str, Any] | None = None
    if any(
        item["action"] == "reconcile_financial_capital_chain"
        for item in plan.get("changes") or []
    ):
        capital = OwnProductCapitalBlock(
            runtime=runtime,
            timestamp_factory=lambda: applied_at,
        )
        removed = capital.remove_financial_document_expenses(
            ARCHIVED_DOCUMENT_ID,
            recalculate=False,
        )
        materialized = capital.materialize_persisted_expense_events(
            shipment_id=SHIPMENT_ID,
        )
        capital_reconciliation = {
            "archived_events_removed": int(
                removed.get("removed_event_count") or 0
            ),
            "active_chain": materialized,
        }
    queue: dict[str, Any] | None = None
    if any(
        item["action"]
        in {
            "restore_active",
            "archive_duplicate",
            "archive_unexpected_duplicate",
            "reconcile_financial_capital_chain",
        }
        for item in plan.get("changes") or []
    ):
        nm_ids = sorted(
            {
                int(item.get("internal_nm_id") or 0)
                for item in shipment.get("lines") or []
                if int(item.get("internal_nm_id") or 0) > 0
            }
        )
        queue = enqueue_warehouse_targeted_recalculation(
            runtime=runtime,
            stable_source_id=f"supplier_shipment:{SHIPMENT_ID}",
            source_revision=str(plan["fingerprint"]),
            effective_date=str(
                (shipment.get("header") or {}).get("actual_shipment_date")
                or (shipment.get("header") or {}).get("invoice_date")
                or applied_at[:10]
            )[:10],
            affected_nm_ids=nm_ids,
            requested_at=applied_at,
        )
    date_confirmation: dict[str, Any] | None = None
    if any(
        item["action"] == "confirm_ff_acceptance_date"
        for item in plan.get("changes") or []
    ):
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime.runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: applied_at,
        )
        preview = entrypoint.handle_supplier_factual_dates_preview_request(
            SHIPMENT_ID,
            {"actual_ff_acceptance_date": TARGET_FF_ACCEPTANCE_DATE},
        )
        date_confirmation = (
            entrypoint.handle_supplier_factual_dates_confirm_request(
                SHIPMENT_ID,
                {"confirmation_token": preview["confirmation_token"]},
                actor="supplier_26gn390_recovery",
            )
        )
    post = build_plan(runtime.db_path)
    if post["would_change"]:
        raise ValueError("post-apply readback is not idempotent")
    if post["non_target_before_digest"] != plan["non_target_before_digest"]:
        raise ValueError("non-target invariant changed after recovery")
    readback = dict(post["readback"])
    if (
        int(readback.get("active_count") or 0) != 1
        or int(readback.get("excluded_count") or 0) < 1
        or int(readback.get("active_financial_capital_event_count") or 0) <= 0
        or int(readback.get("archived_financial_capital_event_count") or 0) != 0
        or int(readback.get("ff_receipt_count") or 0) != 1
        or int(readback.get("ff_cost_layer_count") or 0) != 1
    ):
        raise ValueError("post-apply document/capital/FF chain invariant failed")
    return {
        **dict(plan),
        "mode": "apply",
        "applied": True,
        "backup": {
            "path": str(backup_path),
            "sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
        },
        "targeted_recalculation": queue,
        "capital_reconciliation": capital_reconciliation,
        "date_confirmation": date_confirmation,
        "post_apply": {
            "idempotent": True,
            "changed_rows": int(plan["expected_affected_rows"]),
            "readback": readback,
            "non_target_digest": post["non_target_before_digest"],
        },
    }


def _shipment(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
        (SHIPMENT_ID,),
    ).fetchone()
    if row is None:
        raise ValueError(f"target shipment is missing: {SHIPMENT_ID}")
    return dict(row)


def _documents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT document.*,
               COALESCE((
                   SELECT SUM(COALESCE(line.amount_rub,0))
                   FROM sheet_vitrina_v1_supplier_financial_expense_lines line
                   WHERE line.financial_document_id=document.document_id
               ),0) expense_amount_rub,
               COALESCE((
                   SELECT COUNT(*)
                   FROM sheet_vitrina_v1_supplier_financial_expense_lines line
                   WHERE line.financial_document_id=document.document_id
               ),0) expense_line_count
        FROM sheet_vitrina_v1_supplier_financial_documents document
        WHERE supplier_order_id=?
        ORDER BY document_id
        """,
        (SHIPMENT_ID,),
    ).fetchall()
    return [dict(row) for row in rows]


def _validate_identity(
    shipment: Mapping[str, Any], documents: Iterable[Mapping[str, Any]]
) -> None:
    if str(shipment.get("invoice_no") or "") != INVOICE_NO:
        raise ValueError("target shipment invoice identity mismatch")
    by_id = {str(item.get("document_id") or ""): item for item in documents}
    for document_id in TARGET_DOCUMENT_IDS:
        if document_id not in by_id:
            raise ValueError(f"target financial document is missing: {document_id}")
        document = by_id[document_id]
        if not _is_invoice_136(document):
            raise ValueError(f"financial document identity mismatch: {document_id}")
        if str(document.get("file_sha256") or "") != EXPECTED_FILE_SHA256:
            raise ValueError(f"financial document SHA mismatch: {document_id}")
        if _money(document.get("total_amount_rub")) != EXPECTED_AMOUNT_RUB:
            raise ValueError(f"financial document amount mismatch: {document_id}")
        if _money(document.get("expense_amount_rub")) != EXPECTED_AMOUNT_RUB:
            raise ValueError(
                f"financial expense-line conservation mismatch: {document_id}"
            )
        if int(document.get("expense_line_count") or 0) != 1:
            raise ValueError(
                f"financial document must have exactly one expense line: {document_id}"
            )


def _is_invoice_136(document: Mapping[str, Any]) -> bool:
    return (
        str(document.get("document_type") or "") == "logistics_invoice"
        and str(document.get("document_number") or "") == DOCUMENT_NUMBER
        and str(document.get("document_date") or "") == "2026-07-15"
        and str(document.get("currency") or "").upper() == "RUB"
        and _money(document.get("total_amount_rub")) == EXPECTED_AMOUNT_RUB
    )


def _readback_projection(
    shipment: Mapping[str, Any],
    documents: Iterable[Mapping[str, Any]],
    side_effects: Mapping[str, Any],
) -> dict[str, Any]:
    docs = [dict(item) for item in documents if _is_invoice_136(item)]
    return {
        "shipment_id": SHIPMENT_ID,
        "invoice_no": str(shipment.get("invoice_no") or ""),
        "actual_shipment_date": str(shipment.get("actual_shipment_date") or ""),
        "actual_ff_acceptance_date": str(
            shipment.get("actual_ff_acceptance_date") or ""
        ),
        "expenses_complete": bool(shipment.get("expenses_complete")),
        "invoice_136": [
            {
                "document_id": item["document_id"],
                "parse_status": item["parse_status"],
                "file_sha256": item["file_sha256"],
                "amount_rub": _money(item["total_amount_rub"]),
                "expense_line_count": int(item["expense_line_count"]),
                "expense_amount_rub": _money(item["expense_amount_rub"]),
            }
            for item in docs
        ],
        "active_count": sum(
            1 for item in docs if str(item["parse_status"]) != "excluded"
        ),
        "excluded_count": sum(
            1 for item in docs if str(item["parse_status"]) == "excluded"
        ),
        **dict(side_effects),
    }


def _side_effects(conn: sqlite3.Connection) -> dict[str, Any]:
    receipt_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM sheet_vitrina_v1_ff_stock_operations
            WHERE source_key=?
            """,
            (f"supplier_shipment_acceptance:{SHIPMENT_ID}",),
        ).fetchone()[0]
    )
    layer_rows = conn.execute(
        """
        SELECT layer_id,status,accepted_ff_date,reconciliation_status,
               reconciliation_delta_rub,is_current
        FROM sheet_vitrina_v1_supplier_ff_cost_layers
        WHERE supplier_shipment_id=? AND is_current=1
        ORDER BY layer_id
        """,
        (SHIPMENT_ID,),
    ).fetchall()
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    capital_counts = {
        ACTIVE_DOCUMENT_ID: 0,
        ARCHIVED_DOCUMENT_ID: 0,
    }
    if "sheet_vitrina_v1_own_capital_events" in tables:
        for document_id in capital_counts:
            capital_counts[document_id] = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sheet_vitrina_v1_own_capital_events
                    WHERE event_type='cost_payment'
                      AND event_id LIKE ? ESCAPE '\\'
                    """,
                    (
                        _literal_like_prefix(
                            f"cost_payment:financial_expense:{document_id}:"
                        ),
                    ),
                ).fetchone()[0]
            )
    return {
        "ff_receipt_count": receipt_count,
        "ff_cost_layer_count": len(layer_rows),
        "ff_cost_layers": [dict(row) for row in layer_rows],
        "active_financial_capital_event_count": capital_counts[
            ACTIVE_DOCUMENT_ID
        ],
        "archived_financial_capital_event_count": capital_counts[
            ARCHIVED_DOCUMENT_ID
        ],
    }


def _target_digest(
    shipment: Mapping[str, Any], documents: Iterable[Mapping[str, Any]]
) -> str:
    return "sha256:" + _hash(
        {
            "shipment": {
                key: shipment.get(key)
                for key in (
                    "shipment_id",
                    "invoice_no",
                    "actual_shipment_date",
                    "actual_ff_acceptance_date",
                    "expenses_complete",
                    "updated_at",
                )
            },
            "documents": [
                {
                    key: item.get(key)
                    for key in (
                        "document_id",
                        "parse_status",
                        "file_sha256",
                        "total_amount_rub",
                        "expense_amount_rub",
                        "expense_line_count",
                        "updated_at",
                    )
                }
                for item in documents
                if str(item.get("document_id") or "") in TARGET_DOCUMENT_IDS
                or _is_invoice_136(item)
            ],
        }
    )


def _non_target_digest(conn: sqlite3.Connection) -> str:
    documents = [
        dict(row)
        for row in conn.execute(
            """
            SELECT document_id,supplier_order_id,parse_status,file_sha256,
                   total_amount_rub,updated_at
            FROM sheet_vitrina_v1_supplier_financial_documents
            WHERE document_id NOT IN (?,?)
              AND NOT (
                  supplier_order_id=? AND document_type='logistics_invoice'
                  AND document_number=? AND document_date='2026-07-15'
              )
            ORDER BY document_id
            """,
            (
                ACTIVE_DOCUMENT_ID,
                ARCHIVED_DOCUMENT_ID,
                SHIPMENT_ID,
                DOCUMENT_NUMBER,
            ),
        ).fetchall()
    ]
    shipments = [
        dict(row)
        for row in conn.execute(
            """
            SELECT shipment_id,updated_at,expenses_complete,actual_shipment_date,
                   actual_ff_acceptance_date
            FROM sheet_vitrina_v1_supplier_shipments
            WHERE shipment_id<>? ORDER BY shipment_id
            """,
            (SHIPMENT_ID,),
        ).fetchall()
    ]
    return "sha256:" + _hash({"documents": documents, "shipments": shipments})


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


def _money(value: Any) -> str:
    from decimal import Decimal, ROUND_HALF_UP

    return str(Decimal(str(value or 0)).quantize(Decimal("0.01"), ROUND_HALF_UP))


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


def _literal_like_prefix(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
        + "%"
    )


if __name__ == "__main__":
    raise SystemExit(main())
