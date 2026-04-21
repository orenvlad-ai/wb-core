"""Integration smoke-check for the daily auto update path in sheet_vitrina_v1."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from typing import Callable
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope  # noqa: E402

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-13T12:00:03Z"
AUTO_STARTED_AT = "2026-04-13T12:06:00Z"
AUTO_FINISHED_AT = "2026-04-13T12:07:10Z"
REFRESHED_AT = "2026-04-13T12:06:40Z"
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


class FakeSheetLoadRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, plan: SheetVitrinaV1Envelope, log: Callable[[str], None]) -> dict[str, object]:
        self.calls.append(plan.snapshot_id)
        log("Тестовый auto bridge записал prepared snapshot в таблицу.")
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
        }


class SequenceTimestampFactory:
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self) -> str:
        if not self._values:
            raise ValueError("timestamp sequence must not be empty")
        if self._index >= len(self._values):
            return self._values[-1]
        value = self._values[self._index]
        self._index += 1
        return value


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    with TemporaryDirectory(prefix="sheet-vitrina-auto-update-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        counters = _build_counting_blocks()
        load_runner = FakeSheetLoadRunner()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=SequenceTimestampFactory([AUTO_STARTED_AT, AUTO_FINISHED_AT]),
            refreshed_at_factory=lambda: REFRESHED_AT,
            now_factory=lambda: SERVER_NOW,
            sheet_load_runner=load_runner,
        )
        entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            now_factory=lambda: SERVER_NOW,
            **counters,
        )

        runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)

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
            refresh_url = f"{base_url}{config.sheet_refresh_path}"
            status_url = f"{base_url}{config.sheet_status_path}"
            operator_url = f"{base_url}{config.sheet_operator_ui_path}"

            operator_status, operator_html = _get_text(operator_url)
            if operator_status != 200:
                raise AssertionError(f"operator UI must return 200, got {operator_status}")
            for expected in (
                "Автообновления",
                "Автоцепочка",
                "Последний автозапуск",
                "Статус последнего автозапуска",
                "Последнее успешное автообновление",
            ):
                if expected not in operator_html:
                    raise AssertionError(f"operator UI must expose {expected!r}")

            refresh_status, refresh_payload = _post_json(
                refresh_url,
                {"as_of_date": AS_OF_DATE, "auto_load": True},
            )
            if refresh_status != 200:
                raise AssertionError(f"auto update must return 200, got {refresh_status}")
            if refresh_payload.get("operation") != "auto_update":
                raise AssertionError("auto update must expose the combined operation")
            if refresh_payload.get("technical_status") != "success":
                raise AssertionError("auto update must keep the technical completion flag")
            if refresh_payload.get("status") != "error" or refresh_payload.get("semantic_status") != "error":
                raise AssertionError("auto update must expose the semantic failure on the combined route")
            if refresh_payload.get("auto_update_started_at") != AUTO_STARTED_AT:
                raise AssertionError("auto update must expose the start timestamp")
            if refresh_payload.get("auto_update_finished_at") != AUTO_FINISHED_AT:
                raise AssertionError("auto update must expose the finish timestamp")
            if refresh_payload.get("refreshed_at") != REFRESHED_AT:
                raise AssertionError("auto update must keep the refresh timestamp from the prepared snapshot")
            if refresh_payload.get("bridge_result", {}).get("bridge") != "fake":
                raise AssertionError("auto update must run the sheet bridge")
            if refresh_payload.get("manual_context") != _expected_manual_context():
                raise AssertionError("auto update must not pollute manual operator timestamps")
            if refresh_payload.get("semantic_status") != "error":
                raise AssertionError(f"auto update must surface semantic error when upstream sources are materially bad, got {refresh_payload}")
            if load_runner.calls != [str(refresh_payload["snapshot_id"])]:
                raise AssertionError("auto update must write the prepared snapshot exactly once")
            _assert_counting_calls(counters)

            status_code, status_payload = _get_json(status_url)
            if status_code != 200:
                raise AssertionError(f"status must return 200 after auto update, got {status_code}")
            if status_payload.get("snapshot_id") != refresh_payload.get("snapshot_id"):
                raise AssertionError("status must point to the persisted snapshot created by auto update")
            server_context = status_payload.get("server_context") or {}
            if server_context.get("last_auto_run_status") != "success":
                raise AssertionError("status must keep the technical auto-run state")
            if server_context.get("last_auto_run_status_label") != "Ошибка":
                raise AssertionError(f"status must surface the semantic auto-run label, got {server_context}")
            if server_context.get("last_auto_run_technical_status_label") != "успех":
                raise AssertionError(f"status must keep the technical auto-run label separately, got {server_context}")
            if "предыдущая отправка для сравнения отсутствует" not in str(server_context.get("last_auto_run_status_reason", "")):
                raise AssertionError(f"status must explain why auto-run is warning, got {server_context}")
            if "Ошибки по" not in str(server_context.get("last_auto_run_status_reason", "")):
                raise AssertionError(f"status must explain the upstream source failures, got {server_context}")
            if (server_context.get("last_auto_run_result") or {}).get("semantic_status") != "error":
                raise AssertionError(f"status must persist the semantic auto-run result, got {server_context}")
            if status_payload.get("manual_context") != _expected_manual_context():
                raise AssertionError("status must keep manual timestamps empty after auto update")

            print(f"auto_update: ok -> {refresh_payload['snapshot_id']}")
            print(f"auto_status: ok -> {status_payload['server_context']['last_auto_run_status_label']}")
            print(f"auto_success_at: ok -> {status_payload['server_context']['last_successful_auto_update_at']}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _expected_manual_context() -> dict[str, object]:
    return {
        "last_successful_manual_refresh_at": "",
        "last_successful_manual_load_at": "",
        "last_manual_refresh_result": None,
        "last_manual_load_result": None,
    }


def _assert_counting_calls(counters: dict[str, CountingBlock]) -> None:
    expected_by_source = {
        "seller_funnel_snapshot": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "sales_funnel_history": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "web_source_snapshot": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "prices_snapshot": [TODAY_CURRENT_DATE],
        "sf_period": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "spp": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "ads_bids": [TODAY_CURRENT_DATE],
        "stocks": [AS_OF_DATE],
        "ads_compact": [AS_OF_DATE, TODAY_CURRENT_DATE],
        "fin_report_daily": [AS_OF_DATE, TODAY_CURRENT_DATE],
    }
    for source_key, block in counters.items():
        expected_dates = expected_by_source[block.source_key]
        if block.request_dates != expected_dates:
            raise AssertionError(
                f"{source_key} request dates mismatch: expected {expected_dates}, got {block.request_dates}"
            )


def _build_counting_blocks() -> dict[str, CountingBlock]:
    return {
        "seller_funnel_block": CountingBlock("seller_funnel_snapshot"),
        "sales_funnel_history_block": CountingBlock("sales_funnel_history"),
        "web_source_block": CountingBlock("web_source_snapshot"),
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


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(request, timeout=60) as response:
        return response.getcode(), json.loads(response.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"})
    with urllib_request.urlopen(request, timeout=60) as response:
        return response.getcode(), json.loads(response.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    request = urllib_request.Request(url, headers={"Accept": "text/html"})
    with urllib_request.urlopen(request, timeout=60) as response:
        return response.getcode(), response.read().decode("utf-8")


if __name__ == "__main__":
    main()
