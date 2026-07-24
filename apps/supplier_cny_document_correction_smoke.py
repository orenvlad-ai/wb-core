#!/usr/bin/env python3
"""Targeted smoke-check for supplier CNY dedupe, relink and exclusion."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)


NOW = "2026-07-24T10:00:00Z"
WRONG_SHIPMENT = "supplier-cny-wrong"
TARGET_SHIPMENT = "supplier-cny-target"
PAYMENT_TEXT = """Заявление на перевод № 10
от 20 июля 2026
Исполнен 20.07.2026 в 08:53:18
Please debit our account with you): 40802156616580000008
Валюта Currency Code CNY
Сумма перевода Amount of transfer 76646,00
50 Ordering Customer
IE SAGITOV VLADISLAV RADIKOVICH
57 Банк получателя
VTBRCNSHXXX VTB BANK SHANGHAI BRANCH
59 Получатель
40807156200610034920 GUANGZHOU ZIFRIEND COMMUNICATE TECHNOLOGY CO., LTD
Назначение платежа Details of payment 70
CONTRACT FR-001/26 DD 08.06.2026
Расходы и комиссии OUR
"""


def main() -> int:
    with TemporaryDirectory(prefix="supplier-cny-correction-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_shipment(runtime, WRONG_SHIPMENT, "26GN582")
        _seed_shipment(runtime, TARGET_SHIPMENT, "26GN583")
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime.runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: NOW,
        )
        entrypoint.cny_ledger_block.pdf_text_extractor = (
            lambda _body, _filename: (
                PAYMENT_TEXT,
                {"method": "supplier_cny_correction_fixture"},
                [],
            )
        )
        entrypoint.cny_ledger_block.create_opening_balance(
            {
                "operation_date": "2026-07-01",
                "cny_amount": "1000000",
                "average_rate": "12",
            }
        )
        _assert_contract(runtime, entrypoint)
    print("supplier_cny_document_correction_smoke: ok")
    return 0


def _assert_contract(
    runtime: RegistryUploadDbBackedRuntime,
    entrypoint: RegistryUploadHttpEntrypoint,
) -> None:
    body = b"%PDF-1.4 supplier payment 10"
    preview = entrypoint.handle_supplier_financial_documents_upload_request(
        WRONG_SHIPMENT,
        body,
        uploaded_filename="mt103_10.pdf",
        uploaded_content_type="application/pdf",
        actor="operator-1",
    )
    _assert(preview["duplicate_action"] == "create", "new SHA is staged")
    _assert(
        not _supplier_payment_documents(runtime),
        "preview creates no active CNY document",
    )
    created = entrypoint.handle_supplier_financial_documents_confirm_upload_request(
        WRONG_SHIPMENT,
        {"confirmation_token": preview["confirmation_token"]},
    )
    document_id = str(created["document_id"])
    _assert(
        created["outcome"] == "created"
        and created["supplier_order_id"] == WRONG_SHIPMENT
        and created["readback_confirmed"],
        "create outcome is exact and read back",
    )
    _assert_single_chain(runtime, document_id, WRONG_SHIPMENT)

    same_preview = entrypoint.handle_supplier_financial_documents_upload_request(
        WRONG_SHIPMENT,
        body,
        uploaded_filename="mt103_10-copy.pdf",
        uploaded_content_type="application/pdf",
    )
    _assert(
        same_preview["duplicate_action"] == "already_present",
        "same SHA in the same shipment is explicit",
    )
    same_result = entrypoint.handle_supplier_financial_documents_confirm_upload_request(
        WRONG_SHIPMENT,
        {"confirmation_token": same_preview["confirmation_token"]},
    )
    _assert(
        same_result["outcome"] == "already_present"
        and same_result["document_id"] == document_id
        and len(_supplier_payment_documents(runtime)) == 1,
        "same-shipment retry is idempotent",
    )

    conflict_preview = entrypoint.handle_supplier_financial_documents_upload_request(
        TARGET_SHIPMENT,
        body,
        uploaded_filename="mt103_10.pdf",
        uploaded_content_type="application/pdf",
    )
    _assert(
        conflict_preview["duplicate_action"] == "conflict_other_shipment"
        and conflict_preview["outcome"] == "conflict_other_shipment",
        "global SHA conflict identifies the other shipment",
    )
    _rejects(
        lambda: entrypoint.handle_supplier_financial_documents_confirm_upload_request(
            TARGET_SHIPMENT,
            {"confirmation_token": conflict_preview["confirmation_token"]},
        ),
        "explicit relink",
    )
    _assert_single_chain(runtime, document_id, WRONG_SHIPMENT)
    queue_before_relink = _queue_count(runtime, document_id)
    relinked = entrypoint.handle_supplier_financial_documents_confirm_upload_request(
        TARGET_SHIPMENT,
        {
            "confirmation_token": conflict_preview["confirmation_token"],
            "allow_relink": True,
        },
    )
    _assert(
        relinked["outcome"] == "relinked"
        and relinked["old_supplier_order_id"] == WRONG_SHIPMENT
        and relinked["supplier_order_id"] == TARGET_SHIPMENT,
        "confirmed relink reports old and new bindings",
    )
    _assert_single_chain(runtime, document_id, TARGET_SHIPMENT)
    _assert(
        _queue_count(runtime, document_id) == queue_before_relink + 1,
        "relink creates one targeted recalculation request",
    )
    repeated = entrypoint.handle_supplier_financial_documents_confirm_upload_request(
        TARGET_SHIPMENT,
        {
            "confirmation_token": conflict_preview["confirmation_token"],
            "allow_relink": True,
        },
    )
    _assert(repeated.get("idempotent"), "repeat relink confirmation is idempotent")
    _assert(
        _queue_count(runtime, document_id) == queue_before_relink + 1,
        "repeat relink creates no second queue request",
    )

    delete_preview = entrypoint.handle_supplier_financial_document_delete_preview_request(
        TARGET_SHIPMENT,
        document_id,
        actor="operator-1",
    )
    _assert(
        delete_preview["linked_operation_count"] == 1,
        "delete preview exposes the linked CNY operation",
    )
    _assert_single_chain(runtime, document_id, TARGET_SHIPMENT)
    queue_before_delete = _queue_count(runtime, document_id)
    deleted = entrypoint.handle_supplier_financial_document_delete_request(
        TARGET_SHIPMENT,
        document_id,
        delete_preview["confirmation_token"],
        actor="operator-1",
    )
    _assert(
        deleted["outcome"] == "excluded" and deleted["readback_confirmed"],
        "confirmed deletion is an audited exclusion",
    )
    _assert(
        not _active_document_rows(entrypoint, TARGET_SHIPMENT)
        and len(
            entrypoint.handle_supplier_order_documents_list_request(
                TARGET_SHIPMENT
            )["archived_documents"]
        )
        == 1,
        "excluded CNY document leaves active projection and enters archive",
    )
    _assert(
        not _operations(runtime, document_id)
        and not _capital_layers(runtime, document_id),
        "excluded CNY document has no active ledger or capital dependency",
    )
    _assert(
        _queue_count(runtime, document_id) == queue_before_delete + 1,
        "exclusion creates one targeted recalculation request",
    )
    repeated_delete = entrypoint.handle_supplier_financial_document_delete_request(
        TARGET_SHIPMENT,
        document_id,
        delete_preview["confirmation_token"],
        actor="operator-1",
    )
    _assert(
        repeated_delete.get("idempotent")
        and _queue_count(runtime, document_id) == queue_before_delete + 1,
        "repeat exclusion is a no-op",
    )

    restore_preview = entrypoint.handle_supplier_financial_documents_upload_request(
        TARGET_SHIPMENT,
        body,
        uploaded_filename="mt103_10.pdf",
        uploaded_content_type="application/pdf",
    )
    _assert(
        restore_preview["duplicate_action"] == "restore",
        "excluded SHA offers restore",
    )
    restored = entrypoint.handle_supplier_financial_documents_confirm_upload_request(
        TARGET_SHIPMENT,
        {"confirmation_token": restore_preview["confirmation_token"]},
    )
    _assert(
        restored["outcome"] == "restored"
        and restored["document_id"] == document_id,
        "restore reuses the archived document",
    )
    _assert_single_chain(runtime, document_id, TARGET_SHIPMENT)

    semantic_preview = entrypoint.handle_supplier_financial_documents_upload_request(
        TARGET_SHIPMENT,
        b"%PDF-1.4 rescanned supplier payment 10",
        uploaded_filename="mt103_10-rescan.pdf",
        uploaded_content_type="application/pdf",
    )
    _assert(
        semantic_preview["duplicate_action"] == "semantic_warning",
        "same requisites with another SHA are warned",
    )
    _rejects(
        lambda: entrypoint.handle_supplier_financial_documents_confirm_upload_request(
            TARGET_SHIPMENT,
            {"confirmation_token": semantic_preview["confirmation_token"]},
        ),
        "explicit confirmation",
    )


def _seed_shipment(
    runtime: RegistryUploadDbBackedRuntime,
    shipment_id: str,
    invoice_no: str,
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
                "source_row": 1,
                "source_sheet": "Invoice",
                "barcode": "4600000000000",
                "source_model": "MODEL",
                "normalized_model": "model",
                "match_key": "model",
                "internal_nm_id": 100001 if shipment_id == WRONG_SHIPMENT else 100002,
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


def _supplier_payment_documents(
    runtime: RegistryUploadDbBackedRuntime,
) -> list[dict[str, object]]:
    return [
        item
        for item in runtime.list_cny_documents()
        if item.get("document_type") == "supplier_cny_payment"
    ]


def _operations(
    runtime: RegistryUploadDbBackedRuntime,
    document_id: str,
) -> list[dict[str, object]]:
    return [
        item
        for item in runtime.list_cny_ledger_operations()
        if item.get("source_document_id") == document_id
    ]


def _capital_layers(
    runtime: RegistryUploadDbBackedRuntime,
    document_id: str,
) -> list[sqlite3.Row]:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT *
            FROM sheet_vitrina_v1_own_capital_payment_layers
            WHERE payment_id = ?
            """,
            (document_id,),
        ).fetchall()


