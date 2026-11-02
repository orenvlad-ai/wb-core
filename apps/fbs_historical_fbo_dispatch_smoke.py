#!/usr/bin/env python3
"""Synthetic local checks; no database, source capture or publication occurs."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.fbs_historical_fbo_dispatch import (
    CAPTURE_CONTRACT, HistoricalFboDispatchError, prepare_historical_fbo_dispatch_plan,
    validate_historical_fbo_dispatch_plan,
)


def fixture():
    scope = {"facility_id": "synthetic-moscow", "dispatches": [
        {"supply_id": "101", "business_date": "2026-10-19", "expected_quantity": 20},
        {"supply_id": "102", "business_date": "2026-10-21", "expected_quantity": 40},
        {"supply_id": "103", "business_date": "2026-10-23", "expected_quantity": 30}],
        "reallocation": {"business_date": "2026-10-20", "nm_id": 1, "quantity": 10,
                         "source_pool": "FBS", "destination_pool": "FBO",
                         "documentary_source_digest": fingerprint("synthetic distribution")}}
    capture = {"contract": CAPTURE_CONTRACT, "book_version": fingerprint("synthetic book"),
        "native_query_fence_digest": fingerprint("synthetic pinned source tables"),
        "opening": {"business_date": "2026-10-15", "period_digest": fingerprint("synthetic closed period"),
            "source_digest": fingerprint("synthetic opening"), "document_manifest": {},
            "fbo_rows": {"synthetic-moscow:FBO:1": {"facility_id": "synthetic-moscow", "nm_id": 1,
                         "quantity": "80", "capital_rub": "4000", "wac_rub": "50"}},
            "fbs_reallocation_row": {"facility_id": "synthetic-moscow", "nm_id": 1,
                                     "quantity": "100", "capital_rub": "10000", "wac_rub": "100"}},
        "native_cost_document_coverage": {"complete": True, "date_from": "2026-10-16", "date_to": "2026-10-23",
            "documents": [], "requests": [], "query_fence_digest": fingerprint("synthetic empty interval")},
        "known_document_manifest": {},
        "protected_source_fingerprints": {"synthetic-protected-source": fingerprint("unchanged expense")},
        "reallocation_physical_source": {"business_date": "2026-10-20", "facility_id": "synthetic-moscow", "nm_id": 1,
            "complete": True, "quantity_before": 10, "source_digest": fingerprint("synthetic physical proof")},
        "restored_pool_allocation": {"basis": "owner_authorized_documentary_restoration", "business_date": "2026-10-20",
            "source_pool": "FBS", "destination_pool": "FBO", "nm_id": 1, "quantity": 10, "facility_id": "synthetic-moscow",
            "source_digest": fingerprint("synthetic owner instruction"), "native_ledger_basis_digest": fingerprint("synthetic native attribution")},
        "supply_sources": {d["supply_id"]: {"supply_id": d["supply_id"], "wb_supply_id": d["supply_id"],
            "cache_key": "supply:" + d["supply_id"], "business_date": d["business_date"],
            "source_digest": fingerprint(["synthetic WB source", d]), "existing_debits": [],
            "items": [{"nm_id": 1, "quantity": d["expected_quantity"]}]} for d in scope["dispatches"]}}
    seal(capture)
    return capture, scope


def seal(capture):
    capture["source_digest"] = fingerprint({k: v for k, v in capture.items() if k != "source_digest"})


class HistoricalFboDispatchTests(unittest.TestCase):
    def test_sequential_daily_cost_uses_historical_basis_and_conserves_distribution(self):
        capture, scope = fixture()
        before = deepcopy(capture)
        plan = prepare_historical_fbo_dispatch_plan(capture, scope)
        self.assertEqual(plan["status"], "prepared_unconnected", plan)
        self.assertEqual(capture, before)
        self.assertFalse(plan["database_written"])
        self.assertEqual(plan["official_quantity_periods_created"], [])
        self.assertEqual(len(plan["effects"]), 4)
        first, transfer, second, third = plan["effects"]
        self.assertEqual(first["pool_movements"][0]["wac_rub"], "50")
        self.assertEqual(transfer["aggregate_movements"], [])
        self.assertEqual(sum(m["quantity_delta"] for m in transfer["pool_movements"]), 0)
        self.assertEqual(sum(Decimal(m["capital_delta_rub"]) for m in transfer["pool_movements"]), 0)
        self.assertEqual(transfer["pool_movements"][1]["capital_delta_rub"], "1000")
        self.assertEqual(transfer["expense_rub"], "0")
        self.assertEqual(plan["fbo_rows_after"]["synthetic-moscow:FBO:1"]["quantity"], "0")
        self.assertTrue(Decimal(second["pool_movements"][0]["wac_rub"]) > 50)
        self.assertEqual(second["pool_movements"], second["aggregate_movements"])
        self.assertEqual(first["source_key"], "wb_supply_debit:supply:101")
        self.assertTrue(plan["unresolved_obligations"])
        self.assertEqual(validate_historical_fbo_dispatch_plan(plan, capture=capture, scope=scope), plan)

    def test_current_and_late_price_never_supply_missing_historical_cost(self):
        capture, scope = fixture()
        capture["current_pool_wac"] = "150"
        capture["late_26_oct_receipt_wac"] = "200"
        seal(capture)
        result = prepare_historical_fbo_dispatch_plan(capture, scope)
        self.assertEqual(result["effects"][0]["pool_movements"][0]["wac_rub"], "50")
        capture["opening"]["fbo_rows"]["synthetic-moscow:FBO:1"]["wac_rub"] = None
        seal(capture)
        result = prepare_historical_fbo_dispatch_plan(capture, scope)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("effects", result)

    def test_future_distribution_does_not_cover_earlier_per_sku_shortage(self):
        capture, scope = fixture()
        row = capture["opening"]["fbo_rows"]["synthetic-moscow:FBO:1"]
        row.update(quantity="19", capital_rub="950")
        seal(capture)
        result = prepare_historical_fbo_dispatch_plan(capture, scope)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "negative_independent_fbo_quantity")
        self.assertNotIn("effects", result)

    def test_accounting_available_is_not_physical_reallocation_stock(self):
        capture, scope = fixture()
        capture["reallocation_physical_source"]["quantity_before"] = 9
        seal(capture)
        result = prepare_historical_fbo_dispatch_plan(capture, scope)
        self.assertEqual(result["reason"], "historical_fbo_reallocation_physical_basis_missing")

    def test_post_recovery_book_rebinding_invalidates_equal_numeric_preview(self):
        capture, scope = fixture()
        plan = prepare_historical_fbo_dispatch_plan(capture, scope)
        capture["book_version"] = fingerprint("post recovery book")
        seal(capture)
        with self.assertRaisesRegex(HistoricalFboDispatchError, "plan_or_source_changed"):
            validate_historical_fbo_dispatch_plan(plan, capture=capture, scope=scope)

    def test_existing_debit_or_alias_ambiguity_never_creates_second_effect(self):
        for edit in (lambda s: s.update(cache_key="other-alias"),
                     lambda s: s.update(existing_debits=["existing-native-operation"]),
                     lambda s: s["items"].append(deepcopy(s["items"][0]))):
            capture, scope = fixture()
            edit(capture["supply_sources"]["101"])
            seal(capture)
            result = prepare_historical_fbo_dispatch_plan(capture, scope)
            self.assertEqual(result["status"], "blocked")
            self.assertNotIn("effects", result)

    def test_nonempty_or_incomplete_cost_interval_is_not_zero(self):
        for change in ({"complete": False}, {"date_to": "2026-10-22"},
                       {"documents": ["new unreviewed cost source"]}, {"requests": ["new request"]}):
            capture, scope = fixture()
            capture["native_cost_document_coverage"].update(change)
            seal(capture)
            self.assertEqual(prepare_historical_fbo_dispatch_plan(capture, scope)["status"], "blocked")

    def test_late_receipt_cannot_repair_previous_shortage_or_enter_scope(self):
        capture, scope = fixture()
        capture["native_cost_document_coverage"]["documents"] = [{"business_date": "2026-10-26", "quantity": 10000}]
        seal(capture)
        self.assertEqual(prepare_historical_fbo_dispatch_plan(capture, scope)["status"], "blocked")

    def test_original_source_fingerprints_are_retained_and_bound(self):
        capture, scope = fixture()
        plan = prepare_historical_fbo_dispatch_plan(capture, scope)
        self.assertEqual(plan["protected_source_fingerprints"], capture["protected_source_fingerprints"])
        capture["protected_source_fingerprints"]["synthetic-protected-source"] = fingerprint("changed native money")
        seal(capture)
        with self.assertRaisesRegex(HistoricalFboDispatchError, "plan_or_source_changed"):
            validate_historical_fbo_dispatch_plan(plan, capture=capture, scope=scope)

    def test_tampered_costs_or_missing_capture_digest_refused(self):
        capture, scope = fixture()
        plan = prepare_historical_fbo_dispatch_plan(capture, scope)
        plan["effects"][0]["pool_movements"][0]["capital_delta_rub"] = "-1"
        with self.assertRaisesRegex(HistoricalFboDispatchError, "plan_or_source_changed"):
            validate_historical_fbo_dispatch_plan(plan, capture=capture, scope=scope)
        capture["opening"]["fbs_reallocation_row"]["wac_rub"] = "999"
        self.assertEqual(prepare_historical_fbo_dispatch_plan(capture, scope)["reason"], "historical_fbo_capture_digest_mismatch")


if __name__ == "__main__":
    unittest.main()
