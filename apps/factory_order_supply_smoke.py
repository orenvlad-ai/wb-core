"""Targeted smoke-check for the factory-order supply block."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.factory_order_supply import FactoryOrderSupplyBlock
from packages.application.factory_order_sales_history import persist_sales_history_result_exact_dates
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_live_plan import STATUS_HEADER
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.application.stock_ff_onec_source import ONEC_FF_STOCK_QTY_METRIC_KEY
from packages.contracts.factory_order_supply import (
    DATASET_INBOUND_FACTORY_TO_FF,
    DATASET_INBOUND_FF_TO_WB,
    DATASET_STOCK_FF,
    STOCK_FF_SOURCE_ONEC_FF_STOCK,
)
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryItem, SalesFunnelHistorySuccess
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
ACTIVATED_AT = "2026-04-18T09:00:00Z"

SALES_BY_DATE = {
    "2026-03-28": {210183919: 10.0, 210184534: 1.0},
    "2026-03-29": {210183919: 20.0, 210184534: 2.0},
    "2026-03-30": {210183919: 30.0, 210184534: 3.0},
    "2026-03-31": {210183919: 40.0, 210184534: 4.0},
    "2026-04-01": {210183919: 50.0, 210184534: 5.0},
    "2026-04-02": {210183919: 60.0, 210184534: 6.0},
    "2026-04-03": {210183919: 70.0, 210184534: 7.0},
    "2026-04-04": {210183919: 72.0, 210184534: 11.0},
    "2026-04-05": {210183919: 74.0, 210184534: 12.0},
    "2026-04-06": {210183919: 76.0, 210184534: 13.0},
    "2026-04-07": {210183919: 78.0, 210184534: 14.0},
    "2026-04-08": {210183919: 80.0, 210184534: 15.0},
    "2026-04-09": {210183919: 90.0, 210184534: 18.0},
    "2026-04-10": {210183919: 100.0, 210184534: 21.0},
    "2026-04-11": {210183919: 110.0, 210184534: 24.0},
    "2026-04-12": {210183919: 120.0, 210184534: 27.0},
    "2026-04-13": {210183919: 130.0, 210184534: 30.0},
    "2026-04-14": {210183919: 140.0, 210184534: 33.0},
    "2026-04-15": {210183919: 150.0, 210184534: 36.0},
    "2026-04-16": {210183919: 160.0, 210184534: 39.0},
    "2026-04-17": {210183919: 170.0, 210184534: 42.0},
}


class FakeStocksBlock:
    def __init__(self, nm_ids: list[int]) -> None:
        self.nm_ids = list(nm_ids)

    def execute(self, request_obj: object) -> SimpleNamespace:
        items = []
        for nm_id in self.nm_ids:
            stock_total = 0.0
            if nm_id == 210183919:
                stock_total = 100.0
            elif nm_id == 210184534:
                stock_total = 20.0
            items.append(SimpleNamespace(nm_id=nm_id, stock_total=stock_total))
        return SimpleNamespace(result=SimpleNamespace(kind="success", items=items))


class FakeSalesHistoryBlock:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute(self, request_obj: object) -> SimpleNamespace:
        self.calls.append((str(request_obj.date_from), str(request_obj.date_to)))
        start = date.fromisoformat(request_obj.date_from)
        end = date.fromisoformat(request_obj.date_to)
        items = []
        current = start
        while current <= end:
            lookup = SALES_BY_DATE.get(current.isoformat(), {})
            for nm_id in request_obj.nm_ids:
                items.append(
                    SimpleNamespace(
                        date=current.isoformat(),
                        nm_id=nm_id,
                        metric="orderCount",
                        value=float(lookup.get(int(nm_id), 0.0)),
                    )
                )
            current += timedelta(days=1)
        return SimpleNamespace(result=SimpleNamespace(kind="success", items=items))


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="factory-order-supply-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        active_nm_ids = [item.nm_id for item in runtime.load_current_state().config_v2 if item.enabled]
        _seed_runtime_sales_history(runtime, active_nm_ids=active_nm_ids, missing_dates=set())
        history_block = FakeSalesHistoryBlock()
        block = FactoryOrderSupplyBlock(
            runtime=runtime,
            stocks_block=FakeStocksBlock(active_nm_ids),
            sales_funnel_history_block=history_block,
            now_factory=lambda: NOW,
            timestamp_factory=lambda: ACTIVATED_AT,
        )

        stock_template, stock_template_name = block.build_template(DATASET_STOCK_FF)
        stock_rows = read_first_sheet_rows(stock_template)
        if stock_rows[0] != ["nmId", "Комментарий SKU", "Остаток ФФ", "Дата остатка", "Комментарий"]:
            raise AssertionError("stock_ff template must use Russian headers")
        if len(stock_rows) - 1 != len(active_nm_ids):
            raise AssertionError("stock_ff template must be prefilled with active SKU rows")
        if stock_template_name != "sheet-vitrina-v1-factory-order-stock-ff-template.xlsx":
            raise AssertionError("stock_ff template filename changed unexpectedly")

        inbound_template, _ = block.build_template(DATASET_INBOUND_FACTORY_TO_FF)
        inbound_rows = read_first_sheet_rows(inbound_template)
        if inbound_rows[0] != [
            "nmId",
            "Комментарий SKU",
            "Количество в пути",
            "Планируемая дата прихода на ФФ",
            "Комментарий",
            "Поставка",
        ]:
            raise AssertionError("factory inbound template must expose shipment summary headers")

        invalid_headers_blob = build_single_sheet_workbook_bytes("Bad", [["sku", "qty"], ["210183919", "10"]])
        try:
            block.upload_dataset(DATASET_STOCK_FF, invalid_headers_blob)
        except ValueError as exc:
            if "Неверные заголовки" not in str(exc):
                raise
        else:
            raise AssertionError("upload must reject invalid headers")

        duplicate_stock_rows = [list(row) for row in stock_rows[:4]]
        duplicate_stock_rows[1][2] = 30
        duplicate_stock_rows[2][0] = duplicate_stock_rows[1][0]
        duplicate_stock_rows[2][2] = 10
        try:
            block.upload_dataset(
                DATASET_STOCK_FF,
                build_single_sheet_workbook_bytes("Остатки ФФ", duplicate_stock_rows),
            )
        except ValueError as exc:
            if "повторяющийся nmId" not in str(exc):
                raise
        else:
            raise AssertionError("stock_ff upload must reject repeated nmId")

        stock_upload_rows = [list(row) for row in stock_rows]
        for row in stock_upload_rows[1:]:
            row[2] = 0
        stock_upload_rows[1][2] = 30
        stock_upload_rows[2][2] = 10
        stock_upload = block.upload_dataset(
            DATASET_STOCK_FF,
            build_single_sheet_workbook_bytes("Остатки ФФ", stock_upload_rows),
            uploaded_filename="factory-stock-input.xlsx",
        )
        if stock_upload.accepted_row_count != len(active_nm_ids):
            raise AssertionError("stock_ff upload must accept one row per active SKU")
        if stock_upload.dataset.uploaded_filename != "factory-stock-input.xlsx":
            raise AssertionError("stock_ff upload must keep the original uploaded filename")

        downloaded_stock_bytes, downloaded_stock_name, downloaded_stock_type = block.download_uploaded_dataset(DATASET_STOCK_FF)
        if downloaded_stock_name != "factory-stock-input.xlsx":
            raise AssertionError("current uploaded stock_ff file must be downloadable by its stored filename")
        if downloaded_stock_type != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            raise AssertionError("uploaded stock_ff content type must stay XLSX")
        if read_first_sheet_rows(downloaded_stock_bytes)[1][0] != stock_rows[1][0]:
            raise AssertionError("downloaded stock_ff file must preserve the uploaded workbook content")

        with TemporaryDirectory(prefix="factory-order-onec-stock-ff-") as onec_tmp:
            onec_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(onec_tmp) / "runtime")
            onec_runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
            onec_active_nm_ids = [item.nm_id for item in onec_runtime.load_current_state().config_v2 if item.enabled]
            _seed_runtime_sales_history(onec_runtime, active_nm_ids=onec_active_nm_ids, missing_dates=set())
            _seed_onec_ff_stock_ready_snapshot(
                onec_runtime,
                stock_by_nm={
                    onec_active_nm_ids[0]: 17.0,
                    onec_active_nm_ids[1]: 0.0,
                    **{nm_id: 0.0 for nm_id in onec_active_nm_ids[2:]},
                },
            )
            onec_block = FactoryOrderSupplyBlock(
                runtime=onec_runtime,
                stocks_block=FakeStocksBlock(onec_active_nm_ids),
                sales_funnel_history_block=FakeSalesHistoryBlock(),
                now_factory=lambda: NOW,
                timestamp_factory=lambda: ACTIVATED_AT,
            )
            onec_check = onec_block.build_onec_stock_ff_check()
            if onec_check.status != "ready" or onec_check.positive_stock_sku_count != 1 or onec_check.zero_stock_sku_count != len(onec_active_nm_ids) - 1:
                raise AssertionError(f"1C FF_STOCK check must treat zero qty as covered, got {onec_check}")
            onec_result = onec_block.calculate(
                {
                    "prod_lead_time_days": 10,
                    "lead_time_factory_to_ff_days": 5,
                    "lead_time_ff_to_wb_days": 2,
                    "safety_days_mp": 3,
                    "safety_days_ff": 2,
                    "cycle_order_days": 14,
                    "order_batch_qty": 50,
                    "report_date_override": "2026-04-18",
                    "sales_avg_period_days": 3,
                    "stock_ff_source": STOCK_FF_SOURCE_ONEC_FF_STOCK,
                }
            )
            onec_rows = {item.nm_id: item for item in onec_result.rows}
            if onec_result.stock_ff_source != STOCK_FF_SOURCE_ONEC_FF_STOCK:
                raise AssertionError("1C stock_ff source must be persisted in calculation result")
            if onec_rows[onec_active_nm_ids[0]].stock_ff != 17.0:
                raise AssertionError("1C source must use onec_FF_STOCK_qty, not onec_total_qty")
            if onec_rows[onec_active_nm_ids[1]].stock_ff != 0.0:
                raise AssertionError("1C source must treat explicit zero FF_STOCK as valid stock_ff=0")
            if onec_result.manual_stock_ff_dataset.status != "missing":
                raise AssertionError("1C source must not require or write a manual stock_ff upload")

            partial_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(onec_tmp) / "partial-runtime")
            partial_runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
            partial_active_nm_ids = [item.nm_id for item in partial_runtime.load_current_state().config_v2 if item.enabled]
            _seed_runtime_sales_history(partial_runtime, active_nm_ids=partial_active_nm_ids, missing_dates=set())
            _seed_onec_ff_stock_ready_snapshot(
                partial_runtime,
                stock_by_nm={partial_active_nm_ids[0]: 17.0},
                omit_nm_ids=set(partial_active_nm_ids[1:]),
            )
            partial_block = FactoryOrderSupplyBlock(
                runtime=partial_runtime,
                stocks_block=FakeStocksBlock(partial_active_nm_ids),
                sales_funnel_history_block=FakeSalesHistoryBlock(),
                now_factory=lambda: NOW,
                timestamp_factory=lambda: ACTIVATED_AT,
            )
            try:
                partial_block.calculate(
                    {
                        "prod_lead_time_days": 10,
                        "lead_time_factory_to_ff_days": 5,
                        "lead_time_ff_to_wb_days": 2,
                        "safety_days_mp": 3,
                        "safety_days_ff": 2,
                        "cycle_order_days": 14,
                        "order_batch_qty": 50,
                        "report_date_override": "2026-04-18",
                        "sales_avg_period_days": 3,
                        "stock_ff_source": STOCK_FF_SOURCE_ONEC_FF_STOCK,
                    }
                )
            except ValueError as exc:
                if "1С / Фулфилмент" not in str(exc) or "does not cover active SKU" not in str(exc):
                    raise
            else:
                raise AssertionError("partial 1C FF_STOCK active SKU coverage must block calculation")

        # Scenario 1: calculate without any inbound files.
        result_without_inbound = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "cycle_order_days": 14,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 3,
            }
        )
        by_nm_id = {item.nm_id: item for item in result_without_inbound.rows}
        sku_one = by_nm_id[210183919]
        sku_two = by_nm_id[210184534]
        if round(sku_one.daily_demand_total, 2) != _expected_average(210183919, report_date="2026-04-18", period_days=3):
            raise AssertionError("3-day lookback must average only the last 3 closed days")
        if round(sku_one.coverage_qty, 2) != 130.0:
            raise AssertionError("coverage without inbound files must include only stock_total_mp + stock_ff")
        if sku_one.inbound_factory_to_ff != 0.0 or sku_one.inbound_ff_to_wb != 0.0:
            raise AssertionError("missing inbound files must be treated as zero, not as blocking input")
        if round(sku_two.target_qty, 2) != _expected_target_qty(
            210184534,
            report_date="2026-04-18",
            period_days=3,
            prod_lead_time_days=10,
            lead_time_factory_to_ff_days=5,
            lead_time_ff_to_wb_days=2,
            safety_days_mp=3,
            safety_days_ff=2,
            cycle_order_days=14,
        ):
            raise AssertionError("cycle_order_days must be part of the factory target_qty")
        if sku_two.recommended_order_qty != _expected_recommended_order_qty(
            210184534,
            report_date="2026-04-18",
            period_days=3,
            prod_lead_time_days=10,
            lead_time_factory_to_ff_days=5,
            lead_time_ff_to_wb_days=2,
            safety_days_mp=3,
            safety_days_ff=2,
            cycle_order_days=14,
            stock_total_mp=20.0,
            stock_ff=10.0,
            inbound_factory_to_ff=0.0,
            inbound_ff_to_wb=0.0,
            order_batch_qty=50,
        ):
            raise AssertionError("recommended qty without inbound files must still round by box multiple after cycle extension")

        # Scenario 2: zero-only inbound files are accepted as an empty dataset.
        inbound_factory_zero_upload = block.upload_dataset(
            DATASET_INBOUND_FACTORY_TO_FF,
            build_single_sheet_workbook_bytes(
                "В пути от фабрики",
                [
                    inbound_rows[0][:-1],
                    [210183919, "SKU 1", 0, "", ""],
                    [210184534, "SKU 2", 0, "", ""],
                ],
            ),
            uploaded_filename="factory-inbound-zero.xlsx",
        )
        if inbound_factory_zero_upload.accepted_row_count != 0 or inbound_factory_zero_upload.ignored_row_count != 2:
            raise AssertionError("zero-only factory inbound upload must be accepted as an empty dataset")
        if inbound_factory_zero_upload.shipment_summary:
            raise AssertionError("zero-only factory inbound upload must expose an empty shipment summary")

        inbound_ff_to_wb_zero_upload = block.upload_dataset(
            DATASET_INBOUND_FF_TO_WB,
            build_single_sheet_workbook_bytes(
                "В пути ФФ -> WB",
                [
                    ["nmId", "Комментарий SKU", "Количество в пути", "Планируемая дата прихода на Wildberries", "Комментарий"],
                    [210183919, "SKU 1", 0, "", ""],
                    [210184534, "SKU 2", 0, "", ""],
                ],
            ),
            uploaded_filename="ff-to-wb-zero.xlsx",
        )
        if inbound_ff_to_wb_zero_upload.accepted_row_count != 0 or inbound_ff_to_wb_zero_upload.ignored_row_count != 2:
            raise AssertionError("zero-only ff_to_wb upload must be accepted as an empty dataset")

        zero_only_status = block.build_status()
        if zero_only_status.datasets[DATASET_INBOUND_FACTORY_TO_FF].row_count != 0:
            raise AssertionError("accepted zero-only inbound_factory upload must persist as row_count=0")
        if zero_only_status.datasets[DATASET_INBOUND_FF_TO_WB].row_count != 0:
            raise AssertionError("accepted zero-only inbound_ff_to_wb upload must persist as row_count=0")

        result_zero_only_inbound = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "cycle_order_days": 14,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 3,
            }
        )
        zero_only_sku = {item.nm_id: item for item in result_zero_only_inbound.rows}[210183919]
        if zero_only_sku.inbound_factory_to_ff != 0.0 or zero_only_sku.inbound_ff_to_wb != 0.0:
            raise AssertionError("zero-only inbound uploads must keep coverage terms at zero")

        # Scenario 3: upload both inbound files with zero rows mixed in and keep only the positive events inside horizon.
        inbound_factory_upload = block.upload_dataset(
            DATASET_INBOUND_FACTORY_TO_FF,
            build_single_sheet_workbook_bytes(
                "В пути от фабрики",
                [
                    inbound_rows[0],
                    [210183919, "SKU 1", 5, "2026-04-10", "", "Старая поставка"],
                    [210183919, "SKU 1", 40, "2026-04-25", "", "Поставка A"],
                    [210184534, "SKU 2", 15, "2026-04-25", "", "Поставка A"],
                    [210184534, "SKU 2", 0, "", "", ""],
                    [210183919, "SKU 1", 12, "2026-05-05", "", ""],
                    [210183919, "SKU 1", 999, "2026-05-25", "", "Поздняя поставка"],
                ],
            ),
            uploaded_filename="factory-inbound.xlsx",
        )
        if inbound_factory_upload.accepted_row_count != 5 or inbound_factory_upload.ignored_row_count != 1:
            raise AssertionError("factory inbound upload must ignore zero rows and keep positive rows grouped by shipment")
        shipment_summary = list(inbound_factory_upload.shipment_summary)
        if len(shipment_summary) != 4:
            raise AssertionError("factory inbound upload must expose all shipment summary rows independently from calc window")
        if (
            shipment_summary[1].shipment != "Поставка A"
            or shipment_summary[1].total_quantity != 55.0
            or shipment_summary[1].acceptance_date != "2026-04-25"
        ):
            raise AssertionError("explicit factory shipment summary must aggregate quantities by shipment/date")
        if (
            shipment_summary[2].shipment != "Поставка №1"
            or shipment_summary[2].total_quantity != 12.0
            or shipment_summary[2].acceptance_date != "2026-05-05"
        ):
            raise AssertionError("factory shipment summary must label missing shipment ids by row group order")
        persisted_summary = list(block.build_status().datasets[DATASET_INBOUND_FACTORY_TO_FF].shipment_summary)
        if [item.total_quantity for item in persisted_summary] != [5.0, 55.0, 12.0, 999.0]:
            raise AssertionError("factory status must expose persisted shipment summary totals")

        inbound_ff_to_wb_upload = block.upload_dataset(
            DATASET_INBOUND_FF_TO_WB,
            build_single_sheet_workbook_bytes(
                "В пути ФФ -> WB",
                [
                    ["nmId", "Комментарий SKU", "Количество в пути", "Планируемая дата прихода на Wildberries", "Комментарий"],
                    [210183919, "SKU 1", 6, "2026-04-17", ""],
                    [210183919, "SKU 1", 10, "2026-04-26", ""],
                    [210183919, "SKU 1", 0, "", ""],
                    [210183919, "SKU 1", 7, "2026-05-10", ""],
                    [210183919, "SKU 1", 888, "2026-05-25", ""],
                    [210184534, "SKU 2", 25, "2026-04-28", ""],
                ],
            ),
            uploaded_filename="ff-to-wb.xlsx",
        )
        if inbound_ff_to_wb_upload.accepted_row_count != 5 or inbound_ff_to_wb_upload.ignored_row_count != 1:
            raise AssertionError("ff_to_wb upload must ignore zero rows and keep positive rows")

        result_with_inbound = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "cycle_order_days": 14,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 7,
            }
        )
        by_nm_id = {item.nm_id: item for item in result_with_inbound.rows}
        sku_one = by_nm_id[210183919]
        sku_two = by_nm_id[210184534]
        if result_with_inbound.target_window_days != 36 or result_with_inbound.inbound_window_end != "2026-05-24":
            raise AssertionError("factory inbound upper bound must use the full target window, not only logistics days")
        if round(sku_one.daily_demand_total, 2) != _expected_average(210183919, report_date="2026-04-18", period_days=7):
            raise AssertionError("7-day lookback must change the average demand relative to 3-day lookback")
        if round(sku_one.inbound_factory_to_ff, 2) != 52.0:
            raise AssertionError("factory inbound must skip old rows, count inside full target window, and skip after target window")
        if round(sku_one.inbound_ff_to_wb, 2) != 17.0:
            raise AssertionError("ff_to_wb inbound must use report_date..target_window_end bounds")
        if round(sku_two.inbound_ff_to_wb, 2) != 25.0:
            raise AssertionError("ff_to_wb uploaded rows must contribute to coverage")
        effective_factory_rows = [
            (item.planned_arrival_date, item.effective_arrival_date, item.quantity)
            for item in result_with_inbound.effective_inbound_factory_to_ff
            if item.nm_id == 210183919
        ]
        if effective_factory_rows != [("2026-04-25", "2026-04-27", 40.0), ("2026-05-05", "2026-05-07", 12.0)]:
            raise AssertionError(f"factory inbound effective date must add ff_to_wb lead time and enforce lower/upper bounds, got {effective_factory_rows}")
        if sku_one.recommended_order_qty != _expected_recommended_order_qty(
            210183919,
            report_date="2026-04-18",
            period_days=7,
            prod_lead_time_days=10,
            lead_time_factory_to_ff_days=5,
            lead_time_ff_to_wb_days=2,
            safety_days_mp=3,
            safety_days_ff=2,
            cycle_order_days=14,
            stock_total_mp=100.0,
            stock_ff=30.0,
            inbound_factory_to_ff=52.0,
            inbound_ff_to_wb=17.0,
            order_batch_qty=50,
        ):
            raise AssertionError("recommended qty must reflect corrected inbound coverage and still round by order_batch_qty")

        recommendation_bytes, _ = block.download_recommendation()
        recommendation_rows = read_first_sheet_rows(recommendation_bytes)
        if recommendation_rows[0] != ["nmId", "Комментарий SKU", "Рекомендовано к заказу"]:
            raise AssertionError("recommendation workbook must use Russian headers")
        summary_rows = recommendation_rows[-3:]
        if summary_rows[0][0] != "Общее количество" or summary_rows[1][0] != "Расчётный вес":
            raise AssertionError("recommendation workbook summary must stay operator-facing and Russian")

        # Scenario 4: delete inbound files and verify recalculation falls back to zero.
        delete_inbound_factory = block.delete_dataset(DATASET_INBOUND_FACTORY_TO_FF)
        delete_inbound_ff_to_wb = block.delete_dataset(DATASET_INBOUND_FF_TO_WB)
        if delete_inbound_factory.status != "deleted" or delete_inbound_ff_to_wb.status != "deleted":
            raise AssertionError("uploaded inbound files must be deletable")
        try:
            block.download_uploaded_dataset(DATASET_INBOUND_FACTORY_TO_FF)
        except ValueError as exc:
            if "отсутствует" not in str(exc):
                raise
        else:
            raise AssertionError("deleted inbound file must no longer be downloadable")

        result_after_delete = block.calculate(
            {
                "prod_lead_time_days": 8,
                "lead_time_factory_to_ff_days": 4,
                "lead_time_ff_to_wb_days": 3,
                "safety_days_mp": 1,
                "safety_days_ff": 1,
                "cycle_order_days": 14,
                "order_batch_qty": 25,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 3,
            }
        )
        sku_one_after_delete = {item.nm_id: item for item in result_after_delete.rows}[210183919]
        if sku_one_after_delete.inbound_factory_to_ff != 0.0 or sku_one_after_delete.inbound_ff_to_wb != 0.0:
            raise AssertionError("after delete the inbound coverage terms must return to zero")
        if sku_one_after_delete.recommended_order_qty % 25 != 0:
            raise AssertionError("box multiple must still be applied after inbound deletion")

        # Scenario 5: supplier registry can be selected instead of the manual factory inbound file.
        supplier_empty_result = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "cycle_order_days": 14,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 3,
                "factory_inbound_source": "supplier_registry",
            }
        )
        supplier_empty_sku = {item.nm_id: item for item in supplier_empty_result.rows}[210183919]
        if supplier_empty_result.factory_inbound_source != "supplier_registry":
            raise AssertionError("supplier registry source selection must be persisted in calculation result")
        if supplier_empty_sku.inbound_factory_to_ff != 0.0:
            raise AssertionError("empty supplier registry source must not fall back to stale manual factory inbound rows")
        if not supplier_empty_result.warnings:
            raise AssertionError("supplier registry source with zero usable rows must expose a truthful warning")

        _seed_supplier_factory_inbound_fixture(runtime)
        supplier_source_result = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "cycle_order_days": 14,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 3,
                "factory_inbound_source": "supplier_registry",
            }
        )
        supplier_summary = supplier_source_result.supplier_registry_inbound_summary
        supplier_sku = {item.nm_id: item for item in supplier_source_result.rows}[210183919]
        if round(supplier_sku.inbound_factory_to_ff, 2) != 33.0:
            raise AssertionError("supplier registry source must use only matched product line qty inside the calculation window")
        if supplier_summary.diagnostics.usable_line_count != 2 or round(supplier_summary.diagnostics.usable_quantity, 2) != 133.0:
            raise AssertionError("supplier registry diagnostics must count matched usable source lines before calc-window filtering")
        if supplier_summary.diagnostics.unmatched_line_count != 1 or supplier_summary.diagnostics.ambiguous_line_count != 1:
            raise AssertionError("supplier registry diagnostics must surface unmatched and ambiguous lines")
        effective_supplier_rows = [
            (item.shipment_name, item.planned_arrival_date, item.effective_arrival_date, item.quantity)
            for item in supplier_source_result.effective_inbound_factory_to_ff
            if item.nm_id == 210183919
        ]
        if effective_supplier_rows != [("26GN390", "2026-05-20", "2026-05-22", 33.0)]:
            raise AssertionError(f"supplier registry inbound must use shipment_date + 30 days and then ff_to_wb lead time, got {effective_supplier_rows}")

        # Scenario 6: a different report date and box multiple must change the recommendation math.
        shifted_result = block.calculate(
            {
                "prod_lead_time_days": 4,
                "lead_time_factory_to_ff_days": 3,
                "lead_time_ff_to_wb_days": 1,
                "safety_days_mp": 1,
                "safety_days_ff": 0,
                "cycle_order_days": 14,
                "order_batch_qty": 25,
                "report_date_override": "2026-04-16",
                "sales_avg_period_days": 2,
            }
        )
        shifted_rows = {item.nm_id: item for item in shifted_result.rows}
        shifted_sku_one = shifted_rows[210183919]
        shifted_sku_two = shifted_rows[210184534]
        if round(shifted_sku_one.daily_demand_total, 2) != _expected_average(210183919, report_date="2026-04-16", period_days=2):
            raise AssertionError("shifted report date must recalculate the average demand on the selected closed-day window")
        if shifted_sku_one.recommended_order_qty != _expected_recommended_order_qty(
            210183919,
            report_date="2026-04-16",
            period_days=2,
            prod_lead_time_days=4,
            lead_time_factory_to_ff_days=3,
            lead_time_ff_to_wb_days=1,
            safety_days_mp=1,
            safety_days_ff=0,
            cycle_order_days=14,
            stock_total_mp=100.0,
            stock_ff=30.0,
            inbound_factory_to_ff=0.0,
            inbound_ff_to_wb=0.0,
            order_batch_qty=25,
        ):
            raise AssertionError("shifted scenario must keep the exact 25-piece box multiple for SKU 1 after cycle extension")
        if shifted_sku_two.recommended_order_qty != _expected_recommended_order_qty(
            210184534,
            report_date="2026-04-16",
            period_days=2,
            prod_lead_time_days=4,
            lead_time_factory_to_ff_days=3,
            lead_time_ff_to_wb_days=1,
            safety_days_mp=1,
            safety_days_ff=0,
            cycle_order_days=14,
            stock_total_mp=20.0,
            stock_ff=10.0,
            inbound_factory_to_ff=0.0,
            inbound_ff_to_wb=0.0,
            order_batch_qty=25,
        ):
            raise AssertionError("shifted scenario must round SKU 2 up to the next 25-piece box multiple after cycle extension")

        # Scenario 7: stable covered windows keep the calendar average; ramp-up windows expose adjusted diagnostics.
        for period_days in (10, 14):
            covered_result = block.calculate(
                {
                    "prod_lead_time_days": 10,
                    "lead_time_factory_to_ff_days": 5,
                    "lead_time_ff_to_wb_days": 2,
                    "safety_days_mp": 3,
                    "safety_days_ff": 2,
                    "cycle_order_days": 14,
                    "order_batch_qty": 50,
                    "report_date_override": "2026-04-18",
                    "sales_avg_period_days": period_days,
                }
            )
            covered_sku = {item.nm_id: item for item in covered_result.rows}[210183919]
            if round(covered_sku.daily_demand_total, 2) != _expected_average(
                210183919,
                report_date="2026-04-18",
                period_days=period_days,
            ):
                raise AssertionError(f"{period_days}-day lookback must use the exact covered runtime window")
            if covered_sku.demand_warning:
                raise AssertionError(f"{period_days}-day stable window must not produce a demand warning")

        ramp_up_result = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "cycle_order_days": 14,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 21,
            }
        )
        ramp_up_sku = {item.nm_id: item for item in ramp_up_result.rows}[210183919]
        if ramp_up_sku.valid_sales_day_count != 18 or "Собрано 18 валидных" not in ramp_up_sku.demand_warning:
            raise AssertionError("21-day ramp-up fixture must expose insufficient valid-day diagnostics")

        default_period_result = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
            }
        )
        default_sku = {item.nm_id: item for item in default_period_result.rows}[210183919]
        if round(default_sku.daily_demand_total, 2) != _expected_average(
            210183919,
            report_date="2026-04-18",
            period_days=14,
        ):
            raise AssertionError("missing sales_avg_period_days must fall back to the current 14-day window")
        if default_period_result.settings.sales_avg_period_days != 14:
            raise AssertionError("last successful calc must persist the current 14-day default window")
        if default_period_result.settings.cycle_order_days != 14:
            raise AssertionError("last successful calc must persist the current 14-day cycle_order_days default")

        cycle_short_result = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "cycle_order_days": 14,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 14,
            }
        )
        cycle_long_result = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "cycle_order_days": 28,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 14,
            }
        )
        if cycle_long_result.summary.total_qty <= cycle_short_result.summary.total_qty:
            raise AssertionError("larger cycle_order_days must materially increase factory total_qty")

        insufficient_history_result = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "cycle_order_days": 14,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 60,
            }
        )
        insufficient_sku = {item.nm_id: item for item in insufficient_history_result.rows}[210183919]
        if insufficient_sku.sales_lookup_days != 120 or insufficient_sku.valid_sales_day_count != 18:
            raise AssertionError("insufficient history must clamp to covered lookup and expose valid day count")
        if "Собрано 18 валидных" not in insufficient_sku.demand_warning:
            raise AssertionError("insufficient history must return a row-level demand warning")

        with TemporaryDirectory(prefix="factory-order-recent-fill-") as second_tmp:
            runtime_dir = Path(second_tmp) / "runtime"
            runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
            runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
            active_nm_ids = [item.nm_id for item in runtime.load_current_state().config_v2 if item.enabled]
            _seed_runtime_sales_history(runtime, active_nm_ids=active_nm_ids, missing_dates={"2026-04-17"})
            recent_history_block = FakeSalesHistoryBlock()
            recent_block = FactoryOrderSupplyBlock(
                runtime=runtime,
                stocks_block=FakeStocksBlock(active_nm_ids),
                sales_funnel_history_block=recent_history_block,
                now_factory=lambda: NOW,
                timestamp_factory=lambda: ACTIVATED_AT,
            )
            recent_block.upload_dataset(
                DATASET_STOCK_FF,
                build_single_sheet_workbook_bytes("Остатки ФФ", stock_upload_rows),
            )
            recent_fill_result = recent_block.calculate(
                {
                    "prod_lead_time_days": 10,
                    "lead_time_factory_to_ff_days": 5,
                    "lead_time_ff_to_wb_days": 2,
                    "safety_days_mp": 3,
                    "safety_days_ff": 2,
                    "cycle_order_days": 14,
                    "order_batch_qty": 50,
                    "report_date_override": "2026-04-18",
                    "sales_avg_period_days": 3,
                }
            )
            if recent_history_block.calls != [("2026-04-17", "2026-04-17")]:
                raise AssertionError(f"missing recent date must be refetched as one exact-date batch, got {recent_history_block.calls}")
            recent_sku = {item.nm_id: item for item in recent_fill_result.rows}[210183919]
            if round(recent_sku.daily_demand_total, 2) != _expected_average(210183919, report_date="2026-04-18", period_days=3):
                raise AssertionError("recent authoritative fill must preserve the covered averaging semantics")

        status = block.build_status()
        if "2026-03-28..2026-04-17" not in status.coverage_contract_note:
            raise AssertionError("status must expose the current authoritative runtime coverage window")
        if status.datasets[DATASET_STOCK_FF].uploaded_filename != "factory-stock-input.xlsx":
            raise AssertionError("status must expose the current uploaded filename")
        if status.datasets[DATASET_INBOUND_FACTORY_TO_FF].status != "missing":
            raise AssertionError("status must reflect deleted inbound_factory state")
        if status.last_result is None or status.last_result.settings.sales_avg_period_days != 60:
            raise AssertionError("status must persist the last successful calculation result")
        persisted_sku = {item.nm_id: item for item in status.last_result.rows}[210183919]
        if "Собрано 18 валидных" not in persisted_sku.demand_warning:
            raise AssertionError("status must persist row-level demand diagnostics")

        print(f"scenario_without_inbound: ok -> total_qty={result_without_inbound.summary.total_qty}")
        print("scenario_onec_stock_ff_source: ok -> calc without manual stock_ff upload, zero qty covered, partial coverage blocked")
        print("scenario_zero_only_inbound: ok -> accepted_row_count=0, coverage=0")
        print(f"scenario_multi_inbound: ok -> sku_one_inbound_factory={sku_one.inbound_factory_to_ff}")
        print(f"scenario_delete_then_zero: ok -> sku_one_coverage={sku_one_after_delete.coverage_qty}")
        print(f"scenario_supplier_registry_source: ok -> inbound_factory={supplier_sku.inbound_factory_to_ff}")
        print(f"scenario_shifted_report_date: ok -> sku_one_qty={shifted_sku_one.recommended_order_qty}, sku_two_qty={shifted_sku_two.recommended_order_qty}")
        print("scenario_covered_windows: ok -> stable periods=10,14; ramp-up period=21 warns")
        print(f"scenario_default_sales_avg: ok -> daily_demand={round(default_sku.daily_demand_total, 2)}")
        print(f"scenario_cycle_order_days: ok -> {cycle_short_result.summary.total_qty} -> {cycle_long_result.summary.total_qty}")
        print("scenario_insufficient_history: ok -> clamped covered lookup with row-level demand warning")
        print("scenario_recent_authoritative_fill: ok -> fetched missing recent date 2026-04-17")

def _seed_runtime_sales_history(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    active_nm_ids: list[int],
    missing_dates: set[str],
) -> None:
    items: list[SalesFunnelHistoryItem] = []
    for snapshot_date, values in sorted(SALES_BY_DATE.items()):
        if snapshot_date in missing_dates:
            continue
        for nm_id in sorted(active_nm_ids):
            items.append(
                SalesFunnelHistoryItem(
                    date=snapshot_date,
                    nm_id=nm_id,
                    metric="orderCount",
                    value=float(values.get(nm_id, 0.0)),
                )
            )
    persist_sales_history_result_exact_dates(
        runtime=runtime,
        payload=SalesFunnelHistorySuccess(
            kind="success",
            date_from=min(date for date in SALES_BY_DATE if date not in missing_dates),
            date_to=max(date for date in SALES_BY_DATE if date not in missing_dates),
            count=len(items),
            items=items,
        ),
        captured_at=ACTIVATED_AT,
    )


def _seed_onec_ff_stock_ready_snapshot(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    stock_by_nm: dict[int, float],
    omit_nm_ids: set[int] | None = None,
) -> None:
    snapshot_date = "2026-04-18"
    omit_nm_ids = set(omit_nm_ids or set())
    current_state = runtime.load_current_state()
    active_skus = [
        (int(item.nm_id), str(item.display_name))
        for item in current_state.config_v2
        if item.enabled
    ]
    data_rows: list[list[object]] = []
    for nm_id, display_name in active_skus:
        if nm_id not in omit_nm_ids:
            data_rows.append(
                [
                    f"{display_name} · 1C FF_STOCK",
                    f"SKU:{nm_id}|{ONEC_FF_STOCK_QTY_METRIC_KEY}",
                    float(stock_by_nm.get(nm_id, 0.0)),
                ]
            )
        data_rows.append(
            [
                f"{display_name} · 1C total qty distractor",
                f"SKU:{nm_id}|onec_total_qty",
                9999.0,
            ]
        )
    plan = SheetVitrinaV1Envelope(
        plan_version="factory_order_supply_smoke__onec_ff_stock",
        snapshot_id="factory_order_supply_smoke__onec_ff_stock__ready",
        as_of_date=snapshot_date,
        date_columns=[snapshot_date],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="today_current",
                slot_label="current",
                column_date=snapshot_date,
            )
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:C{len(data_rows) + 1}",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", snapshot_date],
                rows=data_rows,
                row_count=len(data_rows),
                column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    [
                        "onec_stocks[today_current]",
                        "success",
                        "loaded",
                        snapshot_date,
                        snapshot_date,
                        snapshot_date,
                        snapshot_date,
                        len(active_skus),
                        len(active_skus) - len(omit_nm_ids),
                        ",".join(str(item) for item in sorted(omit_nm_ids)),
                        "factory-order 1C FF_STOCK smoke fixture",
                    ]
                ],
                row_count=1,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )
    runtime.save_sheet_vitrina_ready_snapshot(
        current_state=current_state,
        refreshed_at=ACTIVATED_AT,
        plan=plan,
    )


def _seed_supplier_factory_inbound_fixture(runtime: RegistryUploadDbBackedRuntime) -> None:
    def line(
        line_id: str,
        *,
        sort_order: int,
        nm_id: int | None,
        qty: float,
        match_status: str,
        name: str,
    ) -> dict[str, object]:
        return {
            "line_id": line_id,
            "line_type": "product",
            "sort_order": sort_order,
            "source_no": line_id,
            "product_type": "clear",
            "model_raw": name,
            "model_normalized": name.lower().replace(" ", "_"),
            "match_key": "clear|" + name.lower().replace(" ", "_"),
            "internal_sku": "SKU-" + str(nm_id or ""),
            "internal_nm_id": nm_id,
            "internal_name": name,
            "qty": qty,
            "unit_price": 1,
            "amount": qty,
            "currency": "RMB",
            "comment": "",
            "match_status": match_status,
            "manual_override": False,
            "raw": {},
        }

    runtime.save_supplier_shipment(
        header={
            "shipment_id": "sup_factory_inbound_inside_window",
            "created_at": "2026-04-18T09:00:00Z",
            "updated_at": "2026-04-18T09:00:00Z",
            "shipment_date": "2026-04-20",
            "invoice_no": "26GN390",
            "invoice_date": "2026-04-19",
            "contract_no": "",
            "contract_date": "",
            "supplier_name": "HanShang Technology",
            "customer_name": "",
            "currency": "RMB",
            "product_qty_total": 45,
            "product_amount_total": 45,
            "extras_amount_total": 0,
            "invoice_amount_total": 45,
            "declared_invoice_total": 45,
            "match_status": "has_unmatched",
            "source_filename": "supplier.xlsx",
            "source_file_sha256": "",
            "source_file_path": "",
            "parser_version": "smoke",
            "warnings": [],
            "errors": [],
        },
        lines=[
            line("ln_inside_1", sort_order=1, nm_id=210183919, qty=33, match_status="matched", name="Clear iPhone 14 Pro"),
            line("ln_inside_2", sort_order=2, nm_id=None, qty=5, match_status="unmatched", name="Unknown"),
            line("ln_inside_3", sort_order=3, nm_id=None, qty=7, match_status="ambiguous", name="Ambiguous"),
        ],
    )
    runtime.save_supplier_shipment(
        header={
            "shipment_id": "sup_factory_inbound_after_window",
            "created_at": "2026-04-18T09:05:00Z",
            "updated_at": "2026-04-18T09:05:00Z",
            "shipment_date": "2026-05-10",
            "invoice_no": "LATE-1",
            "invoice_date": "2026-05-09",
            "contract_no": "",
            "contract_date": "",
            "supplier_name": "HanShang Technology",
            "customer_name": "",
            "currency": "RMB",
            "product_qty_total": 100,
            "product_amount_total": 100,
            "extras_amount_total": 0,
            "invoice_amount_total": 100,
            "declared_invoice_total": 100,
            "match_status": "all_matched",
            "source_filename": "late.xlsx",
            "source_file_sha256": "",
            "source_file_path": "",
            "parser_version": "smoke",
            "warnings": [],
            "errors": [],
        },
        lines=[
            line("ln_late_1", sort_order=1, nm_id=210183919, qty=100, match_status="matched", name="Clear iPhone 14 Pro"),
        ],
    )


def _expected_average(nm_id: int, *, report_date: str, period_days: int) -> float:
    end = date.fromisoformat(report_date) - timedelta(days=1)
    start = end - timedelta(days=period_days - 1)
    current = start
    values: list[float] = []
    while current <= end:
        values.append(float(SALES_BY_DATE[current.isoformat()][nm_id]))
        current += timedelta(days=1)
    return round(sum(values) / len(values), 2)


def _expected_target_qty(
    nm_id: int,
    *,
    report_date: str,
    period_days: int,
    prod_lead_time_days: int,
    lead_time_factory_to_ff_days: int,
    lead_time_ff_to_wb_days: int,
    safety_days_mp: int,
    safety_days_ff: int,
    cycle_order_days: int,
) -> float:
    daily_demand_total = _expected_average(nm_id, report_date=report_date, period_days=period_days)
    return round(
        daily_demand_total
        * (
            prod_lead_time_days
            + lead_time_factory_to_ff_days
            + lead_time_ff_to_wb_days
            + safety_days_mp
            + safety_days_ff
            + cycle_order_days
        ),
        2,
    )


def _expected_recommended_order_qty(
    nm_id: int,
    *,
    report_date: str,
    period_days: int,
    prod_lead_time_days: int,
    lead_time_factory_to_ff_days: int,
    lead_time_ff_to_wb_days: int,
    safety_days_mp: int,
    safety_days_ff: int,
    cycle_order_days: int,
    stock_total_mp: float,
    stock_ff: float,
    inbound_factory_to_ff: float,
    inbound_ff_to_wb: float,
    order_batch_qty: int,
) -> int:
    target_qty = _expected_target_qty(
        nm_id,
        report_date=report_date,
        period_days=period_days,
        prod_lead_time_days=prod_lead_time_days,
        lead_time_factory_to_ff_days=lead_time_factory_to_ff_days,
        lead_time_ff_to_wb_days=lead_time_ff_to_wb_days,
        safety_days_mp=safety_days_mp,
        safety_days_ff=safety_days_ff,
        cycle_order_days=cycle_order_days,
    )
    coverage_qty = stock_total_mp + stock_ff + inbound_factory_to_ff + inbound_ff_to_wb
    shortage_qty = max(target_qty - coverage_qty, 0.0)
    if shortage_qty <= 0:
        return 0
    return int(math.ceil(shortage_qty / order_batch_qty) * order_batch_qty)


if __name__ == "__main__":
    main()
