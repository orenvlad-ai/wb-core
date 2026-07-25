#!/usr/bin/env python3
"""Legacy read-only diagnostic for the superseded 26GN527 recovery plan.

Production apply moved to ``warehouse_cost_unified_recovery.py`` so bank fees,
physical movements, factual dates and their one targeted replay share one exact
manifest.  Keeping an independent apply here would reintroduce a full-database
backup and multiple cost replays.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.recovery_file_utils import file_sha256  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.supplier_financial_documents import (  # noqa: E402
    SupplierFinancialDocumentsBlock,
    build_bank_fee_statement_import_preview,
    parse_financial_document_pdf,
)


SHIPMENT_ID = "sup_adc29a3cba934403bca4842c2add8b7d"
INVOICE_NO = "26GN527"
SOURCE_DOCUMENT_ID = "fdoc_3209b13d72264737b113eca66c8376ab"
SOURCE_SHA256 = "132901c6faaa83901ef445787b3b6f4bb4478f79ac2aa4b2a7dd95ae40c1569d"
EXPECTED_DEFAULT_AMOUNTS = ["4788.83", "948.60", "13668.11"]
EXPECTED_DEFAULT_TOTAL = "19405.54"
EXPECTED_REVIEW_AMOUNTS = ["20000.00", "58113.66"]
EXPECTED_ALL_TOTAL = "97519.20"
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    args = parser.parse_args(argv)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    plan = build_plan(runtime)
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    raise ValueError(
        "legacy 26GN527 apply is disabled; use "
        "apps/warehouse_cost_unified_recovery.py with its exact dry-run fingerprint"
    )


def build_plan(runtime: RegistryUploadDbBackedRuntime) -> dict[str, Any]:
    source_runtime_dir = runtime.runtime_dir.resolve()
    with TemporaryDirectory(prefix="supplier-26gn527-readonly-") as temp_dir:
        snapshot_runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(temp_dir) / "runtime"
        )
        snapshot_runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        _readonly_target_snapshot(runtime.db_path, snapshot_runtime.db_path)
        return _build_plan_from_snapshot(
            snapshot_runtime,
            source_runtime_dir=source_runtime_dir,
        )


def _build_plan_from_snapshot(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    source_runtime_dir: Path,
) -> dict[str, Any]:
    shipment = runtime.load_supplier_shipment(SHIPMENT_ID) or {}
    header = dict(shipment.get("header") or {})
    if str(header.get("invoice_no") or "") != INVOICE_NO:
        raise ValueError("26GN527 shipment identity mismatch")
    source = runtime.load_supplier_financial_document(
        supplier_order_id=SHIPMENT_ID,
        document_id=SOURCE_DOCUMENT_ID,
    )
    if source is None:
        raise ValueError("diagnosed source statement is missing")
    if str(source.get("file_sha256") or "") != SOURCE_SHA256:
        raise ValueError("diagnosed source statement SHA changed")
    if str(source.get("parse_status") or "") != "excluded":
        raise ValueError("diagnosed misclassified source must remain archived")
    source_path = _resolve_source_file_path(source_runtime_dir, source)
    if not source_path.is_file() or file_sha256(source_path) != SOURCE_SHA256:
        raise ValueError("diagnosed source file is missing or changed")

    block = SupplierFinancialDocumentsBlock(runtime=runtime)
    parsed = parse_financial_document_pdf(
        source_path.read_bytes(),
        filename=str(source.get("original_filename") or source_path.name),
    )
    normalized = dict(parsed.get("normalized_parse") or {})
    preview = build_bank_fee_statement_import_preview(
        normalized,
        shipment=shipment,
        payment_documents=block._supplier_order_payment_documents(SHIPMENT_ID),  # noqa: SLF001
        existing_operation_ids=block._existing_bank_fee_operation_ids(SHIPMENT_ID),  # noqa: SLF001
        existing_operation_index=block._existing_bank_fee_operation_index(  # noqa: SLF001
            SHIPMENT_ID
        ),
    )
    rows = [dict(item) for item in preview.get("matched_fee_rows") or []]
    default_rows = [
        item
        for item in rows
        if str(item.get("operation_status") or "") == "new"
        and bool(item.get("selected_by_default"))
    ]
    review_rows = [
        item
        for item in rows
        if str(item.get("operation_status") or "") == "needs_review"
    ]
    all_relevant = [
        item
        for item in rows
        if _money(item.get("amount"))
        in set(EXPECTED_DEFAULT_AMOUNTS + EXPECTED_REVIEW_AMOUNTS)
    ]
    if (
        len(all_relevant) != 5
        or len(
            {
                str(item.get("semantic_operation_id") or "")
                for item in all_relevant
            }
        )
        != 5
    ):
        raise ValueError(
            "26GN527 preview must expose five distinct diagnosed operations"
        )
    if any(
        not bool(item.get("import_allowed", True))
        or str(item.get("confidence") or "") not in {"strong", "probable"}
        for item in default_rows
    ):
        raise ValueError(
            "default 26GN527 commissions are not safely importable"
        )
    if any(
        bool(item.get("selected_by_default"))
        for item in review_rows
    ):
        raise ValueError(
            "ambiguous 26GN527 operations must remain unchecked"
        )
    default_amounts = sorted(_money(item.get("amount")) for item in default_rows)
    review_amounts = sorted(_money(item.get("amount")) for item in review_rows)
    if review_amounts != sorted(EXPECTED_REVIEW_AMOUNTS):
        raise ValueError(f"review commission rows differ from approved target: {review_amounts}")
    if _money(
        sum(
            (_decimal(item.get("amount")) for item in all_relevant),
            _decimal(0),
        )
    ) != EXPECTED_ALL_TOTAL:
        raise ValueError("26GN527 matched fee total differs from 97,519.20 RUB")
    sections = list(normalized.get("account_sections") or [])
    classification = dict(
        dict(parsed.get("raw_parse") or {}).get("classification") or {}
    )
    if (
        str(classification.get("document_type") or "")
        != "bank_fee_statement"
        or not list(classification.get("reasons") or [])
    ):
        raise ValueError(
            "26GN527 source is not structurally classified as a bank statement"
        )
    if (
        len(sections) != 2
        or {str(item.get("account_currency") or "") for item in sections}
        != {"CNY", "RUB"}
        or len(
            {
                str(item.get("account_number") or "")
                for item in sections
                if str(item.get("account_number") or "")
            }
        )
        != 2
    ):
        raise ValueError("26GN527 statement account sections are not exact CNY/RUB")

    imported = _imported_rows(runtime)
    imported_amounts = sorted(
        _money(item.get("amount"))
        for item in imported
        if str(item.get("semantic_operation_id") or "")
        in {
            str(row.get("semantic_operation_id") or "") for row in all_relevant
        }
    )
    if any(amount in EXPECTED_REVIEW_AMOUNTS for amount in imported_amounts):
        raise ValueError("ambiguous 20,000/58,113.66 operation was already imported")
    confirmed_default_amounts = sorted(
        default_amounts
        + [
            amount
            for amount in imported_amounts
            if amount in EXPECTED_DEFAULT_AMOUNTS
        ]
    )
    if confirmed_default_amounts != sorted(EXPECTED_DEFAULT_AMOUNTS):
        raise ValueError(
            "new plus already-imported unambiguous commissions differ from "
            f"approved target: {confirmed_default_amounts}"
        )
    selected_ids = sorted(
        str(item.get("semantic_operation_id") or "") for item in default_rows
    )
    plan_material = {
        "contract_name": "supplier_26gn527_vtb_recovery_plan_v1",
        "scope": {
            "shipment_id": SHIPMENT_ID,
            "invoice_no": INVOICE_NO,
            "source_document_id": SOURCE_DOCUMENT_ID,
            "source_sha256": SOURCE_SHA256,
        },
        "classification": classification,
        "account_sections": [
            {
                key: item.get(key)
                for key in (
                    "section_id",
                    "account_number",
                    "account_currency",
                    "period_start",
                    "period_end",
                )
            }
            for item in normalized.get("account_sections") or []
        ],
        "selected_operation_ids": selected_ids,
        "selected_rows": [_row_projection(item) for item in default_rows],
        "review_rows": [_row_projection(item) for item in review_rows],
        "expected_default_total_rub": EXPECTED_DEFAULT_TOTAL,
        "expected_all_total_rub": EXPECTED_ALL_TOTAL,
        "already_imported_amounts": imported_amounts,
        "source_digest": _source_digest(runtime),
    }
    return {
        **plan_material,
        "mode": "dry_run",
        "would_change": bool(selected_ids),
        "fingerprint": "sha256:" + _hash(plan_material),
    }


def _imported_rows(runtime: RegistryUploadDbBackedRuntime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in runtime.list_supplier_financial_expense_lines(SHIPMENT_ID):
        raw = dict(line.get("raw") or {})
        source_row = dict(raw.get("row") or {})
        semantic_id = str(
            raw.get("semantic_operation_id")
            or source_row.get("semantic_operation_id")
            or ""
        )
        if semantic_id:
            rows.append(
                {
                    "semantic_operation_id": semantic_id,
                    "amount": line.get("amount"),
                }
            )
    return rows


def _row_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "semantic_operation_id",
            "operation_date",
            "account_number",
            "account_currency",
            "bank_document_number",
            "currency",
            "amount",
            "fee_category",
            "confidence",
            "matched_anchor_operation_number",
            "operation_status",
            "selected_by_default",
            "review_warning",
            "payment_purpose",
        )
    }


def _source_digest(runtime: RegistryUploadDbBackedRuntime) -> str:
    with sqlite3.connect(f"file:{runtime.db_path.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT document_id,document_type,parse_status,file_sha256,updated_at
                FROM sheet_vitrina_v1_supplier_financial_documents
                WHERE supplier_order_id=? ORDER BY document_id
                """,
                (SHIPMENT_ID,),
            ).fetchall()
        ]
    return "sha256:" + _hash(rows)


