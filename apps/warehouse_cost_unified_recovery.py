#!/usr/bin/env python3
"""Bounded unified warehouse/cost recovery with an exact dry-run manifest."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterator, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_stock_ledger import FfStockLedgerBlock  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.supplier_financial_documents import (  # noqa: E402
    SupplierFinancialDocumentsBlock,
    _statement_reference_identity,
    build_bank_fee_statement_import_preview,
)
from packages.application.supplier_shipment_factual_correction import (  # noqa: E402
    SupplierShipmentFactualCorrectionBlock,
)
from packages.application.warehouse_functional import (  # noqa: E402
    WarehouseFunctionalBlock,
)
from packages.application.warehouse_functional_economics_backfill import (  # noqa: E402
    apply_functional_economics_backfill_plan,
    build_functional_economics_backfill_plan,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)
from packages.application.wb_supply_box_correction import (  # noqa: E402
    BOX_CORRECTION_TABLE,
    apply_unique_box_correction,
    ensure_wb_supply_box_correction_schema,
    solve_unique_box_correction,
)


AUDIT_TABLE = "sheet_vitrina_v1_warehouse_cost_unified_recovery_audit"
AUDIT_SQLITE_LOCK_WAIT_MS = 300_000
ZERO = Decimal("0")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--shipment-id", required=True)
    parser.add_argument("--invoice-no", required=True)
    parser.add_argument("--expected-planned-date", required=True)
    parser.add_argument("--actual-shipment-date", required=True)
    parser.add_argument("--statement-document-id", required=True)
    parser.add_argument(
        "--commission-amount",
        action="append",
        default=[],
        help="Expected atomic amount of the one logical commission to import.",
    )
    parser.add_argument("--expected-bank-total", default="")
    parser.add_argument("--expected-logical-fee-count", type=int, default=0)
    parser.add_argument("--expected-atomic-fee-count", type=int, default=0)
    parser.add_argument("--supply-id", action="append", default=[])
    parser.add_argument("--box-supply-id", default="")
    parser.add_argument("--factory-box-size", type=int, default=0)
    parser.add_argument("--actor", default="warehouse-cost-unified-recovery")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime = RegistryUploadDbBackedRuntime(
        runtime_dir=Path(args.runtime_dir).resolve()
    )
    if args.apply and args.fingerprint:
        existing = _load_audit_record(runtime, args.fingerprint)
        if existing is not None and existing["status"] == "complete":
            print(
                json.dumps(
                    {
                        **dict(existing.get("report") or {}),
                        "applied": False,
                        "idempotent": True,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
            )
            return 0
        if existing is not None and existing["status"] in {"running", "failed"}:
            plan = dict(existing["plan"])
            if str(plan.get("fingerprint") or "") != str(args.fingerprint):
                raise ValueError("stored unified recovery plan fingerprint changed")
        else:
            plan = build_plan(runtime, args)
    else:
        plan = build_plan(runtime, args)
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if str(args.fingerprint or "") != str(plan["fingerprint"]):
        raise ValueError("apply requires the exact current unified dry-run fingerprint")
    result = apply_plan(runtime, args, plan)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def build_plan(
    runtime: RegistryUploadDbBackedRuntime,
    args: argparse.Namespace,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    with _connect_readonly(runtime.db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        shipment = _shipment(conn, args.shipment_id)
        header = dict(shipment["header"])
        if str(header.get("invoice_no") or "") != str(args.invoice_no):
            raise ValueError("supplier shipment invoice identity changed")
        if str(header.get("shipment_date") or "")[:10] != str(
            args.expected_planned_date
        )[:10]:
            raise ValueError("supplier shipment planned date changed")
        actual_before = str(header.get("actual_shipment_date") or "")
        if actual_before not in {"", str(args.actual_shipment_date)}:
            raise ValueError("supplier shipment actual date has unexpected drift")

        bank = _bank_plan(
            conn,
            runtime=runtime,
            args=args,
            shipment=shipment,
        )
        physical = _physical_plan(conn, args=args, tables=tables)
        box = _box_plan(conn, args=args, tables=tables)
        ff_balance = _ff_balance(conn)
        prospective = dict(ff_balance)
        for raw_nm_id, raw_delta in dict(
            box.get("physical_adjustment") or {}
        ).items():
            nm_id = int(raw_nm_id)
            prospective[nm_id] = prospective.get(nm_id, ZERO) + Decimal(
                str(raw_delta)
            )
        for supply in physical["supplies"]:
            if supply["already_debited"]:
                continue
            for raw_nm_id, raw_quantity in supply["composition"].items():
                nm_id = int(raw_nm_id)
                prospective[nm_id] = prospective.get(nm_id, ZERO) - Decimal(
                    str(raw_quantity)
                )
        shortages = {
            str(nm_id): str(quantity)
            for nm_id, quantity in prospective.items()
            if quantity < ZERO
        }
        if shortages:
            raise ValueError(
                "target physical closure has insufficient FF stock: "
                + json.dumps(shortages, sort_keys=True)
            )
        affected_nm_ids = sorted(
            {
                int(line.get("internal_nm_id") or 0)
                for line in shipment["lines"]
                if int(line.get("internal_nm_id") or 0) > 0
            }
            | {
                int(nm_id)
                for item in physical["supplies"]
                for nm_id in item["composition"]
            }
            | {
                int(nm_id)
                for nm_id in dict(box.get("corrected") or {})
            }
        )
        earliest_business_date = min(
            {
                str(value)[:10]
                for value in [
                    str(args.actual_shipment_date),
                    *list(bank.get("atomic_business_dates") or []),
                ]
                if len(str(value)) >= 10
            }
        )
        material = {
            "contract_name": "warehouse_cost_unified_recovery_v1",
            "scope": {
                "shipment_id": str(args.shipment_id),
                "invoice_no": str(args.invoice_no),
                "statement_document_id": str(args.statement_document_id),
                "supply_ids": sorted({str(item) for item in args.supply_id}),
                "box_supply_id": str(args.box_supply_id or ""),
                "affected_nm_ids": affected_nm_ids,
                "earliest_business_date": earliest_business_date,
            },
            "shipment": {
                "source_revision": _fingerprint(
                    {"header": header, "lines": shipment["lines"]}
                ),
                "planned_date": str(header.get("shipment_date") or "")[:10],
                "actual_date_before": actual_before,
                "actual_date_after": str(args.actual_shipment_date),
                "would_change": actual_before != str(args.actual_shipment_date),
            },
            "bank": bank,
            "physical": physical,
            "box": box,
            "ff": {
                "balance_before": _decimal_map(ff_balance),
                "balance_after_targets": _decimal_map(prospective),
                "shortage_count": 0,
            },
            "active_version": _active_version(conn),
            "target_queue_before": _target_queue(conn, str(args.shipment_id)),
        }
        would_change = any(
            (
                material["shipment"]["would_change"],
                bank["would_change"],
                physical["would_change"],
                box["would_change"],
            )
        )
        source_digest = _fingerprint(material)
        total_changes = conn.total_changes
        query_only = int(conn.execute("PRAGMA query_only").fetchone()[0])
    elapsed = (
        datetime.now(timezone.utc) - started
    ).total_seconds() * 1000
    return {
        **material,
        "source_digest": source_digest,
        "would_change": would_change,
        "mode": "dry_run",
        "fingerprint": _fingerprint(
            {
                **material,
                "source_digest": source_digest,
                "would_change": would_change,
            }
        ),
        "performance": {
            "elapsed_ms": round(elapsed, 3),
            "copy_bytes": 0,
            "full_database_copy": False,
            "full_database_integrity_scan": False,
            "finance_raw_rows_read": 0,
            "query_only": query_only,
            "sqlite_total_changes": total_changes,
            "complexity": "O(exact shipment + selected supplies + affected SKU rows)",
        },
    }


def apply_plan(
    runtime: RegistryUploadDbBackedRuntime,
    args: argparse.Namespace,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    if not plan.get("would_change"):
        return {
            **dict(plan),
            "mode": "apply",
            "applied": False,
            "idempotent": True,
        }
    audit_sqlite_lock_wait_ms = _ensure_audit_schema(runtime)
    prior_audit = _load_audit_record(
        runtime, str(plan["fingerprint"])
    )
    resuming = bool(
        prior_audit
        and prior_audit.get("status") in {"running", "failed"}
    )
    audit_sqlite_lock_wait_ms += _start_audit(runtime, plan)
    started = datetime.now(timezone.utc)
    audit = _load_audit_record(runtime, str(plan["fingerprint"])) or {}
    steps: dict[str, Any] = dict(audit.get("steps") or {})
    try:
        with warehouse_functional_write_lock(
            runtime.runtime_dir,
            timeout_seconds=300,
        ) as lock_info:
            fresh = build_plan(runtime, args)
            if not resuming:
                if str(fresh["fingerprint"]) != str(plan["fingerprint"]):
                    raise ValueError("unified recovery source changed after dry-run")
            else:
                _validate_resume_invariants(plan, fresh)

            bank_plan = dict(plan["bank"])
            if "bank" not in steps:
                if bank_plan.get("would_change"):
                    document = runtime.load_supplier_financial_document(
                        supplier_order_id=args.shipment_id,
                        document_id=args.statement_document_id,
                    )
                    if document is None:
                        raise ValueError("bank statement disappeared before apply")
                    normalized = dict(document.get("normalized_parse") or {})
                    refreshed_import = dict(bank_plan["fresh_statement_import"])
                    refreshed_import["target_revision"] = str(
                        bank_plan["target_revision"]
                    )
                    refreshed_import["import_status"] = "preview_pending"
                    refreshed_import["confirmed_operation_ids"] = sorted(
                        {
                            str(value)
                            for value in bank_plan.get(
                                "already_imported_operation_ids"
                            )
                            or []
                            if str(value)
                        }
                    )
                    runtime.save_supplier_financial_document(
                        document={
                            **document,
                            "updated_at": _now(),
                            "normalized_parse": {
                                **normalized,
                                "statement_import": refreshed_import,
                            },
                        },
                        expense_lines=[
                            dict(item)
                            for item in document.get("expense_lines") or []
                        ],
                    )
                    steps["bank"] = (
                        SupplierFinancialDocumentsBlock(runtime=runtime)
                        .confirm_bank_fee_statement_import(
                            args.shipment_id,
                            args.statement_document_id,
                            selected_operation_ids=[
                                str(bank_plan["logical_fee_id"])
                            ],
                            expected_source_sha256=str(
                                document.get("file_sha256") or ""
                            ),
                            expected_target_revision=str(
                                bank_plan["target_revision"]
                            ),
                        )
                    )
                else:
                    steps["bank"] = {"applied": False, "idempotent": True}
                audit_sqlite_lock_wait_ms += _checkpoint_audit(
                    runtime, plan, steps
                )

            box_plan = dict(plan["box"])
            if "box" not in steps:
                if box_plan.get("would_change"):
                    with sqlite3.connect(runtime.db_path) as conn:
                        conn.row_factory = sqlite3.Row
                        ensure_wb_supply_box_correction_schema(conn)
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            for nm_id in box_plan.get("box_size_updates") or []:
                                updated = conn.execute(
                                    """
                                    UPDATE sheet_vitrina_v1_nomenclature_items
                                    SET factory_box_size=?,updated_at=?
                                    WHERE nm_id=? AND factory_box_size IS NULL
                                    """,
                                    (
                                        int(args.factory_box_size),
                                        _now(),
                                        int(nm_id),
                                    ),
                                )
                                if int(updated.rowcount or 0) != 1:
                                    existing_size = conn.execute(
                                        """
                                        SELECT factory_box_size
                                        FROM sheet_vitrina_v1_nomenclature_items
                                        WHERE nm_id=?
                                        """,
                                        (int(nm_id),),
                                    ).fetchone()
                                    if (
                                        existing_size is None
                                        or int(existing_size["factory_box_size"] or 0)
                                        != int(args.factory_box_size)
                                    ):
                                        raise ValueError(
                                            f"factory box size drift for nmID {nm_id}"
                                        )
                            steps["box"] = apply_unique_box_correction(
                                conn,
                                supply_id=str(args.box_supply_id),
                                source_revision=str(box_plan["source_revision"]),
                                solution=box_plan["solution"],
                                actor=str(args.actor),
                                created_at=_now(),
                            )
                            conn.commit()
                        except Exception:
                            conn.rollback()
                            raise
                else:
                    steps["box"] = {"applied": False, "idempotent": True}
                audit_sqlite_lock_wait_ms += _checkpoint_audit(
                    runtime, plan, steps
                )

            if "physical" not in steps:
                records = {
                    _record_supply_id(item): item
                    for item in runtime.list_wb_supplies_cache_records()
                }
                ledger = FfStockLedgerBlock(runtime=runtime)
                physical_results = []
                for item in plan["physical"]["supplies"]:
                    supply_id = str(item["supply_id"])
                    if item.get("already_debited"):
                        physical_results.append(
                            {"supply_id": supply_id, "idempotent": True}
                        )
                        continue
                    record = records.get(supply_id)
                    if record is None:
                        raise ValueError(
                            f"selected WB supply disappeared before apply: {supply_id}"
                        )
                    result = ledger.record_wb_supply_debit(record) or {}
                    if result.get("skip_reason") == "wb_supply_already_debited":
                        result = {
                            **result,
                            "supply_id": supply_id,
                            "idempotent": True,
                        }
                    if (
                        not result.get("operation_id")
                        and not result.get("idempotent")
                    ):
                        raise ValueError(
                            f"physical debit was not created for {supply_id}: "
                            + str(result.get("skip_reason") or "unknown")
                        )
                    physical_results.append(result)
                steps["physical"] = physical_results
                audit_sqlite_lock_wait_ms += _checkpoint_audit(
                    runtime, plan, steps
                )

            if "factual_date" not in steps:
                correction = SupplierShipmentFactualCorrectionBlock(runtime=runtime)
                job = correction.create_job(
                    shipment_id=str(args.shipment_id),
                    new_actual_shipment_date=str(args.actual_shipment_date),
                    actor=str(args.actor),
                )
                if str(job.get("status") or "") == "zero_change":
                    steps["factual_date"] = job
                else:
                    steps["factual_date"] = correction.run_job(
                        str(job["correction_id"])
                    )
                factual_status = str(
                    (steps["factual_date"] or {}).get("status") or ""
                )
                if factual_status not in {"success", "zero_change"}:
                    raise ValueError(
                        "factual-date correction did not succeed: "
                        + str(
                            (steps["factual_date"] or {}).get(
                                "error_message"
                            )
                            or factual_status
                        )
                    )
                audit_sqlite_lock_wait_ms += _checkpoint_audit(
                    runtime, plan, steps
                )

            stable_sources = [
                f"supplier_shipment:{args.shipment_id}",
                f"supplier_costs:{args.shipment_id}",
                *[
                    f"wb_supply:{supply_id}"
                    for supply_id in sorted(
                        {str(value) for value in args.supply_id}
                    )
                ],
            ]
            if args.box_supply_id:
                stable_sources.append(f"wb_supply:{args.box_supply_id}")
            if "functional" not in steps:
                warehouse = WarehouseFunctionalBlock(runtime=runtime)
                functional_plan = warehouse.build_targeted_recovery_plan(
                    affected_nm_ids=plan["scope"]["affected_nm_ids"],
                    stable_source_ids=stable_sources,
                )
                steps["functional"] = warehouse.apply_plan(
                    functional_plan,
                    confirm_fingerprint=str(
                        functional_plan["plan_fingerprint"]
                    ),
                )
                audit_sqlite_lock_wait_ms += _checkpoint_audit(
                    runtime, plan, steps
                )

            if "economics" not in steps:
                economics_plan = build_functional_economics_backfill_plan(
                    runtime,
                    affected_nm_ids=plan["scope"]["affected_nm_ids"],
                    earliest_business_date=plan["scope"][
                        "earliest_business_date"
                    ],
                )
                steps["economics"] = apply_functional_economics_backfill_plan(
                    runtime,
                    economics_plan,
                    confirm_fingerprint=str(
                        economics_plan["plan_fingerprint"]
                    ),
                    backup_dir=(
                        runtime.runtime_dir
                        / "backups"
                        / "targeted-economics"
                    ).resolve(),
                    target_scoped_undo=True,
                )
                audit_sqlite_lock_wait_ms += _checkpoint_audit(
                    runtime, plan, steps
                )

            after = build_plan(runtime, args)
            report = {
            "contract_name": str(plan["contract_name"]),
            "plan_fingerprint": str(plan["fingerprint"]),
            "applied_at": _now(),
            "lock_wait_ms": int(lock_info["wait_ms"]),
            "sqlite_lock_wait_ms": round(audit_sqlite_lock_wait_ms, 3),
            "steps": steps,
            "before": {
                "shipment": plan["shipment"],
                "bank": _bank_summary(plan["bank"]),
                "physical": plan["physical"],
                "box": _box_summary(plan["box"]),
                "ff": plan["ff"],
                "active_version": plan["active_version"],
            },
            "after": {
                "shipment": after["shipment"],
                "bank": _bank_summary(after["bank"]),
                "physical": after["physical"],
                "box": _box_summary(after["box"]),
                "ff": after["ff"],
                "active_version": after["active_version"],
            },
            "performance": {
                "elapsed_ms": round(
                    (
                        datetime.now(timezone.utc) - started
                    ).total_seconds()
                    * 1000,
                    3,
                ),
                "copy_bytes": 0,
                "full_database_copy": False,
                "finance_raw_rows_read": 0,
                "sqlite_lock_wait_ms": round(
                    audit_sqlite_lock_wait_ms, 3
                ),
            },
            }
            if after["would_change"]:
                raise ValueError(
                    "unified recovery post-apply readback is not a no-op"
                )
            _save_audit(runtime, report)
    except Exception as exc:
        _mark_audit_failed(runtime, plan, steps, exc)
        raise
    return {
        **report,
        "mode": "apply",
        "applied": True,
        "idempotent": False,
        "second_run": {
            "would_change": False,
            "idempotent": True,
        },
    }


def _bank_plan(
    conn: sqlite3.Connection,
    *,
    runtime: RegistryUploadDbBackedRuntime,
    args: argparse.Namespace,
    shipment: Mapping[str, Any],
) -> dict[str, Any]:
    document = conn.execute(
        """
        SELECT * FROM sheet_vitrina_v1_supplier_financial_documents
        WHERE supplier_order_id=? AND document_id=?
        """,
        (args.shipment_id, args.statement_document_id),
    ).fetchone()
    if document is None:
        raise ValueError("target bank statement document was not found")
    statement = _loads(document["normalized_parse_json"], {})
    financial_documents = SupplierFinancialDocumentsBlock(runtime=runtime)
    payment_documents = (
        financial_documents._supplier_order_payment_documents(
            str(args.shipment_id)
        )
    )
    expense_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_supplier_financial_expense_lines
            WHERE supplier_order_id=? ORDER BY line_id
            """,
            (args.shipment_id,),
        )
    ]
    existing_ids: set[str] = set()
    existing_index: dict[str, set[str]] = {}
    for raw in expense_rows:
        evidence = _loads(raw.get("raw_json"), {})
        row = dict(evidence.get("row") or {})
        operation_id = str(
            evidence.get("semantic_operation_id")
            or row.get("semantic_operation_id")
            or ""
        )
        if operation_id:
            existing_ids.add(operation_id)
        identity = _statement_reference_identity(row)
        if operation_id and identity:
            existing_index.setdefault(identity, set()).add(operation_id)
    preview = build_bank_fee_statement_import_preview(
        statement,
        shipment=shipment,
        payment_documents=payment_documents,
        existing_operation_ids=existing_ids,
        existing_operation_index=existing_index,
    )
    expected_amounts = sorted(
        _money(value) for value in args.commission_amount
    )
    matching_groups = [
        dict(group)
        for group in preview.get("logical_fee_groups") or []
        if sorted(
            _money(row.get("amount"))
            for row in group.get("atomic_rows") or []
        )
        == expected_amounts
    ]
    if len(matching_groups) != 1:
        raise ValueError(
            "exact logical bank commission group is not uniquely proven"
        )
    group = matching_groups[0]
    if int(args.expected_logical_fee_count or 0) and len(
        preview.get("logical_fee_groups") or []
    ) != int(args.expected_logical_fee_count):
        raise ValueError("logical bank commission count changed")
    rows = list(preview.get("matched_fee_rows") or [])
    if int(args.expected_atomic_fee_count or 0) and len(rows) != int(
        args.expected_atomic_fee_count
    ):
        raise ValueError("atomic bank commission count changed")
    total = sum((Decimal(_money(row.get("amount"))) for row in rows), ZERO)
    if args.expected_bank_total and _money(total) != _money(
        args.expected_bank_total
    ):
        raise ValueError("bank commission total changed")
    target_revision = financial_documents._bank_fee_preview_revision(
        str(args.shipment_id),
        exclude_document_id=str(args.statement_document_id),
        statement_file_sha256=str(document["file_sha256"] or ""),
    )
    new_atomic = [
        str(row.get("semantic_operation_id") or "")
        for row in group.get("atomic_rows") or []
        if str(row.get("operation_status") or "") == "new"
    ]
    blocked = [
        str(row.get("semantic_operation_id") or "")
        for row in group.get("atomic_rows") or []
        if str(row.get("operation_status") or "")
        not in {"new", "already_imported"}
    ]
    if blocked:
        raise ValueError(
            "approved logical bank commission is not safely importable"
        )
    return {
        "document_id": str(args.statement_document_id),
        "document_revision": _fingerprint(
            {
                "updated_at": document["updated_at"],
                "file_sha256": document["file_sha256"],
                "normalized_parse": statement,
            }
        ),
        "target_revision": target_revision,
        "logical_fee_id": str(group["logical_fee_id"]),
        "reference": str(group.get("reference") or ""),
        "amount": _money(group.get("amount")),
        "atomic_operation_ids": [
            str(value) for value in group.get("atomic_operation_ids") or []
        ],
        "atomic_business_dates": sorted(
            {
                str(row.get("operation_date") or "")[:10]
                for row in group.get("atomic_rows") or []
                if str(row.get("operation_date") or "")
            }
        ),
        "new_atomic_operation_ids": new_atomic,
        "already_imported_operation_ids": sorted(existing_ids),
        "logical_fee_count": len(preview.get("logical_fee_groups") or []),
        "atomic_fee_count": len(rows),
        "total_rub": _money(total),
        "fresh_statement_import": preview,
        "would_change": bool(new_atomic),
    }


