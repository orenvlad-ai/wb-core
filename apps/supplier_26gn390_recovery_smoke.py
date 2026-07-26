#!/usr/bin/env python3
"""Smoke checks for the bounded 26GN390 recovery runner."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch


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
        stale_side_effects = {
            "ff_receipt_count": 1,
            "ff_cost_layer_count": 1,
            "ff_cost_layers": [{"layer_id": "stale", "is_current": 1}],
            "current_ff_cost_layer_capital_rub": "10177161.12",
            "active_financial_capital_event_count": 0,
            "archived_financial_capital_event_count": 1,
        }
        candidate = {
            "allocation": {
                "eligible_component_count": 9,
                "allocated_component_count": 9,
                "eligible_amount_rub": "9102131.12",
                "allocated_amount_rub": "9102131.12",
                "unallocated_amount_rub": "0.00",
            },
            "functional_publication": {
                "plan_fingerprint": "sha256:candidate"
            },
        }
        with (
            patch(
                "apps.supplier_26gn390_recovery._side_effects",
                return_value=stale_side_effects,
            ),
            patch(
                "apps.supplier_26gn390_recovery._candidate_recovery_projection",
                return_value=candidate,
            ),
            patch(
                "apps.supplier_26gn390_recovery._supplier_functional_fingerprint_projection",
                return_value={
                    "active_version_id": "whfv-stale",
                    "active_source_fingerprint": "sha256:old-source",
                    "active_calculation_fingerprint": "sha256:old-calculation",
                    "current_source_fingerprint": "sha256:new-source",
                    "current_calculation_fingerprint": "sha256:new-calculation",
                    "matches_active_version": False,
                },
            ),
        ):
            plan = build_plan(runtime.db_path)
        _assert(plan["would_change"], "dry-run detects stale capital chain/layer")
        _assert(plan["expected_affected_rows"] == 3, "exact bounded action count")
        _assert(
            plan["candidate"]["allocation"]["eligible_component_count"] == 9,
            "candidate binds exact 9/9 allocation",
        )
        before = runtime.db_path.read_bytes()
        try:
            apply_plan(
                runtime,
                plan,
                backup_root=runtime.runtime_dir / "backups",
            )
        except ValueError as exc:
            _assert("disabled" in str(exc).lower(), "legacy apply is explicitly disabled")
        else:
            raise AssertionError("legacy apply entrypoint must fail closed")
        _assert(runtime.db_path.read_bytes() == before, "disabled apply changes no data")
        _assert(
            not list((runtime.runtime_dir / "backups").glob("*")),
            "disabled apply creates no recovery artifact",
        )
    print("supplier_26gn390_recovery_smoke: diagnostic-only apply disabled")
    return 0


def _seed(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": SHIPMENT_ID,
            "created_at": "2026-07-24T08:00:00Z",
            "updated_at": "2026-07-24T08:00:00Z",
            "shipment_date": "2026-06-15",
            "actual_shipment_date": "2026-06-25",
            "actual_ff_acceptance_date": "2026-07-21",
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
    _save_document(runtime, ACTIVE_DOCUMENT_ID, "parsed")
    _save_document(runtime, ARCHIVED_DOCUMENT_ID, "excluded")
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