def _resolve_source_file_path(
    source_runtime_dir: Path,
    source: Mapping[str, Any],
) -> Path:
    runtime_root = Path(source_runtime_dir).resolve()
    stored = Path(str(source.get("stored_file_path") or "").strip())
    if not str(stored):
        raise ValueError("diagnosed source file path is missing")
    resolved = (
        stored.resolve()
        if stored.is_absolute()
        else (runtime_root / stored).resolve()
    )
    if resolved != runtime_root and runtime_root not in resolved.parents:
        raise ValueError("diagnosed source file escapes canonical runtime")
    return resolved


def _readonly_target_snapshot(source: Path, target: Path) -> None:
    """Copy only the bounded supplier-order rows needed by the recovery plan."""

    if target.exists():
        raise ValueError(f"target snapshot already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Let the deployed runtime create its current schema in the disposable DB.
    RegistryUploadDbBackedRuntime(runtime_dir=target.parent).list_supplier_shipments()
    table_filters = (
        ("sheet_vitrina_v1_supplier_shipments", "shipment_id = ?", (SHIPMENT_ID,)),
        (
            "sheet_vitrina_v1_supplier_shipment_lines",
            "shipment_id = ?",
            (SHIPMENT_ID,),
        ),
        (
            "sheet_vitrina_v1_supplier_financial_documents",
            "supplier_order_id = ?",
            (SHIPMENT_ID,),
        ),
        (
            "sheet_vitrina_v1_supplier_financial_expense_lines",
            "supplier_order_id = ?",
            (SHIPMENT_ID,),
        ),
        (
            "sheet_vitrina_v1_cny_documents",
            "source_order_id = ? OR context_order_id = ?",
            (SHIPMENT_ID, SHIPMENT_ID),
        ),
    )
    with sqlite3.connect(
        f"file:{source.resolve()}?mode=ro", uri=True
    ) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.row_factory = sqlite3.Row
        source_conn.execute("PRAGMA query_only=ON")
        for table_name, where_sql, params in table_filters:
            source_columns = [
                str(row[1])
                for row in source_conn.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            ]
            target_columns = {
                str(row[1])
                for row in target_conn.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            }
            columns = [
                column for column in source_columns if column in target_columns
            ]
            if not columns:
                raise ValueError(
                    f"target snapshot schema is missing table {table_name}"
                )
            quoted_columns = ",".join(f'"{column}"' for column in columns)
            rows = source_conn.execute(
                f"SELECT {quoted_columns} FROM {table_name} "
                f"WHERE {where_sql}",
                params,
            ).fetchall()
            if rows:
                target_conn.executemany(
                    f"INSERT INTO {table_name}({quoted_columns}) VALUES("
                    + ",".join("?" for _ in columns)
                    + ")",
                    [tuple(row[column] for column in columns) for row in rows],
                )
        target_conn.commit()
    with sqlite3.connect(
        f"file:{target.resolve()}?mode=ro", uri=True
    ) as check:
        check.execute("PRAGMA query_only=ON")
        if str(check.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
            raise ValueError("bounded read-only SQLite snapshot integrity_check failed")


def _money(value: Any) -> str:
    from decimal import Decimal, ROUND_HALF_UP

    return str(Decimal(str(value or 0)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _decimal(value: Any):
    from decimal import Decimal

    return Decimal(str(value or 0))


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
