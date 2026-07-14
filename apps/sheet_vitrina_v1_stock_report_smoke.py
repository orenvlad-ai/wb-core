"""Targeted smoke-check for the sheet_vitrina_v1 stock-report builder."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.ff_stock_ledger import (
    FF_STOCK_OPERATION_MANUAL_RECEIPT,
    FF_STOCK_SOURCE_MANUAL_EXCEL,
)
from packages.application.sheet_vitrina_v1_stock_report import (
    STOCK_REPORT_DISTRICTS,
    SheetVitrinaV1StockReportBlock,
    list_active_sku_options,
)
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryItem, SalesFunnelHistorySuccess
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)
from packages.contracts.supplier_shipments import (
    LINE_TYPE_PRODUCT,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_MATCHED_BY_COMPATIBILITY,
    MATCH_STATUS_UNMATCHED,
    ORDER_STATUS_ACCEPTED_FF,
    ORDER_STATUS_IN_TRANSIT,
    ORDER_STATUS_PRODUCTION,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc)
CAPTURED_AT = "2026-04-19T09:00:00Z"
STATUS_HEADER = [
    "source_key",
    "kind",
    "freshness",
    "snapshot_date",
    "date",
    "date_from",
    "date_to",
    "requested_count",
    "covered_count",
    "missing_nm_ids",
    "note",
]


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-stock-report-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        result = runtime.ingest_bundle(bundle, activated_at="2026-04-19T09:00:00Z")
        if result.status != "accepted":
            raise AssertionError(f"bundle ingest must be accepted, got {result}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        if len(enabled) < 4:
            raise AssertionError("fixture must expose at least 4 enabled SKU rows")

        nm_ids = [item.nm_id for item in enabled[:4]]
        active_sku_count = len(enabled)
        metric_labels = {item.metric_key: item.label_ru for item in current_state.metrics_v2 if item.enabled}
        _seed_nomenclature(runtime, nm_ids[0])
        _seed_sales_history(runtime, nm_ids)
        _seed_supplier_shipments(runtime, nm_ids)
        _seed_wb_supplies(runtime, nm_ids)
        _seed_ff_stock_balances(runtime, nm_ids)
        for snapshot_date in ["2026-04-15", "2026-04-16", "2026-04-17", "2026-04-18"]:
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{snapshot_date}T09:05:00Z",
                plan=_build_plan(
                    as_of_date=snapshot_date,
                    current_state=current_state,
                    metric_labels=metric_labels,
                    closed_sku_values=_closed_sku_values(snapshot_date, nm_ids),
                    today_sku_values={nm_id: {"stock_total": 999.0} for nm_id in nm_ids},
                ),
            )

        payload = SheetVitrinaV1StockReportBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(sales_avg_period_days=3)

        if payload.get("status") != "available":
            raise AssertionError(f"stock report must be available, got {payload}")
        if payload.get("report_date") != "2026-04-18":
            raise AssertionError(f"stock report date must default to previous closed business day, got {payload}")
        if payload.get("sales_avg_period_days") != 3:
            raise AssertionError(f"stock report must disclose requested averaging period, got {payload}")
        if payload.get("threshold_lt") != 50:
            raise AssertionError(f"legacy risk threshold can remain disclosed but must not filter rows, got {payload}")

        source_of_truth = payload.get("source_of_truth") or {}
        if source_of_truth.get("read_model") != "persisted_ready_snapshot":
            raise AssertionError(f"stock report must disclose persisted ready snapshot source, got {source_of_truth}")
        if source_of_truth.get("temporal_slot") != "yesterday_closed" or source_of_truth.get("slot_date") != "2026-04-18":
            raise AssertionError(f"stock report must read yesterday_closed for the selected date, got {source_of_truth}")
        if source_of_truth.get("wb_supplies_excluded_status_ids") != [1, 2, 5]:
            raise AssertionError(f"stock report must disclose WB supply status exclusions, got {source_of_truth}")
        if source_of_truth.get("stock_ff_source") != "ff_stock_ledger current balances":
            raise AssertionError(f"stock report must disclose ФФ ledger source, got {source_of_truth}")
        if source_of_truth.get("supplier_shipments_source") != "sheet_vitrina_v1_supplier_shipments runtime registry":
            raise AssertionError(f"stock report must disclose supplier shipment registry source, got {source_of_truth}")
        supplier_statuses = source_of_truth.get("supplier_shipments_included_statuses") or []
        if [item.get("code") for item in supplier_statuses] != ["production", "in_transit"]:
            raise AssertionError(f"stock report must map supplier statuses production/in_transit, got {supplier_statuses}")

        district_map = {
            item["metric_key"]: item["label"]
            for item in payload.get("districts") or []
        }
        expected_district_map = dict(STOCK_REPORT_DISTRICTS)
        if district_map != expected_district_map:
            raise AssertionError(f"stock report must expose the supported compact district set, got {district_map}")
        if "stock_ru_far_siberia" in district_map:
            raise AssertionError(f"whole merged far-east bucket must stay excluded, got {district_map}")

        rows = payload.get("rows") or []
        if payload.get("row_count") != active_sku_count or len(rows) != active_sku_count:
            raise AssertionError(f"stock report must return all active SKU rows, got row_count={payload.get('row_count')}")
        if [int(item["nm_id"]) for item in rows[:4]] != nm_ids:
            raise AssertionError(f"stock report must preserve active config_v2 order by default, got {rows[:4]}")
        if any("breached_districts" in item for item in rows):
            raise AssertionError(f"old breached-list row contract must be removed, got {rows[:2]}")

        first_row = rows[0]
        if first_row["nomenclature_name"] != "Nomenclature Alpha" or not first_row["identity_label"].startswith("Nomenclature Alpha"):
            raise AssertionError(f"nomenclature name must enrich identity when active item exists, got {first_row}")
        if first_row["stock_total"] != 150.0:
            raise AssertionError(f"stock_total must come from yesterday_closed, not today_current, got {first_row}")
        if first_row["stock_wb"] != 150.0:
            raise AssertionError(f"stock_wb alias must preserve current stock_total semantics, got {first_row}")
        if first_row["supplier_production_qty"] != 21.0 or first_row["supplier_in_transit_qty"] != 9.0:
            raise AssertionError(f"supplier cycle quantities must split production and China transit by status, got {first_row}")
        if first_row["wb_supplies_inbound_qty"] != 48.0:
            raise AssertionError(
                "WB supplies inbound must count statuses 3/4/6 and other non-excluded statuses, "
                f"excluding 1/2/5 and excluded labels, got {first_row}"
            )
        if first_row["stock_ff"] != 33.0:
            raise AssertionError(f"stock_ff must come from current ФФ ledger balance, got {first_row}")
        if first_row["zero_district_count"] != 1:
            raise AssertionError(f"zero_district_count must count numeric zero only, got {first_row}")
        if first_row["promotion_participation"] is not True or first_row["promotion_participation_label"] != "Да":
            raise AssertionError(f"promo_participation >0 must map to Да, got {first_row}")
        if round(float(first_row["avg_sales_per_day"]), 2) != 13.33:
            raise AssertionError(f"availability-adjusted demand must skip low/anomalous days, got {first_row}")
        if first_row["diagnostics"]["sales"]["excluded_low_sales_day_count"] < 1:
            raise AssertionError(f"demand diagnostics must expose skipped low days, got {first_row}")
        if round(float(first_row["days_left_total"]), 2) != 11.25:
            raise AssertionError(f"days_left_total must be stock_total / avg_sales_per_day, got {first_row}")

        first_districts = {item["metric_key"]: item for item in first_row["districts"]}
        central = first_districts["stock_ru_central"]
        if central["stock"] != 70.0 or round(float(central["avg_daily_burn"]), 2) != 10.0:
            raise AssertionError(f"district stock/burn must come from historical depletion, got {central}")
        if round(float(central["days_left"]), 2) != 7.0:
            raise AssertionError(f"district days_left must be district_stock / avg_daily_burn, got {central}")
        northwest = first_districts["stock_ru_northwest"]
        if northwest["stock"] != 0.0 or northwest["days_left"] != 0.0:
            raise AssertionError(f"district zero stock must stay numeric zero and days_left zero when burn exists, got {northwest}")
        volga = first_districts["stock_ru_volga"]
        if volga["stock"] is not None or volga["days_left"] is not None:
            raise AssertionError(f"missing district stock must stay null/blank, got {volga}")

        second_row = rows[1]
        if second_row["promotion_participation"] is not False or second_row["promotion_participation_label"] != "Нет":
            raise AssertionError(f"promo_participation 0 must map to Нет, got {second_row}")
        if second_row["zero_district_count"] != 1:
            raise AssertionError(f"numeric zero in one district must be counted once, got {second_row}")
        if second_row["supplier_production_qty"] != 4.0 or second_row["supplier_in_transit_qty"] != 0.0:
            raise AssertionError(f"matched_by_compatibility production line must be included by nmId, got {second_row}")
        if second_row["wb_supplies_inbound_qty"] != 5.0 or second_row["stock_ff"] != -4.0:
            raise AssertionError(f"second row must expose WB inbound and negative ФФ ledger balance, got {second_row}")

        third_row = rows[2]
        if third_row["promotion_participation"] is not None or third_row["promotion_participation_label"] != "н/д":
            raise AssertionError(f"missing promo metric must stay unavailable, got {third_row}")
        if third_row["avg_sales_per_day"] is not None or third_row["days_left_total"] is not None:
            raise AssertionError(f"missing/zero denominator must keep API null, got {third_row}")
        if third_row["wb_supplies_inbound_qty"] != 0.0 or third_row["stock_ff"] != 0.0:
            raise AssertionError(f"missing WB/FF rows must render as numeric zero, got {third_row}")
        if third_row["supplier_production_qty"] != 0.0 or third_row["supplier_in_transit_qty"] != 6.0:
            raise AssertionError(f"in_transit supplier shipment must aggregate by internal_nm_id, got {third_row}")

        summary_row = payload.get("summary_row") or {}
        if summary_row.get("identity_label") != "Итого" or not summary_row.get("is_summary"):
            raise AssertionError(f"stock report must expose an Итого summary row, got {summary_row}")
        if summary_row.get("supplier_production_qty") != 25.0 or summary_row.get("supplier_in_transit_qty") != 15.0:
            raise AssertionError(f"summary row must aggregate supplier cycle columns, got {summary_row}")
        if summary_row.get("stock_ff") != 29.0 or summary_row.get("wb_supplies_inbound_qty") != 53.0:
            raise AssertionError(f"summary row must aggregate FF and WB supplies columns, got {summary_row}")
        if summary_row.get("stock_wb") != 500.0 or summary_row.get("stock_total") != 500.0:
            raise AssertionError(f"summary row must aggregate WB stock while preserving stock_total alias, got {summary_row}")
        expected_total_sales = sum(float(row["avg_sales_per_day"] or 0.0) for row in rows)
        if round(float(summary_row["avg_sales_per_day"]), 2) != round(expected_total_sales, 2):
            raise AssertionError(f"summary row must sum SKU daily demand, got {summary_row}")
        expected_days_left = 500.0 / expected_total_sales
        if round(float(summary_row["days_left_total"]), 2) != round(expected_days_left, 2):
            raise AssertionError(f"summary row must calculate total days from aggregate stock/demand, got {summary_row}")

        wb_summary = payload.get("wb_supplies_inbound_summary") or {}
        if wb_summary.get("records_excluded_by_status") != 4:
            raise AssertionError(f"WB summary must exclude accepted/planned/unplanned statuses and labels, got {wb_summary}")
        if wb_summary.get("included_status_ids") != [3, 4, 6, 99]:
            raise AssertionError(f"WB summary must include every non-excluded status id, got {wb_summary}")
        supplier_summary = payload.get("supplier_shipments_cycle_summary") or {}
        if supplier_summary.get("shipments_included") != 2 or supplier_summary.get("shipments_skipped_by_status") != 1:
            raise AssertionError(f"supplier cycle summary must include only production/in_transit shipments, got {supplier_summary}")
        if supplier_summary.get("lines_counted") != 4 or supplier_summary.get("lines_skipped_by_match_status") != 1:
            raise AssertionError(f"supplier cycle summary must count matched product lines and skip unmatched, got {supplier_summary}")

        period_2_payload = SheetVitrinaV1StockReportBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(sales_avg_period_days=2)
        period_2_first = (period_2_payload.get("rows") or [])[0]
        if round(float(period_2_first["avg_sales_per_day"]), 2) != 14.0:
            raise AssertionError(f"sales_avg_period_days must affect demand calculation, got {period_2_first}")
        if round(float(period_2_first["days_left_total"]), 2) == round(float(first_row["days_left_total"]), 2):
            raise AssertionError("different averaging periods must change days_left_total when demand differs")

        default_payload = SheetVitrinaV1StockReportBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build()
        if default_payload.get("sales_avg_period_days") != 14:
            raise AssertionError(f"missing sales_avg_period_days must default to supply default 14, got {default_payload}")

        override_payload = SheetVitrinaV1StockReportBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(as_of_date="2026-04-17", sales_avg_period_days=2)
        if override_payload.get("report_date") != "2026-04-17":
            raise AssertionError(f"explicit stock report as_of_date must override the default, got {override_payload}")
        override_first = (override_payload.get("rows") or [])[0]
        if override_first["stock_total"] != 160.0:
            raise AssertionError(f"explicit as_of_date must read exact requested closed-day stock, got {override_first}")

        strict_missing = SheetVitrinaV1StockReportBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(as_of_date="2026-04-14")
        if strict_missing.get("status") != "unavailable" or strict_missing.get("report_date") != "2026-04-14":
            raise AssertionError(f"explicit missing as_of_date must remain strict unavailable, got {strict_missing}")

        active_skus = list_active_sku_options(current_state.config_v2)
        if [item["nm_id"] for item in active_skus[:4]] != nm_ids:
            raise AssertionError(f"active SKU selector source must preserve enabled config_v2 order, got {active_skus[:4]}")
        selected_subset = {nm_ids[1]}
        filtered_rows = [row for row in rows if int(row["nm_id"]) in selected_subset]
        if [int(row["nm_id"]) for row in filtered_rows] != [nm_ids[1]]:
            raise AssertionError(f"SKU filter semantics must exclude deselected rows and keep selected rows, got {filtered_rows}")

        print("stock_report_status: ok ->", payload["status"])
        print("stock_report_rows: ok ->", payload["row_count"], "active SKU")
        print("stock_report_promo: ok -> Да / Нет / н/д")
        print("stock_report_supplier_cycle: ok -> production", first_row["supplier_production_qty"], "in_transit", first_row["supplier_in_transit_qty"])
        print("stock_report_wb_supplies: ok -> status exclusions 1/2/5, qty", first_row["wb_supplies_inbound_qty"])
        print("stock_report_stock_ff: ok -> ledger qty", first_row["stock_ff"])
        print("stock_report_summary_row: ok ->", summary_row["identity_label"], summary_row["stock_wb"])
        print("stock_report_demand: ok -> period 3 avg", first_row["avg_sales_per_day"])
        print("stock_report_district_days: ok -> central", central["days_left"])
        print("stock_report_override: ok ->", override_payload["report_date"])


def _seed_nomenclature(runtime: RegistryUploadDbBackedRuntime, nm_id: int) -> None:
    runtime.save_nomenclature_item(
        {
            "item_id": "stock_report_nom_alpha",
            "is_active": True,
            "our_sku": "alpha",
            "nm_id": nm_id,
            "nomenclature_name": "Nomenclature Alpha",
            "product_type": "other",
            "match_key": "other|alpha",
            "aliases": [],
            "compatible_models_text": "",
            "compatible_model_keys": [],
            "comment": "stock report smoke",
            "created_at": CAPTURED_AT,
            "updated_at": CAPTURED_AT,
        }
    )


def _seed_sales_history(runtime: RegistryUploadDbBackedRuntime, nm_ids: list[int]) -> None:
    values_by_nm = {
        nm_ids[0]: [3, 10, 11, 12, 1, 12, 1, 16],
        nm_ids[1]: [5, 0, 1, 20, 0, 22, 24, 26],
        nm_ids[2]: [0, 0, 0, 0, 0, 0, 0, 0],
        nm_ids[3]: [8, 8, 8, 8, 8, 8, 8, 8],
    }
    dates = [
        "2026-04-11",
        "2026-04-12",
        "2026-04-13",
        "2026-04-14",
        "2026-04-15",
        "2026-04-16",
        "2026-04-17",
        "2026-04-18",
    ]
    for index, snapshot_date in enumerate(dates):
        items = [
            SalesFunnelHistoryItem(
                date=snapshot_date,
                nm_id=nm_id,
                metric="orderCount",
                value=float(values[index]),
            )
            for nm_id, values in values_by_nm.items()
        ]
        runtime.save_temporal_source_snapshot(
            source_key="sales_funnel_history",
            snapshot_date=snapshot_date,
            captured_at=CAPTURED_AT,
            payload=SalesFunnelHistorySuccess(
                kind="success",
                date_from=snapshot_date,
                date_to=snapshot_date,
                count=len(items),
                items=items,
            ),
        )


def _seed_supplier_shipments(runtime: RegistryUploadDbBackedRuntime, nm_ids: list[int]) -> None:
    runtime.save_supplier_shipment(
        header=_supplier_shipment_header("stock-report-production", ORDER_STATUS_PRODUCTION, product_qty_total=125.0),
        lines=[
            _supplier_shipment_line("stock-report-production-1", nm_ids[0], 21.0, MATCH_STATUS_MATCHED, sort_order=1),
            _supplier_shipment_line("stock-report-production-2", nm_ids[1], 4.0, MATCH_STATUS_MATCHED_BY_COMPATIBILITY, sort_order=2),
            _supplier_shipment_line("stock-report-production-unmatched", None, 100.0, MATCH_STATUS_UNMATCHED, sort_order=3),
        ],
    )
    runtime.save_supplier_shipment(
        header=_supplier_shipment_header("stock-report-in-transit", ORDER_STATUS_IN_TRANSIT, product_qty_total=15.0),
        lines=[
            _supplier_shipment_line("stock-report-in-transit-1", nm_ids[0], 9.0, MATCH_STATUS_MATCHED, sort_order=1),
            _supplier_shipment_line("stock-report-in-transit-2", nm_ids[2], 6.0, MATCH_STATUS_MATCHED, sort_order=2),
        ],
    )
    runtime.save_supplier_shipment(
        header=_supplier_shipment_header("stock-report-accepted-ff", ORDER_STATUS_ACCEPTED_FF, product_qty_total=500.0),
        lines=[
            _supplier_shipment_line("stock-report-accepted-ff-1", nm_ids[0], 500.0, MATCH_STATUS_MATCHED, sort_order=1),
        ],
    )


def _supplier_shipment_header(shipment_id: str, order_status: str, *, product_qty_total: float) -> dict[str, object]:
    actual_shipment_date = ""
    actual_ff_acceptance_date = ""
    if order_status == ORDER_STATUS_IN_TRANSIT:
        actual_shipment_date = "2026-04-20"
    elif order_status == ORDER_STATUS_ACCEPTED_FF:
        actual_shipment_date = "2026-04-20"
        actual_ff_acceptance_date = "2026-04-22"
    return {
        "shipment_id": shipment_id,
        "created_at": CAPTURED_AT,
        "updated_at": CAPTURED_AT,
        "shipment_date": "2026-04-19",
        "actual_shipment_date": actual_shipment_date,
        "actual_ff_acceptance_date": actual_ff_acceptance_date,
        "order_status": order_status,
        "expenses_complete": False,
        "invoice_no": shipment_id,
        "invoice_date": "2026-04-19",
        "contract_no": "",
        "contract_date": "",
        "supplier_name": "HanShang Technology",
        "customer_name": "",
        "currency": "RMB",
        "product_qty_total": product_qty_total,
        "product_amount_total": product_qty_total,
        "extras_amount_total": 0.0,
        "invoice_amount_total": product_qty_total,
        "declared_invoice_total": product_qty_total,
        "match_status": "all_matched",
        "source_filename": f"{shipment_id}.xlsx",
        "source_file_sha256": "",
        "source_file_path": "",
        "parser_version": "stock_report_smoke",
        "warnings": [],
        "errors": [],
    }


def _supplier_shipment_line(
    line_id: str,
    nm_id: int | None,
    qty: float,
    match_status: str,
    *,
    sort_order: int,
) -> dict[str, object]:
    return {
        "line_id": line_id,
        "line_type": LINE_TYPE_PRODUCT,
        "sort_order": sort_order,
        "source_no": line_id,
        "product_type": "clear",
        "model_raw": "Stock report SKU",
        "model_normalized": "stock_report_sku",
        "match_key": "clear|stock_report_sku",
        "internal_sku": "SKU-" + str(nm_id or ""),
        "internal_nm_id": nm_id,
        "internal_name": "Stock report SKU",
        "qty": qty,
        "unit_price": 1.0,
        "amount": qty,
        "currency": "RMB",
        "comment": "",
        "match_status": match_status,
        "manual_override": False,
        "raw": {},
    }


def _seed_ff_stock_balances(runtime: RegistryUploadDbBackedRuntime, nm_ids: list[int]) -> None:
    runtime.create_ff_stock_operation(
        operation_id="stock_report_ff_seed",
        operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
        source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
        source_key="stock_report_smoke_ff_seed",
        source_object_id="stock_report_smoke_ff_seed",
        source_object_label="stock report smoke FF balance seed",
        created_at=CAPTURED_AT,
        created_by="smoke",
        lines=[
            {
                "nm_id": nm_ids[0],
                "sku": "alpha",
                "nomenclature_name": "Nomenclature Alpha",
                "quantity_delta": 33.0,
            },
            {
                "nm_id": nm_ids[1],
                "sku": "beta",
                "quantity_delta": -4.0,
            },
        ],
    )


def _seed_wb_supplies(runtime: RegistryUploadDbBackedRuntime, nm_ids: list[int]) -> None:
    rows = [
        _wb_supply_row("wb-status-3", 3, "Отгрузка разрешена", [{"nmID": nm_ids[0], "quantity": 7}, {"nmID": nm_ids[1], "quantity": 5}]),
        _wb_supply_row("wb-status-4", 4, "Идёт приёмка", [{"nmID": nm_ids[0], "quantity": 11}]),
        _wb_supply_row("wb-status-6", 6, "Отгружено на воротах", [{"nmID": nm_ids[0], "quantity": 13}]),
        _wb_supply_row("wb-status-99", 99, "На сортировке", [{"nmID": nm_ids[0], "quantity": 17}]),
        _wb_supply_row("wb-status-1", 1, "Не запланировано", [{"nmID": nm_ids[0], "quantity": 100}]),
        _wb_supply_row("wb-status-2", 2, "Запланировано", [{"nmID": nm_ids[0], "quantity": 100}]),
        _wb_supply_row("wb-status-5", 5, "Принято", [{"nmID": nm_ids[0], "quantity": 100}]),
        _wb_supply_row("wb-status-label-excluded", None, "Незапланировано", [{"nmID": nm_ids[0], "quantity": 100}]),
    ]
    runtime.save_wb_supply_rows(
        rows=rows,
        warehouses=[{"warehouse_id": "507", "warehouse_name": "Коледино"}],
        synced_at=CAPTURED_AT,
    )


def _wb_supply_row(
    supply_id: str,
    status_id: int | None,
    status_label: str,
    raw_goods: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "supply_id": supply_id,
        "cache_key": supply_id,
        "wb_supply_id": supply_id,
        "preorder_id": "pre-" + supply_id,
        "number_label": supply_id,
        "status_id": status_id,
        "status_label": status_label,
        "warehouse_id": "507",
        "warehouse_name": "Коледино",
        "warehouse_display": "Коледино",
        "supply_date": "2026-04-19",
        "quantity_for_size_filter": sum(float(item.get("quantity") or 0.0) for item in raw_goods),
        "raw_list": {"supplyID": supply_id, "statusID": status_id, "supplyDate": "2026-04-19"},
        "raw_detail": {"warehouseID": 507, "warehouseName": "Коледино"},
        "raw_goods": raw_goods,
        "raw_package": [],
    }


def _closed_sku_values(snapshot_date: str, nm_ids: list[int]) -> dict[int, dict[str, float]]:
    by_date = {
        "2026-04-15": {
            nm_ids[0]: {
                "stock_total": 180.0,
                "stock_ru_central": 100.0,
                "stock_ru_northwest": 3.0,
                "stock_ru_ural": 20.0,
                "stock_ru_south_caucasus": 16.0,
                "promo_participation": 1.0,
                "stock_ru_far_siberia": 1.0,
            },
            nm_ids[1]: {
                "stock_total": 140.0,
                "stock_ru_south_caucasus": 12.0,
                "promo_participation": 0.0,
            },
            nm_ids[2]: {"stock_total": 40.0},
            nm_ids[3]: {"stock_total": 240.0, "stock_ru_far_siberia": 0.0, "promo_participation": 2.0},
        },
        "2026-04-16": {
            nm_ids[0]: {
                "stock_total": 170.0,
                "stock_ru_central": 90.0,
                "stock_ru_northwest": 2.0,
                "stock_ru_ural": 15.0,
                "stock_ru_south_caucasus": 12.0,
                "promo_participation": 1.0,
            },
            nm_ids[1]: {
                "stock_total": 130.0,
                "stock_ru_south_caucasus": 8.0,
                "promo_participation": 0.0,
            },
            nm_ids[2]: {"stock_total": 40.0},
            nm_ids[3]: {"stock_total": 230.0, "stock_ru_far_siberia": 0.0, "promo_participation": 2.0},
        },
        "2026-04-17": {
            nm_ids[0]: {
                "stock_total": 160.0,
                "stock_ru_central": 80.0,
                "stock_ru_northwest": 1.0,
                "stock_ru_ural": 10.0,
                "stock_ru_south_caucasus": 9.0,
                "promo_participation": 1.0,
            },
            nm_ids[1]: {
                "stock_total": 120.0,
                "stock_ru_south_caucasus": 4.0,
                "promo_participation": 0.0,
            },
            nm_ids[2]: {"stock_total": 40.0},
            nm_ids[3]: {"stock_total": 220.0, "stock_ru_far_siberia": 0.0, "promo_participation": 2.0},
        },
        "2026-04-18": {
            nm_ids[0]: {
                "stock_total": 150.0,
                "stock_ru_central": 70.0,
                "stock_ru_northwest": 0.0,
                "stock_ru_ural": 5.0,
                "stock_ru_south_caucasus": 7.0,
                "promo_participation": 1.0,
                "stock_ru_far_siberia": 0.0,
            },
            nm_ids[1]: {
                "stock_total": 100.0,
                "stock_ru_south_caucasus": 0.0,
                "promo_participation": 0.0,
            },
            nm_ids[2]: {"stock_total": 40.0},
            nm_ids[3]: {
                "stock_total": 210.0,
                "stock_ru_central": 70.0,
                "stock_ru_northwest": 60.0,
                "stock_ru_volga": 80.0,
                "stock_ru_ural": 90.0,
                "stock_ru_south_caucasus": 100.0,
                "stock_ru_far_siberia": 0.0,
                "promo_participation": 2.0,
            },
        },
    }
    return by_date[snapshot_date]


def _build_plan(
    *,
    as_of_date: str,
    current_state: object,
    metric_labels: dict[str, str],
    closed_sku_values: dict[int, dict[str, float]],
    today_sku_values: dict[int, dict[str, float]],
) -> SheetVitrinaV1Envelope:
    rows = []
    today_date = {
        "2026-04-15": "2026-04-16",
        "2026-04-16": "2026-04-17",
        "2026-04-17": "2026-04-18",
        "2026-04-18": "2026-04-19",
    }[as_of_date]
    for config_item in current_state.config_v2:
        if not config_item.enabled:
            continue
        closed_values = closed_sku_values.get(config_item.nm_id, {})
        today_values = today_sku_values.get(config_item.nm_id, {})
        for metric_key in [
            "stock_total",
            "stock_ru_central",
            "stock_ru_northwest",
            "stock_ru_volga",
            "stock_ru_ural",
            "stock_ru_south_caucasus",
            "stock_ru_far_siberia",
            "promo_participation",
        ]:
            if metric_key not in today_values and metric_key not in closed_values:
                continue
            rows.append(
                [
                    f"{config_item.display_name}: {metric_labels.get(metric_key, metric_key)}",
                    f"SKU:{config_item.nm_id}|{metric_key}",
                    closed_values.get(metric_key, ""),
                    today_values.get(metric_key, ""),
                ]
            )

    data_header = ["label", "key", as_of_date, today_date]
    temporal_slots = [
        SheetVitrinaV1TemporalSlot(
            slot_key="yesterday_closed",
            slot_label="Вчера (закрытый день)",
            column_date=as_of_date,
        ),
        SheetVitrinaV1TemporalSlot(
            slot_key="today_current",
            slot_label="Сегодня (текущий день)",
            column_date=today_date,
        ),
    ]
    return SheetVitrinaV1Envelope(
        plan_version="sheet_vitrina_v1_temporal_live_v1__sheet_scaffold_v1",
        snapshot_id=f"stock-report-smoke-{uuid4().hex}",
        as_of_date=as_of_date,
        date_columns=[as_of_date, today_date],
        temporal_slots=temporal_slots,
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:D{len(rows) + 1}",
                clear_range="A:Z",
                write_mode="replace",
                partial_update_allowed=False,
                header=data_header,
                rows=rows,
                row_count=len(rows),
                column_count=len(data_header),
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K1",
                clear_range="A:K",
                write_mode="replace",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[],
                row_count=0,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


if __name__ == "__main__":
    main()
