"""Targeted smoke-check for the factory-order supply block."""

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

from packages.application.factory_order_supply import FactoryOrderSupplyBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.contracts.factory_order_supply import (
    DATASET_INBOUND_FACTORY_TO_FF,
    DATASET_INBOUND_FF_TO_WB,
    DATASET_STOCK_FF,
)

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
ACTIVATED_AT = "2026-04-18T09:00:00Z"

SALES_BY_DATE = {
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
    def execute(self, request_obj: object) -> SimpleNamespace:
        start = date.fromisoformat(request_obj.date_from)
        end = date.fromisoformat(request_obj.date_to)
        items = []
        current = start
        while current <= end:
            lookup = SALES_BY_DATE.get(current.isoformat(), {})
            for nm_id in request_obj.nm_ids:
                if nm_id in lookup:
                    items.append(
                        SimpleNamespace(
                            date=current.isoformat(),
                            nm_id=nm_id,
                            metric="orderCount",
                            value=float(lookup[nm_id]),
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
        block = FactoryOrderSupplyBlock(
            runtime=runtime,
            stocks_block=FakeStocksBlock(active_nm_ids),
            sales_funnel_history_block=FakeSalesHistoryBlock(),
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
        ]:
            raise AssertionError("factory inbound template must use Russian headers")

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

        # Scenario 1: calculate without any inbound files.
        result_without_inbound = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 3,
            }
        )
        by_nm_id = {item.nm_id: item for item in result_without_inbound.rows}
        sku_one = by_nm_id[210183919]
        sku_two = by_nm_id[210184534]
        if round(sku_one.daily_demand_total, 2) != 160.0:
            raise AssertionError("3-day lookback must average only the last 3 closed days")
        if round(sku_one.coverage_qty, 2) != 130.0:
            raise AssertionError("coverage without inbound files must include only stock_total_mp + stock_ff")
        if sku_one.inbound_factory_to_ff != 0.0 or sku_one.inbound_ff_to_wb != 0.0:
            raise AssertionError("missing inbound files must be treated as zero, not as blocking input")
        if sku_two.recommended_order_qty != 850:
            raise AssertionError("recommended qty without inbound files must still round by box multiple")

        # Scenario 2: upload both inbound files and keep only the events inside horizon.
        inbound_factory_upload = block.upload_dataset(
            DATASET_INBOUND_FACTORY_TO_FF,
            build_single_sheet_workbook_bytes(
                "В пути от фабрики",
                [
                    inbound_rows[0],
                    [210183919, "SKU 1", 40, "2026-04-25", ""],
                    [210183919, "SKU 1", 15, "2026-08-20", ""],
                ],
            ),
            uploaded_filename="factory-inbound.xlsx",
        )
        if inbound_factory_upload.accepted_row_count != 2:
            raise AssertionError("factory inbound upload must keep multiple rows for one SKU")

        inbound_ff_to_wb_upload = block.upload_dataset(
            DATASET_INBOUND_FF_TO_WB,
            build_single_sheet_workbook_bytes(
                "В пути ФФ -> WB",
                [
                    ["nmId", "Комментарий SKU", "Количество в пути", "Планируемая дата прихода на Wildberries", "Комментарий"],
                    [210183919, "SKU 1", 10, "2026-04-26", ""],
                    [210184534, "SKU 2", 25, "2026-04-28", ""],
                ],
            ),
            uploaded_filename="ff-to-wb.xlsx",
        )
        if inbound_ff_to_wb_upload.accepted_row_count != 2:
            raise AssertionError("ff_to_wb upload must be accepted")

        result_with_inbound = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 7,
            }
        )
        by_nm_id = {item.nm_id: item for item in result_with_inbound.rows}
        sku_one = by_nm_id[210183919]
        sku_two = by_nm_id[210184534]
        if round(sku_one.daily_demand_total, 2) != 140.0:
            raise AssertionError("7-day lookback must change the average demand relative to 3-day lookback")
        if round(sku_one.inbound_factory_to_ff, 2) != 40.0:
            raise AssertionError("only the inbound_factory event inside horizon must be counted")
        if round(sku_one.inbound_ff_to_wb, 2) != 10.0:
            raise AssertionError("ff_to_wb parity term must be kept when file is uploaded")
        if round(sku_two.inbound_ff_to_wb, 2) != 25.0:
            raise AssertionError("ff_to_wb uploaded rows must contribute to coverage")

        recommendation_bytes, _ = block.download_recommendation()
        recommendation_rows = read_first_sheet_rows(recommendation_bytes)
        if recommendation_rows[0] != ["nmId", "Комментарий SKU", "Рекомендовано к заказу"]:
            raise AssertionError("recommendation workbook must use Russian headers")
        summary_rows = recommendation_rows[-3:]
        if summary_rows[0][0] != "Общее количество" or summary_rows[1][0] != "Расчётный вес":
            raise AssertionError("recommendation workbook summary must stay operator-facing and Russian")

        # Scenario 3: delete inbound files and verify recalculation falls back to zero.
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

        # Scenario 4: a different report date and box multiple must change the recommendation math.
        shifted_result = block.calculate(
            {
                "prod_lead_time_days": 4,
                "lead_time_factory_to_ff_days": 3,
                "lead_time_ff_to_wb_days": 1,
                "safety_days_mp": 1,
                "safety_days_ff": 0,
                "order_batch_qty": 25,
                "report_date_override": "2026-04-16",
                "sales_avg_period_days": 2,
            }
        )
        shifted_rows = {item.nm_id: item for item in shifted_result.rows}
        shifted_sku_one = shifted_rows[210183919]
        shifted_sku_two = shifted_rows[210184534]
        if round(shifted_sku_one.daily_demand_total, 2) != 145.0:
            raise AssertionError("shifted report date must recalculate the average demand on the selected closed-day window")
        if shifted_sku_one.recommended_order_qty != 1175:
            raise AssertionError("shifted scenario must keep the exact 25-piece box multiple for SKU 1")
        if shifted_sku_two.recommended_order_qty != 300:
            raise AssertionError("shifted scenario must round SKU 2 up to the next 25-piece box multiple")

        # Scenario 5: longer lookback still hits the truthful authoritative boundary.
        try:
            block.calculate(
                {
                    "prod_lead_time_days": 10,
                    "lead_time_factory_to_ff_days": 5,
                    "lead_time_ff_to_wb_days": 2,
                    "safety_days_mp": 3,
                    "safety_days_ff": 2,
                    "order_batch_qty": 50,
                    "report_date_override": "2026-04-18",
                    "sales_avg_period_days": 10,
                }
            )
        except ValueError as exc:
            if "current live source сейчас принимает start day не раньше 2026-04-11" not in str(exc):
                raise
        else:
            raise AssertionError("lookback beyond the authoritative history boundary must fail truthfully")

        status = block.build_status()
        if status.datasets[DATASET_STOCK_FF].uploaded_filename != "factory-stock-input.xlsx":
            raise AssertionError("status must expose the current uploaded filename")
        if status.datasets[DATASET_INBOUND_FACTORY_TO_FF].status != "missing":
            raise AssertionError("status must reflect deleted inbound_factory state")
        if status.last_result is None or status.last_result.summary.total_qty != shifted_result.summary.total_qty:
            raise AssertionError("status must persist the last successful calculation result")

        print(f"scenario_without_inbound: ok -> total_qty={result_without_inbound.summary.total_qty}")
        print(f"scenario_multi_inbound: ok -> sku_one_inbound_factory={sku_one.inbound_factory_to_ff}")
        print(f"scenario_delete_then_zero: ok -> sku_one_coverage={sku_one_after_delete.coverage_qty}")
        print(f"scenario_shifted_report_date: ok -> sku_one_qty={shifted_sku_one.recommended_order_qty}, sku_two_qty={shifted_sku_two.recommended_order_qty}")
        print("scenario_period_gt_7: ok -> truthful blocker current live source сейчас принимает start day не раньше 2026-04-11")


if __name__ == "__main__":
    main()
