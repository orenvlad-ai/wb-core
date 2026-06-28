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
    def __init__(self, payload: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers = headers or {}

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
        self.next_headers: dict[str, str] = {}
        self.next_error = None

    def __call__(self, req, timeout):
        self.requests.append((req, timeout))
        if self.next_error is not None:
            raise self.next_error
        return FakeResponse(self.next_payload, headers=self.next_headers)


def main() -> None:
    original_token = os.environ.get("WB_API_TOKEN")
    original_base = os.environ.get("WB_SUPPLIES_API_BASE_URL")
    original_marketplace_base = os.environ.get("WB_MARKETPLACE_API_BASE_URL")
    original_tariffs_base = os.environ.get("WB_TARIFFS_API_BASE_URL")
    try:
        os.environ["WB_API_TOKEN"] = TOKEN_VALUE
        os.environ["WB_SUPPLIES_API_BASE_URL"] = "https://supplies-api.example.test"
        os.environ["WB_MARKETPLACE_API_BASE_URL"] = "https://marketplace-api.example.test"
        os.environ["WB_TARIFFS_API_BASE_URL"] = "https://common-api.example.test"

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

        opener.next_payload = json.dumps(
            {"offices": [{"name": "Коледино", "federalDistrict": "Центральный федеральный округ"}]}
        ).encode("utf-8")
        offices = source.fetch_marketplace_offices()
        if offices[0].get("federalDistrict") != "Центральный федеральный округ":
            raise AssertionError(f"marketplace offices endpoint must parse rows, got {offices}")
        offices_req, _ = opener.requests[-1]
        if offices_req.get_method() != "GET" or offices_req.full_url != "https://marketplace-api.example.test/api/v3/offices":
            raise AssertionError(f"marketplace offices URL changed unexpectedly: {offices_req.full_url}")

        opener.next_payload = json.dumps(
            {"response": {"data": {"warehouseList": [{"warehouseName": "Казань", "geoName": "Приволжский федеральный округ"}]}}}
        ).encode("utf-8")
        box_tariffs = source.fetch_box_tariffs(tariff_date="2026-06-11")
        if box_tariffs[0].get("geoName") != "Приволжский федеральный округ":
            raise AssertionError(f"box tariffs endpoint must parse warehouseList rows, got {box_tariffs}")
        box_req, _ = opener.requests[-1]
        if box_req.get_method() != "GET" or box_req.full_url != "https://common-api.example.test/api/v1/tariffs/box?date=2026-06-11":
            raise AssertionError(f"box tariffs URL changed unexpectedly: {box_req.full_url}")

        opener.next_payload = json.dumps(
            {
                "result": [
                    {
                        "barcode": "4600000000001",
                        "warehouses": [{"warehouseID": 101, "warehouseName": "Коледино", "canBox": True}],
                    }
                ]
            }
        ).encode("utf-8")
        acceptance_options = source.fetch_acceptance_options(
            products=[
                {"barcode": "4600000000001", "quantity": 50},
                {"barcode": "", "quantity": 10},
                {"barcode": "4600000000002", "quantity": "25"},
            ],
            warehouse_id=101,
        )
        if acceptance_options.get("result", [{}])[0].get("warehouses", [])[0].get("warehouseID") != 101:
            raise AssertionError(f"acceptance/options must parse JSON object, got {acceptance_options}")
        acceptance_req, _ = opener.requests[-1]
        expected_acceptance_url = "https://supplies-api.example.test/api/v1/acceptance/options?warehouseID=101"
        if acceptance_req.get_method() != "POST" or acceptance_req.full_url != expected_acceptance_url:
            raise AssertionError(f"acceptance/options URL changed unexpectedly: {acceptance_req.full_url}")
        acceptance_body = json.loads(acceptance_req.data.decode("utf-8"))
        if acceptance_body != [
            {"barcode": "4600000000001", "quantity": 50},
            {"barcode": "4600000000002", "quantity": 25},
        ]:
            raise AssertionError(f"acceptance/options body must be official JSON array, got {acceptance_body}")

        acceptance_request_count = len(opener.requests)
        try:
            source.fetch_acceptance_options(products=[{"barcode": "", "quantity": 10}, {"barcode": " ", "quantity": 0}])
        except ValueError as exc:
            if "at least one product" not in str(exc):
                raise AssertionError(f"empty acceptance/options validation message changed: {exc}")
        else:
            raise AssertionError("empty acceptance/options product list must fail before upstream")
        if len(opener.requests) != acceptance_request_count:
            raise AssertionError("empty acceptance/options product list must not call upstream")

        opener.next_payload = json.dumps(
            {
                "response": {
                    "data": [
                        {"warehouseID": 101, "warehouseName": "Коледино", "date": "2026-07-01", "coefficient": 1}
                    ]
                }
            }
        ).encode("utf-8")
        coefficients = source.fetch_acceptance_coefficients(warehouse_ids=[101, "202"])
        if coefficients[0].get("coefficient") != 1:
            raise AssertionError(f"acceptance coefficients endpoint must parse rows, got {coefficients}")
        coefficients_req, _ = opener.requests[-1]
        expected_coefficients_url = "https://common-api.example.test/api/tariffs/v1/acceptance/coefficients?warehouseIDs=101%2C202"
        if coefficients_req.get_method() != "GET" or coefficients_req.full_url != expected_coefficients_url:
            raise AssertionError(f"acceptance coefficients URL changed unexpectedly: {coefficients_req.full_url}")

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
        opener.next_headers = {"Content-Type": "text/html; charset=utf-8"}
        opener.next_payload = b"<html>not json</html>"
        try:
            source.fetch_warehouses()
        except WbSuppliesTransportError as exc:
            if "non-JSON" not in str(exc) or "content-type=text/html" not in str(exc) or "body_prefix=<html>not json</html>" not in str(exc):
                raise AssertionError(f"non-JSON error must be controlled, got {exc}")
        else:
            raise AssertionError("non-JSON upstream response must raise transport error")

        opener.next_headers = {}
        opener.next_error = urllib_error.HTTPError(
            "https://supplies-api.example.test/api/v1/supplies",
            504,
            "Gateway Timeout",
            hdrs={"Content-Type": "text/html"},
            fp=BytesIO(b"<html>gateway timeout</html>"),
        )
        try:
            source.list_supplies(limit=20, offset=0)
        except WbSuppliesHttpStatusError as exc:
            if exc.status_code != 504 or "text/html" not in exc.content_type or "gateway timeout" not in exc.body_prefix:
                raise AssertionError(f"HTTP non-JSON body diagnostics must be preserved, got {exc}")
        else:
            raise AssertionError("HTTP 504 must raise WbSuppliesHttpStatusError")

        opener.next_error = None
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
        _restore_env("WB_MARKETPLACE_API_BASE_URL", original_marketplace_base)
        _restore_env("WB_TARIFFS_API_BASE_URL", original_tariffs_base)

    print("wb_supplies_api_adapter_smoke: OK")


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    main()
