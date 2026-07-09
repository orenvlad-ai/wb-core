"""Targeted smoke-check for the server-owned Остатки ФФ ledger."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.factory_order_sales_history import persist_sales_history_result_exact_dates
from packages.application.factory_order_supply import FactoryOrderSupplyBlock
from packages.application.ff_stock_ledger import (
    FF_STOCK_OPERATION_AUTO_WRITEOFF,
    FF_STOCK_OPERATION_CORRECTION_RECEIPT,
    FF_STOCK_OPERATION_MANUAL_RECEIPT,
    FF_STOCK_OPERATION_MANUAL_WRITEOFF,
    FF_STOCK_SOURCE_MANUAL_EXCEL,
    FF_STOCK_SOURCE_RUNTIME_REPAIR,
    FF_STOCK_SOURCE_WB_SUPPLY,
    FfStockLedgerBlock,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.application.wb_regional_supply import WbRegionalSupplyBlock
from packages.contracts.factory_order_supply import STOCK_FF_SOURCE_LEDGER
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryItem, SalesFunnelHistorySuccess
from packages.contracts.stocks_block import StocksEnvelope, StocksItem, StocksSuccess
from packages.contracts.supplier_shipments import (
    LINE_TYPE_PRODUCT,
    MATCH_STATUS_MATCHED,
)


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
ACTIVATED_AT = "2026-04-18T09:00:00Z"
AFTER_ACTIVATION = "2026-04-18T09:01:00Z"
BEFORE_ACTIVATION = "2026-04-18T08:59:59Z"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class FakeStocksBlock:
    def __init__(self, nm_ids: list[int]) -> None:
        self.nm_ids = [int(item) for item in nm_ids]

    def execute(self, request_obj: object) -> SimpleNamespace:
        del request_obj
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                items=[
                    SimpleNamespace(
                        nm_id=nm_id,
                        stock_total=0.0,
                        stock_ru_central=100.0,
                        stock_ru_northwest=100.0,
                        stock_ru_volga=0.0,
                        stock_ru_ural=0.0,
                        stock_ru_south_caucasus=0.0,
                        stock_ru_far_siberia=0.0,
                    )
                    for nm_id in self.nm_ids
                ],
            )
        )


class NoopSalesHistoryBlock:
    def execute(self, request_obj: object) -> SimpleNamespace:  # pragma: no cover - should not be called
        raise AssertionError(f"sales history must be seeded, got live fetch request {request_obj!r}")


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="ff-stock-ledger-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        active_nm_ids = [int(item.nm_id) for item in runtime.load_current_state().config_v2 if item.enabled]
        probe_nm_id = active_nm_ids[0]
        second_nm_id = active_nm_ids[1]
        _seed_nomenclature(runtime, active_nm_ids)
        _seed_sales_history(runtime, active_nm_ids)
        _seed_stock_history(runtime, active_nm_ids)

        cold_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "cold-runtime")
        cold_runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        _seed_nomenclature(cold_runtime, active_nm_ids)
        cold_block = FfStockLedgerBlock(runtime=cold_runtime, timestamp_factory=lambda: ACTIVATED_AT)
        cold_skip = cold_block.record_wb_supply_debit(_wb_record("wb-cold-ledger", 5, second_nm_id, 1))
        _assert(
            cold_skip and cold_skip.get("skip_reason") == "wb_supply_auto_writeoff_checkpoint_missing",
            f"WB auto writeoff must fail closed until checkpoint exists, got {cold_skip}",
        )
        cold_block.ensure_wb_supply_auto_writeoff_checkpoint([], reason="smoke_cold_checkpoint", created_by="smoke")
        cold_after_checkpoint = cold_block.record_wb_supply_debit(_wb_record("wb-cold-after-checkpoint", 5, second_nm_id, 1))
        _assert(
            cold_after_checkpoint and cold_after_checkpoint.get("skip_reason") == "wb_supply_ledger_not_activated",
            f"WB auto writeoff must wait for opening receipt/activation after checkpoint, got {cold_after_checkpoint}",
        )

        block = FfStockLedgerBlock(runtime=runtime, timestamp_factory=lambda: ACTIVATED_AT)
        barcode = f"460{probe_nm_id}"
        receipt_xlsx = _operation_xlsx(
            [
                [barcode, probe_nm_id, "Probe", "Clear", 100],
                [f"460{second_nm_id}", second_nm_id, "Second", "Clear", 0],
            ]
        )
        preview = block.parse_manual_operation_preview(
            receipt_xlsx,
            operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
            uploaded_filename="receipt.xlsx",
            uploaded_content_type=XLSX_TYPE,
        )
        _assert(preview["apply_allowed"], "manual receipt preview must be applicable")
        _assert(preview["preview"]["summary"]["sku_count"] == 1, "zero quantity rows must be skipped without errors")
        receipt = block.confirm_manual_operation(preview["preview"]["preview_id"], created_by="operator")
        _assert(receipt["operation"]["file_available"], "manual receipt operation must keep source XLSX")
        source_file, source_name, source_type = block.download_operation_source_file(receipt["operation"]["operation_id"])
        _assert(source_file == receipt_xlsx and source_name == "receipt.xlsx" and source_type == XLSX_TYPE, "source XLSX download must roundtrip")
        _assert(_balance(block, probe_nm_id) == 100.0, "manual receipt must increase balance")

        exported, exported_name, _ = block.export_current_balances_xlsx()
        _assert(exported_name == "sheet-vitrina-v1-ff-stock-balances.xlsx", "export filename changed")
        export_rows = read_first_sheet_rows(exported)
        _assert(export_rows[0] == ["barcode", "nmId", "SKU/название/комментарий", "группа", "количество"], "export headers changed")
        export_preview = block.parse_manual_operation_preview(
            exported,
            operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
            uploaded_filename="roundtrip.xlsx",
        )
        _assert(not export_preview["preview"]["errors"], "export workbook must parse back without row errors")

        writeoff_preview = block.parse_manual_operation_preview(
            _operation_xlsx([[barcode, probe_nm_id, "Probe", "Clear", 125]]),
            operation_type=FF_STOCK_OPERATION_MANUAL_WRITEOFF,
            uploaded_filename="writeoff.xlsx",
        )
        block.confirm_manual_operation(writeoff_preview["preview"]["preview_id"], created_by="operator")
        _assert(_balance(block, probe_nm_id) == -25.0, "manual writeoff must allow negative balance")
        status = block.get_status()
        negative_rows = [item for item in status["registry"]["rows"] if int(item["nm_id"]) == probe_nm_id]
        _assert(negative_rows and negative_rows[0]["negative_balance"], "negative balance warning must be exposed")

        supplier_detail = {
            "header": {"shipment_id": "sup-ledger-1", "invoice_no": "INV-1", "invoice_date": "2026-04-18"},
            "lines": [
                {
                    "line_type": LINE_TYPE_PRODUCT,
                    "match_status": MATCH_STATUS_MATCHED,
                    "internal_nm_id": second_nm_id,
                    "internal_sku": "SKU-2",
                    "internal_name": "Second",
                    "qty": 10,
                }
            ],
        }
        supplier_op = block.record_supplier_acceptance(supplier_detail)
        supplier_op_repeat = block.record_supplier_acceptance(supplier_detail)
        _assert(not supplier_op.get("idempotent") and supplier_op_repeat.get("idempotent"), "supplier auto receipt must be idempotent")
        _assert(_balance(block, second_nm_id) == 10.0, "supplier auto receipt must add accepted quantity")

        checkpoint = block.ensure_wb_supply_auto_writeoff_checkpoint(
            [_wb_record("wb-baseline-known", 5, second_nm_id, 1)],
            reason="smoke_baseline",
            created_by="smoke",
        )
        checkpoint_repeat = block.ensure_wb_supply_auto_writeoff_checkpoint([], reason="smoke_repeat", created_by="smoke")
        _assert(
            checkpoint.get("baseline_record_count") == 1 and not checkpoint.get("idempotent"),
            f"checkpoint must capture baseline known WB supply, got {checkpoint}",
        )
        _assert(checkpoint_repeat.get("idempotent"), f"checkpoint ensure must be idempotent, got {checkpoint_repeat}")
        baseline_known = block.record_wb_supply_debit(_wb_record("wb-baseline-known", 5, second_nm_id, 1))
        _assert(
            baseline_known and baseline_known.get("skip_reason") == "wb_supply_before_auto_writeoff_checkpoint",
            f"baseline-known WB supply must not backfill into ledger, got {baseline_known}",
        )
        _assert(
            "source_key" in (baseline_known.get("checkpoint_match_fields") or []),
            f"baseline-known skip must report matched checkpoint identity, got {baseline_known}",
        )

        for status_id in (1, 2):
            result = block.record_wb_supply_debit(_wb_record(f"wb-skip-{status_id}", status_id, second_nm_id, 5))
            _assert(result is None, f"WB status {status_id} must not debit ФФ")
        for status_id in (3, 4, 5, 6):
            result = block.record_wb_supply_debit(_wb_record(f"wb-debit-{status_id}", status_id, second_nm_id, 1))
            repeat = block.record_wb_supply_debit(_wb_record(f"wb-debit-{status_id}", status_id, second_nm_id, 1))
            _assert(result and not result.get("idempotent"), f"WB status {status_id} must debit ФФ")
            _assert(repeat and repeat.get("idempotent"), f"WB status {status_id} debit must be idempotent")
        _assert(_balance(block, second_nm_id) == 6.0, "WB debits must subtract supply composition quantity")
        historical_debit = block.record_wb_supply_debit(
            _wb_record("wb-before-ledger", 5, second_nm_id, 1, source_created_at=BEFORE_ACTIVATION)
        )
        _assert(
            historical_debit and historical_debit.get("skip_reason") == "wb_supply_before_auto_writeoff_checkpoint",
            f"historical WB supply must not backfill across checkpoint, got {historical_debit}",
        )
        oversized_debit = block.record_wb_supply_debit(_wb_record("wb-too-large", 5, second_nm_id, 100))
        _assert(
            oversized_debit and oversized_debit.get("skip_reason") == "wb_supply_would_make_negative_balance",
            f"WB auto writeoff must not create negative balance, got {oversized_debit}",
        )
        bulk_repeat = block.record_wb_supply_debits(
            [
                _wb_record("wb-baseline-known", 5, second_nm_id, 1),
                _wb_record("wb-debit-5", 5, second_nm_id, 1),
            ]
        )
        _assert(
            bulk_repeat["created_count"] == 0
            and bulk_repeat["skipped_reasons"].get("wb_supply_before_auto_writeoff_checkpoint") == 1,
            f"repeated baseline/existing WB sync must not create duplicate debits, got {bulk_repeat}",
        )
        _assert(_balance(block, second_nm_id) == 6.0, "skipped WB debits must not change balance")
        doprinato_virtual = block.record_wb_supply_debit(_wb_record("wb-dopr-virtual", 5, second_nm_id, 100, virtual_type_id=5))
        doprinato_label = block.record_wb_supply_debit(_wb_record("wb-dopr-label", 5, second_nm_id, 100, type_label="Допринято"))
        _assert(doprinato_virtual and doprinato_virtual.get("skip_reason"), "virtual_type_id=5 must skip debit")
        _assert(doprinato_label and doprinato_label.get("skip_reason"), "type_label=Допринято must skip debit")
        _assert(_balance(block, second_nm_id) == 6.0, "Допринято skips must not change balance")

        _seed_wb_supply_overlay_fixture(runtime, supply_id="wb-ledger-overlay", nm_id=probe_nm_id, quantity=10.0)
        factory_result = FactoryOrderSupplyBlock(
            runtime=runtime,
            stocks_block=FakeStocksBlock(active_nm_ids),
            sales_funnel_history_block=NoopSalesHistoryBlock(),
            now_factory=lambda: NOW,
            timestamp_factory=lambda: ACTIVATED_AT,
        ).calculate(_factory_settings(selected_wb_supply_ids=["wb-ledger-overlay"]))
        _assert(factory_result.stock_ff_source == STOCK_FF_SOURCE_LEDGER, "factory calculation must keep ledger source")
        factory_probe = next(item for item in factory_result.rows if item.nm_id == probe_nm_id)
        _assert(factory_probe.stock_ff == -25.0, "factory calculation must use computed ledger balance, including negative")
        _assert(factory_probe.inbound_ff_to_wb == 10.0, "factory ledger source must still add selected WB supply inbound")
        _assert(
            factory_probe.coverage_qty == -15.0,
            "factory ledger coverage must be stock_total_mp + ledger_stock_ff + inbound_factory_to_ff + selected_wb_supply_inbound_ff_to_wb",
        )
        factory_overlay = factory_result.wb_supply_overlay or {}
        factory_overlay_stock = factory_overlay.get("stock_ff", {})
        _assert(
            factory_overlay_stock.get("stock_deduction_applied") is False,
            "factory ledger source must not deduct selected WB supply from stock_ff again",
        )
        _assert(
            factory_overlay_stock.get("by_nm_id", {}).get(str(probe_nm_id), {}).get("effective_stock_ff") == -25.0,
            "factory ledger overlay diagnostics must keep ledger stock_ff unchanged",
        )

        regional_result = WbRegionalSupplyBlock(
            runtime=runtime,
            stocks_block=FakeStocksBlock(active_nm_ids),
            sales_funnel_history_block=NoopSalesHistoryBlock(),
            now_factory=lambda: NOW,
            timestamp_factory=lambda: ACTIVATED_AT,
        ).calculate(_regional_settings(selected_wb_supply_ids=["wb-ledger-overlay"]))
        _assert(regional_result.stock_ff_source == STOCK_FF_SOURCE_LEDGER, "WB regional calculation must keep ledger source")
        _assert(regional_result.summary.total_qty >= 0, "WB regional calculation with ledger source must complete")
        _assert(
            any("рекомендации к поставке ограничены доступным ФФ-остатком" in warning for warning in regional_result.warnings),
            f"WB regional calculation must expose critical ledger stock warning, got {regional_result.warnings}",
        )
        _assert(
            (regional_result.diagnostics.get("stock_ff_source_state") or {}).get("negative_sku_count") >= 1,
            f"WB regional diagnostics must expose ledger stock state, got {regional_result.diagnostics}",
        )
        regional_overlay = regional_result.wb_supply_overlay or {}
        regional_overlay_stock = regional_overlay.get("stock_ff", {})
        regional_overlay_projection = regional_overlay.get("wb_regional", {})
        _assert(
            regional_overlay_stock.get("stock_deduction_applied") is False,
            "WB regional ledger source must not deduct selected WB supply from stock_ff again",
        )
        _assert(
            regional_overlay_projection.get("added_qty_by_district", {}).get("central") == 10.0,
            "WB regional ledger source must still add selected WB supply to district projection",
        )

        balance_before_pagination_status = _balance(block, second_nm_id)
        _seed_operation_journal_pagination_fixture(runtime)
        default_page = block.get_status()
        _assert(default_page["operations_page"]["current_page"] == 1, "default status must return first operations page")
        _assert(default_page["operations_page"]["limit"] == 50, "default operations page size must remain 50")
        _assert(
            default_page["operations_page"]["show_technical_archive"] is True,
            "block status must keep technical archive visible by default for backwards compatibility",
        )
        working_page_1 = block.get_status(operations_limit=50, operations_page=1, show_technical_archive=False)
        working_page_2 = block.get_status(operations_limit=50, operations_page=2, show_technical_archive=False)
        _assert(working_page_1["operations_page"]["total_count"] >= 60, "working journal page must count visible operations")
        _assert(working_page_1["operations_page"]["has_next"], "working journal first page must expose next page")
        _assert(working_page_2["operations_page"]["current_page"] == 2, "working journal must return requested second page")
        _assert(working_page_2["operations"], "working journal second page must include rows")
        _assert(
            working_page_1["operations_page"]["hidden_archive_count"] >= 2,
            f"archive-off view must report hidden technical rows, got {working_page_1['operations_page']}",
        )
        archive_page = block.get_status(operations_limit=200, operations_page=1, show_technical_archive=True)
        _assert(
            any(item["source_type"] == FF_STOCK_SOURCE_RUNTIME_REPAIR for item in archive_page["operations"]),
            "archive-on view must retrieve runtime_repair operations",
        )
        _assert(
            any(item["source_type"] == FF_STOCK_SOURCE_WB_SUPPLY for item in archive_page["operations"]),
            "archive-on view must retrieve old WB auto_writeoff operations",
        )
        _assert(_balance(block, second_nm_id) == balance_before_pagination_status, "journal pagination/archive status must not change balances")

    print("ff_stock_ledger_smoke: ok")


def _operation_xlsx(rows: list[list[object]]) -> bytes:
    return build_single_sheet_workbook_bytes(
        "Остатки ФФ",
        [["barcode", "nmId", "SKU/название/комментарий", "группа", "количество"], *rows],
    )


def _seed_nomenclature(runtime: RegistryUploadDbBackedRuntime, active_nm_ids: list[int]) -> None:
    runtime.save_sku_group(
        {
            "group_key": "clear",
            "label": "Clear",
            "is_active": True,
            "is_system": False,
            "created_at": ACTIVATED_AT,
            "updated_at": ACTIVATED_AT,
        }
    )
    runtime.save_nomenclature_items_atomic(
        [
            {
                "item_id": f"nom_{nm_id}",
                "is_active": True,
                "is_hidden": False,
                "our_sku": f"SKU-{index}",
                "nm_id": nm_id,
                "barcode": f"460{nm_id}",
                "barcodes": [f"460{nm_id}"],
                "nomenclature_name": f"SKU name {index}",
                "product_type": "clear",
                "match_key": f"sku-{index}",
                "comment": f"comment {index}",
                "created_at": ACTIVATED_AT,
                "updated_at": ACTIVATED_AT,
            }
            for index, nm_id in enumerate(active_nm_ids, start=1)
        ]
    )


def _seed_sales_history(runtime: RegistryUploadDbBackedRuntime, active_nm_ids: list[int]) -> None:
    start = date(2026, 3, 20)
    items: list[SalesFunnelHistoryItem] = []
    for offset in range(29):
        snapshot_date = (start + timedelta(days=offset)).isoformat()
        for nm_id in active_nm_ids:
            items.append(
                SalesFunnelHistoryItem(
                    date=snapshot_date,
                    nm_id=int(nm_id),
                    metric="orderCount",
                    value=1.0,
                )
            )
    persist_sales_history_result_exact_dates(
        runtime=runtime,
        payload=SalesFunnelHistorySuccess(
            kind="success",
            date_from=start.isoformat(),
            date_to=(start + timedelta(days=28)).isoformat(),
            count=len(items),
            items=items,
        ),
        captured_at=ACTIVATED_AT,
    )


def _seed_stock_history(runtime: RegistryUploadDbBackedRuntime, active_nm_ids: list[int]) -> None:
    start = date(2026, 4, 3)
    for offset in range(15):
        snapshot_date = start + timedelta(days=offset)
        items = [
            StocksItem(
                nm_id=int(nm_id),
                stock_total=200.0,
                stock_ru_central=100.0,
                stock_ru_northwest=100.0,
                stock_ru_volga=0.0,
                stock_ru_ural=0.0,
                stock_ru_south_caucasus=0.0,
                stock_ru_far_siberia=0.0,
            )
            for nm_id in active_nm_ids
        ]
        runtime.save_temporal_source_snapshot(
            source_key="stocks",
            snapshot_date=snapshot_date.isoformat(),
            captured_at=ACTIVATED_AT,
            payload=StocksEnvelope(
                result=StocksSuccess(
                    kind="success",
                    snapshot_date=snapshot_date.isoformat(),
                    count=len(items),
                    items=items,
                )
            ),
        )


def _wb_record(
    cache_key: str,
    status_id: int,
    nm_id: int,
    quantity: float,
    *,
    virtual_type_id: int | None = None,
    type_label: str = "",
    source_created_at: str = AFTER_ACTIVATION,
) -> dict[str, object]:
    return {
        "cache_key": cache_key,
        "supply_id": cache_key,
        "normalized": {
            "cache_key": cache_key,
            "supply_id": cache_key,
            "visible_number": cache_key,
            "status_id": status_id,
            "virtual_type_id": virtual_type_id,
            "type_label": type_label,
            "source_created_at": source_created_at,
            "supply_date": source_created_at[:10],
        },
        "raw_goods": [{"nmID": int(nm_id), "quantity": float(quantity)}],
    }


def _balance(block: FfStockLedgerBlock, nm_id: int) -> float:
    rows = block.current_balance_rows_for_active_skus([(int(nm_id), "")])
    return float(rows[0]["current_stock_ff"])


def _factory_settings(*, selected_wb_supply_ids: list[str] | None = None) -> dict[str, object]:
    settings: dict[str, object] = {
        "prod_lead_time_days": 1,
        "lead_time_factory_to_ff_days": 1,
        "lead_time_ff_to_wb_days": 1,
        "safety_days_mp": 0,
        "safety_days_ff": 0,
        "cycle_order_days": 1,
        "order_batch_qty": 10,
        "sales_avg_period_days": 7,
        "report_date_override": "2026-04-18",
        "stock_ff_source": STOCK_FF_SOURCE_LEDGER,
    }
    if selected_wb_supply_ids:
        settings["selected_wb_supply_ids"] = selected_wb_supply_ids
    return settings


def _regional_settings(*, selected_wb_supply_ids: list[str] | None = None) -> dict[str, object]:
    settings: dict[str, object] = {
        "sales_avg_period_days": 7,
        "cycle_supply_days": 1,
        "lead_time_to_region_days": 1,
        "safety_days": 0,
        "order_batch_qty": 10,
        "report_date_override": "2026-04-18",
        "stock_ff_source": STOCK_FF_SOURCE_LEDGER,
        "included_district_keys": ["central", "northwest"],
    }
    if selected_wb_supply_ids:
        settings["selected_wb_supply_ids"] = selected_wb_supply_ids
    return settings


def _seed_wb_supply_overlay_fixture(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    supply_id: str,
    nm_id: int,
    quantity: float,
) -> None:
    runtime.save_wb_supply_rows(
        rows=[
            {
                "supply_id": supply_id,
                "cache_key": supply_id,
                "wb_supply_id": supply_id,
                "preorder_id": "pre-" + supply_id,
                "number_label": supply_id,
                "status_id": 3,
                "status_label": "Отгрузка разрешена",
                "warehouse_id": "507",
                "warehouse_name": "Коледино",
                "planned_warehouse_id": "507",
                "planned_warehouse_name": "Коледино",
                "target_warehouse_id": "507",
                "target_warehouse_name": "Коледино",
                "warehouse_display": "Коледино",
                "district_source_warehouse_id": "507",
                "district_source_warehouse_name": "Коледино",
                "district_source_warehouse_role": "planned",
                "district_source_warehouse_evidence": "fixture.warehouse_name",
                "supply_date": "2026-04-19",
                "district_key": "central",
                "district_label_ru": "Центральный федеральный округ",
                "quantity_for_size_filter": float(quantity),
                "raw_list": {"supplyID": supply_id, "statusID": 3, "supplyDate": "2026-04-19"},
                "raw_detail": {"warehouseID": 507, "warehouseName": "Коледино"},
                "raw_goods": [{"nmID": int(nm_id), "quantity": float(quantity)}],
                "raw_package": [],
            }
        ],
        warehouses=[{"warehouse_id": "507", "warehouse_name": "Коледино"}],
        synced_at=ACTIVATED_AT,
    )


def _seed_operation_journal_pagination_fixture(runtime: RegistryUploadDbBackedRuntime) -> None:
    for index in range(60):
        runtime.create_ff_stock_operation(
            operation_id=f"ffso_page_visible_{index:03d}",
            operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
            source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
            source_key=f"manual_excel:page-visible:{index:03d}",
            source_object_id=f"page-visible-{index:03d}",
            source_object_label=f"pagination visible {index:03d}",
            created_at=f"2026-04-18T09:{10 + (index // 60):02d}:{index % 60:02d}Z",
            created_by="smoke",
            lines=[],
        )
    runtime.create_ff_stock_operation(
        operation_id="ffso_page_repair_archive",
        operation_type=FF_STOCK_OPERATION_CORRECTION_RECEIPT,
        source_type=FF_STOCK_SOURCE_RUNTIME_REPAIR,
        source_key="runtime_repair:page-archive",
        source_object_id="repair-page-archive",
        source_object_label="runtime_repair archive",
        created_at="2026-04-18T09:59:59Z",
        created_by="smoke",
        diagnostics={"reason": "pagination smoke"},
        lines=[],
    )
    runtime.create_ff_stock_operation(
        operation_id="ffso_page_old_wb_archive",
        operation_type=FF_STOCK_OPERATION_AUTO_WRITEOFF,
        source_type=FF_STOCK_SOURCE_WB_SUPPLY,
        source_key="wb_supply:page-old-auto-writeoff",
        source_object_id="page-old-auto-writeoff",
        source_object_label="old WB auto_writeoff",
        created_at=BEFORE_ACTIVATION,
        created_by="system",
        diagnostics={"cache_key": "page-old-auto-writeoff"},
        lines=[],
    )


def _assert(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
