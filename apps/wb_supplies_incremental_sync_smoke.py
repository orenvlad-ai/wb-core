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
        self.list_calls: list[dict[str, object]] = []
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
        self.list_calls.append({"limit": limit, "offset": offset, "status_ids": list(status_ids or []), "dates": list(dates or [])})
        rows = self.rows
        if status_ids:
            wanted = {int(item) for item in status_ids}
            rows = [row for row in rows if int(row.get("statusID") or 0) in wanted]
        page = rows[offset : offset + limit]
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
        quantity = row.get("quantity", 0)
        return {
            "supplyID": row["supplyID"],
            "statusID": row["statusID"],
            "warehouseID": row["warehouseID"],
            "warehouseName": row["warehouseName"],
            "actualWarehouseID": row["warehouseID"],
            "actualWarehouseName": row["warehouseName"],
            "quantity": quantity,
            "acceptedQuantity": max(0, quantity - 5),
            "acceptanceCost": 0,
        }

    def fetch_supply_goods(self, supply_id, *, limit=1000, offset=0, is_preorder_id=False):
        self.goods_calls.append(str(supply_id))
        row = next(item for item in self.rows if str(item["supplyID"]) == str(supply_id))
        quantity = row.get("quantity", 0)
        return [{"quantity": quantity, "acceptedQuantity": max(0, quantity - 5)}]

    def fetch_supply_package(self, supply_id):
        return []


class TargetedPlannedSource(IncrementalSource):
    def list_supplies(self, *, limit=100, offset=0, status_ids=None, dates=None):
        self.list_calls.append({"limit": limit, "offset": offset, "status_ids": list(status_ids or []), "dates": list(dates or [])})
        if status_ids:
            wanted = {int(item) for item in status_ids}
            rows = [row for row in self.rows if int(row.get("statusID") or 0) in wanted]
        else:
            rows = [row for row in self.rows if int(row.get("statusID") or 0) != 2]
        page = rows[offset : offset + limit]
        return WbSuppliesListResult(
            rows=page,
            raw_count=len(page),
            limit=limit,
            offset=offset,
            status_ids=list(status_ids or []),
            dates=list(dates or []),
        )


class ActiveDeletionSource(IncrementalSource):
    def __init__(self) -> None:
        super().__init__()
        self.rows = [
            {
                "supplyID": 8001,
                "preorderID": 8601,
                "supplyDate": "2026-06-20T00:00:00+03:00",
                "updatedDate": "2026-06-10T12:00:00+03:00",
                "statusID": 2,
                "warehouseID": 777,
                "warehouseName": "Электросталь",
                "quantity": 120,
            },
            {
                "supplyID": 8002,
                "preorderID": 8602,
                "supplyDate": "2026-05-20T00:00:00+03:00",
                "factDate": "2026-05-20T12:00:00+03:00",
                "updatedDate": "2026-05-20T12:00:00+03:00",
                "statusID": 5,
                "warehouseID": 507,
                "warehouseName": "Коледино",
                "quantity": 700,
            },
        ]


class ActiveUpdateSource(IncrementalSource):
    def __init__(self) -> None:
        super().__init__()
        self.rows = [
            {
                "supplyID": 9001,
                "preorderID": 9601,
                "supplyDate": "2026-06-20T00:00:00+03:00",
                "updatedDate": "2026-06-10T12:00:00+03:00",
                "statusID": 2,
                "warehouseID": 777,
                "warehouseName": "Электросталь",
                "quantity": 1,
            },
        ]