def _physical_plan(
    conn: sqlite3.Connection,
    *,
    args: argparse.Namespace,
    tables: set[str],
) -> dict[str, Any]:
    selected_ids = sorted({str(value) for value in args.supply_id})
    supplies = []
    for supply_id in selected_ids:
        row = conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=?
            """,
            (supply_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"selected WB supply was not found: {supply_id}")
        goods = _loads(row["raw_goods_json"], [])
        composition = _goods_composition(goods, "quantity")
        if not composition:
            raise ValueError(f"selected WB supply has no exact goods: {supply_id}")
        debit = conn.execute(
            """
            SELECT operation_id,total_quantity_delta,created_at
            FROM sheet_vitrina_v1_ff_stock_operations
            WHERE operation_type='auto_writeoff'
              AND source_type='wb_supply' AND source_object_id=?
            ORDER BY created_at,operation_id
            """,
            (supply_id,),
        ).fetchall()
        if len(debit) > 1:
            raise ValueError(f"duplicate physical debits for supply {supply_id}")
        debit_composition: dict[int, Decimal] = {}
        if debit:
            debit_composition = {
                int(raw["nm_id"]): -Decimal(str(raw["quantity"]))
                for raw in conn.execute(
                    """
                    SELECT nm_id,SUM(quantity_delta) quantity
                    FROM sheet_vitrina_v1_ff_stock_operation_lines
                    WHERE operation_id=?
                    GROUP BY nm_id
                    """,
                    (str(debit[0]["operation_id"]),),
                )
            }
            expected_debit = {
                nm_id: Decimal(quantity)
                for nm_id, quantity in composition.items()
            }
            if (
                debit_composition != expected_debit
                or Decimal(str(debit[0]["total_quantity_delta"]))
                != -sum(expected_debit.values(), ZERO)
            ):
                raise ValueError(
                    f"physical debit composition changed for supply {supply_id}"
                )
        reservation = {
            int(raw["nm_id"]): Decimal(str(raw["quantity"]))
            for raw in conn.execute(
                """
                SELECT line.nm_id,SUM(line.quantity_delta) quantity
                FROM sheet_vitrina_v1_ff_stock_reservation_lines line
                JOIN sheet_vitrina_v1_ff_stock_reservation_operations operation
                  ON operation.operation_id=line.operation_id
                WHERE operation.supply_id=?
                GROUP BY line.nm_id HAVING SUM(line.quantity_delta)>0
                """,
                (supply_id,),
            )
        }
        if not debit and reservation != {
            nm_id: Decimal(quantity)
            for nm_id, quantity in composition.items()
        }:
            raise ValueError(
                f"physical reserve identity changed for supply {supply_id}"
            )
        if debit and reservation:
            raise ValueError(
                f"fulfilled physical debit still has a reserve for supply {supply_id}"
            )
        supplies.append(
            {
                "supply_id": supply_id,
                "status_id": int(row["status_id"]),
                "source_revision": "sha256:"
                + str(row["raw_goods_hash"] or ""),
                "composition": {
                    str(key): value for key, value in composition.items()
                },
                "quantity": sum(composition.values()),
                "reservation_quantity": _money(sum(reservation.values(), ZERO)),
                "already_debited": bool(debit),
                "debit_operation_id": (
                    str(debit[0]["operation_id"]) if debit else ""
                ),
                "debit_composition": {
                    str(key): _money(value)
                    for key, value in sorted(debit_composition.items())
                },
            }
        )
    return {
        "supplies": supplies,
        "would_change": any(not item["already_debited"] for item in supplies),
    }


def _box_plan(
    conn: sqlite3.Connection,
    *,
    args: argparse.Namespace,
    tables: set[str],
) -> dict[str, Any]:
    supply_id = str(args.box_supply_id or "")
    if not supply_id:
        return {"would_change": False}
    if int(args.factory_box_size or 0) <= 0:
        raise ValueError("positive factory box size is required")
    row = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=?",
        (supply_id,),
    ).fetchone()
    if row is None:
        raise ValueError("box-correction WB supply was not found")
    goods = _loads(row["raw_goods_json"], [])
    declared = _goods_composition(goods, "quantity")
    accepted = _goods_composition(goods, "acceptedQuantity")
    columns = {
        str(item["name"])
        for item in conn.execute(
            "PRAGMA table_info(sheet_vitrina_v1_nomenclature_items)"
        )
    }
    if "factory_box_size" not in columns:
        raise ValueError("factory box size schema is not deployed")
    sizes = {}
    updates = []
    for nm_id in sorted(set(declared) | set(accepted)):
        item = conn.execute(
            """
            SELECT factory_box_size
            FROM sheet_vitrina_v1_nomenclature_items WHERE nm_id=?
            """,
            (nm_id,),
        ).fetchone()
        if item is None:
            raise ValueError(f"nomenclature identity missing for nmID {nm_id}")
        value = int(item["factory_box_size"] or 0)
        if value and value != int(args.factory_box_size):
            raise ValueError(f"factory box size drift for nmID {nm_id}")
        if not value:
            updates.append(nm_id)
            value = int(args.factory_box_size)
        sizes[nm_id] = value
    solution = solve_unique_box_correction(
        declared=declared,
        accepted=accepted,
        factory_box_sizes=sizes,
        final_acceptance=int(row["status_id"]) in {4, 5, 6},
    )
    if str(solution.get("status") or "") != "unique":
        raise ValueError(
            "fresh official WB composition does not prove one box correction: "
            + str(solution.get("reason") or "")
        )
    existing = None
    if BOX_CORRECTION_TABLE in tables:
        existing = conn.execute(
            f"""
            SELECT * FROM {BOX_CORRECTION_TABLE}
            WHERE supply_id=? AND status='applied'
            ORDER BY applied_at DESC LIMIT 1
            """,
            (supply_id,),
        ).fetchone()
    adjustment = {
        str(nm_id): int(declared.get(nm_id, 0))
        - int(solution["corrected"].get(nm_id, 0))
        for nm_id in sorted(set(declared) | set(solution["corrected"]))
        if int(declared.get(nm_id, 0))
        != int(solution["corrected"].get(nm_id, 0))
    }
    if existing is not None and str(existing["plan_fingerprint"]) != str(
        solution["plan_fingerprint"]
    ):
        raise ValueError("another box correction is already active")
    return {
        "supply_id": supply_id,
        "status_id": int(row["status_id"]),
        "source_revision": "sha256:" + str(row["raw_goods_hash"] or ""),
        "solution": solution,
        "gross_shortage": solution["gross_shortage"],
        "gross_surplus": solution["gross_surplus"],
        "corrected": solution["corrected"],
        "physical_adjustment": adjustment,
        "box_size_updates": updates if existing is None else [],
        "already_applied": existing is not None,
        "would_change": existing is None,
    }


def _shipment(
    conn: sqlite3.Connection,
    shipment_id: str,
) -> dict[str, Any]:
    header = conn.execute(
        """
        SELECT * FROM sheet_vitrina_v1_supplier_shipments
        WHERE shipment_id=?
        """,
        (shipment_id,),
    ).fetchone()
    if header is None:
        raise ValueError("supplier shipment was not found")
    lines = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines
            WHERE shipment_id=? ORDER BY sort_order,line_id
            """,
            (shipment_id,),
        )
    ]
    return {"header": dict(header), "lines": lines}


