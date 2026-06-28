"""Targeted smoke-check for WB regional supply planning assistant."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_supplies import WbSuppliesHttpStatusError, WbSuppliesTransportError  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.wb_regional_supply_planning import WbRegionalSupplyPlanningBlock  # noqa: E402
from packages.contracts.supplier_shipments import (  # noqa: E402
    NOMENCLATURE_BARCODE_SOURCE_MANUAL,
    NOMENCLATURE_BARCODE_STATUS_MANUAL,
)
from packages.contracts.wb_regional_supply import (  # noqa: E402
    DISTRICT_CENTRAL,
    DISTRICT_NORTHWEST,
)


ACTIVATED_AT = "2026-06-28T08:00:00Z"
MAIN_NM_ID = 210183919
SECOND_NM_ID = 123456789


class FakePlanningSource:
    def __init__(self) -> None:
        self.acceptance_requests: list[dict[str, object]] = []
        self.fail_acceptance: Exception | None = None
        self.fail_box_tariffs = False
        self.acceptance_payload: dict[str, object] = {
            "result": [
                {
                    "barcode": "4600000000001",
                    "warehouses": [
                        {"warehouseID": 101, "warehouseName": "Коледино", "canBox": True},
                        {"warehouseID": 202, "warehouseName": "Склад Шушары", "canBox": True},
                        {
                            "warehouseID": 303,
                            "warehouseName": "Электросталь",
                            "transitWarehouseName": "Обухово",
                            "isTransit": True,
                        },
                    ],
                },
                {
                    "barcode": "4600000000002",
                    "errors": [{"message": "barcode is temporarily unavailable"}],
                },
            ]
        }

    def fetch_acceptance_options(self, *, products, warehouse_id=None):
        self.acceptance_requests.append({"products": list(products), "warehouse_id": warehouse_id})
        if self.fail_acceptance is not None:
            raise self.fail_acceptance
        return self.acceptance_payload

    def fetch_warehouses(self):
        return [
            {"warehouseID": 101, "warehouseName": "Коледино"},
            {"warehouseID": 202, "warehouseName": "Склад Шушары"},
            {"warehouseID": 303, "warehouseName": "Электросталь"},
        ]

    def fetch_marketplace_offices(self):
        return [
            {"name": "Коледино", "federalDistrict": "Центральный федеральный округ"},
            {"name": "Электросталь", "federalDistrict": "Центральный федеральный округ"},
            {"name": "Склад Шушары", "federalDistrict": "Северо-Западный федеральный округ"},
        ]

    def fetch_box_tariffs(self, *, tariff_date=None):
        if self.fail_box_tariffs:
            raise WbSuppliesTransportError("box tariffs fixture failure")
        return [
            {"warehouseName": "Коледино", "geoName": "Центральный федеральный округ", "boxDeliveryBase": "5"},
            {"warehouseName": "Электросталь", "geoName": "Центральный федеральный округ", "boxDeliveryBase": "7"},
            {"warehouseName": "Склад Шушары", "geoName": "Северо-Западный федеральный округ", "boxDeliveryBase": "1"},
        ]

    def fetch_transit_tariffs(self):
        return [
            {
                "transitWarehouseName": "Обухово",
                "destinationWarehouseName": "Электросталь",
                "boxTariff": [{"value": "2"}],
            }
        ]

    def fetch_acceptance_coefficients(self, *, warehouse_ids=None):
        return [
            {"warehouseID": 101, "warehouseName": "Коледино", "date": "2026-07-01", "coefficient": 1, "allowUnload": True},
            {"warehouseID": 202, "warehouseName": "Склад Шушары", "date": "2026-07-01", "coefficient": 0, "allowUnload": True},
            {"warehouseID": 303, "warehouseName": "Электросталь", "date": "2026-07-02", "coefficient": 1, "allowUnload": True},
        ]


def main() -> None:
    with TemporaryDirectory(prefix="wb-regional-planning-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_last_result(runtime)
        _seed_nomenclature(runtime, include_second=True)
        source = FakePlanningSource()
        block = WbRegionalSupplyPlanningBlock(
            runtime=runtime,
            source=source,
            timestamp_factory=lambda: ACTIVATED_AT,
        )

        payload = block.build_options({"district_key": DISTRICT_CENTRAL})
        if payload.get("status") != "ready":
            raise AssertionError(f"happy path must be ready, got {payload}")
        if len(source.acceptance_requests) != 1:
            raise AssertionError("happy path must call acceptance/options exactly once")
        request_products = source.acceptance_requests[0]["products"]
        if request_products != [{"barcode": "4600000000001", "quantity": 50}, {"barcode": "4600000000002", "quantity": 25}]:
            raise AssertionError(f"acceptance/options request must use barcode+quantity, got {request_products}")
        options = payload.get("options") or []
        if [item["warehouse_name"] for item in options[:3]] != ["Коледино", "Электросталь", "Склад Шушары"]:
            raise AssertionError(f"same-district/direct ranking changed unexpectedly: {options}")
        if options[0]["warehouse_scope"] != "same_district" or options[2]["warehouse_scope"] != "outside_district":
            raise AssertionError(f"warehouse scope enrichment changed: {options}")
        if options[1]["route_type"] != "transit" or not options[1]["transit_warehouse_name"]:
            raise AssertionError(f"transit option must stay visible: {options[1]}")
        if not options[0].get("operator_handoff", {}).get("products"):
            raise AssertionError("planning option must expose manual operator handoff payload")
        if not any("Second SKU" in warning and "temporarily unavailable" in warning for warning in payload.get("warnings", [])):
            raise AssertionError(f"mixed barcode-level errors must be visible as warnings, got {payload.get('warnings')}")
        acceptance_evidence = payload.get("evidence", {}).get("acceptance_options", {})
        if acceptance_evidence.get("http_status") != 200 or acceptance_evidence.get("request_shape") != "json_array":
            raise AssertionError(f"successful acceptance/options evidence must expose official request shape, got {acceptance_evidence}")

        source.fail_box_tariffs = True
        partial = block.build_options({"district_key": DISTRICT_CENTRAL})
        if partial.get("status") != "ready" or not any("box tariffs" in warning for warning in partial.get("warnings", [])):
            raise AssertionError(f"partial tariff enrichment must warn without failing: {partial}")
        source.fail_box_tariffs = False

        _seed_last_result(runtime, allocated_second=0)
        empty = block.build_options({"district_key": DISTRICT_NORTHWEST})
        if empty.get("status") != "empty":
            raise AssertionError(f"empty district must be controlled, got {empty}")

        _seed_last_result(runtime)
        _seed_nomenclature(runtime, include_second=False)
        missing = block.build_options({"district_key": DISTRICT_CENTRAL})
        if missing.get("status") != "blocked" or not missing.get("blockers"):
            raise AssertionError(f"missing barcode must block safely, got {missing}")
        if len(source.acceptance_requests) != 2:
            raise AssertionError("missing barcode path must not call acceptance/options")

        _seed_nomenclature(runtime, include_second=True)
        source.acceptance_payload = {
            "result": [
                {"barcode": "4600000000001", "warehouses": []},
                {"barcode": "4600000000002", "error": "no warehouses available"},
            ]
        }
        no_options = block.build_options({"district_key": DISTRICT_CENTRAL})
        if no_options.get("status") != "no_options" or no_options.get("options"):
            raise AssertionError(f"no warehouses must be controlled no-options, got {no_options}")
        if not no_options.get("blockers") or not any("no warehouses available" in item.get("message", "") for item in no_options.get("blockers", [])):
            raise AssertionError(f"no-options barcode errors must be exposed as blockers, got {no_options.get('blockers')}")

        source.fail_acceptance = WbSuppliesHttpStatusError(429, '{"token":"secret-token"}')
        rate_limited = block.build_options({"district_key": DISTRICT_CENTRAL})
        if rate_limited.get("status") != "upstream_error":
            raise AssertionError(f"upstream errors must be controlled, got {rate_limited}")
        serialized = json.dumps(rate_limited, ensure_ascii=False)
        if "secret-token" in serialized:
            raise AssertionError("upstream error payload must not leak body/token values")

        source.fail_acceptance = WbSuppliesHttpStatusError(
            400,
            '{"error":"bad request","token":"secret-token","barcode":"4600000000001"}',
        )
        bad_request = block.build_options({"district_key": DISTRICT_CENTRAL})
        if bad_request.get("status") != "upstream_error":
            raise AssertionError(f"HTTP 400 errors must be controlled, got {bad_request}")
        diagnostics = bad_request.get("blockers", [{}])[0].get("diagnostics", {})
        serialized_diagnostics = json.dumps(diagnostics, ensure_ascii=False)
        if "secret-token" in serialized_diagnostics or "4600000000001" in serialized_diagnostics:
            raise AssertionError(f"HTTP 400 diagnostics must not leak secrets or full barcodes, got {diagnostics}")
        if (
            diagnostics.get("http_status") != 400
            or diagnostics.get("request_shape") != "json_array"
            or diagnostics.get("product_count") != 2
            or "wb_body_prefix" not in diagnostics
        ):
            raise AssertionError(f"HTTP 400 diagnostics must include safe request evidence, got {diagnostics}")

    print("wb_regional_supply_planning_smoke: OK")


def _seed_last_result(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    allocated_second: int = 25,
) -> None:
    payload = {
        "status": "success",
        "calculation_id": "calc-planning-smoke",
        "calculated_at": ACTIVATED_AT,
        "report_date": "2026-06-28",
        "horizon_days": 7,
        "active_sku_count": 2,
        "settings": {"included_district_keys": [DISTRICT_CENTRAL, DISTRICT_NORTHWEST]},
        "summary": {"total_qty": 50 + allocated_second, "estimated_weight": 0, "estimated_volume": 0},
        "districts": [
            {
                "district_key": DISTRICT_CENTRAL,
                "district_name_ru": "Центральный федеральный округ",
                "total_qty": 50 + allocated_second,
                "deficit_qty": 0,
                "rows": [
                    {"nm_id": MAIN_NM_ID, "sku_comment": "Main SKU", "allocated_qty": 50, "deficit_qty": 0},
                    {"nm_id": SECOND_NM_ID, "sku_comment": "Second SKU", "allocated_qty": allocated_second, "deficit_qty": 0},
                ],
            },
            {
                "district_key": DISTRICT_NORTHWEST,
                "district_name_ru": "Северо-Западный федеральный округ",
                "total_qty": 0,
                "deficit_qty": 0,
                "rows": [
                    {"nm_id": MAIN_NM_ID, "sku_comment": "Main SKU", "allocated_qty": 0, "deficit_qty": 0},
                ],
            },
        ],
    }
    runtime.save_wb_regional_supply_result_state(calculated_at=ACTIVATED_AT, payload=payload)


def _seed_nomenclature(runtime: RegistryUploadDbBackedRuntime, *, include_second: bool) -> None:
    runtime.save_nomenclature_items_atomic(
        [
            _nomenclature_item(MAIN_NM_ID, "Main SKU", "4600000000001"),
            *([_nomenclature_item(SECOND_NM_ID, "Second SKU", "4600000000002")] if include_second else []),
        ]
    )
    if not include_second and runtime.load_nomenclature_item(f"item-{SECOND_NM_ID}") is not None:
        runtime.delete_nomenclature_item(f"item-{SECOND_NM_ID}", updated_at=ACTIVATED_AT)


def _nomenclature_item(nm_id: int, name: str, barcode: str) -> dict[str, object]:
    return {
        "item_id": f"item-{nm_id}",
        "is_active": True,
        "nm_id": nm_id,
        "barcode": barcode,
        "barcodes": [barcode],
        "barcode_source": NOMENCLATURE_BARCODE_SOURCE_MANUAL,
        "barcode_status": NOMENCLATURE_BARCODE_STATUS_MANUAL,
        "barcode_updated_at": ACTIVATED_AT,
        "nomenclature_name": name,
        "product_type": "clear",
        "match_key": f"match-{nm_id}",
        "created_at": ACTIVATED_AT,
        "updated_at": ACTIVATED_AT,
    }


if __name__ == "__main__":
    main()
