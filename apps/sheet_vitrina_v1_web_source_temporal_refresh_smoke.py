"""Integration smoke-check for two-slot web-source refresh/read materialization."""

from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
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
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.adapters.seller_funnel_snapshot_block import HttpBackedSellerFunnelSnapshotSource
from packages.adapters.web_source_snapshot_block import HttpBackedWebSourceSnapshotSource
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.seller_funnel_snapshot_block import SellerFunnelSnapshotBlock
from packages.application.sheet_vitrina_v1_live_plan import (
    EXECUTION_MODE_AUTO_DAILY,
    SheetVitrinaV1LivePlanBlock,
)
from packages.application.web_source_snapshot_block import WebSourceSnapshotBlock
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-13T12:00:03Z"
REFRESHED_AT = "2026-04-13T12:05:00Z"
FIRST_AS_OF_DATE = "2026-04-12"
SECOND_AS_OF_DATE = "2026-04-13"
FIRST_CURRENT_DATE = "2026-04-13"
SECOND_CURRENT_DATE = "2026-04-14"


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]]
    probe_nm_id = requested_nm_ids[0]

    with TemporaryDirectory(prefix="sheet-vitrina-web-source-temporal-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            refreshed_at_factory=lambda: REFRESHED_AT,
        )

        with _MockSellerosServer(requested_nm_ids) as upstream:
            closure_sync = _FakeClosedDayWebSourceSync(upstream)
            entrypoint.sheet_plan_block = _build_live_plan(
                runtime,
                upstream.base_url,
                FIRST_CURRENT_DATE,
                closure_sync=closure_sync,
                current_hour=8,
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
                refresh_url = f"http://127.0.0.1:{config.port}{config.sheet_refresh_path}"
                status_url = f"http://127.0.0.1:{config.port}{config.sheet_status_path}"
                plan_url = f"http://127.0.0.1:{config.port}{config.sheet_plan_path}"

                upload_status, upload_payload = _post_json(upload_url, bundle)
                if upload_status != 200 or upload_payload["status"] != "accepted":
                    raise AssertionError(f"fixture upload must be accepted, got {upload_status} {upload_payload}")

                first_refresh_payload = entrypoint._run_sheet_refresh(
                    as_of_date=FIRST_AS_OF_DATE,
                    log=None,
                    execution_mode=EXECUTION_MODE_AUTO_DAILY,
                )
                if first_refresh_payload["status"] != "success":
                    raise AssertionError("first refresh must succeed")
                first_status_rows = _fetch_status_rows(status_url, FIRST_AS_OF_DATE)
                for source_key in ("web_source_snapshot", "seller_funnel_snapshot"):
                    if first_status_rows[f"{source_key}[yesterday_closed]"][1] != "closure_retrying":
                        raise AssertionError(f"{source_key} first refresh yesterday slot must enter closure retry state")
                    note = str(first_status_rows[f"{source_key}[yesterday_closed]"][10])
                    if "closure_state=closure_retrying" not in note:
                        raise AssertionError(f"{source_key} first refresh must disclose closure retry state in STATUS note")
                    if first_status_rows[f"{source_key}[today_current]"][1] != "success":
                        raise AssertionError(f"{source_key} first refresh today slot must materialize current latest")

                upstream.set_latest_date(SECOND_CURRENT_DATE)
                entrypoint.sheet_plan_block = _build_live_plan(
                    runtime,
                    upstream.base_url,
                    SECOND_CURRENT_DATE,
                    closure_sync=closure_sync,
                    current_hour=8,
                )
                second_refresh_payload = entrypoint._run_sheet_refresh(
                    as_of_date=SECOND_AS_OF_DATE,
                    log=None,
                    execution_mode=EXECUTION_MODE_AUTO_DAILY,
                )
                if second_refresh_payload["status"] != "success":
                    raise AssertionError("second refresh must succeed")
                if second_refresh_payload["date_columns"] != [SECOND_AS_OF_DATE, SECOND_CURRENT_DATE]:
                    raise AssertionError("second refresh must materialize requested yesterday + current today")

                second_status_rows = _fetch_status_rows(status_url, SECOND_AS_OF_DATE)
                for source_key in ("web_source_snapshot", "seller_funnel_snapshot"):
                    if second_status_rows[f"{source_key}[yesterday_closed]"][1] != "closure_retrying":
                        raise AssertionError(f"{source_key} yesterday slot must wait for accepted closed-day snapshot")
                    if second_status_rows[f"{source_key}[today_current]"][1] != "success":
                        raise AssertionError(f"{source_key} today slot must materialize as success")
                    retry_note = str(second_status_rows[f"{source_key}[yesterday_closed]"][10])
                    if "closure_state=closure_retrying" not in retry_note:
                        raise AssertionError(f"{source_key} yesterday slot must disclose closure retry state")

                plan_status, plan_payload = _get_json(
                    f"{plan_url}?{urllib_parse.urlencode({'as_of_date': SECOND_AS_OF_DATE})}"
                )
                if plan_status != 200:
                    raise AssertionError("plan endpoint must return persisted ready snapshot after second refresh")
                data_sheet = next(sheet for sheet in plan_payload["sheets"] if sheet["sheet_name"] == "DATA_VITRINA")
                data_rows = {row[1]: row for row in data_sheet["rows"]}
                if data_rows[f"SKU:{probe_nm_id}|views_current"][2:] != ["", float(_web_views_value(SECOND_CURRENT_DATE, 0))]:
                    raise AssertionError("web_source closed slot must stay blank while closure is retrying")
                if data_rows[f"SKU:{probe_nm_id}|view_count"][2:] != ["", float(_seller_view_count(SECOND_CURRENT_DATE, 0))]:
                    raise AssertionError("seller_funnel closed slot must stay blank while closure is retrying")

                closure_sync.enable_acceptance(FIRST_CURRENT_DATE)
                entrypoint.sheet_plan_block = _build_live_plan(
                    runtime,
                    upstream.base_url,
                    SECOND_CURRENT_DATE,
                    closure_sync=closure_sync,
                    current_hour=10,
                )
                third_refresh_payload = entrypoint._run_sheet_refresh(
                    as_of_date=SECOND_AS_OF_DATE,
                    log=None,
                    execution_mode=EXECUTION_MODE_AUTO_DAILY,
                )
                if third_refresh_payload["status"] != "success":
                    raise AssertionError("third refresh must succeed")

                third_status_rows = _fetch_status_rows(status_url, SECOND_AS_OF_DATE)
                for source_key in ("web_source_snapshot", "seller_funnel_snapshot"):
                    if third_status_rows[f"{source_key}[yesterday_closed]"][1] != "success":
                        raise AssertionError(f"{source_key} accepted closed-day slot must materialize as success")
                    accepted_note = str(third_status_rows[f"{source_key}[yesterday_closed]"][10])
                    if "accepted_closed" not in accepted_note:
                        raise AssertionError(f"{source_key} yesterday slot must disclose accepted closed-day resolution")

                plan_status, plan_payload = _get_json(
                    f"{plan_url}?{urllib_parse.urlencode({'as_of_date': SECOND_AS_OF_DATE})}"
                )
                if plan_status != 200:
                    raise AssertionError("plan endpoint must return persisted ready snapshot after third refresh")
                data_sheet = next(sheet for sheet in plan_payload["sheets"] if sheet["sheet_name"] == "DATA_VITRINA")
                data_rows = {row[1]: row for row in data_sheet["rows"]}
                expected_first_day_web = _web_views_value(FIRST_CURRENT_DATE, 0)
                expected_second_day_web = _web_views_value(SECOND_CURRENT_DATE, 0)
                expected_first_day_seller = _seller_view_count(FIRST_CURRENT_DATE, 0)
                expected_second_day_seller = _seller_view_count(SECOND_CURRENT_DATE, 0)
                if data_rows[f"SKU:{probe_nm_id}|views_current"][2:] != [
                    float(expected_first_day_web),
                    float(expected_second_day_web),
                ]:
                    raise AssertionError("web_source metric row must expose accepted yesterday + current today values")
                if data_rows[f"SKU:{probe_nm_id}|view_count"][2:] != [
                    float(expected_first_day_seller),
                    float(expected_second_day_seller),
                ]:
                    raise AssertionError("seller_funnel metric row must expose accepted yesterday + current today values")

                ready_load = _run_load_harness(upload_url, as_of_date=SECOND_AS_OF_DATE)
                if ready_load["load_error"]:
                    raise AssertionError(f"sheet load must succeed after second refresh, got {ready_load['load_error']!r}")
                if ready_load["load_result"]["http_status"] != 200:
                    raise AssertionError("sheet load must read the persisted ready snapshot")
                if ready_load["sheets"]["DATA_VITRINA"]["values"][0] != ["дата", "key", SECOND_AS_OF_DATE, SECOND_CURRENT_DATE]:
                    raise AssertionError("sheet load must keep the two-slot date header")

                print(f"first_refresh: ok -> {first_refresh_payload['snapshot_id']}")
                print(f"second_refresh_retrying: ok -> {second_refresh_payload['snapshot_id']}")
                print(f"third_refresh_accepted: ok -> {third_refresh_payload['snapshot_id']}")
                print(
                    "status_retry_surface: ok -> "
                    f"{second_status_rows['web_source_snapshot[yesterday_closed]'][10]}"
                )
                print(
                    "status_accepted_surface: ok -> "
                    f"{third_status_rows['web_source_snapshot[yesterday_closed]'][10]}"
                )
                print("sheet_load: ok -> ready snapshot carries accepted yesterday + current today")
                print("smoke-check passed")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


def _build_live_plan(
    runtime: RegistryUploadDbBackedRuntime,
    upstream_base_url: str,
    current_date: str,
    *,
    closure_sync: object,
    current_hour: int,
) -> SheetVitrinaV1LivePlanBlock:
    return SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        web_source_block=WebSourceSnapshotBlock(HttpBackedWebSourceSnapshotSource(base_url=upstream_base_url)),
        seller_funnel_block=SellerFunnelSnapshotBlock(
            HttpBackedSellerFunnelSnapshotSource(base_url=upstream_base_url)
        ),
        closed_day_web_source_sync=closure_sync,
        now_factory=lambda current_date=current_date, current_hour=current_hour: datetime.fromisoformat(
            f"{current_date}T{current_hour:02d}:00:00+00:00"
        ),
        **_build_synthetic_blocks(),
    )