def _queue_count(
    runtime: RegistryUploadDbBackedRuntime,
    document_id: str,
) -> int:
    with sqlite3.connect(runtime.db_path) as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
                WHERE stable_source_id = ?
                """,
                ("cny_document:" + document_id,),
            ).fetchone()[0]
        )


def _active_document_rows(
    entrypoint: RegistryUploadHttpEntrypoint,
    shipment_id: str,
) -> list[dict[str, object]]:
    return [
        item
        for item in entrypoint.handle_supplier_order_documents_list_request(
            shipment_id
        )["documents"]
        if item.get("source") == "cny_document"
    ]


def _assert_single_chain(
    runtime: RegistryUploadDbBackedRuntime,
    document_id: str,
    shipment_id: str,
) -> None:
    documents = _supplier_payment_documents(runtime)
    operations = _operations(runtime, document_id)
    layers = _capital_layers(runtime, document_id)
    _assert(
        len(documents) == 1
        and documents[0].get("document_id") == document_id
        and documents[0].get("source_order_id") == shipment_id,
        "one CNY document has the expected binding",
    )
    _assert(
        len(operations) == 1
        and operations[0].get("source_order_id") == shipment_id,
        "one ledger operation has the expected binding",
    )
    _assert(
        len(layers) == 1 and str(layers[0]["shipment_id"]) == shipment_id,
        "one capital layer has the expected binding",
    )


def _rejects(callback, text: str) -> None:
    try:
        callback()
    except ValueError as exc:
        _assert(text in str(exc), f"expected {text!r}, got {exc!s}")
    else:
        raise AssertionError(f"expected ValueError containing {text!r}")


def _assert(value: object, label: str) -> None:
    if not value:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
