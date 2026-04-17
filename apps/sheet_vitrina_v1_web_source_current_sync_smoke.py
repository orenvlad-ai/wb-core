"""Targeted smoke-check for on-demand current-day web-source sync during refresh."""

from __future__ import annotations

from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket
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
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.application.web_source_snapshot_block import WebSourceSnapshotBlock
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
AS_OF_DATE = "2026-04-16"
CURRENT_DATE = "2026-04-17"
ACTIVATED_AT = "2026-04-17T15:00:00Z"
REFRESHED_AT = "2026-04-17T15:05:00Z"


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]]
    probe_nm_id = requested_nm_ids[0]

    with TemporaryDirectory(prefix="sheet-vitrina-web-source-current-sync-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            refreshed_at_factory=lambda: REFRESHED_AT,
        )

        with _MockSellerosServer(requested_nm_ids, initial_latest_date=AS_OF_DATE) as upstream:
            entrypoint.sheet_plan_block = _build_live_plan(runtime, upstream, CURRENT_DATE)
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
                plan_url = f"http://127.0.0.1:{config.port}{config.sheet_plan_path}"

                upload_status, upload_payload = _post_json(upload_url, bundle)
                if upload_status != 200 or upload_payload["status"] != "accepted":
                    raise AssertionError(f"fixture upload must be accepted, got {upload_status} {upload_payload}")

                refresh_status, refresh_payload = _post_json(refresh_url, {"as_of_date": AS_OF_DATE})
                if refresh_status != 200 or refresh_payload["status"] != "success":
                    raise AssertionError(f"refresh must succeed, got {refresh_status} {refresh_payload}")

                plan_status, plan_payload = _get_json(
                    f"{plan_url}?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}"
                )
                if plan_status != 200:
                    raise AssertionError(f"plan endpoint must return 200, got {plan_status}")

                status_sheet = next(sheet for sheet in plan_payload["sheets"] if sheet["sheet_name"] == "STATUS")
                status_rows = {row[0]: row for row in status_sheet["rows"]}
                for source_key in ("seller_funnel_snapshot", "web_source_snapshot"):
                    if status_rows[f"{source_key}[yesterday_closed]"][1] != "success":
                        raise AssertionError(f"{source_key} yesterday slot must stay success")
                    if status_rows[f"{source_key}[today_current]"][1] != "success":
                        raise AssertionError(f"{source_key} today slot must be materialized after current sync")

                data_sheet = next(sheet for sheet in plan_payload["sheets"] if sheet["sheet_name"] == "DATA_VITRINA")
                data_rows = {row[1]: row for row in data_sheet["rows"]}
                if data_rows[f"SKU:{probe_nm_id}|views_current"][2:] != [100.0, 200.0]:
                    raise AssertionError("web_source current sync must fill yesterday + today views_current")
                if data_rows[f"SKU:{probe_nm_id}|view_count"][2:] != [300.0, 400.0]:
                    raise AssertionError("seller_funnel current sync must fill yesterday + today view_count")

                print(f"refresh: ok -> {refresh_payload['snapshot_id']}")
                print("status: ok -> current-day web-source rows materialized via on-demand sync")
                print("smoke-check passed")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


def _build_live_plan(
    runtime: RegistryUploadDbBackedRuntime,
    upstream: "_MockSellerosServer",
    current_date: str,
) -> SheetVitrinaV1LivePlanBlock:
    return SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        web_source_block=WebSourceSnapshotBlock(HttpBackedWebSourceSnapshotSource(base_url=upstream.base_url)),
        seller_funnel_block=SellerFunnelSnapshotBlock(
            HttpBackedSellerFunnelSnapshotSource(base_url=upstream.base_url)
        ),
        current_web_source_sync=_SyntheticCurrentWebSourceSync(upstream),
        now_factory=lambda current_date=current_date: datetime.fromisoformat(f"{current_date}T08:00:00+00:00"),
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


class _SyntheticCurrentWebSourceSync:
    def __init__(self, upstream: "_MockSellerosServer") -> None:
        self.upstream = upstream

    def ensure_snapshot(self, snapshot_date: str) -> None:
        self.upstream.add_snapshot(snapshot_date)


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
    def __init__(self, requested_nm_ids: list[int], *, initial_latest_date: str) -> None:
        self.requested_nm_ids = requested_nm_ids
        self.available_dates = {initial_latest_date}
        self.latest_date = initial_latest_date
        self._server = HTTPServer(("127.0.0.1", _reserve_free_port()), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def add_snapshot(self, snapshot_date: str) -> None:
        self.available_dates.add(snapshot_date)
        self.latest_date = max(self.available_dates)

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
                query = urllib_parse.parse_qs(parsed.query)
                if parsed.path == "/v1/search-analytics/snapshot":
                    self._write_web(query)
                    return
                if parsed.path == "/v1/sales-funnel/daily":
                    self._write_seller(query)
                    return
                self.send_error(404)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

            def _write_web(self, query: dict[str, list[str]]) -> None:
                requested_date = _single_value(query, "date_to")
                if requested_date:
                    if requested_date not in parent.available_dates:
                        self._write_json(404, {"detail": "explicit not found"})
                        return
                    self._write_json(200, _build_web_payload(requested_date, parent.requested_nm_ids))
                    return
                self._write_json(200, _build_web_payload(parent.latest_date, parent.requested_nm_ids))

            def _write_seller(self, query: dict[str, list[str]]) -> None:
                requested_date = _single_value(query, "date")
                if requested_date:
                    if requested_date not in parent.available_dates:
                        self._write_json(404, {"detail": "explicit not found"})
                        return
                    self._write_json(200, _build_seller_payload(requested_date, parent.requested_nm_ids))
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


def _single_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    if not values:
        return None
    value = str(values[0] or "").strip()
    return value or None


def _build_web_payload(snapshot_date: str, requested_nm_ids: list[int]) -> dict[str, object]:
    base = 100 if snapshot_date == AS_OF_DATE else 200
    return {
        "date_from": snapshot_date,
        "date_to": snapshot_date,
        "count": len(requested_nm_ids),
        "items": [
            {
                "nm_id": nm_id,
                "views_current": base + index,
                "ctr_current": 20 + index,
                "orders_current": 5 + index,
                "position_avg": 10 + index,
            }
            for index, nm_id in enumerate(requested_nm_ids)
        ],
    }


def _build_seller_payload(snapshot_date: str, requested_nm_ids: list[int]) -> dict[str, object]:
    base = 300 if snapshot_date == AS_OF_DATE else 400
    return {
        "date": snapshot_date,
        "count": len(requested_nm_ids),
        "items": [
            {
                "nm_id": nm_id,
                "name": f"NM {nm_id}",
                "vendor_code": f"VC-{nm_id}",
                "view_count": base + index,
                "open_card_count": 30 + index,
                "ctr": 40 + index,
            }
            for index, nm_id in enumerate(requested_nm_ids)
        ],
    }


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
