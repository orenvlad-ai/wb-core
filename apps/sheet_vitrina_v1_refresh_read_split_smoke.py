"""Integration smoke-check для split refresh/read в sheet_vitrina_v1."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from urllib import error, parse as urllib_parse, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_JOB_PATH,
    DEFAULT_SHEET_LOAD_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-13T12:00:03Z"
REFRESHED_AT = "2026-04-13T12:05:00Z"
AS_OF_DATE = "2026-04-12"
TODAY_CURRENT_DATE = "2026-04-13"
SERVER_NOW = datetime(2026, 4, 13, 8, 0, tzinfo=timezone.utc)
CURRENT_ONLY_SOURCE_KEYS = {"prices_snapshot", "ads_bids"}


class CountingBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key
        self.request_dates: list[str] = []

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        self.request_dates.append(request_date)
        payload = SimpleNamespace(
            kind="success",
            items=_build_items(self.source_key),
            snapshot_date=request_date,
            date=request_date,
            date_from=request_date,
            date_to=request_date,
            detail=f"{self.source_key} synthetic success for {request_date}",
            storage_total=None,
        )
        return SimpleNamespace(result=payload)


class _NoopClosedDayWebSourceSync:
    def ensure_closed_day_snapshot(self, *, source_key: str, snapshot_date: str) -> None:
        return


def _build_items(source_key: str) -> list[SimpleNamespace]:
    if source_key == "seller_funnel_snapshot":
        return [SimpleNamespace(nm_id=100000001, view_count=11, open_card_count=3)]
    if source_key == "web_source_snapshot":
        return [SimpleNamespace(nm_id=100000001, views_current=7, ctr_current=0.21, orders_current=2)]
    if source_key == "sales_funnel_history":
        return [SimpleNamespace(nm_id=100000001, add_to_cart_count=5, orders_count=2)]
    if source_key in {"prices_snapshot", "sf_period", "spp", "ads_bids", "stocks", "ads_compact", "fin_report_daily"}:
        return [SimpleNamespace(nm_id=100000001)]
    return []


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    with TemporaryDirectory(prefix="sheet-vitrina-refresh-read-split-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            refreshed_at_factory=lambda: REFRESHED_AT,
            now_factory=lambda: SERVER_NOW,
        )
        counters = _build_counting_blocks()
        entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            now_factory=lambda: SERVER_NOW,
            closed_day_web_source_sync=_NoopClosedDayWebSourceSync(),
            **counters,
        )

        port = _reserve_free_port()
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            upload_url = f"http://127.0.0.1:{config.port}{config.upload_path}"
            plan_url = f"http://127.0.0.1:{config.port}{config.sheet_plan_path}"
            operator_ui_url = f"http://127.0.0.1:{config.port}{config.sheet_operator_ui_path}"

            operator_ui_status, operator_ui_html = _get_text(operator_ui_url)
            if operator_ui_status != 200:
                raise AssertionError(f"operator UI must return 200, got {operator_ui_status}")
            if (
                "Обновление данных витрины" not in operator_ui_html
                or "Загрузить данные" not in operator_ui_html
                or "Отправить данные" not in operator_ui_html
            ):
                raise AssertionError("operator UI must expose the expected operator controls")
            if "Статус" not in operator_ui_html or "Лог" not in operator_ui_html or "ожидание" not in operator_ui_html:
                raise AssertionError("operator UI must keep the compact Russian chrome")
            if "Скачать лог" not in operator_ui_html or "max-height: 420px" not in operator_ui_html:
                raise AssertionError("operator UI must expose log download control and fixed-height log viewport")
            if "Строки DATA_VITRINA" not in operator_ui_html or "Строки STATUS" not in operator_ui_html:
                raise AssertionError("operator UI must expose row-count fields with Russian labels")
            if "Сервер и расписание" not in operator_ui_html or "Часовой пояс" not in operator_ui_html:
                raise AssertionError("operator UI must expose the compact server context block")
            if (
                "Автообновление" not in operator_ui_html
                or "Последний автозапуск" not in operator_ui_html
                or "Статус последнего автозапуска" not in operator_ui_html
                or "Последнее успешное автообновление" not in operator_ui_html
                or "Технический триггер" not in operator_ui_html
            ):
                raise AssertionError("operator UI must expose scheduler labels in Russian")
            if "Снимок пока не подготовлен." not in operator_ui_html:
                raise AssertionError("operator UI must keep the Russian empty-state helper text")
            if (
                "UTC yesterday" in operator_ui_html
                or "server-side refresh" in operator_ui_html
                or "Ready snapshot пока не materialized." in operator_ui_html
            ):
                raise AssertionError("operator UI must not keep the stale explanatory date subtitle")
            operator_ui_config = _extract_operator_ui_config(operator_ui_html)
            if operator_ui_config["refresh_path"] != config.sheet_refresh_path:
                raise AssertionError("operator UI must point to the existing refresh endpoint")
            if operator_ui_config["load_path"] != DEFAULT_SHEET_LOAD_PATH:
                raise AssertionError("operator UI must point to the existing load endpoint")
            if operator_ui_config["status_path"] != config.sheet_status_path:
                raise AssertionError("operator UI must point to the cheap status endpoint")
            if operator_ui_config["job_path"] != DEFAULT_SHEET_JOB_PATH:
                raise AssertionError("operator UI must point to the operator job endpoint")
            refresh_url = f"http://127.0.0.1:{config.port}{operator_ui_config['refresh_path']}"
            status_url = (
                f"http://127.0.0.1:{config.port}{operator_ui_config['status_path']}"
                f"?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}"
            )

            upload_status, upload_payload = _post_json(upload_url, bundle)
            if upload_status != 200 or upload_payload["status"] != "accepted":
                raise AssertionError(f"fixture upload must be accepted, got {upload_status} {upload_payload}")

            missing_load = _run_load_harness(upload_url, as_of_date=AS_OF_DATE)
            if "ready snapshot missing" not in missing_load["load_error"]:
                raise AssertionError(f"load must surface ready-snapshot miss, got {missing_load['load_error']!r}")
            if any(block.request_dates for block in counters.values()):
                raise AssertionError("missing snapshot read path must not trigger heavy source blocks")

            missing_status, missing_payload = _get_json(f"{plan_url}?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}")
            if missing_status != 422 or "ready snapshot missing" not in str(missing_payload.get("error", "")):
                raise AssertionError("cheap read endpoint must report missing ready snapshot before refresh")

            pre_refresh_status, pre_refresh_payload = _get_json(status_url)
            if pre_refresh_status != 422 or "ready snapshot missing" not in str(pre_refresh_payload.get("error", "")):
                raise AssertionError("cheap status endpoint must report missing ready snapshot before refresh")
            if pre_refresh_payload.get("server_context") != _expected_server_context():
                raise AssertionError("cheap status endpoint must expose server_context even before refresh")

            refresh_status, refresh_payload = _post_json(refresh_url, {"as_of_date": AS_OF_DATE})
            if refresh_status != 200:
                raise AssertionError(f"refresh endpoint must return 200, got {refresh_status}")
            if refresh_payload["status"] != "success":
                raise AssertionError("refresh endpoint must report success")
            if refresh_payload["refreshed_at"] != REFRESHED_AT:
                raise AssertionError("refresh_result refreshed_at mismatch")
            if refresh_payload["as_of_date"] != AS_OF_DATE:
                raise AssertionError("refresh_result as_of_date mismatch")
            if refresh_payload["date_columns"] != [AS_OF_DATE, TODAY_CURRENT_DATE]:
                raise AssertionError("refresh_result date_columns mismatch")
            if refresh_payload["server_context"] != _expected_server_context():
                raise AssertionError("refresh_result must expose the same server_context block")
            if [slot["slot_key"] for slot in refresh_payload["temporal_slots"]] != [
                "yesterday_closed",
                "today_current",
            ]:
                raise AssertionError("refresh_result temporal_slots mismatch")
            _assert_counting_calls(counters)

            status_after_refresh, status_payload = _get_json(status_url)
            if status_after_refresh != 200:
                raise AssertionError(f"status endpoint must return 200 after refresh, got {status_after_refresh}")
            if status_payload["snapshot_id"] != refresh_payload["snapshot_id"]:
                raise AssertionError("status endpoint must return persisted refresh metadata")
            if status_payload["refreshed_at"] != REFRESHED_AT:
                raise AssertionError("status endpoint refreshed_at mismatch")
            if status_payload["sheet_row_counts"] != refresh_payload["sheet_row_counts"]:
                raise AssertionError("status endpoint row counts must match refresh result")
            if status_payload["date_columns"] != [AS_OF_DATE, TODAY_CURRENT_DATE]:
                raise AssertionError("status endpoint must expose both materialized dates")
            if status_payload["server_context"] != refresh_payload["server_context"]:
                raise AssertionError("status endpoint must expose the same server_context metadata")

            plan_status, plan_payload = _get_json(f"{plan_url}?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}")
            if plan_status != 200:
                raise AssertionError(f"plan read endpoint must return 200 after refresh, got {plan_status}")
            if plan_payload["snapshot_id"] != refresh_payload["snapshot_id"]:
                raise AssertionError("read endpoint must return the persisted ready snapshot")
            if plan_payload["date_columns"] != [AS_OF_DATE, TODAY_CURRENT_DATE]:
                raise AssertionError("plan endpoint must expose both materialized dates")
            if any(
                block.request_dates
                != (
                    [TODAY_CURRENT_DATE]
                    if block.source_key in CURRENT_ONLY_SOURCE_KEYS
                    else ([AS_OF_DATE] if block.source_key == "stocks" else [AS_OF_DATE, TODAY_CURRENT_DATE])
                )
                for block in counters.values()
            ):
                raise AssertionError("cheap read endpoint must not trigger heavy source blocks after refresh")
            status_sheet = next(sheet for sheet in plan_payload["sheets"] if sheet["sheet_name"] == "STATUS")
            status_rows = {row[0]: row for row in status_sheet["rows"]}
            if status_rows["stocks[yesterday_closed]"][1] != "success":
                raise AssertionError("stocks yesterday_closed must materialize historical closed-day data")
            if status_rows["stocks[today_current]"][1] != "not_available":
                raise AssertionError("stocks today_current must stay explicitly unavailable")
            if status_rows["prices_snapshot[yesterday_closed]"][1] != "not_available":
                raise AssertionError("prices yesterday_closed must stay explicitly unavailable")
            if status_rows["prices_snapshot[today_current]"][1] != "success":
                raise AssertionError("prices today_current must materialize current data")
            if status_rows["seller_funnel_snapshot[yesterday_closed]"][1] != "success":
                raise AssertionError("dual-day source must materialize yesterday_closed")
            if status_rows["seller_funnel_snapshot[today_current]"][1] != "success":
                raise AssertionError("dual-day source must materialize today_current")
            data_sheet = next(sheet for sheet in plan_payload["sheets"] if sheet["sheet_name"] == "DATA_VITRINA")
            if data_sheet["header"] != ["label", "key", AS_OF_DATE, TODAY_CURRENT_DATE]:
                raise AssertionError("DATA_VITRINA plan header must contain yesterday + today")

            ready_load = _run_load_harness(upload_url, as_of_date=AS_OF_DATE)
            if ready_load["load_error"]:
                raise AssertionError(f"sheet-side load must succeed after refresh, got {ready_load['load_error']!r}")
            if ready_load["load_result"]["http_status"] != 200:
                raise AssertionError("sheet-side load must receive 200 from cheap read endpoint")
            if ready_load["sheets"]["DATA_VITRINA"]["values"][0] != ["дата", "key", AS_OF_DATE, TODAY_CURRENT_DATE]:
                raise AssertionError("sheet-side load must materialize yesterday + today")
            if ready_load["sheets"]["STATUS"]["values"][1][0] != "registry_upload_current_state":
                raise AssertionError("STATUS sheet must be materialized from ready snapshot")
            if any(
                block.request_dates
                != (
                    [TODAY_CURRENT_DATE]
                    if block.source_key in CURRENT_ONLY_SOURCE_KEYS
                    else ([AS_OF_DATE] if block.source_key == "stocks" else [AS_OF_DATE, TODAY_CURRENT_DATE])
                )
                for block in counters.values()
            ):
                raise AssertionError("sheet-side load must not trigger heavy source blocks after refresh")

            print(f"missing_snapshot: ok -> {missing_load['load_error']}")
            print(f"operator_page: ok -> {config.sheet_operator_ui_path}")
            print(f"refresh_endpoint: ok -> {refresh_payload['snapshot_id']}")
            print(f"status_endpoint: ok -> {status_payload['snapshot_id']}")
            print(f"cheap_read_endpoint: ok -> {plan_payload['snapshot_id']}")
            print("sheet_load: ok -> ready snapshot only")
            print("smoke-check passed")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def _build_counting_blocks() -> dict[str, CountingBlock]:
    return {
        "web_source_block": CountingBlock("web_source_snapshot"),
        "seller_funnel_block": CountingBlock("seller_funnel_snapshot"),
        "sales_funnel_history_block": CountingBlock("sales_funnel_history"),
        "prices_snapshot_block": CountingBlock("prices_snapshot"),
        "sf_period_block": CountingBlock("sf_period"),
        "spp_block": CountingBlock("spp"),
        "ads_bids_block": CountingBlock("ads_bids"),
        "stocks_block": CountingBlock("stocks"),
        "ads_compact_block": CountingBlock("ads_compact"),
        "fin_report_daily_block": CountingBlock("fin_report_daily"),
    }


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    raise AssertionError("synthetic source request must carry a date field")


def _assert_counting_calls(counters: dict[str, CountingBlock]) -> None:
    expected_by_source = {
        "web_source_snapshot": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "seller_funnel_snapshot": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "sales_funnel_history": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "prices_snapshot": [TODAY_CURRENT_DATE],
        "sf_period": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "spp": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "ads_bids": [TODAY_CURRENT_DATE],
        "stocks": [AS_OF_DATE],
        "ads_compact": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "fin_report_daily": [AS_OF_DATE, TODAY_CURRENT_DATE],
    }
    for block in counters.values():
        expected = expected_by_source[block.source_key]
        if block.request_dates != expected:
            raise AssertionError(
                f"{block.source_key} request_dates mismatch: expected {expected}, got {block.request_dates}"
            )


def _run_load_harness(endpoint_url: str, as_of_date: str) -> dict[str, object]:
    return json.loads(
        subprocess.check_output(
            [
                "node",
                str(ROOT / "apps" / "sheet_vitrina_v1_registry_upload_trigger_harness.js"),
                "--mode",
                "load_only",
                "--scriptPath",
                str(ROOT / "gas" / "sheet_vitrina_v1" / "RegistryUploadTrigger.gs"),
                "--endpointUrl",
                endpoint_url,
                "--asOfDate",
                as_of_date,
            ],
            cwd=ROOT,
            text=True,
        )
    )


def _post_json(url: str, payload: object) -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, object]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, response.read().decode("utf-8")
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _extract_operator_ui_config(html: str) -> dict[str, object]:
    match = re.search(
        r'<script id="sheet-vitrina-v1-operator-config" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError("operator UI config script is missing")
    return json.loads(match.group(1))


def _expected_server_context() -> dict[str, str]:
    return {
        "business_timezone": "Asia/Yekaterinburg",
        "business_now": "2026-04-13T13:00:00+05:00",
        "default_as_of_date": AS_OF_DATE,
        "today_current_date": TODAY_CURRENT_DATE,
        "daily_refresh_business_time": "11:00 Asia/Yekaterinburg",
        "daily_refresh_systemd_time": "06:00:00 UTC",
        "daily_refresh_systemd_oncalendar": "*-*-* 06:00:00 UTC",
        "daily_auto_action": "загрузка данных + отправка данных в таблицу",
        "daily_auto_description": "Ежедневно в 11:00 Asia/Yekaterinburg: загрузка данных + отправка данных в таблицу",
        "daily_auto_trigger_name": "wb-core-sheet-vitrina-refresh.timer",
        "daily_auto_trigger_description": "wb-core-sheet-vitrina-refresh.timer -> POST /v1/sheet-vitrina-v1/refresh (auto_load=true)",
        "last_auto_run_status": "never",
        "last_auto_run_status_label": "ещё не выполнялся",
        "last_auto_run_time": "",
        "last_auto_run_finished_at": "",
        "last_successful_auto_update_at": "",
        "last_auto_run_error": "",
    }


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
