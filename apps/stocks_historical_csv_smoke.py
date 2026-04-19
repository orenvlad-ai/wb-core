"""Targeted smoke-check for historical stocks CSV adapter."""

from __future__ import annotations

from dataclasses import asdict
import io
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socket
import sys
import threading
import zipfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.stocks_block import HistoricalCsvBackedStocksSource
from packages.application.stocks_block import transform_legacy_payload
from packages.contracts.stocks_block import StocksRequest

TOKEN_ENV = "WB_STOCKS_HISTORY_SMOKE_TOKEN"


def main() -> None:
    previous_token = __import__("os").environ.get(TOKEN_ENV)
    __import__("os").environ[TOKEN_ENV] = "stocks-history-smoke-token"
    try:
        with _HistoricalStocksApiStub() as stub:
            sleep_calls: list[float] = []
            source = HistoricalCsvBackedStocksSource(
                base_url=stub.base_url,
                token_env_var=TOKEN_ENV,
                base_url_env_var="",
                timeout_seconds=5.0,
                poll_interval_seconds=0.01,
                max_poll_attempts=5,
                max_days_per_report=2,
                max_retries_on_429=2,
                sleep_fn=lambda seconds: sleep_calls.append(seconds),
                warehouse_region_resolver=lambda _nm_ids: {
                    "Коледино": "Центральный",
                    "Краснодар": "Южный и Северо-Кавказский",
                },
            )
            window_result = source.fetch_window(
                date_from="2026-04-12",
                date_to="2026-04-14",
                nm_ids=[101, 202],
            )
            if len(window_result.download_ids) != 2:
                raise AssertionError(f"expected 2 report batches, got {window_result.download_ids}")
            if sorted(window_result.payloads) != ["2026-04-12", "2026-04-13", "2026-04-14"]:
                raise AssertionError(f"unexpected payload dates: {sorted(window_result.payloads)}")
            envelope = transform_legacy_payload(window_result.payloads["2026-04-12"])
            if envelope.result.kind != "success":
                raise AssertionError(f"historical payload must stay success, got {asdict(envelope)}")
            total_by_nm = {item.nm_id: item.stock_total for item in envelope.result.items}
            if total_by_nm != {101: 7.0, 202: 4.0}:
                raise AssertionError(f"unexpected totals for 2026-04-12: {total_by_nm}")
            if not any(body["params"]["currentPeriod"] == {"start": "2026-04-12", "end": "2026-04-13"} for body in stub.created_reports):
                raise AssertionError("first batch currentPeriod mismatch")
            if not any(body["params"]["currentPeriod"] == {"start": "2026-04-14", "end": "2026-04-14"} for body in stub.created_reports):
                raise AssertionError("second batch currentPeriod mismatch")

            single_payload = source.fetch(
                StocksRequest(
                    snapshot_type="stocks",
                    snapshot_date="2026-04-13",
                    nm_ids=[101, 202],
                )
            )
            single_envelope = transform_legacy_payload(single_payload)
            if single_envelope.result.kind != "success":
                raise AssertionError("single-date historical fetch must stay success")
            single_total = sum(item.stock_total for item in single_envelope.result.items)
            if single_total != 16.0:
                raise AssertionError(f"unexpected single-date stock total: {single_total}")
            if not sleep_calls:
                raise AssertionError("expected bounded 429 retry sleep during polling")
            if stub.poll_request_count < 2:
                raise AssertionError(f"expected repeated poll attempts after 429, got {stub.poll_request_count}")

            print(f"window_fetch: ok -> {window_result.download_ids}")
            print(f"single_fetch: ok -> total_stock_total={single_total}")
            print(f"rate_limit_retry: ok -> poll_count={stub.poll_request_count}")
            print("smoke-check passed")
    finally:
        if previous_token is None:
            __import__("os").environ.pop(TOKEN_ENV, None)
        else:
            __import__("os").environ[TOKEN_ENV] = previous_token


