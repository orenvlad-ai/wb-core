"""Smoke-check first 20 accepted WB supplies parity normalization."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_supplies import WbSuppliesListResult  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.wb_supplies import WbSuppliesBlock  # noqa: E402


TARGET_ROWS = [
    ("39961480", "2026-06-09", "Екатеринбург - Перспективная 14", 2, 2),
    ("39914199", "2026-06-07", "Екатеринбург - Перспективная 14", 1, 1),
    ("39750013", "2026-06-01", "Электросталь", 1, 1),
    ("39605280", "2026-05-26", "Краснодар (Тихорецкая)", 1, 1),
    ("39572333", "2026-05-25", "Краснодар (Тихорецкая)", 1, 1),
    ("39558014", "2026-05-24", "Краснодар (Тихорецкая)", 2, 2),
    ("39543474", "2026-05-23", "Краснодар (Тихорецкая)", 1, 1),
    ("39423793", "2026-05-19", "Екатеринбург - Перспективная 14", 1, 1),
    ("39389370", "2026-05-18", "Краснодар (Тихорецкая)", 15, 15),
    ("39389369", "2026-05-18", "Новосемейкино", 1, 1),
    ("39375226", "2026-05-17", "Новосемейкино", 1, 1),
    ("39361305", "2026-05-16", "Екатеринбург - Перспективная 14", 1, 1),
    ("39361304", "2026-05-16", "Электросталь", 3, 3),
    ("39332993", "2026-05-15", "Екатеринбург - Перспективная 14", 1, 1),
    ("39265540", "2026-05-15", "Электросталь", 9250, 9237),
    ("39265519", "2026-05-15", "Краснодар (Тихорецкая) → Обухово", 4750, 4728),
    ("39265492", "2026-05-15", "Склад Шушары → Обухово", 7500, 7483),
    ("39265590", "2026-05-14", "Екатеринбург - Перспективная 14 → Чехов 2, Новоселки вл 11 стр 7", 3000, 2996),
    ("39265571", "2026-05-14", "Новосемейкино → Чехов 1, Новоселки вл 11 стр 2", 5750, 5735),
    ("39238882", "2026-05-12", "Электросталь", 3, 3),
]


class First20Source:
    def __init__(self) -> None:
        self.details = _details()
        self.goods = {
            supply_id: [{"nmID": 100000 + idx, "barcode": f"bc{idx}", "vendorCode": f"vc{idx}", "quantity": qty, "acceptedQuantity": accepted}]
            for idx, (supply_id, _date, _warehouse, qty, accepted) in enumerate(TARGET_ROWS)
        }
        self.rows = [
            {
                "supplyID": int(supply_id),
                "preorderID": int(supply_id) + 1000 if int(supply_id) in {39265540, 39265519, 39265492, 39265590, 39265571} else 0,
                "statusID": 5,
                "boxTypeID": self.details[supply_id]["boxTypeID"],
                "createDate": date + "T00:00:00+03:00",
                "supplyDate": date + "T00:00:00+03:00",
                "factDate": date + "T01:00:00+03:00",
                "updatedDate": date + "T02:00:00+03:00",
            }
            for supply_id, date, _warehouse, _qty, _accepted in TARGET_ROWS
        ]

    def fetch_warehouses(self):
        return [
            {"ID": 120762, "name": "Электросталь"},
            {"ID": 130744, "name": "Краснодар (Тихорецкая)"},
            {"ID": 300571, "name": "Екатеринбург - Перспективная 14"},
            {"ID": 301805, "name": "Новосемейкино"},
            {"ID": 218210, "name": "Обухово"},
            {"ID": 206968, "name": "Чехов 1, Новоселки вл 11 стр 2"},
            {"ID": 206969, "name": "Чехов 2, Новоселки вл 11 стр 7"},
        ]

    def list_supplies(self, *, limit=1000, offset=0, status_ids=None, dates=None):
        page = self.rows[offset : offset + limit]
        return WbSuppliesListResult(rows=page, raw_count=len(page), limit=limit, offset=offset, status_ids=list(status_ids or []), dates=list(dates or []))

    def fetch_supply_details(self, supply_id, *, is_preorder_id=False):
        return self.details[str(supply_id)]

    def fetch_supply_goods(self, supply_id, *, limit=1000, offset=0, is_preorder_id=False):
        return self.goods[str(supply_id)][offset : offset + limit]

    def fetch_supply_package(self, supply_id):
        return []


def main() -> None:
    with TemporaryDirectory(prefix="wb-supplies-first20-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        block = WbSuppliesBlock(runtime=runtime, source=First20Source(), timestamp_factory=_timestamp_factory())
        block.sync_supplies({"limit": 1000, "enrich": "all"})
        payload = block.list_supplies({"status_id": 5, "size_filter": "all", "limit": 20, "sort_key": "supply_date", "sort_dir": "desc"})
        rows = payload.get("rows", [])
        ids = [str(row.get("wb_supply_id")) for row in rows]
        expected_ids = [item[0] for item in TARGET_ROWS]
        if ids != expected_ids:
            raise AssertionError(f"first20 accepted order mismatch: {ids}")
        by_id = {str(row.get("wb_supply_id")): row for row in rows}
        for supply_id, _date, warehouse, qty, accepted in TARGET_ROWS:
            row = by_id[supply_id]
            if row.get("warehouse_display") != warehouse:
                raise AssertionError(f"{supply_id}: warehouse mismatch {row}")
            if row.get("quantity_added") != qty or row.get("accepted_quantity") != accepted:
                raise AssertionError(f"{supply_id}: quantity mismatch {row}")
            if "Тип 0" in str(row.get("type_label") or ""):
                raise AssertionError(f"{supply_id}: technical type leaked {row}")
            if "→" not in warehouse and row.get("cost_total") != 0:
                raise AssertionError(f"{supply_id}: non-transit accepted row must show zero cost {row}")
        print("wb_supplies_first20_parity_smoke: OK")


def _details() -> dict[str, dict[str, object]]:
    details: dict[str, dict[str, object]] = {}
    warehouse_ids = {
        "Электросталь": 120762,
        "Краснодар (Тихорецкая)": 130744,
        "Екатеринбург - Перспективная 14": 300571,
        "Новосемейкино": 301805,
        "Склад Шушары": 50045246,
    }
    transit_ids = {
        "Обухово": 218210,
        "Чехов 1, Новоселки вл 11 стр 2": 206968,
        "Чехов 2, Новоселки вл 11 стр 7": 206969,
    }
    for supply_id, date, warehouse, qty, accepted in TARGET_ROWS:
        if "→" in warehouse:
            source_name, dest_name = [part.strip() for part in warehouse.split("→", 1)]
            details[supply_id] = {
                "supplyID": int(supply_id),
                "statusID": 5,
                "boxTypeID": 1,
                "warehouseID": warehouse_ids[source_name],
                "warehouseName": source_name,
                "actualWarehouseID": transit_ids[dest_name],
                "actualWarehouseName": dest_name,
                "transitWarehouseID": transit_ids[dest_name],
                "transitWarehouseName": dest_name,
                "quantity": qty,
                "acceptedQuantity": accepted,
                "acceptanceCost": 0,
                "paidAcceptanceCoefficient": 0,
                "supplyDate": date + "T00:00:00+03:00",
                "factDate": date + "T01:00:00+03:00",
            }
        else:
            details[supply_id] = {
                "supplyID": int(supply_id),
                "statusID": 5,
                "boxTypeID": 0 if qty < 250 else 1,
                "virtualTypeID": 5 if qty < 250 else None,
                "warehouseID": warehouse_ids[warehouse],
                "warehouseName": warehouse,
                "quantity": qty,
                "acceptedQuantity": accepted,
                "acceptanceCost": None if qty < 250 else 0,
                "paidAcceptanceCoefficient": 0,
                "supplyDate": date + "T00:00:00+03:00",
                "factDate": date + "T01:00:00+03:00",
            }
    return details


def _timestamp_factory():
    counter = {"value": 0}

    def _next() -> str:
        counter["value"] += 1
        return f"2026-06-10T00:00:{counter['value']:02d}Z"

    return _next


if __name__ == "__main__":
    main()
