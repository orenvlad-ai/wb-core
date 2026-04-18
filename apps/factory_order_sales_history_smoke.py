"""Targeted smoke-check for runtime-backed factory-order sales history coverage and reconcile helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.factory_order_sales_history import (
    FactoryOrderAuthoritativeSalesHistory,
    describe_runtime_sales_history_coverage,
    extract_data_vitrina_order_count_window,
    load_runtime_sales_history_payloads,
    replace_runtime_sales_history_window,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryItem, SalesFunnelHistorySuccess


NOW = datetime(2026, 4, 17, 9, 0, tzinfo=timezone.utc)
CAPTURED_AT = "2026-04-17T09:00:00Z"


class FakeSalesHistoryBlock:
    def __init__(self, values_by_date: dict[str, dict[int, float]]) -> None:
        self.values_by_date = values_by_date
        self.calls: list[tuple[str, str]] = []

    def execute(self, request_obj: object) -> SimpleNamespace:
        self.calls.append((str(request_obj.date_from), str(request_obj.date_to)))
        items: list[SalesFunnelHistoryItem] = []
        for snapshot_date in sorted(self.values_by_date):
            if not (str(request_obj.date_from) <= snapshot_date <= str(request_obj.date_to)):
                continue
            for nm_id in request_obj.nm_ids:
                value = self.values_by_date[snapshot_date].get(int(nm_id))
                if value is None:
                    continue
                items.append(
                    SalesFunnelHistoryItem(
                        date=snapshot_date,
                        nm_id=int(nm_id),
                        metric="orderCount",
                        value=float(value),
                    )
                )
        return SimpleNamespace(
            result=SalesFunnelHistorySuccess(
                kind="success",
                date_from=str(request_obj.date_from),
                date_to=str(request_obj.date_to),
                count=len(items),
                items=items,
            )
        )


def main() -> None:
    values = [
        ["дата", "key", "2026-04-14", "2026-04-15", "2026-04-16"],
        ["", "total_orderCount", 30, 33, 36],
        ["", "SKU:210183919", "", "", ""],
        ["", "orderCount", 10, 11, 12],
        ["", "cartCount", 22, 23, 24],
        ["", "SKU:210184534", "", "", ""],
        ["", "orderCount", 20, 22, 24],
    ]
    window = extract_data_vitrina_order_count_window(
        values,
        date_from="2026-04-14",
        date_to="2026-04-16",
    )
    if window.sku_count != 2 or window.day_count != 3 or window.item_count != 6:
        raise AssertionError("synthetic DATA_VITRINA extraction must produce the expected compact orderCount window")
    if window.total_row_mismatch_count != 0:
        raise AssertionError("synthetic DATA_VITRINA total_orderCount row must match the extracted SKU totals")

    with TemporaryDirectory(prefix="factory-order-sales-history-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_runtime_with_polluted_rows(runtime)

        before = load_runtime_sales_history_payloads(
            runtime=runtime,
            date_from="2026-04-14",
            date_to="2026-04-16",
        )
        if sorted(before) != ["2026-04-14", "2026-04-15"]:
            raise AssertionError("polluted runtime seed must start with missing and mismatched dates")

        replace_summary = replace_runtime_sales_history_window(
            runtime=runtime,
            date_from=window.date_from,
            date_to=window.date_to,
            exact_date_payloads=window.exact_date_payloads,
            captured_at=CAPTURED_AT,
        )
        if replace_summary != {"deleted_snapshot_count": 2, "saved_snapshot_count": 3}:
            raise AssertionError(f"unexpected replace summary: {replace_summary}")

        coverage = describe_runtime_sales_history_coverage(runtime)
        if coverage.earliest_available_date != "2026-04-14" or coverage.latest_available_date != "2026-04-16":
            raise AssertionError("replace must leave an exact-date authoritative window in runtime storage")

        history = FactoryOrderAuthoritativeSalesHistory(
            runtime=runtime,
            sales_funnel_history_block=FakeSalesHistoryBlock(_window_values()),
            now_factory=lambda: NOW,
            timestamp_factory=lambda: CAPTURED_AT,
        )
        samples = history.load_order_count_samples(
            date_from="2026-04-14",
            date_to="2026-04-16",
            nm_ids=[210183919, 210184534],
        )
        if samples[210183919] != [10.0, 11.0, 12.0] or samples[210184534] != [20.0, 22.0, 24.0]:
            raise AssertionError("runtime-backed history must return exact per-date orderCount samples after replacement")

    with TemporaryDirectory(prefix="factory-order-sales-history-fill-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        partial_payloads = {
            key: value
            for key, value in window.exact_date_payloads.items()
            if key != "2026-04-16"
        }
        replace_runtime_sales_history_window(
            runtime=runtime,
            date_from="2026-04-14",
            date_to="2026-04-15",
            exact_date_payloads=partial_payloads,
            captured_at=CAPTURED_AT,
        )
        fake_history = FakeSalesHistoryBlock(_window_values())
        history = FactoryOrderAuthoritativeSalesHistory(
            runtime=runtime,
            sales_funnel_history_block=fake_history,
            now_factory=lambda: NOW,
            timestamp_factory=lambda: CAPTURED_AT,
        )
        filled_samples = history.load_order_count_samples(
            date_from="2026-04-14",
            date_to="2026-04-16",
            nm_ids=[210183919, 210184534],
        )
        if fake_history.calls != [("2026-04-16", "2026-04-16")]:
            raise AssertionError(f"missing recent date must be refetched as an exact-date batch, got {fake_history.calls}")
        if filled_samples[210183919][-1] != 12.0 or filled_samples[210184534][-1] != 24.0:
            raise AssertionError("recent auto-fill must persist and expose the newly fetched authoritative orderCount samples")
        coverage = describe_runtime_sales_history_coverage(runtime)
        if coverage.exact_date_snapshot_count != 3:
            raise AssertionError("recent auto-fill must persist the fetched snapshot into runtime coverage")

    print("data_vitrina_extract: ok -> sku_count=2, day_count=3, item_count=6")
    print("runtime_window_replace: ok -> deleted=2, saved=3, after_diff=0")
    print("recent_authoritative_fill: ok -> fetched_missing_date=2026-04-16")


def _seed_runtime_with_polluted_rows(runtime: RegistryUploadDbBackedRuntime) -> None:
    polluted = SalesFunnelHistorySuccess(
        kind="success",
        date_from="2026-04-14",
        date_to="2026-04-15",
        count=4,
        items=[
            SalesFunnelHistoryItem(date="2026-04-14", nm_id=210183919, metric="orderCount", value=999.0),
            SalesFunnelHistoryItem(date="2026-04-14", nm_id=210184534, metric="orderCount", value=998.0),
            SalesFunnelHistoryItem(date="2026-04-15", nm_id=210183919, metric="orderCount", value=997.0),
            SalesFunnelHistoryItem(date="2026-04-15", nm_id=210184534, metric="orderCount", value=996.0),
        ],
    )
    for snapshot_date, payload in {
        item.date: SalesFunnelHistorySuccess(
            kind="success",
            date_from=item.date,
            date_to=item.date,
            count=2,
            items=[
                row
                for row in polluted.items
                if row.date == item.date
            ],
        )
        for item in polluted.items
    }.items():
        runtime.save_temporal_source_snapshot(
            source_key="sales_funnel_history",
            snapshot_date=snapshot_date,
            captured_at="2026-04-16T10:00:00Z",
            payload=payload,
        )


def _window_values() -> dict[str, dict[int, float]]:
    return {
        "2026-04-14": {210183919: 10.0, 210184534: 20.0},
        "2026-04-15": {210183919: 11.0, 210184534: 22.0},
        "2026-04-16": {210183919: 12.0, 210184534: 24.0},
    }


if __name__ == "__main__":
    main()
