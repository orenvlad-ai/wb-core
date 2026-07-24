#!/usr/bin/env python3
"""Bounded parse-and-confirm recovery for the 26GN527 VTB statement."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.application.supplier_shipment_factual_correction import (  # noqa: E402
    _sqlite_backup as create_verified_sqlite_backup,
    restore_verified_supplier_backup,
)
from packages.application.supplier_financial_documents import (  # noqa: E402
    SupplierFinancialDocumentsBlock,
    build_bank_fee_statement_import_preview,
    parse_financial_document_pdf,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)


SHIPMENT_ID = "sup_adc29a3cba934403bca4842c2add8b7d"
INVOICE_NO = "26GN527"
SOURCE_DOCUMENT_ID = "fdoc_3209b13d72264737b113eca66c8376ab"
SOURCE_SHA256 = "132901c6faaa83901ef445787b3b6f4bb4478f79ac2aa4b2a7dd95ae40c1569d"
EXPECTED_DEFAULT_AMOUNTS = ["4788.83", "948.60", "13668.11"]
EXPECTED_DEFAULT_TOTAL = "19405.54"
EXPECTED_REVIEW_AMOUNTS = ["20000.00", "58113.66"]
EXPECTED_ALL_TOTAL = "97519.20"
AUDIT_TABLE = "sheet_vitrina_v1_supplier_26gn527_vtb_recovery_audit"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args(argv)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    plan = build_plan(runtime)
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if str(args.fingerprint or "") != str(plan["fingerprint"]):
        raise ValueError("apply requires the exact current dry-run fingerprint")
    backup_root = (
        Path(args.backup_dir)
        if args.backup_dir
        else runtime.runtime_dir / "backups" / "supplier-26gn527-vtb-recovery"
    )
    result = apply_plan(runtime, plan, backup_root=backup_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def build_plan(runtime: RegistryUploadDbBackedRuntime) -> dict[str, Any]:
    with TemporaryDirectory(prefix="supplier-26gn527-readonly-") as temp_dir:
        snapshot_runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(temp_dir) / "runtime"
        )
        snapshot_runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        _readonly_sqlite_copy(runtime.db_path, snapshot_runtime.db_path)
        return _build_plan_from_snapshot(snapshot_runtime)


def _build_plan_from_snapshot(
    runtime: RegistryUploadDbBackedRuntime,
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
    source_path = Path(str(source.get("stored_file_path") or ""))
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


def apply_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    backup_root: Path,
) -> dict[str, Any]:
    if not plan.get("would_change"):
        return {**dict(plan), "mode": "apply", "applied": False, "idempotent": True}
    with warehouse_functional_write_lock(runtime.runtime_dir):
        return _apply_plan_locked(runtime, plan, backup_root=backup_root)


def _apply_plan_locked(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    backup_root: Path,
) -> dict[str, Any]:
    current = build_plan(runtime)
    if str(current["fingerprint"]) != str(plan["fingerprint"]):
        raise ValueError("26GN527 source changed after dry-run")
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_root / f"registry_upload.26gn527.{stamp}.sqlite3"
    backup = create_verified_sqlite_backup(runtime.db_path, backup_path)
    source = runtime.load_supplier_financial_document(
        supplier_order_id=SHIPMENT_ID,
        document_id=SOURCE_DOCUMENT_ID,
    ) or {}
    source_path = Path(str(source.get("stored_file_path") or ""))
    document_id = ""
    preview: dict[str, Any] = {}
    created_document_path: Path | None = None
    try:
        block = SupplierFinancialDocumentsBlock(runtime=runtime)
        preview = block.upload_bank_fee_statement_preview(
            SHIPMENT_ID,
            file_bytes=source_path.read_bytes(),
            uploaded_filename=str(
                source.get("original_filename") or source_path.name
            ),
            uploaded_content_type=str(
                source.get("file_content_type") or "application/pdf"
            ),
        )
        document_id = str(preview.get("document_id") or "")
        if not bool(preview.get("idempotent")):
            created_document_path = Path(str(preview.get("stored_file_path") or ""))
        applied_at = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime.runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: applied_at,
        )
        confirmed = (
            entrypoint.handle_supplier_financial_document_confirm_import_request(
                SHIPMENT_ID,
                document_id,
                selected_operation_ids=list(
                    plan.get("selected_operation_ids") or []
                ),
            )
        )
        repeated = (
            entrypoint.handle_supplier_financial_document_confirm_import_request(
                SHIPMENT_ID,
                document_id,
                selected_operation_ids=list(
                    plan.get("selected_operation_ids") or []
                ),
            )
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {AUDIT_TABLE}(
                    recovery_id TEXT PRIMARY KEY,
                    plan_fingerprint TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL,
                    backup_path TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    report_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""INSERT INTO {AUDIT_TABLE}(
                        recovery_id,plan_fingerprint,applied_at,backup_path,document_id,report_json
                    ) VALUES(?,?,?,?,?,?)""",
                (
                    "s527_"
                    + str(plan["fingerprint"]).split(":", 1)[-1][:24],
                    str(plan["fingerprint"]),
                    applied_at,
                    str(backup_path),
                    document_id,
                    json.dumps(
                        dict(plan), ensure_ascii=False, sort_keys=True
                    ),
                ),
            )
            conn.commit()
        post = build_plan(runtime)
        if post.get("would_change"):
            raise ValueError(
                "26GN527 recovery did not become an idempotent no-op"
            )
        if any(
            amount in EXPECTED_REVIEW_AMOUNTS
            for amount in post.get("already_imported_amounts") or []
        ):
            raise ValueError("ambiguous statement rows were imported")
    except Exception:
        restore_verified_supplier_backup(backup_path, runtime.db_path)
        if (
            created_document_path is not None
            and created_document_path.is_file()
            and runtime.runtime_dir.resolve()
            in created_document_path.resolve().parents
        ):
            created_document_path.unlink()
        raise
    return {
        **dict(plan),
        "mode": "apply",
        "applied": True,
        "document_id": document_id,
        "backup": backup,
        "confirmed_expense_line_count": len(confirmed.get("expense_lines") or []),
        "repeat_confirm_idempotent": bool(repeated.get("idempotent")),
        "post_apply": post,
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


def _readonly_sqlite_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(
        f"file:{source.resolve()}?mode=ro", uri=True
    ) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.execute("PRAGMA query_only=ON")
        source_conn.backup(target_conn)
        target_conn.commit()
    with sqlite3.connect(
        f"file:{target.resolve()}?mode=ro", uri=True
    ) as check:
        check.execute("PRAGMA query_only=ON")
        if str(check.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
            raise ValueError("read-only SQLite snapshot integrity_check failed")


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
