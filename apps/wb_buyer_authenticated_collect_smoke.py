#!/usr/bin/env python3
"""Focused collector cadence, coverage, source isolation and publication tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_buyer_authenticated_collect import (  # noqa: E402
    _recent_configured_slot, _sanitize_seller_goods, _requested_ids, collect_once, publish_once,
)
from packages.application.wb_buyer_authenticated_observations import (  # noqa: E402
    load_active_requested_nm_ids, load_daily_projection, load_source_requested_nm_ids,
)
from packages.application.stock_catalog_scope import StockCatalogScopeError  # noqa: E402
from packages.application.stock_monitor_market import build_market  # noqa: E402


class Seller:
    def fetch(self, request):
        return {"data": {"listGoods": [{"nmID": request.nm_ids[0], "vendorCode": "SECRET",
                 "discount": 87, "clubDiscount": 0, "sizes": [{"sizeID": 691548293,
                 "price": 1500, "discountedPrice": 195, "clubDiscountedPrice": 195,
                 "techSizeName": "private"}]}]}}


def check_monitor_catalog_collection(root, buyer):
    database = root / "monitor-catalog.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE registry_upload_config_v2(bundle_version TEXT,nm_id INTEGER,enabled INTEGER);
            CREATE TABLE registry_upload_current_state(slot INTEGER,bundle_version TEXT);
            INSERT INTO registry_upload_current_state VALUES(1,'bundle-one');
            INSERT INTO registry_upload_config_v2 VALUES('bundle-one',1,1),('bundle-one',2,1),
                ('bundle-one',94,0),('bundle-one',9999,1),('old-bundle',8888,1);
            CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                item_id TEXT,nm_id INTEGER,is_active INTEGER,is_hidden INTEGER,updated_at TEXT);
        """)
        connection.executemany("INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,?,?,?)",
            [(f"item-{nm}",nm,int(nm<=92),int(nm>=93),"") for nm in range(1,95)]
            + [("retired-item",95,0,0,"")])
        config_before = connection.execute("SELECT * FROM registry_upload_config_v2").fetchall()
    runtime = SimpleNamespace(db_path=database)
    # Disabled and retained-hidden catalog cards are measured without becoming
    # enabled reporting cards. An enabled card outside the catalog is retained.
    assert load_active_requested_nm_ids(runtime) == [1,2,9999]
    assert _requested_ids(runtime) == [*range(1,95),9999]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM registry_upload_config_v2").fetchall() == config_before
        connection.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET nm_id=1 WHERE item_id='item-94'")
    try:
        _requested_ids(runtime)
    except StockCatalogScopeError:
        pass
    else:
        raise AssertionError("an ambiguous catalog must not silently shrink collection scope")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET nm_id=94 WHERE item_id='item-94'")
        connection.execute("INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES('new-no-frame',96,1,0,'')")
    assert _requested_ids(runtime) == [*range(1,95),96,9999]

    # A 94-card catalog progresses through three 40-card batches instead of
    # revisiting the first batch. Synthetic deferred rows must not count as visits.
    coverage_database = root / "full-monitor-coverage.sqlite"
    sqlite3.connect(coverage_database).close()
    coverage_runtime = SimpleNamespace(db_path=coverage_database)
    roster = list(range(1,95))
    selections = []
    with patch("apps.wb_buyer_authenticated_collect.wb_buyer_chrome_price.current_context_references",
               return_value={"profile_reference":"opaque-profile","auth_run_reference":"auth-one"}):
        for index, timestamp in enumerate(("2026-09-28T09:00:00+00:00", "2026-09-28T12:00:00+00:00",
                                           "2026-09-28T15:00:00+00:00")):
            def read_batch(ids, at=timestamp):
                selections.append(list(ids))
                return [{**buyer,"nm_id":nm,"measured_at":at} for nm in ids]
            result = collect_once(coverage_runtime, run_id=f"full-catalog-{index}",requested_nm_ids=roster,
                                  seller_source=Seller(),buyer_read=read_batch,now=lambda at=timestamp:at)
            assert result["requested_count"] == 94 and result["buyer_covered_count"] == 40
            assert result["daily_covered_count"] == min(94,(index+1)*40)
            assert result["status"] == ("completed" if index==2 else "partial")
    assert selections[0] == list(range(1,41)) and selections[1] == list(range(41,81))
    assert selections[2][:14] == list(range(81,95)) and all(len(ids)<=40 for ids in selections)
    projection = load_daily_projection(coverage_runtime,"2026-09-28",roster)
    assert projection["covered_count"] == 94 and projection["missing_nm_ids"] == [] and projection["kind"] == "success"
    assert projection["diagnostics"]["latest_attempt_measured_at_by_nm_id"]["41"] == "2026-09-28T12:00:00+00:00"
    assert projection["diagnostics"]["latest_attempt_reason_by_nm_id"]["41"] != "batch_chunk_deferred"
    assert load_source_requested_nm_ids(coverage_runtime,"2026-09-28") == (roster,"frozen_collection_run")
    # The consumer still requests its own original 33-card reporting scope.
    assert load_daily_projection(coverage_runtime,"2026-09-28",roster[:33])["requested_count"] == 33
    market = build_market(coverage_runtime,nm_ids=roster,today="2026-09-28",previous={},
                          ads_loader=lambda:{"index":{}})
    assert all(row["buyer_wallet_price"]["value"] == 139 and row["buyer_wallet_price"]["status"] == "ready"
               for row in market.values())
    # A new login context replaces coverage rather than composing prices from
    # two accounts, even when all 94 cards had already been measured today.
    next_context = "2026-09-28T18:00:00+00:00"
    with patch("apps.wb_buyer_authenticated_collect.wb_buyer_chrome_price.current_context_references",
               return_value={"profile_reference":"opaque-profile","auth_run_reference":"auth-new"}):
        new_account = collect_once(coverage_runtime,run_id="new-account",requested_nm_ids=roster,
            seller_source=Seller(),now=lambda:next_context,
            buyer_read=lambda ids:[{**buyer,"nm_id":nm,"measured_at":next_context,"auth_run_reference":"auth-new"} for nm in ids])
    assert new_account["status"] == "partial" and new_account["daily_covered_count"] == 40
    new_projection = load_daily_projection(coverage_runtime,"2026-09-28",roster)
    assert new_projection["covered_count"] == 40
    assert all(item["auth_run_reference"] == "auth-new" for item in new_projection["items"])
    new_market = build_market(coverage_runtime,nm_ids=roster,today="2026-09-28",previous=market,
                              ads_loader=lambda:{"index":{}})
    assert sum(row["buyer_wallet_price"]["value"] is not None for row in new_market.values()) == 40

    # Reader-budget exhaustion differs from an actual unavailable card. Both
    # must let the rest of the roster advance; the failed card can be retried later.
    budget_database = root / "monitor-budget.sqlite"
    sqlite3.connect(budget_database).close()
    budget_runtime = SimpleNamespace(db_path=budget_database)
    attempted = []
    for index, timestamp in enumerate(("2026-09-28T09:00:00+00:00","2026-09-28T12:00:00+00:00")):
        def bounded_reader(ids, at=timestamp):
            attempted.append(list(ids))
            return [({**buyer,"nm_id":nm,"measured_at":at} if offset<9 else
                     {**buyer,"nm_id":nm,"measured_at":at,"status":"price_unavailable",
                      "reason":"authenticated_price_batch_budget_exhausted"}) for offset,nm in enumerate(ids)]
        collect_once(budget_runtime,run_id=f"budget-{index}",requested_nm_ids=roster,
                     seller_source=Seller(),buyer_read=bounded_reader,now=lambda at=timestamp:at)
    assert attempted[0][:9] == list(range(1,10)) and attempted[1][:9] == list(range(10,19))
    assert load_daily_projection(budget_runtime,"2026-09-28",roster)["covered_count"] == 18
    # A genuine failed price read counts as a visit, so it does not pin the
    # traversal to that card while the remaining cards still lack measurements.
    failed_at = "2026-09-28T15:00:00+00:00"
    def one_unavailable(ids):
        attempted.append(list(ids))
        return [{**buyer,"nm_id":nm,"measured_at":failed_at,"status":"price_unavailable",
                 "reason":("authenticated_price_unavailable" if offset==0 else
                            "authenticated_price_batch_budget_exhausted")} for offset,nm in enumerate(ids)]
    collect_once(budget_runtime,run_id="unavailable-card",requested_nm_ids=roster,
                 seller_source=Seller(),buyer_read=one_unavailable,now=lambda:failed_at)
    following = []
    collect_once(budget_runtime,run_id="after-unavailable",requested_nm_ids=roster,
                 seller_source=Seller(),buyer_read=lambda ids:following.extend(ids) or [],
                 now=lambda:"2026-09-28T18:00:00+00:00")
    assert attempted[-1][0] == 19 and following[0] == 20
    # Current-day rotation starts afresh rather than claiming yesterday's prices.
    selected_next_day = []
    collect_once(budget_runtime,run_id="next-day",requested_nm_ids=roster,seller_source=Seller(),
                 buyer_read=lambda ids:selected_next_day.extend(ids) or [],
                 now=lambda:"2026-09-29T09:00:00+00:00")
    assert selected_next_day == list(range(1,41))
    assert load_daily_projection(budget_runtime,"2026-09-29",roster)["covered_count"] == 0
    with patch("apps.wb_buyer_authenticated_collect.select_collection_nm_ids",side_effect=sqlite3.OperationalError("fixture")):
        try:
            collect_once(budget_runtime,run_id="selection-failed",requested_nm_ids=roster,
                         seller_source=Seller(),buyer_read=lambda _ids:[],
                         now=lambda:"2026-09-29T12:00:00+00:00")
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("selection failure must propagate")
    with sqlite3.connect(budget_database) as connection:
        assert connection.execute("SELECT status FROM wb_buyer_authenticated_runs WHERE run_id='selection-failed'").fetchone()[0] == "error"