def _build_synthetic_blocks() -> dict[str, object]:
    return {
        "sales_funnel_history_block": _SyntheticSuccessBlock("sales_funnel_history"),
        "prices_snapshot_block": _SyntheticSuccessBlock("prices_snapshot"),
        "sf_period_block": _SyntheticSuccessBlock("sf_period"),
        "spp_block": _SyntheticSuccessBlock("spp"),
        "ads_bids_block": _SyntheticSuccessBlock("ads_bids"),
        "stocks_block": _SyntheticSuccessBlock("stocks"),
        "ads_compact_block": _SyntheticSuccessBlock("ads_compact"),
        "fin_report_daily_block": _SyntheticSuccessBlock("fin_report_daily"),
    }


class _SyntheticSuccessBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                items=[],
                snapshot_date=request_date,
                date=request_date,
                date_from=request_date,
                date_to=request_date,
                detail=f"{self.source_key} synthetic success for {request_date}",
                storage_total=None,
            )
        )


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    raise AssertionError("synthetic request must carry a date field")


def _fetch_status_rows(status_url: str, as_of_date: str) -> dict[str, list[object]]:
    plan_status, plan_payload = _get_json(f"{status_url}?{urllib_parse.urlencode({'as_of_date': as_of_date})}")
    if plan_status != 200:
        raise AssertionError(f"status endpoint must return 200, got {plan_status}")
    plan = _get_plan(status_url.replace(DEFAULT_SHEET_STATUS_PATH, DEFAULT_SHEET_PLAN_PATH), as_of_date)
    status_sheet = next(sheet for sheet in plan["sheets"] if sheet["sheet_name"] == "STATUS")
    return {row[0]: row for row in status_sheet["rows"]}


