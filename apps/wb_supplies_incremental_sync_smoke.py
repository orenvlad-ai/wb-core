"""Smoke-check for WB supplies incremental latest-window sync."""

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


class IncrementalSource:
    def __init__(self) -> None:
        self.detail_calls: list[str] = []
        self.goods_calls: list[str] = []
        self.rows = [
            {
                "supplyID": 7001,
                "preorderID": 6001,
                "supplyDate": "2026-06-10T00:00:00+03:00",
                "updatedDate": "2026-06-10T12:00:00+03:00",
                "statusID": 5,
                "warehouseID": 507,
                "warehouseName": "Коледино",
                "quantity": 700,
            },
            {
                "supplyID": 7002,
                "preorderID": 6002,
                "supplyDate": "2026-06-09T00:00:00+03:00",
                "updatedDate": "2026-06-09T12:00:00+03:00",
                "statusID": 2,
                "warehouseID": 777,
                "warehouseName": "Электросталь",
                "quantity": 120,
            },
        ]

    def fetch_warehouses(self):
        return [{"ID": 507, "name": "Коледино"}, {"ID": 777, "name": "Электросталь"}]

    def list_supplies(self, *, limit=100, offset=0, status_ids=None, dates=None):
        page = self.rows[offset : offset + limit]
        return WbSuppliesListResult(
            rows=page,
            raw_count=len(page),
            limit=limit,
            offset=offset,
            status_ids=list(status_ids or []),
            dates=list(dates or []),
        )

    def fetch_supply_details(self, supply_id, *, is_preorder_id=False):
        self.detail_calls.append(str(supply_id))
        row = next(item for item in self.rows if str(item["supplyID"]) == str(supply_id))
        return {
            "supplyID": row["supplyID"],
            "statusID": row["statusID"],
            "warehouseID": row["warehouseID"],
            "warehouseName": row["warehouseName"],
            "actualWarehouseID": row["warehouseID"],
            "actualWarehouseName": row["warehouseName"],
            "quantity": row["quantity"],
            "acceptedQuantity": row["quantity"] - 5,
            "acceptanceCost": 0,
        }

    def fetch_supply_goods(self, supply_id, *, limit=1000, offset=0, is_preorder_id=False):
        self.goods_calls.append(str(supply_id))
        row = next(item for item in self.rows if str(item["supplyID"]) == str(supply_id))
        return [{"quantity": row["quantity"], "acceptedQuantity": row["quantity"] - 5}]

    def fetch_supply_package(self, supply_id):
        return []


def main() -> None:
    with TemporaryDirectory(prefix="wb-supplies-incremental-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        source = IncrementalSource()
        block = WbSuppliesBlock(runtime=runtime, source=source, timestamp_factory=_timestamp_factory())

        first = block.sync_supplies({"limit": 1000})
        first_sync = first.get("sync", {})
        if (
            first_sync.get("new_rows") != 2
            or first_sync.get("changed_rows") != 0
            or first_sync.get("unchanged_rows") != 0
            or first_sync.get("enriched") != 2
        ):
            raise AssertionError(f"first incremental must enrich new rows, got {first_sync}")
        if source.detail_calls != ["7001", "7002"] or source.goods_calls != ["7001", "7002"]:
            raise AssertionError(f"first incremental must call detail/goods once per new row, got {source.detail_calls} {source.goods_calls}")

        source.detail_calls.clear()
        source.goods_calls.clear()
        second = block.sync_supplies({"limit": 1000})
        second_sync = second.get("sync", {})
        if (
            second_sync.get("new_rows") != 0
            or second_sync.get("changed_rows") != 0
            or second_sync.get("unchanged_rows") != 2
            or second_sync.get("enriched") != 0
            or second_sync.get("upserted_count") != 0
        ):
            raise AssertionError(f"second incremental must skip unchanged enrichment, got {second_sync}")
        if source.detail_calls or source.goods_calls:
            raise AssertionError(f"second incremental must not call detail/goods for unchanged rows, got {source.detail_calls} {source.goods_calls}")

        source.rows[1]["updatedDate"] = "2026-06-10T13:00:00+03:00"
        source.rows[1]["statusID"] = 5
        changed = block.sync_supplies({"limit": 1000})
        changed_sync = changed.get("sync", {})
        if (
            changed_sync.get("changed_rows") != 1
            or changed_sync.get("unchanged_rows") != 1
            or changed_sync.get("enriched") != 1
            or source.detail_calls != ["7002"]
        ):
            raise AssertionError(f"changed row must be upserted/enriched only once, got {changed_sync}")

    print("wb_supplies_incremental_sync_smoke: OK")


def _timestamp_factory():
    counter = {"value": 0}

    def _next() -> str:
        counter["value"] += 1
        return f"2026-06-10T01:00:{counter['value']:02d}Z"

    return _next


if __name__ == "__main__":
    main()
