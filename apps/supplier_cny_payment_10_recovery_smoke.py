#!/usr/bin/env python3
"""Smoke checks for the bounded CNY payment №10 recovery runner."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.supplier_cny_payment_10_recovery import (  # noqa: E402
    AMOUNT_CNY,
    DOCUMENT_ID,
    FILE_SHA256,
    OLD_SHIPMENT_ID,
    TARGET_SHIPMENT_ID,
    apply_plan,
    build_plan,
)
from packages.application.cny_ledger import CnyLedgerBlock  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


NOW = "2026-07-24T10:00:00Z"


def main() -> int:
    with TemporaryDirectory(prefix="supplier-cny-payment-10-recovery-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_shipment(runtime, OLD_SHIPMENT_ID, "26GN582", 100001)
        _seed_shipment(runtime, TARGET_SHIPMENT_ID, "26GN583", 100002)
        block = CnyLedgerBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        block.create_opening_balance(
            {
                "operation_date": "2026-07-01",
                "cny_amount": "1000000",
                "average_rate": "12",
            }
        )
        runtime.save_cny_document(
            {
                "document_id": DOCUMENT_ID,
                "document_type": "supplier_cny_payment",
                "source": "supplier_order",
                "source_order_id": OLD_SHIPMENT_ID,
                "context_order_id": OLD_SHIPMENT_ID,
                "linked_financial_document_id": "",
                "original_filename": "mt103_10.pdf",
                "stored_file_path": "cny_documents/files/fixture/mt103_10.pdf",
                "file_content_type": "application/pdf",
                "file_sha256": FILE_SHA256,
                "natural_key": "supplier_cny_payment:sha256:" + FILE_SHA256,
                "uploaded_at": NOW,
                "created_at": NOW,
                "updated_at": NOW,
                "operation_date": "2026-07-20",
                "operation_datetime": "2026-07-20T08:53:18Z",
                "status": "posted",
                "document_number": "10",
                "currency": "CNY",
                "rub_amount": "",
                "cny_amount": AMOUNT_CNY,
                "bank_rate": "",
                "parsed_payload": {
                    "document_type": "supplier_cny_payment",
                    "document_number": "10",
                    "operation_date": "2026-07-20",
                    "operation_datetime": "2026-07-20T08:53:18Z",
                    "currency": "CNY",
                    "cny_amount": AMOUNT_CNY,
                    "payment_date_provenance": {"source": "parsed_document"},
                },
                "raw_parse": {},
                "parser_version": "smoke",
                "warnings": [],
                "errors": [],
            }
        )
        block.replay_ledger(reason="smoke_seed")
        plan = build_plan(runtime.db_path)
        _assert(plan["would_change"], "dry-run detects the old shipment binding")
        _assert(plan["expected_affected_rows"] == 1, "exact affected row count")
        with patch.object(
            Path,
            "read_bytes",
            side_effect=MemoryError("whole-file reads are forbidden"),
        ):
            result = apply_plan(
                runtime,
                plan,
                backup_root=runtime.runtime_dir / "backups",
            )
        _assert(result["applied"], "approved relink is applied")
        _assert(Path(result["backup"]["path"]).is_file(), "coherent backup exists")
        readback = result["post_apply"]["readback"]
        _assert(
            readback["source_order_id"] == TARGET_SHIPMENT_ID
            and readback["operation_shipment_id"] == TARGET_SHIPMENT_ID
            and readback["capital_shipment_id"] == TARGET_SHIPMENT_ID,
            "document, ledger and capital move as one chain",
        )
        _assert(
            readback["same_sha_document_count"] == 1
            and readback["operation_count"] == 1
            and readback["capital_layer_count"] == 1,
            "no duplicate document or derived chain is created",
        )
        second = build_plan(runtime.db_path)
        _assert(not second["would_change"], "second dry-run is a no-op")
        second_result = apply_plan(
            runtime,
            second,
            backup_root=runtime.runtime_dir / "backups",
        )
        _assert(not second_result["applied"], "second apply is idempotent")
    print("supplier_cny_payment_10_recovery_smoke: ok")
    return 0


def _seed_shipment(
    runtime: RegistryUploadDbBackedRuntime,
    shipment_id: str,
    invoice_no: str,
    nm_id: int,
) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": shipment_id,
            "created_at": NOW,
            "updated_at": NOW,
            "shipment_date": "2026-08-20",
            "invoice_no": invoice_no,
            "invoice_date": "2026-07-20",
            "currency": "CNY",
            "product_qty_total": 100,
            "product_amount_total": 500000,
            "extras_amount_total": 0,
            "invoice_amount_total": 500000,
            "declared_invoice_total": 500000,
            "match_status": "all_matched",
            "order_status": "production",
            "expenses_complete": False,
            "warnings": [],
            "errors": [],
        },
        lines=[
            {
                "line_id": shipment_id + "-line",
                "line_type": "product",
                "sort_order": 1,
                "source_no": "1",
                "barcode": "4600000000000",
                "source_model": "MODEL",
                "normalized_model": "model",
                "match_key": "model",
                "internal_nm_id": nm_id,
                "internal_name": "Model",
                "qty": 100,
                "unit_price": 5000,
                "amount": 500000,
                "currency": "CNY",
                "comment": "",
                "manual_override": False,
                "match_status": "matched",
                "raw": {},
            }
        ],
    )


def _assert(value: object, label: str) -> None:
    if not value:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
