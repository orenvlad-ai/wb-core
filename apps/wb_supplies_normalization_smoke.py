"""Targeted smoke-check for WB supplies field normalization."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_supplies import _normalize_supply_row  # noqa: E402


def main() -> None:
    _check_transit_route_cost_quantity_fixture()
    _check_non_transit_quantity_fixture()
    _check_virtual_type_zero_cost_fixture()
    _check_empty_detail_does_not_overwrite_list_evidence()
    _check_unknown_transit_cost_is_not_zero()
    print("wb_supplies_normalization_smoke: OK")


def _check_transit_route_cost_quantity_fixture() -> None:
    row = _normalize_supply_row(
        raw_list={
            "supplyID": 39265492,
            "preorderID": 501,
            "statusID": 5,
            "boxTypeID": 1,
            "supplyDate": "2026-05-15T00:00:00+03:00",
            "factDate": "2026-05-15T13:00:00+03:00",
        },
        raw_detail={
            "supplyID": 39265492,
            "statusID": 5,
            "boxTypeID": 1,
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
        },
        raw_goods=[
            {"quantity": 2500, "acceptedQuantity": 2490, "supplierBoxAmount": 2500},
            {"quantity": 5000, "acceptedQuantity": 4993, "supplierBoxAmount": 5000},
        ],
        raw_package=[{"quantity": 250} for _ in range(30)],
        warehouse_by_id={},
        synced_at="2026-06-10T00:00:00Z",
        warnings=[],
    )
    _assert(row["warehouse_display"] == "Склад Шушары → Обухово", row)
    _assert(row["warehouse_from_name"] == "Склад Шушары", row)
    _assert(row["warehouse_to_name"] == "Обухово", row)
    _assert(row["warehouse_fact_line"] == "", row)
    _assert(row["quantity_added"] == 7500, row)
    _assert(row["packed_quantity"] == 7500, row)
    _assert(row["accepted_quantity"] == 7483, row)
    _assert(row["cost_total"] == 11543.52, row)
    _assert(row["has_transit_cost_marker"] is True, row)
    _assert(row["type_label"] == "Короб · с транзитом", row)
    _assert("boxTypeID" not in row["type_label"], row)


def _check_non_transit_quantity_fixture() -> None:
    row = _normalize_supply_row(
        raw_list={"supplyID": 39265540, "statusID": 5, "boxTypeID": 1},
        raw_detail={
            "supplyID": 39265540,
            "statusID": 5,
            "boxTypeID": 1,
            "warehouseID": 120762,
            "warehouseName": "Электросталь",
            "actualWarehouseID": 120762,
            "actualWarehouseName": "Электросталь",
            "quantity": 9250,
            "acceptedQuantity": 9237,
            "acceptanceCost": 0,
            "paidAcceptanceCoefficient": 0,
        },
        raw_goods=[
            {"quantity": 4250, "acceptedQuantity": 4239},
            {"quantity": 5000, "acceptedQuantity": 4998},
        ],
        raw_package=[{"quantity": 250} for _ in range(37)],
        warehouse_by_id={},
        synced_at="2026-06-10T00:00:00Z",
        warnings=[],
    )
    _assert(row["warehouse_display"] == "Электросталь", row)
    _assert(row["quantity_added"] == 9250, row)
    _assert(row["packed_quantity"] == 9250, row)
    _assert(row["accepted_quantity"] == 9237, row)
    _assert(row["cost_total"] == 0, row)
    _assert(row["type_label"] == "Короб", row)


def _check_virtual_type_zero_cost_fixture() -> None:
    row = _normalize_supply_row(
        raw_list={"supplyID": 39605280, "statusID": 5, "boxTypeID": 0},
        raw_detail={
            "supplyID": 39605280,
            "statusID": 5,
            "boxTypeID": 0,
            "virtualTypeID": 5,
            "warehouseID": 130744,
            "warehouseName": "Краснодар (Тихорецкая)",
            "quantity": 1,
            "acceptedQuantity": 1,
            "acceptanceCost": None,
            "paidAcceptanceCoefficient": 0,
        },
        raw_goods=[{"quantity": 1, "acceptedQuantity": 1, "readyForSaleQuantity": 1}],
        raw_package=[],
        warehouse_by_id={},
        synced_at="2026-06-10T00:00:00Z",
        warnings=[],
    )
    _assert(row["warehouse_display"] == "Краснодар (Тихорецкая)", row)
    _assert(row["quantity_added"] == 1, row)
    _assert(row["accepted_quantity"] == 1, row)
    _assert(row["type_label"] == "Допринято", row)
    _assert("Тип 0" not in row["type_label"], row)
    _assert(row["acceptance_coefficient"] == 0, row)
    _assert(row["cost_total"] == 0, row)
    _assert(row["cost_evidence"] == "paidAcceptanceCoefficient.free_accepted_non_transit", row)


def _check_empty_detail_does_not_overwrite_list_evidence() -> None:
    row = _normalize_supply_row(
        raw_list={
            "supplyID": 9001,
            "statusID": 5,
            "warehouseID": 120762,
            "warehouseName": "Электросталь",
            "quantity": 9250,
            "acceptanceCost": 0,
        },
        raw_detail={
            "supplyID": 9001,
            "statusID": 5,
            "warehouseID": None,
            "warehouseName": "",
            "quantity": None,
            "acceptedQuantity": 9237,
            "acceptanceCost": None,
        },
        raw_goods=[{"quantity": 9250, "acceptedQuantity": 9237}],
        raw_package=None,
        warehouse_by_id={},
        synced_at="2026-06-10T00:00:00Z",
        warnings=[],
    )
    _assert(row["warehouse_display"] == "Электросталь", row)
    _assert(row["quantity_added"] == 9250, row)
    _assert(row["cost_total"] == 0, row)
    _assert(row["warehouse_evidence"]["warehouse_name"] == "list.warehouseName", row)


def _check_unknown_transit_cost_is_not_zero() -> None:
    row = _normalize_supply_row(
        raw_list={"supplyID": 9002, "statusID": 5, "boxTypeID": 1},
        raw_detail={
            "supplyID": 9002,
            "statusID": 5,
            "warehouseName": "Краснодар (Тихорецкая)",
            "transitWarehouseName": "Обухово",
            "quantity": 1000,
            "acceptedQuantity": 999,
            "acceptanceCost": 0,
        },
        raw_goods=None,
        raw_package=None,
        warehouse_by_id={},
        synced_at="2026-06-10T00:00:00Z",
        warnings=[],
    )
    _assert(row["warehouse_display"] == "Краснодар (Тихорецкая) → Обухово", row)
    _assert(row["planned_warehouse_name"] == "Краснодар (Тихорецкая)", row)
    _assert(row["target_warehouse_name"] == "Краснодар (Тихорецкая)", row)
    _assert(row["district_source_warehouse_name"] == "Краснодар (Тихорецкая)", row)
    _assert(row["district_source_warehouse_role"] == "planned", row)
    _assert(row["cost_total"] is None, row)
    _assert(row["acceptance_cost"] == 0, row)
    _assert(row["cost_evidence"] == "transit_total_absent_in_official_supply_detail", row)


def _assert(condition: bool, payload: object) -> None:
    if not condition:
        raise AssertionError(payload)


if __name__ == "__main__":
    main()
