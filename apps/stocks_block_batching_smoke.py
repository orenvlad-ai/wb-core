"""Targeted smoke-check for batched stocks adapter and 429 handling."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import socket
import sys
import threading
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.stocks_block import HttpBackedStocksSource
from packages.application.stocks_block import StocksBlock
from packages.contracts.stocks_block import StocksRequest

TOKEN_ENV = "WB_STOCKS_BATCHING_SMOKE_TOKEN"


def main() -> None:
    previous_token = os.environ.get(TOKEN_ENV)
    os.environ[TOKEN_ENV] = "stocks-batching-smoke-token"
    try:
        _check_batched_request_shape()
        _check_more_than_one_thousand_ids_are_chunked_atomically()
        _check_three_quantity_fields()
        _check_repeated_full_page_is_incomplete()
        _check_invalid_or_duplicate_rows_fail_closed()
        _check_cached_reuse()
        _check_retry_after_429()
        _check_retry_exhaustion_surfaces_429()
        print("smoke-check passed")
    finally:
        if previous_token is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = previous_token


def _check_batched_request_shape() -> None:
    with _StocksApiStub(
        [
            _json_response(
                200,
                {
                    "data": {
                        "items": [
                            _stock_item(101, 1011, 1, "Центральный", 7),
                            _stock_item(101, 1011, 2, "Уральский", 5),
                            _stock_item(202, 2021, 1, "Центральный", 4),
                        ]
                    }
                },
            )
        ]
    ) as stub:
        result = _execute_request(
            stub,
            nm_ids=[101, 202],
            page_limit=250000,
            reuse_ttl_seconds=0.0,
        )
        if result["result"]["kind"] != "success":
            raise AssertionError(f"expected success, got {result['result']['kind']}")
        if result["result"]["count"] != 2:
            raise AssertionError(f"expected count=2, got {result['result']['count']}")
        if len(stub.request_bodies) != 1:
            raise AssertionError("stocks adapter must batch multiple nmIds into one request")
        body = stub.request_bodies[0]
        if sorted(body.get("nmIds", [])) != [101, 202]:
            raise AssertionError(f"unexpected nmIds payload: {body}")
        if body.get("chrtIds") != []:
            raise AssertionError("stocks adapter must send explicit empty chrtIds for nmId-only snapshot")
        if "nmID" in body:
            raise AssertionError("stocks adapter must not use legacy per-nmID request body")
        print("batched-request: ok -> one request carries the whole nmIds set")


def _check_more_than_one_thousand_ids_are_chunked_atomically() -> None:
    nm_ids = list(range(1, 1002))
    first_items = [_stock_item(nm_id, nm_id * 10, 1, "Центральный", 1) for nm_id in nm_ids[:1000]]
    second_items = [_stock_item(nm_ids[-1], nm_ids[-1] * 10, 1, "Центральный", 2)]
    with _StocksApiStub(
        [
            _json_response(200, {"data": {"items": first_items}}),
            _json_response(200, {"data": {"items": second_items}}),
        ]
    ) as stub:
        result = _execute_request(
            stub,
            nm_ids=nm_ids,
            page_limit=250000,
            min_request_interval_seconds=0.0,
        )
        if result["result"]["kind"] != "success" or result["result"]["count"] != len(nm_ids):
            raise AssertionError("all chunked nmIds must publish as one successful snapshot")
        if [len(body.get("nmIds") or []) for body in stub.request_bodies] != [1000, 1]:
            raise AssertionError("official stock batches must never exceed 1000 nmIds")


def _check_cached_reuse() -> None:
    with _StocksApiStub(
        [
            _json_response(
                200,
                {
                    "data": {
                        "items": [
                            _stock_item(101, 1011, 1, "Центральный", 11),
                            _stock_item(202, 2021, 1, "Центральный", 12),
                        ]
                    }
                },
            )
        ]
    ) as stub:
        source = _build_source(
            stub,
            page_limit=250000,
            reuse_ttl_seconds=30.0,
        )
        block = StocksBlock(source)
        request = StocksRequest(snapshot_type="stocks", snapshot_date="2026-04-15", nm_ids=[101, 202])
        first = block.execute(request)
        second = block.execute(request)
        if first.result.kind != "success" or second.result.kind != "success":
            raise AssertionError("cached fetch must preserve success shape")
        if len(stub.request_bodies) != 1:
            raise AssertionError("cached duplicate request must reuse the same stocks snapshot")
        print("cache-reuse: ok -> repeated request reuses the shared snapshot")


def _check_three_quantity_fields() -> None:
    with _StocksApiStub(
        [
            _json_response(
                200,
                {
                    "data": {
                        "items": [
                            {
                                "nmId": 101,
                                "chrtId": 1011,
                                "warehouseId": 1,
                                "regionName": "Центральный",
                                "quantity": 7,
                                "inWayToClient": 3,
                                "inWayFromClient": 2,
                            }
                        ]
                    }
                },
            )
        ]
    ) as stub:
        result = _execute_request(stub, nm_ids=[101], page_limit=250000)
        item = result["result"]["items"][0]
        if (
            item["stock_total"] != 7
            or item["in_way_to_client"] != 3
            or item["in_way_from_client"] != 2
            or item["wb_contour_total"] != 12
        ):
            raise AssertionError(f"official WB contour fields were lost: {item}")


def _check_repeated_full_page_is_incomplete() -> None:
    repeated = {
        "data": {"items": [_stock_item(101, 1011, 1, "Центральный", 1)]}
    }
    with _StocksApiStub([_json_response(200, repeated), _json_response(200, repeated)]) as stub:
        try:
            _execute_request(stub, nm_ids=[101], page_limit=1)
        except RuntimeError as exc:
            if not any(
                marker in str(exc)
                for marker in ("repeated a full page", "duplicate size/warehouse")
            ):
                raise AssertionError(f"unexpected pagination failure: {exc}") from exc
        else:
            raise AssertionError("repeated full official page must not publish a snapshot")


def _check_invalid_or_duplicate_rows_fail_closed() -> None:
    invalid = _stock_item(101, 1011, 1, "Центральный", 1)
    invalid.pop("inWayFromClient")
    invalid_warehouse = _stock_item(101, 1011, 1, "Центральный", 1)
    invalid_warehouse["warehouseId"] = None
    for items, expected in (
        ([invalid], "invalid inWayFromClient"),
        (
            [invalid_warehouse],
            "invalid warehouseId for nmId 101 "
            "(present=true, type=NoneType, value=None, "
            'context={"warehouseName":"Склад 1","regionName":"Центральный",'
            '"quantity":1,"inWayToClient":0,"inWayFromClient":0}, row_digest=',
        ),
        (
            [
                _stock_item(101, 1011, 1, "Центральный", 1),
                _stock_item(101, 1011, 1, "Центральный", 2),
            ],
            "duplicate size/warehouse",
        ),
    ):
        with _StocksApiStub([_json_response(200, {"data": {"items": items}})]) as stub:
            try:
                _execute_request(stub, nm_ids=[101], page_limit=250000)
            except RuntimeError as exc:
                if expected not in str(exc):
                    raise AssertionError(f"unexpected invalid-payload failure: {exc}") from exc
            else:
                raise AssertionError("invalid official payload must not publish a snapshot")


def _check_retry_after_429() -> None:
    with _StocksApiStub(
        [
            _json_response(
                429,
                {"title": "too many requests", "status": 429},
                headers={
                    "Content-Type": "application/json",
                    "X-Ratelimit-Retry": "0",
                    "X-Ratelimit-Reset": "0",
                },
            ),
            _json_response(
                200,
                {
                    "data": {
                        "items": [
                            _stock_item(101, 1011, 1, "Центральный", 1),
                            _stock_item(202, 2021, 1, "Центральный", 2),
                        ]
                    }
                },
            ),
        ]
    ) as stub:
        result = _execute_request(
            stub,
            nm_ids=[101, 202],
            page_limit=250000,
            min_request_interval_seconds=0.0,
            max_retries_on_429=1,
            reuse_ttl_seconds=0.0,
        )
        if result["result"]["kind"] != "success":
            raise AssertionError("bounded 429 retry must recover when the next attempt succeeds")
        if len(stub.request_bodies) != 2:
            raise AssertionError("429 recovery must issue exactly one retry")
        print("retry-429: ok -> bounded retry respects rate-limit response and recovers")


def _check_retry_exhaustion_surfaces_429() -> None:
    with _StocksApiStub(
        [
            _json_response(
                429,
                {"title": "too many requests", "status": 429},
                headers={
                    "Content-Type": "application/json",
                    "X-Ratelimit-Retry": "0",
                    "X-Ratelimit-Reset": "0",
                },
            ),
            _json_response(
                429,
                {"title": "too many requests", "status": 429},
                headers={
                    "Content-Type": "application/json",
                    "X-Ratelimit-Retry": "0",
                    "X-Ratelimit-Reset": "0",
                },
            ),
        ]
    ) as stub:
        try:
            _execute_request(
                stub,
                nm_ids=[101, 202],
                page_limit=250000,
                min_request_interval_seconds=0.0,
                max_retries_on_429=1,
                reuse_ttl_seconds=0.0,
            )
        except RuntimeError as exc:
            if "status 429" not in str(exc):
                raise AssertionError(f"unexpected 429 error text: {exc}") from exc
        else:
            raise AssertionError("exhausted 429 retries must surface the upstream failure")
        if len(stub.request_bodies) != 2:
            raise AssertionError("429 exhaustion must stop after the bounded retry budget")
        print("retry-429-exhausted: ok -> 429 is surfaced without fake success")


def _execute_request(
    stub: "_StocksApiStub",
    *,
    nm_ids: list[int],
    page_limit: int,
    min_request_interval_seconds: float = 0.0,
    max_retries_on_429: int = 0,
    reuse_ttl_seconds: float = 0.0,
) -> dict[str, Any]:
    source = _build_source(
        stub,
        page_limit=page_limit,
        min_request_interval_seconds=min_request_interval_seconds,
        max_retries_on_429=max_retries_on_429,
        reuse_ttl_seconds=reuse_ttl_seconds,
    )
    block = StocksBlock(source)
    return _as_dict(
        block.execute(
            StocksRequest(
                snapshot_type="stocks",
                snapshot_date="2026-04-15",
                nm_ids=nm_ids,
            )
        )
    )


def _build_source(
    stub: "_StocksApiStub",
    *,
    page_limit: int,
    min_request_interval_seconds: float = 0.0,
    max_retries_on_429: int = 0,
    reuse_ttl_seconds: float = 0.0,
) -> HttpBackedStocksSource:
    return HttpBackedStocksSource(
        base_url=stub.base_url,
        token_env_var=TOKEN_ENV,
        base_url_env_var="",
        timeout_seconds=5.0,
        page_limit=page_limit,
        min_request_interval_seconds=min_request_interval_seconds,
        max_retries_on_429=max_retries_on_429,
        reuse_ttl_seconds=reuse_ttl_seconds,
    )


def _as_dict(value: Any) -> Any:
    if hasattr(value, "__dict__"):
        return {key: _as_dict(item) for key, item in value.__dict__.items()}
    if isinstance(value, list):
        return [_as_dict(item) for item in value]
    return value


def _json_response(
    status: int,
    body: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _stock_item(
    nm_id: int,
    chrt_id: int,
    warehouse_id: int,
    region_name: str,
    quantity: int,
) -> dict[str, Any]:
    return {
        "nmId": nm_id,
        "chrtId": chrt_id,
        "warehouseId": warehouse_id,
        "warehouseName": f"Склад {warehouse_id}",
        "regionName": region_name,
        "quantity": quantity,
        "inWayToClient": 0,
        "inWayFromClient": 0,
    }


class _StocksApiStub:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
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
                response = stub._responses.pop(0)
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


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