class LedgerDebitSource(IncrementalSource):
    def __init__(self, nm_id: int) -> None:
        super().__init__()
        self.nm_id = int(nm_id)
        self.rows = [
            {
                "supplyID": 9101,
                "preorderID": 9701,
                "supplyDate": "2026-06-10T14:00:00Z",
                "updatedDate": "2026-06-10T14:00:00Z",
                "statusID": 5,
                "warehouseID": 507,
                "warehouseName": "Коледино",
                "quantity": 3,
            },
            {
                "supplyID": 9102,
                "preorderID": 9702,
                "supplyDate": "2026-06-10T15:00:03Z",
                "updatedDate": "2026-06-10T15:00:03Z",
                "statusID": 5,
                "warehouseID": 507,
                "warehouseName": "Коледино",
                "quantity": 4,
            },
        ]

    def fetch_supply_goods(self, supply_id, *, limit=1000, offset=0, is_preorder_id=False):
        self.goods_calls.append(str(supply_id))
        row = next(item for item in self.rows if str(item["supplyID"]) == str(supply_id))
        return [{"nmID": self.nm_id, "quantity": row.get("quantity", 0)}]


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
            or second_sync.get("enriched_active_rows") != 0
            or second_sync.get("refreshed_recent_historical_rows") != 0
            or second_sync.get("upserted_count") != 0
        ):
            raise AssertionError(f"second incremental must skip unchanged already-enriched rows, got {second_sync}")
        if source.detail_calls or source.goods_calls:
            raise AssertionError(f"second incremental must not refresh unchanged rows, got {source.detail_calls} {source.goods_calls}")
        source.detail_calls.clear()
        source.goods_calls.clear()

        source.rows[1]["updatedDate"] = "2026-06-10T13:00:00+03:00"
        source.rows[1]["statusID"] = 5
        changed = block.sync_supplies({"limit": 1000})
        changed_sync = changed.get("sync", {})
        if (
            changed_sync.get("changed_rows") != 1
            or changed_sync.get("unchanged_rows") != 1
            or changed_sync.get("enriched") != 1
            or changed_sync.get("refreshed_recent_historical_rows") != 1
            or source.detail_calls != ["7002"]
        ):
            raise AssertionError(f"changed row must be upserted/enriched only once, got {changed_sync}")

    with TemporaryDirectory(prefix="wb-supplies-incremental-planned-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        source = TargetedPlannedSource()
        block = WbSuppliesBlock(runtime=runtime, source=source, timestamp_factory=_timestamp_factory())

        planned = block.sync_supplies({"limit": 1000})
        planned_sync = planned.get("sync", {})
        planned_rows = block.list_supplies({"status_ids": "2", "size_filter": "all"}).get("rows", [])
        if (
            planned_sync.get("new_rows") != 2
            or planned_sync.get("targeted_status_ids") != [1, 2, 3, 4]
            or planned_sync.get("targeted_raw_fetched_count") != 1
            or [row.get("wb_supply_id") for row in planned_rows] != ["7002"]
            or not any(call["status_ids"] == [1, 2, 3, 4] for call in source.list_calls)
        ):
            raise AssertionError(f"targeted planned refresh must upsert planned rows, got {planned_sync} {planned_rows} {source.list_calls}")

    with TemporaryDirectory(prefix="wb-supplies-active-delete-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        source = ActiveDeletionSource()
        block = WbSuppliesBlock(runtime=runtime, source=source, timestamp_factory=_timestamp_factory())

        initial = block.sync_supplies({"limit": 1000})
        if initial.get("sync", {}).get("new_rows") != 2:
            raise AssertionError(f"active deletion seed must load planned and accepted rows, got {initial.get('sync')}")
        source.rows = []
        deleted = block.sync_supplies({"limit": 1000})
        deleted_sync = deleted.get("sync", {})
        remaining_ids = [row.get("wb_supply_id") for row in block.list_supplies({"size_filter": "all"}).get("rows", [])]
        deleted_record = runtime.load_wb_supply_record("8001")
        accepted_record = runtime.load_wb_supply_record("8002")
        if (
            deleted_sync.get("deleted_active_rows") != 1
            or remaining_ids != ["8002"]
            or deleted_record is not None
            or accepted_record is None
            or accepted_record.get("raw_goods") is None
            or deleted_sync.get("skipped_historical_absent") != 1
        ):
            raise AssertionError(f"active reconcile must delete absent active row and preserve historical row, got {deleted_sync} {remaining_ids}")

    with TemporaryDirectory(prefix="wb-supplies-active-update-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        source = ActiveUpdateSource()
        block = WbSuppliesBlock(runtime=runtime, source=source, timestamp_factory=_timestamp_factory())

        first = block.sync_supplies({"limit": 1000})
        first_rows = block.list_supplies({"status_ids": "2", "size_filter": "all"}).get("rows", [])
        if first.get("sync", {}).get("new_rows") != 1 or first_rows[0].get("quantity_for_size_filter") != 1:
            raise AssertionError(f"active update seed must load planned qty=1, got {first.get('sync')} {first_rows}")
        source.detail_calls.clear()
        source.goods_calls.clear()
        source.rows[0]["quantity"] = 300
        source.rows[0]["supplyDate"] = "2026-06-21T00:00:00+03:00"
        updated = block.sync_supplies({"limit": 1000})
        updated_sync = updated.get("sync", {})
        updated_all = block.list_supplies({"status_ids": "2", "size_filter": "all"}).get("rows", [])
        updated_main = block.list_supplies({"status_ids": "2", "size_filter": "main_250"}).get("rows", [])
        updated_small = block.list_supplies({"status_ids": "2", "size_filter": "small_lt_250"}).get("rows", [])
        goods = runtime.load_wb_supply_record("9001").get("raw_goods")
        if (
            updated_sync.get("changed_rows") != 1
            or updated_sync.get("changed_active_rows") != 1
            or updated_sync.get("enriched_active_rows") != 1
            or updated_all[0].get("quantity_for_size_filter") != 300
            or not str(updated_all[0].get("supply_date") or "").startswith("2026-06-21")
            or [row.get("wb_supply_id") for row in updated_main] != ["9001"]
            or updated_small
            or goods != [{"quantity": 300, "acceptedQuantity": 295}]
        ):
            raise AssertionError(f"active reconcile must update date/quantity/goods and size filters, got {updated_sync} {updated_all} {goods}")

    with TemporaryDirectory(prefix="wb-supplies-incremental-missing-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        source = IncrementalSource()
        for row in source.rows:
            row.pop("quantity", None)
        block = WbSuppliesBlock(runtime=runtime, source=source, timestamp_factory=_timestamp_factory())

        backfill_run = block.run_full_backfill({"limit": 1000, "enrich": False, "run_id": "missing-critical-backfill"})
        if backfill_run.get("status") != "success" or backfill_run.get("enriched") != 0:
            raise AssertionError(f"list-only backfill must not enrich missing rows, got {backfill_run}")
        source.detail_calls.clear()
        source.goods_calls.clear()

        latest = block.sync_supplies({"limit": 1000})
        latest_sync = latest.get("sync", {})
        if (
            latest_sync.get("new_rows") != 0
            or latest_sync.get("changed_rows") != 2
            or latest_sync.get("unchanged_rows") != 0
            or latest_sync.get("changed_active_rows") != 1
            or latest_sync.get("enriched_active_rows") != 1
            or latest_sync.get("refreshed_recent_historical_rows") != 1
            or latest_sync.get("enriched") != 2
            or latest_sync.get("upserted_count") != 2
            or source.detail_calls != ["7001", "7002"]
            or source.goods_calls != ["7001", "7002"]
        ):
            raise AssertionError(f"default incremental must reconcile active and recent historical missing rows, got {latest_sync}")
        source.detail_calls.clear()
        source.goods_calls.clear()

        explicit = block.sync_supplies({"limit": 1000, "enrich": "missing_critical"})
        explicit_sync = explicit.get("sync", {})
        if (
            explicit_sync.get("unchanged_rows") != 2
            or explicit_sync.get("enriched") != 0
            or source.detail_calls
            or source.goods_calls
        ):
            raise AssertionError(f"explicit missing-critical enrichment must skip already repaired rows, got {explicit_sync}")

    with TemporaryDirectory(prefix="wb-supplies-ledger-checkpoint-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        nm_id = 123456
        runtime.create_ff_stock_operation(
            operation_id="ffso_opening_smoke",
            operation_type="manual_receipt",
            source_type="manual_excel",
            source_key="manual_excel:opening-smoke",
            source_object_id="opening-smoke",
            source_object_label="opening-smoke.xlsx",
            created_at="2026-06-10T14:59:00Z",
            created_by="smoke",
            lines=[{"nm_id": nm_id, "quantity_delta": 20}],
        )
        runtime.save_wb_supply_rows(
            rows=[
                {
                    "supply_id": "9101",
                    "cache_key": "supply:9101",
                    "wb_supply_id": "9101",
                    "preorder_id": "9701",
                    "number_label": "9101",
                    "status_id": 5,
                    "status_label": "Принято",
                    "warehouse_id": "507",
                    "warehouse_name": "Коледино",
                    "supply_date": "2026-06-10T14:00:00Z",
                    "source_created_at": "2026-06-10T14:00:00Z",
                    "raw_list": {
                        "supplyID": 9101,
                        "preorderID": 9701,
                        "supplyDate": "2026-06-10T14:00:00Z",
                        "updatedDate": "2026-06-10T14:00:00Z",
                        "statusID": 5,
                    },
                    "raw_goods": [{"nmID": nm_id, "quantity": 3}],
                    "raw_package": [],
                }
            ],
            warehouses=[{"warehouse_id": "507", "warehouse_name": "Коледино"}],
            synced_at="2026-06-10T14:58:00Z",
        )
        source = LedgerDebitSource(nm_id)
        block = WbSuppliesBlock(runtime=runtime, source=source, timestamp_factory=_timestamp_factory())

        first = block.sync_supplies({"limit": 1000})
        first_sync = first.get("sync", {})
        first_debits = first_sync.get("ff_stock_debits") or {}
        checkpoint = first_sync.get("ff_auto_writeoff_checkpoint") or {}
        balance_after_first = _balance(runtime, nm_id)
        if (
            checkpoint.get("baseline_record_count") != 1
            or first_debits.get("created_count") != 1
            or first_debits.get("skipped_reasons", {}).get("wb_supply_before_auto_writeoff_checkpoint") != 1
            or balance_after_first != 16.0
        ):
            raise AssertionError(
                f"sync must skip baseline-known WB supply and debit one post-checkpoint supply, "
                f"got checkpoint={checkpoint} debits={first_debits} balance={balance_after_first}"
            )
        operations = runtime.list_ff_stock_operations(limit=20)
        wb_ops = [item for item in operations if item.get("source_type") == "wb_supply"]
        if len(wb_ops) != 1 or wb_ops[0].get("source_object_id") != "9102":
            raise AssertionError(f"only post-checkpoint supply 9102 must create WB debit, got {wb_ops}")

        second = block.sync_supplies({"limit": 1000})
        second_debits = (second.get("sync") or {}).get("ff_stock_debits") or {}
        if second_debits.get("created_count") != 0 or _balance(runtime, nm_id) != 16.0:
            raise AssertionError(f"repeated sync must not duplicate WB debit, got {second_debits}")

    print("wb_supplies_incremental_sync_smoke: OK")


def _timestamp_factory():
    counter = {"value": 0}

    def _next() -> str:
        counter["value"] += 1
        return f"2026-06-10T15:00:{counter['value']:02d}Z"

    return _next


def _balance(runtime: RegistryUploadDbBackedRuntime, nm_id: int) -> float:
    balances = {
        int(item.get("nm_id") or 0): float(item.get("balance") or 0.0)
        for item in runtime.list_ff_stock_balances()
    }
    return float(balances.get(int(nm_id), 0.0))


if __name__ == "__main__":
    main()
