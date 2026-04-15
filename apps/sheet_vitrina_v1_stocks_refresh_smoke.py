"""Integration smoke-check for stocks path inside sheet_vitrina_v1 refresh."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import socket
import sys
import subprocess
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
from packages.adapters.stocks_block import HttpBackedStocksSource
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.application.stocks_block import StocksBlock
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-16T10:00:00Z"
REFRESHED_AT = "2026-04-16T10:05:00Z"
NORMAL_AS_OF_DATE = "2026-04-15"
RATE_LIMIT_AS_OF_DATE = "2026-04-14"
TODAY_CURRENT_DATE = "2026-04-16"
TOKEN_ENV = "WB_STOCKS_REFRESH_SMOKE_TOKEN"


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    _check_refresh_uses_batched_stocks_request(bundle)
    _check_refresh_surfaces_stocks_429_honestly(bundle)
    print("smoke-check passed")


def _check_refresh_uses_batched_stocks_request(bundle: dict[str, Any]) -> None:
    with _StocksApiStub(mode="success") as stocks_api, TemporaryDirectory(prefix="sheet-vitrina-stocks-ok-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        refresh_payload, plan_payload, _status_payload, _endpoint_url, load_harness_result = _run_refresh_scenario(
            bundle=bundle,
            runtime_dir=runtime_dir,
            stocks_base_url=stocks_api.base_url,
            as_of_date=NORMAL_AS_OF_DATE,
            run_load_harness=True,
        )
        if refresh_payload["status"] != "success":
            raise AssertionError("refresh must persist the ready snapshot in normal scenario")

        if refresh_payload["date_columns"] != [NORMAL_AS_OF_DATE, TODAY_CURRENT_DATE]:
            raise AssertionError("refresh_result must expose yesterday + today date columns")
        stocks_yesterday = _find_status_row(plan_payload, "stocks[yesterday_closed]")
        stocks_status = _find_status_row(plan_payload, "stocks[today_current]")
        if stocks_yesterday["kind"] != "not_available":
            raise AssertionError(f"stocks[yesterday_closed] must stay explicitly unavailable, got {stocks_yesterday}")
        if stocks_status["kind"] != "success":
            raise AssertionError(f"stocks status must be success, got {stocks_status}")
        if stocks_status["requested_count"] != 33 or stocks_status["covered_count"] != 33:
            raise AssertionError(f"stocks coverage mismatch: {stocks_status}")
        if not stocks_status["freshness"]:
            raise AssertionError("stocks status must expose non-empty freshness")

        if len(stocks_api.request_bodies) != 1:
            raise AssertionError("refresh path must use one batched stocks request for current bundle")
        request_body = stocks_api.request_bodies[0]
        if len(request_body.get("nmIds", [])) != 33:
            raise AssertionError(f"unexpected nmIds count in batched stocks request: {request_body}")
        if request_body.get("chrtIds") != []:
            raise AssertionError("refresh path must pass explicit empty chrtIds")
        if "nmID" in request_body:
            raise AssertionError("refresh path must not fall back to legacy per-nmID request body")

        stock_rows = _find_data_rows(plan_payload, "stock_total")
        if not stock_rows:
            raise AssertionError("plan must contain stock_total rows in DATA_VITRINA")
        if any(row[2] not in ("", None) for row in stock_rows):
            raise AssertionError("stocks[yesterday_closed] must stay blank instead of using current snapshot")
        if any(row[3] in ("", None) for row in stock_rows[:5]):
            raise AssertionError("normal stocks refresh must materialize current-day stock_total values")
        south_rows = _find_data_rows(plan_payload, "stock_ru_south_caucasus")
        if not any(_row_today_value(row) == 5.0 for row in south_rows):
            raise AssertionError("SKU south/caucasus stock rows must materialize live-shaped regional values")
        total_south_rows = _find_data_rows(plan_payload, "total_stock_ru_south_caucasus")
        if not any(_row_today_value(row) == 165.0 for row in total_south_rows):
            raise AssertionError("TOTAL south/caucasus stock row must aggregate all SKU regional values")
        far_rows = _find_data_rows(plan_payload, "stock_ru_far_siberia")
        if not any(_row_today_value(row) == 2.0 for row in far_rows):
            raise AssertionError("SKU far/siberia stock rows must materialize live-shaped regional values")
        total_far_rows = _find_data_rows(plan_payload, "total_stock_ru_far_siberia")
        if not any(_row_today_value(row) == 66.0 for row in total_far_rows):
            raise AssertionError("TOTAL far/siberia stock row must aggregate all SKU regional values")
        stock_total_rows = _find_data_rows(plan_payload, "stock_total")
        if not any(_row_today_value(row) == 18.0 for row in stock_total_rows):
            raise AssertionError("stock_total must keep unmapped quantity inside total for affected SKU rows")
        total_stock_rows = _find_data_rows(plan_payload, "total_stock_total")
        if not any(_row_today_value(row) == 562.0 for row in total_stock_rows):
            raise AssertionError("TOTAL stock_total must aggregate mapped + unmapped quantities honestly")
        if "Армения=1" not in str(stocks_status["note"]):
            raise AssertionError(f"stocks status must surface unmapped non-district quantity, got {stocks_status['note']!r}")

        if not load_harness_result or load_harness_result["load_result"]["http_status"] != 200:
            raise AssertionError(f"sheet load must return 200 after refresh, got {load_harness_result['load_result']}")
        loaded_rows = load_harness_result["sheets"]["DATA_VITRINA"]["values"]
        if not any(_row_today_value(row) == 5.0 for row in _find_loaded_rows(loaded_rows, "stock_ru_south_caucasus")):
            raise AssertionError("loaded DATA_VITRINA must keep south/caucasus district values")
        if not any(_row_today_value(row) == 165.0 for row in _find_loaded_rows(loaded_rows, "total_stock_ru_south_caucasus")):
            raise AssertionError("loaded DATA_VITRINA must keep TOTAL south/caucasus aggregate")
        if not any(_row_today_value(row) == 2.0 for row in _find_loaded_rows(loaded_rows, "stock_ru_far_siberia")):
            raise AssertionError("loaded DATA_VITRINA must keep far/siberia district values")
        if not any(_row_today_value(row) == 562.0 for row in _find_loaded_rows(loaded_rows, "total_stock_total")):
            raise AssertionError("loaded DATA_VITRINA must keep honest stock total including unmapped residual")
        print("refresh-batched: ok -> one stocks request serves the whole refresh path")


def _check_refresh_surfaces_stocks_429_honestly(bundle: dict[str, Any]) -> None:
    with _StocksApiStub(mode="always_429") as stocks_api, TemporaryDirectory(prefix="sheet-vitrina-stocks-429-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        refresh_payload, plan_payload, _status_payload, _endpoint_url, _load_harness_result = _run_refresh_scenario(
            bundle=bundle,
            runtime_dir=runtime_dir,
            stocks_base_url=stocks_api.base_url,
            as_of_date=RATE_LIMIT_AS_OF_DATE,
        )
        if refresh_payload["status"] != "success":
            raise AssertionError("refresh should persist the snapshot shell even when one source errors")

        if refresh_payload["date_columns"] != [RATE_LIMIT_AS_OF_DATE, TODAY_CURRENT_DATE]:
            raise AssertionError("refresh_result must expose requested yesterday + current today")
        stocks_yesterday = _find_status_row(plan_payload, "stocks[yesterday_closed]")
        stocks_status = _find_status_row(plan_payload, "stocks[today_current]")
        if stocks_yesterday["kind"] != "not_available":
            raise AssertionError("stocks[yesterday_closed] must stay explicitly unavailable")
        if stocks_status["kind"] != "error":
            raise AssertionError(f"stocks 429 must surface as source-level error, got {stocks_status}")
        if "status 429" not in str(stocks_status["note"]):
            raise AssertionError(f"stocks error note must expose 429, got {stocks_status['note']!r}")
        if stocks_status["freshness"] not in ("", None):
            raise AssertionError("errored stocks source must not fake freshness")
        if len(stocks_api.request_bodies) != 2:
            raise AssertionError("forced 429 scenario must stop after one bounded retry")

        stock_rows = _find_data_rows(plan_payload, "stock_total")
        if not stock_rows:
            raise AssertionError("plan must still contain stock_total rows for honest error scenario")
        if any(row[2] not in ("", None) or row[3] not in ("", None) for row in stock_rows):
            raise AssertionError("errored stocks source must keep both yesterday and today stock_total cells blank")
        print("refresh-429-honest: ok -> stocks error is surfaced without fake stock values")


def _run_refresh_scenario(
    *,
    bundle: dict[str, Any],
    runtime_dir: Path,
    stocks_base_url: str,
    as_of_date: str,
    run_load_harness: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, dict[str, Any] | None]:
    previous_token = os.environ.get(TOKEN_ENV)
    os.environ[TOKEN_ENV] = "stocks-refresh-smoke-token"
    try:
        entrypoint = _build_entrypoint(runtime_dir=runtime_dir, stocks_base_url=stocks_base_url)
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
            refresh_url = f"http://127.0.0.1:{config.port}{config.sheet_refresh_path}"
            plan_url = (
                f"http://127.0.0.1:{config.port}{config.sheet_plan_path}"
                f"?{urllib_parse.urlencode({'as_of_date': as_of_date})}"
            )
            status_url = (
                f"http://127.0.0.1:{config.port}{config.sheet_status_path}"
                f"?{urllib_parse.urlencode({'as_of_date': as_of_date})}"
            )

            upload_status, upload_payload = _post_json(upload_url, bundle)
            if upload_status != 200 or upload_payload["status"] != "accepted":
                raise AssertionError(f"fixture upload must be accepted, got {upload_status} {upload_payload}")

            refresh_status, refresh_payload = _post_json(refresh_url, {"as_of_date": as_of_date})
            if refresh_status != 200:
                raise AssertionError(f"refresh endpoint must return 200, got {refresh_status}")

            plan_status, plan_payload = _get_json(plan_url)
            if plan_status != 200:
                raise AssertionError(f"plan endpoint must return 200 after refresh, got {plan_status}")

            status_code, status_payload = _get_json(status_url)
            if status_code != 200:
                raise AssertionError(f"status endpoint must return 200 after refresh, got {status_code}")
            load_harness_result = (
                _run_load_only_harness(endpoint_url=upload_url, as_of_date=as_of_date)
                if run_load_harness
                else None
            )
            return refresh_payload, plan_payload, status_payload, upload_url, load_harness_result
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
    finally:
        if previous_token is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = previous_token


def _build_entrypoint(*, runtime_dir: Path, stocks_base_url: str) -> RegistryUploadHttpEntrypoint:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=runtime_dir,
        runtime=runtime,
        activated_at_factory=lambda: ACTIVATED_AT,
        refreshed_at_factory=lambda: REFRESHED_AT,
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
        stocks_block=StocksBlock(
            HttpBackedStocksSource(
                base_url=stocks_base_url,
                token_env_var=TOKEN_ENV,
                base_url_env_var="",
                timeout_seconds=5.0,
                min_request_interval_seconds=0.0,
                max_retries_on_429=1,
                reuse_ttl_seconds=0.0,
            )
        ),
        ads_compact_block=_SyntheticSuccessBlock("ads_compact"),
        fin_report_daily_block=_SyntheticSuccessBlock("fin_report_daily"),
        now_factory=lambda: datetime(2026, 4, 16, 9, 0, tzinfo=timezone.utc),
    )
    return entrypoint


class _SyntheticSuccessBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key

    def execute(self, request: object) -> SimpleNamespace:
        as_of_date = _request_date(request)
        payload = SimpleNamespace(
            kind="success",
            items=[],
            snapshot_date=as_of_date,
            date=as_of_date,
            date_from=as_of_date,
            date_to=as_of_date,
            detail=f"{self.source_key} synthetic success for {as_of_date}",
            storage_total=None,
        )
        return SimpleNamespace(result=payload)


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    return NORMAL_AS_OF_DATE


class _StocksApiStub:
    def __init__(self, *, mode: str) -> None:
        self.mode = mode
        self.request_bodies: list[dict[str, Any]] = []
        self._server = HTTPServer(("127.0.0.1", _reserve_free_port()), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "_StocksApiStub":
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/api/analytics/v1/stocks-report/wb-warehouses":
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return

                raw_length = int(self.headers.get("Content-Length", "0") or "0")
                payload = json.loads(self.rfile.read(raw_length).decode("utf-8"))
                stub.request_bodies.append(payload)
                response = stub._response_for(payload)
                body = response["body"].encode("utf-8")
                self.send_response(response["status"])
                for header, value in response["headers"].items():
                    self.send_header(header, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

        return Handler

    def _response_for(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.mode == "success":
            nm_ids = [int(nm_id) for nm_id in payload.get("nmIds", [])]
            items = []
            for nm_id in nm_ids:
                items.extend(
                    [
                        {
                            "nmId": nm_id,
                            "regionName": "Центральный",
                            "quantity": 10,
                        },
                        {
                            "nmId": nm_id,
                            "regionName": "Южный и Северо-Кавказский",
                            "quantity": 5,
                        },
                        {
                            "nmId": nm_id,
                            "regionName": "Дальневосточный и Сибирский",
                            "quantity": 2,
                        },
                    ]
                )
            if nm_ids:
                items.append(
                    {
                        "nmId": nm_ids[0],
                        "regionName": "Армения",
                        "quantity": 1,
                    }
                )
            return _json_response(200, {"data": {"items": items}})
        if self.mode == "always_429":
            return _json_response(
                429,
                {"title": "too many requests", "status": 429},
                headers={"X-Ratelimit-Retry": "0", "X-Ratelimit-Reset": "0"},
            )
        raise ValueError(f"unsupported mode: {self.mode}")


def _json_response(
    status: int,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "body": json.dumps(body, ensure_ascii=False),
    }


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
    return [row for row in data_sheet["rows"] if len(row) >= 3 and str(row[1]).endswith(f"|{metric_key}")]


def _find_loaded_rows(rows: list[list[Any]], metric_key: str) -> list[list[Any]]:
    return [row for row in rows if len(row) >= 4 and str(row[1]) == metric_key]


def _row_today_value(row: list[Any]) -> float | None:
    if len(row) < 4 or row[3] in ("", None):
        return None
    return float(row[3])


def _find_sheet(plan_payload: dict[str, Any], sheet_name: str) -> dict[str, Any]:
    for sheet in plan_payload["sheets"]:
        if sheet["sheet_name"] == sheet_name:
            return sheet
    raise AssertionError(f"sheet {sheet_name!r} is missing")


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
