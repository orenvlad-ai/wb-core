#!/usr/bin/env python3
"""Adversarial periodic FBS WAC and isolated persistence checks."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.fbs_snapshot_cost import (
    CandidateStore, FbsSnapshotCostError, candidate_period_view, close_candidate_period,
    cost_continuity_missing_dates, evaluate_candidate, fingerprint, initialize_candidate,
)


def document(identity="receipt-1", day="2026-09-08", *, quantity="1000", capital="200000", kind="receipt", nm_id=1):
    doc = {"document_id": identity, "business_date": day, "posted_at": day + "T13:00:00Z",
           "kind": kind, "events": [{"nm_id": nm_id, "facility_id": "ff-1", "kind": kind,
           "quantity": quantity, "capital_rub": capital, "source": {"document_id": identity, "basis": "confirmed_document_money"}}]}
    doc["fingerprint"] = fingerprint(doc)
    return doc


def capture(day="2026-09-07", *, quantity="1000", docs=None, wac="100", extra=None, timestamp=None):
    docs = docs or []
    rows = [{"nm_id": 1, "facility_id": "ff-1", "quantity": quantity}, *(extra or [])]
    result = {"business_date": day, "captured_at": day + "T14:00:00Z",
              "quantity_snapshot": {"complete": True, "date": day, "id": "snapshot-" + day,
                "captured_at": timestamp or day + "T14:00:00Z", "digest": fingerprint(rows), "rows": rows},
              "documents_complete": True, "documents": docs,
              "baseline_costs": {"available": True, "version_id": "published-1",
                  "rows": [{"nm_id": 1, "facility_id": "ff-1", "wac_rub": wac, "source": {"version_id": "published-1"}}],
                  "document_manifest": {d["document_id"]: d["fingerprint"] for d in docs}}}
    result["source_digest"] = fingerprint(result)
    return result


def value(state, day="2026-09-08", key="ff-1:1"):
    return state["periods"][day]["rows"][key]


class CostTests(unittest.TestCase):
    def setUp(self):
        self.initial = initialize_candidate(capture())

    def test_receipt_after_snapshot_order_and_idempotency(self):
        received = document()
        before_doc = evaluate_candidate(self.initial, capture("2026-09-08", quantity="2000"))
        final = evaluate_candidate(before_doc, capture("2026-09-08", quantity="2000", docs=[received]))
        direct = evaluate_candidate(self.initial, capture("2026-09-08", quantity="2000", docs=[received]))
        self.assertEqual(final, direct)
        self.assertEqual(value(final)["wac_rub"], "150")
        self.assertEqual(value(final)["quantity"], "2000")
        self.assertEqual(value(final)["cost_mass_quantity"], "2000")
        self.assertEqual(evaluate_candidate(final, capture("2026-09-08", quantity="2000", docs=[received])), final)

    def test_stock_snapshot_after_document_capture_is_rejected(self):
        stale_capture = capture("2026-09-08", timestamp="2026-09-08T14:00:01Z")
        with self.assertRaisesRegex(FbsSnapshotCostError, "snapshot_after_document_capture"):
            evaluate_candidate(self.initial, stale_capture)

    def test_document_before_snapshot_and_sales(self):
        received = document()
        early = evaluate_candidate(self.initial, capture("2026-09-08", docs=[received]))
        later = evaluate_candidate(early, capture("2026-09-08", quantity="1900", docs=[received]))
        self.assertEqual(value(later)["wac_rub"], "150")
        self.assertEqual(value(later)["capital_rub"], "285000")
        closed = close_candidate_period(later, "2026-09-08", today="2026-09-09")
        next_day = evaluate_candidate(closed, capture("2026-09-09", quantity="2900", docs=[received, document("r2", "2026-09-09", capital="250000")]))
        self.assertEqual(Decimal(value(next_day, "2026-09-09")["wac_rub"]).quantize(Decimal("0.000001")), Decimal("184.482759"))
        self.assertEqual(value(next_day, "2026-09-09")["opening_quantity"], "1900")
        self.assertEqual(next_day["periods"]["2026-09-08"], closed["periods"]["2026-09-08"])

    def test_expense_once_and_outgoing_not_subtracted_twice(self):
        docs = [document(), document("expense", quantity="0", capital="2000", kind="expense"),
                document("outgoing", quantity="100", capital="0", kind="outgoing")]
        result = evaluate_candidate(self.initial, capture("2026-09-08", quantity="1900", docs=docs))
        self.assertEqual(value(result)["wac_rub"], "151")
        self.assertEqual(value(result)["cost_mass_quantity"], "2000")
        self.assertEqual(value(result)["expense_capital_rub"], "2000")
        self.assertEqual(evaluate_candidate(result, capture("2026-09-08", quantity="1900", docs=docs)), result)

    def test_baseline_included_document_not_added_again(self):
        old = document("absorbed", "2026-09-07")
        initial = initialize_candidate(capture(docs=[old]))
        result = evaluate_candidate(initial, capture("2026-09-08", quantity="900", docs=[old]))
        self.assertEqual(value(result)["wac_rub"], "100")
        self.assertEqual(value(result)["receipt_quantity"], "0")

    def test_open_revision_replaces_original_not_adds(self):
        old, revised = document(), document(capital="220000")
        result = evaluate_candidate(self.initial, capture("2026-09-08", docs=[old]))
        result = evaluate_candidate(result, capture("2026-09-08", docs=[revised]))
        self.assertEqual(value(result)["wac_rub"], "160")

    def test_late_closed_and_absorbed_revision_stay_pending(self):
        received = document()
        current = evaluate_candidate(self.initial, capture("2026-09-08", quantity="1900", docs=[received]))
        closed = close_candidate_period(current, "2026-09-08", today="2026-09-09")
        old_period = deepcopy(closed["periods"]["2026-09-08"])
        late = document("late", "2026-09-08", capital="300000")
        result = evaluate_candidate(closed, capture("2026-09-09", quantity="1800", docs=[received, late]))
        self.assertEqual(value(result, "2026-09-09")["wac_rub"], "150")
        self.assertEqual(result["periods"]["2026-09-08"], old_period)
        self.assertEqual(result["pending_documents"][0]["reason"], "late_closed_period_document")
        with self.assertRaisesRegex(FbsSnapshotCostError, "incomplete_period"):
            close_candidate_period(result, "2026-09-09", today="2026-09-10")
        changed = evaluate_candidate(closed, capture("2026-09-09", docs=[document(capital="250000")]))
        self.assertEqual(changed["pending_documents"][0]["reason"], "absorbed_document_missing_or_changed")
        self.assertEqual(value(changed, "2026-09-09")["receipt_quantity"], "0")

    def test_zero_quantity_retains_cost_first_receipt_new_sku(self):
        zero = evaluate_candidate(self.initial, capture("2026-09-08", quantity="0"))
        self.assertEqual(value(zero)["wac_rub"], "100")
        self.assertEqual(value(zero)["capital_rub"], "0")
        closed = close_candidate_period(zero, "2026-09-08", today="2026-09-09")
        result = evaluate_candidate(closed, capture("2026-09-09", quantity="1000", docs=[document(day="2026-09-09")]))
        self.assertEqual(value(result, "2026-09-09")["wac_rub"], "200")
        extra = [{"nm_id": 2, "facility_id": "ff-1", "quantity": "1000"}]
        new_sku = evaluate_candidate(self.initial, capture("2026-09-08", extra=extra, docs=[document(nm_id=2)]))
        self.assertEqual(value(new_sku, key="ff-1:2")["wac_rub"], "200")
        missing = evaluate_candidate(self.initial, capture("2026-09-08", extra=extra))
        self.assertIsNone(value(missing, key="ff-1:2")["capital_rub"])

    def test_unknown_initial_cost_not_washed_away_by_receipt(self):
        initial = initialize_candidate(capture(wac=None))
        result = evaluate_candidate(initial, capture("2026-09-08", docs=[document()]))
        self.assertIsNone(value(result)["wac_rub"])
        self.assertEqual(result["periods"]["2026-09-08"]["quality"], "incomplete")

    def test_growth_does_not_invent_purchase_or_change_wac(self):
        result = evaluate_candidate(self.initial, capture("2026-09-08", quantity="1500"))
        self.assertEqual(value(result)["wac_rub"], "100")
        self.assertEqual(value(result)["receipt_quantity"], "0")
        self.assertEqual(result["periods"]["2026-09-08"]["diagnostics"][0]["reason"], "quantity_increase_without_receipt")

    def test_unsupported_transfer_visible_without_legacy_cost(self):
        doc = document(kind="unsupported", capital="9999999")
        result = evaluate_candidate(self.initial, capture("2026-09-08", quantity="2000", docs=[doc]))
        self.assertIsNone(value(result)["wac_rub"])
        self.assertIsNone(value(result)["capital_rub"])
        self.assertEqual(result["pending_documents"][0]["reason"], "document_requires_independent_cost")
        self.assertEqual(result["periods"]["2026-09-08"]["quality"], "incomplete")

    def test_missing_and_regressed_sources_never_accepted(self):
        bad = capture("2026-09-08");bad["quantity_snapshot"]["complete"] = False
        with self.assertRaisesRegex(FbsSnapshotCostError, "incomplete_source"):
            evaluate_candidate(self.initial, bad)
        gap = evaluate_candidate(self.initial, capture("2026-09-10"))
        self.assertEqual(gap["periods"], {})
        self.assertEqual(gap["last_attempt"]["status"], "missing_period_source")
        state = evaluate_candidate(self.initial, capture("2026-09-08", timestamp="2026-09-08T14:00:00+00:00"))
        with self.assertRaisesRegex(FbsSnapshotCostError, "time_regression"):
            evaluate_candidate(state, capture("2026-09-08", timestamp="2026-09-08T16:00:00+03:00"))
        with self.assertRaisesRegex(FbsSnapshotCostError, "current_day"):
            close_candidate_period(state, "2026-09-08", today="2026-09-08")

    def test_missing_pending_document_stays_unresolved(self):
        received = document()
        state = evaluate_candidate(self.initial, capture("2026-09-08", docs=[received]))
        closed = close_candidate_period(state, "2026-09-08", today="2026-09-09")
        late = document("late", "2026-09-08")
        seen = evaluate_candidate(closed, capture("2026-09-09", docs=[received, late]))
        absent = evaluate_candidate(seen, capture("2026-09-09", docs=[received]))
        self.assertEqual(absent["pending_documents"][0]["reason"], "observed_document_missing")
        self.assertEqual(absent["pending_documents"][0]["fingerprint"], late["fingerprint"])
        with self.assertRaisesRegex(FbsSnapshotCostError, "incomplete_period"):
            close_candidate_period(absent, "2026-09-09", today="2026-09-10")
        # A disappeared open-period receipt is also not silently discarded.
        missing = evaluate_candidate(state, capture("2026-09-08"))
        self.assertEqual(missing["periods"]["2026-09-08"]["quality"], "incomplete")
        self.assertIsNone(value(missing)["wac_rub"])

    def test_new_day_document_revision_revalues_only_saved_open_day(self):
        current = evaluate_candidate(self.initial, capture("2026-09-08", quantity="1900", docs=[document()]))
        next_capture = capture("2026-09-09", quantity="1700", docs=[document(capital="400000")])
        revised = evaluate_candidate(current, next_capture)
        self.assertEqual(value(revised)["wac_rub"], "250")
        self.assertEqual(value(revised)["quantity"], "1900")
        self.assertEqual(revised["last_attempt"]["status"], "prior_open_period_revalued")
        self.assertNotIn("2026-09-09", revised["periods"])
        with self.assertRaisesRegex(FbsSnapshotCostError, "document_observation_time_regression"):
            evaluate_candidate(revised, capture("2026-09-08", quantity="1900", docs=[document()]))
        closed = close_candidate_period(revised, "2026-09-08", today="2026-09-09")
        next_day = evaluate_candidate(closed, next_capture)
        self.assertEqual(value(next_day, "2026-09-09")["wac_rub"], "250")
        self.assertEqual(value(next_day, "2026-09-09")["quantity"], "1700")

    def test_exact_date_read_and_changed_old_cost_ignored(self):
        incoming = capture("2026-09-08", docs=[document()])
        expected = evaluate_candidate(self.initial, incoming)
        incoming["baseline_costs"] = {"available": False, "rows": [{"wac_rub": "999999"}]}
        self.assertEqual(evaluate_candidate(self.initial, incoming), expected)
        view = candidate_period_view(expected, "2026-09-08")
        self.assertEqual(view["wac_rub"], "150")
        self.assertEqual(candidate_period_view(expected, "2026-09-10")["reason"], "exact_date_cost_unavailable")

    def test_store_cas_idempotence_closed_immutability_and_business_db_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.sqlite3"
            store = CandidateStore(path)
            baseline = store.save(self.initial, expected_fingerprint=None)
            self.assertEqual(store.load(), (self.initial, baseline))
            self.assertEqual(store.save(self.initial, expected_fingerprint=None), baseline)
            current = evaluate_candidate(self.initial, capture("2026-09-08", docs=[document()]))
            with self.assertRaisesRegex(FbsSnapshotCostError, "compare_and_swap"):
                store.save(current, expected_fingerprint=None)
            version = store.save(current, expected_fingerprint=baseline)
            closed = close_candidate_period(current, "2026-09-08", today="2026-09-09")
            version = store.save(closed, expected_fingerprint=version)
            altered = deepcopy(closed);altered["periods"]["2026-09-08"]["rows"]["ff-1:1"]["wac_rub"] = "1"
            with self.assertRaisesRegex(FbsSnapshotCostError, "closed_period_changed"):
                store.save(altered, expected_fingerprint=version)
            altered = deepcopy(closed);altered["baseline"]["cost_version_id"] = "new-price"
            with self.assertRaisesRegex(FbsSnapshotCostError, "frozen_baseline"):
                store.save(altered, expected_fingerprint=version)
            business = Path(directory) / "operational.sqlite3"
            c = sqlite3.connect(business);c.execute("CREATE TABLE accounting(value)");c.commit();c.close()
            before = business.read_bytes()
            with self.assertRaisesRegex(FbsSnapshotCostError, "not_an_isolated"):
                CandidateStore(business).save(self.initial, expected_fingerprint=None)
            self.assertEqual(business.read_bytes(), before)


def gap_capture(day="2026-09-07", **kwargs):
    result = capture(day, **kwargs)
    result["quantity_snapshot"]["facility_evidence"] = {
        "ff-1": {"seller_warehouse_id": 123, "mapping_id": "official-map-1"}}
    result["source_digest"] = fingerprint(result)
    return result


class CostContinuityTests(unittest.TestCase):
    def setUp(self):
        self.initial = initialize_candidate(gap_capture())

    def advance(self, state=None, day="2026-09-10", **kwargs):
        return evaluate_candidate(state or self.initial, gap_capture(day, **kwargs),
                                  allow_cost_continuity_gap=True)

    def test_real_current_quantity_without_invented_gap_periods(self):
        default = evaluate_candidate(self.initial, gap_capture("2026-09-10"))
        self.assertEqual(default["last_attempt"]["status"], "missing_period_source")
        current = self.advance(quantity="800")
        self.assertEqual(set(current["periods"]), {"2026-09-10"})
        self.assertEqual(cost_continuity_missing_dates(current), {"2026-09-08", "2026-09-09"})
        for day in ("2026-09-08", "2026-09-09"):
            self.assertFalse(candidate_period_view(current, day)["available"])
        view = candidate_period_view(current, "2026-09-10")
        self.assertEqual((view["quantity"], view["capital_rub"], view["wac_rub"]), ("800", "80000", "100"))
        self.assertIn("cost_continuity", view["source"])
        refreshed = self.advance(current, quantity="750")
        self.assertEqual(value(refreshed, "2026-09-10")["capital_rub"], "75000")
        self.assertEqual(refreshed["periods"]["2026-09-10"]["cost_continuity"],
                         current["periods"]["2026-09-10"]["cost_continuity"])

    def test_all_new_document_domains_block_including_empty_events(self):
        for day in ("2026-09-08", "2026-09-10"):
            for kind in ("receipt", "expense", "outgoing", "adjustment", "fbo_overhead"):
                with self.subTest(day=day, kind=kind):
                    doc = document(day=day, kind=kind)
                    if kind == "fbo_overhead":
                        doc["events"] = []
                        doc["fingerprint"] = fingerprint(doc)
                    with self.assertRaisesRegex(FbsSnapshotCostError, "cost_continuity_unabsorbed_document"):
                        self.advance(docs=[doc])

    def test_known_document_changes_and_missing_observations_block(self):
        old = document(day="2026-09-07")
        state = initialize_candidate(gap_capture(docs=[old]))
        self.advance(state, docs=[old])
        for docs in ([], [document(day="2026-09-07", capital="250000")]):
            with self.assertRaisesRegex(FbsSnapshotCostError, "cost_continuity_known_document_changed"):
                self.advance(state, docs=docs)
        observed = deepcopy(self.initial)
        observed["observed_documents"]["gone"] = {"business_date": "2026-09-08", "fingerprint": "gone", "affects_fbs": True}
        with self.assertRaisesRegex(FbsSnapshotCostError, "cost_continuity_observed_document_changed"):
            self.advance(observed)

    def test_scope_mapping_and_completeness_are_required(self):
        for mutation in ("mapping", "scope", "complete"):
            image = gap_capture("2026-09-10")
            if mutation == "mapping":
                image["quantity_snapshot"]["facility_evidence"]["ff-1"]["seller_warehouse_id"] = 124
            elif mutation == "scope":
                image["quantity_snapshot"]["rows"].append({"facility_id": "ff-1", "nm_id": 2, "quantity": "0"})
            else:
                image["quantity_snapshot"]["complete"] = False
            with self.subTest(mutation=mutation), self.assertRaises(FbsSnapshotCostError):
                evaluate_candidate(self.initial, image, allow_cost_continuity_gap=True)

    def test_following_day_receipt_uses_real_resumed_opening(self):
        current = self.advance(quantity="750")
        doc = document(day="2026-09-11", quantity="250", capital="50000")
        revalued = self.advance(current, day="2026-09-11", quantity="900", docs=[doc])
        self.assertEqual(revalued["last_attempt"]["target_date"], "2026-09-10")
        self.assertEqual(value(revalued, "2026-09-10")["quantity"], "750")
        closed = close_candidate_period(revalued, "2026-09-10", today="2026-09-11")
        after = self.advance(closed, day="2026-09-11", quantity="900", docs=[doc])
        row = value(after, "2026-09-11")
        self.assertEqual((row["opening_quantity"], row["wac_rub"], row["capital_rub"]), ("750", "125", "112500"))
        late = document("late", day="2026-09-10")
        with self.assertRaisesRegex(FbsSnapshotCostError, "cost_continuity_unabsorbed_document"):
            self.advance(after, day="2026-09-11", docs=[doc, late])

    def test_tampered_proof_cannot_advance_or_close(self):
        current = self.advance()
        current["periods"]["2026-09-10"]["cost_continuity"]["missing_dates"] = []
        with self.assertRaisesRegex(FbsSnapshotCostError, "cost_continuity_proof_invalid"):
            self.advance(current)
        with self.assertRaisesRegex(FbsSnapshotCostError, "cost_continuity_proof_invalid"):
            close_candidate_period(current, "2026-09-10", today="2026-09-11")


if __name__ == "__main__":
    unittest.main()
