"""Fixture-backed smoke for the bounded 1C/Soykasoft WB stocks source."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import socketserver
import sys
import threading
from urllib import parse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.onec_stocks_block import (
    ArtifactBackedOnecStocksSource,
    HttpBackedOnecStocksSource,
    OnecStocksHttpError,
)
from packages.application.onec_stocks_block import (
    OnecStocksBlock,
    normalize_onec_stocks_payload,
    parse_onec_stocks_payload,
)
from packages.contracts.onec_stocks_block import (
    ONEC_STOCKS_PARTIAL_FETCH_META_KEY,
    OnecStocksRequest,
)


ARTIFACTS = ROOT / "artifacts" / "onec_stocks_block"
EXPECTED_STAGE_NAMES = ["В_пути", "ВБ", "Фулфиллмент"]


def _load_fixture() -> dict:
    source = ArtifactBackedOnecStocksSource(ARTIFACTS)
    return dict(
        source.fetch(
            OnecStocksRequest(
                snapshot_type="onec_stocks",
                account_id="000000001",
                nm_ids=[428855306],
            )
        )
    )


def _check_parser(payload: dict) -> None:
    parsed = parse_onec_stocks_payload(payload)
    if parsed.meta.version != "1.0":
        raise AssertionError(f"unexpected meta.version: {parsed.meta.version}")
    if parsed.meta.currency != "RUB":
        raise AssertionError(f"unexpected meta.currency: {parsed.meta.currency}")
    if len(parsed.items) != 1:
        raise AssertionError(f"unexpected item count: {len(parsed.items)}")
    stage_names = list(parsed.items[0].stages)
    if stage_names != EXPECTED_STAGE_NAMES:
        raise AssertionError(f"unexpected fixture stages: {stage_names}")
    print("parser: ok")


def _check_dynamic_stage_parser(payload: dict) -> None:
    dynamic_payload = deepcopy(payload)
    dynamic_stage_name = "Новая секция 1С / тест"
    dynamic_payload["items"][0]["stages"][dynamic_stage_name] = {
        "qty": 1,
        "unit_cost_rub": 2.0,
        "cost_total_rub": 2.0,
    }
    parsed = parse_onec_stocks_payload(dynamic_payload)
    if dynamic_stage_name not in parsed.items[0].stages:
        raise AssertionError("parser must preserve previously unknown 1C stage names")
    print("dynamic-stage-parser: ok")


def _check_normalization(payload: dict) -> None:
    envelope = normalize_onec_stocks_payload(payload)
    result = envelope.result
    if result.kind != "success":
        raise AssertionError(f"unexpected result kind: {result.kind}")
    if result.item_count != 1 or result.stage_count != 3:
        raise AssertionError(
            f"unexpected counts: item_count={result.item_count}, stage_count={result.stage_count}"
        )
    if result.dynamic_stage_names != EXPECTED_STAGE_NAMES:
        raise AssertionError(f"unexpected dynamic stages: {result.dynamic_stage_names}")
    if [row.stage_name for row in result.items] != EXPECTED_STAGE_NAMES:
        raise AssertionError("normalizer must keep source stage rows separate")
    if any(row.canonical_stage_code is not None for row in result.items):
        raise AssertionError("canonical stage codes must be empty without mapping config")
    print("normalization: ok")


def _check_mapping_boundary(payload: dict) -> None:
    envelope = normalize_onec_stocks_payload(
        payload,
        stage_mapping={"ВБ": "WB_STOCK"},
    )
    result = envelope.result
    if result.kind != "success":
        raise AssertionError(f"unexpected mapped result kind: {result.kind}")
    if result.stage_count != 3:
        raise AssertionError("stage mapping boundary must not aggregate source stages")
    mapped_rows = {row.stage_name: row.canonical_stage_code for row in result.items}
    if mapped_rows.get("ВБ") != "WB_STOCK":
        raise AssertionError(f"expected WB_STOCK mapping, got {mapped_rows.get('ВБ')}")
    if mapped_rows.get("В_пути") is not None or mapped_rows.get("Фулфиллмент") is not None:
        raise AssertionError("unmapped source stages must stay unmapped")
    print("stage-mapping-boundary: ok")


def _check_block(payload: dict) -> None:
    source = ArtifactBackedOnecStocksSource(ARTIFACTS)
    block = OnecStocksBlock(source)
    result = block.execute(
        OnecStocksRequest(
            snapshot_type="onec_stocks",
            account_id=str(payload["meta"]["account_id"]),
            nm_ids=[428855306],
        )
    ).result
    if result.kind != "success" or result.stage_count != 3:
        raise AssertionError(f"unexpected block result: {result}")
    print("block: ok")


def _check_partial_block(payload: dict) -> None:
    partial_payload = deepcopy(payload)
    partial_payload[ONEC_STOCKS_PARTIAL_FETCH_META_KEY] = {
        "requested_count": 2,
        "requested_nm_ids": [428855306, 210183919],
        "successful_request_count": 1,
        "failure_count": 1,
        "missing_nm_ids": [210183919],
        "status_codes": {"401": 1},
        "error_kinds": {"http": 1},
    }
    block = OnecStocksBlock(_StaticOnecStocksSource(partial_payload))
    result = block.execute(
        OnecStocksRequest(
            snapshot_type="onec_stocks",
            account_id=str(payload["meta"]["account_id"]),
            nm_ids=[428855306, 210183919],
        )
    ).result
    if result.kind != "incomplete":
        raise AssertionError(f"partial block must return incomplete, got {result.kind}")
    if result.requested_count != 2 or result.covered_count != 1:
        raise AssertionError(
            f"unexpected partial counts: requested={result.requested_count}, covered={result.covered_count}"
        )
    if result.missing_nm_ids != [210183919]:
        raise AssertionError(f"unexpected partial missing nmIds: {result.missing_nm_ids}")
    if result.snapshot_date != str(payload["meta"]["date"]):
        raise AssertionError(f"partial snapshot date mismatch: {result.snapshot_date}")
    if "status_codes=401:1" not in result.detail:
        raise AssertionError(f"partial detail must keep sanitized status code counts, got {result.detail}")
    print("partial-block: ok")


def _check_http_account_snapshot_fallback(payload: dict) -> None:
    opener = _PerSkuUnauthorizedThenAccountSnapshot(payload)
    with _temporary_onec_live_env():
        block = OnecStocksBlock(HttpBackedOnecStocksSource(opener=opener))
        full_result = block.execute(
            OnecStocksRequest(
                snapshot_type="onec_stocks",
                account_id=str(payload["meta"]["account_id"]),
                nm_ids=[428855306],
            )
        ).result
        if full_result.kind != "success" or full_result.stage_count != 3:
            raise AssertionError(f"fallback full coverage must be success, got {full_result}")

    account_snapshot_urls = [
        url
        for url in opener.urls
        if "nmId=" not in parse.urlparse(url).query
    ]
    if len(account_snapshot_urls) != 1:
        raise AssertionError(
            f"expected one account snapshot fallback request, got {account_snapshot_urls}"
        )
    if any("account_id=000000001" not in url for url in account_snapshot_urls):
        raise AssertionError(f"unexpected account snapshot URLs: {account_snapshot_urls}")
    print("http-account-snapshot-fallback: ok")


def _check_http_account_snapshot_primary_multi_sku(payload: dict) -> None:
    opener = _AccountSnapshotOnly(payload)
    with _temporary_onec_live_env():
        block = OnecStocksBlock(HttpBackedOnecStocksSource(opener=opener))
        partial_result = block.execute(
            OnecStocksRequest(
                snapshot_type="onec_stocks",
                account_id=str(payload["meta"]["account_id"]),
                nm_ids=[428855306, 210183919],
            )
        ).result

    if partial_result.kind != "incomplete":
        raise AssertionError(f"primary account snapshot partial coverage must be incomplete, got {partial_result}")
    if partial_result.requested_count != 2 or partial_result.covered_count != 1:
        raise AssertionError(
            "unexpected primary account snapshot partial counts: "
            f"requested={partial_result.requested_count}, covered={partial_result.covered_count}"
        )
    if partial_result.missing_nm_ids != [210183919]:
        raise AssertionError(
            f"unexpected primary account snapshot partial missing nmIds: {partial_result.missing_nm_ids}"
        )
    if "status_codes=" in partial_result.detail or "account_snapshot_primary" not in partial_result.detail:
        raise AssertionError(
            "primary account snapshot partial detail must not invent per-SKU failure counts, "
            f"got {partial_result.detail}"
        )
    account_snapshot_urls = [
        url
        for url in opener.urls
        if "nmId=" not in parse.urlparse(url).query
    ]
    if len(account_snapshot_urls) != 1:
        raise AssertionError(
            f"expected one primary account snapshot request, got {account_snapshot_urls}"
        )
    if any("account_id=000000001" not in url for url in account_snapshot_urls):
        raise AssertionError(f"unexpected account snapshot URLs: {account_snapshot_urls}")
    if any("nmId=" in parse.urlparse(url).query for url in opener.urls):
        raise AssertionError(f"multi-SKU account snapshot must avoid per-SKU URLs first, got {opener.urls}")
    print("http-account-snapshot-primary-multi-sku: ok")


def _check_http_request_headers(payload: dict) -> None:
    opener = _HeaderCapturingOpener(payload)
    with _temporary_onec_live_env():
        block = OnecStocksBlock(HttpBackedOnecStocksSource(opener=opener))
        result = block.execute(
            OnecStocksRequest(
                snapshot_type="onec_stocks",
                account_id=str(payload["meta"]["account_id"]),
                nm_ids=[428855306],
            )
        ).result

    if result.kind != "success":
        raise AssertionError(f"header capture request must succeed, got {result.kind}")
    if len(opener.requests) != 1:
        raise AssertionError(f"expected one captured request, got {len(opener.requests)}")
    captured = opener.requests[0]
    if captured["method"] != "GET":
        raise AssertionError(f"1C request must use GET, got {captured['method']}")
    headers = captured["headers"]
    if headers.get("accept") != "application/json":
        raise AssertionError("1C request must send Accept: application/json")
    if headers.get("content-type") != "application/json":
        raise AssertionError("1C request must send Content-Type: application/json")
    if not str(headers.get("authorization") or "").startswith("Basic "):
        raise AssertionError("1C request must send Basic Authorization header")
    if not str(headers.get("token") or "").strip():
        raise AssertionError("1C request must send token header")
    print("http-request-headers: ok")


def _check_low_level_http_wire_request(payload: dict) -> None:
    with _RawOnecHttpServer(payload) as server:
        base_url = f"http://127.0.0.1:{server.port}/base/hs/soykasoft/stocks_wb?tenant=demo"
        with _temporary_onec_live_env({"ONEC_STOCKS_BASE_URL": base_url}):
            block = OnecStocksBlock(HttpBackedOnecStocksSource())
            result = block.execute(
                OnecStocksRequest(
                    snapshot_type="onec_stocks",
                    account_id=str(payload["meta"]["account_id"]),
                    nm_ids=[428855306],
                )
            ).result

    if result.kind != "success":
        raise AssertionError(f"wire capture request must succeed, got {result.kind}")
    if len(server.raw_requests) != 1:
        raise AssertionError(f"expected one raw HTTP request, got {len(server.raw_requests)}")
    raw_request = server.raw_requests[0].decode("iso-8859-1")
    request_line, raw_headers = raw_request.split("\r\n", 1)
    if not request_line.startswith("GET /base/hs/soykasoft/stocks_wb?"):
        raise AssertionError(f"unexpected 1C request target: {request_line}")
    if request_line.count("/hs/soykasoft/stocks_wb") != 1:
        raise AssertionError(f"1C request target must not duplicate endpoint path: {request_line}")
    if "tenant=demo" not in request_line or "account_id=000000001" not in request_line or "nmId=428855306" not in request_line:
        raise AssertionError(f"1C request target must preserve base and adapter query params: {request_line}")

    header_names = [
        line.split(":", 1)[0]
        for line in raw_headers.split("\r\n")
        if line and ":" in line
    ]
    for expected in ["host", "accept", "content-type", "authorization", "token"]:
        if expected not in header_names:
            raise AssertionError(f"wire request missing lower-case header {expected!r}: {header_names}")
    forbidden_cased_headers = {"Host", "Accept", "Content-Type", "Authorization", "Token"}
    leaked_cased_headers = sorted(forbidden_cased_headers.intersection(header_names))
    if leaked_cased_headers:
        raise AssertionError(f"wire request leaked non-lower-case headers: {leaked_cased_headers}")
    print("low-level-http-wire-request: ok")


class _StaticOnecStocksSource:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def fetch(self, request: OnecStocksRequest) -> dict:
        return deepcopy(self._payload)


class _PerSkuUnauthorizedThenAccountSnapshot:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.urls: list[str] = []

    def __call__(self, url: str, *, headers: dict[str, str], timeout: float) -> bytes:
        del headers
        del timeout
        self.urls.append(url)
        parsed_url = parse.urlparse(url)
        query = parse.parse_qs(parsed_url.query)
        if "nmId" in query:
            raise OnecStocksHttpError(401)
        return json.dumps(self._payload).encode("utf-8")


class _AccountSnapshotOnly:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.urls: list[str] = []

    def __call__(self, url: str, *, headers: dict[str, str], timeout: float) -> bytes:
        del headers
        del timeout
        self.urls.append(url)
        parsed_url = parse.urlparse(url)
        query = parse.parse_qs(parsed_url.query)
        if "nmId" in query:
            raise AssertionError(f"unexpected per-SKU request before account snapshot: {url}")
        return json.dumps(self._payload).encode("utf-8")


class _HeaderCapturingOpener:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.requests: list[dict[str, object]] = []

    def __call__(self, url: str, *, headers: dict[str, str], timeout: float) -> bytes:
        del timeout
        self.requests.append(
            {
                "method": "GET",
                "url": url,
                "headers": dict(headers),
            }
        )
        return json.dumps(self._payload).encode("utf-8")


class _RawOnecHttpServer:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.raw_requests: list[bytes] = []
        self._server = _ThreadedTcpServer(("127.0.0.1", 0), _RawOnecHttpHandler)
        self._server.payload = payload
        self._server.raw_requests = self.raw_requests
        self.port = int(self._server.server_address[1])

    def __enter__(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False


class _ThreadedTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _RawOnecHttpHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        raw_request = b""
        self.request.settimeout(5)
        while b"\r\n\r\n" not in raw_request:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            raw_request += chunk
        self.server.raw_requests.append(raw_request)
        body = json.dumps(self.server.payload).encode("utf-8")
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"content-type: application/json\r\n"
            + f"content-length: {len(body)}\r\n".encode("ascii")
            + b"connection: close\r\n"
            b"\r\n"
            + body
        )
        self.request.sendall(response)


class _temporary_onec_live_env:
    _VALUES = {
        "ONEC_STOCKS_BASE_URL": "https://onec.example",
        "ONEC_STOCKS_BASIC_USER": "user",
        "ONEC_STOCKS_BASIC_PASSWORD": "password",
        "ONEC_STOCKS_TOKEN": "token",
    }

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self._values = dict(self._VALUES)
        if overrides:
            self._values.update(overrides)

    def __enter__(self):
        self._previous = {name: os.environ.get(name) for name in self._values}
        os.environ.update(self._values)
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False


def main() -> None:
    payload = _load_fixture()
    _check_parser(payload)
    _check_dynamic_stage_parser(payload)
    _check_normalization(payload)
    _check_mapping_boundary(payload)
    _check_block(payload)
    _check_partial_block(payload)
    _check_http_account_snapshot_fallback(payload)
    _check_http_account_snapshot_primary_multi_sku(payload)
    _check_http_request_headers(payload)
    _check_low_level_http_wire_request(payload)
    print("smoke-check passed")


if __name__ == "__main__":
    main()