def _get_plan(plan_url: str, as_of_date: str) -> dict[str, object]:
    plan_status, plan_payload = _get_json(f"{plan_url}?{urllib_parse.urlencode({'as_of_date': as_of_date})}")
    if plan_status != 200:
        raise AssertionError(f"plan endpoint must return 200, got {plan_status}")
    return plan_payload


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


def _post_json(url: str, payload: object) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class _MockSellerosServer:
    def __init__(self, requested_nm_ids: list[int]) -> None:
        self.requested_nm_ids = requested_nm_ids
        self.latest_date = FIRST_CURRENT_DATE
        self.accepted_closed_dates: set[str] = set()
        self._server = HTTPServer(("127.0.0.1", _reserve_free_port()), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def set_latest_date(self, snapshot_date: str) -> None:
        self.latest_date = snapshot_date

    def accept_closed_date(self, snapshot_date: str) -> None:
        self.accepted_closed_dates.add(snapshot_date)

    def __enter__(self) -> "_MockSellerosServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib_parse.urlparse(self.path)
                if parsed.path == "/v1/search-analytics/snapshot":
                    self._write_web(parsed.query)
                    return
                if parsed.path == "/v1/sales-funnel/daily":
                    self._write_seller(parsed.query)
                    return
                self.send_error(404)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

            def _write_web(self, query: str) -> None:
                parsed = urllib_parse.parse_qs(query)
                if query:
                    requested = (parsed.get("date_to") or [""])[0]
                    if requested and requested in parent.accepted_closed_dates:
                        self._write_json(200, _build_web_payload(requested, parent.requested_nm_ids))
                        return
                    self._write_json(404, {"detail": "explicit not found"})
                    return
                self._write_json(200, _build_web_payload(parent.latest_date, parent.requested_nm_ids))

            def _write_seller(self, query: str) -> None:
                parsed = urllib_parse.parse_qs(query)
                if query:
                    requested = (parsed.get("date") or [""])[0]
                    if requested and requested in parent.accepted_closed_dates:
                        self._write_json(200, _build_seller_payload(requested, parent.requested_nm_ids))
                        return
                    self._write_json(404, {"detail": "explicit not found"})
                    return
                self._write_json(200, _build_seller_payload(parent.latest_date, parent.requested_nm_ids))

            def _write_json(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def _build_web_payload(snapshot_date: str, requested_nm_ids: list[int]) -> dict[str, object]:
    return {
        "date_from": snapshot_date,
        "date_to": snapshot_date,
        "count": len(requested_nm_ids),
        "items": [
            {
                "nm_id": nm_id,
                "views_current": _web_views_value(snapshot_date, index),
                "ctr_current": 20 + index,
                "orders_current": 5 + index,
                "position_avg": 10 + index,
            }
            for index, nm_id in enumerate(requested_nm_ids)
        ],
    }


def _build_seller_payload(snapshot_date: str, requested_nm_ids: list[int]) -> dict[str, object]:
    return {
        "date": snapshot_date,
        "count": len(requested_nm_ids),
        "items": [
            {
                "nm_id": nm_id,
                "name": f"NM {nm_id}",
                "vendor_code": f"VC-{nm_id}",
                "view_count": _seller_view_count(snapshot_date, index),
                "open_card_count": 30 + index,
                "ctr": 40 + index,
            }
            for index, nm_id in enumerate(requested_nm_ids)
        ],
    }


def _web_views_value(snapshot_date: str, index: int) -> int:
    return (100 if snapshot_date == FIRST_CURRENT_DATE else 200) + index


def _seller_view_count(snapshot_date: str, index: int) -> int:
    return (300 if snapshot_date == FIRST_CURRENT_DATE else 400) + index


class _FakeClosedDayWebSourceSync:
    def __init__(self, upstream: _MockSellerosServer) -> None:
        self.upstream = upstream
        self.allowed_dates: set[str] = set()

    def enable_acceptance(self, snapshot_date: str) -> None:
        self.allowed_dates.add(snapshot_date)

    def ensure_closed_day_snapshot(self, *, source_key: str, snapshot_date: str) -> None:
        if snapshot_date in self.allowed_dates:
            self.upstream.accept_closed_date(snapshot_date)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
