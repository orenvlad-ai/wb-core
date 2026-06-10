"""HTTP smoke-check for the WB FBW supplies cache/API/UI contract."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    DEFAULT_WB_SUPPLIES_PATH,
    DEFAULT_WB_SUPPLIES_SYNC_PATH,
    build_registry_upload_http_server,
)
from packages.adapters.wb_supplies import WbSuppliesHttpStatusError, WbSuppliesListResult, WbSuppliesTransportError  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


class MissingTokenSource:
    def fetch_warehouses(self):
        raise RuntimeError("required env WB_API_TOKEN is not set")


class NonJsonListSource:
    def fetch_warehouses(self):
        return []

    def list_supplies(self, *, limit=100, offset=0, status_ids=None, dates=None):
        raise WbSuppliesTransportError(
            "WB supplies API returned non-JSON response",
            status_code=504,
            content_type="text/html",
            body_prefix="<html>gateway timeout</html>",
        )


class FakeWbSuppliesSource:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []
        self.detail_http_errors: dict[str, int] = {}
        self.goods_http_errors: dict[str, int] = {}
        self.warehouse_rows = [
            {"ID": 507, "name": "Коледино"},
            {"ID": 777, "name": "Электросталь"},
            {"ID": 888, "name": "Казань"},
            {"ID": 218210, "name": "Обухово"},
            {"ID": 50045246, "name": "Склад Шушары"},
        ]
        self.list_rows = [
            {
                "supplyID": 39265492,
                "preorderID": 39260001,
                "createDate": "2026-05-14T10:00:00+03:00",
                "supplyDate": "2026-05-15T00:00:00+03:00",
                "factDate": "2026-05-15T13:30:00+03:00",
                "updatedDate": "2026-06-09T14:00:00+03:00",
                "statusID": 5,
                "boxTypeID": 1,
            },
            {
                "supplyID": 39265540,
                "preorderID": 39260002,
                "createDate": "2026-05-14T10:00:00+03:00",
                "supplyDate": "2026-05-15T00:00:00+03:00",
                "factDate": "2026-05-15T13:30:00+03:00",
                "updatedDate": "2026-06-09T13:00:00+03:00",
                "statusID": 5,
                "boxTypeID": 1,
            },
            {
                "supplyID": 1001,
                "preorderID": 2001,
                "createDate": "2026-06-01T10:00:00+03:00",
                "supplyDate": "2026-06-07T00:00:00+03:00",
                "factDate": "2026-06-07T13:30:00+03:00",
                "updatedDate": "2026-06-07T14:00:00+03:00",
                "statusID": 5,
                "boxTypeID": 2,
            },
            {
                "supplyID": 1002,
                "preorderID": 2002,
                "createDate": "2026-06-02T10:00:00+03:00",
                "supplyDate": "2026-06-08T00:00:00+03:00",
                "updatedDate": "2026-06-08T12:00:00+03:00",
                "statusID": 2,
                "boxTypeID": 5,
            },
            {
                "supplyID": 1003,
                "preorderID": 2003,
                "createDate": "2026-06-03T10:00:00+03:00",
                "supplyDate": "2026-06-09T00:00:00+03:00",
                "updatedDate": "2026-06-09T12:00:00+03:00",
                "statusID": 3,
                "boxTypeID": 1,
            },
            {
                "supplyID": 1004,
                "preorderID": 2004,
                "createDate": "2026-06-04T10:00:00+03:00",
                "supplyDate": "2026-06-10T00:00:00+03:00",
                "updatedDate": "2026-06-10T12:00:00+03:00",
                "statusID": 6,
            },
        ]
        self.details = {
            "39265492": {
                "supplyID": 39265492,
                "statusID": 5,
                "warehouseID": 50045246,
                "warehouseName": "Склад Шушары",
                "actualWarehouseID": 218210,
                "actualWarehouseName": "Обухово",
                "transitWarehouseID": 218210,
                "transitWarehouseName": "Обухово",
                "quantity": 7500,
                "acceptedQuantity": 7483,
                "acceptanceCost": 0,
                "transitCost": 11543.52,
                "paidAcceptanceCoefficient": 0,
                "boxTypeID": 1,
            },
            "39265540": {
                "supplyID": 39265540,
                "statusID": 5,
                "warehouseID": 777,
                "warehouseName": "Электросталь",
                "actualWarehouseID": 777,
                "actualWarehouseName": "Электросталь",
                "quantity": 9250,
                "acceptedQuantity": 9237,
                "acceptanceCost": 0,
                "paidAcceptanceCoefficient": 0,
                "boxTypeID": 1,
            },
            "1001": {
                "supplyID": 1001,
                "statusID": 5,
                "warehouseID": 507,
                "warehouseName": "Коледино",
                "actualWarehouseID": 507,
                "actualWarehouseName": "Коледино",
                "quantity": 500,
                "acceptedQuantity": 480,
                "unloadingQuantity": 500,
                "acceptanceCost": 0,
                "paidAcceptanceCoefficient": 0,
                "isBoxOnPallet": True,
            },
            "1002": {
                "supplyID": 1002,
                "statusID": 2,
                "warehouseID": 777,
                "warehouseName": "Электросталь",
                "actualWarehouseID": 777,
                "actualWarehouseName": "Электросталь",
                "quantity": 100,
                "acceptedQuantity": 0,
                "acceptanceCost": 15523.72,
                "paidAcceptanceCoefficient": 10,
            },
            "1003": {
                "supplyID": 1003,
                "statusID": 3,
                "warehouseID": 507,
                "warehouseName": "Коледино",
                "actualWarehouseID": 888,
                "actualWarehouseName": "Казань",
                "transitWarehouseID": 888,
                "transitWarehouseName": "Казань",
                "acceptanceCost": None,
                "paidAcceptanceCoefficient": None,
            },
            "1004": {
                "supplyID": 1004,
                "statusID": 6,
                "warehouseID": 888,
                "warehouseName": "Казань",
                "actualWarehouseID": 888,
                "actualWarehouseName": "Казань",
            },
        }
        self.goods = {
            "39265492": [
                {"quantity": 2500, "acceptedQuantity": 2490},
                {"quantity": 5000, "acceptedQuantity": 4993},
            ],
            "39265540": [
                {"quantity": 4250, "acceptedQuantity": 4239},
                {"quantity": 5000, "acceptedQuantity": 4998},
            ],
            "1001": [{"quantity": 500, "acceptedQuantity": 480}],
            "1002": [{"quantity": 100, "acceptedQuantity": 0}],
            "1003": [
                {"quantity": 150, "acceptedQuantity": 20},
                {"quantity": 150, "acceptedQuantity": 10},
            ],
        }

    def fetch_warehouses(self):
        return self.warehouse_rows

    def list_supplies(self, *, limit=100, offset=0, status_ids=None, dates=None):
        self.list_calls.append({"limit": limit, "offset": offset, "status_ids": status_ids or [], "dates": dates or []})
        return WbSuppliesListResult(
            rows=self.list_rows[offset : offset + limit],
            raw_count=len(self.list_rows[offset : offset + limit]),
            limit=limit,
            offset=offset,
            status_ids=list(status_ids or []),
            dates=list(dates or []),
        )

    def fetch_supply_details(self, supply_id, *, is_preorder_id=False):
        key = str(supply_id)
        if key in self.detail_http_errors:
            raise WbSuppliesHttpStatusError(self.detail_http_errors[key], "{}")
        return self.details[key]

    def fetch_supply_goods(self, supply_id, *, limit=1000, offset=0, is_preorder_id=False):
        key = str(supply_id)
        if key in self.goods_http_errors:
            raise WbSuppliesHttpStatusError(self.goods_http_errors[key], "{}")
        if key == "1004":
            raise WbSuppliesTransportError("goods unavailable in smoke")
        return self.goods.get(key, [])[offset : offset + limit]

    def fetch_supply_package(self, supply_id):
        return []


def main() -> None:
    with TemporaryDirectory(prefix="wb-supplies-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        port = _reserve_free_port()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-06-08T08:00:00Z",
        )
        cfg = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(cfg, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{cfg.port}"

            empty_status, empty_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}")
            if empty_status != 200 or empty_payload.get("contract_name") != "sheet_vitrina_v1_wb_supplies":
                raise AssertionError(f"empty cache route must return contract JSON, got {empty_status} {empty_payload}")
            if empty_payload.get("meta", {}).get("cache_empty") is not True or empty_payload.get("rows") != []:
                raise AssertionError(f"empty cache route must be controlled, got {empty_payload}")
            if empty_payload.get("filters", {}).get("current", {}).get("size_filter") != "main_250":
                raise AssertionError("default size filter must be main_250")

            entrypoint.wb_supplies_block.source = MissingTokenSource()
            token_status, token_payload = _post_json(f"{base_url}{DEFAULT_WB_SUPPLIES_SYNC_PATH}", {"limit": 100})
            if token_status != 503 or "WB_API_TOKEN" not in str(token_payload.get("error", "")):
                raise AssertionError(f"missing token must be controlled 503 JSON, got {token_status} {token_payload}")

            entrypoint.wb_supplies_block.source = NonJsonListSource()
            non_json_status, non_json_payload = _post_json(f"{base_url}{DEFAULT_WB_SUPPLIES_SYNC_PATH}", {"limit": 100})
            if (
                non_json_status != 502
                or "non-JSON" not in str(non_json_payload.get("error", ""))
                or "content-type=text/html" not in str(non_json_payload.get("error", ""))
                or "body_prefix=<html>gateway timeout</html>" not in str(non_json_payload.get("error", ""))
            ):
                raise AssertionError(f"upstream non-JSON must return controlled JSON error, got {non_json_status} {non_json_payload}")

            fake_source = FakeWbSuppliesSource()
            entrypoint.wb_supplies_block.source = fake_source
            sync_status, sync_payload = _post_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_SYNC_PATH}",
                {"limit": 100, "offset": 0, "enrich_details": True},
            )
            if sync_status != 200 or sync_payload.get("sync", {}).get("upserted_count") != 6:
                raise AssertionError(f"sync latest 100 must upsert fake rows, got {sync_status} {sync_payload}")
            if fake_source.list_calls[-1]["limit"] != 100 or fake_source.list_calls[-1]["offset"] != 0:
                raise AssertionError(f"sync must call upstream with limit/offset, got {fake_source.list_calls[-1]}")

            duplicate_status, duplicate_payload = _post_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_SYNC_PATH}",
                {"limit": 100, "offset": 0, "enrich_details": True},
            )
            if duplicate_status != 200 or duplicate_payload.get("meta", {}).get("cached_total_rows") != 6:
                raise AssertionError(f"duplicate sync must not duplicate rows, got {duplicate_status} {duplicate_payload}")
            duplicate_sync = duplicate_payload.get("sync", {})
            if (
                duplicate_sync.get("upserted_count") != 0
                or duplicate_sync.get("unchanged_rows") != 6
                or duplicate_sync.get("enriched") != 0
            ):
                raise AssertionError(f"second incremental sync must skip unchanged enrichment, got {duplicate_sync}")

            fake_source.list_rows[4]["updatedDate"] = "2026-06-09T15:00:00+03:00"
            fake_source.goods_http_errors = {"1003": 429}
            rate_limited_status, rate_limited_payload = _post_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_SYNC_PATH}",
                {"limit": 100, "offset": 0, "enrich_details": True},
            )
            rate_limited_sync = rate_limited_payload.get("sync", {})
            if (
                rate_limited_status != 200
                or rate_limited_sync.get("upserted_count") != 1
                or rate_limited_sync.get("changed_rows") != 1
                or rate_limited_sync.get("unchanged_rows") != 5
                or rate_limited_sync.get("failed_enrich") != 1
            ):
                raise AssertionError(
                    f"detail/goods 429 must not fail list sync, got {rate_limited_status} {rate_limited_payload}"
                )
            rate_limited_detail_status, rate_limited_detail_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}/1003")
            rate_limited_warnings = rate_limited_detail_payload.get("supply", {}).get("warnings", [])
            if rate_limited_detail_status != 200 or "goods fetch failed for 1003: status 429" not in rate_limited_warnings:
                raise AssertionError(f"goods 429 warning must be cached on the row, got {rate_limited_detail_payload}")
            fake_source.goods_http_errors = {}
            restore_status, restore_payload = _post_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_SYNC_PATH}",
                {"limit": 100, "offset": 0, "enrich_details": True},
            )
            if restore_status != 200 or restore_payload.get("sync", {}).get("upserted_count") != 0:
                raise AssertionError(f"restore sync must keep fake cache usable, got {restore_status} {restore_payload}")

            main_status, main_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=main_250&limit=20")
            main_ids = {row["wb_supply_id"] for row in main_payload.get("rows", [])}
            if main_status != 200 or main_ids != {"39265492", "39265540", "1001", "1003"}:
                raise AssertionError(f"main_250 must return >=250 rows only, got {main_status} {main_ids} {main_payload}")
            if main_payload.get("summary", {}).get("hidden_by_size_filter_count") != 2:
                raise AssertionError("summary must expose rows hidden by size filter")
            if main_payload.get("summary", {}).get("unknown_quantity_count") != 1:
                raise AssertionError("summary must expose unknown quantity rows")

            small_status, small_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=small_lt_250")
            if small_status != 200 or [row["wb_supply_id"] for row in small_payload.get("rows", [])] != ["1002"]:
                raise AssertionError(f"small_lt_250 must return small rows only, got {small_status} {small_payload}")

            all_status, all_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=all&limit=100")
            all_ids = {row["wb_supply_id"] for row in all_payload.get("rows", [])}
            if all_status != 200 or all_ids != {"39265492", "39265540", "1001", "1002", "1003", "1004"}:
                raise AssertionError(f"all size filter must include unknown quantity rows, got {all_status} {all_payload}")
            status_options = all_payload.get("filters", {}).get("options", {}).get("statuses", [])
            if [item.get("value") for item in status_options] != [1, 2, 3, 4, 5, 6]:
                raise AssertionError(f"status selector must expose official statuses 1..6, got {status_options}")

            sort_desc_status, sort_desc_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=all&limit=100&sort_key=supply_date&sort_dir=desc"
            )
            sort_desc_ids = [row["wb_supply_id"] for row in sort_desc_payload.get("rows", [])[:4]]
            if sort_desc_status != 200 or sort_desc_ids != ["1004", "1003", "1002", "1001"]:
                raise AssertionError(f"supply_date desc sort must apply before pagination, got {sort_desc_ids}")
            sort_asc_status, sort_asc_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=all&limit=100&sort_key=supply_date&sort_dir=asc"
            )
            sort_asc_ids = [row["wb_supply_id"] for row in sort_asc_payload.get("rows", [])[:2]]
            if sort_asc_status != 200 or sort_asc_ids != ["39265492", "39265540"]:
                raise AssertionError(f"supply_date asc sort must be stable for same date, got {sort_asc_ids}")

            warehouse_status, warehouse_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?warehouse_id=507&size_filter=all"
            )
            if warehouse_status != 200 or {row["wb_supply_id"] for row in warehouse_payload.get("rows", [])} != {"1001", "1003"}:
                raise AssertionError(f"warehouse filter must match cached warehouse fields, got {warehouse_payload}")

            status_status, status_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?status_id=5&size_filter=all"
            )
            if status_status != 200 or {row["wb_supply_id"] for row in status_payload.get("rows", [])} != {
                "39265492",
                "39265540",
                "1001",
            }:
                raise AssertionError(f"status filter must work, got {status_status} {status_payload}")

            search_status, search_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?search=2002&size_filter=all"
            )
            if search_status != 200 or [row["wb_supply_id"] for row in search_payload.get("rows", [])] != ["1002"]:
                raise AssertionError(f"search must match preorderID/visible number, got {search_status} {search_payload}")

            page_status, page_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=all&limit=20&offset=20")
            if page_status != 200 or page_payload.get("pagination", {}).get("offset") != 6:
                raise AssertionError(f"oversized offset must clamp to filtered count, got {page_status} {page_payload}")

            detail_status, detail_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}/1001")
            if detail_status != 200 or detail_payload.get("supply", {}).get("raw", {}).get("detail", {}).get("quantity") != 500:
                raise AssertionError(f"detail route must return cached raw evidence, got {detail_status} {detail_payload}")
            transit_detail_status, transit_detail_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}/39265492")
            transit_supply = transit_detail_payload.get("supply", {})
            if (
                transit_detail_status != 200
                or transit_supply.get("warehouse_display") != "Склад Шушары → Обухово"
                or transit_supply.get("warehouse_fact_line") != ""
                or transit_supply.get("quantity_added") != 7500
                or transit_supply.get("packed_quantity") != 7500
                or transit_supply.get("accepted_quantity") != 7483
                or transit_supply.get("cost_total") != 11543.52
                or "boxTypeID" in str(transit_supply.get("type_label") or "")
            ):
                raise AssertionError(f"39265492 fixture must normalize like WB cabinet, got {transit_supply}")
            simple_detail_status, simple_detail_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}/39265540")
            simple_supply = simple_detail_payload.get("supply", {})
            if (
                simple_detail_status != 200
                or simple_supply.get("warehouse_display") != "Электросталь"
                or simple_supply.get("quantity_added") != 9250
                or simple_supply.get("packed_quantity") != 9250
                or simple_supply.get("accepted_quantity") != 9237
                or simple_supply.get("cost_total") != 0
            ):
                raise AssertionError(f"39265540 fixture must keep warehouse/quantities/cost, got {simple_supply}")

            operator_status, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}?embedded_tab=factory-order")
            for expected in (
                "Wildberries",
                "Все поставки",
                "Read-only список поставок WB API / FBW Supplies",
                "Номер поставки",
                "Все склады",
                "Все статусы",
                "Размер поставки",
                "Основные от 250 шт",
                "Показать записей",
                "Загрузить всю историю",
                "wb_supplies_path",
                "wb_supplies_backfill_path",
                "wb_supplies_sync_status_path",
            ):
                if operator_status != 200 or expected not in operator_html:
                    raise AssertionError(f"operator HTML must expose WB supplies UI token {expected!r}")
        finally:
            server.shutdown()
            thread.join(timeout=5)

    print("sheet_vitrina_v1_wb_supplies_http_smoke: OK")


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str) -> tuple[int, dict]:
    req = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
    return _open_json(req)


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    return _open_json(req)


def _open_json(req) -> tuple[int, dict]:
    try:
        with urllib_request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=20) as response:
            return response.status, response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


if __name__ == "__main__":
    main()
