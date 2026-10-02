"""Exercise prepared window_v3 HTTP content negotiation without a runtime fixture."""

from __future__ import annotations

from gzip import compress, decompress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
import threading
from time import perf_counter, sleep
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    _write_web_vitrina_window_v3_prepared_response,
)
from packages.application.web_vitrina_window_v3 import WindowV3Prepared  # noqa: E402
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer  # noqa: E402


def check_prepared_writer() -> None:
    body = b'{"response_schema_version":3,"window_format":"window_v3","kind":"manifest"}'
    prepared = WindowV3Prepared(
        kind="manifest", session_id="test", json_bytes=body, gzip_bytes=compress(body),
        build_ms=1.0, encode_ms=2.0, gzip_ms=3.0,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            _write_web_vitrina_window_v3_prepared_response(
                self, HTTPStatus.OK, prepared, request_started_perf=perf_counter(),
            )

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        cases = [("gzip", True), ("gzip;q=0", False), ("notgzip", False),
                 ("*;q=1,gzip;q=0", False), ("*;q=0.5", True)]
        for accept_encoding, expected_gzip in cases:
            with urlopen(Request(url, headers={"Accept-Encoding": accept_encoding}), timeout=5) as response:
                encoded = response.headers.get("Content-Encoding") == "gzip"
                wire = response.read()
                if encoded != expected_gzip or (decompress(wire) if encoded else wire) != body:
                    raise AssertionError(f"incorrect gzip negotiation for {accept_encoding!r}")
                if not response.headers.get("Server-Timing") or response.headers.get("Cache-Control") != "private, no-store":
                    raise AssertionError("prepared response lost timing or cache headers")
        print("window_v3_http: prepared gzip negotiation ok")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def read_window(base_url: str, operation: str, *, accept_encoding: str = "gzip", **params: str):
    query = {"surface": "page_composition", "window_format": "window_v3", "window_op": operation, **params}
    request = Request(base_url + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?" + urlencode(query),
                      headers={"Accept-Encoding": accept_encoding})
    try:
        response = urlopen(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = decompress(raw)
        return response.status, response.headers, json.loads(raw)


def ready_window_with_job(base_url: str, operation: str, **params: str):
    deadline = perf_counter() + 90
    status, headers, payload = read_window(base_url, operation, **params)
    job_id = payload.get("job_id", "") if status == 202 else ""
    while status == 202 and perf_counter() < deadline:
        sleep(min(0.5, max(0.05, float(payload.get("retry_after_ms", 350)) / 1000)))
        status, headers, payload = read_window(base_url, "job", job_id=payload["job_id"])
    if status != 200:
        raise AssertionError(f"{operation} did not finish: {status} {payload}")
    return headers, payload, job_id


def ready_window(base_url: str, operation: str, **params: str):
    headers, payload, _job_id = ready_window_with_job(base_url, operation, **params)
    return headers, payload


def check_real_http_route() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True, advertise_window_v3=True) as base_url:
        shell_url = base_url + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?surface=page_composition&shell_format=metadata_v2"
        with urlopen(shell_url, timeout=30) as response:
            shell = json.load(response)
        if shell.get("meta", {}).get("window_v3_available") is not True:
            raise AssertionError("metadata_v2 shell did not advertise the ready route")
        headers, manifest, manifest_job_id = ready_window_with_job(
            base_url, "manifest", date_from="2026-04-20", date_to="2026-04-21")
        if manifest.get("response_schema_version") != 3 or len(manifest.get("dates", [])) != 2 \
                or not manifest.get("rows") or headers.get("Content-Encoding") != "gzip":
            raise AssertionError("real HTTP manifest shape/gzip failed")
        if manifest["dates"][0]["date"] != "2026-04-20" or manifest["dates"][1]["date"] != "2026-04-21":
            raise AssertionError("real manifest lost ascending date order")
        session = manifest["session_id"]
        token = manifest["content_token"]
        static = manifest["rows"][0]["static_cells"]
        if not manifest.get("static_value_encoding") or not static or not all(isinstance(value, list) for value in static.values()):
            raise AssertionError("manifest static cells were not compact")
        if not manifest_job_id:
            raise AssertionError("manifest did not expose a pollable job before ACK")
        repeated_status, _repeated_headers, repeated = read_window(base_url, "job", job_id=manifest_job_id)
        if repeated_status != 200 or repeated.get("session_id") != session:
            raise AssertionError("unacknowledged manifest result was not repeatable")
        _, global_result = ready_window(base_url, "global", session_id=session, content_token=token,
                                        search="", sort="", ack_job_ids=manifest_job_id)
        acked_status, _acked_headers, _acked = read_window(base_url, "job", job_id=manifest_job_id)
        if acked_status != 404:
            raise AssertionError("manifest bytes remained in the job cache after piggyback ACK")
        if global_result.get("matched_row_count") != len(manifest["rows"]):
            raise AssertionError("global full catalog count mismatch")
        status, _headers, seek = read_window(base_url, "seek", session_id=session, content_token=token,
                                            date_index="0", row_start="0", row_count="5")
        if status != 200 or not seek.get("cursor"):
            raise AssertionError(f"seek failed: {status} {seek}")
        _, chunk, chunk_job_id = ready_window_with_job(
            base_url, "chunk", session_id=session, content_token=token, cursor=seek["cursor"])
        if chunk.get("dates") != ["2026-04-20", "2026-04-21"] or chunk.get("row_count") != 5 \
                or len(chunk.get("rows", [])) != 5 or chunk.get("value_encoding", {}).get("format") != "indexed_cells_v2" \
                or not 0 < chunk.get("decoded_bytes", 0) <= 8388608:
            raise AssertionError("chunk bounds or indexed cell shape failed")
        if not chunk_job_id:
            raise AssertionError("chunk did not expose a pollable job before ACK")
        repeated_status, _repeated_headers, repeated = read_window(base_url, "job", job_id=chunk_job_id)
        if repeated_status != 200 or repeated.get("row_count") != chunk["row_count"]:
            raise AssertionError("unacknowledged chunk result was not repeatable")
        status, _headers, next_seek = read_window(base_url, "seek", session_id=session, content_token=token,
                                                 date_index="1", row_start="0", row_count="1",
                                                 ack_job_ids=chunk_job_id)
        if status != 200 or not next_seek.get("cursor"):
            raise AssertionError("chunk ACK cancelled its active session")
        acked_status, _acked_headers, _acked = read_window(base_url, "job", job_id=chunk_job_id)
        if acked_status != 404:
            raise AssertionError("chunk bytes remained in the job cache after piggyback ACK")
        status, _headers, _next_seek = read_window(base_url, "seek", session_id=session, content_token=token,
                                                  date_index="1", row_start="0", row_count="1",
                                                  ack_job_ids=chunk_job_id)
        if status != 200:
            raise AssertionError("repeating an already delivered ACK was not idempotent")
        invalid_url = base_url + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?surface=page_composition&window_format=window_v3&window_op=manifest&date_from=2026-04-20&date_from=2026-04-21&date_to=2026-04-21"
        try:
            urlopen(invalid_url, timeout=10)
        except HTTPError as error:
            if error.code != 422:
                raise AssertionError("duplicate query was not rejected") from error
        else:
            raise AssertionError("duplicate query was accepted")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1200, "height": 800})
                page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH +
                          "?history_mode=explicit&date_from=2026-04-20&date_to=2026-04-21",
                          wait_until="domcontentloaded", timeout=90000)
                page.wait_for_function("() => state.windowV3.visibleCells.size > 0", timeout=90000)
                visible = page.evaluate("""() => ({rows: document.querySelectorAll('[data-table-body] tr.metric-data-row').length,
                  dates: document.querySelectorAll('[data-table-head] th[data-col-id^="date:"]').length,
                  error: state.windowV3.error, chunks: state.windowV3.transferChunks.size})""")
                if not 0 < visible["rows"] <= 200 or visible["dates"] != 2 or visible["chunks"] > 2 or visible["error"]:
                    raise AssertionError(f"real HTTP browser window failed: {visible}")
            finally:
                browser.close()
        print("window_v3_http: manifest/global/seek/chunk/browser ok ->", len(manifest["rows"]), "rows")


def main() -> None:
    check_prepared_writer()
    check_real_http_route()


if __name__ == "__main__":
    main()