def _active_version(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT active.version_id,active.updated_at,version.plan_fingerprint,
               version.version_kind,version.effective_at
        FROM sheet_vitrina_v1_warehouse_functional_active active
        JOIN sheet_vitrina_v1_warehouse_functional_versions version
          ON version.version_id=active.version_id
        WHERE active.slot=1
        """
    ).fetchone()
    return dict(row) if row else {}


def _target_queue(
    conn: sqlite3.Connection,
    shipment_id: str,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT queue_id,stable_source_id,source_revision,effective_date,
                   affected_nm_ids_json,status,requested_at
            FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
            WHERE stable_source_id LIKE ?
            ORDER BY requested_at,queue_id
            """,
            (f"%{shipment_id}%",),
        )
    ]


def _ff_balance(conn: sqlite3.Connection) -> dict[int, Decimal]:
    return {
        int(row["nm_id"]): Decimal(str(row["quantity"]))
        for row in conn.execute(
            """
            SELECT nm_id,SUM(quantity_delta) quantity
            FROM sheet_vitrina_v1_ff_stock_operation_lines
            GROUP BY nm_id
            """
        )
    }


def _goods_composition(
    goods: Any,
    quantity_key: str,
) -> dict[int, int]:
    rows = goods
    if isinstance(rows, Mapping):
        rows = rows.get("goods") or rows.get("items") or []
    result: dict[int, int] = {}
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, Mapping):
            continue
        nm_id = int(
            item.get("nmID")
            or item.get("nmId")
            or item.get("nm_id")
            or 0
        )
        quantity = int(item.get(quantity_key) or 0)
        if nm_id > 0:
            result[nm_id] = result.get(nm_id, 0) + quantity
    return result


