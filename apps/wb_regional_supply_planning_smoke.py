"""Contract smoke for fail-closed WB storage-warehouse planning."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_supplies import WbSuppliesHttpStatusError  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.wb_regional_supply_planning import WbRegionalSupplyPlanningBlock  # noqa: E402
from packages.contracts.supplier_shipments import (  # noqa: E402
    NOMENCLATURE_BARCODE_SOURCE_MANUAL,
    NOMENCLATURE_BARCODE_STATUS_MANUAL,
)
from packages.contracts.wb_supply_planning_zones import (  # noqa: E402
    PLANNING_ZONE_CENTRAL_EAST,
    PLANNING_ZONE_CENTRAL_NORTH,
    PLANNING_ZONE_CENTRAL_SOUTH,
    SUPPLY_PLANNING_ZONE_KEYS,
    resolve_central_storage_warehouse,
    warehouse_name_exclusion_codes,
)
from packages.contracts.wb_regional_supply import DISTRICT_NORTHWEST  # noqa: E402


ACTIVATED_AT = "2026-07-19T08:00:00Z"
MAIN_NM_ID = 210183919
SECOND_NM_ID = 123456789
FIXTURE_PATH = (
    ROOT
    / "artifacts"
    / "wb_regional_supply_planning"
    / "fixtures"
    / "central_storage_contract.json"
)


class FixturePlanningSource:
    def __init__(self, fixture: dict[str, object]) -> None:
        self.fixture = deepcopy(fixture)
        self.acceptance_payload = deepcopy(self.fixture["acceptance_options"])
        self.coefficients_payload = deepcopy(self.fixture["coefficients"])
        self.warehouses_payload = deepcopy(self.fixture["warehouses"])
        self.acceptance_requests: list[dict[str, object]] = []
        self.fail_acceptance: Exception | None = None

    def fetch_acceptance_options(self, *, products, warehouse_id=None):
        self.acceptance_requests.append(
            {"products": deepcopy(list(products)), "warehouse_id": warehouse_id}
        )
        if self.fail_acceptance is not None:
            raise self.fail_acceptance
        if warehouse_id not in (None, ""):
            probes = dict(self.fixture.get("warehouse_probes") or {})
            return deepcopy(probes.get(str(warehouse_id), {"result": []}))
        return deepcopy(self.acceptance_payload)

    def fetch_warehouses(self):
        return deepcopy(self.warehouses_payload)

    def fetch_marketplace_offices(self):
        return []

    def fetch_box_tariffs(self, *, tariff_date=None):
        del tariff_date
        return deepcopy(self.fixture.get("box_tariffs") or [])

    def fetch_transit_tariffs(self):
        return [
            {
                "transitWarehouseName": "СЦ Обухово",
                "destinationWarehouseName": "Коледино",
                "boxTariff": [{"value": 1}],
            }
        ]

    def fetch_acceptance_coefficients(self, *, warehouse_ids=None):
        del warehouse_ids
        return deepcopy(self.coefficients_payload)


def main() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    _assert_registry_identity_contract()
    with TemporaryDirectory(prefix="wb-regional-planning-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_last_result(runtime)
        _seed_nomenclature(runtime, include_second=True)
        source = FixturePlanningSource(fixture)
        block = WbRegionalSupplyPlanningBlock(
            runtime=runtime,
            source=source,
            timestamp_factory=lambda: ACTIVATED_AT,
        )

        south = block.build_options(
            {"planning_zone_key": PLANNING_ZONE_CENTRAL_SOUTH, "package_type": "box"}
        )
        _assert_south_manager_view(south, source)

        east = block.build_options({"district_key": PLANNING_ZONE_CENTRAL_EAST})
        if [item.get("warehouse_name") for item in east.get("options") or []] != [
            "Владимир Воршинское",
            "Рязань (Тюшевское)",
        ]:
            raise AssertionError(f"east must expose only enabled storage warehouses: {east}")
        if any(
            item.get("warehouse_name") in {"Электросталь", "Котовск"}
            for item in east.get("options") or []
        ):
            raise AssertionError("blocked historical warehouses leaked into manager view")

        _assert_exact_tver_probe(block, source)
        _assert_operator_exclusions(block, source, fixture)
        _assert_available_reserve_outranks_unavailable_primary(block, source, fixture)
        _assert_conflicting_can_box_fails_closed(block, source, fixture)
        _assert_missing_storage_does_not_reallocate(block, source, runtime, fixture)
        _assert_one_sku_request(block, source, runtime)
        _assert_controlled_failures(block, source, runtime)

    print("wb_regional_supply_planning_smoke: OK")


def _assert_registry_identity_contract() -> None:
    expected = {
        301806: PLANNING_ZONE_CENTRAL_NORTH,
        301981: PLANNING_ZONE_CENTRAL_EAST,
        301760: PLANNING_ZONE_CENTRAL_EAST,
        120762: PLANNING_ZONE_CENTRAL_EAST,
        301809: PLANNING_ZONE_CENTRAL_EAST,
        507: PLANNING_ZONE_CENTRAL_SOUTH,
        206348: PLANNING_ZONE_CENTRAL_SOUTH,
        301808: PLANNING_ZONE_CENTRAL_SOUTH,
    }
    for warehouse_id, zone_key in expected.items():
        item, source = resolve_central_storage_warehouse(
            warehouse_id=warehouse_id,
            warehouse_name="deliberately wrong",
            historical=False,
        )
        if item is None or item.planning_zone_key != zone_key or source != "warehouse_id":
            raise AssertionError(f"warehouseID must be primary identity: {warehouse_id}, {item}, {source}")
    for unsafe_name in (
        "Электросталь: Питание",
        "Коледино: Горючее",
        "СЦ Тверь",
        "СЦ: Тверь",
        "Неизвестный склад ЦФО",
    ):
        item, _ = resolve_central_storage_warehouse(
            warehouse_id=None,
            warehouse_name=unsafe_name,
            historical=True,
        )
        if item is not None:
            raise AssertionError(f"unsafe/fuzzy historical name must stay unmapped: {unsafe_name}")
    if "central_west" in SUPPLY_PLANNING_ZONE_KEYS:
        raise AssertionError("ЦФО Запад must not exist in the planning contract")
    for sorting_center_name in ("СЦ Тверь", "СЦ: Тверь"):
        if "sorting_center_name" not in warehouse_name_exclusion_codes(sorting_center_name):
            raise AssertionError(f"sorting-center prefix must be an exact exclusion: {sorting_center_name}")


def _assert_south_manager_view(
    payload: dict[str, object], source: FixturePlanningSource
) -> None:
    if payload.get("status") != "ready":
        raise AssertionError(f"south happy path must be ready: {payload}")
    options = list(payload.get("options") or [])
    if [item.get("warehouse_name") for item in options] != ["Коледино", "Тула", "Воронеж"]:
        raise AssertionError(f"manager view must contain only south storage warehouses: {options}")
    forbidden_tokens = (
        "СЦ",
        "СГТ",
        "Питание",
        "Горючее",
        "Шины",
        "Электросталь",
        "Котовск",
    )
    serialized_options = json.dumps(options, ensure_ascii=False)
    if any(token in serialized_options for token in forbidden_tokens):
        raise AssertionError(f"excluded warehouses leaked into manager options: {serialized_options}")
    if any(
        not item.get("accepts_all_barcodes")
        or not item.get("package_supported")
        or not item.get("is_storage_warehouse")
        or item.get("is_sorting_center")
        or not item.get("direct_destination")
        for item in options
    ):
        raise AssertionError(f"manager options violate the fail-closed contract: {options}")
    first = options[0]
    dates = list(first.get("dates") or [])
    if first.get("first_available_date") != "2026-07-20":
        raise AssertionError(f"nearest available date must be chronological: {first}")
    if first.get("first_free_date") != "2026-07-20":
        raise AssertionError(f"nearest free date must be calculated separately: {first}")
    if first.get("unique_available_date_count") != 3 or first.get("unique_free_date_count") != 2:
        raise AssertionError(f"unique day counters are wrong: {first}")
    if len({item.get("date") for item in dates}) != len(dates):
        raise AssertionError(f"calendar dates must be deduplicated: {dates}")
    unavailable = {item.get("date"): item for item in dates}
    if unavailable["2026-07-18"].get("is_available") or unavailable["2026-07-18"].get("is_free_date"):
        raise AssertionError("allowUnload=false date must be unavailable and non-free")
    if "2026-07-19" in unavailable:
        raise AssertionError("monopallet boxTypeID=5 must not be mixed into box dates")
    if unavailable["2026-07-20"].get("raw_row_count") != 2:
        raise AssertionError(f"same-day box evidence must be folded once: {unavailable['2026-07-20']}")
    if first.get("box_tariff", {}).get("logistics_display") != "110%":
        raise AssertionError(f"tariff evidence missing: {first.get('box_tariff')}")
    diagnostics = dict(payload.get("diagnostics") or {})
    counts = dict(diagnostics.get("exclusion_reason_counts") or {})
    required_codes = {
        "sorting_center",
        "specialized_food",
        "specialized_fuel",
        "specialized_tires",
        "sgt_warehouse",
        "partial_barcode_coverage",
        "box_not_supported",
        "warehouse_inactive",
        "warehouse_unclassified",
        "warehouse_blocked",
    }
    missing_codes = required_codes - set(counts)
    if missing_codes:
        raise AssertionError(f"exclusion diagnostics are incomplete: {missing_codes}, {counts}")
    if diagnostics.get("request_id") != "wb-fixture-request-20260719":
        raise AssertionError(f"WB request ID missing from safe diagnostics: {diagnostics}")
    if diagnostics.get("requested_barcode_count") != 2:
        raise AssertionError(f"requested barcode count missing: {diagnostics}")
    if source.acceptance_requests[0].get("products") != [
        {"barcode": "4600000000001", "quantity": 50},
        {"barcode": "4600000000002", "quantity": 25},
    ]:
        raise AssertionError(f"acceptance/options must use actual barcode quantities: {source.acceptance_requests[0]}")


def _assert_exact_tver_probe(
    block: WbRegionalSupplyPlanningBlock, source: FixturePlanningSource
) -> None:
    source.acceptance_payload = deepcopy(source.fixture["acceptance_options"])
    for row in source.acceptance_payload["result"]:
        row["warehouses"] = [
            item for item in row["warehouses"] if int(item.get("warehouseID") or 0) != 301806
        ]
    north = block.build_options({"district_key": PLANNING_ZONE_CENTRAL_NORTH})
    options = list(north.get("options") or [])
    if [item.get("warehouse_id") for item in options] != ["301806"]:
        raise AssertionError(f"exact Tver probe must not be suppressed by SC Tver: {north}")
    if not any(str(item.get("warehouse_id")) == "301806" for item in source.acceptance_requests):
        raise AssertionError(f"warehouseID-specific probe was not called: {source.acceptance_requests}")


def _assert_operator_exclusions(
    block: WbRegionalSupplyPlanningBlock,
    source: FixturePlanningSource,
    fixture: dict[str, object],
) -> None:
    source.acceptance_payload = deepcopy(fixture["acceptance_options"])
    before = len(source.acceptance_requests)
    south = block.build_options(
        {
            "district_key": PLANNING_ZONE_CENTRAL_SOUTH,
            "excluded_wb_warehouse_ids": [507, 206348],
        }
    )
    option_ids = [str(item.get("warehouse_id") or "") for item in south.get("options") or []]
    if "507" in option_ids or "206348" in option_ids:
        raise AssertionError(f"operator-excluded warehouse leaked into options: {south}")
    counts = dict(south.get("diagnostics", {}).get("exclusion_reason_counts") or {})
    if counts.get("excluded_by_operator", 0) < 2:
        raise AssertionError(f"operator exclusions are not observable: {counts}")
    probe_requests = source.acceptance_requests[before:]
    if any(
        str(item.get("warehouse_id") or "") in {"507", "206348"}
        for item in probe_requests
    ):
        raise AssertionError(f"operator-excluded warehouse was probed: {probe_requests}")

    source.acceptance_payload = deepcopy(fixture["acceptance_options"])
    for barcode_row in source.acceptance_payload["result"]:
        barcode_row["warehouses"].append({"warehouseID": 0, "canBox": True})
    service_group_result = block.build_options(
        {"district_key": PLANNING_ZONE_CENTRAL_SOUTH}
    )
    if any(
        str(item.get("warehouse_id") or "") == "0"
        for item in service_group_result.get("options") or []
    ):
        raise AssertionError("warehouseID 0 must never become a destination")
    service_counts = dict(
        service_group_result.get("diagnostics", {}).get("exclusion_reason_counts") or {}
    )
    if service_counts.get("wb_aggregate_service_group_not_destination", 0) < 1:
        raise AssertionError(f"warehouseID 0 exclusion is not explicit: {service_counts}")
    source.acceptance_payload = deepcopy(fixture["acceptance_options"])

    all_south = block.build_options(
        {
            "district_key": PLANNING_ZONE_CENTRAL_SOUTH,
            "excluded_wb_warehouse_ids": [507, 206348, 301808],
        }
    )
    if all_south.get("status") != "no_options" or not any(
        item.get("code") == "no_eligible_storage_warehouse_after_exclusions"
        for item in all_south.get("blockers") or []
    ):
        raise AssertionError(
            f"all operator exclusions need a controlled empty result: {all_south}"
        )


def _assert_available_reserve_outranks_unavailable_primary(
    block: WbRegionalSupplyPlanningBlock,
    source: FixturePlanningSource,
    fixture: dict[str, object],
) -> None:
    source.acceptance_payload = deepcopy(fixture["acceptance_options"])
    source.coefficients_payload = [
        item
        for item in fixture["coefficients"]
        if int(item.get("warehouseID") or 0) != 507
    ]
    south = block.build_options({"district_key": PLANNING_ZONE_CENTRAL_SOUTH})
    options = list(south.get("options") or [])
    if options[0].get("warehouse_name") != "Тула" or options[-1].get("warehouse_name") != "Коледино":
        raise AssertionError(f"available reserve must outrank unavailable primary: {options}")
    if options[-1].get("blocker_codes") != ["no_available_date"]:
        raise AssertionError(f"unavailable primary must retain an explicit blocker: {options[-1]}")
    source.coefficients_payload = deepcopy(fixture["coefficients"])


def _assert_one_sku_request(
    block: WbRegionalSupplyPlanningBlock,
    source: FixturePlanningSource,
    runtime: RegistryUploadDbBackedRuntime,
) -> None:
    _seed_last_result(runtime, allocated_second=0)
    before = len(source.acceptance_requests)
    payload = block.build_options({"district_key": PLANNING_ZONE_CENTRAL_SOUTH})
    if payload.get("status") != "ready":
        raise AssertionError(f"one-SKU planning must remain usable: {payload}")
    general_requests = [
        item
        for item in source.acceptance_requests[before:]
        if item.get("warehouse_id") in (None, "")
    ]
    if not general_requests or general_requests[0].get("products") != [
        {"barcode": "4600000000001", "quantity": 50}
    ]:
        raise AssertionError(f"one-SKU acceptance request is wrong: {general_requests}")
    _seed_last_result(runtime)


def _assert_missing_storage_does_not_reallocate(
    block: WbRegionalSupplyPlanningBlock,
    source: FixturePlanningSource,
    runtime: RegistryUploadDbBackedRuntime,
    fixture: dict[str, object],
) -> None:
    source.acceptance_payload = {
        "requestId": "wb-fixture-no-east-storage",
        "result": [
            {
                "barcode": barcode,
                "warehouses": [{"warehouseID": 507, "canBox": True}],
            }
            for barcode in fixture["barcodes"]
        ],
    }
    before = deepcopy(runtime.load_wb_regional_supply_result_state())
    blocked = block.build_options({"district_key": PLANNING_ZONE_CENTRAL_EAST})
    if blocked.get("status") != "no_options" or not any(
        item.get("code") == "no_eligible_storage_warehouse"
        for item in blocked.get("blockers") or []
    ):
        raise AssertionError(f"missing east storage must be an explicit blocker: {blocked}")
    after = runtime.load_wb_regional_supply_result_state()
    if after != before:
        raise AssertionError("warehouse planning must not mutate or reallocate the saved calculation")
    source.acceptance_payload = deepcopy(fixture["acceptance_options"])


def _assert_conflicting_can_box_fails_closed(
    block: WbRegionalSupplyPlanningBlock,
    source: FixturePlanningSource,
    fixture: dict[str, object],
) -> None:
    source.acceptance_payload = deepcopy(fixture["acceptance_options"])
    first_barcode = source.acceptance_payload["result"][0]
    first_barcode["warehouses"].insert(0, {"warehouseID": 507, "canBox": False})
    payload = block.build_options({"district_key": PLANNING_ZONE_CENTRAL_SOUTH})
    names = [item.get("warehouse_name") for item in payload.get("options") or []]
    if "Коледино" in names or names != ["Тула", "Воронеж"]:
        raise AssertionError(f"conflicting canBox evidence must fail closed for the warehouse: {payload}")
    counts = dict(payload.get("diagnostics", {}).get("exclusion_reason_counts") or {})
    if counts.get("box_not_supported", 0) < 1:
        raise AssertionError(f"conflicting canBox exclusion must be observable: {counts}")
    source.acceptance_payload = deepcopy(fixture["acceptance_options"])


def _assert_controlled_failures(
    block: WbRegionalSupplyPlanningBlock,
    source: FixturePlanningSource,
    runtime: RegistryUploadDbBackedRuntime,
) -> None:
    before = len(source.acceptance_requests)
    mismatch = block.build_options(
        {"district_key": PLANNING_ZONE_CENTRAL_SOUTH, "calculation_id": "stale"}
    )
    if mismatch.get("status") != "blocked" or len(source.acceptance_requests) != before:
        raise AssertionError(f"stale calculation ID must stop before WB call: {mismatch}")

    empty = block.build_options({"district_key": DISTRICT_NORTHWEST})
    if empty.get("status") != "empty":
        raise AssertionError(f"empty direction must be controlled: {empty}")

    _seed_nomenclature(runtime, include_second=False)
    before = len(source.acceptance_requests)
    missing = block.build_options({"district_key": PLANNING_ZONE_CENTRAL_SOUTH})
    if missing.get("status") != "blocked" or len(source.acceptance_requests) != before:
        raise AssertionError(f"missing barcode must stop before WB call: {missing}")
    _seed_nomenclature(runtime, include_second=True)

    source.fail_acceptance = WbSuppliesHttpStatusError(
        400,
        '{"error":"bad request","token":"secret-token","barcode":"4600000000001"}',
    )
    failed = block.build_options({"district_key": PLANNING_ZONE_CENTRAL_SOUTH})
    serialized = json.dumps(failed, ensure_ascii=False)
    if failed.get("status") != "upstream_error":
        raise AssertionError(f"WB failure must be controlled: {failed}")
    blocker_diagnostics = json.dumps(
        (failed.get("blockers") or [{}])[0].get("diagnostics") or {},
        ensure_ascii=False,
    )
    if "secret-token" in serialized:
        raise AssertionError("controlled WB failure leaked a secret")
    if "4600000000001" in blocker_diagnostics:
        raise AssertionError("safe failure diagnostics leaked a full barcode")
    source.fail_acceptance = None


def _seed_last_result(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    allocated_second: int = 25,
) -> None:
    districts: list[dict[str, object]] = []
    for zone_key, label in (
        (PLANNING_ZONE_CENTRAL_NORTH, "ЦФО Север"),
        (PLANNING_ZONE_CENTRAL_EAST, "ЦФО Восток"),
        (PLANNING_ZONE_CENTRAL_SOUTH, "ЦФО Юг"),
    ):
        districts.append(
            {
                "district_key": zone_key,
                "planning_zone_key": zone_key,
                "planning_zone_label": label,
                "district_name_ru": label,
                "total_qty": 50 + allocated_second,
                "deficit_qty": 100,
                "rows": [
                    {
                        "nm_id": MAIN_NM_ID,
                        "sku_comment": "Main SKU",
                        "allocated_qty": 50,
                        "deficit_qty": 90,
                        "current_stock": 10,
                        "in_transit_qty": 5,
                        "avg_daily_demand": 4,
                        "target_stock": 105,
                        "unmet_deficit_qty": 40,
                    },
                    {
                        "nm_id": SECOND_NM_ID,
                        "sku_comment": "Second SKU",
                        "allocated_qty": allocated_second,
                        "deficit_qty": 50,
                        "current_stock": 5,
                        "in_transit_qty": 0,
                        "avg_daily_demand": 2,
                        "target_stock": 55,
                        "unmet_deficit_qty": max(0, 50 - allocated_second),
                    },
                ],
            }
        )
    districts.append(
        {
            "district_key": DISTRICT_NORTHWEST,
            "district_name_ru": "Северо-Западный федеральный округ",
            "total_qty": 0,
            "deficit_qty": 0,
            "rows": [],
        }
    )
    runtime.save_wb_regional_supply_result_state(
        calculated_at=ACTIVATED_AT,
        payload={
            "status": "success",
            "payload_version": "v2_planning_zones",
            "calculation_id": f"calc-planning-smoke-{allocated_second}",
            "calculated_at": ACTIVATED_AT,
            "report_date": "2026-07-19",
            "settings": {"included_district_keys": list(SUPPLY_PLANNING_ZONE_KEYS)},
            "summary": {"total_qty": 3 * (50 + allocated_second)},
            "districts": districts,
        },
    )


def _seed_nomenclature(runtime: RegistryUploadDbBackedRuntime, *, include_second: bool) -> None:
    runtime.save_nomenclature_items_atomic(
        [
            _nomenclature_item(MAIN_NM_ID, "Main SKU", "4600000000001"),
            *(
                [_nomenclature_item(SECOND_NM_ID, "Second SKU", "4600000000002")]
                if include_second
                else []
            ),
        ]
    )
    if not include_second and runtime.load_nomenclature_item(f"item-{SECOND_NM_ID}") is not None:
        runtime.delete_nomenclature_item(
            f"item-{SECOND_NM_ID}", updated_at=ACTIVATED_AT
        )


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
