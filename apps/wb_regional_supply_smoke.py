"""Targeted smoke-check for the WB regional supply block."""

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
from packages.application.factory_order_sales_history import persist_sales_history_result_exact_dates
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.application.wb_regional_supply import WbRegionalSupplyBlock
from packages.contracts.factory_order_supply import DATASET_STOCK_FF
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryItem, SalesFunnelHistorySuccess

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
ACTIVATED_AT = "2026-04-18T09:00:00Z"
MAIN_NM_ID = 210183919


class FakeStocksBlock:
    def __init__(self, nm_ids: list[int]) -> None:
        self.nm_ids = list(nm_ids)

    def execute(self, request_obj: object) -> SimpleNamespace:
        items = []
        for nm_id in self.nm_ids:
            central = 0.0
            northwest = 0.0
            if nm_id == MAIN_NM_ID:
                central = 100.0
                northwest = 100.0
            items.append(
                SimpleNamespace(
                    nm_id=nm_id,
                    stock_total=central + northwest,
                    stock_ru_central=central,
                    stock_ru_northwest=northwest,
                    stock_ru_volga=0.0,
                    stock_ru_ural=0.0,
                    stock_ru_south_caucasus=0.0,
                    stock_ru_far_siberia=0.0,
                )
            )
        return SimpleNamespace(result=SimpleNamespace(kind="success", items=items))


class NoopSalesHistoryBlock:
    def execute(self, request_obj: object) -> SimpleNamespace:  # pragma: no cover - should not be called
        raise AssertionError("runtime coverage is fully seeded; live fetch must not be called in smoke")


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="wb-regional-supply-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        active_nm_ids = [item.nm_id for item in runtime.load_current_state().config_v2 if item.enabled]
        _seed_runtime_sales_history(runtime, active_nm_ids=active_nm_ids)

        factory_block = FactoryOrderSupplyBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
            timestamp_factory=lambda: ACTIVATED_AT,
        )
        regional_block = WbRegionalSupplyBlock(
            runtime=runtime,
            stocks_block=FakeStocksBlock(active_nm_ids),
            sales_funnel_history_block=NoopSalesHistoryBlock(),
            now_factory=lambda: NOW,
            timestamp_factory=lambda: ACTIVATED_AT,
        )

        stock_template, _ = factory_block.build_template(DATASET_STOCK_FF)
        stock_rows = read_first_sheet_rows(stock_template)
        stock_upload_rows = [list(row) for row in stock_rows]
        for row in stock_upload_rows[1:]:
            row[2] = 0
        for row in stock_upload_rows[1:]:
            if int(row[0]) == MAIN_NM_ID:
                row[2] = 120
                break
        upload_result = factory_block.upload_dataset(
            DATASET_STOCK_FF,
            build_single_sheet_workbook_bytes("Остатки ФФ", stock_upload_rows),
            uploaded_filename="shared-stock-ff.xlsx",
        )
        if upload_result.dataset.uploaded_filename != "shared-stock-ff.xlsx":
            raise AssertionError("shared stock_ff upload must keep the original filename")

        factory_status = factory_block.build_status()
        regional_status = regional_block.build_status()
        if factory_status.datasets["stock_ff"].uploaded_filename != "shared-stock-ff.xlsx":
            raise AssertionError("factory status must expose the shared stock_ff filename")
        if regional_status.shared_datasets["stock_ff"].uploaded_filename != "shared-stock-ff.xlsx":
            raise AssertionError("regional status must reuse the shared stock_ff state")

        result = regional_block.calculate(
            {
                "sales_avg_period_days": 7,
                "supply_horizon_days": 5,
                "lead_time_to_region_days": 2,
                "safety_days": 1,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
            }
        )
        if result.summary.total_qty != 100:
            raise AssertionError(f"regional summary total must reflect FF-limited allocation, got {result.summary.total_qty}")
        districts = {item.district_key: item for item in result.districts}
        if districts["central"].total_qty != 50 or districts["central"].deficit_qty != 100:
            raise AssertionError("central district must keep one box allocated and truthful deficit")
        if districts["northwest"].total_qty != 50 or districts["northwest"].deficit_qty != 100:
            raise AssertionError("northwest district must keep one box allocated and truthful deficit")
        if sum(item.total_qty for item in result.districts) != result.summary.total_qty:
            raise AssertionError("summary total must equal the sum of district totals")
        if sum(item.deficit_qty for item in result.districts) != 200:
            raise AssertionError("deficit totals must equal full recommendation minus allocated supply")

        central_workbook, central_filename = regional_block.download_district_recommendation("central")
        central_rows = read_first_sheet_rows(central_workbook)
        if central_filename != "Центральный федеральный округ.xlsx":
            raise AssertionError("central district filename must be operator-friendly and Russian")
        if central_rows[0][:2] != ["Федеральный округ", "Центральный федеральный округ"]:
            raise AssertionError("district workbook must start with district identification")
        if central_rows[2] != ["nmId", "SKU", "Количество к поставке"]:
            raise AssertionError("district workbook must keep compact Russian headers")
        central_allocated_sum = sum(int(row[2]) for row in central_rows[3:] if len(row) >= 3 and str(row[2]).strip())
        if central_allocated_sum != districts["central"].total_qty:
            raise AssertionError("district workbook sum must equal district total in summary")

        far_workbook, far_filename = regional_block.download_district_recommendation("far_siberia")
        far_rows = read_first_sheet_rows(far_workbook)
        if far_filename != "Дальневосточный и Сибирский федеральный округ.xlsx":
            raise AssertionError("far_siberia filename must follow the Russian district label")
        if len(far_rows) != 3:
            raise AssertionError("district with zero allocation must still materialize an empty operator-friendly workbook")

        print(f"shared_stock_ff_reuse: ok -> {regional_status.shared_datasets['stock_ff'].uploaded_filename}")
        print(f"regional_total_qty: ok -> {result.summary.total_qty}")
        print(f"central_deficit: ok -> {districts['central'].deficit_qty}")
        print(f"northwest_deficit: ok -> {districts['northwest'].deficit_qty}")
        print(f"district_xlsx_sum: ok -> {central_allocated_sum}")


def _seed_runtime_sales_history(runtime: RegistryUploadDbBackedRuntime, *, active_nm_ids: list[int]) -> None:
    report_date = date(2026, 4, 18)
    items: list[SalesFunnelHistoryItem] = []
    for offset in range(7, 0, -1):
        snapshot_date = (report_date - timedelta(days=offset)).isoformat()
        for nm_id in active_nm_ids:
            value = 60.0 if nm_id == MAIN_NM_ID else 0.0
            items.append(
                SalesFunnelHistoryItem(
                    date=snapshot_date,
                    nm_id=int(nm_id),
                    metric="orderCount",
                    value=value,
                )
            )
    persist_sales_history_result_exact_dates(
        runtime=runtime,
        payload=SalesFunnelHistorySuccess(
            kind="success",
            date_from="2026-04-11",
            date_to="2026-04-17",
            count=len(items),
            items=items,
        ),
        captured_at=ACTIVATED_AT,
    )


if __name__ == "__main__":
    main()
