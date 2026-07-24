#!/usr/bin/env python3
"""Smoke checks for the bounded 26GN390 recovery runner."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.supplier_26gn390_recovery import (  # noqa: E402
    ACTIVE_DOCUMENT_ID,
    ARCHIVED_DOCUMENT_ID,
    EXPECTED_FILE_SHA256,
    SHIPMENT_ID,
    apply_plan,
    build_plan,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


def main() -> int:
    with TemporaryDirectory(prefix="supplier-26gn390-recovery-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed(runtime)
        plan = build_plan(runtime.db_path)
        _assert(plan["would_change"], "dry-run detects wrong active/archive statuses")
        _assert(plan["expected_affected_rows"] == 4, "exact affected row count")
        _assert(plan["expected_direct_rows"] == 2, "exact direct row count")
        result = apply_plan(
            runtime,
            plan,
            backup_root=runtime.runtime_dir / "backups",
        )
        _assert(result["applied"], "apply executes approved plan")
        _assert(Path(result["backup"]["path"]).is_file(), "coherent backup exists")
        readback = result["post_apply"]["readback"]
        _assert(readback["active_count"] == 1, "exactly one active invoice 136")
        _assert(readback["excluded_count"] == 1, "exactly one archived invoice 136")
        _assert(
            readback["actual_ff_acceptance_date"] == "2026-07-21",
            "FF acceptance date is confirmed through the server flow",
        )
        _assert(readback["ff_receipt_count"] == 1, "one FF receipt is created")
        _assert(
            readback["ff_cost_layer_count"] == 1,
            "one FF cost layer is created",
        )
        _assert(
            readback["invoice_136"][0]["expense_amount_rub"] == "1075030.00",
            "expense amount conserved",
        )
        second = build_plan(runtime.db_path)
        _assert(not second["would_change"], "second dry-run is no-op")
        second_apply = apply_plan(
            runtime,
            second,
            backup_root=runtime.runtime_dir / "backups",
        )
        _assert(not second_apply["applied"], "second apply is idempotent")
    print("supplier_26gn390_recovery_smoke: ok")
    return 0


def _seed(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": SHIPMENT_ID,
            "created_at": "2026-07-24T08:00:00Z",
            "updated_at": "2026-07-24T08:00:00Z",
            "shipment_date": "2026-06-15",
            "actual_shipment_date": "2026-06-25",
            "actual_ff_acceptance_date": "",
            "order_status": "in_transit",
            "invoice_no": "26GN390",
            "invoice_date": "2026-06-15",
            "currency": "CNY",
            "expenses_complete": True,
        },
        lines=[
            {
                "line_id": "legacy-line",
                "line_type": "product",
                "sort_order": 1,
                "source_no": "1",
                "barcode": "",
                "internal_nm_id": 123,
                "internal_name": "Legacy",
                "qty": 1,
                "unit_price": 1,
                "amount": 1,
                "currency": "CNY",
                "match_status": "matched_by_compatibility",
                "raw": {},
            }
        ],
    )
    _save_document(runtime, ACTIVE_DOCUMENT_ID, "excluded")
    _save_document(runtime, ARCHIVED_DOCUMENT_ID, "parsed")
    runtime.save_supplier_shipment(
        header={
            "shipment_id": "non_target",
            "created_at": "2026-07-24T08:00:00Z",
            "updated_at": "2026-07-24T08:00:00Z",
            "shipment_date": "2026-07-01",
            "order_status": "production",
            "invoice_no": "OTHER",
            "invoice_date": "2026-07-01",
        },
        lines=[],
    )


def _save_document(
    runtime: RegistryUploadDbBackedRuntime, document_id: str, status: str
) -> None:
    runtime.save_supplier_financial_document(
        document={
            "document_id": document_id,
            "supplier_order_id": SHIPMENT_ID,
            "document_type": "logistics_invoice",
            "original_filename": "136.pdf",
            "stored_file_path": "audit/136.pdf",
            "file_content_type": "application/pdf",
            "file_sha256": EXPECTED_FILE_SHA256,
            "uploaded_at": "2026-07-24T08:00:00Z",
            "updated_at": "2026-07-24T08:00:00Z",
            "parse_status": status,
            "vendor": "ООО ВОРЛД-ЛОГИСТИК",
            "document_number": "136",
            "document_date": "2026-07-15",
            "currency": "RUB",
            "total_amount": 1075030,
            "total_amount_rub": 1075030,
            "raw_parse": {},
            "normalized_parse": {},
            "warnings": [],
            "errors": [],
        },
        expense_lines=[
            {
                "line_id": "line_" + document_id,
                "sort_order": 1,
                "category": "delivery_cost",
                "stage": "china_to_ff",
                "description": "Логистика",
                "amount": 1075030,
                "currency": "RUB",
                "amount_rub": 1075030,
                "status": "parsed",
                "raw": {},
            }
        ],
    )


def _assert(value: object, label: str) -> None:
    if not value:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