class _HistoricalStocksApiStub:
    def __init__(self) -> None:
        self.created_reports: list[dict[str, Any]] = []
        self._reports: dict[str, dict[str, Any]] = {}
        self.poll_request_count = 0
        self._server = HTTPServer(("127.0.0.1", _reserve_free_port()), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "_HistoricalStocksApiStub":
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
                if self.path != "/api/v2/nm-report/downloads":
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return
                raw_length = int(self.headers.get("Content-Length", "0") or "0")
                payload = json.loads(self.rfile.read(raw_length).decode("utf-8"))
                stub.created_reports.append(payload)
                report_id = str(payload["id"])
                stub._reports[report_id] = {
                    "id": report_id,
                    "status": "SUCCESS",
                    "name": str(payload["userReportName"]),
                    "createdAt": "2026-04-19 10:00:00",
                    "payload": payload,
                }
                body = json.dumps({"data": "Началось формирование файла/отчета"}, ensure_ascii=False).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/v2/nm-report/downloads":
                    stub.poll_request_count += 1
                    if stub.poll_request_count == 1:
                        self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
                        self.send_header("Retry-After", "0.01")
                        self.end_headers()
                        return
                    body = json.dumps({"data": list(stub._reports.values())}, ensure_ascii=False).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                prefix = "/api/v2/nm-report/downloads/file/"
                if not self.path.startswith(prefix):
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return
                report_id = self.path[len(prefix):]
                report = stub._reports.get(report_id)
                if report is None:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return
                body = _build_csv_zip(report["payload"])
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

        return Handler


def _build_csv_zip(payload: dict[str, Any]) -> bytes:
    period = payload["params"]["currentPeriod"]
    start = period["start"]
    end = period["end"]
    rows = _csv_rows_for_period(start=start, end=end)
    csv_text = _rows_to_csv(rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{payload['id']}.csv", csv_text.encode("utf-8"))
    return buffer.getvalue()


def _csv_rows_for_period(*, start: str, end: str) -> list[dict[str, Any]]:
    all_rows = [
        {
            "VendorCode": "A",
            "Name": "Item A",
            "NmID": 101,
            "SubjectName": "Case",
            "BrandName": "Brand",
            "SizeName": "",
            "ChrtID": 1,
            "OfficeName": "Коледино",
            "12.04.2026": 3,
            "13.04.2026": 5,
            "14.04.2026": 7,
        },
        {
            "VendorCode": "A",
            "Name": "Item A",
            "NmID": 101,
            "SubjectName": "Case",
            "BrandName": "Brand",
            "SizeName": "",
            "ChrtID": 2,
            "OfficeName": "Краснодар",
            "12.04.2026": 4,
            "13.04.2026": 2,
            "14.04.2026": 0,
        },
        {
            "VendorCode": "B",
            "Name": "Item B",
            "NmID": 202,
            "SubjectName": "Case",
            "BrandName": "Brand",
            "SizeName": "",
            "ChrtID": 3,
            "OfficeName": "Коледино",
            "12.04.2026": 4,
            "13.04.2026": 9,
            "14.04.2026": 1,
        },
    ]
    keep_headers = {"VendorCode", "Name", "NmID", "SubjectName", "BrandName", "SizeName", "ChrtID", "OfficeName"}
    for row in all_rows:
        for header in list(row):
            if header in keep_headers:
                continue
            iso_date = _csv_header_to_iso(header)
            if iso_date is None or iso_date < start or iso_date > end:
                row.pop(header, None)
    return all_rows


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    header = list(rows[0].keys())
    lines = [";".join(header)]
    for row in rows:
        lines.append(";".join(str(row.get(column, "")) for column in header))
    return "\n".join(lines)


def _csv_header_to_iso(value: str) -> str | None:
    parts = value.split(".")
    if len(parts) != 3:
        return None
    day, month, year = parts
    if not all(part.isdigit() for part in parts):
        return None
    return f"{year}-{month}-{day}"


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    main()
