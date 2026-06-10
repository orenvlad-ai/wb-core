"""Targeted smoke-check for the official WB FBW supplies adapter."""

from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import sys
from urllib import error as urllib_error

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_supplies import (  # noqa: E402
    HttpBackedWbSuppliesSource,
    WbSuppliesHttpStatusError,
    WbSuppliesTransportError,
)


TOKEN_VALUE = "adapter-smoke-token"


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class RecordingOpener:
    def __init__(self) -> None:
        self.requests = []
        self.next_payload = b"[]"
        self.next_error = None

    def __call__(self, req, timeout):
        self.requests.append((req, timeout))
        if self.next_error is not None:
            raise self.next_error
        return FakeResponse(self.next_payload)


def main() -> None:
    original_token = os.environ.get("WB_API_TOKEN")
    original_base = os.environ.get("WB_SUPPLIES_API_BASE_URL")
    try:
        os.environ["WB_API_TOKEN"] = TOKEN_VALUE
        os.environ["WB_SUPPLIES_API_BASE_URL"] = "https://supplies-api.example.test"

        opener = RecordingOpener()
        opener.next_payload = json.dumps(
            [
                {"supplyID": 101, "preorderID": 201, "statusID": 5},
                {"supplyID": 102, "preorderID": 202, "statusID": 2},
            ]
        ).encode("utf-8")
        source = HttpBackedWbSuppliesSource(opener=opener)
        result = source.list_supplies(limit=50, offset=100, status_ids=[5, "6", "bad"], dates=[])
        if result.raw_count != 2 or result.limit != 50 or result.offset != 100:
            raise AssertionError(f"list result metadata changed unexpectedly: {result}")
        req, timeout = opener.requests[-1]
        if req.get_method() != "POST":
            raise AssertionError("supplies list must use POST")
        if "limit=50" not in req.full_url or "offset=100" not in req.full_url:
            raise AssertionError(f"supplies list must pass limit/offset as query params, got {req.full_url}")
        if req.get_header("Authorization") != TOKEN_VALUE:
            raise AssertionError("supplies adapter must send WB_API_TOKEN in Authorization header")
        if req.get_header("Content-type") != "application/json":
            raise AssertionError("supplies list must send JSON body")
        body = json.loads(req.data.decode("utf-8"))
        if body != {"dates": [], "statusIDs": [5, 6]}:
            raise AssertionError(f"supplies list body shape changed: {body}")
        if timeout <= 0:
            raise AssertionError("supplies adapter must pass timeout to opener")

        opener.next_payload = json.dumps({"warehouseID": 507, "warehouseName": "Коледино"}).encode("utf-8")
        detail = source.fetch_supply_details(101)
        if detail.get("warehouseName") != "Коледино":
            raise AssertionError(f"details endpoint must parse JSON object, got {detail}")
        detail_req, _ = opener.requests[-1]
        if "/api/v1/supplies/101" not in detail_req.full_url or "isPreorderID=false" not in detail_req.full_url:
            raise AssertionError(f"details URL changed unexpectedly: {detail_req.full_url}")

        opener.next_payload = json.dumps(
            [
                {
                    "transitWarehouseName": "Обухово",
                    "destinationWarehouseName": "Склад Шушары",
                    "boxTariff": [{"from": 1500, "to": 0, "value": 3.5}],
                }
            ]
        ).encode("utf-8")
        transit_tariffs = source.fetch_transit_tariffs()
        if transit_tariffs[0].get("transitWarehouseName") != "Обухово":
            raise AssertionError(f"transit tariffs endpoint must parse rows, got {transit_tariffs}")
        transit_req, _ = opener.requests[-1]
        if transit_req.get_method() != "GET" or "/api/v1/transit-tariffs" not in transit_req.full_url:
            raise AssertionError(f"transit tariffs URL changed unexpectedly: {transit_req.full_url}")

        opener.next_error = urllib_error.HTTPError(
            "https://supplies-api.example.test/api/v1/supplies",
            401,
            "Unauthorized",
            hdrs=None,
            fp=BytesIO(b'{"error":"bad token"}'),
        )
        try:
            source.list_supplies(limit=20, offset=0)
        except WbSuppliesHttpStatusError as exc:
            if exc.status_code != 401:
                raise AssertionError(f"401 must be preserved, got {exc.status_code}")
            if TOKEN_VALUE in str(exc) or TOKEN_VALUE in exc.body:
                raise AssertionError("adapter errors must not print token")
        else:
            raise AssertionError("HTTP 401 must raise WbSuppliesHttpStatusError")

        opener.next_error = None
        opener.next_payload = b"<html>not json</html>"
        try:
            source.fetch_warehouses()
        except WbSuppliesTransportError as exc:
            if "non-JSON" not in str(exc):
                raise AssertionError(f"non-JSON error must be controlled, got {exc}")
        else:
            raise AssertionError("non-JSON upstream response must raise transport error")

        opener.next_error = urllib_error.URLError("timeout")
        try:
            source.fetch_warehouses()
        except WbSuppliesTransportError as exc:
            if "transport failed" not in str(exc):
                raise AssertionError(f"transport error must be controlled, got {exc}")
        else:
            raise AssertionError("transport failure must raise WbSuppliesTransportError")
    finally:
        _restore_env("WB_API_TOKEN", original_token)
        _restore_env("WB_SUPPLIES_API_BASE_URL", original_base)

    print("wb_supplies_api_adapter_smoke: OK")


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    main()
