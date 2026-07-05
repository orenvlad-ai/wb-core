"""Regression smoke for WB supplies status/accepted quantity refresh lanes."""

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


class StatusAcceptedRefreshSource:
    def __init__(self) -> None:
        self.phase = "initial"
        self.detail_calls: list[str] = []
        self.goods_calls: list[str] = []
        self.list_calls: list[list[int]] = []
        self.initial_rows = [
            _row(40436428, status_id=3, quantity=5750, updated_at="2026-07-01T10:00:00+03:00"),
            _row(40421940, status_id=3, quantity=300, updated_at="2026-07-01T10:00:00+03:00", cost=45846.09),
            _row(40431461, status_id=5, quantity=36500, updated_at="2026-07-01T10:00:00+03:00"),
        ]
        self.fresh_rows = [
            _row(40436428, status_id=5, quantity=5750, updated_at="2026-07-02T10:00:00+03:00"),
            _row(40421940, status_id=6, quantity=300, updated_at="2026-07-02T10:00:00+03:00", cost=45846.09),
            _row(40431461, status_id=5, quantity=36500, updated_at="2026-07-02T10:00:00+03:00"),
        ]
        self.accepted_by_phase = {
            "initial": {"40436428": 0, "40421940": 0, "40431461": 36420},
            "fresh": {"40436428": 5749, "40421940": 0, "40431461": 36432},
        }

    def advance(self) -> None:
        self.phase = "fresh"

    def fetch_warehouses(self):
        return [{"ID": 507, "name": "Коледино"}]

    def fetch_marketplace_offices(self):
        return [{"name": "Коледино", "federalDistrict": "Центральный федеральный округ"}]

    def fetch_box_tariffs(self, *, tariff_date=None):
        return []

    def list_supplies(self, *, limit=100, offset=0, status_ids=None, dates=None):
        statuses = [int(item) for item in status_ids or []]
        self.list_calls.append(statuses)
        if self.phase == "initial":
            rows = list(self.initial_rows)
        elif statuses:
            wanted = set(statuses)
            rows = [row for row in self.fresh_rows if int(row.get("statusID") or 0) in wanted]
        else:
            rows = []
        return WbSuppliesListResult(
            rows=rows[offset : offset + limit],
            raw_count=len(rows[offset : offset + limit]),
            limit=limit,
            offset=offset,
            status_ids=statuses,
            dates=list(dates or []),
        )

    def fetch_supply_details(self, supply_id, *, is_preorder_id=False):
        self.detail_calls.append(str(supply_id))
        row = next(item for item in [*self.initial_rows, *self.fresh_rows] if str(item["supplyID"]) == str(supply_id))
        accepted = self.accepted_by_phase[self.phase][str(supply_id)]
        stale_status = 3 if self.phase == "fresh" and str(supply_id) in {"40436428", "40421940"} else row["statusID"]
        stale_accepted = 36420 if self.phase == "fresh" and str(supply_id) == "40431461" else accepted
        return {
            **row,
            "statusID": stale_status,
            "acceptedQuantity": stale_accepted,
            "acceptanceCost": row.get("costTotal", 0),
            "paidAcceptanceCoefficient": 0,
        }

    def fetch_supply_goods(self, supply_id, *, limit=1000, offset=0, is_preorder_id=False):
        self.goods_calls.append(str(supply_id))
        row = next(item for item in [*self.initial_rows, *self.fresh_rows] if str(item["supplyID"]) == str(supply_id))
        accepted = self.accepted_by_phase[self.phase][str(supply_id)]
        return [{"nmID": 210183919, "quantity": row["quantity"], "acceptedQuantity": accepted}][offset : offset + limit]

    def fetch_supply_package(self, supply_id):
        return []


def main() -> None:
    with TemporaryDirectory(prefix="wb-supplies-status-accepted-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        source = StatusAcceptedRefreshSource()
        block = WbSuppliesBlock(runtime=runtime, source=source, timestamp_factory=_timestamp_factory())

        seed = block.sync_supplies({"limit": 1000})
        if seed.get("sync", {}).get("new_rows") != 3:
            raise AssertionError(f"seed must load all regression rows, got {seed.get('sync')}")

        runtime.upsert_wb_supply_transit_cost_enrichment(
            {
                "supply_id": "40421940",
                "amount": 45846.09,
                "currency": "RUB",
                "amount_label": "45 846,09 ₽",
                "is_transit": True,
                "source": "seller_portal_browser",
                "evidence_type": "network_json",
                "confidence": "high",
                "fetched_at": "2026-07-01T12:00:00Z",
                "status": "success",
                "error": "",
                "source_endpoint_path": "/api/v1/supply/cost",
                "created_at": "2026-07-01T12:00:00Z",
                "updated_at": "2026-07-01T12:00:00Z",
            }
        )

        source.detail_calls.clear()
        source.goods_calls.clear()
        source.advance()
        refreshed = block.sync_supplies({"limit": 1000})
        sync = refreshed.get("sync", {})
        rows = {row["wb_supply_id"]: row for row in block.list_supplies({"size_filter": "all", "limit": 100}).get("rows", [])}

        if sync.get("recent_historical_status_ids") != [5, 6] or [5, 6] not in source.list_calls:
            raise AssertionError(f"ordinary sync must fetch recent historical status slice, got {sync} {source.list_calls}")
        if sync.get("refreshed_recent_historical_rows") != 3 or sync.get("accepted_qty_changed_rows") != 2:
            raise AssertionError(f"ordinary sync must refresh historical rows and count accepted changes, got {sync}")
        if rows["40436428"].get("status_id") != 5 or rows["40436428"].get("accepted_quantity") != 5749:
            raise AssertionError(f"3 -> 5 row must use fresh list status and accepted qty, got {rows['40436428']}")
        if rows["40421940"].get("status_id") != 6 or rows["40421940"].get("effective_cost_total") != 45846.09:
            raise AssertionError(f"3 -> 6 row must update status and preserve cost enrichment, got {rows['40421940']}")
        if rows["40431461"].get("status_id") != 5 or rows["40431461"].get("accepted_quantity") != 36432:
            raise AssertionError(f"same-status accepted qty change must use fresh goods over stale detail, got {rows['40431461']}")
        if source.detail_calls != ["40436428", "40421940", "40431461"]:
            raise AssertionError(f"fresh historical rows must be detail-refreshed once, got {source.detail_calls}")
        if source.goods_calls != ["40436428", "40421940", "40431461"]:
            raise AssertionError(f"fresh historical rows must be goods-refreshed once, got {source.goods_calls}")

    print("wb_supplies_status_accepted_refresh_smoke: OK")


def _row(supply_id: int, *, status_id: int, quantity: int, updated_at: str, cost: float | None = None) -> dict[str, object]:
    row: dict[str, object] = {
        "supplyID": supply_id,
        "preorderID": supply_id + 1000,
        "createDate": "2026-07-01T09:00:00+03:00",
        "supplyDate": "2026-07-03T00:00:00+03:00",
        "updatedDate": updated_at,
        "statusID": status_id,
        "warehouseID": 507,
        "warehouseName": "Коледино",
        "quantity": quantity,
        "boxTypeID": 1,
    }
    if cost is not None:
        row["costTotal"] = cost
    return row


def _timestamp_factory():
    counter = {"value": 0}

    def _next() -> str:
        counter["value"] += 1
        return f"2026-07-02T01:00:{counter['value']:02d}Z"

    return _next


if __name__ == "__main__":
    main()
