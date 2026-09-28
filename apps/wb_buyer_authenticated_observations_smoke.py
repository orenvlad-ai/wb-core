#!/usr/bin/env python3
"""Focused persistence and semantics checks for account-scoped buyer observations."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_buyer_authenticated_observations import (
    append_observation, begin_run, classify_observation, finish_run,
    import_verified_prior_evidence, load_daily_projection,
)


def main() -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "operational.sqlite"
        sqlite3.connect(database).close()
        runtime = SimpleNamespace(db_path=database)
        buyer = {
            "status": "observed", "session_status": "authenticated_surface",
            "authenticated_session_proof": True, "measured_at": "2026-09-28T18:29:02+00:00",
            "session_checked_at": "2026-09-28T18:28:55+00:00",
            "wallet_price": 139, "normal_price": 144,
            "profile_reference": "profile-opaque", "auth_run_reference": "auth-opaque",
            "destination_context": {"dest_id": 123589313},
        }
        seller = {
            "measured_at": "2026-09-28T18:28:39+00:00", "discount": 87,
            "clubDiscount": 0, "sizes": [{"sizeID": 691548293, "price": 1500,
                                      "discountedPrice": 195, "clubDiscountedPrice": 195}],
        }
        observed = classify_observation(nm_id=497416931, buyer=buyer, seller=seller)
        assert observed["effective_nonwallet_discount"] == 0.261538
        assert observed["authenticated_spp"] is None and observed["spp_evidence"] == "components_unknown"
        assert observed["seller_sizes"] == [{"size_id": 691548293, "price_rub": 1500.0,
                                             "discounted_price_rub": 195.0, "club_discounted_price_rub": 195.0}]
        assert observed["seller_discount_pct"] == 87 and observed["seller_club_discount_pct"] == 0
        assert observed["currency"] == "RUB" and observed["session_checked_at"] == buyer["session_checked_at"]
        assert observed["profile_reference"] == "profile-opaque" and observed["auth_run_reference"] == "auth-opaque"
        assert observed["destination_context"]["dest_id"] == 123589313
        date = begin_run(runtime, run_id="run-one", started_at="2026-09-28T18:28:30+00:00",
                         requested_nm_ids=[497416931, 497416932])
        assert date == "2026-09-28"
        append_observation(runtime, run_id="run-one", observation=observed)
        append_observation(runtime, run_id="run-one", observation=observed)  # identical is idempotent
        finish_run(runtime, run_id="run-one", finished_at="2026-09-28T18:30:00+00:00", status="partial")
        later = dict(buyer, measured_at="2026-09-28T18:31:00+00:00", status="price_unavailable",
                     reason="buyer_login_required", authenticated_session_proof=False)
        failed = classify_observation(nm_id=497416931, buyer=later, seller=None)
        begin_run(runtime, run_id="run-two", started_at="2026-09-28T18:30:50+00:00",
                  requested_nm_ids=[497416931, 497416932])
        append_observation(runtime, run_id="run-two", observation=failed)
        finish_run(runtime, run_id="run-two", finished_at="2026-09-28T18:31:30+00:00", status="partial")
        projection = load_daily_projection(runtime, date, [497416931, 497416932])
        assert projection["kind"] == "incomplete" and projection["covered_count"] == 1
        assert projection["items"][0]["buyer_nonwallet_price_rub"] == 144
        assert projection["diagnostics"]["latest_attempt_reason_by_nm_id"]["497416931"] == "buyer_login_required"
        assert projection["diagnostics"]["first_observed_business_date"] == date
        new_buyer = dict(buyer, measured_at="2026-09-28T18:35:00+00:00", auth_run_reference="auth-new")
        begin_run(runtime, run_id="run-three", started_at="2026-09-28T18:34:00+00:00",
                  requested_nm_ids=[497416931, 497416932])
        append_observation(runtime, run_id="run-three", observation=classify_observation(
            nm_id=497416932, buyer=new_buyer, seller=None))
        finish_run(runtime, run_id="run-three", finished_at="2026-09-28T18:36:00+00:00", status="partial")
        changed_context = load_daily_projection(runtime, date, [497416931, 497416932])
        assert changed_context["covered_count"] == 1 and changed_context["items"][0]["nm_id"] == 497416932
        assert changed_context["diagnostics"]["current_auth_run_reference"] == "auth-new"
        assert load_daily_projection(runtime, date, [497416931])["covered_count"] == 0
        begin_run(runtime, run_id="run-four", started_at="2026-09-28T18:39:00+00:00",
                  requested_nm_ids=[497416931, 497416932])
        append_observation(runtime, run_id="run-four", observation=classify_observation(
            nm_id=497416931, buyer=dict(buyer, status="price_unavailable", measured_at="2026-09-28T18:40:00+00:00",
                                               auth_run_reference="auth-third", reason="buyer_price_unavailable"), seller=None))
        finish_run(runtime, run_id="run-four", finished_at="2026-09-28T18:41:00+00:00", status="error")
        assert load_daily_projection(runtime, date, [497416931, 497416932])["covered_count"] == 0
        different_sizes = dict(seller, sizes=[seller["sizes"][0], {"sizeID": 2, "price": 1000,
                                                                    "discountedPrice": 180, "clubDiscountedPrice": 180}])
        unknown = classify_observation(nm_id=497416931, buyer=buyer, seller=different_sizes)
        assert unknown["effective_nonwallet_discount"] is None
        assert unknown["effective_discount_reason"] == "seller_variants_have_different_prices"
        assert len(unknown["seller_sizes"]) == 2
        stale = classify_observation(nm_id=497416931, buyer=buyer,
                                     seller=dict(seller, measured_at="2026-09-28T18:00:00+00:00"))
        assert stale["effective_nonwallet_discount"] is None
        anonymous = classify_observation(nm_id=497416931, buyer=dict(buyer, authenticated_session_proof=False), seller=seller)
        assert anonymous["status"] == "unavailable" and anonymous["buyer_nonwallet_price_rub"] is None
        prior_db = Path(directory) / "prior.sqlite"
        sqlite3.connect(prior_db).close()
        prior_runtime = SimpleNamespace(db_path=prior_db)
        prior = {
            "evidence_id": "wbc0081-actual-price-observation", "business_date": date,
            "nm_id": 497416931,
            "buyer": {"authenticated_surface": True, "measured_at": buyer["measured_at"],
                      "wallet_price_rub": 139, "nonwallet_price_rub": 144,
                      "destination": {"dest_id": 123589313, "address": "not persisted"}},
            "seller": {"measured_at": seller["measured_at"], "discount": 87,
                       "club_discount": 0,
                       "sizes": [{"size_id": 691548293, "price_rub": 1500,
                                  "discounted_price_rub": 195, "club_discounted_price_rub": 195}]},
        }
        receipt = import_verified_prior_evidence(prior_runtime, prior)
        assert receipt["business_date"] == date
        imported = load_daily_projection(prior_runtime, date, [497416931, 497416932])
        assert imported["kind"] == "incomplete" and imported["covered_count"] == 1
        assert imported["items"][0]["buyer_source"] == "verified_prior_live_diagnostic_evidence"
        assert imported["items"][0]["destination_context"]["dest_id"] == 123589313
        assert "not persisted" not in str(imported)
    print("wb_buyer_authenticated_observations_smoke: OK")


if __name__ == "__main__":
    main()
