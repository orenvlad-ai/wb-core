"""Targeted smoke-check for stocks region normalization and district decomposition."""

from dataclasses import asdict
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.stocks_block import transform_legacy_payload


def main() -> None:
    _check_live_aliases_and_unmapped_note()
    _check_legacy_aliases_still_map()
    _check_central_warehouse_planning_reconciliation()
    print("smoke-check passed")


def _check_live_aliases_and_unmapped_note() -> None:
    payload = {
        "snapshot_date": "2026-04-15",
        "requested_nm_ids": [101],
        "data": {
            "rows": [
                _row("2026-04-15", 101, "Центральный", 10),
                _row("2026-04-15", 101, "Южный и Северо-Кавказский", 5),
                _row("2026-04-15", 101, "Дальневосточный и Сибирский", 2),
                _row("2026-04-15", 101, "Армения", 1),
            ]
        },
    }
    result = asdict(transform_legacy_payload(payload))
    item = result["result"]["items"][0]
    if item["stock_total"] != 18.0:
        raise AssertionError(f"unexpected total stock after alias normalization: {item}")
    if item["stock_ru_south_caucasus"] != 5.0:
        raise AssertionError(f"south/caucasus stock must survive live alias normalization: {item}")
    if item["stock_ru_far_siberia"] != 2.0:
        raise AssertionError(f"far/siberia stock must survive live alias normalization: {item}")
    detail = result["result"]["detail"]
    if "Армения=1" not in detail:
        raise AssertionError(f"unmapped non-district quantity must surface in detail, got {detail!r}")
    print("live-shaped-region-aliases: ok -> south/caucasus and far/siberia survive new endpoint naming")


def _check_legacy_aliases_still_map() -> None:
    payload = {
        "snapshot_date": "2026-04-05",
        "requested_nm_ids": [202],
        "data": {
            "rows": [
                _row("2026-04-05", 202, "Южный + Северо-Кавказский", 7),
                _row("2026-04-05", 202, "Дальневосточный + Сибирский", 3),
            ]
        },
    }
    result = asdict(transform_legacy_payload(payload))
    item = result["result"]["items"][0]
    if item["stock_ru_south_caucasus"] != 7.0 or item["stock_ru_far_siberia"] != 3.0:
        raise AssertionError(f"legacy aliases must remain backward-compatible, got {item}")
    if result["result"]["detail"] != "":
        raise AssertionError(f"mapped legacy regions must not create unmapped detail, got {result['result']['detail']!r}")
    print("legacy-region-aliases: ok -> old plus-sign naming remains compatible")


def _check_central_warehouse_planning_reconciliation() -> None:
    payload = {
        "snapshot_date": "2026-07-19",
        "requested_nm_ids": [303],
        "data": {
            "rows": [
                _warehouse_row(303, "Тверь", 301806, 10),
                _warehouse_row(303, "Владимир Воршинское", 301981, 20),
                _warehouse_row(303, "Электросталь", None, 5),
                _warehouse_row(303, "Котовск", None, 4),
                _warehouse_row(303, "Коледино", 507, 30),
                _warehouse_row(303, "СЦ Тверь", 910004, 7),
                _warehouse_row(303, "Электросталь: Питание", 910001, 3),
                _warehouse_row(303, "Неизвестный склад ЦФО", 910009, 8),
            ]
        },
    }
    result = asdict(transform_legacy_payload(payload))["result"]
    item = result["items"][0]
    if item["stock_ru_central"] != 87.0:
        raise AssertionError(f"canonical central control total changed: {item}")
    if (
        item["stock_ru_central_north"],
        item["stock_ru_central_east"],
        item["stock_ru_central_south"],
    ) != (10.0, 29.0, 30.0):
        raise AssertionError(f"warehouse planning-zone aggregation is wrong: {item}")
    reconciliation = result["planning_reconciliation"]
    if reconciliation != {
        "legacy_central_total": 87.0,
        "central_planning_zone_total": 69.0,
        "central_unmapped_total": 8.0,
        "central_excluded_total": 10.0,
        "difference": 0.0,
    }:
        raise AssertionError(f"central reconciliation is not explainable: {reconciliation}")
    rows = result["warehouse_rows"]
    tver = next(row for row in rows if row["warehouse_name"] == "Тверь")
    if tver["warehouse_id"] != 301806 or tver["planning_zone_key"] != "central_north":
        raise AssertionError(f"current warehouse ID/detail was lost: {tver}")
    for historical_name in ("Электросталь", "Котовск"):
        historical = next(row for row in rows if row["warehouse_name"] == historical_name)
        if historical["planning_zone_key"] != "central_east" or historical["classification_status"] != "mapped":
            raise AssertionError(f"blocked historical warehouse must still classify east: {historical}")
    special = next(row for row in rows if row["warehouse_name"] == "Электросталь: Питание")
    sorting = next(row for row in rows if row["warehouse_name"] == "СЦ Тверь")
    unknown = next(row for row in rows if row["warehouse_name"] == "Неизвестный склад ЦФО")
    if special["classification_status"] != "excluded" or sorting["classification_status"] != "excluded":
        raise AssertionError(f"specialized and sorting points must be excluded: {special}, {sorting}")
    if unknown["classification_status"] != "unmapped" or unknown["planning_zone_key"] is not None:
        raise AssertionError(f"unknown central warehouse must remain unmapped: {unknown}")
    print("central-planning-reconciliation: ok -> ID-first zones plus unmapped/excluded controls")


def _row(snapshot_date: str, nm_id: int, region_name: str, stock_count: float) -> dict[str, object]:
    return {
        "snapshot_date": snapshot_date,
        "snapshot_ts": f"{snapshot_date} 12:00:00",
        "nmId": nm_id,
        "regionName": region_name,
        "stockCount": float(stock_count),
    }


def _warehouse_row(
    nm_id: int,
    warehouse_name: str,
    warehouse_id: int | None,
    stock_count: float,
) -> dict[str, object]:
    return {
        **_row("2026-07-19", nm_id, "Центральный", stock_count),
        "warehouseId": warehouse_id,
        "warehouseName": warehouse_name,
    }


if __name__ == "__main__":
    main()
