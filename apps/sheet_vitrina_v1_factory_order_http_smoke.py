"""HTTP end-to-end smoke-check for the factory-order operator flow."""

from __future__ import annotations

from datetime import datetime, timezone
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
    DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH,
    DEFAULT_FACTORY_ORDER_STATUS_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH,
    DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH,
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
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

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
            entrypoint.factory_order_supply_block.stocks_block = FakeStocksBlock(active_nm_ids)
            entrypoint.factory_order_supply_block.sales_funnel_history_block = FakeSalesHistoryBlock()

            operator_status, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}")
            if operator_status != 200:
                raise AssertionError(f"operator page must return 200, got {operator_status}")
            for expected in (
                "Обновление данных витрины",
                "Расчёт поставок",
                "Заказ на фабрике",
                "Скачать шаблон остатков ФФ",
                "Скачать шаблон товаров в пути от фабрики",
                "Скачать шаблон товаров в пути от ФФ на Wildberries",
                "Рассчитать заказ на фабрике",
                "Скачать рекомендацию",
            ):
                if expected not in operator_html:
                    raise AssertionError(f"operator page must expose {expected!r}")

            stock_template_status, stock_template_bytes, stock_template_headers = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH}"
            )
            if stock_template_status != 200:
                raise AssertionError("stock_ff template route must return XLSX")
            if "spreadsheetml.sheet" not in str(stock_template_headers.get("Content-Type", "")):
                raise AssertionError("stock_ff template route must return XLSX content type")
            stock_rows = read_first_sheet_rows(stock_template_bytes)
            if len(stock_rows) - 1 != len(active_nm_ids):
                raise AssertionError("stock_ff template must be prefilled with active SKU rows")

            inbound_factory_status, inbound_factory_bytes, _ = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH}"
            )
            inbound_ff_to_wb_status, inbound_ff_to_wb_bytes, _ = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH}"
            )
            if inbound_factory_status != 200 or inbound_ff_to_wb_status != 200:
                raise AssertionError("inbound template routes must return XLSX")

            stock_upload_rows = [list(row) for row in stock_rows]
            for row in stock_upload_rows[1:]:
                row[2] = 0
            stock_upload_rows[1][2] = 30
            stock_upload_rows[2][2] = 10
            stock_upload_status, stock_upload_payload = _post_multipart(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOAD_STOCK_FF_PATH}",
                build_single_sheet_workbook_bytes("Остатки ФФ", stock_upload_rows),
            )
            if stock_upload_status != 200 or stock_upload_payload.get("accepted_row_count") != len(active_nm_ids):
                raise AssertionError(f"stock_ff upload must be accepted, got {stock_upload_status} {stock_upload_payload}")

            inbound_factory_rows = read_first_sheet_rows(inbound_factory_bytes)
            inbound_factory_rows = [
                inbound_factory_rows[0],
                [210183919, "SKU 1", 40, "2026-04-25", ""],
                [210183919, "SKU 1", 15, "2026-05-20", ""],
            ]
            inbound_factory_upload_status, inbound_factory_upload_payload = _post_multipart(
                f"{base_url}{DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FACTORY_PATH}",
                build_single_sheet_workbook_bytes("В пути от фабрики", inbound_factory_rows),
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
            )
            if inbound_ff_to_wb_upload_status != 200 or inbound_ff_to_wb_upload_payload.get("accepted_row_count") != 2:
                raise AssertionError("inbound_ff_to_wb upload must be accepted")

            status_code, status_payload = _get_json(f"{base_url}{DEFAULT_FACTORY_ORDER_STATUS_PATH}")
            if status_code != 200:
                raise AssertionError("factory-order status route must return 200")
            if status_payload.get("datasets", {}).get("stock_ff", {}).get("row_count") != len(active_nm_ids):
                raise AssertionError("factory-order status must reflect uploaded stock_ff state")

            calc_status, calc_payload = _post_json(
                f"{base_url}{DEFAULT_FACTORY_ORDER_CALCULATE_PATH}",
                {
                    "prod_lead_time_days": 10,
                    "lead_time_factory_to_ff_days": 5,
                    "lead_time_ff_to_wb_days": 2,
                    "safety_days_mp": 3,
                    "safety_days_ff": 2,
                    "order_batch_qty": 50,
                    "report_date_override": "2026-04-18",
                    "sales_avg_period_days": 2,
                },
            )
            if calc_status != 200 or calc_payload.get("summary", {}).get("total_qty") != 250:
                raise AssertionError(f"factory-order calc must succeed, got {calc_status} {calc_payload}")
            if calc_payload.get("rows", [])[0].get("recommended_order_qty") != 150:
                raise AssertionError("factory-order calc must expose server-side recommended qty")

            recommendation_status, recommendation_bytes, recommendation_headers = _get_bytes(
                f"{base_url}{DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH}"
            )
            if recommendation_status != 200:
                raise AssertionError("recommendation route must return XLSX")
            if "spreadsheetml.sheet" not in str(recommendation_headers.get("Content-Type", "")):
                raise AssertionError("recommendation route must return XLSX content type")
            recommendation_rows = read_first_sheet_rows(recommendation_bytes)
            if recommendation_rows[-3:] != [
                ["Общее количество", None, 250],
                ["Расчётный вес", None, "21.48"],
                ["Расчётный объём", None, "0.11"],
            ]:
                raise AssertionError("recommendation workbook summary must match the UI summary")

            final_status_code, final_status_payload = _get_json(f"{base_url}{DEFAULT_FACTORY_ORDER_STATUS_PATH}")
            if final_status_code != 200:
                raise AssertionError("factory-order status route must remain readable after calculation")
            if final_status_payload.get("last_result", {}).get("summary", {}).get("total_qty") != 250:
                raise AssertionError("factory-order status must persist the last calculation result")

            print(f"operator_tab: ok -> {DEFAULT_SHEET_OPERATOR_UI_PATH}")
            print(f"stock_ff_upload: ok -> {stock_upload_payload['accepted_row_count']}")
            print(f"factory_order_total_qty: ok -> {calc_payload['summary']['total_qty']}")
            print(f"recommendation_download: ok -> {recommendation_headers.get('Content-Type')}")
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


def _post_multipart(url: str, workbook_bytes: bytes) -> tuple[int, dict[str, object]]:
    boundary = "----wbcore" + uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            b'Content-Disposition: form-data; name="file"; filename="input.xlsx"\r\n',
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


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    try:
        with urllib_request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    with urllib_request.urlopen(url, timeout=10) as response:
        return response.status, response.read().decode("utf-8")


def _get_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    with urllib_request.urlopen(url, timeout=10) as response:
        return response.status, response.read(), dict(response.headers.items())


if __name__ == "__main__":
    main()
