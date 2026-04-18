"""Targeted smoke-check for the factory-order supply block."""

from __future__ import annotations

from datetime import datetime, timezone
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
        items = [
            SimpleNamespace(nm_id=210183919, metric="orderCount", value=10.0),
            SimpleNamespace(nm_id=210183919, metric="orderCount", value=20.0),
            SimpleNamespace(nm_id=210184534, metric="orderCount", value=5.0),
        ]
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

        stock_template, _ = block.build_template(DATASET_STOCK_FF)
        stock_rows = read_first_sheet_rows(stock_template)
        if stock_rows[0] != ["nmId", "Комментарий SKU", "Остаток ФФ", "Дата остатка", "Комментарий"]:
            raise AssertionError("stock_ff template must use Russian headers")
        if len(stock_rows) - 1 != len(active_nm_ids):
            raise AssertionError("stock_ff template must be prefilled with active SKU rows")

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

        invalid_headers_blob = build_single_sheet_workbook_bytes(
            "Bad",
            [["sku", "qty"], ["210183919", "10"]],
        )
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
        )
        if stock_upload.accepted_row_count != len(active_nm_ids):
            raise AssertionError("stock_ff upload must accept one row per active SKU")

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
                    "sales_avg_period_days": 8,
                }
            )
        except ValueError as exc:
            if "ограничен 7 днями" not in str(exc):
                raise
        else:
            raise AssertionError("sales_avg_period_days above the live bound must be rejected")

        inbound_factory_upload = block.upload_dataset(
            DATASET_INBOUND_FACTORY_TO_FF,
            build_single_sheet_workbook_bytes(
                "В пути от фабрики",
                [
                    inbound_rows[0],
                    [210183919, "SKU 1", 40, "2026-04-25", ""],
                    [210183919, "SKU 1", 15, "2026-05-20", ""],
                ],
            ),
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
        )
        if inbound_ff_to_wb_upload.accepted_row_count != 2:
            raise AssertionError("ff_to_wb upload must be accepted")

        result = block.calculate(
            {
                "prod_lead_time_days": 10,
                "lead_time_factory_to_ff_days": 5,
                "lead_time_ff_to_wb_days": 2,
                "safety_days_mp": 3,
                "safety_days_ff": 2,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 2,
            }
        )
        by_nm_id = {item.nm_id: item for item in result.rows}
        sku_one = by_nm_id[210183919]
        if round(sku_one.daily_demand_total, 2) != 15.0:
            raise AssertionError("daily demand must average orderCount over valid closed days")
        if round(sku_one.coverage_qty, 2) != 180.0:
            raise AssertionError("coverage must include stock_ff + factory inbound within horizon + ff_to_wb parity term")
        if sku_one.recommended_order_qty != 150:
            raise AssertionError("recommended qty for sku one must round shortage to batch")
        sku_two = by_nm_id[210184534]
        if sku_two.recommended_order_qty != 100:
            raise AssertionError("recommended qty for sku two must include ff_to_wb inbound coverage")
        if result.summary.total_qty != 250:
            raise AssertionError("summary total qty must match result rows")
        if result.summary.estimated_weight != 21.48:
            raise AssertionError("summary weight must use the legacy coefficient")
        if result.summary.estimated_volume != 0.11:
            raise AssertionError("summary volume must use the legacy divisor")
        if not result.coverage_contract_note:
            raise AssertionError("result must explain the ff_to_wb parity contract note")

        recommendation_bytes, _ = block.download_recommendation()
        recommendation_rows = read_first_sheet_rows(recommendation_bytes)
        if recommendation_rows[0] != ["nmId", "Комментарий SKU", "Рекомендовано к заказу"]:
            raise AssertionError("recommendation workbook must use Russian headers")
        if recommendation_rows[-3:] != [
            ["Общее количество", None, 250],
            ["Расчётный вес", None, "21.48"],
            ["Расчётный объём", None, "0.11"],
        ]:
            raise AssertionError("recommendation workbook summary must match UI summary values")

        status = block.build_status()
        if status.active_sku_count != len(active_nm_ids):
            raise AssertionError("status must expose the active SKU count")
        if status.datasets[DATASET_STOCK_FF].status != "uploaded":
            raise AssertionError("status must surface uploaded stock_ff state")
        if status.last_result is None or status.last_result.summary.total_qty != 250:
            raise AssertionError("status must persist the last calculation result")

        print(f"stock_template_headers: ok -> {stock_rows[0]}")
        print(f"multi_row_inbound: ok -> {inbound_factory_upload.accepted_row_count}")
        print(f"parity_ff_to_wb: ok -> {sku_two.coverage_qty}")
        print(f"recommendation_total_qty: ok -> {result.summary.total_qty}")


if __name__ == "__main__":
    main()
