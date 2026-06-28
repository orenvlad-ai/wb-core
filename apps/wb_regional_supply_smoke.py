"""Targeted smoke-check for the WB regional supply block."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import zipfile

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.factory_order_supply import FactoryOrderSupplyBlock
from packages.application.factory_order_sales_history import persist_sales_history_result_exact_dates
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.application.wb_supply_overlay import build_warehouse_district_mapping
from packages.application.wb_regional_supply import WbRegionalSupplyBlock
from packages.contracts.factory_order_supply import DATASET_STOCK_FF
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryItem, SalesFunnelHistorySuccess
from packages.contracts.stocks_block import StocksEnvelope, StocksItem, StocksSuccess
from packages.contracts.wb_regional_supply import (
    DISTRICT_CENTRAL,
    DISTRICT_FAR_SIBERIA,
    DISTRICT_NORTHWEST,
    DISTRICT_SOUTH_CAUCASUS,
)

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
        _seed_runtime_stock_history(runtime, active_nm_ids=active_nm_ids)

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
        if len(regional_status.district_options) != 6:
            raise AssertionError("regional status must expose district options for the operator selector")
        if DISTRICT_FAR_SIBERIA not in regional_status.default_included_district_keys:
            raise AssertionError("regional status default district selection must include far/siberia")

        result = regional_block.calculate(
            {
                "sales_avg_period_days": 14,
                "cycle_supply_days": 5,
                "lead_time_to_region_days": 2,
                "safety_days": 1,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
            }
        )
        legacy_alias_result = regional_block.calculate(
            {
                "sales_avg_period_days": 14,
                "supply_horizon_days": 5,
                "lead_time_to_region_days": 2,
                "safety_days": 1,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
            }
        )
        if result.summary.total_qty != 100:
            raise AssertionError(f"regional summary total must reflect FF-limited allocation, got {result.summary.total_qty}")
        if result.diagnostics is None or result.diagnostics.get("regional_demand_method") not in {
            "full_clean_days",
            "regional_share_ladder",
        }:
            raise AssertionError(f"result diagnostics must expose share ladder methodology, got {result.diagnostics}")
        if result.diagnostics.get("fallback_sku_count") != 0:
            raise AssertionError(f"share ladder must not use current-stock fallback in this fixture, got {result.diagnostics}")
        if not result.diagnostics.get("share_source_counts"):
            raise AssertionError(f"share source counts must be exposed, got {result.diagnostics}")
        if result.diagnostics.get("requested_valid_day_count") != 14:
            raise AssertionError("result diagnostics must expose requested valid depletion day count")
        if result.settings.included_district_keys != regional_status.default_included_district_keys:
            raise AssertionError("old payload without included_district_keys must default to all districts")
        if result.diagnostics.get("district_selection_mode") != "all_districts":
            raise AssertionError("default district selection must be all_districts")
        if legacy_alias_result.summary.total_qty != result.summary.total_qty:
            raise AssertionError("legacy supply_horizon_days alias must keep the same WB regional math")
        districts = {item.district_key: item for item in result.districts}
        if districts["central"].total_qty != 50 or districts["central"].deficit_qty != 100:
            raise AssertionError("central district must keep one box allocated and truthful deficit")
        if districts["northwest"].total_qty != 50 or districts["northwest"].deficit_qty != 100:
            raise AssertionError("northwest district must keep one box allocated and truthful deficit")
        if sum(item.total_qty for item in result.districts) != result.summary.total_qty:
            raise AssertionError("summary total must equal the sum of district totals")
        if sum(item.deficit_qty for item in result.districts) != 200:
            raise AssertionError("deficit totals must equal full recommendation minus allocated supply")
        central_main_row = next(row for row in districts["central"].rows if row.nm_id == MAIN_NM_ID)
        if not central_main_row.demand_diagnostics:
            raise AssertionError("district row must carry regional demand diagnostics")
        if central_main_row.demand_diagnostics.get("regional_demand_method") != "full_clean_days":
            raise AssertionError("main SKU must use full-clean-day methodology")
        if central_main_row.demand_diagnostics.get("selected_valid_day_count") != 14:
            raise AssertionError("main SKU must use 14 selected stock-depletion days")
        if abs(central_main_row.daily_demand_total - 60.0) > 1e-9:
            raise AssertionError("total demand must remain based on orderCount, not absolute depletion")

        _seed_wb_regional_overlay_fixture(
            runtime,
            supply_id="wb-regional-central",
            nm_id=MAIN_NM_ID,
            quantity=50.0,
            supply_date="2026-04-20",
            warehouse_name="Коледино",
            district_key=DISTRICT_CENTRAL,
        )
        regional_overlay_result = regional_block.calculate(
            {
                "sales_avg_period_days": 14,
                "cycle_supply_days": 5,
                "lead_time_to_region_days": 2,
                "safety_days": 1,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "selected_wb_supply_ids": ["wb-regional-central"],
            }
        )
        overlay_diagnostics = regional_overlay_result.wb_supply_overlay or {}
        overlay_stock = overlay_diagnostics.get("stock_ff", {})
        overlay_regional = overlay_diagnostics.get("wb_regional", {})
        if overlay_stock.get("by_nm_id", {}).get(str(MAIN_NM_ID), {}).get("effective_stock_ff") != 70.0:
            raise AssertionError(f"regional selected supply must reduce available FF pool, got {overlay_stock}")
        added_by_district = overlay_regional.get("added_qty_by_district", {})
        if added_by_district.get(DISTRICT_CENTRAL) != 50.0:
            raise AssertionError(f"regional selected supply must add qty to mapped district, got {overlay_regional}")
        if any(float(qty or 0) for key, qty in added_by_district.items() if key != DISTRICT_CENTRAL):
            raise AssertionError(f"selected central supply must not be spread to other districts, got {added_by_district}")
        overlay_districts = {item.district_key: item for item in regional_overlay_result.districts}
        overlay_central_row = next(row for row in overlay_districts[DISTRICT_CENTRAL].rows if row.nm_id == MAIN_NM_ID)
        overlay_northwest_row = next(row for row in overlay_districts[DISTRICT_NORTHWEST].rows if row.nm_id == MAIN_NM_ID)
        if overlay_central_row.demand_diagnostics.get("selected_wb_supply_qty") != 50.0:
            raise AssertionError("central row diagnostics must expose selected WB qty")
        if overlay_northwest_row.demand_diagnostics.get("selected_wb_supply_qty") != 0.0:
            raise AssertionError("other districts must not receive selected central WB qty")
        if overlay_central_row.projected_stock_on_eta <= central_main_row.projected_stock_on_eta:
            raise AssertionError("selected WB supply must improve projected stock only in the mapped district")
        audit_rows = runtime.list_wb_regional_supply_calculation_audit(limit=5)
        if not audit_rows or audit_rows[0].get("calculation_id") != regional_overlay_result.calculation_id:
            raise AssertionError(f"latest regional audit row must track the last calculation, got {audit_rows}")
        latest_audit = audit_rows[0]
        if latest_audit.get("central_total_qty") != overlay_districts[DISTRICT_CENTRAL].total_qty:
            raise AssertionError(f"regional audit must expose central aggregate totals, got {latest_audit}")
        if latest_audit.get("settings", {}).get("selected_wb_supply_ids_count") != 1:
            raise AssertionError(f"regional audit must store selected supply count, got {latest_audit}")
        if "wb-regional-central" in json.dumps(latest_audit, ensure_ascii=False):
            raise AssertionError(f"regional audit must not persist selected WB supply ids, got {latest_audit}")

        _seed_wb_regional_overlay_fixture(
            runtime,
            supply_id="wb-regional-provider-mapped",
            nm_id=MAIN_NM_ID,
            quantity=25.0,
            supply_date="2026-04-20",
            warehouse_name="Электросталь",
            district_key="unmapped",
        )
        regional_block.wb_supply_district_mapping_provider = lambda: build_warehouse_district_mapping(
            warehouse_rows=runtime.list_wb_supplies_warehouses(),
            supply_rows=runtime.list_wb_supplies(),
            office_rows=[{"name": "Электросталь", "federalDistrict": "Центральный федеральный округ"}],
        )
        provider_overlay_result = regional_block.calculate(
            {
                "sales_avg_period_days": 14,
                "cycle_supply_days": 5,
                "lead_time_to_region_days": 2,
                "safety_days": 1,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "selected_wb_supply_ids": ["wb-regional-provider-mapped"],
            }
        )
        provider_overlay = provider_overlay_result.wb_supply_overlay or {}
        provider_regional = provider_overlay.get("wb_regional", {})
        provider_added_by_district = provider_regional.get("added_qty_by_district", {})
        if provider_added_by_district.get(DISTRICT_CENTRAL) != 25.0:
            raise AssertionError(f"provider mapping must map stale-unmapped warehouse to central, got {provider_regional}")
        if any(float(qty or 0) for key, qty in provider_added_by_district.items() if key != DISTRICT_CENTRAL):
            raise AssertionError(f"provider-mapped central supply must not affect other districts, got {provider_added_by_district}")

        _seed_wb_regional_overlay_fixture(
            runtime,
            supply_id="wb-regional-krasnodar-transit",
            nm_id=MAIN_NM_ID,
            quantity=30.0,
            supply_date="2026-04-20",
            warehouse_name="Краснодар (Тихорецкая)",
            district_key=DISTRICT_CENTRAL,
            actual_warehouse_name="Обухово",
            transit_warehouse_name="Обухово",
        )
        regional_block.wb_supply_district_mapping_provider = lambda: build_warehouse_district_mapping(
            warehouse_rows=runtime.list_wb_supplies_warehouses(),
            supply_rows=runtime.list_wb_supplies(),
            tariff_rows=[{"warehouseName": "Обухово", "geoName": "Центральный федеральный округ"}],
        )
        routed_overlay_result = regional_block.calculate(
            {
                "sales_avg_period_days": 14,
                "cycle_supply_days": 5,
                "lead_time_to_region_days": 2,
                "safety_days": 1,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "selected_wb_supply_ids": ["wb-regional-krasnodar-transit"],
            }
        )
        routed_overlay = routed_overlay_result.wb_supply_overlay or {}
        routed_regional = routed_overlay.get("wb_regional", {})
        routed_added_by_district = routed_regional.get("added_qty_by_district", {})
        if routed_added_by_district.get(DISTRICT_SOUTH_CAUCASUS) != 30.0:
            raise AssertionError(f"planned Краснодар warehouse must add qty to south_caucasus, got {routed_regional}")
        if routed_added_by_district.get(DISTRICT_CENTRAL) != 0.0:
            raise AssertionError(f"actual/transit Обухово must not leak qty into central, got {routed_regional}")
        routed_selected = (routed_overlay.get("selected_supplies") or [{}])[0]
        if (
            routed_selected.get("district_source_warehouse_name") != "Краснодар (Тихорецкая)"
            or routed_selected.get("warehouse_display") != "Краснодар (Тихорецкая) → Обухово"
            or routed_selected.get("district_key") != DISTRICT_SOUTH_CAUCASUS
        ):
            raise AssertionError(f"regional overlay diagnostics must expose planned district source, got {routed_selected}")

        _seed_wb_regional_overlay_fixture(
            runtime,
            supply_id="wb-regional-unmapped",
            nm_id=MAIN_NM_ID,
            quantity=10.0,
            supply_date="2026-04-20",
            warehouse_name="Склад без ФО",
            district_key="unmapped",
        )
        unmapped_overlay_result = regional_block.calculate(
            {
                "sales_avg_period_days": 14,
                "cycle_supply_days": 5,
                "lead_time_to_region_days": 2,
                "safety_days": 1,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "selected_wb_supply_ids": ["wb-regional-unmapped"],
            }
        )
        unmapped_overlay = unmapped_overlay_result.wb_supply_overlay or {}
        unmapped_regional = unmapped_overlay.get("wb_regional", {})
        if unmapped_regional.get("added_qty_total") != 0.0 or not unmapped_regional.get("unmapped_events"):
            raise AssertionError(f"unmapped warehouse must not add regional qty and must be diagnosed, got {unmapped_regional}")
        if not any("склад не сопоставлен" in warning for warning in unmapped_overlay_result.warnings):
            raise AssertionError(f"unmapped warehouse must emit regional warning, got {unmapped_overlay_result.warnings}")

        selected_result = regional_block.calculate(
            {
                "sales_avg_period_days": 14,
                "cycle_supply_days": 5,
                "lead_time_to_region_days": 2,
                "safety_days": 1,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "included_district_keys": [DISTRICT_CENTRAL, DISTRICT_NORTHWEST],
            }
        )
        selected_diagnostics = selected_result.diagnostics or {}
        if selected_diagnostics.get("included_district_keys") != [DISTRICT_CENTRAL, DISTRICT_NORTHWEST]:
            raise AssertionError(f"selected district diagnostics not exposed: {selected_diagnostics}")
        if DISTRICT_FAR_SIBERIA not in selected_diagnostics.get("excluded_district_keys", []):
            raise AssertionError("selected district diagnostics must include excluded far/siberia")
        selected_district_keys = [item.district_key for item in selected_result.districts]
        if selected_district_keys != [DISTRICT_CENTRAL, DISTRICT_NORTHWEST]:
            raise AssertionError(f"selected result must expose included districts only, got {selected_district_keys}")

        try:
            regional_block.calculate(
                {
                    "sales_avg_period_days": 14,
                    "cycle_supply_days": 5,
                    "lead_time_to_region_days": 2,
                    "safety_days": 1,
                    "order_batch_qty": 50,
                    "report_date_override": "2026-04-18",
                    "included_district_keys": [],
                }
            )
        except ValueError as exc:
            if "Выберите хотя бы один округ" not in str(exc):
                raise AssertionError(f"empty selected districts must return clear validation error, got {exc}") from exc
        else:
            raise AssertionError("empty selected districts must be rejected")

        central_workbook, central_filename = regional_block.download_district_recommendation("central")
        central_rows = read_first_sheet_rows(central_workbook)
        if central_filename != "wb_regional_central_fo.xlsx" or not _is_ascii(central_filename):
            raise AssertionError(f"central district filename must be stable ASCII, got {central_filename!r}")
        if central_rows[0][:2] != ["Федеральный округ", "Центральный федеральный округ"]:
            raise AssertionError("district workbook must start with district identification")
        if central_rows[2] != ["nmId", "SKU", "Количество к поставке", "Дефицит"]:
            raise AssertionError("district workbook must keep compact Russian headers with deficit")
        load_workbook(BytesIO(central_workbook), data_only=True)
        central_allocated_sum = sum(int(row[2]) for row in central_rows[3:] if len(row) >= 3 and str(row[2]).strip())
        central_deficit_sum = sum(int(row[3]) for row in central_rows[3:] if len(row) >= 4 and str(row[3]).strip())
        if central_allocated_sum != districts["central"].total_qty:
            raise AssertionError("district workbook sum must equal district total in summary")
        if central_deficit_sum != districts["central"].deficit_qty:
            raise AssertionError("district workbook deficit sum must equal district deficit in summary")

        try:
            regional_block.download_district_recommendation("far_siberia")
        except ValueError as exc:
            if "Округ не участвовал в последнем расчёте: far_siberia" not in str(exc):
                raise AssertionError(f"excluded district download must return clear error, got {exc}") from exc
        else:
            raise AssertionError("excluded district direct download must be blocked")

        archive_bytes, archive_filename = regional_block.download_all_recommendations_archive()
        if archive_filename != "wb_regional_recommendations_2026-04-18.zip" or not _is_ascii(archive_filename):
            raise AssertionError(f"ZIP filename must be stable ASCII with report date, got {archive_filename!r}")
        with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
            archive_names = sorted(archive.namelist())
            if archive_names != ["wb_regional_central_fo.xlsx", "wb_regional_northwest_fo.xlsx"]:
                raise AssertionError(f"ZIP must contain only included district XLSX files, got {archive_names}")
            for name in archive_names:
                if not _is_ascii(name):
                    raise AssertionError(f"ZIP member filename must be ASCII, got {name!r}")
                load_workbook(BytesIO(archive.read(name)), data_only=True)

        _seed_runtime_sales_history(runtime, active_nm_ids=active_nm_ids, all_active_signal=False)
        _seed_runtime_stock_history(
            runtime,
            active_nm_ids=active_nm_ids,
            all_active_signal=False,
            persistent_zero_south_for_main=True,
        )
        seed_stock_upload_rows = [list(row) for row in stock_rows]
        for row in seed_stock_upload_rows[1:]:
            row[2] = 0
        for row in seed_stock_upload_rows[1:]:
            if int(row[0]) == MAIN_NM_ID:
                row[2] = 400
                break
        factory_block.upload_dataset(
            DATASET_STOCK_FF,
            build_single_sheet_workbook_bytes("Остатки ФФ", seed_stock_upload_rows),
            uploaded_filename="shared-stock-ff-seed.xlsx",
        )
        seed_result = regional_block.calculate(
            {
                "sales_avg_period_days": 14,
                "cycle_supply_days": 5,
                "lead_time_to_region_days": 2,
                "safety_days": 1,
                "order_batch_qty": 50,
                "report_date_override": "2026-04-18",
                "included_district_keys": [DISTRICT_CENTRAL, DISTRICT_NORTHWEST, "south_caucasus"],
            }
        )
        seed_diagnostics = seed_result.diagnostics or {}
        if seed_diagnostics.get("fallback_sku_count") != 0:
            raise AssertionError(f"seed-floor fixture must not need current-stock fallback, got {seed_diagnostics}")
        if seed_diagnostics.get("seed_floor_sku_district_count", 0) < 1:
            raise AssertionError(f"seed-floor diagnostics must count affected SKU-districts, got {seed_diagnostics}")
        if seed_diagnostics.get("seed_sku_count") != 1 or seed_diagnostics.get("seed_allocated_qty_total") != 50:
            raise AssertionError(f"seed diagnostics must expose one allocated test box, got {seed_diagnostics}")
        seed_districts = {item.district_key: item for item in seed_result.districts}
        south_seed_row = next(row for row in seed_districts["south_caucasus"].rows if row.nm_id == MAIN_NM_ID)
        if south_seed_row.district_daily_demand != 0:
            raise AssertionError("seed floor must not create demand-based district demand")
        if south_seed_row.seed_qty != 50 or not south_seed_row.seed_floor_applied:
            raise AssertionError(f"south_caucasus row must carry one seed box, got {south_seed_row}")
        if south_seed_row.demand_allocated_qty != 0 or south_seed_row.allocated_qty != 50:
            raise AssertionError("seed row must separate demand allocation from test-box allocation")
        if south_seed_row.share_source != "seed_floor":
            raise AssertionError(f"seed row must expose seed_floor share source, got {south_seed_row}")
        if seed_result.summary.total_qty > 400:
            raise AssertionError("seed allocation must not exceed available stock_ff")
        south_workbook, _ = regional_block.download_district_recommendation("south_caucasus")
        south_rows = read_first_sheet_rows(south_workbook)
        south_allocated_sum = sum(int(row[2]) for row in south_rows[3:] if len(row) >= 3 and str(row[2]).strip())
        if south_allocated_sum != seed_districts["south_caucasus"].total_qty:
            raise AssertionError("district XLSX total must include seed-floor qty")

        print(f"shared_stock_ff_reuse: ok -> {regional_status.shared_datasets['stock_ff'].uploaded_filename}")
        print(f"regional_total_qty: ok -> {result.summary.total_qty}")
        print(f"central_deficit: ok -> {districts['central'].deficit_qty}")
        print(f"northwest_deficit: ok -> {districts['northwest'].deficit_qty}")
        print(f"seed_floor: ok -> {seed_diagnostics.get('seed_allocated_qty_total')}")
        print(f"regional_audit: ok -> {latest_audit.get('calculation_id')}")
        print(f"district_xlsx_sum: ok -> {central_allocated_sum}")
        print(f"district_xlsx_deficit_sum: ok -> {central_deficit_sum}")
        print(f"recommendations_zip: ok -> {archive_names}")


def _seed_wb_regional_overlay_fixture(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    supply_id: str,
    nm_id: int,
    quantity: float,
    supply_date: str,
    warehouse_name: str,
    district_key: str,
    actual_warehouse_name: str = "",
    transit_warehouse_name: str = "",
) -> None:
    warehouse_display = (
        f"{warehouse_name} → {transit_warehouse_name}"
        if transit_warehouse_name and transit_warehouse_name != warehouse_name
        else warehouse_name
    )
    runtime.save_wb_supply_rows(
        rows=[
            {
                "supply_id": supply_id,
                "cache_key": supply_id,
                "wb_supply_id": supply_id,
                "preorder_id": "pre-" + supply_id,
                "number_label": supply_id,
                "status_id": 2,
                "status_label": "Запланировано",
                "warehouse_id": supply_id,
                "warehouse_name": warehouse_name,
                "planned_warehouse_id": supply_id,
                "planned_warehouse_name": warehouse_name,
                "target_warehouse_id": supply_id,
                "target_warehouse_name": warehouse_name,
                "actual_warehouse_id": ("actual-" + supply_id) if actual_warehouse_name else "",
                "actual_warehouse_name": actual_warehouse_name,
                "transit_warehouse_id": ("transit-" + supply_id) if transit_warehouse_name else "",
                "transit_warehouse_name": transit_warehouse_name,
                "warehouse_from_name": warehouse_name,
                "warehouse_to_name": transit_warehouse_name,
                "warehouse_actual_name": actual_warehouse_name,
                "warehouse_display": warehouse_display,
                "district_source_warehouse_id": supply_id,
                "district_source_warehouse_name": warehouse_name,
                "district_source_warehouse_role": "planned",
                "district_source_warehouse_evidence": "fixture.warehouse_name",
                "supply_date": supply_date,
                "district_key": district_key,
                "district_label_ru": "",
                "quantity_for_size_filter": quantity,
                "raw_list": {"supplyID": supply_id, "statusID": 2, "supplyDate": supply_date},
                "raw_detail": {
                    "warehouseName": warehouse_name,
                    "actualWarehouseName": actual_warehouse_name,
                    "transitWarehouseName": transit_warehouse_name,
                },
                "raw_goods": [{"nmID": int(nm_id), "quantity": float(quantity)}],
                "raw_package": [],
            }
        ],
        warehouses=[{"warehouse_id": supply_id, "warehouse_name": warehouse_name}],
        synced_at=ACTIVATED_AT,
    )


def _is_ascii(value: str) -> bool:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _seed_runtime_sales_history(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    active_nm_ids: list[int],
    all_active_signal: bool = False,
) -> None:
    report_date = date(2026, 4, 18)
    items: list[SalesFunnelHistoryItem] = []
    for offset in range(14, 0, -1):
        snapshot_date = (report_date - timedelta(days=offset)).isoformat()
        for nm_id in active_nm_ids:
            value = 60.0 if nm_id == MAIN_NM_ID or all_active_signal else 0.0
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
            date_from="2026-04-04",
            date_to="2026-04-17",
            count=len(items),
            items=items,
        ),
        captured_at=ACTIVATED_AT,
    )


def _seed_runtime_stock_history(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    active_nm_ids: list[int],
    all_active_signal: bool = False,
    persistent_zero_south_for_main: bool = False,
) -> None:
    first_snapshot = date(2026, 4, 3)
    for index in range(15):
        snapshot_date = first_snapshot + timedelta(days=index)
        items: list[StocksItem] = []
        for nm_id in active_nm_ids:
            central = 0.0
            northwest = 0.0
            volga = 0.0
            ural = 0.0
            south = 0.0
            far = 0.0
            if nm_id == MAIN_NM_ID or all_active_signal:
                central = 1000.0 - (10.0 * index)
                northwest = 1000.0 - (10.0 * index)
                volga = 500.0
                ural = 500.0
                south = 0.0 if persistent_zero_south_for_main and nm_id == MAIN_NM_ID else 500.0
                far = 500.0
            items.append(
                StocksItem(
                    nm_id=int(nm_id),
                    stock_total=central + northwest + volga + ural + south + far,
                    stock_ru_central=central,
                    stock_ru_northwest=northwest,
                    stock_ru_volga=volga,
                    stock_ru_ural=ural,
                    stock_ru_south_caucasus=south,
                    stock_ru_far_siberia=far,
                )
            )
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


if __name__ == "__main__":
    main()