def main() -> None:
    now = "2026-09-28T18:29:02+00:00"
    with TemporaryDirectory() as directory:
        root = Path(directory)
        db = root / "operational.sqlite"
        sqlite3.connect(db).close()
        runtime = SimpleNamespace(db_path=db)
        sanitized = _sanitize_seller_goods(Seller().fetch(SimpleNamespace(nm_ids=[497416931])),
                                           [497416931], now)
        assert sanitized[497416931]["sizes"][0]["sizeID"] == 691548293
        assert "SECRET" not in str(sanitized) and "private" not in str(sanitized)
        buyer = {"nm_id": 497416931, "measured_at": now, "session_checked_at": now,
                 "status": "observed", "session_status": "authenticated_surface",
                 "authenticated_session_proof": True, "wallet_price": 139, "normal_price": 144,
                 "profile_reference": "opaque-profile", "auth_run_reference": "auth-one"}
        result = collect_once(runtime, run_id="collector-run-1", requested_nm_ids=[497416931, 497416932],
                              seller_source=Seller(), buyer_read=lambda ids: [buyer], now=lambda: now)
        assert result["status"] == "partial" and result["buyer_covered_count"] == 1
        assert result["effective_discount_covered_count"] == 1
        projection = load_daily_projection(runtime, "2026-09-28", [497416931, 497416932])
        assert projection["covered_count"] == 1 and projection["requested_count"] == 2
        assert projection["items"][0]["effective_nonwallet_discount"] == 0.261538
        assert projection["items"][0]["authenticated_spp"] is None
        assert projection["diagnostics"]["latest_attempt_reason_by_nm_id"]["497416932"] == "buyer_reader_failed"
        calls = []
        def post(url, payload, *, cookie, timeout):
            calls.append((url, payload, cookie, timeout))
            return {"job_id": "job-1", "status": "queued"}
        def poll(**kwargs):
            return {"status": "success"}
        receipt = publish_once(runtime, run_id="collector-run-1", business_date="2026-09-28",
                               cookie="fixture-cookie", post_json=post, poll_job=poll, now=lambda: now)
        assert receipt == {"status": "completed", "job_id": "job-1"}
        assert calls[0][1] == {"source_group_id": "wb_buyer_authenticated", "as_of_date": "2026-09-28"}
        assert publish_once(runtime, run_id="collector-run-1", business_date="2026-09-28",
                            cookie="fixture-cookie", post_json=post, poll_job=poll, now=lambda: now)["status"] == "already_completed"
        assert len(calls) == 1
        # Both prices reached the parent before child cleanup failed. They
        # remain facts, while the run and daily projection cannot claim a
        # completed healthy traversal.
        later = "2026-09-28T18:30:02+00:00"
        failed_cleanup_rows = [
            {**buyer, "nm_id": nm_id, "measured_at": later, "reader_lifecycle_status": "cleanup_failed"}
            for nm_id in (497416931, 497416932)
        ]
        failed_cleanup = collect_once(runtime, run_id="collector-run-cleanup-failed",
                                      requested_nm_ids=[497416931, 497416932],
                                      seller_source=Seller(), buyer_read=lambda _ids: failed_cleanup_rows,
                                      now=lambda: later)
        assert failed_cleanup["buyer_covered_count"] == 2
        assert failed_cleanup["status"] == "partial"
        assert failed_cleanup["reader_lifecycle_status"] == "cleanup_failed"
        assert load_daily_projection(runtime, "2026-09-28", [497416931, 497416932])["kind"] == "incomplete"
        # A later login context with zero observed prices must still request
        # a group publication so the old account's ready cells can be cleared.
        newest = "2026-09-28T18:31:02+00:00"
        new_context_rows = [{"nm_id": nm_id, "measured_at": newest,
                             "status": "price_unavailable", "reason": "buyer_reader_failed",
                             "session_status": "probe_error", "authenticated_session_proof": False,
                             }
                            for nm_id in (497416931, 497416932)]
        with patch("apps.wb_buyer_authenticated_collect.wb_buyer_chrome_price.current_context_references",
                   return_value={"profile_reference": "opaque-profile", "auth_run_reference": "auth-two"}):
            new_context = collect_once(runtime, run_id="collector-run-new-context-failed",
                                       requested_nm_ids=[497416931, 497416932],
                                       seller_source=Seller(), buyer_read=lambda _ids: new_context_rows,
                                       now=lambda: newest)
        assert new_context["buyer_covered_count"] == 0
        assert new_context["publication_business_dates"] == ["2026-09-28"]
        cleared = load_daily_projection(runtime, "2026-09-28", [497416931, 497416932])
        assert cleared["covered_count"] == 0 and cleared["diagnostics"]["current_auth_run_reference"] == "auth-two"
        assert publish_once(runtime, run_id="collector-run-new-context-failed", business_date="2026-09-28",
                            cookie="fixture-cookie", post_json=post, poll_job=poll, now=lambda: newest)["status"] == "completed"
        assert len(calls) == 2
        def lost_response(*_args, **_kwargs):
            raise TimeoutError("response may have been accepted")
        ambiguous = publish_once(runtime, run_id="ambiguous-run", business_date="2026-09-28",
                                 cookie="fixture-cookie", post_json=lost_response, poll_job=poll, now=lambda: now)
        assert ambiguous["status"] == "ambiguous"
        assert publish_once(runtime, run_id="ambiguous-run", business_date="2026-09-28",
                            cookie="fixture-cookie", post_json=post, poll_job=poll, now=lambda: now)["status"] == "ambiguous_previous_attempt"
        assert len(calls) == 2
        schedule = {"schedules": [{"id": "slot-one", "enabled": True, "local_time_hhmm": "23:00",
                   "timezone": "Asia/Yekaterinburg", "last_status": "error",
                   "last_due_at": "2026-09-28T18:00:00+00:00",
                   "last_finished_at": "2026-09-28T18:20:00+00:00"}]}
        (root / "sheet_vitrina_v1_auto_refresh_schedules.json").write_text(json.dumps(schedule))
        assert _recent_configured_slot(root, datetime(2026, 9, 28, 18, 29, tzinfo=timezone.utc)) == (
            "slot-one", "2026-09-28T18:00:00+00:00")
        assert _recent_configured_slot(root, datetime(2026, 9, 28, 19, 0, tzinfo=timezone.utc)) is None

        rotation_db = root / "rotation.sqlite"
        sqlite3.connect(rotation_db).close()
        rotation_runtime = SimpleNamespace(db_path=rotation_db)
        roster = list(range(1, 34))
        selections = []
        for index, timestamp in enumerate(("2026-09-28T18:00:00+00:00", "2026-09-28T18:10:00+00:00")):
            def budget_cutoff_reader(ids, at=timestamp):
                selections.append(list(ids))
                return [{**buyer, "nm_id": nm_id, "measured_at": at} for nm_id in ids[:9]]
            rotated = collect_once(rotation_runtime, run_id=f"due-slot-{index}",
                                   requested_nm_ids=roster, seller_source=Seller(),
                                   buyer_read=budget_cutoff_reader, now=lambda at=timestamp: at)
            assert rotated["requested_count"] == 33 and rotated["buyer_covered_count"] == 9
            assert rotated["status"] == "partial"
        assert selections[0][:9] == list(range(1, 10))
        assert selections[1][:9] == list(range(10,19))
        assert load_daily_projection(rotation_runtime, "2026-09-28", roster)["covered_count"] == 18
        check_monitor_catalog_collection(root, buyer)
    print("wb_buyer_authenticated_collect_smoke: OK")


if __name__ == "__main__":
    main()
