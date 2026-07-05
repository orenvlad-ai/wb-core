"""HTTP smoke-check for the WB FBW supplies cache/API/UI contract."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
import time
from urllib import error as urllib_error, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    DEFAULT_WB_SUPPLIES_OVERLAY_OPTIONS_PATH,
    DEFAULT_WB_SUPPLIES_PATH,
    DEFAULT_WB_SUPPLIES_SYNC_PATH,
    DEFAULT_WB_SUPPLIES_TRANSIT_COST_ENRICH_PATH,
    DEFAULT_WB_SUPPLIES_TRANSIT_COST_STATUS_PATH,
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
            {
                "supplyID": 1005,
                "preorderID": 2005,
                "createDate": "2026-06-10T19:00:00+03:00",
                "updatedDate": "2026-06-10T19:00:00+03:00",
                "statusID": 1,
                "boxTypeID": 2,
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
            "1005": {
                "supplyID": 1005,
                "statusID": 1,
                "warehouseID": 888,
                "warehouseName": "Казань",
                "actualWarehouseID": 888,
                "actualWarehouseName": "Казань",
            },
        }
        self.goods = {
            "39265492": [
                {"nmID": 210183919, "quantity": 2500, "acceptedQuantity": 2490},
                {"nmID": 210184534, "quantity": 5000, "acceptedQuantity": 4993},
            ],
            "39265540": [
                {"nmID": 210183919, "quantity": 4250, "acceptedQuantity": 4239},
                {"nmID": 210184534, "quantity": 5000, "acceptedQuantity": 4998},
            ],
            "1001": [{"nmID": 210183919, "quantity": 500, "acceptedQuantity": 480}],
            "1002": [{"nmID": 210183919, "quantity": 100, "acceptedQuantity": 0}],
            "1003": [
                {"nmID": 210183919, "quantity": 150, "acceptedQuantity": 20},
                {"nmID": 210184534, "quantity": 150, "acceptedQuantity": 10},
            ],
        }

    def fetch_warehouses(self):
        return self.warehouse_rows

    def fetch_marketplace_offices(self):
        return [
            {"name": "Коледино", "federalDistrict": "Центральный федеральный округ"},
            {"name": "Электросталь", "federalDistrict": "Центральный федеральный округ"},
        ]

    def fetch_box_tariffs(self, *, tariff_date=None):
        return [
            {"warehouseName": "Казань", "geoName": "Приволжский федеральный округ"},
            {"warehouseName": "Обухово", "geoName": "Северо-Западный федеральный округ"},
        ]

    def list_supplies(self, *, limit=100, offset=0, status_ids=None, dates=None):
        self.list_calls.append({"limit": limit, "offset": offset, "status_ids": status_ids or [], "dates": dates or []})
        rows = self.list_rows
        if status_ids:
            wanted = {int(item) for item in status_ids}
            rows = [row for row in rows if int(row.get("statusID") or 0) in wanted]
        return WbSuppliesListResult(
            rows=rows[offset : offset + limit],
            raw_count=len(rows[offset : offset + limit]),
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
        if key == "1005":
            raise WbSuppliesTransportError("goods unavailable in smoke")
        return self.goods.get(key, [])[offset : offset + limit]

    def fetch_supply_package(self, supply_id):
        return []


class FakeTransitCostSource:
    def __init__(self, amounts: dict[str, float]) -> None:
        self.amounts = {str(key): float(value) for key, value in amounts.items()}
        self.calls: list[list[str]] = []

    def fetch_costs(self, candidates, *, run_id, runtime_dir, fetched_at):
        supply_ids = [str(item.get("supply_id") or "") for item in candidates]
        self.calls.append(supply_ids)
        results = []
        for supply_id in supply_ids:
            amount = self.amounts.get(supply_id)
            if amount is None:
                results.append(
                    {
                        "supply_id": supply_id,
                        "amount": None,
                        "currency": "RUB",
                        "amount_label": "",
                        "is_transit": True,
                        "source": "seller_portal_browser",
                        "evidence_type": "network_json",
                        "confidence": "none",
                        "fetched_at": fetched_at,
                        "status": "not_found",
                        "error": "target row not found",
                        "source_endpoint_path": "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost",
                    }
                )
                continue
            results.append(
                {
                    "supply_id": supply_id,
                    "amount": amount,
                    "currency": "RUB",
                    "amount_label": f"{int(amount):,}".replace(",", " ") + " ₽",
                    "is_transit": True,
                    "source": "seller_portal_browser",
                    "evidence_type": "network_json",
                    "confidence": "high",
                    "fetched_at": fetched_at,
                    "status": "success",
                    "error": "",
                    "source_endpoint_path": "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost",
                }
            )
        return results


def main() -> None:
    with TemporaryDirectory(prefix="wb-supplies-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.ingest_bundle(json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8")), activated_at="2026-06-08T08:00:00Z")
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
            fake_transit_cost_source = FakeTransitCostSource({"1003": 3333.0})
            entrypoint.wb_supplies_block.transit_cost_source = fake_transit_cost_source
            sync_status, sync_payload = _post_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_SYNC_PATH}",
                {"limit": 100, "offset": 0, "enrich_details": True},
            )
            if sync_status != 200 or sync_payload.get("sync", {}).get("upserted_count") != 7:
                raise AssertionError(f"sync latest 100 must upsert fake rows, got {sync_status} {sync_payload}")
            if not any(call["limit"] == 100 and call["offset"] == 0 and call["status_ids"] == [] for call in fake_source.list_calls):
                raise AssertionError(f"sync must call upstream unfiltered latest window, got {fake_source.list_calls}")
            if not any(call["status_ids"] == [1, 2, 3, 4] for call in fake_source.list_calls):
                raise AssertionError(f"sync must call targeted active status refresh, got {fake_source.list_calls}")

            duplicate_status, duplicate_payload = _post_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_SYNC_PATH}",
                {"limit": 100, "offset": 0, "enrich_details": True},
            )
            if duplicate_status != 200 or duplicate_payload.get("meta", {}).get("cached_total_rows") != 7:
                raise AssertionError(f"duplicate sync must not duplicate rows, got {duplicate_status} {duplicate_payload}")
            duplicate_sync = duplicate_payload.get("sync", {})
            if (
                duplicate_sync.get("upserted_count") != 6
                or duplicate_sync.get("unchanged_rows") != 7
                or duplicate_sync.get("enriched") != 5
                or duplicate_sync.get("enriched_active_rows") != 2
                or duplicate_sync.get("refreshed_recent_historical_rows") != 4
                or duplicate_sync.get("failed_enrich") != 1
            ):
                raise AssertionError(f"second incremental sync must refresh active and recent historical rows, got {duplicate_sync}")

            fake_source.list_rows[4]["updatedDate"] = "2026-06-09T15:00:00+03:00"
            fake_source.goods_http_errors = {"1003": 429}
            rate_limited_status, rate_limited_payload = _post_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_SYNC_PATH}",
                {"limit": 100, "offset": 0, "enrich_details": True},
            )
            rate_limited_sync = rate_limited_payload.get("sync", {})
            if (
                rate_limited_status != 200
                or rate_limited_sync.get("upserted_count") != 6
                or rate_limited_sync.get("changed_rows") != 1
                or rate_limited_sync.get("unchanged_rows") != 6
                or rate_limited_sync.get("enriched_active_rows") != 1
                or rate_limited_sync.get("refreshed_recent_historical_rows") != 4
                or rate_limited_sync.get("failed_enrich") != 2
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
            restore_sync = restore_payload.get("sync", {})
            if (
                restore_status != 200
                or restore_sync.get("upserted_count") != 6
                or restore_sync.get("enriched_active_rows") != 2
                or restore_sync.get("refreshed_recent_historical_rows") != 4
                or restore_sync.get("failed_enrich") != 1
            ):
                raise AssertionError(f"restore sync must keep fake cache usable, got {restore_status} {restore_payload}")

            main_status, main_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=main_250&limit=20")
            main_ids = {row["wb_supply_id"] for row in main_payload.get("rows", [])}
            if main_status != 200 or main_ids != {"39265492", "39265540", "1001", "1003"}:
                raise AssertionError(f"main_250 must return numeric >=250 rows only, got {main_status} {main_ids} {main_payload}")
            schema_labels = [item.get("label") for item in main_payload.get("schema", {}).get("columns", [])]
            if "Транзит" not in schema_labels or "Услуги фулфилмента" not in schema_labels or "Стоимость" in schema_labels:
                raise AssertionError(f"WB supplies schema must expose transit/fulfillment labels, got {schema_labels}")
            row_39265492 = next(row for row in main_payload.get("rows", []) if row.get("wb_supply_id") == "39265492")
            if "₽/шт" not in str(row_39265492.get("transit_per_unit_display") or ""):
                raise AssertionError(f"transit column must expose per-unit display, got {row_39265492}")
            if main_payload.get("summary", {}).get("hidden_by_size_filter_count") != 3:
                raise AssertionError("summary must expose rows hidden by size filter")
            if main_payload.get("summary", {}).get("unknown_quantity_count") != 2:
                raise AssertionError("summary must expose unknown quantity rows")

            small_status, small_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=small_lt_250")
            if small_status != 200 or [row["wb_supply_id"] for row in small_payload.get("rows", [])] != ["1002"]:
                raise AssertionError(f"small_lt_250 must return small rows only, got {small_status} {small_payload}")

            all_status, all_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=all&limit=100")
            all_ids = {row["wb_supply_id"] for row in all_payload.get("rows", [])}
            if all_status != 200 or all_ids != {"39265492", "39265540", "1001", "1002", "1003", "1004", "1005"}:
                raise AssertionError(f"all size filter must include unknown quantity rows, got {all_status} {all_payload}")
            all_rows = {row["wb_supply_id"]: row for row in all_payload.get("rows", [])}
            if all_rows["1003"].get("effective_cost_source") != "unknown":
                raise AssertionError(f"unknown transit row must remain unknown before Seller Portal enrichment: {all_rows['1003']}")
            status_options = all_payload.get("filters", {}).get("options", {}).get("statuses", [])
            if [item.get("value") for item in status_options] != [1, 2, 3, 4, 5, 6]:
                raise AssertionError(f"status selector must expose official statuses 1..6, got {status_options}")
            district_options = all_payload.get("filters", {}).get("options", {}).get("districts", [])
            if [item.get("label") for item in district_options] != ["ЦФО", "СЗФО", "ПФО", "УрФО", "Юг+СК", "Сиб+ДВ"]:
                raise AssertionError(f"district presets must expose six WB regional districts, got {district_options}")

            central_status, central_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?district_keys=central&size_filter=all"
            )
            if central_status != 200 or {row["wb_supply_id"] for row in central_payload.get("rows", [])} != {
                "39265540",
                "1001",
                "1002",
                "1003",
            }:
                raise AssertionError(f"central district preset filter must work with size_filter=all, got {central_payload}")
            central_1003 = next(row for row in central_payload.get("rows", []) if row.get("wb_supply_id") == "1003")
            if (
                central_1003.get("warehouse_display") != "Коледино → Казань"
                or central_1003.get("district_source_warehouse_name") != "Коледино"
                or central_1003.get("district_warehouse_name") != "Коледино"
                or central_1003.get("district_key") != "central"
            ):
                raise AssertionError(f"transit route must map by planned warehouse, got {central_1003}")
            northwest_status, northwest_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?district_keys=northwest&size_filter=all"
            )
            if northwest_status != 200 or {row["wb_supply_id"] for row in northwest_payload.get("rows", [])} != {
                "39265492",
            }:
                raise AssertionError(f"northwest district preset must use planned Shushary, not transit Obukhovo, got {northwest_payload}")
            volga_status, volga_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?district_keys=volga&size_filter=all"
            )
            if volga_status != 200 or {row["wb_supply_id"] for row in volga_payload.get("rows", [])} != {"1004", "1005"}:
                raise AssertionError(f"volga district preset filter must not use transit Казань for Коледино rows, got {volga_payload}")

            overlay_status, overlay_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_OVERLAY_OPTIONS_PATH}")
            if overlay_status != 200 or overlay_payload.get("eligible_status_ids") != [2, 3, 4, 6]:
                raise AssertionError(f"overlay options route must expose eligible status contract, got {overlay_status} {overlay_payload}")
            if [item.get("label") for item in overlay_payload.get("district_options", [])] != ["ЦФО", "СЗФО", "ПФО", "УрФО", "Юг+СК", "Сиб+ДВ"]:
                raise AssertionError(f"overlay options route must expose six district presets, got {overlay_payload}")
            overlay_options = {item.get("supply_id"): item for item in overlay_payload.get("options", [])}
            if not overlay_options.get("1002", {}).get("eligible_for_overlay"):
                raise AssertionError(f"planned supply with date/composition/active SKU must be selectable, got {overlay_options.get('1002')}")
            if overlay_options.get("1003", {}).get("district_key") != "central":
                raise AssertionError(f"transit overlay option must use planned warehouse district, got {overlay_options.get('1003')}")
            if "1001" in overlay_options or "1005" in overlay_options:
                raise AssertionError(f"status 1/5 supplies must not be returned to overlay selector, got {overlay_options.keys()}")
            if overlay_payload.get("summary", {}).get("excluded_by_status") != 4:
                raise AssertionError(f"overlay selector must count status-excluded rows, got {overlay_payload.get('summary')}")

            sort_desc_status, sort_desc_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=all&limit=100&sort_key=supply_date&sort_dir=desc"
            )
            sort_desc_ids = [row["wb_supply_id"] for row in sort_desc_payload.get("rows", [])[:4]]
            if sort_desc_status != 200 or sort_desc_ids != ["1004", "1003", "1002", "1001"]:
                raise AssertionError(f"supply_date desc sort must apply before pagination, got {sort_desc_ids}")
            sort_desc_last_ids = [row["wb_supply_id"] for row in sort_desc_payload.get("rows", [])[-2:]]
            if sort_desc_last_ids[-1:] != ["1005"]:
                raise AssertionError(f"no-date rows must stay at bottom for desc sort, got {sort_desc_last_ids}")
            sort_asc_status, sort_asc_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=all&limit=100&sort_key=supply_date&sort_dir=asc"
            )
            sort_asc_ids = [row["wb_supply_id"] for row in sort_asc_payload.get("rows", [])[:2]]
            if sort_asc_status != 200 or sort_asc_ids != ["39265492", "39265540"]:
                raise AssertionError(f"supply_date asc sort must be stable for same date, got {sort_asc_ids}")
            if [row["wb_supply_id"] for row in sort_asc_payload.get("rows", [])[-1:]] != ["1005"]:
                raise AssertionError(f"no-date rows must stay at bottom for asc sort, got {sort_asc_payload}")

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
            multi_status, multi_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?status_ids=2,5&size_filter=all"
            )
            if multi_status != 200 or {row["wb_supply_id"] for row in multi_payload.get("rows", [])} != {
                "39265492",
                "39265540",
                "1001",
                "1002",
            }:
                raise AssertionError(f"multi-status filter must work, got {multi_status} {multi_payload}")
            repeated_status, repeated_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?status_ids=2&status_ids=5&size_filter=all"
            )
            if repeated_status != 200 or {row["wb_supply_id"] for row in repeated_payload.get("rows", [])} != {
                "39265492",
                "39265540",
                "1001",
                "1002",
            }:
                raise AssertionError(f"repeated status_ids filter must work, got {repeated_status} {repeated_payload}")

            search_status, search_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?search=2002&size_filter=all"
            )
            if search_status != 200 or [row["wb_supply_id"] for row in search_payload.get("rows", [])] != ["1002"]:
                raise AssertionError(f"search must match preorderID/visible number, got {search_status} {search_payload}")

            transit_enrich_status, transit_enrich_payload = _post_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_TRANSIT_COST_ENRICH_PATH}",
                {"supply_ids": ["1003", "39265492", "39265540"], "limit": 10, "force": False},
            )
            if (
                transit_enrich_status != 202
                or transit_enrich_payload.get("accepted") is not True
                or transit_enrich_payload.get("candidate_count") != 1
            ):
                raise AssertionError(
                    f"transit cost enrich route must accept exactly missing transit cost candidate, got "
                    f"{transit_enrich_status} {transit_enrich_payload}"
                )
            transit_run = _wait_transit_cost_run(base_url, str(transit_enrich_payload.get("run_id") or ""))
            if transit_run.get("status") != "success" or transit_run.get("success_count") != 1:
                raise AssertionError(f"transit cost fake run must finish successfully, got {transit_run}")
            if fake_transit_cost_source.calls != [["1003"]]:
                raise AssertionError(f"transit cost source must receive only missing unknown transit row, got {fake_transit_cost_source.calls}")
            enriched_status, enriched_payload = _get_json(
                f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?search=1003&size_filter=all"
            )
            enriched_row = (enriched_payload.get("rows") or [{}])[0]
            if (
                enriched_status != 200
                or enriched_row.get("cost_total") is not None
                or enriched_row.get("effective_cost_total") != 3333.0
                or enriched_row.get("effective_cost_source") != "seller_portal_browser"
                or enriched_row.get("seller_portal_transit_cost_display") != "3 333 ₽"
            ):
                raise AssertionError(f"Seller Portal enrichment must fill effective cost only, got {enriched_row}")

            page_status, page_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=all&limit=20&offset=20")
            if page_status != 200 or page_payload.get("pagination", {}).get("offset") != 7:
                raise AssertionError(f"oversized offset must clamp to filtered count, got {page_status} {page_payload}")

            detail_status, detail_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}/1001")
            if detail_status != 200 or detail_payload.get("supply", {}).get("raw", {}).get("detail", {}).get("quantity") != 500:
                raise AssertionError(f"detail route must return cached raw evidence, got {detail_status} {detail_payload}")
            detail_goods = detail_payload.get("goods", [])
            detail_goods_summary = detail_payload.get("goods_summary", {})
            if (
                detail_payload.get("composition_status") != "available"
                or not detail_goods
                or detail_goods_summary.get("total_quantity") != 500
                or "quantity" not in detail_goods[0]
                or "accepted_quantity" not in detail_goods[0]
            ):
                raise AssertionError(f"detail route must return normalized goods composition, got {detail_payload}")
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
                "Статусы: все",
                "Активные",
                "Размер поставки",
                "Основные от 250 шт",
                "Показать записей",
                "Загрузить всю историю",
                "Учесть WB-поставки",
                "Услуги фулфилмента",
                "Транзит",
                "fulfillment_services_template_path",
                "fulfillment_services_uploads_path",
                "Выбрать eligible",
                "ФО",
                "ЦФО",
                "Сиб+ДВ",
                "wb_supplies_path",
                "wb_supplies_overlay_options_path",
                "wb_supplies_backfill_path",
                "wb_supplies_sync_status_path",
                "wb_supplies_transit_cost_enrich_path",
                "wb_supplies_transit_cost_status_path",
            ):
                if operator_status != 200 or expected not in operator_html:
                    raise AssertionError(f"operator HTML must expose WB supplies UI token {expected!r}")
            if "Обновить стоимость транзита" in operator_html:
                raise AssertionError("operator UI must not expose transit-cost refresh as a second primary button")
            if "<th>Стоимость</th>" in operator_html:
                raise AssertionError("operator UI must no longer expose Стоимость as WB supplies header")
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


def _wait_transit_cost_run(base_url: str, run_id: str) -> dict:
    if not run_id:
        raise AssertionError("run_id is required")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, payload = _get_json(
            f"{base_url}{DEFAULT_WB_SUPPLIES_TRANSIT_COST_STATUS_PATH}?run_id={run_id}"
        )
        if status != 200:
            raise AssertionError(f"transit cost status route failed: {status} {payload}")
        run = payload.get("run") or {}
        if run.get("status") not in {"queued", "running"}:
            return run
        time.sleep(0.05)
    raise AssertionError("transit cost run did not finish")


if __name__ == "__main__":
    main()
