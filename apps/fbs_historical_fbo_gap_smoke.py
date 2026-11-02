#!/usr/bin/env python3
"""Native historical sources -> real resumed/suffix state, LOCAL fixtures."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import fbs_historical_fbo_native_smoke as native_tests
from apps.fbs_historical_fbo_dispatch_smoke import seal
from apps.fbs_snapshot_cost_smoke import capture as basic_capture
from packages.application.fbs_snapshot_cost import (
    initialize_candidate, evaluate_candidate, close_candidate_period, cost_continuity_missing_dates, fingerprint,
)
from packages.application.fbs_historical_fbo_dispatch import prepare_historical_fbo_dispatch_plan
from packages.application.fbs_historical_fbo_sources import capture_historical_fbo_documents
from packages.application.fbs_historical_fbo_gap import build_historical_fbo_state_revision
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application import fbs_historical_fbo_native as native


def cap(day, docs=None):
    result = basic_capture(day, quantity="90", wac="100", docs=docs)
    result["contract"] = "fbs_snapshot_cost_sources_v2"
    result["documents_reason"] = ""
    result["quantity_snapshot"]["rows"][0]["facility_id"] = "synthetic-moscow"
    result["baseline_costs"]["rows"][0]["facility_id"] = "synthetic-moscow"
    result["quantity_snapshot"]["facility_evidence"] = {
        "synthetic-moscow": {"seller_warehouse_id": 123, "mapping_id": "synthetic-mapping"}}
    result["captured_at"] = "2026-11-02T16:00:00Z"
    result["source_digest"] = fingerprint({k: result[k] for k in
        ("contract", "business_date", "quantity_snapshot", "documents_complete", "documents", "documents_reason")})
    return result


class HistoricalFboGapTests(unittest.TestCase):
    apply = native_tests.NativeHistoricalFboTests.apply

    def setUp(self):
        native_tests.NativeHistoricalFboTests.setUp(self)
        state = initialize_candidate(cap("2026-10-15"))
        state["baseline"]["document_cost_state"]["fbo_rows"] = deepcopy(self.capture["opening"]["fbo_rows"])
        state["baseline"]["id"] = fingerprint({k: v for k, v in state["baseline"].items() if k != "id"})
        state = evaluate_candidate(state, cap("2026-10-21"), allow_cost_continuity_gap=True)
        state = close_candidate_period(state, "2026-10-21", today="2026-10-24")
        for day in ("2026-10-22", "2026-10-23", "2026-10-24"):
            state = evaluate_candidate(state, cap(day))
            if day != "2026-10-24":
                state = close_candidate_period(state, day, today="2026-10-24")
        self.book = {"schema": "fbs_active_snapshot_accounting_v1", "active": True, "state": state}
        self.capture["book_version"] = fingerprint(self.book)
        seal(self.capture)
        self.plan = prepare_historical_fbo_dispatch_plan(self.capture, self.scope)
        native.load.return_value = (self.book, self.capture["book_version"])
        self.apply()
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute("PRAGMA query_only=ON"); conn.execute("BEGIN")
            self.docs = capture_historical_fbo_documents(conn)
        self.latest = cap("2026-10-24", self.docs)

    def test_typed_gap_keeps_missing_days_and_original_fbs_values_and_replays_suffix(self):
        before = deepcopy(self.book)
        result = build_historical_fbo_state_revision(self.book, self.latest)
        self.assertEqual(self.book, before)
        self.assertEqual(result["status"], "state_prepared_pending_stage_revision")
        state = result["candidate_state"]
        self.assertEqual(set(state["periods"]), set(before["state"]["periods"]))
        self.assertEqual(cost_continuity_missing_dates(state), {"2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20"})
        for day in state["periods"]:
            self.assertEqual(state["periods"][day]["snapshot"], before["state"]["periods"][day]["snapshot"])
            self.assertEqual(state["periods"][day]["rows"], before["state"]["periods"][day]["rows"])
        self.assertEqual(len(state["periods"]["2026-10-21"]["applied_documents"]), 3)
        self.assertEqual(len(state["periods"]["2026-10-23"]["applied_documents"]), 1)
        self.assertEqual(state["periods"]["2026-10-24"]["document_cost_state"]["fbo_rows"]["synthetic-moscow:FBO:1"]["quantity"], "0")
        repeated = evaluate_candidate(state, self.latest)
        for day, period in state["periods"].items():
            self.assertEqual({k: v for k, v in repeated["periods"][day].items() if k != "source_digest"},
                             {k: v for k, v in period.items() if k != "source_digest"})
        self.assertEqual(evaluate_candidate(repeated, self.latest), repeated)

    def test_later_receipt_is_used_once_on_its_actual_date(self):
        state = build_historical_fbo_state_revision(self.book, self.latest)["candidate_state"]
        state = close_candidate_period(state, "2026-10-24", today="2026-10-27")
        state = evaluate_candidate(state, cap("2026-10-25", self.docs))
        state = close_candidate_period(state, "2026-10-25", today="2026-10-27")
        receipt = {"document_id": "synthetic-receipt-26", "root_document_id": "synthetic-receipt-26", "kind": "china_acceptance",
            "business_date": "2026-10-26", "posted_at": "2026-10-26T12:00:00Z", "events": [],
            "cost_document": {"domain": {}, "lines": [{"line_role": "accepted_pool_allocation", "facility_id": "synthetic-moscow",
                "pool": "FBO", "nm_id": 1, "quantity": 100, "capital_rub": "10000", "expense_rub": "0"}], "expense_lines": [], "movements": []}}
        receipt["fingerprint"] = fingerprint(receipt)
        docs = [*self.docs, receipt]
        state = evaluate_candidate(state, cap("2026-10-26", docs))
        state = close_candidate_period(state, "2026-10-26", today="2026-10-27")
        state = evaluate_candidate(state, cap("2026-10-27", docs))
        self.assertEqual(state["periods"]["2026-10-27"]["document_cost_state"]["fbo_rows"]["synthetic-moscow:FBO:1"]["quantity"], "100")
        self.assertEqual(state["periods"]["2026-10-27"]["applied_documents"], {})
        self.assertEqual(state["periods"]["2026-10-26"]["applied_documents"], {receipt["document_id"]: receipt["fingerprint"]})

    def test_book_or_native_source_drift_blocks_and_does_not_mutate(self):
        book = deepcopy(self.book); book["extra_revision"] = True
        with self.assertRaisesRegex(ValueError, "book_compare_and_swap_failed"):
            build_historical_fbo_state_revision(book, self.latest)
        capture = deepcopy(self.latest)
        capture["documents"][0]["cost_document"]["movements"][0]["capital_delta_rub"] = "-1"
        with self.assertRaisesRegex(ValueError, "capture_digest_mismatch"):
            build_historical_fbo_state_revision(self.book, capture)

    def test_failed_or_inactive_book_never_becomes_monetary_authority(self):
        for changes in ({"active": False}, {"schema": "other"}, {"publication_error": {"reason": "incomplete_period_cannot_close"}}):
            book = {**deepcopy(self.book), **changes}
            with self.assertRaisesRegex(ValueError, "active_healthy_book_required"):
                build_historical_fbo_state_revision(book, self.latest)

    def test_new_early_document_does_not_enter_the_approved_cost_gap(self):
        capture = cap("2026-10-24", [*self.docs, {"document_id": "unapproved", "business_date": "2026-10-20", "fingerprint": fingerprint("unapproved") }])
        with self.assertRaisesRegex(ValueError, "unapproved_new_document"):
            build_historical_fbo_state_revision(self.book, capture)


if __name__ == "__main__":
    unittest.main()
