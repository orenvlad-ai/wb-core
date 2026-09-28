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
    _recent_configured_slot, _sanitize_seller_goods, collect_once, publish_once,
)
from packages.application.wb_buyer_authenticated_observations import load_daily_projection  # noqa: E402


class Seller:
    def fetch(self, request):
        return {"data": {"listGoods": [{"nmID": request.nm_ids[0], "vendorCode": "SECRET",
                 "discount": 87, "clubDiscount": 0, "sizes": [{"sizeID": 691548293,
                 "price": 1500, "discountedPrice": 195, "clubDiscountedPrice": 195,
                 "techSizeName": "private"}]}]}}


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
        assert 33 in selections[1][:9] and selections[1][0] != selections[0][0]
        assert load_daily_projection(rotation_runtime, "2026-09-28", roster)["covered_count"] == 17
    print("wb_buyer_authenticated_collect_smoke: OK")


if __name__ == "__main__":
    main()
