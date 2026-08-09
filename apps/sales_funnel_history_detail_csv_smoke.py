"""Targeted smoke for official DETAIL_HISTORY_REPORT CSV acquisition."""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
from typing import Any
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.sales_funnel_history_block import (  # noqa: E402
    DetailHistoryCsvBackedSalesFunnelHistorySource,
)
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock  # noqa: E402
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryRequest  # noqa: E402


TOKEN_ENV = "WB_DETAIL_HISTORY_CSV_SMOKE_TOKEN"


def main() -> None:
    previous_token = os.environ.get(TOKEN_ENV)
    os.environ[TOKEN_ENV] = "detail-history-smoke-token"
    try:
        with _DetailHistoryApiStub() as stub:
            source = _source(stub)
            result = SalesFunnelHistoryBlock(source).execute(_request()).result
            if result.kind != "success" or result.count != 8:
                raise AssertionError(f"unexpected complete result: {asdict(result)}")
            metrics = {
                (item.date, item.nm_id, item.metric): item.value
                for item in result.items
            }
            if metrics[("2026-07-22", 101, "orderCount")] != 10.0:
                raise AssertionError(f"ordersCount mapping failed: {metrics}")
            if metrics[("2026-07-22", 101, "buyoutPercent")] != 0.5:
                raise AssertionError(f"buyoutPercent fraction mapping failed: {metrics}")
            if metrics[("2026-07-23", 202, "buyoutPercent")] != 0.0:
                raise AssertionError(f"zero buyoutPercent mapping failed: {metrics}")
            created = stub.created_reports[0]
            if created.get("reportType") != "DETAIL_HISTORY_REPORT":
                raise AssertionError(f"wrong report type: {created}")
            expected_params = {
                "nmIDs": [101, 202],
                "subjectIds": [],
                "brandNames": [],
                "tagIds": [],
                "startDate": "2026-07-22",
                "endDate": "2026-07-23",
                "timezone": "Asia/Yekaterinburg",
                "aggregationLevel": "day",
                "skipDeletedNm": False,
            }
            if created.get("params") != expected_params:
                raise AssertionError(f"wrong DETAIL_HISTORY_REPORT params: {created.get('params')}")
            evidence = dict(source.last_fetch_evidence)
            if evidence.get("covered_pair_count") != 4 or evidence.get("expected_pair_count") != 4:
                raise AssertionError(f"coverage evidence mismatch: {evidence}")
            if not str(evidence.get("csv_sha256") or "").startswith("sha256:"):
                raise AssertionError(f"CSV digest missing: {evidence}")

            stub.omit_last_pair = True
            try:
                SalesFunnelHistoryBlock(_source(stub)).execute(_request())
            except RuntimeError as exc:
                if "missing_pair_count=1" not in str(exc):
                    raise AssertionError(f"wrong incomplete-coverage failure: {exc}") from exc
            else:
                raise AssertionError("incomplete enabled-SKU/date CSV must fail closed")

            print("detail_history_csv_complete_coverage: ok -> 4/4 pairs")
            print("detail_history_csv_normalization: ok -> percent fraction + orderCount")
            print("detail_history_csv_incomplete_coverage: ok -> failed closed")
            print("smoke-check passed")
    finally:
        if previous_token is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = previous_token


def _request() -> SalesFunnelHistoryRequest:
    return SalesFunnelHistoryRequest(
        snapshot_type="sales_funnel_history",
        date_from="2026-07-22",
        date_to="2026-07-23",
        nm_ids=[202, 101],
    )


def _source(stub: "_DetailHistoryApiStub") -> DetailHistoryCsvBackedSalesFunnelHistorySource:
    return DetailHistoryCsvBackedSalesFunnelHistorySource(
        base_url=stub.base_url,
        token_env_var=TOKEN_ENV,
        base_url_env_var="",
        timeout_seconds=5.0,
        poll_interval_seconds=0.01,
        max_poll_attempts=3,
        sleep_fn=lambda _seconds: None,
    )


class _DetailHistoryApiStub:
    def __init__(self) -> None:
        self.created_reports: list[dict[str, Any]] = []
        self._reports: dict[str, dict[str, Any]] = {}
        self.omit_last_pair = False
        self._server = HTTPServer(("127.0.0.1", _reserve_free_port()), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "_DetailHistoryApiStub":
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/api/v2/nm-report/downloads":
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                stub.created_reports.append(payload)
                report_id = str(payload["id"])
                stub._reports[report_id] = {
                    "id": report_id,
                    "status": "SUCCESS",
                    "name": payload["userReportName"],
                    "createdAt": "2026-08-09 14:30:00",
                    "payload": payload,
                }
                self._json({"data": "started"})

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/v2/nm-report/downloads":
                    self._json({"data": list(stub._reports.values())})
                    return
                prefix = "/api/v2/nm-report/downloads/file/"
                if not self.path.startswith(prefix):
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return
                report = stub._reports.get(self.path[len(prefix) :])
                if report is None:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return
                body = _csv_zip(omit_last_pair=stub.omit_last_pair)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

        return Handler


def _csv_zip(*, omit_last_pair: bool) -> bytes:
    rows = [
        [101, "2026-07-22", 10, "50"],
        [202, "2026-07-22", 5, "80,5"],
        [101, "2026-07-23", 0, 100],
        [202, "2026-07-23", 0, 0],
    ]
    if omit_last_pair:
        rows.pop()
    text = "nmID;dt;ordersCount;buyoutPercent\n" + "\n".join(
        ";".join(str(value) for value in row) for row in rows
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("detail.csv", text.encode("utf-8"))
    return buffer.getvalue()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
