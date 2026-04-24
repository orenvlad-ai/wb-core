"""Focused smoke-check for sheet_vitrina_v1 operator async refresh/load contract."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
from typing import Callable
from urllib import error, parse as urllib_parse, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_JOB_PATH,
    DEFAULT_SHEET_LOAD_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-13T12:00:03Z"
REFRESHED_AT = "2026-04-13T12:05:00Z"
AS_OF_DATE = "2026-04-12"
TODAY_CURRENT_DATE = "2026-04-13"
SERVER_NOW = datetime(2026, 4, 13, 8, 0, tzinfo=timezone.utc)
class CountingBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key
        self.request_dates: list[str] = []

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        self.request_dates.append(request_date)
        payload = SimpleNamespace(
            kind="success",
            items=[],
            snapshot_date=request_date,
            date=request_date,
            date_from=request_date,
            date_to=request_date,
            detail=f"{self.source_key} synthetic success for {request_date}",
            storage_total=None,
        )
        return SimpleNamespace(result=payload)


class DelayedFakeSheetLoadRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, plan: SheetVitrinaV1Envelope, log: Callable[[str], None]) -> dict[str, object]:
        self.calls.append(plan.snapshot_id)
        row_counts = {item.sheet_name: item.row_count for item in plan.sheets}
        log("Тестовый sheet bridge принял ready snapshot.")
        time.sleep(0.35)
        log("Тестовый sheet bridge записал данные в live shell.")
        return {
            "bridge": "fake",
            "write_result": {
                "spreadsheet_id": "fake-sheet",
                "snapshot_id": plan.snapshot_id,
                "sheets": [
                    {
                        "sheet_name": item.sheet_name,
                        "row_count": item.row_count,
                        "write_rect": item.write_rect,
                    }
                    for item in plan.sheets
                ],
            },
            "sheet_state": {
                "spreadsheet_id": "fake-sheet",
                "sheets": [
                    {
                        "sheet_name": item.sheet_name,
                        "present": True,
                        "last_row": item.row_count + 1,
                        "last_column": item.column_count,
                    }
                    for item in plan.sheets
                ],
            },
            "sheet_row_counts": row_counts,
        }


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    with TemporaryDirectory(prefix="sheet-vitrina-operator-load-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        counters = _build_counting_blocks()
        load_runner = DelayedFakeSheetLoadRunner()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            refreshed_at_factory=lambda: REFRESHED_AT,
            now_factory=lambda: SERVER_NOW,
        )
        entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            now_factory=lambda: SERVER_NOW,
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
            base_url = f"http://127.0.0.1:{config.port}"
            upload_url = f"{base_url}{config.upload_path}"
            refresh_url = f"{base_url}{config.sheet_refresh_path}"
            load_url = f"{base_url}{DEFAULT_SHEET_LOAD_PATH}"
            job_url = f"{base_url}{DEFAULT_SHEET_JOB_PATH}"
            operator_url = f"{base_url}{config.sheet_operator_ui_path}"

            operator_status, operator_html = _get_text(operator_url)
            if operator_status != 200:
                raise AssertionError(f"operator UI must return 200, got {operator_status}")
            for expected in (
                "Ручная загрузка данных",
                "Legacy Google Sheets",
                "архивирован",
                "Скачать лог",
                "Лог",
                "Автообновления",
                "Последняя удачная загрузка",
                "Legacy Google Sheets",
                "Последний автозапуск",
                "Статус последнего автозапуска",
                "Последнее успешное автообновление",
                "max-height: 420px",
                DEFAULT_SHEET_JOB_PATH,
            ):
                if expected not in operator_html:
                    raise AssertionError(f"operator UI must expose {expected!r}")

            upload_status, upload_payload = _post_json(upload_url, bundle)
            if upload_status != 200 or upload_payload.get("status") != "accepted":
                raise AssertionError(f"fixture upload must be accepted, got {upload_status} {upload_payload}")

            missing_load_status, missing_load_payload = _post_json(load_url, {"as_of_date": AS_OF_DATE})
            if missing_load_status != 410:
                raise AssertionError(f"load before refresh must return 410 archived, got {missing_load_status}")
            if missing_load_payload.get("status") != "archived":
                raise AssertionError(f"load before refresh must surface archived legacy contour, got {missing_load_payload}")
            if any(block.request_dates for block in counters.values()):
                raise AssertionError("load before refresh must not trigger heavy source blocks")

            refresh_start_status, refresh_start_payload = _post_json(
                refresh_url,
                {"as_of_date": AS_OF_DATE, "async": True},
            )
            if refresh_start_status != 202:
                raise AssertionError(f"async refresh must return 202, got {refresh_start_status}")
            if refresh_start_payload.get("operation") != "refresh":
                raise AssertionError("async refresh must expose refresh operation metadata")
            refresh_job = _wait_for_job(job_url, str(refresh_start_payload["job_id"]))
            if refresh_job["status"] != "success":
                raise AssertionError(f"async refresh job must succeed, got {refresh_job}")
            refresh_logs = "\n".join(refresh_job.get("log_lines", []))
            for expected in (
                "event=cycle_start cycle=refresh",
                "event=source_step_start source=seller_funnel_snapshot",
                "event=metric_batch_result",
                "event=cycle_finish cycle=refresh",
            ):
                if expected not in refresh_logs:
                    raise AssertionError(f"refresh live-log must contain {expected!r}")
            if refresh_job["result"]["date_columns"] != [AS_OF_DATE, TODAY_CURRENT_DATE]:
                raise AssertionError("async refresh result must keep dual-date snapshot semantics")
            refresh_manual_context = refresh_job["result"].get("manual_context") or {}
            if refresh_manual_context.get("last_successful_manual_refresh_at") != "2026-04-13T17:05:00+05:00":
                raise AssertionError("async refresh result must update the manual refresh timestamp")
            if refresh_manual_context.get("last_successful_manual_load_at") != "":
                raise AssertionError("async refresh result must keep manual load timestamp empty")
            refresh_manual_result = refresh_manual_context.get("last_manual_refresh_result") or {}
            if not isinstance(refresh_manual_context.get("last_manual_refresh_result"), dict):
                raise AssertionError("async refresh result must persist the semantic refresh result")
            if refresh_manual_result.get("technical_status") != "success":
                raise AssertionError("async refresh result must keep the technical refresh result")
            prior_load_result = refresh_manual_context.get("last_manual_load_result") or {}
            if prior_load_result:
                raise AssertionError(
                    "async refresh result must not create a fake manual load result for archived Google Sheets"
                )
            _assert_counting_calls(counters)

            before_load_requests = {key: list(block.request_dates) for key, block in counters.items()}

            load_start_status, load_start_payload = _post_json(
                load_url,
                {"as_of_date": AS_OF_DATE, "async": True},
            )
            if load_start_status != 410:
                raise AssertionError(f"async load must be denied as archived, got {load_start_status}")
            if load_start_payload.get("status") != "archived":
                raise AssertionError(f"async load must expose archived status, got {load_start_payload}")
            if load_runner.calls:
                raise AssertionError("archived async load must not call the sheet bridge")
            if {
                key: block.request_dates
                for key, block in counters.items()
            } != before_load_requests:
                raise AssertionError("archived load must not trigger heavy source blocks or implicit refresh")

            status_after_load, status_payload = _get_json(
                f"{base_url}{config.sheet_status_path}?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}"
            )
            if status_after_load != 200:
                raise AssertionError(f"status after archived load probe must return 200, got {status_after_load}")
            if status_payload["snapshot_id"] != refresh_job["result"]["snapshot_id"]:
                raise AssertionError("status after archived load probe must still point to the prepared ready snapshot")
            if status_payload.get("manual_context") != refresh_job["result"].get("manual_context"):
                raise AssertionError("archived load probe must not alter manual timestamps")

            print(f"load_archived: ok -> {missing_load_payload['status']}")
            print(f"async_refresh_logs: ok -> {len(refresh_job['log_lines'])} lines")
            print(f"async_load_archived: ok -> {load_start_status}")
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


def _wait_for_job(job_url: str, job_id: str) -> dict[str, object]:
    deadline = time.time() + 10
    while time.time() < deadline:
        status, payload = _get_json(f"{job_url}?{urllib_parse.urlencode({'job_id': job_id})}")
        if status != 200:
            raise AssertionError(f"job route must return 200, got {status}")
        if payload.get("status") != "running":
            return payload
        time.sleep(0.1)
    raise AssertionError(f"operator job did not finish in time: {job_id}")


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


def _get_text_with_headers(url: str) -> tuple[int, str, dict[str, str]]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers.items())
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers.items())


def _expected_manual_context(
    *,
    refresh_at: str = "",
    load_at: str = "",
    refresh_result: dict[str, object] | None = None,
    load_result: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "last_successful_manual_refresh_at": refresh_at,
        "last_successful_manual_load_at": load_at,
        "last_manual_refresh_result": refresh_result,
        "last_manual_load_result": load_result,
    }


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
