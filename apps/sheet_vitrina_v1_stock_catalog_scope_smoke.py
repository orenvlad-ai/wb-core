"""Offline stock catalog coverage and closed-snapshot preservation checks."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_live_plan import (
    EXECUTION_MODE_MANUAL_OPERATOR,
    SOURCE_TEMPORAL_POLICIES,
    TEMPORAL_ROLE_ACCEPTED_CLOSED,
    TEMPORAL_ROLE_ACCEPTED_CURRENT,
    TEMPORAL_SLOT_TODAY_CURRENT,
    TEMPORAL_SLOT_YESTERDAY_CLOSED,
    SheetVitrinaV1LivePlanBlock,
    _capture_live_source,
)
from packages.application.stock_catalog_scope import NOMENCLATURE_TABLE
from packages.application.stocks_block import StocksBlock
from packages.adapters.stocks_block import HistoricalCsvBackedStocksSource
from packages.adapters.seller_analytics_csv_report import SellerAnalyticsCsvReport, parse_csv_dict_rows
from packages.contracts.prices_snapshot_block import PricesSnapshotItem, PricesSnapshotSuccess
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot
from packages.contracts.stocks_block import StocksItem, StocksSuccess, StocksRequest

DAY = "2026-09-06"
NOW = datetime(2026, 9, 7, 8, tzinfo=timezone.utc)
CONFIG = [ConfigV2Item(101, True, "Visible", "test", 1)]


def stocks(day, nm_ids):
    return StocksSuccess("success", day, len(nm_ids), [
        StocksItem(nm_id, 7, 7, 0, 0, 0, 0, 0) for nm_id in nm_ids
    ], warehouse_granularity_complete=False)


def seed(runtime, rows):
    runtime.list_nomenclature_items()
    with sqlite3.connect(runtime.db_path) as conn:
        for nm_id, active, hidden in rows:
            conn.execute(
                f"INSERT INTO {NOMENCLATURE_TABLE}(item_id,is_active,is_hidden,nm_id,"
                "nomenclature_name,product_type,match_key,aliases_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'clean',?,'[]',?,?)",
                (f"nom-{nm_id}", active, hidden, nm_id, str(nm_id), str(nm_id),
                 "2026-09-07T08:00:00Z", "2026-09-07T08:00:00Z"),
            )


def slot_kwargs(day=DAY, role=TEMPORAL_ROLE_ACCEPTED_CLOSED, nm_ids=None):
    return dict(
        source_key="stocks",
        temporal_slot=(TEMPORAL_SLOT_YESTERDAY_CLOSED if role == TEMPORAL_ROLE_ACCEPTED_CLOSED
                       else TEMPORAL_SLOT_TODAY_CURRENT),
        temporal_policy=SOURCE_TEMPORAL_POLICIES["stocks"], column_date=day,
        requested_nm_ids=nm_ids or [101, 202, 303],
    )


def capture(block, loader, *, day=DAY, role=TEMPORAL_ROLE_ACCEPTED_CLOSED, nm_ids=None):
    return block._capture_temporal_source_with_acceptance(
        **slot_kwargs(day, role, nm_ids), loader=loader,
        execution_mode=EXECUTION_MODE_MANUAL_OPERATOR,
        accepted_role=role, allow_persisted_retry=False,
        current_web_source_sync_note=None,
    )


def main():
    with TemporaryDirectory(prefix="wb-stock-catalog-temporal-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        seed(runtime, [(101, 1, 0), (202, 1, 1), (303, 0, 1)])
        stock_requests, price_requests = [], []

        def execute_stocks(request):
            stock_requests.append(request)
            return SimpleNamespace(result=stocks(request.snapshot_date, request.nm_ids))

        def execute_prices(request):
            price_requests.append(request)
            return SimpleNamespace(result=PricesSnapshotSuccess(
                "success", request.snapshot_date, len(request.nm_ids),
                [PricesSnapshotItem(nm_id, 100, 90) for nm_id in request.nm_ids],
            ))

        block = SheetVitrinaV1LivePlanBlock(
            runtime, stocks_block=SimpleNamespace(execute=execute_stocks),
            prices_snapshot_block=SimpleNamespace(execute=execute_prices),
            now_factory=lambda: NOW,
        )

        def load(day, sources):
            return block._load_live_sources(
                CONFIG,
                [SheetVitrinaV1TemporalSlot(TEMPORAL_SLOT_YESTERDAY_CLOSED, "Closed", day),
                 SheetVitrinaV1TemporalSlot(TEMPORAL_SLOT_TODAY_CURRENT, "Current", "2026-09-07")],
                None, source_keys=sources, execution_mode=EXECUTION_MODE_MANUAL_OPERATOR,
            )

        loaded = load(DAY, {"stocks", "prices_snapshot"})
        assert stock_requests[0].nm_ids == [101, 202, 303]
        assert all(request.nm_ids == [101] for request in price_requests)
        stock_status = next(status for status in loaded.statuses if status.source_key == "stocks"
                            and status.temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED)
        assert stock_status.kind == "success" and stock_status.requested_count == 3
        assert stock_status.diagnostics["stock_catalog_scope"]["requested_nm_ids"] == [101, 202, 303]
        assert CONFIG == [ConfigV2Item(101, True, "Visible", "test", 1)]

        seed(runtime, [(404, 1, 0)])
        load("2026-09-05", {"stocks"})
        assert stock_requests[-1].nm_ids == [101, 202, 303, 404]

        # Accepted closed data keeps its payload and original capture time even
        # when a later catalog grows. No history loader or snapshot writer runs.
        accepted_before = runtime.load_temporal_source_slot_snapshot(
            source_key="stocks", snapshot_date=DAY, snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        )
        loader = Mock(side_effect=AssertionError("closed day must not be refetched"))
        with ExitStack() as stack:
            writes = [stack.enter_context(patch.object(runtime, name, side_effect=AssertionError(name)))
                      for name in ("save_temporal_source_snapshot", "save_temporal_source_slot_snapshot",
                                   "save_temporal_source_closure_state")]
            status, payload = capture(block, loader, nm_ids=[101, 202, 303, 404])
        assert status.kind == "incomplete" and status.missing_nm_ids == [404]
        assert status.covered_count == 3 and "historical_refetch=disabled" in status.note
        assert "stock_catalog_scope_extended_beyond_closed_snapshot" in status.note
        assert [item.nm_id for item in payload.items] == [101, 202, 303]
        loader.assert_not_called()
        assert all(not writer.called for writer in writes)
        assert runtime.load_temporal_source_slot_snapshot(
            source_key="stocks", snapshot_date=DAY, snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        ) == accepted_before

        # A raw or accepted-current 33-SKU cache cannot certify the new 92-SKU
        # request. Membership, not payload count, controls admission.
        small_ids, full_ids = list(range(1, 34)), list(range(1, 93))
        for day, role in [("2026-09-04", TEMPORAL_ROLE_ACCEPTED_CLOSED),
                          ("2026-09-07", TEMPORAL_ROLE_ACCEPTED_CURRENT)]:
            runtime.save_temporal_source_snapshot(source_key="stocks", snapshot_date=day,
                                                 captured_at="2026-09-07T08:00:00Z",
                                                 payload=stocks(day, small_ids))
            if role == TEMPORAL_ROLE_ACCEPTED_CURRENT:
                runtime.save_temporal_source_slot_snapshot(source_key="stocks", snapshot_date=day,
                    snapshot_role=role, captured_at="2026-09-07T08:00:00Z", payload=stocks(day, small_ids))
            with ExitStack() as stack:
                for name in ("save_temporal_source_snapshot", "save_temporal_source_slot_snapshot",
                             "save_temporal_source_closure_state"):
                    stack.enter_context(patch.object(runtime, name, side_effect=AssertionError(name)))
                status, payload = capture(block, lambda: stocks(day, small_ids), day=day,
                                          role=role, nm_ids=full_ids)
            assert status.kind == "incomplete" and status.requested_count == 92
            assert status.covered_count == 33 and status.missing_nm_ids == list(range(34, 93))
            assert [item.nm_id for item in payload.items] == small_ids
            assert "partial_stock_display_only" in status.note

        # Exercise the real historical CSV adapter and stock transform. A report
        # with 33 observed rows out of 92 retains observations without exposing
        # ordinary business `items`, admitting a snapshot, or inventing 59 zeros.
        csv_day = "2026-09-03"
        csv_rows = parse_csv_dict_rows("NmID;OfficeName;03.09.2026\n" + "".join(
            f"{nm_id};Коледино;7\n" for nm_id in small_ids
        ))
        source = HistoricalCsvBackedStocksSource(
            current_inventory_source=SimpleNamespace(fetch_warehouse_region_map=lambda nm_ids: {"Коледино": "Центральный"}),
            now_factory=lambda: NOW,
        )
        report = SellerAnalyticsCsvReport("test-download", "test-report", "2026-09-07T08:00:00Z", csv_rows, "test-digest")
        with patch("packages.adapters.stocks_block.load_runtime_config", return_value=SimpleNamespace(
            base_url="https://example.invalid", token="offline-fixture", timeout_seconds=1,
        )), patch.object(source._csv_transport, "fetch", return_value=report) as report_fetch:
            partial_loader = StocksBlock(source)
            partial = partial_loader.execute(StocksRequest("stocks", csv_day, full_ids)).result
            assert partial.kind == "incomplete" and not hasattr(partial, "items")
            assert [item.nm_id for item in partial.observed_items] == small_ids
            assert partial.missing_nm_ids == list(range(34, 93))
            with ExitStack() as stack:
                for name in ("save_temporal_source_snapshot", "save_temporal_source_slot_snapshot",
                             "save_temporal_source_closure_state"):
                    stack.enter_context(patch.object(runtime, name, side_effect=AssertionError(name)))
                status, payload = capture(block, lambda: partial_loader.execute(
                    StocksRequest("stocks", csv_day, full_ids)).result,
                    day=csv_day, nm_ids=full_ids,
                )
            assert status.kind == "incomplete" and status.covered_count == 33
            assert status.missing_nm_ids == list(range(34, 93))
            assert payload.kind == "incomplete" and [item.nm_id for item in payload.items] == small_ids
            assert all(item.stock_total == 7 for item in payload.items)
            assert report_fetch.call_args.kwargs["params"]["nmIds"] == full_ids
        assert runtime.load_temporal_source_slot_snapshot(
            source_key="stocks", snapshot_date=csv_day, snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        )[0] is None

        for ids, expected_kind in [([101, 202, 303, 909], "success"),
                                   ([101, 101, 202, 303], "incomplete"),
                                   ([101, 202], "incomplete")]:
            status, _ = _capture_live_source(**slot_kwargs(), loader=lambda ids=ids: stocks(DAY, ids))
            assert status.kind == expected_kind and status.covered_count <= 3
        status, _ = _capture_live_source(**{**slot_kwargs(), "source_key": "prices_snapshot"},
                                         loader=lambda: stocks(DAY, [101]))
        assert status.kind == "success", "coverage tightening must remain stocks-only"

        # Catalog failure is source-local, and non-stock-only calls never read it.
        stock_count = len(stock_requests)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(f"DROP TABLE {NOMENCLATURE_TABLE}")
        with patch("packages.application.sheet_vitrina_v1_live_plan.require_stock_catalog_scope",
                   side_effect=AssertionError("unselected stocks catalog read")):
            load(DAY, {"prices_snapshot"})
        with ExitStack() as stack:
            for name in ("save_temporal_source_snapshot", "save_temporal_source_slot_snapshot",
                         "save_temporal_source_closure_state"):
                stack.enter_context(patch.object(runtime, name, side_effect=AssertionError(name)))
            preserved_unknown = load(DAY, {"stocks"})
        unknown_status = next(status for status in preserved_unknown.statuses
                              if status.temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED)
        assert unknown_status.kind == "error" and "stock_catalog_scope=unknown" in unknown_status.note
        assert unknown_status.diagnostics["preserved_display_nm_ids"] == [101, 202, 303]
        assert set(preserved_unknown.slot_lookups[TEMPORAL_SLOT_YESTERDAY_CLOSED].stocks_lookup) == {101, 202, 303}
        failed = load(DAY, {"stocks", "prices_snapshot"})
        failure = next(status for status in failed.statuses if status.source_key == "stocks"
                       and status.temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED)
        assert failure.kind == "error" and "stock_nomenclature" in failure.note
        assert len(stock_requests) == stock_count
        assert any(status.kind == "success" and status.source_key == "prices_snapshot"
                   for status in failed.statuses)

    print("stock-only catalog scope; automatic new SKU; closed history preserved; cache membership; source-local errors: ok")


if __name__ == "__main__":
    main()
