"""HTTP end-to-end smoke-check for the factory-order operator flow."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from urllib import error, request as urllib_request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_FACTORY_ORDER_CALCULATE_PATH,
    DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FACTORY_PATH,
    DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FF_TO_WB_PATH,
    DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH,
    DEFAULT_FACTORY_ORDER_STATUS_PATH,
    DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_CHECK_PATH,
    DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_XLSX_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH,
    DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FACTORY_PATH,
    DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FF_TO_WB_PATH,
    DEFAULT_FACTORY_ORDER_UPLOADED_STOCK_FF_PATH,
    DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FACTORY_PATH,
    DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FF_TO_WB_PATH,
    DEFAULT_FACTORY_ORDER_UPLOAD_STOCK_FF_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    build_registry_upload_http_server,
)
from packages.application.factory_order_sales_history import persist_sales_history_result_exact_dates
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_live_plan import STATUS_HEADER
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.application.stock_ff_onec_source import ONEC_FF_STOCK_QTY_METRIC_KEY
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryItem, SalesFunnelHistorySuccess
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)
from packages.contracts.supplier_shipments import ORDER_STATUS_ACCEPTED_FF, ORDER_STATUS_IN_TRANSIT

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
    def execute(self, request_obj: object) -> SimpleNamespace:
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
    with TemporaryDirectory(prefix="sheet-vitrina-factory-order-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        port = _reserve_free_port()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            now_factory=lambda: NOW,
        )
        cfg = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(cfg, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{cfg.port}"

            upload_status, upload_payload = _post_json(f"{base_url}{DEFAULT_UPLOAD_PATH}", bundle)
            if upload_status != 200 or upload_payload.get("status") != "accepted":
                raise AssertionError(f"bundle upload must be accepted, got {upload_status} {upload_payload}")

            active_nm_ids = [item.nm_id for item in runtime.load_current_state().config_v2 if item.enabled]
            _seed_runtime_sales_history(runtime, active_nm_ids=active_nm_ids)
            _seed_onec_ff_stock_ready_snapshot(
                runtime,
                stock_by_nm={
                    active_nm_ids[0]: 17.0,
                    active_nm_ids[1]: 0.0,
                    **{nm_id: 0.0 for nm_id in active_nm_ids[2:]},
                },
            )
            entrypoint.factory_order_supply_block.stocks_block = FakeStocksBlock(active_nm_ids)
            fake_history_block = FakeSalesHistoryBlock()
            entrypoint.factory_order_supply_block.sales_funnel_history_block = fake_history_block
            entrypoint.factory_order_supply_block.sales_history.sales_funnel_history_block = fake_history_block

            operator_status, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}?embedded_tab=factory-order")
            if operator_status != 200:
                raise AssertionError(f"operator page must return 200, got {operator_status}")
            for expected in (
                "Обновление данных",
                "Поставки",
                "Заказ на фабрике",
                "Цикл заказов, дней",
                "Поставка на Wildberries",
                "Кратность штук в коробке",
                "Скачать загруженный файл",
                "Рассчитать заказ на фабрике",
                "Сводка поставок",
                "Общее количество товаров",
                "Источник «Товары в пути от фабрики»",
                "Источник «Остатки ФФ»",
                "1С / Фулфилмент",
                "Проверить данные 1С",
                "Скачать Excel для проверки",
                "Товары в пути из реестра поставщика",
                "refreshSupplierRegistryInboundButton",
                "Обновить",
                "Реестр поставщика",
            ):
                if expected not in operator_html:
                    raise AssertionError(f"operator page must expose {expected!r}")
            if "window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30000)" not in operator_html:
                raise AssertionError("operator XLSX downloads must keep blob URLs alive long enough for browser save")
            if "https://docs.google.com/spreadsheets/d/" in operator_html:
                raise AssertionError("factory-order operator surface must not expose legacy Google Sheets as an active link")
            if (
                "Загрузить остатки ФФ" in operator_html
                or "Загрузить товары в пути от фабрики" in operator_html
                or "Загрузить товары в пути от ФФ на Wildberries" in operator_html
                or ".addEventListener(\"change\", () => uploadDataset(" not in operator_html
            ):
                raise AssertionError("operator page must use auto-upload after file pick without separate upload buttons")
            if (
                "value=\"30\"" not in operator_html
                or "value=\"15\"" not in operator_html
                or "value=\"14\"" not in operator_html
                or "value=\"250\"" not in operator_html
            ):
                raise AssertionError("operator page must prefill the new factory defaults in the form")

            stock_template_status, stock_template_bytes, stock_template_headers = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH}"
            )
            inbound_factory_status, inbound_factory_bytes, _ = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH}"
            )
            inbound_ff_to_wb_status, inbound_ff_to_wb_bytes, _ = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH}"
            )
            if stock_template_status != 200 or inbound_factory_status != 200 or inbound_ff_to_wb_status != 200:
                raise AssertionError("all template routes must return XLSX")
            if "spreadsheetml.sheet" not in str(stock_template_headers.get("Content-Type", "")):
                raise AssertionError("stock_ff template route must return XLSX content type")
            stock_rows = read_first_sheet_rows(stock_template_bytes)
            if len(stock_rows) - 1 != len(active_nm_ids):
                raise AssertionError("stock_ff template must be prefilled with active SKU rows")
            inbound_factory_template_rows = read_first_sheet_rows(inbound_factory_bytes)
            if inbound_factory_template_rows[0][-1] != "Поставка":
                raise AssertionError("inbound_factory template must include a shipment column for summary grouping")

            onec_check_status, onec_check_payload = _get_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_CHECK_PATH}"
            )
            if onec_check_status != 200 or onec_check_payload.get("status") != "ready":
                raise AssertionError(f"1C FF_STOCK check route must return ready summary, got {onec_check_status} {onec_check_payload}")
            if (
                onec_check_payload.get("positive_stock_sku_count") != 1
                or onec_check_payload.get("zero_stock_sku_count") != len(active_nm_ids) - 1
                or onec_check_payload.get("total_stock_ff") != 17.0
            ):
                raise AssertionError(f"1C FF_STOCK check summary must count positive/zero/total, got {onec_check_payload}")

            onec_xlsx_status, onec_xlsx_bytes, onec_xlsx_headers = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_XLSX_PATH}"
            )
            if onec_xlsx_status != 200 or "spreadsheetml.sheet" not in str(onec_xlsx_headers.get("Content-Type", "")):
                raise AssertionError("1C FF_STOCK XLSX route must return a valid XLSX")
            onec_xlsx_rows = read_first_sheet_rows(onec_xlsx_bytes)
            if onec_xlsx_rows[0] != ["nmId", "Комментарий SKU", "Остаток ФФ", "Дата остатка", "Комментарий"]:
                raise AssertionError("1C FF_STOCK XLSX must keep manual stock_ff headers")
            if len(onec_xlsx_rows) - 1 != len(active_nm_ids) or onec_xlsx_rows[1][2] != 17.0 or onec_xlsx_rows[2][2] != 0.0:
                raise AssertionError(f"1C FF_STOCK XLSX must expose full active SKU coverage including zero rows, got {onec_xlsx_rows}")

            onec_calc_status, onec_calc_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                    "stock_ff_source": "onec_ff_stock",
                },
            )
            if onec_calc_status != 200:
                raise AssertionError(f"1C FF_STOCK calc must not require manual stock_ff upload, got {onec_calc_status} {onec_calc_payload}")
            onec_calc_sku = next(item for item in onec_calc_payload.get("rows", []) if item.get("nm_id") == active_nm_ids[0])
            if onec_calc_payload.get("stock_ff_source") != "onec_ff_stock" or onec_calc_sku.get("stock_ff") != 17.0:
                raise AssertionError(f"1C FF_STOCK calc must use selected source and FF_STOCK qty, got {onec_calc_payload}")
            if onec_calc_payload.get("manual_stock_ff_dataset", {}).get("status") != "missing":
                raise AssertionError("1C FF_STOCK calc must not write or require manual stock_ff upload")

            stock_upload_rows = [list(row) for row in stock_rows]
            for row in stock_upload_rows[1:]:
                row[2] = 0
            stock_upload_rows[1][2] = 30
            stock_upload_rows[2][2] = 10
            stock_upload_bytes = build_single_sheet_workbook_bytes("Остатки ФФ", stock_upload_rows)
            stock_upload_status, stock_upload_payload = _post_multipart(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOAD_STOCK_FF_PATH}",
                stock_upload_bytes,
                filename="factory-stock.xlsx",
            )
            if stock_upload_status != 200 or stock_upload_payload.get("accepted_row_count") != len(active_nm_ids):
                raise AssertionError(f"stock_ff upload must be accepted, got {stock_upload_status} {stock_upload_payload}")

            uploaded_stock_status, uploaded_stock_bytes, uploaded_stock_headers = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOADED_STOCK_FF_PATH}"
            )
            if uploaded_stock_status != 200 or uploaded_stock_headers.get("Content-Disposition", "").find("factory-stock.xlsx") < 0:
                raise AssertionError("current uploaded stock_ff file must be downloadable after upload")
            if read_first_sheet_rows(uploaded_stock_bytes)[1][0] != stock_rows[1][0]:
                raise AssertionError("uploaded stock_ff download must preserve the uploaded workbook content")

            # Scenario 1: calculation succeeds without inbound files.
            calc_without_inbound_status, calc_without_inbound_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                },
            )
            if calc_without_inbound_status != 200:
                raise AssertionError(f"calc without inbound files must succeed, got {calc_without_inbound_status} {calc_without_inbound_payload}")
            first_rows = calc_without_inbound_payload.get("rows", [])
            first_sku = next(item for item in first_rows if item.get("nm_id") == 210183919)
            if first_sku.get("inbound_factory_to_ff") != 0.0 or first_sku.get("inbound_ff_to_wb") != 0.0:
                raise AssertionError("missing inbound files must be treated as zero in HTTP calc path")
            if calc_without_inbound_payload.get("factory_inbound_source") != "manual_excel":
                raise AssertionError("HTTP calc must default factory inbound source to manual_excel")
            if calc_without_inbound_payload.get("target_window_days") != 36 or calc_without_inbound_payload.get("inbound_window_end") != "2026-05-24":
                raise AssertionError("HTTP calc must expose the full inbound target window")

            _seed_wb_supply_overlay_fixture(
                runtime,
                supply_id="wb-http-overlay",
                nm_id=210183919,
                quantity=20.0,
                supply_date="2026-04-20",
            )
            overlay_status, overlay_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                    "selected_wb_supply_ids": ["wb-http-overlay"],
                },
            )
            if overlay_status != 200:
                raise AssertionError(f"factory selected WB supply calc must succeed over HTTP, got {overlay_status} {overlay_payload}")
            overlay_sku = next(item for item in overlay_payload.get("rows", []) if item.get("nm_id") == 210183919)
            if overlay_sku.get("stock_ff") != 10.0 or overlay_sku.get("inbound_ff_to_wb") != 20.0:
                raise AssertionError(f"HTTP selected WB supply must move qty from FF to FF->WB, got {overlay_sku}")
            if round(float(overlay_sku.get("coverage_qty", 0.0)), 2) != round(float(first_sku.get("coverage_qty", 0.0)), 2):
                raise AssertionError("HTTP selected WB supply inside inbound window should keep ideal coverage unchanged")
            overlay_diag = overlay_payload.get("wb_supply_overlay") or {}
            if overlay_diag.get("factory_order", {}).get("added_inbound_ff_to_wb_qty_total") != 20.0:
                raise AssertionError(f"HTTP factory result must expose WB overlay diagnostics, got {overlay_diag}")

            inbound_factory_zero_rows = read_first_sheet_rows(inbound_factory_bytes)
            inbound_factory_zero_rows = [
                inbound_factory_zero_rows[0],
                [210183919, "SKU 1", 0, "", ""],
                [210184534, "SKU 2", 0, "", ""],
            ]
            inbound_factory_zero_upload_status, inbound_factory_zero_upload_payload = _post_multipart(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FACTORY_PATH}",
                build_single_sheet_workbook_bytes("В пути от фабрики", inbound_factory_zero_rows),
                filename="factory-inbound-zero.xlsx",
            )
            if (
                inbound_factory_zero_upload_status != 200
                or inbound_factory_zero_upload_payload.get("accepted_row_count") != 0
                or inbound_factory_zero_upload_payload.get("ignored_row_count") != 2
            ):
                raise AssertionError("zero-only inbound_factory upload must be accepted as an empty dataset")
            if inbound_factory_zero_upload_payload.get("shipment_summary"):
                raise AssertionError("zero-only inbound_factory upload must expose an empty shipment summary")

            inbound_ff_to_wb_zero_rows = read_first_sheet_rows(inbound_ff_to_wb_bytes)
            inbound_ff_to_wb_zero_rows = [
                inbound_ff_to_wb_zero_rows[0],
                [210183919, "SKU 1", 0, "", ""],
                [210184534, "SKU 2", 0, "", ""],
            ]
            inbound_ff_to_wb_zero_upload_status, inbound_ff_to_wb_zero_upload_payload = _post_multipart(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FF_TO_WB_PATH}",
                build_single_sheet_workbook_bytes("В пути ФФ -> WB", inbound_ff_to_wb_zero_rows),
                filename="ff-to-wb-zero.xlsx",
            )
            if (
                inbound_ff_to_wb_zero_upload_status != 200
                or inbound_ff_to_wb_zero_upload_payload.get("accepted_row_count") != 0
                or inbound_ff_to_wb_zero_upload_payload.get("ignored_row_count") != 2
            ):
                raise AssertionError("zero-only inbound_ff_to_wb upload must be accepted as an empty dataset")

            zero_only_status_code, zero_only_status_payload = _get_json(f"{base_url}{DEFAULT_FACTORY_ORDER_STATUS_PATH}")
            if zero_only_status_code != 200:
                raise AssertionError("factory-order status route must return 200 after zero-only inbound upload")
            zero_only_inbound_factory_state = zero_only_status_payload.get("datasets", {}).get("inbound_factory_to_ff", {})
            zero_only_inbound_ff_to_wb_state = zero_only_status_payload.get("datasets", {}).get("inbound_ff_to_wb", {})
            if zero_only_inbound_factory_state.get("row_count") != 0 or zero_only_inbound_ff_to_wb_state.get("row_count") != 0:
                raise AssertionError("zero-only inbound uploads must persist as uploaded datasets with row_count=0")
            if zero_only_inbound_factory_state.get("shipment_summary"):
                raise AssertionError("zero-only inbound_factory status must expose an empty shipment summary")

            calc_zero_only_status, calc_zero_only_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                },
            )
            if calc_zero_only_status != 200:
                raise AssertionError("calc with zero-only inbound uploads must still succeed")
            zero_only_first_sku = next(item for item in calc_zero_only_payload.get("rows", []) if item.get("nm_id") == 210183919)
            if zero_only_first_sku.get("inbound_factory_to_ff") != 0.0 or zero_only_first_sku.get("inbound_ff_to_wb") != 0.0:
                raise AssertionError("zero-only inbound uploads must keep HTTP coverage terms at zero")

            inbound_factory_rows = read_first_sheet_rows(inbound_factory_bytes)
            inbound_factory_rows = [
                inbound_factory_rows[0],
                [210183919, "SKU 1", 5, "2026-04-10", "", "Старая поставка"],
                [210183919, "SKU 1", 40, "2026-04-25", "", "Поставка A"],
                [210184534, "SKU 2", 15, "2026-04-25", "", "Поставка A"],
                [210184534, "SKU 2", 0, "", "", ""],
                [210183919, "SKU 1", 12, "2026-05-05", "", ""],
                [210183919, "SKU 1", 999, "2026-05-25", "", "Поздняя поставка"],
            ]
            inbound_factory_upload_status, inbound_factory_upload_payload = _post_multipart(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FACTORY_PATH}",
                build_single_sheet_workbook_bytes("В пути от фабрики", inbound_factory_rows),
                filename="factory-inbound.xlsx",
            )
            if (
                inbound_factory_upload_status != 200
                or inbound_factory_upload_payload.get("accepted_row_count") != 5
                or inbound_factory_upload_payload.get("ignored_row_count") != 1
            ):
                raise AssertionError("inbound_factory upload must ignore zero rows and aggregate positive rows by shipment")
            upload_summary = inbound_factory_upload_payload.get("shipment_summary")
            if (
                not isinstance(upload_summary, list)
                or len(upload_summary) != 4
                or upload_summary[1].get("shipment") != "Поставка A"
                or upload_summary[1].get("total_quantity") != 55.0
                or upload_summary[1].get("acceptance_date") != "2026-04-25"
                or upload_summary[2].get("shipment") != "Поставка №1"
                or upload_summary[2].get("total_quantity") != 12.0
                or upload_summary[2].get("acceptance_date") != "2026-05-05"
            ):
                raise AssertionError(f"inbound_factory upload must return sanitized shipment summary, got {upload_summary}")

            inbound_ff_to_wb_rows = read_first_sheet_rows(inbound_ff_to_wb_bytes)
            inbound_ff_to_wb_rows = [
                inbound_ff_to_wb_rows[0],
                [210183919, "SKU 1", 6, "2026-04-17", ""],
                [210183919, "SKU 1", 10, "2026-04-26", ""],
                [210183919, "SKU 1", 0, "", ""],
                [210183919, "SKU 1", 7, "2026-05-10", ""],
                [210183919, "SKU 1", 888, "2026-05-25", ""],
                [210184534, "SKU 2", 25, "2026-04-28", ""],
            ]
            inbound_ff_to_wb_upload_status, inbound_ff_to_wb_upload_payload = _post_multipart(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FF_TO_WB_PATH}",
                build_single_sheet_workbook_bytes("В пути ФФ -> WB", inbound_ff_to_wb_rows),
                filename="ff-to-wb.xlsx",
            )
            if (
                inbound_ff_to_wb_upload_status != 200
                or inbound_ff_to_wb_upload_payload.get("accepted_row_count") != 5
                or inbound_ff_to_wb_upload_payload.get("ignored_row_count") != 1
            ):
                raise AssertionError("inbound_ff_to_wb upload must ignore zero rows and keep positive rows")

            # Scenario 2: file lifecycle state is visible through status and current uploaded routes.
            status_code, status_payload = _get_json(f"{base_url}{DEFAULT_FACTORY_ORDER_STATUS_PATH}")
            if status_code != 200:
                raise AssertionError("factory-order status route must return 200")
            if "2026-03-28..2026-04-17" not in str(status_payload.get("coverage_contract_note", "")):
                raise AssertionError("status route must expose the authoritative runtime coverage window")
            inbound_factory_state = status_payload.get("datasets", {}).get("inbound_factory_to_ff", {})
            if inbound_factory_state.get("uploaded_filename") != "factory-inbound.xlsx":
                raise AssertionError("status must expose the stored uploaded filename")
            if inbound_factory_state.get("download_path") != DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FACTORY_PATH:
                raise AssertionError("status must expose the current uploaded file download path")
            if inbound_factory_state.get("delete_path") != DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FACTORY_PATH:
                raise AssertionError("status must expose the delete path")
            status_summary = inbound_factory_state.get("shipment_summary")
            if (
                not isinstance(status_summary, list)
                or [item.get("total_quantity") for item in status_summary] != [5.0, 55.0, 12.0, 999.0]
            ):
                raise AssertionError(f"status must expose the persisted inbound_factory shipment summary, got {status_summary}")
            supplier_registry_summary = status_payload.get("supplier_registry_inbound_summary", {})
            if supplier_registry_summary.get("acceptance_days") != 30:
                raise AssertionError("status must expose supplier registry +30 day acceptance default")
            if status_payload.get("stock_ff_onec_check_path") != DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_CHECK_PATH:
                raise AssertionError("status must expose the 1C FF_STOCK check route")
            if status_payload.get("stock_ff_onec_xlsx_path") != DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_XLSX_PATH:
                raise AssertionError("status must expose the 1C FF_STOCK XLSX route")
            if status_payload.get("onec_stock_ff_summary", {}).get("status") != "ready":
                raise AssertionError("status must expose current 1C FF_STOCK summary")

            current_inbound_status, _, current_inbound_headers = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FACTORY_PATH}"
            )
            if current_inbound_status != 200 or "factory-inbound.xlsx" not in str(current_inbound_headers.get("Content-Disposition", "")):
                raise AssertionError("current inbound_factory file must be downloadable after upload")

            # Scenario 3: calculation with inbound files keeps only horizon-relevant events.
            calc_with_inbound_status, calc_with_inbound_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                },
            )
            if calc_with_inbound_status != 200:
                raise AssertionError(f"factory-order calc with inbound must succeed, got {calc_with_inbound_status} {calc_with_inbound_payload}")
            first_sku_with_inbound = next(
                item for item in calc_with_inbound_payload.get("rows", []) if item.get("nm_id") == 210183919
            )
            if first_sku_with_inbound.get("daily_demand_total") != _expected_average(210183919, report_date="2026-04-18", period_days=7):
                raise AssertionError("7-day lookback must change the HTTP average demand")
            if first_sku_with_inbound.get("inbound_factory_to_ff") != 52.0 or first_sku_with_inbound.get("inbound_ff_to_wb") != 17.0:
                raise AssertionError("HTTP calc must use report_date..full target window and factory effective dates")
            effective_factory_rows = [
                (item.get("planned_arrival_date"), item.get("effective_arrival_date"), item.get("quantity"))
                for item in calc_with_inbound_payload.get("effective_inbound_factory_to_ff", [])
                if item.get("nm_id") == 210183919
            ]
            if effective_factory_rows != [("2026-04-25", "2026-04-27", 40.0), ("2026-05-05", "2026-05-07", 12.0)]:
                raise AssertionError(f"HTTP calc must expose effective factory inbound rows used, got {effective_factory_rows}")

            recommendation_status, recommendation_bytes, recommendation_headers = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH}"
            )
            if recommendation_status != 200 or "spreadsheetml.sheet" not in str(recommendation_headers.get("Content-Type", "")):
                raise AssertionError("recommendation route must return XLSX after calculation")
            recommendation_disposition = str(recommendation_headers.get("Content-Disposition", ""))
            if "attachment" not in recommendation_disposition or "factory-order-recommendation-2026-04-18.xlsx" not in recommendation_disposition:
                raise AssertionError("recommendation route must return an attachment filename matching the calculated result")
            recommendation_rows = read_first_sheet_rows(recommendation_bytes)
            if recommendation_rows[-3][0] != "Общее количество":
                raise AssertionError("recommendation workbook summary must stay aligned with UI summary")

            # Scenario 4: delete inbound files and recalculate with zero coverage terms again.
            delete_factory_status, delete_factory_payload = _delete_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FACTORY_PATH}"
            )
            delete_ff_to_wb_status, delete_ff_to_wb_payload = _delete_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FF_TO_WB_PATH}"
            )
            if delete_factory_status != 200 or delete_factory_payload.get("status") != "deleted":
                raise AssertionError("delete inbound_factory must succeed")
            if delete_ff_to_wb_status != 200 or delete_ff_to_wb_payload.get("status") != "deleted":
                raise AssertionError("delete inbound_ff_to_wb must succeed")
            deleted_download_status, deleted_download_payload = _get_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FACTORY_PATH}"
            )
            if deleted_download_status != 404 or "отсутствует" not in str(deleted_download_payload.get("error", "")):
                raise AssertionError("deleted uploaded file must disappear from current download route")

            calc_after_delete_status, calc_after_delete_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                },
            )
            if calc_after_delete_status != 200:
                raise AssertionError("calc after inbound delete must still succeed")
            first_sku = next(item for item in calc_after_delete_payload.get("rows", []) if item.get("nm_id") == 210183919)
            if first_sku.get("inbound_factory_to_ff") != 0.0 or first_sku.get("inbound_ff_to_wb") != 0.0:
                raise AssertionError("after delete the HTTP calc must restore zero inbound terms")

            supplier_empty_status, supplier_empty_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                },
            )
            if supplier_empty_status != 200:
                raise AssertionError(f"supplier registry source with zero usable rows must not fail, got {supplier_empty_status} {supplier_empty_payload}")
            supplier_empty_sku = next(item for item in supplier_empty_payload.get("rows", []) if item.get("nm_id") == 210183919)
            if supplier_empty_sku.get("inbound_factory_to_ff") != 0.0 or not supplier_empty_payload.get("warnings"):
                raise AssertionError("empty supplier registry source must produce zero factory inbound with a warning")

            _seed_supplier_factory_inbound_fixture(runtime)
            supplier_source_status, supplier_source_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                },
            )
            if supplier_source_status != 200:
                raise AssertionError(f"supplier registry source must calculate, got {supplier_source_status} {supplier_source_payload}")
            supplier_source_sku = next(item for item in supplier_source_payload.get("rows", []) if item.get("nm_id") == 210183919)
            supplier_diagnostics = supplier_source_payload.get("supplier_registry_inbound_summary", {}).get("diagnostics", {})
            if supplier_source_sku.get("inbound_factory_to_ff") != 33.0:
                raise AssertionError("supplier registry source must use matched supplier quantity in factory inbound coverage")
            if supplier_diagnostics.get("usable_line_count") != 2 or supplier_diagnostics.get("unmatched_line_count") != 1 or supplier_diagnostics.get("ambiguous_line_count") != 1:
                raise AssertionError(f"supplier registry diagnostics must expose usable/unmatched/ambiguous counts, got {supplier_diagnostics}")
            if (
                supplier_diagnostics.get("excluded_accepted_ff_shipment_count") != 1
                or supplier_diagnostics.get("excluded_accepted_ff_line_count") != 1
                or supplier_diagnostics.get("excluded_accepted_ff_quantity") != 44.0
            ):
                raise AssertionError(f"supplier registry diagnostics must expose accepted_ff exclusions, got {supplier_diagnostics}")
            supplier_summaries = supplier_source_payload.get("supplier_registry_inbound_summary", {}).get("shipment_summary", [])
            if any(item.get("shipment_id") == "sup_factory_inbound_accepted_ff" for item in supplier_summaries):
                raise AssertionError("accepted_ff supplier shipments must be excluded from factory inbound shipment summary")
            if not any("accepted_ff" in item for item in supplier_source_payload.get("warnings", [])):
                raise AssertionError("supplier registry source must warn when accepted_ff shipments are excluded")
            effective_supplier_rows = [
                (item.get("shipment_name"), item.get("planned_arrival_date"), item.get("effective_arrival_date"), item.get("quantity"))
                for item in supplier_source_payload.get("effective_inbound_factory_to_ff", [])
                if item.get("nm_id") == 210183919
            ]
            if effective_supplier_rows != [("26GN390", "2026-05-20", "2026-05-22", 33.0)]:
                raise AssertionError(f"supplier registry source must expose effective rows used, got {effective_supplier_rows}")

            patch_status, patch_payload = _patch_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/sup_factory_inbound_inside_window",
                {"actual_ff_acceptance_date": "2026-05-23"},
            )
            if patch_status != 200 or patch_payload.get("order_status") != ORDER_STATUS_ACCEPTED_FF:
                raise AssertionError(f"actual_ff_acceptance_date PATCH must trigger accepted_ff, got {patch_status} {patch_payload}")

            patched_status_code, patched_status_payload = _get_json(f"{base_url}{DEFAULT_FACTORY_ORDER_STATUS_PATH}")
            if patched_status_code != 200:
                raise AssertionError(f"factory status after actual_ff_acceptance_date PATCH must return 200, got {patched_status_code}")
            patched_supplier_summary = patched_status_payload.get("supplier_registry_inbound_summary", {})
            patched_supplier_shipments = patched_supplier_summary.get("shipment_summary", [])
            if any(item.get("shipment_id") == "sup_factory_inbound_inside_window" for item in patched_supplier_shipments):
                raise AssertionError(f"accepted_ff shipment must disappear from status supplier summary, got {patched_supplier_shipments}")
            patched_diagnostics = patched_supplier_summary.get("diagnostics", {})
            if (
                patched_diagnostics.get("excluded_accepted_ff_shipment_count") != 2
                or patched_diagnostics.get("excluded_accepted_ff_line_count") != 4
                or patched_diagnostics.get("excluded_accepted_ff_quantity") != 89.0
            ):
                raise AssertionError(f"status after actual_ff_acceptance_date PATCH must expose excluded counters, got {patched_diagnostics}")

            patched_supplier_status, patched_supplier_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                },
            )
            if patched_supplier_status != 200:
                raise AssertionError(f"supplier registry calc after actual_ff_acceptance_date PATCH must succeed, got {patched_supplier_status} {patched_supplier_payload}")
            patched_supplier_sku = next(item for item in patched_supplier_payload.get("rows", []) if item.get("nm_id") == 210183919)
            if patched_supplier_sku.get("inbound_factory_to_ff") != 0.0:
                raise AssertionError(f"accepted_ff shipment must not count in supplier registry inbound after PATCH, got {patched_supplier_sku}")
            if any(item.get("shipment_name") == "26GN390" for item in patched_supplier_payload.get("effective_inbound_factory_to_ff", [])):
                raise AssertionError("accepted_ff shipment must not leak into effective inbound rows after PATCH")

            for period_days in (10, 14):
                covered_status, covered_payload = _post_json(
                    f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                    },
                )
                if covered_status != 200:
                    raise AssertionError(f"HTTP calc must succeed for covered window {period_days}, got {covered_status} {covered_payload}")
                covered_sku = next(item for item in covered_payload.get("rows", []) if item.get("nm_id") == 210183919)
                if round(float(covered_sku.get("daily_demand_total", 0.0)), 2) != _expected_average(
                    210183919,
                    report_date="2026-04-18",
                    period_days=period_days,
                ):
                    raise AssertionError(f"HTTP calc must average exact covered runtime history for {period_days}-day lookback")
                if covered_sku.get("demand_warning"):
                    raise AssertionError(f"HTTP calc must not warn for stable covered window {period_days}")

            ramp_up_status, ramp_up_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                },
            )
            if ramp_up_status != 200:
                raise AssertionError(f"HTTP calc must succeed for ramp-up window diagnostics, got {ramp_up_status} {ramp_up_payload}")
            ramp_up_sku = next(item for item in ramp_up_payload.get("rows", []) if item.get("nm_id") == 210183919)
            if ramp_up_sku.get("valid_sales_day_count") != 18 or "Собрано 18 валидных" not in str(ramp_up_sku.get("demand_warning", "")):
                raise AssertionError("HTTP calc must expose 21-day ramp-up insufficient valid-day diagnostics")

            default_period_status, default_period_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
                {
                    "prod_lead_time_days": 10,
                    "lead_time_factory_to_ff_days": 5,
                    "lead_time_ff_to_wb_days": 2,
                    "safety_days_mp": 3,
                    "safety_days_ff": 2,
                    "order_batch_qty": 50,
                    "report_date_override": "2026-04-18",
                },
            )
            if default_period_status != 200:
                raise AssertionError(f"HTTP calc must keep current default sales_avg_period_days, got {default_period_status} {default_period_payload}")
            default_sku = next(item for item in default_period_payload.get("rows", []) if item.get("nm_id") == 210183919)
            if round(float(default_sku.get("daily_demand_total", 0.0)), 2) != _expected_average(
                210183919,
                report_date="2026-04-18",
                period_days=14,
            ):
                raise AssertionError("missing sales_avg_period_days must fall back to the current 14-day window in HTTP calc")
            if int(default_period_payload.get("settings", {}).get("sales_avg_period_days", 0)) != 14:
                raise AssertionError("HTTP calc payload must persist the current 14-day sales average default")
            if int(default_period_payload.get("settings", {}).get("cycle_order_days", 0)) != 14:
                raise AssertionError("HTTP calc payload must persist the current 14-day cycle_order_days default")

            cycle_short_status, cycle_short_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                },
            )
            cycle_long_status, cycle_long_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                },
            )
            if cycle_short_status != 200 or cycle_long_status != 200:
                raise AssertionError("HTTP calc must succeed for both cycle_order_days comparison scenarios")
            if int(cycle_long_payload.get("summary", {}).get("total_qty", 0)) <= int(cycle_short_payload.get("summary", {}).get("total_qty", 0)):
                raise AssertionError("larger cycle_order_days must materially increase the factory total_qty")

            calc_insufficient_history_status, calc_insufficient_history_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
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
                },
            )
            if calc_insufficient_history_status != 200:
                raise AssertionError(
                    "HTTP calc must succeed with row-level insufficient-history diagnostics, "
                    f"got {calc_insufficient_history_status} {calc_insufficient_history_payload}"
                )
            insufficient_history_sku = next(
                item for item in calc_insufficient_history_payload.get("rows", []) if item.get("nm_id") == 210183919
            )
            if insufficient_history_sku.get("sales_lookup_days") != 120 or insufficient_history_sku.get("valid_sales_day_count") != 18:
                raise AssertionError("HTTP calc must expose clamped lookup and valid day diagnostics")
            if "Собрано 18 валидных" not in str(insufficient_history_sku.get("demand_warning", "")):
                raise AssertionError("HTTP calc must expose insufficient-history demand warning")

            print(f"scenario_without_inbound_http: ok -> total_qty={calc_without_inbound_payload['summary']['total_qty']}")
            print("scenario_onec_stock_ff_http: ok -> check route, XLSX route and calc without manual stock_ff upload")
            print("scenario_zero_only_inbound_http: ok -> accepted_row_count=0, coverage=0")
            print(f"scenario_current_file_lifecycle_http: ok -> {inbound_factory_state['uploaded_filename']}")
            print(
                f"scenario_multi_inbound_http: ok -> inbound_factory={first_sku_with_inbound.get('inbound_factory_to_ff', 0.0)}"
            )
            print("scenario_covered_windows_http: ok -> stable periods=10,14; ramp-up period=21 warns")
            print(f"scenario_default_sales_avg_http: ok -> daily_demand={round(float(default_sku.get('daily_demand_total', 0.0)), 2)}")
            print(
                "scenario_cycle_order_days_http: ok -> "
                f"{cycle_short_payload.get('summary', {}).get('total_qty')} -> {cycle_long_payload.get('summary', {}).get('total_qty')}"
            )
            print("scenario_insufficient_history_http: ok -> clamped covered lookup with row-level demand warning")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    req = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _patch_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    req = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="PATCH",
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_multipart(url: str, workbook_bytes: bytes, *, filename: str) -> tuple[int, dict[str, object]]:
    boundary = "----wbcore" + uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n",
            workbook_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    req = urllib_request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _delete_json(url: str) -> tuple[int, dict[str, object]]:
    req = urllib_request.Request(url, headers={"Accept": "application/json"}, method="DELETE")
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    try:
        with urllib_request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _seed_runtime_sales_history(runtime: RegistryUploadDbBackedRuntime, *, active_nm_ids: list[int]) -> None:
    items: list[SalesFunnelHistoryItem] = []
    for snapshot_date, values in sorted(SALES_BY_DATE.items()):
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
            date_from=min(SALES_BY_DATE),
            date_to=max(SALES_BY_DATE),
            count=len(items),
            items=items,
        ),
        captured_at=ACTIVATED_AT,
    )


def _seed_onec_ff_stock_ready_snapshot(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    stock_by_nm: dict[int, float],
) -> None:
    snapshot_date = "2026-04-18"
    current_state = runtime.load_current_state()
    active_skus = [
        (int(item.nm_id), str(item.display_name))
        for item in current_state.config_v2
        if item.enabled
    ]
    data_rows: list[list[object]] = []
    for nm_id, display_name in active_skus:
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
        plan_version="factory_order_http_smoke__onec_ff_stock",
        snapshot_id="factory_order_http_smoke__onec_ff_stock__ready",
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
                        len(active_skus),
                        "",
                        "factory-order HTTP 1C FF_STOCK smoke fixture",
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


def _seed_wb_supply_overlay_fixture(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    supply_id: str,
    nm_id: int,
    quantity: float,
    supply_date: str,
) -> None:
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
                "warehouse_id": "507",
                "warehouse_name": "Коледино",
                "warehouse_display": "Коледино",
                "supply_date": supply_date,
                "district_key": "central",
                "district_label_ru": "Центральный федеральный округ",
                "quantity_for_size_filter": quantity,
                "raw_list": {"supplyID": supply_id, "statusID": 2, "supplyDate": supply_date},
                "raw_detail": {"warehouseID": 507, "warehouseName": "Коледино"},
                "raw_goods": [{"nmID": int(nm_id), "quantity": float(quantity)}],
                "raw_package": [],
            }
        ],
        warehouses=[{"warehouse_id": "507", "warehouse_name": "Коледино"}],
        synced_at=ACTIVATED_AT,
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
            "order_status": ORDER_STATUS_IN_TRANSIT,
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
    runtime.save_supplier_shipment(
        header={
            "shipment_id": "sup_factory_inbound_accepted_ff",
            "created_at": "2026-04-18T09:10:00Z",
            "updated_at": "2026-04-18T09:10:00Z",
            "shipment_date": "2026-04-20",
            "order_status": ORDER_STATUS_ACCEPTED_FF,
            "invoice_no": "ACCEPTED-FF",
            "invoice_date": "2026-04-19",
            "contract_no": "",
            "contract_date": "",
            "supplier_name": "HanShang Technology",
            "customer_name": "",
            "currency": "RMB",
            "product_qty_total": 44,
            "product_amount_total": 44,
            "extras_amount_total": 0,
            "invoice_amount_total": 44,
            "declared_invoice_total": 44,
            "match_status": "all_matched",
            "source_filename": "accepted.xlsx",
            "source_file_sha256": "",
            "source_file_path": "",
            "parser_version": "smoke",
            "warnings": [],
            "errors": [],
        },
        lines=[
            line("ln_accepted_1", sort_order=1, nm_id=210183919, qty=44, match_status="matched", name="Clear iPhone 14 Pro"),
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


def _get_text(url: str) -> tuple[int, str]:
    with urllib_request.urlopen(url, timeout=10) as response:
        return response.status, response.read().decode("utf-8")


def _get_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    try:
        with urllib_request.urlopen(url, timeout=10) as response:
            return response.status, response.read(), dict(response.headers.items())
    except error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


if __name__ == "__main__":
    main()
