#!/usr/bin/env python3
"""Targeted checks for supplier date and financial confirmation flows."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.application.supplier_financial_documents import (  # noqa: E402
    StaticUsdRateProvider,
    SupplierFinancialDocumentsBlock,
)
from packages.application.warehouse_functional import FUNCTIONAL_CUTOVER_ID, ensure_warehouse_functional_schema
from packages.application.warehouse_business_projection import ensure_functional_version_business_time_schema


NOW = "2026-07-24T09:00:00Z"
SHIPMENT_ID = "legacy_confirmation"
INVOICE_TEXT = """
Счет на оплату № 136 от 15 июля 2026 г.
Поставщик (Исполнитель): ООО "ВОРЛД-ЛОГИСТИК"
Основание: ДОГОВОР ТРАНСПОРТНОЙ ЭКСПЕДИЦИИ № ORE от 04.06.2026
1 Организация экспедирования груза по маршруту г. Пограничный - г. Москва
Итого: 1 075 030,00
В том числе НДС 5%: 51 191,90
Всего к оплате: 1 075 030,00
"""


def main() -> int:
    with TemporaryDirectory(prefix="supplier-confirmation-flows-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed(runtime)
        _seed_empty_functional(runtime, NOW)
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime.runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: NOW,
        )
        _assert_date_contract(runtime, entrypoint)
        _assert_financial_contract(runtime)
        _assert_batch_financial_contract(runtime, entrypoint)
    print("supplier_confirmation_flows_smoke: ok")
    return 0


def _assert_date_contract(
    runtime: RegistryUploadDbBackedRuntime,
    entrypoint: RegistryUploadHttpEntrypoint,
) -> None:
    block = entrypoint.supplier_shipments_block
    persisted_before = deepcopy(
        (runtime.load_supplier_shipment(SHIPMENT_ID) or {}).get("lines") or []
    )
    compatibility_lines = block.get_shipment(SHIPMENT_ID)["lines"]
    block.update_shipment(
        SHIPMENT_ID,
        {
            "shipment_date": "2026-06-16",
            "lines": compatibility_lines,
        },
    )
    persisted_after = (
        runtime.load_supplier_shipment(SHIPMENT_ID) or {}
    ).get("lines") or []
    _assert(
        persisted_after == persisted_before,
        "unchanged compatibility lines remain field-for-field identical",
    )
    edited = deepcopy(compatibility_lines)
    edited[0]["qty"] = 2
    _rejects(
        lambda: block.update_shipment(SHIPMENT_ID, {"lines": edited}),
        "line edits are blocked",
    )
    direct_payload = {"actual_ff_acceptance_date": "2026-07-21"}
    _rejects(
        lambda: entrypoint.handle_supplier_shipments_patch_request(
            SHIPMENT_ID, direct_payload
        ),
        "confirmation token",
    )
    # Module 34: FF acceptance is owned by the unified receipt document;
    # ordinary factual-date confirmation now owns shipment dates only.
    source_before = runtime.load_supplier_shipment(SHIPMENT_ID)
    receipts_before = runtime.list_ff_stock_operations()
    _rejects(
        lambda: entrypoint.handle_supplier_factual_dates_preview_request(SHIPMENT_ID, {
            "actual_shipment_date": "2026-06-26",
            "actual_ff_acceptance_date": "2026-07-21",
        }),
        "Принять на FF",
    )
    preview = entrypoint.handle_supplier_factual_dates_preview_request(
        SHIPMENT_ID, {"actual_shipment_date": "2026-06-26"}
    )
    _assert(len(preview["changes"]) == 1, "one shipment date in preview")
    _assert(
        preview["changes"][0]["old_value"] == "2026-06-25"
        and preview["changes"][0]["new_value"] == "2026-06-26",
        "preview binds old and new values",
    )
    _assert(runtime.load_supplier_shipment(SHIPMENT_ID) == source_before,
            "rejected FF edit and shipment preview preserve source")
    _assert(runtime.list_ff_stock_operations() == receipts_before,
            "preview/cancel has no FF effects")

    def run_inline(*, operation, runner):
        _assert(operation == "supplier_factual_date_correction", "correct worker")
        return runner(lambda *_args, **_kwargs: None)

    # Only scheduling is synchronous; the persisted correction and its real
    # transaction/readback execute against this disposable runtime.
    with patch.object(entrypoint.operator_jobs, "start", side_effect=run_inline) as start:
        result = entrypoint.handle_supplier_factual_dates_confirm_request(
            SHIPMENT_ID, {"confirmation_token": preview["confirmation_token"]}, actor="smoke",
        )
        _assert(result["status"] == "accepted", "confirmation admits correction")
        correction = entrypoint.supplier_shipment_factual_correction_block.get_job(
            result["correction"]["correction_id"]
        )
        _assert(correction["status"] == "success", "real correction completes")
        saved = runtime.load_supplier_shipment(SHIPMENT_ID)
        _assert(saved["header"]["actual_shipment_date"] == "2026-06-26",
                "confirmed shipment date is saved")
        _assert(saved["lines"] == source_before["lines"], "no composition mutation")
        repeated = entrypoint.handle_supplier_factual_dates_confirm_request(
            SHIPMENT_ID, {"confirmation_token": preview["confirmation_token"]}, actor="smoke",
        )
        _assert(repeated.get("idempotent"), "repeated date confirmation is idempotent")
        _assert(start.call_count == 1, "repeat does not admit a second worker")
        _assert(runtime.load_supplier_shipment(SHIPMENT_ID) == saved, "repeat has no source effect")
    _assert(runtime.list_ff_stock_operations() == receipts_before, "shipment date never posts FF receipt")
    _assert(
        saved["header"]["actual_ff_acceptance_date"] == "",
        "FF date remains owned by unified receipt",
    )
    stale = entrypoint.handle_supplier_factual_dates_preview_request(
        SHIPMENT_ID, {"actual_shipment_date": "2026-06-27"}
    )
    shipment = runtime.load_supplier_shipment(SHIPMENT_ID) or {}
    runtime.save_supplier_shipment(
        header={**shipment["header"], "updated_at": "2026-07-24T09:01:00Z"},
        lines=shipment["lines"],
    )
    _rejects(
        lambda: entrypoint.handle_supplier_factual_dates_confirm_request(
            SHIPMENT_ID,
            {"confirmation_token": stale["confirmation_token"]},
            actor="smoke",
        ),
        "stale",
    )


def _assert_financial_contract(runtime: RegistryUploadDbBackedRuntime) -> None:
    block = SupplierFinancialDocumentsBlock(
        runtime=runtime,
        timestamp_factory=lambda: "2026-07-24T09:02:00Z",
        usd_rate_provider=StaticUsdRateProvider({"2026-07-15": "1"}),
        pdf_text_extractor=lambda _body, _filename: (
            INVOICE_TEXT,
            {"method": "fixture"},
            [],
        ),
    )
    counters = {"capital": 0, "queue": 0, "reset": 0}
    block._materialize_own_capital_expense_events = lambda _shipment_id: (  # type: ignore[method-assign]
        counters.__setitem__("capital", counters["capital"] + 1)
        or {}
    )
    block._enqueue_functional_recalculation = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        counters.__setitem__("queue", counters["queue"] + 1)
        or {"status": "queued"}
    )
    block._reset_own_capital_expense_certification = lambda _shipment_id: counters.__setitem__(  # type: ignore[method-assign]
        "reset", counters["reset"] + 1
    )
    source = b"%PDF-1.4 invoice 136"
    with patch.object(
        runtime,
        "save_supplier_confirmation_preview",
        side_effect=RuntimeError("injected durable preview failure"),
    ):
        try:
            block.preview_document_upload(
                SHIPMENT_ID,
                file_bytes=b"%PDF-1.4 staging failure",
                uploaded_filename="staging-failure.pdf",
            )
        except RuntimeError as exc:
            _assert(
                "injected durable preview failure" in str(exc),
                "injected preview failure propagated",
            )
        else:
            raise AssertionError("injected durable preview failure was not raised")
    staging_root = runtime.runtime_dir / "supplier_financial_staging"
    _assert(
        not staging_root.exists() or not any(staging_root.rglob("*")),
        "failed durable preview removes its owned staging file",
    )
    expiring_preview = block.preview_document_upload(
        SHIPMENT_ID,
        file_bytes=b"%PDF-1.4 expiring staging fixture",
        uploaded_filename="expiring-staging.pdf",
        ttl_seconds=1,
    )
    expired_count = runtime.cleanup_expired_supplier_confirmation_previews(
        now="2026-07-24T09:03:00Z"
    )
    _assert(expired_count == 1, "expired preview lifecycle removed one token")
    _assert(
        runtime.load_supplier_confirmation_preview(
            expiring_preview["confirmation_token"]
        )
        is None,
        "expired preview token was not deleted",
    )
    _assert(
        not staging_root.exists() or not any(staging_root.rglob("*")),
        "expired preview lifecycle leaves no staging orphan",
    )
    preview = block.preview_document_upload(
        SHIPMENT_ID, file_bytes=source, uploaded_filename="136.pdf"
    )
    _assert(
        not runtime.list_supplier_financial_documents(SHIPMENT_ID),
        "upload preview creates no active document",
    )
    _assert(not any(counters.values()), "upload preview has no calculation effects")
    created = block.confirm_document_upload(
        SHIPMENT_ID, confirmation_token=preview["confirmation_token"]
    )
    _assert(created.get("document_id"), "upload confirmation creates document")
    _assert(
        len(runtime.list_supplier_financial_documents(SHIPMENT_ID)) == 1,
        "one financial document after confirm",
    )
    _assert(
        not staging_root.exists() or not any(staging_root.rglob("*")),
        "consumed durable preview removes its owned staging file",
    )
    first_queue_count = counters["queue"]
    repeat = block.confirm_document_upload(
        SHIPMENT_ID, confirmation_token=preview["confirmation_token"]
    )
    _assert(repeat.get("idempotent"), "repeat upload confirmation is idempotent")
    _assert(counters["queue"] == first_queue_count, "repeat confirm creates no queue")
    exact_preview = block.preview_document_upload(
        SHIPMENT_ID, file_bytes=source, uploaded_filename="136-copy.pdf"
    )
    _assert(
        exact_preview["duplicate_action"] == "idempotent_active",
        "active SHA duplicate is identified",
    )
    exact_result = block.confirm_document_upload(
        SHIPMENT_ID, confirmation_token=exact_preview["confirmation_token"]
    )
    _assert(
        exact_result["duplicate_action"] == "existing_active"
        and len(runtime.list_supplier_financial_documents(SHIPMENT_ID)) == 1,
        "active SHA duplicate creates no row",
    )
    _assert(
        not staging_root.exists() or not any(staging_root.rglob("*")),
        "idempotent duplicate confirmation leaves no staging orphan",
    )
    document_id = str(created["document_id"])
    delete_preview = block.preview_document_delete(SHIPMENT_ID, document_id)
    _assert(
        len(block.list_documents(SHIPMENT_ID)["documents"]) == 1,
        "delete preview/cancel leaves active projection unchanged",
    )
    deleted = block.confirm_document_delete(
        SHIPMENT_ID,
        document_id,
        confirmation_token=delete_preview["confirmation_token"],
    )
    _assert(deleted["archived"], "delete confirmation archives document")
    listed = block.list_documents(SHIPMENT_ID)
    _assert(
        not listed["documents"]
        and len(listed["archived_documents"]) == 1
        and not listed["expense_lines"],
        "excluded document and expense lines are absent from active projection",
    )
    delete_repeat = block.confirm_document_delete(
        SHIPMENT_ID,
        document_id,
        confirmation_token=delete_preview["confirmation_token"],
    )
    _assert(delete_repeat.get("idempotent"), "repeat delete is idempotent")
    restore_preview = block.preview_document_upload(
        SHIPMENT_ID, file_bytes=source, uploaded_filename="136-restored.pdf"
    )
    _assert(
        restore_preview["duplicate_action"] == "restore_excluded",
        "excluded SHA offers restore",
    )
    restored = block.confirm_document_upload(
        SHIPMENT_ID, confirmation_token=restore_preview["confirmation_token"]
    )
    _assert(
        restored.get("restored")
        and len(runtime.list_supplier_financial_documents(SHIPMENT_ID)) == 1,
        "excluded SHA restores original row",
    )
    semantic_preview = block.preview_document_upload(
        SHIPMENT_ID,
        file_bytes=b"%PDF-1.4 semantically same invoice 136",
        uploaded_filename="136-rescan.pdf",
    )
    _assert(
        semantic_preview["duplicate_action"] == "semantic_warning",
        "different SHA semantic duplicate is warned",
    )
    _rejects(
        lambda: block.confirm_document_upload(
            SHIPMENT_ID,
            confirmation_token=semantic_preview["confirmation_token"],
        ),
        "requires explicit confirmation",
    )
    semantic_result = block.confirm_document_upload(
        SHIPMENT_ID,
        confirmation_token=semantic_preview["confirmation_token"],
        allow_semantic_duplicate=True,
        duplicate_reason="Повторный оригинал от контрагента",
    )
    _assert(
        semantic_result["duplicate_action"] == "explicit_semantic_duplicate"
        and len(runtime.list_supplier_financial_documents(SHIPMENT_ID)) == 2,
        "semantic duplicate requires and records a reason",
    )
    multi_parse = {
        "normalized_parse": {
            "document_type": "logistics_invoice",
            "document_number": "MULTI-1",
            "document_date": "2026-07-24",
            "currency": "RUB",
            "total_amount": 300,
        },
        "raw_parse": {"fixture": "multi-result"},
        "expense_lines": [
            {
                "category": "delivery_cost",
                "description": "Доставка",
                "amount": 100,
                "amount_rub": 100,
                "currency": "RUB",
                "status": "parsed",
            },
            {
                "category": "other_bank_fee",
                "description": "Дополнительная комиссия",
                "amount": 200,
                "amount_rub": 200,
                "currency": "RUB",
                "status": "parsed",
            },
        ],
        "warnings": [],
        "errors": [],
        "parser_version": "multi-result-smoke-v1",
    }
    with patch(
        "packages.application.supplier_financial_documents.parse_financial_document_upload",
        return_value=multi_parse,
    ):
        multi_preview = block.preview_document_upload(
            SHIPMENT_ID,
            file_bytes=b"%PDF-1.4 multi-result",
            uploaded_filename="multi-result.pdf",
        )
        results = multi_preview.get("detected_results") or []
        _assert(
            len(results) == 2 and all(item.get("default_selected") for item in results),
            "multi-result preview exposes every detected line selected by default",
        )
        selected_id = str(results[1]["result_id"])
        selected = block.confirm_document_upload(
            SHIPMENT_ID,
            confirmation_token=multi_preview["confirmation_token"],
            selected_result_ids=[selected_id],
        )
        stored = block.get_document(SHIPMENT_ID, str(selected["document_id"]))
        _assert(
            len(stored.get("expense_lines") or []) == 1
            and stored["expense_lines"][0]["category"] == "other_bank_fee"
            and (stored.get("normalized_parse") or {}).get("preview_selection", {}).get("selected_result_ids") == [selected_id],
            "confirmation imports only selected result and retains selection evidence on one parent document",
        )
        repeated_selected = block.confirm_document_upload(
            SHIPMENT_ID,
            confirmation_token=multi_preview["confirmation_token"],
            selected_result_ids=[selected_id],
        )
        _assert(
            repeated_selected.get("idempotent")
            and len(block.get_document(SHIPMENT_ID, str(selected["document_id"])).get("expense_lines") or []) == 1,
            "selected-result confirmation is idempotent",
        )


def _assert_batch_financial_contract(
    runtime: RegistryUploadDbBackedRuntime,
    entrypoint: RegistryUploadHttpEntrypoint,
) -> None:
    def extractor(body: bytes, _filename: str) -> tuple[str, dict[str, str], list[str]]:
        number = "201" if b"batch-201" in body else "202"
        return (
            INVOICE_TEXT.replace("136", number),
            {"method": "batch-fixture"},
            [],
        )

    entrypoint.supplier_financial_documents_block.pdf_text_extractor = extractor
    previews = [
        entrypoint.supplier_financial_documents_block.preview_document_upload(
            SHIPMENT_ID,
            file_bytes=body,
            uploaded_filename=filename,
        )
        for body, filename in (
            (b"%PDF-1.4 batch-201", "201.pdf"),
            (b"%PDF-1.4 batch-202", "202.pdf"),
        )
    ]
    before_count = len(runtime.list_supplier_financial_documents(SHIPMENT_ID))
    result = entrypoint.handle_supplier_financial_documents_confirm_upload_request(
        SHIPMENT_ID,
        {
            "confirmation_tokens": [
                item["confirmation_token"] for item in previews
            ]
        },
    )
    _assert(
        len(result.get("results") or []) == 2
        and result.get("readback_confirmed")
        and len(runtime.list_supplier_financial_documents(SHIPMENT_ID))
        == before_count + 2,
        "batch confirmation commits every preview with target readback",
    )
    repeated = entrypoint.handle_supplier_financial_documents_confirm_upload_request(
        SHIPMENT_ID,
        {
            "confirmation_tokens": [
                item["confirmation_token"] for item in previews
            ]
        },
    )
    _assert(
        all(item.get("idempotent") for item in repeated.get("results") or [])
        and len(runtime.list_supplier_financial_documents(SHIPMENT_ID))
        == before_count + 2,
        "repeat batch confirmation creates no duplicate documents",
    )


def _seed_empty_functional(runtime: RegistryUploadDbBackedRuntime, now: str) -> None:
    """Unpaid temporary orders: active empty warehouse, no invented capital.

    The supported factual correction requires a published functional version.
    It will retain truthful missing-payment blockers, not call the disabled
    pre-cutover monolithic writer. The targeted replay smoke covers stock rows.
    """
    with sqlite3.connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        ensure_functional_version_business_time_schema(conn)
        conn.execute("""INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers
            VALUES(?,?,'posted','sha256:confirmation-fixture','{}','{}','{}',?,?)""",
            (FUNCTIONAL_CUTOVER_ID, now, now, now))
        conn.execute("""INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
            version_id,cutover_id,version_kind,effective_at,status,plan_fingerprint,
            local_source_digest,source_watermarks_json,created_at,business_effective_date,published_at)
            VALUES('confirmation-base',?,'hourly_wb_sync',?,'good',
            'sha256:confirmation-base','sha256:empty','{}',?,?,?)""",
            (FUNCTIONAL_CUTOVER_ID, now, now, now[:10], now))
        conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,'confirmation-base',?)", (now,))


def _seed(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": SHIPMENT_ID,
            "created_at": NOW,
            "updated_at": NOW,
            "shipment_date": "2026-06-15",
            "actual_shipment_date": "2026-06-25",
            "actual_ff_acceptance_date": "",
            "order_status": "in_transit",
            "invoice_no": "26GN390",
            "invoice_date": "2026-06-15",
            "currency": "CNY",
            "approx_yuan_rate": 12,
            "product_qty_total": 1,
            "product_amount_total": 1,
            "extras_amount_total": 0,
            "invoice_amount_total": 1,
            "declared_invoice_total": 1,
            "match_status": "all_matched",
            "warnings": [],
            "errors": [],
        },
        lines=[
            {
                "line_id": "legacy-1",
                "line_type": "product",
                "sort_order": 1,
                "source_no": "1",
                "source_row": 1,
                "source_sheet": "Invoice",
                "barcode": "",
                "source_model": "Legacy",
                "normalized_model": "legacy",
                "match_key": "legacy",
                "internal_nm_id": 123,
                "internal_name": "Legacy",
                "qty": 1,
                "unit_price": 1,
                "amount": 1,
                "currency": "CNY",
                "comment": "",
                "manual_override": False,
                "match_status": "matched_by_compatibility",
                "raw": {"legacy": True},
            }
        ],
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
