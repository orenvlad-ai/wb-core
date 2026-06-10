"""Smoke-check WB supplies detail route normalized goods composition."""

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


class LazyGoodsSource:
    def __init__(self) -> None:
        self.detail_calls: list[str] = []
        self.goods_calls: list[str] = []
        self.package_calls: list[str] = []

    def fetch_warehouses(self):
        return [{"ID": 50045246, "name": "Склад Шушары"}, {"ID": 218210, "name": "Обухово"}]

    def list_supplies(self, *, limit=1000, offset=0, status_ids=None, dates=None):
        rows = [
            {
                "supplyID": 39265492,
                "preorderID": 51162081,
                "statusID": 5,
                "boxTypeID": 1,
                "supplyDate": "2026-05-15T00:00:00+03:00",
                "factDate": "2026-05-15T14:56:38+03:00",
                "updatedDate": "2026-05-17T16:22:08+03:00",
            }
        ]
        page = rows[offset : offset + limit]
        return WbSuppliesListResult(rows=page, raw_count=len(page), limit=limit, offset=offset, status_ids=list(status_ids or []), dates=list(dates or []))

    def fetch_supply_details(self, supply_id, *, is_preorder_id=False):
        self.detail_calls.append(str(supply_id))
        return {
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
            "unloadingQuantity": 0,
            "readyForSaleQuantity": 7483,
            "acceptanceCost": 0,
            "paidAcceptanceCoefficient": 0,
        }

    def fetch_supply_goods(self, supply_id, *, limit=1000, offset=0, is_preorder_id=False):
        self.goods_calls.append(str(supply_id))
        rows = [
            {
                "nmID": 111,
                "barcode": "460000000001",
                "vendorCode": "SKU-1",
                "techSize": "0",
                "color": "чёрный",
                "quantity": 2500,
                "acceptedQuantity": 2494,
                "unloadingQuantity": 0,
                "readyForSaleQuantity": 2494,
            },
            {
                "nmID": 222,
                "barcode": "460000000002",
                "vendorCode": "SKU-2",
                "techSize": "M",
                "color": "белый",
                "quantity": 5000,
                "acceptedQuantity": 4989,
                "unloadingQuantity": 0,
                "readyForSaleQuantity": 4989,
            },
        ]
        return rows[offset : offset + limit]

    def fetch_supply_package(self, supply_id):
        self.package_calls.append(str(supply_id))
        return [{"packageCode": "WB_1", "quantity": 2, "barcodes": [{"barcode": "460000000001", "quantity": 1}]}]


def main() -> None:
    with TemporaryDirectory(prefix="wb-supplies-goods-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        source = LazyGoodsSource()
        block = WbSuppliesBlock(runtime=runtime, source=source, timestamp_factory=_timestamp_factory())
        block.sync_supplies({"limit": 1000, "enrich": "none"})
        if source.detail_calls or source.goods_calls:
            raise AssertionError("list-only sync must not fetch detail/goods")
        detail = block.get_supply("39265492")
        if source.detail_calls != ["39265492"] or source.goods_calls != ["39265492"]:
            raise AssertionError(f"detail route must lazy fetch one supply, got {source.detail_calls} {source.goods_calls}")
        supply = detail.get("supply", {})
        goods = detail.get("goods", [])
        summary = detail.get("goods_summary", {})
        if supply.get("warehouse_display") != "Склад Шушары → Обухово":
            raise AssertionError(f"detail must normalize route, got {supply}")
        if detail.get("composition_status") != "available" or len(goods) != 2:
            raise AssertionError(f"detail must expose normalized goods, got {detail}")
        if goods[0].get("nm_id") != 111 or goods[0].get("barcode") != "460000000001" or goods[0].get("vendor_code") != "SKU-1":
            raise AssertionError(f"goods schema mismatch: {goods[0]}")
        if summary.get("total_quantity") != 7500 or summary.get("total_accepted_quantity") != 7483:
            raise AssertionError(f"goods totals mismatch: {summary}")
        cached = block.get_supply("39265492")
        if source.detail_calls != ["39265492"] or source.goods_calls != ["39265492"]:
            raise AssertionError("second detail call must use cached composition")
        if cached.get("goods_summary", {}).get("goods_row_count") != 2:
            raise AssertionError(f"cached detail must preserve goods composition: {cached}")
        print("wb_supplies_goods_composition_smoke: OK")


def _timestamp_factory():
    counter = {"value": 0}

    def _next() -> str:
        counter["value"] += 1
        return f"2026-06-10T00:01:{counter['value']:02d}Z"

    return _next


if __name__ == "__main__":
    main()
