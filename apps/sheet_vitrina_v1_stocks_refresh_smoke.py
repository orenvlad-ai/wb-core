"""Integration smoke-check for historical stocks path inside sheet_vitrina_v1 refresh."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from typing import Any
from urllib import error, parse as urllib_parse, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
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
from packages.application.stocks_block import StocksBlock, transform_legacy_payload
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-16T21:00:00Z"
REFRESHED_AT = "2026-04-16T21:05:00Z"
AS_OF_DATE = "2026-04-15"
TODAY_CURRENT_DATE = "2026-04-17"


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    with TemporaryDirectory(prefix="sheet-vitrina-stocks-history-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _ingest_bundle(runtime=runtime, bundle=bundle)
        current_state = runtime.load_current_state()
        requested_nm_ids = [int(item.nm_id) for item in current_state.config_v2 if item.enabled]
        runtime.save_temporal_source_snapshot(
            source_key="stocks",
            snapshot_date=AS_OF_DATE,
            captured_at=REFRESHED_AT,
            payload=transform_legacy_payload(_historical_stocks_payload(requested_nm_ids)).result,
        )

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            refreshed_at_factory=lambda: REFRESHED_AT,
            now_factory=lambda: datetime(2026, 4, 16, 21, 30, tzinfo=timezone.utc),
        )
        entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            web_source_block=_SyntheticSuccessBlock("web_source_snapshot"),
            seller_funnel_block=_SyntheticSuccessBlock("seller_funnel_snapshot"),
            sales_funnel_history_block=_SyntheticSuccessBlock("sales_funnel_history"),
            prices_snapshot_block=_SyntheticSuccessBlock("prices_snapshot"),
            sf_period_block=_SyntheticSuccessBlock("sf_period"),
            spp_block=_SyntheticSuccessBlock("spp"),
            ads_bids_block=_SyntheticSuccessBlock("ads_bids"),
            stocks_block=StocksBlock(_HybridStocksSource(requested_nm_ids=requested_nm_ids)),
            ads_compact_block=_SyntheticSuccessBlock("ads_compact"),
            fin_report_daily_block=_SyntheticSuccessBlock("fin_report_daily"),
            now_factory=lambda: datetime(2026, 4, 16, 21, 30, tzinfo=timezone.utc),
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
            refresh_url = f"http://127.0.0.1:{config.port}{config.sheet_refresh_path}"
            plan_url = (
                f"http://127.0.0.1:{config.port}{config.sheet_plan_path}"
                f"?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}"
            )
            status_url = (
                f"http://127.0.0.1:{config.port}{config.sheet_status_path}"
                f"?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}"
            )
            upload_url = f"http://127.0.0.1:{config.port}{config.upload_path}"

            refresh_status, refresh_payload = _post_json(refresh_url, {"as_of_date": AS_OF_DATE})
            if refresh_status != 200 or refresh_payload["status"] != "success":
                raise AssertionError(f"refresh must succeed, got {refresh_status} {refresh_payload}")
            if refresh_payload["date_columns"] != [AS_OF_DATE, TODAY_CURRENT_DATE]:
                raise AssertionError("refresh must keep yesterday + today columns")

            plan_status, plan_payload = _get_json(plan_url)
            if plan_status != 200:
                raise AssertionError(f"plan must return 200 after refresh, got {plan_status}")
            status_code, status_payload = _get_json(status_url)
            if status_code != 200:
                raise AssertionError(f"status must return 200 after refresh, got {status_code}")

            stocks_yesterday = _find_status_row(plan_payload, "stocks[yesterday_closed]")
            stocks_today = _find_status_row(plan_payload, "stocks[today_current]")
            if stocks_yesterday["kind"] != "success":
                raise AssertionError(f"stocks[yesterday_closed] must be success, got {stocks_yesterday}")
            if stocks_yesterday["freshness"] != AS_OF_DATE:
                raise AssertionError(f"stocks[yesterday_closed] freshness mismatch: {stocks_yesterday}")
            if "resolution_rule=exact_date_stocks_history_runtime_cache" not in str(stocks_yesterday["note"]):
                raise AssertionError(f"stocks cache note missing: {stocks_yesterday}")
            if stocks_today["kind"] != "not_available":
                raise AssertionError(f"stocks[today_current] must stay truthfully non-required, got {stocks_today}")
            stocks_outcome = next(
                item for item in status_payload["source_outcomes"] if item["source_key"] == "stocks"
            )
            if stocks_outcome["status"] != "success":
                raise AssertionError(f"stocks source outcome must stay green, got {stocks_outcome}")
            if "текущий день для этого источника не требуется" not in str(stocks_outcome["reason"]):
                raise AssertionError(f"stocks source outcome must explain yesterday-only policy, got {stocks_outcome}")

            stock_rows = _find_data_rows(plan_payload, "stock_total")
            if not stock_rows:
                raise AssertionError("plan must contain stock_total rows")
            if not any(_row_yesterday_value(row) == 18.0 for row in stock_rows):
                raise AssertionError("historical stocks must materialize SKU yesterday values")
            if not all(_row_today_value(row) in {"", None} for row in stock_rows):
                raise AssertionError("today_current stock values must stay blank for yesterday-only stocks policy")
            total_stock_rows = _find_data_rows(plan_payload, "total_stock_total")
            if not any(_row_yesterday_value(row) == 50.0 for row in total_stock_rows):
                raise AssertionError("TOTAL stock_total must aggregate historical closed-day values")
            if not all(_row_today_value(row) in {"", None} for row in total_stock_rows):
                raise AssertionError("TOTAL stock_total must stay blank for today_current under yesterday-only policy")
            if not any(_row_yesterday_value(row) == 5.0 for row in _find_data_rows(plan_payload, "stock_ru_south_caucasus")):
                raise AssertionError("south/caucasus district must materialize from cached historical stocks")
            if not any(_row_yesterday_value(row) == 2.0 for row in _find_data_rows(plan_payload, "stock_ru_far_siberia")):
                raise AssertionError("far/siberia district must materialize from cached historical stocks")

            load_harness_result = _run_load_only_harness(endpoint_url=upload_url, as_of_date=AS_OF_DATE)
            if load_harness_result["load_error"]:
                raise AssertionError(f"sheet-side load must succeed, got {load_harness_result['load_error']!r}")
            if load_harness_result["load_result"]["http_status"] != 200:
                raise AssertionError("sheet-side load must receive 200")
            loaded_rows = load_harness_result["sheets"]["DATA_VITRINA"]["values"]
            if not any(_row_yesterday_value(row) == 50.0 for row in _find_loaded_rows(loaded_rows, "total_stock_total")):
                raise AssertionError("loaded DATA_VITRINA must keep historical total_stock_total")
            if not all(
                _row_today_value(row) in {"", None} for row in _find_loaded_rows(loaded_rows, "stock_total")
            ):
                raise AssertionError("loaded DATA_VITRINA must keep today_current stocks blank under yesterday-only policy")
            if status_payload["snapshot_id"] != refresh_payload["snapshot_id"]:
                raise AssertionError("status snapshot_id must match refreshed snapshot")

            print(f"stocks_yesterday_closed: ok -> {stocks_yesterday['freshness']}")
            print(f"stocks_today_current: ok -> {stocks_today['kind']}")
            print(f"total_stock_total: ok -> 50.0")
            print("smoke-check passed")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


class _SyntheticSuccessBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
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


class _HybridStocksSource:
    def __init__(self, *, requested_nm_ids: list[int]) -> None:
        self.requested_nm_ids = requested_nm_ids

    def fetch(self, request: object) -> dict[str, Any]:
        snapshot_date = str(getattr(request, "snapshot_date"))
        raise AssertionError(f"stocks current-day loader must not be called under yesterday-only policy, got {snapshot_date}")


def _historical_stocks_payload(requested_nm_ids: list[int]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = [
        _row(AS_OF_DATE, requested_nm_ids[0], "Центральный", 10),
        _row(AS_OF_DATE, requested_nm_ids[0], "Южный и Северо-Кавказский", 5),
        _row(AS_OF_DATE, requested_nm_ids[0], "Дальневосточный и Сибирский", 2),
        _row(AS_OF_DATE, requested_nm_ids[0], "Армения", 1),
    ]
    for nm_id in requested_nm_ids[1:]:
        rows.append(_row(AS_OF_DATE, nm_id, "Центральный", 1))
    return {
        "snapshot_date": AS_OF_DATE,
        "requested_nm_ids": requested_nm_ids,
        "source": {
            "report_type": "STOCK_HISTORY_DAILY_CSV",
            "download_id": "smoke-download-id",
        },
        "data": {
            "rows": rows,
            "requested_snapshot_date": AS_OF_DATE,
        },
    }
def _row(snapshot_date: str, nm_id: int, region_name: str, stock_count: float) -> dict[str, Any]:
    return {
        "snapshot_date": snapshot_date,
        "snapshot_ts": f"{snapshot_date} 21:30:00",
        "nmId": nm_id,
        "regionName": region_name,
        "stockCount": float(stock_count),
    }


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    return AS_OF_DATE


def _ingest_bundle(*, runtime: RegistryUploadDbBackedRuntime, bundle: dict[str, Any]) -> None:
    result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
    if result.status != "accepted":
        raise AssertionError(f"fixture ingest must be accepted, got {asdict(result)}")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _post_json(url: str, payload: object) -> tuple[int, dict[str, Any]]:
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


def _get_json(url: str) -> tuple[int, dict[str, Any]]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _run_load_only_harness(*, endpoint_url: str, as_of_date: str) -> dict[str, Any]:
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


def _find_status_row(plan_payload: dict[str, Any], source_key: str) -> dict[str, Any]:
    status_sheet = _find_sheet(plan_payload, "STATUS")
    for row in status_sheet["rows"]:
        if row and row[0] == source_key:
            return dict(zip(status_sheet["header"], row))
    raise AssertionError(f"STATUS row for {source_key!r} is missing")


def _find_data_rows(plan_payload: dict[str, Any], metric_key: str) -> list[list[Any]]:
    data_sheet = _find_sheet(plan_payload, "DATA_VITRINA")
    return [
        row
        for row in data_sheet["rows"]
        if len(row) >= 4 and (row[1] == metric_key or str(row[1]).endswith(f"|{metric_key}"))
    ]


def _find_loaded_rows(rows: list[list[Any]], metric_key: str) -> list[list[Any]]:
    return [
        row
        for row in rows
        if len(row) >= 4 and (row[1] == metric_key or str(row[1]).endswith(f"|{metric_key}"))
    ]


def _find_sheet(plan_payload: dict[str, Any], sheet_name: str) -> dict[str, Any]:
    for sheet in plan_payload["sheets"]:
        if sheet["sheet_name"] == sheet_name:
            return sheet
    raise AssertionError(f"{sheet_name} sheet is missing from payload")


def _row_yesterday_value(row: list[Any]) -> Any:
    return row[2] if len(row) > 2 else None


def _row_today_value(row: list[Any]) -> Any:
    return row[3] if len(row) > 3 else None


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    main()
