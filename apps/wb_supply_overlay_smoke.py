"""Targeted smoke-check for selected WB supplies calculation overlay."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_supplies import _normalize_list_request, _row_matches_districts  # noqa: E402
from packages.application.wb_supply_overlay import (  # noqa: E402
    apply_stock_ff_overlay,
    augment_supply_row_with_district,
    build_selected_wb_supply_overlay,
    build_warehouse_district_mapping,
    build_wb_supply_overlay_options,
    convert_raw_district_to_key,
    district_filter_options,
    factory_inbound_overlay_rows,
    regional_overlay_quantities,
)
from packages.contracts.factory_order_supply import FactoryOrderStockFfRow  # noqa: E402


ACTIVE_SKUS = [(101, "SKU 101"), (102, "SKU 102")]


class FakeRuntime:
    def __init__(self) -> None:
        self.warehouses = [
            {"warehouse_id": "1", "warehouse_name": "Коледино"},
            {"warehouse_id": "2", "warehouse_name": "Казань"},
            {"warehouse_id": "3", "warehouse_name": "Склад без ФО"},
        ]
        self.records = [
            _record(
                "s2",
                status_id=2,
                status_label="Запланировано",
                warehouse_name="Коледино",
                supply_date="2026-04-20",
                district_key="central",
                raw_goods=[{"nmID": 101, "quantity": 30}],
            ),
            _record(
                "s3-central",
                status_id=3,
                status_label="Отгрузка разрешена",
                warehouse_name="Коледино",
                supply_date="2026-04-20",
                district_key="central",
                raw_goods=[
                    {"nmID": 101, "quantity": 30},
                    {"nmID": 999, "quantity": 7},
                    {"quantity": 3},
                ],
            ),
            _record(
                "s3",
                status_id=3,
                status_label="Отгрузка разрешена",
                warehouse_name="Казань",
                supply_date="2026-04-21",
                district_key="volga",
                raw_goods=[{"nmID": 102, "quantity": 50}],
            ),
            _record(
                "s4-no-date",
                status_id=4,
                status_label="Идёт приёмка",
                warehouse_name="Коледино",
                supply_date="",
                district_key="central",
                raw_goods=[{"nmID": 101, "quantity": 10}],
            ),
            _record(
                "s6-unmapped",
                status_id=6,
                status_label="Отгружено на воротах",
                warehouse_name="Склад без ФО",
                supply_date="2026-04-22",
                district_key="unmapped",
                raw_goods=[{"nmID": 101, "quantity": 5}],
            ),
            _record(
                "s1",
                status_id=1,
                status_label="Не запланировано",
                warehouse_name="Коледино",
                supply_date="2026-04-23",
                district_key="central",
                raw_goods=[{"nmID": 101, "quantity": 5}],
            ),
            _record(
                "s5",
                status_id=5,
                status_label="Принято",
                warehouse_name="Казань",
                supply_date="2026-04-24",
                district_key="volga",
                raw_goods=[{"nmID": 102, "quantity": 5}],
            ),
            _record(
                "s-no-goods",
                status_id=3,
                status_label="Отгрузка разрешена",
                warehouse_name="Коледино",
                supply_date="2026-04-25",
                district_key="central",
                raw_goods=[],
            ),
            _record(
                "s-no-usable",
                status_id=3,
                status_label="Отгрузка разрешена",
                warehouse_name="Коледино",
                supply_date="2026-04-26",
                district_key="central",
                raw_goods=[{"nmID": 999, "quantity": 20}],
            ),
            _record(
                "s-dopr",
                status_id=3,
                status_label="Отгрузка разрешена",
                warehouse_name="Коледино",
                supply_date="2026-04-27",
                district_key="central",
                raw_goods=[{"nmID": 101, "quantity": 20}],
                type_label="Допринято",
            ),
        ]

    def list_wb_supplies_cache_records(self):
        return self.records

    def list_wb_supplies_warehouses(self):
        return self.warehouses


def main() -> None:
    _assert_district_mapping()
    _assert_list_filter_contract()
    _assert_overlay_selector_and_math()
    print("wb_supply_overlay_smoke: OK")


def _assert_district_mapping() -> None:
    options = district_filter_options()
    if [item["label"] for item in options] != ["ЦФО", "СЗФО", "ПФО", "УрФО", "Юг+СК", "Сиб+ДВ"]:
        raise AssertionError(f"district preset labels changed: {options}")
    expected = {
        "Центральный федеральный округ": "central",
        "Северо-Западный федеральный округ": "northwest",
        "Приволжский федеральный округ": "volga",
        "Уральский федеральный округ": "ural",
        "Южный федеральный округ": "south_caucasus",
        "Северо-Кавказский федеральный округ": "south_caucasus",
        "Сибирский федеральный округ": "far_siberia",
        "Дальневосточный федеральный округ": "far_siberia",
    }
    for raw, district_key in expected.items():
        if convert_raw_district_to_key(raw) != district_key:
            raise AssertionError(f"raw district {raw!r} must map to {district_key}")

    mapping = build_warehouse_district_mapping(
        warehouse_rows=[
            {"warehouse_id": "1", "warehouse_name": "Коледино"},
            {"warehouse_id": "2", "warehouse_name": "Казань"},
            {"warehouse_id": "3", "warehouse_name": "Новосибирск"},
            {"warehouse_id": "4", "warehouse_name": "Склад без ФО"},
        ],
        supply_rows=[
            {"warehouse_id": "1", "warehouse_name": "Коледино"},
            {"warehouse_id": "2", "warehouse_name": "Казань"},
            {"warehouse_id": "3", "warehouse_name": "Новосибирск"},
            {"warehouse_id": "4", "warehouse_name": "Склад без ФО"},
        ],
        office_rows=[{"name": "Коледино", "federalDistrict": "Центральный федеральный округ"}],
        tariff_rows=[
            {"warehouseName": "Коледино", "geoName": "Приволжский федеральный округ"},
            {"warehouseName": "Казань", "geoName": "Приволжский федеральный округ"},
            {"warehouseName": "Новосибирск", "geoName": "Сибирский федеральный округ"},
        ],
    )
    by_name = mapping["by_normalized_name"]
    if by_name["коледино"]["district_key"] != "central" or by_name["коледино"]["source"] != "marketplace_offices":
        raise AssertionError("Marketplace offices mapping must win over tariffs by exact normalized name")
    if by_name["казань"]["district_key"] != "volga" or by_name["казань"]["source"] != "tariffs_box":
        raise AssertionError("tariffs/box must be used as district fallback")
    if by_name["новосибирск"]["district_key"] != "far_siberia":
        raise AssertionError("Siberia/Far East raw names must collapse into far_siberia")
    if not any("Склад без ФО" in item for item in mapping["warnings"]):
        raise AssertionError(f"unmapped warehouses must emit warning, got {mapping['warnings']}")
    if mapping.get("unmapped_warehouse_count") != 1 or mapping.get("unmapped_warehouses") != ["Склад без ФО"]:
        raise AssertionError(f"unmapped warehouse summary must stay compact/countable, got {mapping}")

    catalog_only = build_warehouse_district_mapping(
        warehouse_rows=[{"warehouse_id": "5", "warehouse_name": "Глобальный склад без поставок"}],
        supply_rows=[],
    )
    if catalog_only.get("unmapped_warehouse_count") != 0 or catalog_only.get("warnings"):
        raise AssertionError(f"global warehouse catalog alone must not emit overlay warnings, got {catalog_only}")

    manual = build_warehouse_district_mapping(
        warehouse_rows=[],
        supply_rows=[
            {
                "warehouse_id": "130744",
                "warehouse_name": "Краснодар (Тихорецкая)",
                "actual_warehouse_id": "218210",
                "actual_warehouse_name": "Обухово",
                "transit_warehouse_id": "218210",
                "transit_warehouse_name": "Обухово",
                "warehouse_from_name": "Краснодар (Тихорецкая)",
                "warehouse_to_name": "Обухово",
                "warehouse_display": "Краснодар (Тихорецкая) → Обухово",
            }
        ],
        tariff_rows=[{"warehouseName": "Обухово", "geoName": "Центральный федеральный округ"}],
    )
    routed = manual["by_normalized_name"].get("краснодар тихорецкая")
    if not routed or routed.get("district_key") != "south_caucasus" or routed.get("source") != "manual_known_wb_warehouse":
        raise AssertionError(f"planned Краснодар warehouse must map to south_caucasus, not transit Обухово, got {manual}")


def _assert_list_filter_contract() -> None:
    request = _normalize_list_request({"district_keys": "central,volga,unknown"})
    if request["district_keys"] != ["central", "volga"]:
        raise AssertionError(f"district_keys parser must keep only supported keys, got {request}")
    if not _row_matches_districts({"district_key": "central"}, request["district_keys"]):
        raise AssertionError("district filter must match mapped rows")
    if _row_matches_districts({"district_key": "unmapped"}, request["district_keys"]):
        raise AssertionError("unmapped rows must not enter district presets")

    mapping = build_warehouse_district_mapping(
        supply_rows=[
            {
                "warehouse_id": "130744",
                "warehouse_name": "Краснодар (Тихорецкая)",
                "actual_warehouse_id": "218210",
                "actual_warehouse_name": "Обухово",
                "transit_warehouse_id": "218210",
                "transit_warehouse_name": "Обухово",
                "warehouse_from_name": "Краснодар (Тихорецкая)",
                "warehouse_to_name": "Обухово",
                "warehouse_display": "Краснодар (Тихорецкая) → Обухово",
            }
        ],
        tariff_rows=[{"warehouseName": "Обухово", "geoName": "Центральный федеральный округ"}],
    )
    routed = augment_supply_row_with_district(
        {
            "warehouse_id": "130744",
            "warehouse_name": "Краснодар (Тихорецкая)",
            "actual_warehouse_id": "218210",
            "actual_warehouse_name": "Обухово",
            "transit_warehouse_id": "218210",
            "transit_warehouse_name": "Обухово",
            "warehouse_from_name": "Краснодар (Тихорецкая)",
            "warehouse_to_name": "Обухово",
            "warehouse_display": "Краснодар (Тихорецкая) → Обухово",
            "district_key": "central",
        },
        mapping,
    )
    if routed.get("district_key") != "south_caucasus":
        raise AssertionError(f"Краснодар -> Обухово must map by planned warehouse to south_caucasus, got {routed}")
    if not _row_matches_districts(routed, ["south_caucasus"]):
        raise AssertionError("south_caucasus filter must include planned Краснодар transit route")
    if _row_matches_districts(routed, ["central"]):
        raise AssertionError("central filter must not include planned Краснодар route just because actual warehouse is Обухово")


def _assert_overlay_selector_and_math() -> None:
    runtime = FakeRuntime()
    payload = build_wb_supply_overlay_options(runtime=runtime, active_skus=ACTIVE_SKUS)
    options = {item["supply_id"]: item for item in payload["options"]}
    eligible = {item["supply_id"] for item in payload["options"] if item["eligible_for_overlay"]}
    if eligible != {"s3-central", "s3", "s6-unmapped"}:
        raise AssertionError(f"eligible statuses/composition/date set mismatch: {eligible}")
    if {"s1", "s2", "s5", "s-dopr"} & set(options):
        raise AssertionError(f"status 1/2/5 and Допринято must not be returned to the selector, got {options.keys()}")
    if payload.get("eligible_status_ids") != [3, 4, 6]:
        raise AssertionError(f"calculation overlay eligible status ids must be 3/4/6, got {payload.get('eligible_status_ids')}")
    if payload.get("summary", {}).get("excluded_by_status") != 4:
        raise AssertionError(f"selector must count status-excluded rows, got {payload.get('summary')}")
    if "нет расчётной даты поставки" not in options["s4-no-date"]["disabled_reasons"]:
        raise AssertionError("no-date supplies must be disabled")
    if "нет состава поставки" not in options["s-no-goods"]["disabled_reasons"]:
        raise AssertionError("no-composition supplies must be disabled")
    if "нет usable active SKU quantity" not in options["s-no-usable"]["disabled_reasons"]:
        raise AssertionError("supplies without active SKU quantity must be disabled")
    if {item["reason"] for item in options["s3-central"]["skipped_goods"]} != {"nm_id_not_active", "missing_nm_id"}:
        raise AssertionError(f"unknown/missing nmId goods must be diagnosed, got {options['s3-central']['skipped_goods']}")

    overlay = build_selected_wb_supply_overlay(
        runtime=runtime,
        selected_supply_ids=("s3-central", "s3", "s6-unmapped", "s2", "s5", "s1", "s4-no-date", "s-dopr", "missing"),
        active_skus=ACTIVE_SKUS,
    )
    if overlay["qty_by_nm_id"] != {"101": 35.0, "102": 50.0}:
        raise AssertionError(f"selected overlay must sum active nmId composition only, got {overlay}")
    skipped_reasons = {item["reason"] for item in overlay["skipped"]}
    if skipped_reasons != {"disabled_supply", "supply_not_found"}:
        raise AssertionError(f"disabled/missing selected supplies must be diagnosed, got {overlay['skipped']}")

    stock_rows = [
        FactoryOrderStockFfRow(nm_id=101, sku_comment="SKU 101", stock_ff=20.0, snapshot_date=None, comment=""),
        FactoryOrderStockFfRow(nm_id=102, sku_comment="SKU 102", stock_ff=60.0, snapshot_date=None, comment=""),
    ]
    effective_rows, stock_diagnostics, stock_warnings = apply_stock_ff_overlay(
        stock_ff_rows=stock_rows,
        active_skus=ACTIVE_SKUS,
        overlay=overlay,
    )
    effective_by_nm = {row.nm_id: row.stock_ff for row in effective_rows}
    if effective_by_nm != {101: 0.0, 102: 10.0}:
        raise AssertionError(f"selected WB qty must subtract from stock_ff with floor zero, got {effective_by_nm}")
    if stock_diagnostics["by_nm_id"]["101"]["over_reserved_qty"] != 15.0 or not stock_warnings:
        raise AssertionError(f"over-reserved diagnostics/warning missing: {stock_diagnostics}, {stock_warnings}")
    if not stock_diagnostics["stock_deduction_applied"]:
        raise AssertionError(f"default stock overlay must deduct selected WB supplies, got {stock_diagnostics}")

    ledger_effective_rows, ledger_stock_diagnostics, ledger_stock_warnings = apply_stock_ff_overlay(
        stock_ff_rows=stock_rows,
        active_skus=ACTIVE_SKUS,
        overlay=overlay,
        deduct_selected_supplies=False,
    )
    ledger_effective_by_nm = {row.nm_id: row.stock_ff for row in ledger_effective_rows}
    if ledger_effective_by_nm != {101: 20.0, 102: 60.0}:
        raise AssertionError(f"ledger source must not deduct selected WB qty from stock_ff, got {ledger_effective_by_nm}")
    if ledger_stock_diagnostics["stock_deduction_applied"] or ledger_stock_warnings:
        raise AssertionError(f"ledger source must diagnose selected qty without over-reserve warnings, got {ledger_stock_diagnostics}, {ledger_stock_warnings}")

    regional_qty, regional_diagnostics, regional_warnings = regional_overlay_quantities(overlay=overlay)
    if regional_qty != {101: {"central": 30.0}, 102: {"volga": 50.0}}:
        raise AssertionError(f"regional overlay must add qty only to mapped district, got {regional_qty}")
    if regional_diagnostics["added_qty_by_district"]["central"] != 30.0:
        raise AssertionError("central mapped qty must be exposed in diagnostics")
    if not regional_warnings or not regional_diagnostics["unmapped_events"]:
        raise AssertionError("unmapped warehouse selected supply must warn and skip district overlay")

    inbound_rows, inbound_diagnostics, inbound_warnings = factory_inbound_overlay_rows(
        overlay=overlay,
        report_date=date(2026, 4, 18),
        inbound_window_end=date(2026, 4, 21),
    )
    inbound_by_nm = {}
    for row in inbound_rows:
        inbound_by_nm[row.nm_id] = inbound_by_nm.get(row.nm_id, 0.0) + row.quantity
    if inbound_by_nm != {101: 30.0, 102: 50.0}:
        raise AssertionError(f"factory overlay must add selected rows inside inbound window, got {inbound_by_nm}")
    if inbound_diagnostics["added_inbound_ff_to_wb_qty_total"] != 80.0:
        raise AssertionError(f"factory inbound diagnostics missing total, got {inbound_diagnostics}")
    if not inbound_warnings or len(inbound_diagnostics["outside_inbound_window_events"]) != 1:
        raise AssertionError("factory overlay must diagnose selected supplies outside inbound window")


def _record(
    supply_id: str,
    *,
    status_id: int,
    status_label: str,
    warehouse_name: str,
    supply_date: str,
    district_key: str,
    raw_goods: list[dict[str, object]],
    virtual_type_id: int | None = None,
    type_label: str = "",
) -> dict[str, object]:
    normalized = {
        "supply_id": supply_id,
        "cache_key": supply_id,
        "wb_supply_id": supply_id,
        "preorder_id": f"pre-{supply_id}",
        "number_label": supply_id,
        "status_id": status_id,
        "status_label": status_label,
        "virtual_type_id": virtual_type_id,
        "type_label": type_label,
        "warehouse_id": supply_id,
        "warehouse_name": warehouse_name,
        "warehouse_display": warehouse_name,
        "supply_date": supply_date,
        "district_key": district_key,
        "district_label_ru": "",
    }
    return {
        "supply_id": supply_id,
        "cache_key": supply_id,
        "wb_supply_id": supply_id,
        "preorder_id": f"pre-{supply_id}",
        "normalized": normalized,
        "raw_list": {"supplyID": supply_id, "statusID": status_id, "supplyDate": supply_date},
        "raw_detail": {"warehouseName": warehouse_name},
        "raw_goods": raw_goods,
        "raw_package": [],
    }


if __name__ == "__main__":
    main()
