"""Smoke-check for WB supplies full backfill pagination/resume semantics."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_supplies import WbSuppliesListResult, WbSuppliesTransportError  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.wb_supplies import WbSuppliesBlock  # noqa: E402


class PagedWbSuppliesSource:
    def __init__(self, *, fail_offset: int | None = None, fail_once: bool = False) -> None:
        self.fail_offset = fail_offset
        self.fail_once = fail_once
        self.failed_offsets: set[int] = set()
        self.list_calls: list[dict[str, int]] = []
        self.rows = [
            {
                "supplyID": 9000 + idx,
                "preorderID": 8000 + idx,
                "createDate": f"2026-05-{(idx % 28) + 1:02d}T10:00:00+03:00",
                "supplyDate": f"2026-06-{(idx % 28) + 1:02d}T00:00:00+03:00",
                "updatedDate": f"2026-06-{(idx % 28) + 1:02d}T12:00:00+03:00",
                "statusID": 5,
                "warehouseID": 507,
                "warehouseName": "Коледино",
                "quantity": 300 + idx,
            }
            for idx in range(25)
        ]

    def fetch_warehouses(self):
        return [{"ID": 507, "name": "Коледино"}]

    def list_supplies(self, *, limit=100, offset=0, status_ids=None, dates=None):
        self.list_calls.append({"limit": int(limit), "offset": int(offset)})
        if self.fail_offset is not None and offset == self.fail_offset and (not self.fail_once or offset not in self.failed_offsets):
            self.failed_offsets.add(offset)
            raise WbSuppliesTransportError("temporary non-JSON upstream body", status_code=502, content_type="text/html", body_prefix="<html>bad gateway</html>")
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
        return {
            "supplyID": int(supply_id),
            "statusID": 5,
            "warehouseID": 507,
            "warehouseName": "Коледино",
            "actualWarehouseID": 507,
            "actualWarehouseName": "Коледино",
            "quantity": 500,
            "acceptedQuantity": 490,
            "acceptanceCost": 0,
        }

    def fetch_supply_goods(self, supply_id, *, limit=1000, offset=0, is_preorder_id=False):
        return [{"quantity": 500, "acceptedQuantity": 490}]

    def fetch_supply_package(self, supply_id):
        return []


def main() -> None:
    with TemporaryDirectory(prefix="wb-supplies-backfill-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        source = PagedWbSuppliesSource()
        block = WbSuppliesBlock(runtime=runtime, source=source, timestamp_factory=_timestamp_factory())
        first_run = block.run_full_backfill({"limit": 10, "start_offset": 0, "resume": False, "enrich": True})
        if first_run.get("status") != "success" or first_run.get("pages_fetched") != 3:
            raise AssertionError(f"full backfill must finish after short page, got {first_run}")
        state = runtime.load_wb_supplies_sync_state()
        if not state.get("backfill_complete") or state.get("highest_synced_offset") != 25:
            raise AssertionError(f"backfill state must mark completion at offset 25, got {state}")
        rows = block.list_supplies({"size_filter": "all", "limit": 20, "offset": 20})
        if rows.get("pagination", {}).get("total") != 25 or len(rows.get("rows", [])) != 5:
            raise AssertionError(f"list sorting/pagination must apply to all cached rows, got {rows.get('pagination')}")

    with TemporaryDirectory(prefix="wb-supplies-backfill-resume-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        source = PagedWbSuppliesSource(fail_offset=10)
        block = WbSuppliesBlock(runtime=runtime, source=source, timestamp_factory=_timestamp_factory())
        partial = block.run_full_backfill({"limit": 10, "start_offset": 0, "resume": False, "enrich": False})
        if partial.get("status") != "partial" or partial.get("raw_fetched") != 10:
            raise AssertionError(f"temporary upstream failure must keep partial cache, got {partial}")
        state = runtime.load_wb_supplies_sync_state()
        if state.get("backfill_complete") or state.get("highest_synced_offset") != 10:
            raise AssertionError(f"partial state must be resumable at offset 10, got {state}")
        source.fail_offset = None
        resumed = block.run_full_backfill({"limit": 10, "resume": True, "enrich": False})
        if resumed.get("status") != "success" or resumed.get("raw_fetched") != 15:
            raise AssertionError(f"resume must continue from saved offset, got {resumed}")
        payload = block.list_supplies({"size_filter": "all", "limit": 20})
        if payload.get("summary", {}).get("cached_total_rows") != 25:
            raise AssertionError(f"resume must dedupe/upsert all rows, got {payload.get('summary')}")

    print("wb_supplies_backfill_smoke: OK")


def _timestamp_factory():
    counter = {"value": 0}

    def _next() -> str:
        counter["value"] += 1
        return f"2026-06-10T00:00:{counter['value']:02d}Z"

    return _next


if __name__ == "__main__":
    main()
