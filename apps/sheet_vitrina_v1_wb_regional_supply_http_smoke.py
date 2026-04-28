"""HTTP end-to-end smoke-check for the WB regional supply operator flow."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from urllib import error, request as urllib_request

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_FACTORY_ORDER_DELETE_STOCK_FF_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH,
    DEFAULT_FACTORY_ORDER_UPLOAD_STOCK_FF_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    DEFAULT_WB_REGIONAL_CALCULATE_PATH,
    DEFAULT_WB_REGIONAL_STATUS_PATH,
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
        raise AssertionError("runtime coverage is fully seeded; live fetch must not be called in HTTP smoke")


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-wb-regional-http-") as tmp:
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
            entrypoint.wb_regional_supply_block.stocks_block = FakeStocksBlock(active_nm_ids)
            entrypoint.wb_regional_supply_block.sales_funnel_history_block = NoopSalesHistoryBlock()
            entrypoint.wb_regional_supply_block.sales_history.sales_funnel_history_block = NoopSalesHistoryBlock()

            operator_status, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}?embedded_tab=factory-order")
            if operator_status != 200:
                raise AssertionError(f"operator page must return 200, got {operator_status}")
            for expected in (
                "Общий вход для двух расчётов",
                "Поставка на Wildberries",
                "Цикл поставок, дней",
                "Доставка до склада Wildberries, дней",
                "Рассчитать поставку на Wildberries",
                "Сводка по федеральным округам",
                "Скачать Excel",
                "<th>Общее количество</th>",
                "<th>Дефицит</th>",
                "<th>Скачать Excel</th>",
                "data-regional-district-download",
            ):
                if expected not in operator_html:
                    raise AssertionError(f"operator page must expose {expected!r}")
            for removed in ("XLSX по округам", "Excel по округам", "regionalDistrictDownloads", "district-download-list"):
                if removed in operator_html:
                    raise AssertionError(f"regional UI must not render duplicated district download block token {removed!r}")
            if "https://docs.google.com/spreadsheets/d/" in operator_html:
                raise AssertionError("wb-regional supply surface must not expose legacy Google Sheets as an active link")
            if "value=\"14\"" not in operator_html or "value=\"15\"" not in operator_html or "value=\"250\"" not in operator_html:
                raise AssertionError("operator page must prefill the WB defaults directly in the form")

            stock_template_status, stock_template_bytes, _ = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH}"
            )
            if stock_template_status != 200:
                raise AssertionError("shared stock_ff template route must stay available")
            stock_rows = read_first_sheet_rows(stock_template_bytes)
            stock_upload_rows = [list(row) for row in stock_rows]
            for row in stock_upload_rows[1:]:
                row[2] = 0
            for row in stock_upload_rows[1:]:
                if int(row[0]) == MAIN_NM_ID:
                    row[2] = 120
                    break
            stock_upload_status, stock_upload_payload = _post_multipart(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOAD_STOCK_FF_PATH}",
                build_single_sheet_workbook_bytes("Остатки ФФ", stock_upload_rows),
                filename="shared-stock-ff.xlsx",
            )
            if stock_upload_status != 200 or stock_upload_payload.get("dataset", {}).get("uploaded_filename") != "shared-stock-ff.xlsx":
                raise AssertionError("stock_ff upload must stay shared and downloadable")

            regional_status_code, regional_status_payload = _get_json(f"{base_url}{DEFAULT_WB_REGIONAL_STATUS_PATH}")
            if regional_status_code != 200:
                raise AssertionError("regional status route must return 200")
            shared_dataset = regional_status_payload.get("shared_datasets", {}).get("stock_ff", {})
            if shared_dataset.get("uploaded_filename") != "shared-stock-ff.xlsx":
                raise AssertionError("regional status must expose the shared stock_ff filename")
            if shared_dataset.get("download_path") != "/v1/sheet-vitrina-v1/supply/factory-order/uploaded/stock-ff.xlsx":
                raise AssertionError("regional status must point to the shared stock_ff download route")

            calc_status, calc_payload = _post_json(
                f"{base_url}{DEFAULT_WB_REGIONAL_CALCULATE_PATH}",
                {
                    "sales_avg_period_days": 14,
                    "cycle_supply_days": 5,
                    "lead_time_to_region_days": 2,
                    "safety_days": 1,
                    "order_batch_qty": 50,
                    "report_date_override": "2026-04-18",
                },
            )
            if calc_status != 200:
                raise AssertionError(f"regional calculate route must succeed, got {calc_status} {calc_payload}")
            districts = {item["district_key"]: item for item in calc_payload.get("districts", [])}
            if districts["central"]["total_qty"] != 50 or districts["central"]["deficit_qty"] != 100:
                raise AssertionError("regional summary must expose truthful central allocation and deficit")
            if districts["northwest"]["total_qty"] != 50 or districts["northwest"]["deficit_qty"] != 100:
                raise AssertionError("regional summary must expose truthful northwest allocation and deficit")
            if sum(int(item.get("total_qty", 0)) for item in calc_payload.get("districts", [])) != int(calc_payload.get("summary", {}).get("total_qty", 0)):
                raise AssertionError("regional HTTP summary total must equal the sum of district totals")

            central_download_path = districts["central"].get("download_path")
            if central_download_path != "/v1/sheet-vitrina-v1/supply/wb-regional/district/central.xlsx":
                raise AssertionError("regional district route must use narrow server-owned download path")
            district_status, district_bytes, district_headers = _get_bytes(f"{base_url}{central_download_path}")
            if district_status != 200 or "spreadsheetml.sheet" not in str(district_headers.get("Content-Type", "")):
                raise AssertionError("district download route must return XLSX")
            load_workbook(BytesIO(district_bytes), data_only=True)
            district_rows = read_first_sheet_rows(district_bytes)
            if district_rows[2] != ["nmId", "SKU", "Количество к поставке", "Дефицит"]:
                raise AssertionError(f"district XLSX must expose the deficit column, got {district_rows[2]}")
            district_qty_sum = sum(int(row[2]) for row in district_rows[3:] if len(row) >= 3 and str(row[2]).strip())
            district_deficit_sum = sum(int(row[3]) for row in district_rows[3:] if len(row) >= 4 and str(row[3]).strip())
            if district_qty_sum != districts["central"]["total_qty"]:
                raise AssertionError("district XLSX must match the regional summary total for the same district")
            if district_deficit_sum != districts["central"]["deficit_qty"]:
                raise AssertionError("district XLSX deficit must match the regional summary deficit for the same district")

            delete_status, delete_payload = _delete_json(f"{base_url}{DEFAULT_FACTORY_ORDER_DELETE_STOCK_FF_PATH}")
            if delete_status != 200 or delete_payload.get("status") != "deleted":
                raise AssertionError("shared stock_ff delete route must still work")
            blocked_status, blocked_payload = _post_json(
                f"{base_url}{DEFAULT_WB_REGIONAL_CALCULATE_PATH}",
                {
                    "sales_avg_period_days": 14,
                    "cycle_supply_days": 5,
                    "lead_time_to_region_days": 2,
                    "safety_days": 1,
                    "order_batch_qty": 50,
                    "report_date_override": "2026-04-18",
                },
            )
            if blocked_status != 422 or "Остатки ФФ" not in str(blocked_payload.get("error", "")):
                raise AssertionError("regional calculate must truthfully block when shared stock_ff is missing")

            print(f"regional_status_shared_stock: ok -> {shared_dataset.get('uploaded_filename')}")
            print(f"regional_summary_total: ok -> {calc_payload.get('summary', {}).get('total_qty')}")
            print(f"regional_central_deficit: ok -> {districts['central']['deficit_qty']}")
            print(f"regional_district_xlsx_sum: ok -> {district_qty_sum}")
            print(f"regional_district_xlsx_deficit_sum: ok -> {district_deficit_sum}")
            print(f"regional_missing_shared_blocker: ok -> {blocked_payload.get('error')}")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def _seed_runtime_sales_history(runtime: RegistryUploadDbBackedRuntime, *, active_nm_ids: list[int]) -> None:
    report_date = date(2026, 4, 18)
    items: list[SalesFunnelHistoryItem] = []
    for offset in range(14, 0, -1):
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
            date_from="2026-04-04",
            date_to="2026-04-17",
            count=len(items),
            items=items,
        ),
        captured_at=ACTIVATED_AT,
    )


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(url: str, *, method: str = "GET", body: bytes | None = None, headers: dict[str, str] | None = None):
    req = urllib_request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, response.read(), response.headers
    except error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    status, body, _ = _request(
        url,
        method="POST",
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
    )
    return status, json.loads(body.decode("utf-8"))


def _delete_json(url: str) -> tuple[int, dict[str, object]]:
    status, body, _ = _request(url, method="DELETE", headers={"Accept": "application/json"})
    return status, json.loads(body.decode("utf-8"))


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    status, body, _ = _request(url, headers={"Accept": "application/json"})
    return status, json.loads(body.decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    status, body, _ = _request(url)
    return status, body.decode("utf-8")


def _get_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    status, body, headers = _request(url)
    return status, body, dict(headers.items())


def _post_multipart(url: str, workbook_bytes: bytes, *, filename: str) -> tuple[int, dict[str, object]]:
    boundary = "----wb-core-regional-smoke-boundary"
    body = (
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n"
            "\r\n"
        ).encode("utf-8")
        + workbook_bytes
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    status, response_body, _ = _request(
        url,
        method="POST",
        body=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    return status, json.loads(response_body.decode("utf-8"))


if __name__ == "__main__":
    main()