def _record_supply_id(record: Mapping[str, Any]) -> str:
    normalized = dict(record.get("normalized") or record)
    return str(
        normalized.get("supply_id")
        or normalized.get("wb_supply_id")
        or normalized.get("preorder_id")
        or record.get("supply_id")
        or ""
    )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro",
        uri=True,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise ValueError("unified recovery could not enable query_only")
    return conn


@contextmanager
def _audit_write_connection(
    runtime: RegistryUploadDbBackedRuntime,
) -> Iterator[tuple[sqlite3.Connection, float]]:
    conn = sqlite3.connect(
        runtime.db_path,
        timeout=AUDIT_SQLITE_LOCK_WAIT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={AUDIT_SQLITE_LOCK_WAIT_MS}")
    started = time.monotonic()
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).casefold():
                raise RuntimeError(
                    "unified_recovery_sqlite_write_wait_expired"
                ) from exc
            raise
        wait_ms = (time.monotonic() - started) * 1000
        try:
            yield conn, wait_ms
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _ensure_audit_schema(
    runtime: RegistryUploadDbBackedRuntime,
) -> float:
    runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
    with _audit_write_connection(runtime) as (conn, wait_ms):
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {AUDIT_TABLE}(
                plan_fingerprint TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                applied_at TEXT,
                plan_json TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                report_json TEXT NOT NULL,
                error TEXT
            )
            """
        )
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({AUDIT_TABLE})")
        }
        additions = {
            "status": "TEXT NOT NULL DEFAULT 'complete'",
            "started_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
            "plan_json": "TEXT NOT NULL DEFAULT '{}'",
            "steps_json": "TEXT NOT NULL DEFAULT '{}'",
            "error": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE {AUDIT_TABLE} ADD COLUMN {name} {declaration}"
                )
    return wait_ms


def _load_audit_record(
    runtime: RegistryUploadDbBackedRuntime,
    fingerprint: str,
) -> dict[str, Any] | None:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            """
            SELECT 1 FROM sqlite_master WHERE type='table' AND name=?
            """,
            (AUDIT_TABLE,),
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(
            f"SELECT * FROM {AUDIT_TABLE} WHERE plan_fingerprint=?",
            (fingerprint,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    return {
        "plan_fingerprint": str(item.get("plan_fingerprint") or ""),
        "status": str(item.get("status") or ""),
        "started_at": str(item.get("started_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
        "applied_at": str(item.get("applied_at") or ""),
        "plan": _loads(item.get("plan_json"), {}),
        "steps": _loads(item.get("steps_json"), {}),
        "report": _loads(item.get("report_json"), {}),
        "error": str(item.get("error") or ""),
    }


def _start_audit(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
) -> float:
    now = _now()
    with _audit_write_connection(runtime) as (conn, wait_ms):
        conn.execute(
            f"""
            INSERT INTO {AUDIT_TABLE}(
                plan_fingerprint,status,started_at,updated_at,applied_at,
                plan_json,steps_json,report_json,error
            ) VALUES(?,'running',?,?,NULL,?,'{{}}','{{}}',NULL)
            ON CONFLICT(plan_fingerprint) DO UPDATE SET
                status=CASE
                    WHEN {AUDIT_TABLE}.status='complete' THEN 'complete'
                    ELSE 'running'
                END,
                updated_at=excluded.updated_at,
                error=CASE
                    WHEN {AUDIT_TABLE}.status='complete'
                    THEN {AUDIT_TABLE}.error
                    ELSE NULL
                END
            """,
            (
                str(plan["fingerprint"]),
                now,
                now,
                _json_value(plan),
            ),
        )
    return wait_ms


def _checkpoint_audit(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    steps: Mapping[str, Any],
) -> float:
    with _audit_write_connection(runtime) as (conn, wait_ms):
        updated = conn.execute(
            f"""
            UPDATE {AUDIT_TABLE}
            SET status='running',updated_at=?,steps_json=?,error=NULL
            WHERE plan_fingerprint=? AND status IN ('running','failed')
            """,
            (
                _now(),
                _json_value(steps),
                str(plan["fingerprint"]),
            ),
        )
        if int(updated.rowcount or 0) != 1:
            raise ValueError("unified recovery audit journal changed")
    return wait_ms


def _mark_audit_failed(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    steps: Mapping[str, Any],
    exc: Exception,
) -> float:
    wait_ms = _ensure_audit_schema(runtime)
    with _audit_write_connection(runtime) as (conn, write_wait_ms):
        conn.execute(
            f"""
            UPDATE {AUDIT_TABLE}
            SET status='failed',updated_at=?,steps_json=?,error=?
            WHERE plan_fingerprint=? AND status<>'complete'
            """,
            (
                _now(),
                _json_value(steps),
                str(exc).replace("\n", " ")[:1000],
                str(plan["fingerprint"]),
            ),
        )
    return wait_ms + write_wait_ms


def _save_audit(
    runtime: RegistryUploadDbBackedRuntime,
    report: Mapping[str, Any],
) -> float:
    with _audit_write_connection(runtime) as (conn, wait_ms):
        updated = conn.execute(
            f"""
            UPDATE {AUDIT_TABLE}
            SET status='complete',updated_at=?,applied_at=?,
                steps_json=?,report_json=?,error=NULL
            WHERE plan_fingerprint=? AND status='running'
            """,
            (
                _now(),
                str(report["applied_at"]),
                _json_value(report.get("steps") or {}),
                _json_value(report),
                str(report["plan_fingerprint"]),
            ),
        )
        if int(updated.rowcount or 0) != 1:
            raise ValueError("unified recovery audit could not complete")
    return wait_ms


def _validate_resume_invariants(
    original: Mapping[str, Any],
    current: Mapping[str, Any],
) -> None:
    if dict(original.get("scope") or {}) != dict(current.get("scope") or {}):
        raise ValueError("unified recovery scope changed during resume")
    original_shipment = dict(original.get("shipment") or {})
    current_shipment = dict(current.get("shipment") or {})
    if (
        str(original_shipment.get("planned_date") or "")
        != str(current_shipment.get("planned_date") or "")
        or str(current_shipment.get("actual_date_before") or "")
        not in {
            str(original_shipment.get("actual_date_before") or ""),
            str(original_shipment.get("actual_date_after") or ""),
        }
    ):
        raise ValueError("unified recovery shipment identity changed during resume")
    original_supplies = {
        str(item.get("supply_id") or ""): str(
            item.get("source_revision") or ""
        )
        for item in (original.get("physical") or {}).get("supplies") or []
    }
    current_supplies = {
        str(item.get("supply_id") or ""): str(
            item.get("source_revision") or ""
        )
        for item in (current.get("physical") or {}).get("supplies") or []
    }
    if original_supplies != current_supplies:
        raise ValueError("unified recovery WB supply revisions changed during resume")
    if str((original.get("box") or {}).get("source_revision") or "") != str(
        (current.get("box") or {}).get("source_revision") or ""
    ):
        raise ValueError("unified recovery box evidence changed during resume")


def _json_value(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _money(value: Any) -> str:
    return format(Decimal(str(value or 0)).quantize(Decimal("0.01")), "f")


def _decimal_map(values: Mapping[int, Decimal]) -> dict[str, str]:
    return {
        str(key): format(value.normalize(), "f")
        for key, value in sorted(values.items())
    }


def _bank_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "logical_fee_count",
            "atomic_fee_count",
            "total_rub",
            "amount",
            "reference",
            "would_change",
        )
    }


def _box_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "supply_id",
            "gross_shortage",
            "gross_surplus",
            "corrected",
            "physical_adjustment",
            "already_applied",
            "would_change",
        )
    }


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


if __name__ == "__main__":
    raise SystemExit(main())
