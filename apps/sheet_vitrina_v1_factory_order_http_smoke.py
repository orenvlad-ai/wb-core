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
    build_registry_upload_http_server,
)
from packages.application.factory_order_sales_history import persist_sales_history_result_exact_dates
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryItem, SalesFunnelHistorySuccess

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
            entrypoint.factory_order_supply_block.stocks_block = FakeStocksBlock(active_nm_ids)
            fake_history_block = FakeSalesHistoryBlock()
            entrypoint.factory_order_supply_block.sales_funnel_history_block = fake_history_block
            entrypoint.factory_order_supply_block.sales_history.sales_funnel_history_block = fake_history_block

            operator_status, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}")
            if operator_status != 200:
                raise AssertionError(f"operator page must return 200, got {operator_status}")
            for expected in (
                "Обновление данных витрины",
                "Расчёт поставок",
                "Заказ на фабрике",
                "Кратность штук в коробке",
                "Скачать загруженный файл",
                "Удалить этот файл",
                "Рассчитать заказ на фабрике",
            ):
                if expected not in operator_html:
                    raise AssertionError(f"operator page must expose {expected!r}")

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

            inbound_factory_rows = read_first_sheet_rows(inbound_factory_bytes)
            inbound_factory_rows = [
                inbound_factory_rows[0],
                [210183919, "SKU 1", 40, "2026-04-25", ""],
                [210183919, "SKU 1", 12, "2026-05-05", ""],
            ]
            inbound_factory_upload_status, inbound_factory_upload_payload = _post_multipart(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FACTORY_PATH}",
                build_single_sheet_workbook_bytes("В пути от фабрики", inbound_factory_rows),
                filename="factory-inbound.xlsx",
            )
            if inbound_factory_upload_status != 200 or inbound_factory_upload_payload.get("accepted_row_count") != 2:
                raise AssertionError("inbound_factory upload must keep multiple rows per SKU")

            inbound_ff_to_wb_rows = read_first_sheet_rows(inbound_ff_to_wb_bytes)
            inbound_ff_to_wb_rows = [
                inbound_ff_to_wb_rows[0],
                [210183919, "SKU 1", 10, "2026-04-26", ""],
                [210184534, "SKU 2", 25, "2026-04-28", ""],
            ]
            inbound_ff_to_wb_upload_status, inbound_ff_to_wb_upload_payload = _post_multipart(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FF_TO_WB_PATH}",
                build_single_sheet_workbook_bytes("В пути ФФ -> WB", inbound_ff_to_wb_rows),
                filename="ff-to-wb.xlsx",
            )
            if inbound_ff_to_wb_upload_status != 200 or inbound_ff_to_wb_upload_payload.get("accepted_row_count") != 2:
                raise AssertionError("inbound_ff_to_wb upload must be accepted")

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
            if first_sku_with_inbound.get("inbound_factory_to_ff") != 40.0 or first_sku_with_inbound.get("inbound_ff_to_wb") != 10.0:
                raise AssertionError("HTTP calc must keep only the inbound events that can still reach WB inside the planning horizon")

            recommendation_status, recommendation_bytes, recommendation_headers = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH}"
            )
            if recommendation_status != 200 or "spreadsheetml.sheet" not in str(recommendation_headers.get("Content-Type", "")):
                raise AssertionError("recommendation route must return XLSX after calculation")
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

            for period_days in (10, 14, 21):
                covered_status, covered_payload = _post_json(
                    f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
                    {
                        "prod_lead_time_days": 10,
                        "lead_time_factory_to_ff_days": 5,
                        "lead_time_ff_to_wb_days": 2,
                        "safety_days_mp": 3,
                        "safety_days_ff": 2,
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
                raise AssertionError(f"HTTP calc must keep legacy default sales_avg_period_days, got {default_period_status} {default_period_payload}")
            default_sku = next(item for item in default_period_payload.get("rows", []) if item.get("nm_id") == 210183919)
            if round(float(default_sku.get("daily_demand_total", 0.0)), 2) != _expected_average(
                210183919,
                report_date="2026-04-18",
                period_days=21,
            ):
                raise AssertionError("missing sales_avg_period_days must fall back to the legacy 21-day window in HTTP calc")
            if int(default_period_payload.get("settings", {}).get("sales_avg_period_days", 0)) != 21:
                raise AssertionError("HTTP calc payload must persist the legacy 21-day default window")

            calc_out_of_range_status, calc_out_of_range_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
                {
                    "prod_lead_time_days": 10,
                    "lead_time_factory_to_ff_days": 5,
                    "lead_time_ff_to_wb_days": 2,
                    "safety_days_mp": 3,
                    "safety_days_ff": 2,
                    "order_batch_qty": 50,
                    "report_date_override": "2026-04-18",
                    "sales_avg_period_days": 60,
                },
            )
            error_text = str(calc_out_of_range_payload.get("error", ""))
            if calc_out_of_range_status != 422 or "нужен диапазон 2026-02-17..2026-04-17" not in error_text or "2026-03-28..2026-04-17" not in error_text:
                raise AssertionError("HTTP calc must surface the exact coverage blocker outside the persisted runtime window")

            print(f"scenario_without_inbound_http: ok -> total_qty={calc_without_inbound_payload['summary']['total_qty']}")
            print(f"scenario_current_file_lifecycle_http: ok -> {inbound_factory_state['uploaded_filename']}")
            print(
                f"scenario_multi_inbound_http: ok -> inbound_factory={first_sku_with_inbound.get('inbound_factory_to_ff', 0.0)}"
            )
            print("scenario_covered_windows_http: ok -> periods=10,14,21")
            print(f"scenario_default_sales_avg_http: ok -> daily_demand={round(float(default_sku.get('daily_demand_total', 0.0)), 2)}")
            print("scenario_out_of_range_http: ok -> blocker exposes needed range 2026-02-17..2026-04-17 and available 2026-03-28..2026-04-17")
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
