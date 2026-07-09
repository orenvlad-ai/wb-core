"""HTTP/browser-route smoke for Reports default ready-snapshot fallback."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_DAILY_REPORT_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_STOCK_REPORT_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

from sheet_vitrina_v1_reports_ready_snapshot_boundary_smoke import BUNDLE_FIXTURE, NIGHT_NOW, _build_plan


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-reports-ready-browser-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        port = _reserve_free_port()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-05-11T00:00:00Z",
            now_factory=lambda: NIGHT_NOW,
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            upload_status, upload_payload = _post_json(f"{base_url}{DEFAULT_UPLOAD_PATH}", bundle)
            if upload_status != 200 or upload_payload.get("status") != "accepted":
                raise AssertionError(f"bundle upload must be accepted, got {upload_status} {upload_payload}")

            current_state = runtime.load_current_state()
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at="2026-05-09T06:00:00Z",
                plan=_build_plan(as_of_date="2026-05-08", current_state=current_state),
            )
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at="2026-05-10T06:00:00Z",
                plan=_build_plan(as_of_date="2026-05-09", current_state=current_state),
            )

            operator_code, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}?embedded_tab=reports")
            if operator_code != 200:
                raise AssertionError(f"operator reports tab must render, got {operator_code}")
            for token in (
                "Ежедневные отчёты",
                "Отчёт по остаткам",
                "Период усреднения продаж",
                "Настройте SKU, период и столбцы, затем нажмите «Рассчитать».",
                "stockReportColumnSelector",
                "на произв.",
                "в пути Китай",
                "ост. ФФ",
                "поставки ВБ",
                "ост. ВБ",
                "Дн. всего",
                "stock-report-table",
                DEFAULT_SHEET_DAILY_REPORT_PATH,
                DEFAULT_SHEET_STOCK_REPORT_PATH,
            ):
                if token not in operator_html:
                    raise AssertionError(f"operator reports tab must expose {token!r}")

            daily_code, daily_payload = _get_json(f"{base_url}{DEFAULT_SHEET_DAILY_REPORT_PATH}")
            if daily_code != 200 or daily_payload.get("status") != "available":
                raise AssertionError(f"daily report browser route must be available, got {daily_code} {daily_payload}")
            if daily_payload.get("requested_as_of_date") != "2026-05-10":
                raise AssertionError(f"daily route must disclose requested default date, got {daily_payload}")
            if daily_payload.get("newer_closed_date") != "2026-05-09":
                raise AssertionError(f"daily route must use latest persisted ready date, got {daily_payload}")

            stock_code, stock_payload = _get_json(f"{base_url}{DEFAULT_SHEET_STOCK_REPORT_PATH}?sales_avg_period_days=14")
            if stock_code != 200 or stock_payload.get("status") != "available":
                raise AssertionError(f"stock report browser route must be available, got {stock_code} {stock_payload}")
            if stock_payload.get("requested_as_of_date") != "2026-05-10" or stock_payload.get("report_date") != "2026-05-09":
                raise AssertionError(f"stock route default must fallback to persisted ready date, got {stock_payload}")
            if stock_payload.get("sales_avg_period_days") != 14:
                raise AssertionError(f"stock route must accept sales_avg_period_days, got {stock_payload}")
            if "promotion_participation" not in (stock_payload.get("rows") or [{}])[0]:
                raise AssertionError(f"stock route must expose promotion participation field, got {stock_payload}")
            if "districts" not in (stock_payload.get("rows") or [{}])[0]:
                raise AssertionError(f"stock route must expose district stock/days fields, got {stock_payload}")

            strict_code, strict_payload = _get_json(f"{base_url}{DEFAULT_SHEET_STOCK_REPORT_PATH}?as_of_date=2026-05-10&sales_avg_period_days=14")
            if strict_code != 200 or strict_payload.get("status") != "unavailable":
                raise AssertionError(f"explicit stock route must stay strict unavailable, got {strict_code} {strict_payload}")

            invalid_code, invalid_payload = _get_json_allow_error(f"{base_url}{DEFAULT_SHEET_STOCK_REPORT_PATH}?sales_avg_period_days=abc")
            if invalid_code != 422 or "Период усреднения продаж" not in str(invalid_payload.get("error")):
                raise AssertionError(f"invalid sales_avg_period_days must return controlled 422 JSON, got {invalid_code} {invalid_payload}")

            print("reports_ready_browser_operator: ok -> reports tab rendered")
            print("reports_ready_browser_daily: ok -> requested 2026-05-10 selected 2026-05-09")
            print("reports_ready_browser_stock: ok -> default fallback, explicit strict")
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib_request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    with urllib_request.urlopen(url, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _get_json_allow_error(url: str) -> tuple[int, dict[str, object]]:
    try:
        return _get_json(url)
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    with urllib_request.urlopen(url, timeout=10) as response:
        return response.status, response.read().decode("utf-8")


if __name__ == "__main__":
    main()
