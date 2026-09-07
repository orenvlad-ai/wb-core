#!/usr/bin/env python3
"""Independent document allocations and shipment-cost conservation scenarios."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from datetime import timedelta
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.fbs_snapshot_cost import (
    initialize_candidate, evaluate_candidate, close_candidate_period, candidate_period_view, fingerprint,
)


def line(facility="A", pool="FBS", nm=1, q=100, role="shipped", capital="999999", expense="0"):
    return {"line_no": 1, "facility_id": facility, "pool": pool, "nm_id": nm,
            "quantity": q, "line_role": role, "capital_rub": capital,
            "expense_rub": expense, "metadata_json": "{}"}


def doc(identity, kind, *, lines=(), domain=None, amount=None, day="2026-09-08", root=None, hour=12):
    result = {"document_id": identity, "kind": kind, "root_document_id": root or identity,
              "business_date": day, "posted_at": f"{day}T{hour:02}:00:00Z",
              "events": [], "cost_document": {"domain": domain or {}, "lines": list(lines),
                    "expense_lines": [] if amount is None else [{"amount_rub": str(amount)}],
                    "relations": [], "movements": []}}
    result["fingerprint"] = fingerprint(result)
    return result


def root(identity="t", src="A", dst="B", src_pool="FBS", dst_pool="FBS", day="2026-09-08"):
    return doc(identity, "transfer_root", day=day, domain={
        "source": {"facility_id": src, "pool": src_pool},
        "destination": {"facility_id": dst, "pool": dst_pool}})


def shipment(identity="s", transfer="t", q=500, day="2026-09-08", amount="0", src="A", pool="FBS", capital="999999"):
    return doc(identity, "transfer_shipment", root=transfer, day=day, amount=amount,
               lines=[line(src, pool, q=q, expense=amount, capital=capital)])


def receipt(identity="r", transfer="t", q=500, day="2026-09-08", dst="B", pool="FBS", hour=13):
    return doc(identity, "transfer_receipt", root=transfer, day=day, hour=hour,
               lines=[line(dst, pool, q=q, role="received")])


def china(identity="china", facility="A", q=1000, amount="200000", day="2026-09-08", pool="FBS", nm=1):
    return doc(identity, "china_acceptance", day=day, lines=[
        line(facility, pool, nm, q, role="accepted_pool_allocation", capital=amount)])


def overhead(identity="fee", amount="1000", scope="FBS", facility="A", day="2026-09-08"):
    # Deliberately incorrect old quantity and SKU allocation: authoritative
    # header money/scope, not these old operands, must drive the candidate.
    return doc(identity, "pool_overhead", day=day, amount=amount,
               domain={"facility_id": facility, "scope": scope, "amount_rub": amount},
               lines=[line(facility, "FBO", q=99999999, capital=amount, expense=amount, role="overhead_allocation")])


def capture(day="2026-09-07", docs=(), rows=None, fbo=()):
    if rows is None:
        rows = [("A", 1, 1000, "100"), ("B", 1, 1000, "200")]
    quantities = [{"facility_id": f, "nm_id": nm, "quantity": q} for f, nm, q, _w in rows]
    result = {"business_date": day, "captured_at": day + "T20:00:00Z",
              "quantity_snapshot": {"complete": True, "date": day, "captured_at": day + "T19:00:00Z",
                                    "id": "stock-" + day, "digest": fingerprint(quantities), "rows": quantities},
              "documents_complete": True, "documents": list(docs),
              "baseline_costs": {"available": True, "version_id": "published-original",
                  "document_manifest": {d["document_id"]: d["fingerprint"] for d in docs},
                  "rows": [{"facility_id": f, "nm_id": nm, "wac_rub": w, "source": {"version": "published-original"}} for f, nm, _q, w in rows],
                  "fbo_rows": [{"facility_id": f, "nm_id": nm, "quantity": q, "wac_rub": w,
                                "capital_rub": str(Decimal(w) * q), "source": {"version": "published-original"}} for f, nm, q, w in fbo]}}
    result["source_digest"] = fingerprint(result)
    return result


def row(state, key="A:1", day="2026-09-08"):
    return state["periods"][day]["rows"][key]


class DocumentCostTests(unittest.TestCase):
    def test_old_candidate_is_not_relabelled_as_new_policy(self):
        old=initialize_candidate(capture())
        old.update(schema="fbs_snapshot_cost_candidate_v1",policy="fbs_periodic_snapshot_fixed_opening_wac_v1")
        saved=deepcopy(old)
        with self.assertRaisesRegex(ValueError,"requires_document_cost_v2"):
            candidate_period_view(old,"2026-09-07")
        self.assertEqual(old,saved)
        old_capture=capture()
        old_capture["contract"]="fbs_snapshot_cost_sources_v1"
        with self.assertRaisesRegex(ValueError,"requires_document_cost_v2"):
            initialize_candidate(old_capture)

    def test_overhead_new_sku_weights_ignore_old_allocation(self):
        basis = [("A", 1, 1000, "100"), ("A", 2, 3000, "50"), ("B", 1, 1000, "200")]
        initial = initialize_candidate(capture(rows=basis))
        cap = capture("2026-09-08", [overhead()], rows=basis)
        result = evaluate_candidate(initial, cap)
        self.assertEqual(row(result)["wac_rub"], "100.25")
        self.assertEqual(row(result, "A:2")["wac_rub"], "50.25")
        self.assertEqual(row(result, "B:1")["wac_rub"], "200")
        self.assertEqual(result["pending_documents"], [])
        self.assertEqual(evaluate_candidate(result, cap), result)
        fee = result["periods"]["2026-09-08"]["document_valuation"]["allocations"][0]
        self.assertEqual(sum(fee["amounts_kopecks"].values()), 100000)

    def test_both_pools_split_new_mass_and_fbo_carry(self):
        initial = initialize_candidate(capture(fbo=[("A", 1, 1000, "300")]))
        result = evaluate_candidate(initial, capture("2026-09-08", [overhead(scope="both")]))
        self.assertEqual(row(result)["wac_rub"], "100.5")
        fbo = result["periods"]["2026-09-08"]["document_cost_state"]["fbo_rows"]["A:FBO:1"]
        self.assertEqual(fbo["wac_rub"], "300.5")
        self.assertEqual(fbo["capital_rub"], "300500.0")
        view=candidate_period_view(result,"2026-09-08")
        self.assertEqual(view["fbo_component"]["rows"]["A:FBO:1"],fbo)
        self.assertEqual(Decimal(view["capital_rub"])+Decimal(view["fbo_component"]["capital_rub"]),Decimal("601000"))

    def test_receipt_then_cost_allocation_uses_fixed_mass(self):
        initial = initialize_candidate(capture())
        docs = [overhead(amount="2000"), china()]
        cap = capture("2026-09-08", docs, rows=[("A", 1, 1500, "999"), ("B", 1, 1000, "999")])
        result = evaluate_candidate(initial, cap)
        self.assertEqual(row(result)["wac_rub"], "151")
        cap["quantity_snapshot"]["rows"][0]["quantity"] = 1400
        result2 = evaluate_candidate(result, cap)
        self.assertEqual(row(result2)["wac_rub"], "151")
        self.assertEqual(row(result2)["expense_capital_rub"], "2000")

    def test_same_day_transfer_uses_same_daily_price(self):
        initial = initialize_candidate(capture())
        docs = [root(), shipment(), receipt(), china()]
        cap = capture("2026-09-08", docs, rows=[("A", 1, 1500, "999"), ("B", 1, 1500, "999")])
        result = evaluate_candidate(initial, cap)
        self.assertEqual(row(result)["wac_rub"], "150")
        self.assertEqual(Decimal(row(result, "B:1")["receipt_capital_rub"]), Decimal("75000"))
        tr = result["periods"]["2026-09-08"]["document_cost_state"]["transfers"]["t:1"]
        self.assertEqual(Decimal(tr["base_capital_rub"]), Decimal("75000"))
        self.assertEqual(Decimal(tr["cost_reference"]["wac_rub"]), Decimal("150"))
        self.assertIn("valuation_basis_id", tr["cost_reference"])
        self.assertEqual(evaluate_candidate(result, cap), result)

    def test_future_partial_receipts_and_cancel_keep_shipment_price(self):
        initial = initialize_candidate(capture())
        docs = [root(), shipment(amount="5"), china()]
        first = evaluate_candidate(initial, capture("2026-09-08", docs, rows=[("A", 1, 1500, "999"), ("B", 1, 1000, "999")]))
        closed = close_candidate_period(first, "2026-09-08", today="2026-09-09")
        next_docs = docs + [receipt(q=200, day="2026-09-09"),
            doc("cancel", "transfer_cancellation", root="t", day="2026-09-09", hour=14,
                lines=[line(q=300, role="cancelled", expense="999999")]),
            china("new-price", q=1000, amount="400000", day="2026-09-09")]
        result = evaluate_candidate(closed, capture("2026-09-09", next_docs,
            rows=[("A", 1, 2800, "999"), ("B", 1, 1200, "999")]))
        self.assertEqual(Decimal(row(result, "B:1", "2026-09-09")["receipt_capital_rub"]), Decimal("30002"))
        self.assertEqual(Decimal(row(result, "A:1", "2026-09-09")["receipt_capital_rub"]), Decimal("445000"))
        self.assertEqual(result["periods"]["2026-09-08"], closed["periods"]["2026-09-08"])
        tr = result["periods"]["2026-09-09"]["document_cost_state"]["transfers"]["t:1"]
        self.assertEqual(tr["terminal_quantity"], 500)
        self.assertEqual(tr["cost_reference"], closed["periods"]["2026-09-08"]["document_cost_state"]["transfers"]["t:1"]["cost_reference"])

    def test_same_day_round_trip_conserves_value(self):
        basis = [("A", 1, 100, "100"), ("B", 1, 100, "200")]
        initial = initialize_candidate(capture(rows=basis))
        docs = [root(), shipment(q=50), receipt(q=50),
                root("back", src="B", dst="A"), shipment("s2", "back", q=50, src="B"),
                receipt("r2", "back", q=50, dst="A")]
        result = evaluate_candidate(initial, capture("2026-09-08", docs, rows=basis))
        self.assertAlmostEqual(Decimal(row(result)["wac_rub"]), Decimal("125"), places=30)
        self.assertAlmostEqual(Decimal(row(result,"B:1")["wac_rub"]), Decimal("175"), places=30)
        self.assertAlmostEqual(sum(Decimal(v["capital_rub"]) for v in result["periods"]["2026-09-08"]["rows"].values()), Decimal("30000"), places=25)

    def test_fbs_fbo_roundtrip_never_uses_old_carried_capital(self):
        basis = [("A", 1, 100, "100"), ("B", 1, 0, "200")]
        initial = initialize_candidate(capture(rows=basis, fbo=[("A", 1, 100, "300")]))
        out = doc("out", "pool_reallocation", domain={"facility_id":"A","source_pool":"FBS","destination_pool":"FBO"},
                  lines=[line(pool="FBO",q=50,role="reallocated",capital="99999999")])
        back = doc("back", "pool_reallocation", domain={"facility_id":"A","source_pool":"FBO","destination_pool":"FBS"},
                   lines=[line(q=50,role="reallocated",capital="1")])
        result = evaluate_candidate(initial, capture("2026-09-08",[out,back],rows=basis))
        self.assertAlmostEqual(Decimal(row(result)["wac_rub"]), Decimal("150"), places=30)
        fbo=result["periods"]["2026-09-08"]["document_cost_state"]["fbo_rows"]["A:FBO:1"]
        self.assertAlmostEqual(Decimal(fbo["wac_rub"]), Decimal("250"), places=30)

    def test_prebaseline_transit_is_accepted_once(self):
        old = [root(day="2026-09-07"), shipment(q=100,day="2026-09-07",capital="12345",amount="1"),
               receipt(q=20,day="2026-09-07")]
        initial=initialize_candidate(capture(docs=old))
        result=evaluate_candidate(initial,capture("2026-09-08",old+[receipt("remaining",q=80)]))
        self.assertEqual(Decimal(row(result,"B:1")["receipt_capital_rub"]),Decimal("9876.8"))
        self.assertEqual(initial["baseline"],result["baseline"])

    def test_initial_partial_transit_keeps_original_kopeck_remainder(self):
        old=[root(day="2026-09-07"),shipment(q=3,day="2026-09-07",capital="1.00"),receipt(q=1,day="2026-09-07")]
        initial=initialize_candidate(capture(docs=old))
        result=evaluate_candidate(initial,capture("2026-09-08",old+[receipt("rest",q=2)]))
        self.assertEqual(Decimal(row(result,"B:1")["receipt_capital_rub"]),Decimal("0.67"))

    def test_late_transfer_fee_partial_loss_and_remaining_receipt(self):
        initial=initialize_candidate(capture())
        docs=[root(),shipment(q=100,amount="0.03"),receipt(q=33),
              doc("late","late_expense",root="t",amount="0.05",hour=14),
              doc("loss","transfer_loss",root="t",hour=15,lines=[line(q=33,role="lost")]),
              receipt("last",q=34,hour=16)]
        result=evaluate_candidate(initial,capture("2026-09-08",docs))
        self.assertEqual(Decimal(row(result,"B:1")["receipt_capital_rub"]),Decimal("6700.04"))
        self.assertEqual(Decimal(row(result,"B:1")["expense_capital_rub"]),Decimal("0.01"))
        self.assertEqual(result["pending_documents"],[])

    def test_open_storno_cancels_new_allocations_without_history_edit(self):
        initial=initialize_candidate(capture())
        fee=overhead()
        reverse=doc("undo","storno",domain={"target_document_id":"fee"},hour=14)
        result=evaluate_candidate(initial,capture("2026-09-08",[fee,reverse]))
        self.assertEqual(row(result)["wac_rub"],"100")
        self.assertEqual(result["pending_documents"],[])
        self.assertEqual(result["baseline"],initial["baseline"])

    def test_over_receipt_and_missing_source_fail_explicitly(self):
        initial=initialize_candidate(capture())
        with self.assertRaisesRegex(ValueError,"exceeds_new_reference"):
            evaluate_candidate(initial,capture("2026-09-08",[root(),shipment(q=100),receipt(q=101)]))
        with self.assertRaisesRegex(ValueError,"without_saved_shipment"):
            evaluate_candidate(initial,capture("2026-09-08",[root(),receipt()]))

    def test_discrepancy_returns_expected_and_values_actual_sku(self):
        basis=[("A",1,1000,"100"),("A",2,1000,"200"),("B",1,0,"100"),("B",2,0,"200")]
        initial=initialize_candidate(capture(rows=basis))
        discrepancy=doc("wrong-sku","transfer_discrepancy",root="t",hour=14,
            lines=[line(q=50,role="expected_not_sent"),line("B",nm=2,q=50,role="unexpected")])
        docs=[root(),shipment(q=100,amount="10"),discrepancy]
        result=evaluate_candidate(initial,capture("2026-09-08",docs,rows=basis))
        self.assertEqual(Decimal(row(result,"B:2")["receipt_capital_rub"]),Decimal("10005"))
        self.assertEqual(Decimal(row(result)["receipt_capital_rub"]),Decimal("5000"))
        self.assertEqual(result["pending_documents"],[])

    def test_signed_independent_expense_preserves_core_contract(self):
        from apps.fbs_snapshot_cost_smoke import capture as simple_capture, document
        initial=initialize_candidate(simple_capture())
        result=evaluate_candidate(initial,simple_capture("2026-09-08",docs=[document(),document("discount",kind="expense",quantity="0",capital="-2000")]))
        self.assertEqual(row(result,"ff-1:1")["wac_rub"],"149")

    def test_no_cost_mass_cannot_hide_late_expense_in_zero_fbo(self):
        docs=[root(dst_pool="FBO",day="2026-09-07"),shipment(q=100,day="2026-09-07"),receipt(q=100,pool="FBO",day="2026-09-07")]
        initial=initialize_candidate(capture(docs=docs))
        late=doc("late","late_expense",root="t",amount="100")
        with self.assertRaisesRegex(ValueError,"expense_without_cost_mass"):
            evaluate_candidate(initial,capture("2026-09-08",docs+[late]))

    def test_query_only_source_to_candidate_with_expense_and_transfer(self):
        from apps.fbs_snapshot_cost_sources_smoke import fixture, document, P, NOW
        from packages.application.fbs_snapshot_cost_sources import capture_current
        with TemporaryDirectory(prefix="fbs-document-integration-") as tmp:
            path=Path(tmp)/"source.sqlite3"
            conn=fixture(path)
            initial=initialize_candidate(capture_current(path,now=NOW))
            day="2026-09-06"
            conn.execute(f"UPDATE {P}wb_fbs_stock_snapshot_runs SET snapshot_at=?",(day+"T10:00:00Z",))
            document(conn,"china","china_acceptance",role="accepted_pool_allocation",q=1,capital="40",day=day)
            domain={"source":{"facility_id":"A","pool":"FBS"},"destination":{"facility_id":"B","pool":"FBS"}}
            document(conn,"root","transfer_root",role=None,q=0,capital="0",day=day,domain=domain)
            document(conn,"shipment","transfer_shipment",role="shipped",q=2,capital="999999",day=day,
                     domain=domain,root_id="root",relation="shipment_of")
            conn.execute(f"UPDATE {P}ff_pool_movement_lines SET quantity_delta=-2,capital_delta_rub='-999999' WHERE operation_id='shipment'")
            document(conn,"receipt","transfer_receipt",role="received",q=2,capital="999999",day=day,
                     facility="B",domain=domain,root_id="root",relation="receipt_of")
            document(conn,"expense","pool_overhead",role="overhead_allocation",q=999999,capital="90",expense="90",day=day,
                     facility="B",pool="FBO",domain={"facility_id":"B","scope":"both","amount_rub":"90"})
            conn.execute(f"INSERT INTO {P}ff_pool_document_expense_lines VALUES('expense',1,'90','Approved','','{{}}')")
            for table in ("ff_pool_balances","warehouse_functional_balances","warehouse_functional_versions","warehouse_business_projection_current_rows"):
                conn.execute(f"DROP TABLE {P}{table}")
            conn.commit()
            before=path.read_bytes()
            captured=capture_current(path,now=NOW+timedelta(days=1),include_baseline=False)
            self.assertTrue(captured["documents_complete"])
            self.assertEqual(path.read_bytes(),before)
            result=evaluate_candidate(initial,captured)
            self.assertEqual(result["pending_documents"],[])
            self.assertEqual(Decimal(row(result,"A:1",day)["wac_rub"]),Decimal("25"))
            self.assertEqual(Decimal(row(result,"B:1",day)["receipt_capital_rub"]),Decimal("50"))
            self.assertEqual(Decimal(row(result,"B:1",day)["expense_capital_rub"]),Decimal("57.27"))
            aux=result["periods"][day]["document_cost_state"]
            self.assertEqual(Decimal(aux["fbo_rows"]["B:FBO:1"]["capital_rub"]),Decimal("152.73"))
            self.assertEqual(evaluate_candidate(result,captured),result)
            closed=close_candidate_period(result,day,today="2026-09-07")
            self.assertEqual(closed["periods"][day]["status"],"closed")
            conn.execute(f"INSERT INTO {P}ff_pool_movement_lines VALUES('unknown-fbo',1,'B','FBO',1,-1,'-30','{{}}')")
            conn.commit()
            invalid=capture_current(path,now=NOW+timedelta(days=1),include_baseline=False)
            self.assertFalse(invalid["documents_complete"])
            self.assertEqual(invalid["documents_reason"],"unmapped_fbo_document_movement")
            conn.close()


if __name__ == "__main__":
    unittest.main()
