#!/usr/bin/env python3
"""Daily shared-cost arithmetic, exact-date and persistence boundaries."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.fbs_snapshot_cost_smoke import capture
from packages.application.fbs_snapshot_cost import initialize_candidate, evaluate_candidate, close_candidate_period, fingerprint
from packages.application.shared_sku_cost import (
    SharedSkuCostError, SharedSkuCostSnapshot, SharedSkuCostStore, build_shared_cost_day,
)


def wb(day="2026-09-07", *, q="500", capital="100000"):
    value = {"contract": "shared_sku_cost_wb_source_v1", "business_date": day,
             "complete": True, "reason": "", "version_id": "wb-good-1",
             "rows": [{"nm_id": 1, "quantity": q, "capital_rub": capital,
                       "quality": "published", "source": {"version_id": "wb-good-1"}}]}
    value["source_digest"] = fingerprint(value)
    return value


def fbs(*, quantity="1000", wac="100", fbo_quantity="500", fbo_wac="300"):
    c = capture(quantity=quantity, wac=wac)
    c["baseline_costs"]["fbo_rows"] = [{"nm_id": 1, "facility_id": "ff-1", "quantity": fbo_quantity,
        "capital_rub": str(Decimal(fbo_quantity)*Decimal(fbo_wac)), "wac_rub": fbo_wac,
        "source": {"version_id": "published-1"}}]
    return initialize_candidate(c)


class SharedCostTests(unittest.TestCase):
    def test_same_sku_weighted_sum_and_matching_fbo(self):
        p = build_shared_cost_day(fbs(), wb(), "2026-09-07")
        r = p["rows"]["1"]
        self.assertEqual(r["unit_cost_rub"], "175")  # (100000+100000+150000)/2000
        self.assertEqual(r["quantity"], "2000")
        self.assertEqual(r["capital_rub"], "350000")
        self.assertEqual([x["component"] for x in r["components"]], ["WB", "FF_FBS", "FF_FBO"])
        self.assertEqual(p["status"], "preliminary")

    def test_zero_wb_with_positive_fbs_is_priced(self):
        p = build_shared_cost_day(fbs(fbo_quantity="0"), wb(q="0",capital="0"), "2026-09-07")
        self.assertEqual(p["rows"]["1"]["unit_cost_rub"], "100")
        self.assertEqual(p["rows"]["1"]["status"], "resolved")

    def test_no_cross_sku_mix(self):
        state = fbs(fbo_quantity="0")
        state["baseline"]["rows"]["ff-1:2"] = {"nm_id":2,"facility_id":"ff-1","quantity":"10","wac_rub":"900","capital_rub":"9000","quality":"accepted_initial_cost"}
        source = wb()
        source["rows"].append({"nm_id":2,"quantity":"0","capital_rub":"0"})
        p = build_shared_cost_day(state, source, "2026-09-07")
        self.assertEqual(p["rows"]["2"]["unit_cost_rub"], "900")
        self.assertEqual(Decimal(p["rows"]["1"]["unit_cost_rub"]).quantize(Decimal(".0001")),Decimal("133.3333"))

    def test_missing_and_partial_never_zero_or_other_channel(self):
        for edit in (lambda x:x.update(complete=False,reason="snapshot_incomplete"),
                     lambda x:x.update(rows=[]),
                     lambda x:x["rows"][0].update(quantity=None,capital_rub=None,status="missing"),
                     lambda x:x["rows"][0].update(capital_rub="0")):
            source=wb(); edit(source)
            p=build_shared_cost_day(fbs(),source,"2026-09-07")
            r=SharedSkuCostSnapshot([p],effective_date="2026-09-07").resolve(nm_id="1",operation_date=date(2026,9,7))
            self.assertEqual(r["status"],"missing")
            self.assertNotIn("unit_cost_rub",r)

    def test_exact_date_and_zero_total(self):
        p=build_shared_cost_day(fbs(quantity="0",fbo_quantity="0"),wb(q="0",capital="0"),"2026-09-07")
        snap=SharedSkuCostSnapshot([p],effective_date="2026-09-07")
        self.assertEqual(snap.resolve(nm_id="1",operation_date=date(2026,9,7))["reason"],"no_inventory_weight_for_shared_cost")
        self.assertEqual(snap.resolve(nm_id="1",operation_date=date(2026,9,8))["reason"],"shared_cost_exact_date_missing")
        self.assertFalse(snap.applies_to(date(2026,9,6)))
        with self.assertRaisesRegex(SharedSkuCostError,"date_mismatch"):
            build_shared_cost_day(fbs(),wb("2026-09-08"),"2026-09-07")

    def test_duplicate_and_unrequested_sku(self):
        source=wb();source["rows"].append(deepcopy(source["rows"][0]))
        with self.assertRaisesRegex(SharedSkuCostError,"duplicate_wb_sku"):
            build_shared_cost_day(fbs(),source,"2026-09-07")
        source["rows"][1]["nm_id"]=999
        with self.assertRaisesRegex(SharedSkuCostError,"outside_fbs_catalog"):
            build_shared_cost_day(fbs(),source,"2026-09-07")

    def test_pending_document_blocks_blend(self):
        state=evaluate_candidate(fbs(),capture("2026-09-08"))
        state["pending_documents"]=[{"document_id":"late","reason":"late_closed_period_document"}]
        p=build_shared_cost_day(state,wb("2026-09-08"),"2026-09-08")
        self.assertEqual(p["rows"]["1"]["reason"],"fbs_period_incomplete")

    def test_pinned_snapshot_immutable_and_versioned(self):
        p=build_shared_cost_day(fbs(),wb(),"2026-09-07")
        snap=SharedSkuCostSnapshot([p],effective_date="2026-09-07")
        p["rows"]["1"]["unit_cost_rub"]="999"
        self.assertEqual(snap.resolve(nm_id="1",operation_date=date(2026,9,7))["unit_cost_rub"],"175")
        with self.assertRaisesRegex(SharedSkuCostError,"fingerprint_mismatch"):
            SharedSkuCostSnapshot([p],effective_date="2026-09-07")
        with self.assertRaises(TypeError):
            snap._days["2026-09-07"]["rows"]["1"]["unit_cost_rub"]="999"

    def test_store_cas_pinning_closed_immutability(self):
        with TemporaryDirectory() as tmp:
            store=SharedSkuCostStore(Path(tmp)/"candidate.sqlite3")
            p=build_shared_cost_day(fbs(),wb(),"2026-09-07")
            old=store.save(p,expected_version=None)
            snap=store.snapshot(effective_date="2026-09-07")
            self.assertEqual(store.save(p,expected_version=old),old)
            new=build_shared_cost_day(fbs(),wb(capital="200000"),"2026-09-07")
            with self.assertRaisesRegex(SharedSkuCostError,"compare_and_swap"):
                store.save(new,expected_version=None)
            store.save(new,expected_version=old)
            self.assertEqual(snap.resolve(nm_id="1",operation_date=date(2026,9,7))["unit_cost_rub"],"175")
            self.assertEqual(store.snapshot(effective_date="2026-09-07").resolve(nm_id="1",operation_date=date(2026,9,7))["unit_cost_rub"],"225")
            state=evaluate_candidate(fbs(),capture("2026-09-08"))
            state=close_candidate_period(state,"2026-09-08",today="2026-09-09")
            closed=build_shared_cost_day(state,wb("2026-09-08"),"2026-09-08")
            store.save(closed,expected_version=None)
            changed=build_shared_cost_day(state,wb("2026-09-08",capital="200000"),"2026-09-08")
            with self.assertRaisesRegex(SharedSkuCostError,"immutable"):
                store.save(changed,expected_version=closed["version_id"])

    def test_foreign_db_readonly_rejection(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"production.sqlite3"
            conn=sqlite3.connect(path);conn.execute("CREATE TABLE business_data(id INTEGER)");conn.commit();conn.close()
            before=path.read_bytes()
            with self.assertRaisesRegex(SharedSkuCostError,"not_an_isolated"):
                SharedSkuCostStore(path).save(build_shared_cost_day(fbs(),wb(),"2026-09-07"),expected_version=None)
            self.assertEqual(path.read_bytes(),before)

    def test_initial_basis_cannot_be_replaced(self):
        original=build_shared_cost_day(fbs(),wb(),"2026-09-07")
        other=build_shared_cost_day(fbs(wac="101"),wb(),"2026-09-07")
        with TemporaryDirectory() as tmp:
            store=SharedSkuCostStore(Path(tmp)/"candidate.sqlite3")
            store.save(original,expected_version=None)
            with self.assertRaisesRegex(SharedSkuCostError,"mixed_fbs_initial"):
                store.save(other,expected_version=original["version_id"])
            self.assertEqual(store.read("2026-09-07"),original)

    def test_closed_version_independent_of_later_state(self):
        initial=fbs()
        baseline=build_shared_cost_day(initial,wb(),"2026-09-07")
        state=evaluate_candidate(initial,capture("2026-09-08"))
        closed=close_candidate_period(state,"2026-09-08",today="2026-09-09")
        p=build_shared_cost_day(closed,wb("2026-09-08"),"2026-09-08")
        next_day=evaluate_candidate(closed,capture("2026-09-09",quantity="900"))
        next_day["pending_documents"]=[{"document_id":"late","reason":"late_closed_period_document"}]
        self.assertEqual(build_shared_cost_day(next_day,wb("2026-09-08"),"2026-09-08"),p)
        self.assertEqual(build_shared_cost_day(next_day,wb(),"2026-09-07"),baseline)
        with TemporaryDirectory() as tmp:
            store=SharedSkuCostStore(Path(tmp)/"candidate.sqlite3")
            store.save(p,expected_version=None)
            self.assertEqual(store.save(build_shared_cost_day(next_day,wb("2026-09-08"),"2026-09-08"),expected_version=p["version_id"]),p["version_id"])


if __name__ == "__main__":
    unittest.main()
