#!/usr/bin/env python3
"""Synthetic dated-book, exact document-byte and adversarial replay fixtures.

All writer characterization uses TemporaryDirectory only. The new planner has
no runtime/production IO; these tests do not construct a runtime or call a runner.
"""
from copy import deepcopy
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.fbs_document_cost_smoke import china, doc, overhead, receipt, root, shipment, line
from packages.application.fbs_snapshot_cost import (
    initialize_candidate, evaluate_candidate, close_candidate_period, fingerprint, canonical,
)
from packages.application.fbs_snapshot_cost_sources import _events
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot
from packages.application.shared_sku_cost import build_shared_cost_day, SharedSkuCostSnapshot
from packages.application import fbs_accounting_historical_revision as revision

BASE = "2026-09-07"
DAY = "2026-09-08"
END = "2026-09-10"
BASIS = [("A", 1, 1000, "100"), ("B", 1, 1000, "200")]


def native(value):
    """Construct the exact shape returned by _documents from synthetic facts."""
    raw = deepcopy(value["cost_document"])
    identity, kind = value["document_id"], value["kind"]
    header = dict(document_id=identity, document_kind=kind, root_document_id=value["root_document_id"],
                  operation_id=identity, source_system="synthetic", source_type=kind,
                  source_id=identity, source_revision="rev:" + identity, idempotency_epoch=1,
                  business_date=value["business_date"], posted_at=value["posted_at"])
    for n, item in enumerate(raw["lines"], 1):
        item.update(document_id=identity, line_no=n)
    for n, item in enumerate(raw["expense_lines"], 1):
        item.update(document_id=identity, expense_line_no=n, basis="Approved", source_file_sha256="", metadata_json="{}")
    raw["movements"] = []
    for item in raw["lines"]:
        q = 0 if kind == "pool_overhead" else item["quantity"]
        capital = Decimal(item["capital_rub"]) + (Decimal(item["expense_rub"]) if kind == "china_acceptance" else 0)
        sign = -1 if kind == "transfer_shipment" else 1
        raw["movements"].append(dict(operation_id=header["operation_id"], line_no=item["line_no"],
            facility_id=item["facility_id"], pool=item["pool"], nm_id=item["nm_id"],
            quantity_delta=sign * q, capital_delta_rub=str(sign * capital), metadata_json="{}"))
    relation = None
    if header["root_document_id"] != identity:
        relation = dict(parent_document_id=header["root_document_id"],
                        relation_type={"transfer_shipment": "shipment_of", "transfer_receipt": "receipt_of",
                                       "transfer_loss": "loss_of"}[kind])
        raw["relations"] = [{**relation, "child_document_id": identity, "root_document_id": header["root_document_id"]}]
    role = "root" if header["root_document_id"] == identity else kind.removeprefix("transfer_")
    # Synthetic fixtures mirror native _apply_plan serialization, including
    # ordered posted lines and the immutable request's document cohort.
    posted = {key: header[key] for key in ("document_id", "document_kind", "root_document_id", "business_date")}
    posted.update(contract_name="ff_pool_business_documents_v1", request_id="request:" + identity,
        document_role=role, feature_epoch=1, primary_document_id=identity,
        domain=raw["domain"], source={"system": header["source_system"], "type": kind,
        "id": identity, "revision": header["source_revision"], "idempotency_epoch": 1, "file_sha256": ""},
        documents=[dict(document_id=identity, document_kind=kind, document_role=role,
                        root_document_id=header["root_document_id"], relation=relation,
                        line_count=len(raw["lines"]), movement_count=len(raw["movements"]),
                        expense_line_count=len(raw["expense_lines"]))],
        lines=[{**{key: v[key] for key in ("line_role", "facility_id", "pool", "nm_id", "quantity", "capital_rub", "expense_rub")},
                "metadata": json.loads(v["metadata_json"])} for v in raw["lines"]])
    encoded = canonical(posted)
    header["posted_manifest_sha256"] = fingerprint(posted)
    raw.update(contract="ff_pool_posted_cost_document_v1", posted_manifest_sha256=header["posted_manifest_sha256"])
    result = {**header, "kind": kind, "cost_document": raw,
              "events": _events(header, raw["lines"], raw["expense_lines"], raw["movements"])}
    result["fingerprint"] = fingerprint({"document": header, "lines": raw["lines"], "expenses": raw["expense_lines"],
                                         "relations": raw["relations"], "movements": raw["movements"]})
    return result, encoded


def captured(day, facts=(), basis=BASIS, fbo=(), quantities=None):
    items = [native(v) for v in sorted(facts, key=lambda v: v["document_id"])]
    rows = [dict(facility_id=f, nm_id=nm, quantity=(quantities or {}).get((f, nm), q)) for f, nm, q, _w in basis]
    result = dict(contract="fbs_snapshot_cost_sources_v2", business_date=day, captured_at=day + "T20:00:00Z",
                  documents_complete=True, documents_reason="", documents=[v[0] for v in items],
                  quantity_snapshot=dict(complete=True, date=day, id="stock:" + day,
                      digest=fingerprint(rows), captured_at=day + "T19:00:00Z", rows=rows),
                  baseline_costs=dict(available=True, version_id="accepted-initial",
                      document_manifest={v[0]["document_id"]: v[0]["fingerprint"] for v in items},
                      rows=[dict(facility_id=f, nm_id=nm, wac_rub=w, source={"version": "accepted-initial"}) for f, nm, _q, w in basis],
                      fbo_rows=[dict(facility_id=f, nm_id=nm, quantity=q, wac_rub=w,
                          capital_rub=str(Decimal(w)*q), source={"version": "accepted-initial"}) for f, nm, q, w in fbo]),
                  posted_manifest_json_by_id={v[0]["document_id"]: v[1] for v in items})
    rehash_capture(result)
    return result


def rehash_capture(cap):
    cap["source_digest"] = fingerprint({key: cap[key] for key in (
        "contract", "business_date", "quantity_snapshot", "documents_complete", "documents", "documents_reason")})


def rehash_normalized(doc):
    header = {key: doc[key] for key in revision.HEADER_FIELDS}
    raw = doc["cost_document"]
    doc["events"] = _events(header, raw["lines"], raw["expense_lines"], raw["movements"])
    doc["fingerprint"] = fingerprint(dict(document=header, lines=raw["lines"], expenses=raw["expense_lines"],
                                         relations=raw["relations"], movements=raw["movements"]))


def rebind_posted(cap, identity, posted):
    doc = next(v for v in cap["documents"] if v["document_id"] == identity)
    encoded = canonical(posted)
    cap["posted_manifest_json_by_id"][identity] = encoded
    doc["posted_manifest_sha256"] = doc["cost_document"]["posted_manifest_sha256"] = fingerprint(posted)
    rehash_normalized(doc)
    rehash_capture(cap)


def dated_sources(day, basis):
    nms = sorted({nm for _f, nm, _q, _w in basis})
    wb = dict(contract="shared_sku_cost_wb_source_v1", business_date=day, complete=True,
              authority_complete=True, version_id="wb:" + day, reason="", rows=[
                  dict(nm_id=nm, status="available", quantity="500", capital_rub="100000",
                       quality="published", components={"physical": "500"}, source={"version_id": "wb:" + day}) for nm in nms])
    wb["source_digest"] = fingerprint(wb)
    rows = {}
    for nm in nms:
        stages = {s: dict(quantity="10", capital_rub="500", wac_rub="50")
                  for s in ("PRODUCTION", "PRODUCTION_TO_FF", "FF_TO_WB", "WB_ACCEPTANCE_DISCREPANCY")}
        stages["WB"] = dict(quantity="500", capital_rub="100000", wac_rub="200")
        rows[str(nm)] = dict(stages=stages, identity={"name": "Synthetic", "sku": str(nm)})
    retained = dict(date=day, wb_version_id=wb["version_id"], rows=rows)
    retained["source_digest"] = fingerprint(retained)
    return wb, retained


def saved_book(*, end=END, facts=(), basis=BASIS, fbo=(), quantities=None, native_capture=None):
    initial = captured(BASE, [v for v in facts if v["business_date"] <= BASE], basis, fbo,
                       (quantities or {}).get(BASE))
    if native_capture is not None:
        initial["documents"] = deepcopy([v for v in native_capture["documents"] if v["business_date"] <= BASE])
        initial["posted_manifest_json_by_id"] = {v["document_id"]: native_capture["posted_manifest_json_by_id"][v["document_id"]]
                                                 for v in initial["documents"]}
        initial["baseline_costs"]["document_manifest"] = {v["document_id"]: v["fingerprint"] for v in initial["documents"]}
        rehash_capture(initial)
    state = initialize_candidate(initial, open_initial_day=True)
    book = dict(schema=revision.BOOK_SCHEMA, active=True, effective_date=BASE,
                shared_days={}, wb_days={}, retained_days={}, presentations={})
    current = date.fromisoformat(BASE)
    while current.isoformat() <= end:
        day = current.isoformat()
        if state["periods"]:
            prior = max(state["periods"])
            state = close_candidate_period(state, prior, today=day)
            book["shared_days"][prior] = build_shared_cost_day(state, book["wb_days"][prior], prior)
            book["presentations"][prior] = FbsInventorySnapshot(fbs_state=state, wb_capture=book["wb_days"][prior],
                                                              retained=book["retained_days"][prior], day=prior).payload()
        cap = captured(day, [v for v in facts if v["business_date"] <= day], basis, fbo,
                       (quantities or {}).get(day))
        if native_capture is not None:
            cap["documents"] = deepcopy([v for v in native_capture["documents"] if v["business_date"] <= day])
            cap["posted_manifest_json_by_id"] = {v["document_id"]: native_capture["posted_manifest_json_by_id"][v["document_id"]]
                                                 for v in cap["documents"]}
            rehash_capture(cap)
        state = evaluate_candidate(state, cap)
        wb, retained = dated_sources(day, basis)
        book["wb_days"][day], book["retained_days"][day] = wb, retained
        book["shared_days"][day] = build_shared_cost_day(state, wb, day)
        book["presentations"][day] = FbsInventorySnapshot(fbs_state=state, wb_capture=wb, retained=retained, day=day).payload()
        current += timedelta(days=1)
    book.update(state=state, prepared_at=cap["captured_at"], source_digest=cap["source_digest"])
    return book, cap


def late(facts=(), *, end=END, identity="late", day=DAY, facility="A", pool="FBS", nm=1, q=1000, amount="200000",
         basis=BASIS, fbo=(), quantities=None):
    value = china(identity, day=day, facility=facility, pool=pool, nm=nm, q=q, amount=amount)
    value["posted_at"] = end + "T12:00:00Z"
    return captured(end, [*facts, value], basis, fbo, (quantities or {}).get(end))


def native_writer_fixture(path, *, end=END, receipt_source_type=None, receipt_source_id=None, service_receipt=False,
                          advancing_receipt_clock=False, confirmation_actor=None):
    """LOCAL TEST DB only: real builder/_apply_plan + capture_current/_documents.

    The official stock fixture supplies synthetic registry/stock evidence. Its
    two minimal pool tables are replaced with the actual native schema before
    posting; no runtime/service constructor or production path is used.
    """
    from apps.web_vitrina_official_fbs_smoke import fixture as stock_fixture
    from packages.application.ff_pool_documents import (
        ensure_ff_pool_document_schema, _build_posting_plan, _apply_plan, REQUESTS_TABLE,
    )
    from packages.application.ff_pool_foundation import FACILITIES_TABLE, FEATURE_EPOCHS_TABLE, BALANCES_TABLE
    from packages.application.fbs_snapshot_cost_sources import capture_current
    END = end  # Explicit LOCAL fixture date; original three-day default unchanged.
    conn = stock_fixture(path)
    conn.row_factory = sqlite3.Row
    conn.execute(f"DROP TABLE {BALANCES_TABLE}")
    conn.execute(f"DROP TABLE {FACILITIES_TABLE}")
    ensure_ff_pool_document_schema(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE sheet_vitrina_v1_nomenclature_items(item_id,nm_id,is_active,is_hidden,updated_at)")
    conn.executemany("INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,1,0,?)",
                     [("synthetic-1", 1, BASE), ("synthetic-2", 2, BASE)])
    for facility in ("A", "B"):
        conn.execute(f"INSERT INTO {FACILITIES_TABLE} VALUES(?,?,?,1,'Asia/Yekaterinburg',?,?)",
                     (facility, facility, "Synthetic " + facility, BASE + "T09:00:00Z", BASE + "T09:00:00Z"))
    conn.execute(f"INSERT INTO {FEATURE_EPOCHS_TABLE} VALUES(1,1,0,'native-fixture',?,'{{}}')", (BASE + "T09:00:00Z",))
    basis = [*BASIS, ("A", 2, 0, "100"), ("B", 2, 0, "200")]
    for facility, nm, quantity, cost in basis:
        conn.execute(f"INSERT INTO {BALANCES_TABLE} VALUES(?,'FBS',?,1,?,?,?,?,?)",
                     (facility, nm, quantity, str(Decimal(cost) * quantity), cost if quantity else None,
                      "synthetic-native-opening", BASE + "T09:00:00Z"))
    conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_warehouse_registry_runs SET started_at=?,completed_at=?",
                 (END + "T13:55:00Z", END + "T13:56:00Z"))
    conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_runs SET snapshot_at=?", (END + "T13:55:00Z",))
    conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SET amount=1000 WHERE nm_id=1")

    def post(name, kind, manifest, posted_at, business_date=DAY):
        request = dict(request_id="native:" + name, request_identity=fingerprint(name), client_request_id=name,
            document_kind=kind, state="accepted", source_system="synthetic-native", source_type="fixture:" + kind,
            source_id=name, source_revision="revision:" + name, idempotency_epoch=1, actor="fixture-operator",
            business_date=business_date, source_filename="", source_content_type="", source_sha256="",
            template_fingerprint="", request_payload_json=canonical(manifest), accepted_at=posted_at, updated_at=posted_at)
        if name == "new-late-receipt" and receipt_source_type:
            # Native intrinsic posting is the real replay entry point. This
            # fixture does not claim guided supplier/legacy side effects.
            request.update(source_type=receipt_source_type,source_id=receipt_source_id or name)
        columns = list(request)
        conn.execute(f"INSERT INTO {REQUESTS_TABLE}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                     tuple(request.values()))
        if name=='new-late-receipt' and service_receipt:
            # LOCAL actual service creates its own before-images, mutation
            # recovery owner and immutable accepted confirmation BEFORE post.
            # This is a generic receipt, not a guided supplier-service witness.
            conn.commit()
            from apps.fbs_accounting_historical_history_smoke import complete_local_metadata
            from packages.application.ff_pool_documents import FfPoolDocumentService
            from packages.application.operator_warehouse_documents import confirm_source
            from types import SimpleNamespace
            complete_local_metadata(path)
            stamp=datetime.fromisoformat(posted_at.replace('Z','+00:00'))
            ticks=iter(range(1,1000))
            clock=(lambda:(stamp+timedelta(seconds=next(ticks))).isoformat().replace('+00:00','Z')) if advancing_receipt_clock else (lambda:posted_at)
            service=FfPoolDocumentService(db_path=path,runtime_dir=path.parent,timestamp_factory=clock,resume=False,bootstrap=False)
            service.process_request(request['request_id'])
            confirm_source(SimpleNamespace(db_path=path,runtime_dir=path.parent,_now=clock),request['request_id'],actor=confirmation_actor)
            service.post(request['request_id'],defer_replay=True)
            status=service.status(request_id=request['request_id'])
            assert status['state']=='posted',status
            return dict(primary_document_id=status['document']['document_id'])
        plan = _build_posting_plan(conn, request=request, manifest=manifest, epoch=1,
                                   intrinsic_only=bool(name == "new-late-receipt" and receipt_source_type))
        _apply_plan(conn, request=request, plan=plan, epoch=1, posted_at=posted_at)
        conn.commit()
        return plan

    def capture():
        # This is the exact augmentation B needs: bytes and normalized capture
        # from ONE pinned RO transaction, not a follow-up write connection read.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            reader.execute("PRAGMA query_only=ON")
            reader.execute("BEGIN")
            cap = capture_current(path, now=datetime.fromisoformat(END + "T14:00:00+00:00"),
                                  include_baseline=False, connection=reader)
            cap["posted_manifest_json_by_id"] = {v[0]: v[1] for v in reader.execute(
                "SELECT document_id,posted_manifest_json FROM sheet_vitrina_v1_ff_pool_documents")}
        assert cap["documents_complete"], cap["documents_reason"]
        assert cap["quantity_snapshot"]["complete"], cap["quantity_snapshot"]
        return cap

    inventory = post("inventory-with-child", "pool_inventory",
        dict(facility_id="A", scope="FBS", targets=[dict(nm_id=1, target_fbs=1050)]), BASE + "T12:00:00Z", BASE)
    assert len(inventory["documents"]) == 2  # actual root + inventory_surplus
    old_capture = capture()
    acceptance = post("new-late-receipt", "china_acceptance",
        dict(facility_id="A", allocations=[dict(nm_id=1, quantity_fbo=0, quantity_fbs=1000,
             accepted_quantity=1000, accepted_capital_rub="200000", identity_evidence_digest="sha256:synthetic-catalog")]),
        END + "T12:00:00Z")
    cap = capture()
    conn.close()
    book, _ = saved_book(end=(date.fromisoformat(end) - timedelta(days=1)).isoformat(), basis=basis, native_capture=old_capture)
    wb, retained = dated_sources(END, basis)
    cap["current_dated_inputs"] = dict(wb_capture=wb, retained_capture=retained)
    return book, cap, acceptance["primary_document_id"], inventory


class HistoricalRevisionTests(unittest.TestCase):
    def ready(self, book, cap, ids=("late",)):
        plan = revision.build_historical_revision_plan(book, cap, receipt_document_ids=ids)
        self.assertEqual(plan["status"], "ready", plan.get("blocker"))
        self.assertEqual(revision.validate_historical_revision_plan(plan, book=book, capture=cap), plan)
        return plan

    def blocked(self, book, cap, code, ids=("late",)):
        before = deepcopy((book, cap))
        plan = revision.build_historical_revision_plan(book, cap, receipt_document_ids=ids)
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocker"]["code"], code, plan["blocker"])
        self.assertNotIn("candidate_book", plan)
        self.assertEqual((book, cap), before)
        return plan

    def test_saved_quantities_shared_cost_presentation_and_immutable_inputs(self):
        quantities = {DAY: {("A", 1): 1500}, END: {("A", 1): 900}}
        book, _ = saved_book(quantities=quantities)
        cap = late(quantities=quantities)
        before = deepcopy((book, cap))
        plan = self.ready(book, cap)
        candidate = plan["candidate_book"]
        rows = candidate["state"]["periods"]
        self.assertEqual(rows[DAY]["rows"]["A:1"]["wac_rub"], "150")
        self.assertEqual(rows[DAY]["rows"]["A:1"]["capital_rub"], "225000")
        self.assertEqual(rows[END]["rows"]["A:1"]["capital_rub"], "135000")
        # WB500 + A1500 + B1000: 100000 + 225000 + 200000.
        self.assertEqual(Decimal(candidate["shared_days"][DAY]["rows"]["1"]["unit_cost_rub"]), Decimal("175"))
        self.assertEqual(Decimal(candidate["presentations"][DAY]["totals"]["total"]["capital_rub"]), Decimal("527000"))
        self.assertEqual(candidate["state"]["baseline"], book["state"]["baseline"])
        self.assertEqual(candidate["state"]["periods"][BASE], book["state"]["periods"][BASE])
        self.assertEqual(candidate["wb_days"], book["wb_days"])
        self.assertEqual(candidate["retained_days"], book["retained_days"])
        self.assertEqual((book, cap), before)
        shared = SharedSkuCostSnapshot(list(candidate["shared_days"].values()), effective_date=BASE)
        self.assertEqual(shared.resolve(nm_id="1", operation_date=date.fromisoformat(DAY))["unit_cost_rub"], "175")
        self.assertIn("TOTAL", plan["targets"][DAY]["presentation_metrics"])
        self.assertIn("exact_retained_stage_applicability_or_revision_proof", plan["writer_requirements"])
        self.assertEqual(plan["derived_stage_applicability"]["status"], "not_proven_by_cost_planner")
        self.assertEqual(plan["derived_stage_applicability"]["retained_digests_by_date"][DAY],
                         fingerprint(book["retained_days"][DAY]))

    def test_same_day_overhead_expands_sku_scope_and_both_pool_allocations(self):
        basis = [("A", 1, 1000, "100"), ("A", 2, 3000, "50"), ("B", 1, 1000, "200"), ("B", 2, 0, "100")]
        fbo = [("A", 1, 1000, "300")]
        facts = [overhead(scope="both")]
        book, _ = saved_book(facts=facts, basis=basis, fbo=fbo)
        plan = self.ready(book, late(facts, basis=basis, fbo=fbo))
        period = plan["candidate_book"]["state"]["periods"][DAY]
        allocation = period["document_valuation"]["allocations"][0]
        self.assertEqual(allocation["amounts_kopecks"], {"A:FBS:1": 33333, "A:FBS:2": 50000, "A:FBO:1": 16667})
        self.assertEqual(sum(allocation["amounts_kopecks"].values()), 100000)
        self.assertEqual(period["rows"]["A:1"]["wac_rub"], "150.166665")
        self.assertEqual(Decimal(period["document_cost_state"]["fbo_rows"]["A:FBO:1"]["wac_rub"]), Decimal("300.16667"))
        self.assertNotEqual(period["rows"]["A:2"]["wac_rub"], book["state"]["periods"][DAY]["rows"]["A:2"]["wac_rub"])
        self.assertEqual(plan["scope"]["affected_nm_ids"], [1, 2])
        self.assertIn("fee", plan["targets"][DAY]["allocations"])
        self.assertIn("A:FBO:1", plan["targets"][DAY]["fbo_locations"])

    def test_prefix_expense_applied_once_and_prior_versions_remain_unchanged(self):
        # Synthetic amount/date; no real supplier or production source bytes.
        fee = overhead('prefix-fee', amount='61425', day=DAY)
        book, _ = saved_book(facts=[fee])
        before = deepcopy(book)
        plan = self.ready(book, late([fee], day='2026-09-09'))
        candidate = plan['candidate_book']
        self.assertEqual(book, before)
        self.assertEqual(revision._prefix(candidate, '2026-09-09'), revision._prefix(book, '2026-09-09'))
        allocations = [(day, allocation) for day, period in candidate['state']['periods'].items()
                       for allocation in period['document_valuation']['allocations'] if allocation['document_id']=='prefix-fee']
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0][0], DAY)
        self.assertEqual(sum(allocations[0][1]['amounts_kopecks'].values()), 6142500)
        self.assertEqual(candidate['state']['periods'][DAY]['applied_documents']['prefix-fee'],
                         book['state']['periods'][DAY]['applied_documents']['prefix-fee'])

    def test_referenced_shipment_partial_receipt_loss_fbo_cost_conservation(self):
        facts = [root(dst_pool="FBO"), shipment(amount="5"), receipt(q=200, day="2026-09-09", pool="FBO"),
                 doc("loss", "transfer_loss", root="t", day="2026-09-09", hour=14,
                     lines=[line(q=300, role="lost")])]
        book, _ = saved_book(facts=facts)
        plan = self.ready(book, late(facts))
        saved = plan["candidate_book"]["state"]["periods"]["2026-09-09"]["document_cost_state"]
        transfer = saved["transfers"]["t:1"]
        self.assertEqual(Decimal(transfer["base_capital_rub"]), Decimal("75000"))
        self.assertEqual(Decimal(transfer["cost_reference"]["wac_rub"]), Decimal("150"))
        self.assertEqual(transfer["terminal_quantity"], 500)
        self.assertEqual(Decimal(saved["fbo_rows"]["B:FBO:1"]["capital_rub"]), Decimal("30002"))
        self.assertIn("t:1", plan["targets"][DAY]["transfer_references"])
        self.assertIn("B:FBO:1", plan["targets"]["2026-09-09"]["fbo_locations"])
        self.assertEqual(Decimal(book["state"]["periods"]["2026-09-09"]["document_cost_state"]["transfers"]["t:1"]["base_capital_rub"]), Decimal("50000"))

    def test_new_backdated_transfer_receipt_uses_existing_saved_reference(self):
        facts = [root(), shipment()]
        book, _ = saved_book(facts=facts)
        new = receipt("late", day="2026-09-09", q=200)
        new["posted_at"] = END + "T12:00:00Z"
        cap = captured(END, [*facts, new])
        plan = self.ready(book, cap)
        r = plan["candidate_book"]["state"]["periods"]["2026-09-09"]["rows"]["B:1"]
        self.assertEqual(Decimal(r["receipt_capital_rub"]), Decimal("20000"))
        self.assertEqual(plan["scope"]["date_from"], "2026-09-09")

    def test_fbo_only_new_receipt_updates_shared_cost_not_official_fbs_quantity(self):
        fbo = [("A", 1, 1000, "300")]
        book, _ = saved_book(fbo=fbo)
        plan = self.ready(book, late(pool="FBO", fbo=fbo))
        state = plan["candidate_book"]["state"]["periods"][DAY]
        self.assertEqual(state["rows"]["A:1"]["quantity"], "1000")
        self.assertEqual(state["rows"]["A:1"]["wac_rub"], "100")
        row = state["document_cost_state"]["fbo_rows"]["A:FBO:1"]
        self.assertEqual((row["quantity"], row["wac_rub"]), ("2000", "250"))
        self.assertIn("A:FBO:1", plan["targets"][DAY]["fbo_locations"])

    def test_history_older_than14_days_and_multiple_new_receipts(self):
        end = "2026-10-03"
        book, _ = saved_book(end=end)
        one = china("late", day=DAY)
        two = china("late-2", day="2026-09-10", q=100, amount="30000")
        cap = captured(end, [one, two])
        plan = self.ready(book, cap, ("late-2", "late"))
        self.assertEqual(plan["scope"]["dates"][0], DAY)
        self.assertEqual(len(plan["scope"]["dates"]), 26)
        self.assertEqual(plan["candidate_book"]["state"]["periods"][end]["rows"]["A:1"]["wac_rub"],
                         plan["candidate_book"]["state"]["periods"]["2026-09-10"]["rows"]["A:1"]["wac_rub"])
        self.assertEqual(revision.build_historical_revision_plan(book, cap, receipt_document_ids=("late", "late-2")), plan)

    def test_observation_metadata_can_advance_without_false_stale(self):
        book, _ = saved_book()
        cap = late()
        plan = self.ready(book, cap)
        later = deepcopy(cap)
        later["captured_at"] = END + "T21:00:00Z"
        self.assertEqual(revision.validate_historical_revision_plan(plan, book=book, capture=later), plan)
        earlier = deepcopy(cap)
        earlier["captured_at"] = END + "T19:30:00Z"
        with self.assertRaisesRegex(revision.HistoricalRevisionError, "material_changed"):
            revision.validate_historical_revision_plan(plan, book=book, capture=earlier)

    def test_next_day_uses_exact_current_capture_and_closes_saved_open_tail(self):
        quantities = {DAY: {("A", 1): 1500}, END: {("A", 1): 900}}
        book, _ = saved_book(end="2026-09-09", quantities=quantities)
        cap = late(quantities=quantities)
        wb, retained = dated_sources(END, BASIS)
        cap["current_dated_inputs"] = dict(wb_capture=wb, retained_capture=retained)
        before = deepcopy((book, cap))
        plan = self.ready(book, cap)
        candidate = plan["candidate_book"]
        periods = candidate["state"]["periods"]
        self.assertEqual(periods["2026-09-09"]["status"], "closed")
        self.assertEqual(periods[END]["status"], "open")
        self.assertEqual(periods[DAY]["rows"]["A:1"]["capital_rub"], "225000")
        self.assertEqual(periods[END]["rows"]["A:1"]["capital_rub"], "135000")
        self.assertEqual(periods["2026-09-09"]["snapshot"], book["state"]["periods"]["2026-09-09"]["snapshot"])
        self.assertEqual(candidate["wb_days"][END], wb)
        self.assertEqual(candidate["retained_days"][END], retained)
        self.assertEqual(plan["dated_inputs"][END]["source_role"], "exact_new_current_capture")
        self.assertIsNone(plan["targets"][END]["before_shared_version"])
        self.assertEqual(plan["immutable_prefix_digest"], fingerprint(revision._prefix(book, DAY)))
        self.assertEqual(revision._prefix(candidate, DAY), revision._prefix(book, DAY))
        self.assertEqual((book, cap), before)

    def test_next_day_missing_current_sources_and_calendar_gap_fail_closed(self):
        book, _ = saved_book(end="2026-09-09")
        self.blocked(book, late(), "current_dated_inputs_missing")
        wb, retained = dated_sources(END, BASIS)
        cap = late()
        cap["current_dated_inputs"] = dict(wb_capture=wb, retained_capture=retained)
        wrong = deepcopy(cap)
        wrong["current_dated_inputs"]["wb_capture"]["business_date"] = "2026-09-09"
        self.blocked(book, wrong, "historical_wb_source_missing")
        wrong = deepcopy(cap)
        wrong["current_dated_inputs"]["retained_capture"]["date"] = "2026-09-09"
        self.blocked(book, wrong, "historical_retained_source_missing")
        gap = late(end="2026-09-11")
        wb, retained = dated_sources("2026-09-11", BASIS)
        gap["current_dated_inputs"] = dict(wb_capture=wb, retained_capture=retained)
        self.blocked(book, gap, "historical_period_gap")

    def test_unrequested_pending_late_receipt_and_prefix_mutation_refuse_plan(self):
        book, _ = saved_book()
        one = china("late", day=DAY)
        two = china("unrequested", day="2026-09-09")
        cap = captured(END, [one, two])
        observed = deepcopy(book)
        observed["state"] = evaluate_candidate(book["state"], cap)
        self.blocked(observed, cap, "unapproved_new_document_scope")
        # A diagnostic without a captured source may not be erased either.
        observed = deepcopy(book)
        observed["state"]["pending_documents"] = [dict(document_id="unrequested", reason="late_closed_period_document")]
        self.blocked(observed, late(), "unrelated_pending_document")
        plan = self.ready(book, late())
        changed_prefix = deepcopy(book)
        changed_prefix["state"]["periods"][BASE]["rows"]["A:1"]["capital_rub"] = "1"
        with self.assertRaisesRegex(revision.HistoricalRevisionError, "candidate_changed"):
            revision.validate_historical_revision_plan(plan, book=changed_prefix, capture=late())

    def test_duplicate_source_unapproved_document_absorbed_and_prebaseline_blocks(self):
        book, _ = saved_book()
        self.blocked(book, late(), "receipt_identity_scope_invalid", ("late", "late"))
        self.blocked(book, late(day="2026-09-06"), "receipt_before_accepted_opening")
        self.blocked(book, late(day=END), "receipt_closed_date_required")
        cap = captured(END, [china("late"), overhead("unexpected", day=END)])
        self.blocked(book, cap, "unapproved_new_document_scope")
        fact = china("late")
        saved, _ = saved_book(facts=[fact])
        self.blocked(saved, captured(END, [fact]), "receipt_already_absorbed")
        bad = late()
        duplicate = deepcopy(bad["documents"][0])
        bad["documents"].append(duplicate)
        rehash_capture(bad)
        self.blocked(book, bad, "invalid_historical_input")

    def test_changed_absorbed_and_missing_observed_facts_block(self):
        facts = [overhead()]
        book, _ = saved_book(facts=facts)
        altered = [overhead(amount="2000")]
        self.blocked(book, late(altered), "absorbed_document_missing_or_changed")
        self.blocked(book, late(), "absorbed_document_missing_or_changed")
        book, _ = saved_book()
        book["state"]["observed_documents"]["gone"] = dict(fingerprint="sha256:gone")
        self.blocked(book, late(), "observed_document_missing_or_changed")

    def test_missing_saved_period_snapshot_wb_retained_and_prefix_gap_blocks(self):
        book, _ = saved_book()
        missing = deepcopy(book)
        del missing["state"]["periods"]["2026-09-09"]
        self.blocked(missing, late(), "historical_period_gap")
        missing = deepcopy(book)
        missing["state"]["periods"][DAY]["snapshot"]["complete"] = False
        self.blocked(missing, late(), "historical_quantity_source_missing")
        for field in ("wb_days", "retained_days", "presentations", "shared_days"):
            missing = deepcopy(book)
            del missing[field][DAY]
            self.blocked(missing, late(), "historical_dated_input_missing")
        missing = deepcopy(book)
        missing["wb_days"][DAY]["complete"] = False
        self.blocked(missing, late(), "historical_wb_source_missing")
        missing = deepcopy(book)
        missing["retained_days"][DAY]["date"] = END
        self.blocked(missing, late(), "historical_retained_source_missing")
        missing = deepcopy(book)
        del missing["state"]["periods"][BASE]
        self.blocked(missing, late(), "historical_prefix_gap")

    def test_exact_raw_manifest_domain_events_and_capture_digest_proofs(self):
        book, _ = saved_book()
        bad = late()
        bad["posted_manifest_json_by_id"]["late"] += " "
        self.blocked(book, bad, "invalid_historical_input")
        bad = late()
        bad["documents"][0]["cost_document"]["domain"]["injected"] = 1
        rehash_capture(bad)
        self.blocked(book, bad, "document_domain_proof_mismatch")
        bad = late()
        bad["documents"][0]["events"][0]["capital_rub"] = "1"
        rehash_capture(bad)
        self.blocked(book, bad, "document_events_proof_mismatch")
        bad = late()
        bad["documents"][0]["cost_document"]["lines"][0]["capital_rub"] = "1"
        rehash_capture(bad)
        self.blocked(book, bad, "document_posted_line_operand_mismatch")
        bad = late()
        bad["documents"][0]["fingerprint"] = "sha256:wrong"
        rehash_capture(bad)
        self.blocked(book, bad, "document_fingerprint_mismatch")
        bad = late()
        bad["source_digest"] = "sha256:forged"
        self.blocked(book, bad, "capture_source_digest_mismatch")
        bad = late()
        del bad["posted_manifest_json_by_id"]["late"]
        self.blocked(book, bad, "document_exact_bytes_scope_mismatch")

    def test_missing_saved_stage_money_is_not_zero_and_duplicate_operation_blocks(self):
        book, _ = saved_book()
        incomplete = deepcopy(book)
        retained = incomplete["retained_days"][DAY]
        retained["rows"]["1"]["stages"]["PRODUCTION"]["capital_rub"] = None
        retained["source_digest"] = fingerprint({k: v for k, v in retained.items() if k != "source_digest"})
        self.blocked(incomplete, late(), "historical_presentation_incomplete")
        cap = captured(END, [china("late"), china("late-2")])
        cap["documents"][1]["operation_id"] = cap["documents"][0]["operation_id"]
        rehash_capture(cap)
        self.blocked(book, cap, "document_operation_identity_duplicate", ("late", "late-2"))

    def test_exact_posted_lines_reject_self_consistent_normalized_money_and_metadata(self):
        book, _ = saved_book()
        cap = late()
        good = self.ready(book, cap)
        self.assertEqual(good["candidate_book"]["state"]["periods"][DAY]["rows"]["A:1"]["wac_rub"], "150")
        for field, value in (("capital_rub", "210000"), ("metadata_json", '{"forged":true}'),
                             ("expense_rub", "100"), ("quantity", 2000), ("facility_id", "B"),
                             ("pool", "FBO"), ("nm_id", 2), ("line_no", 2), ("document_id", "wrong")):
            bad = deepcopy(cap)
            source_bytes = bad["posted_manifest_json_by_id"]["late"]
            normalized = bad["documents"][0]
            normalized["cost_document"]["lines"][0][field] = value
            if field == "capital_rub":
                normalized["cost_document"]["movements"][0]["capital_delta_rub"] = value
            elif field == "expense_rub":
                normalized["cost_document"]["movements"][0]["capital_delta_rub"] = "200100"
            elif field in {"quantity", "facility_id", "pool", "nm_id"}:
                normalized["cost_document"]["movements"][0]["quantity_delta" if field == "quantity" else field] = value
            rehash_normalized(normalized)
            rehash_capture(bad)
            self.assertEqual(bad["posted_manifest_json_by_id"]["late"], source_bytes)
            self.blocked(book, bad, "document_posted_line_operand_mismatch")
            with self.assertRaisesRegex(revision.HistoricalRevisionError, "revision_material_changed"):
                revision.validate_historical_revision_plan(good, book=book, capture=bad)
        missing = deepcopy(cap)
        posted = json.loads(missing["posted_manifest_json_by_id"]["late"])
        del posted["lines"]
        rebind_posted(missing, "late", posted)
        self.blocked(book, missing, "document_posted_line_proof_missing_or_unsupported")

    def test_ordered_native_lines_and_exact_opening_legacy_contract(self):
        book, _ = saved_book()
        two_lines = china("late")
        two_lines["cost_document"]["lines"].append(line(q=500, role="accepted_pool_allocation", capital="125000"))
        cap = captured(END, [two_lines])
        self.ready(book, cap)
        bad = deepcopy(cap)
        normalized = bad["documents"][0]
        normalized["cost_document"]["lines"].reverse()
        for n, v in enumerate(normalized["cost_document"]["lines"], 1):
            v["line_no"] = n
        rehash_normalized(normalized)
        rehash_capture(bad)
        self.blocked(book, bad, "document_posted_line_operand_mismatch")
        opening = doc("exact-opening", "facility_pool_opening", day=BASE)
        cap = captured(END, [opening])
        normalized = cap["documents"][0]
        normalized.update(source_system="ff_pool_cutover", source_type="cutover_manifest", source_id="cutover")
        posted = dict(contract_name="ff_pool_exact_opening_v1", feature_epoch=1, domain={}, allocations=[
            dict(line_no=1, facility_id="A", pool="FBS", nm_id=1, quantity=1000,
                 capital_rub="100000", wac_rub="100", allocation_digest="sha256:allocation")])
        normalized["cost_document"]["movements"] = [dict(operation_id=normalized["operation_id"], line_no=1,
            facility_id="A", pool="FBS", nm_id=1, quantity_delta=1000, capital_delta_rub="100000",
            metadata_json='{"allocation_digest":"sha256:allocation"}')]
        rebind_posted(cap, "exact-opening", posted)
        self.assertIn("exact-opening", revision._verify_documents(cap))
        bad = deepcopy(cap)
        normalized = bad["documents"][0]
        normalized["cost_document"]["movements"][0]["capital_delta_rub"] = "105000"
        rehash_normalized(normalized)
        rehash_capture(bad)
        self.blocked(book, bad, "opening_posted_allocation_mismatch")

    def test_shared_native_request_root_children_allowed_but_conflicting_owner_blocks(self):
        cap = captured(END, [root(), shipment()])
        posted_by_id = {i: json.loads(value) for i, value in cap["posted_manifest_json_by_id"].items()}
        cohort = [v["documents"][0] for v in posted_by_id.values()]
        source = dict(system="synthetic", type="ff_transfer_form", id="one-request", revision="one-revision", idempotency_epoch=1)
        for normalized in cap["documents"]:
            identity = normalized["document_id"]
            normalized.update(source_type=source["type"], source_id=source["id"], source_revision=source["revision"])
            posted = posted_by_id[identity]
            posted.update(source=source, request_id="request:shared", primary_document_id="t", documents=cohort,
                          domain=posted_by_id["t"]["domain"])
            normalized["cost_document"]["domain"] = deepcopy(posted["domain"])
            rebind_posted(cap, identity, posted)
        book, _ = saved_book(native_capture=cap)
        combined = late()
        combined["documents"] += deepcopy(cap["documents"])
        combined["documents"].sort(key=lambda v: v["document_id"])
        combined["posted_manifest_json_by_id"].update(cap["posted_manifest_json_by_id"])
        rehash_capture(combined)
        self.ready(book, combined)
        wrong_owner = deepcopy(combined)
        posted = json.loads(wrong_owner["posted_manifest_json_by_id"]["s"])
        posted["request_id"] = "request:unrelated"
        rebind_posted(wrong_owner, "s", posted)
        self.blocked(book, wrong_owner, "document_request_source_ownership_conflict")

    def test_real_native_writer_request_children_capture_and_immutable_line_attack(self):
        with TemporaryDirectory(prefix="historical-native-writer-fixture-") as directory:
            book, cap, selected, inventory = native_writer_fixture(Path(directory) / "local-synthetic.sqlite3")
        plan = self.ready(book, cap, (selected,))
        self.assertEqual(Decimal(plan["candidate_book"]["state"]["periods"][DAY]["rows"]["A:1"]["wac_rub"]), Decimal("150"))
        ids = {v["document_id"] for v in inventory["documents"]}
        docs = [v for v in cap["documents"] if v["document_id"] in ids]
        self.assertEqual(len(docs), 2)
        self.assertEqual(len({tuple(v[key] for key in ("source_system", "source_type", "source_id", "source_revision", "idempotency_epoch")) for v in docs}), 1)
        self.assertEqual(len({v["operation_id"] for v in docs}), 2)
        self.assertEqual(plan["scope"]["date_from"], DAY)
        bad = deepcopy(cap)
        original_bytes = bad["posted_manifest_json_by_id"][selected]
        normalized = next(v for v in bad["documents"] if v["document_id"] == selected)
        self.assertEqual(json.loads(original_bytes)["lines"][0]["capital_rub"], "200000.00")
        normalized["cost_document"]["lines"][0]["capital_rub"] = "210000.00"
        normalized["cost_document"]["movements"][0]["capital_delta_rub"] = "210000.00"
        rehash_normalized(normalized)
        rehash_capture(bad)
        self.assertEqual(bad["posted_manifest_json_by_id"][selected], original_bytes)
        self.blocked(book, bad, "document_posted_line_operand_mismatch", (selected,))
        with self.assertRaisesRegex(revision.HistoricalRevisionError, "revision_material_changed"):
            revision.validate_historical_revision_plan(plan, book=book, capture=bad)

    def test_validator_replays_numbers_and_rejects_forged_candidate_and_aba(self):
        book, _ = saved_book()
        cap = late()
        plan = self.ready(book, cap)
        forged = deepcopy(plan)
        forged["candidate_book"]["state"]["periods"][DAY]["rows"]["A:1"]["wac_rub"] = "1"
        forged["after_book_digest"] = fingerprint(forged["candidate_book"])
        forged["plan_fingerprint"] = fingerprint({k: v for k, v in forged.items() if k != "plan_fingerprint"})
        with self.assertRaisesRegex(revision.HistoricalRevisionError, "candidate_changed"):
            revision.validate_historical_revision_plan(forged, book=book, capture=cap)
        newer = deepcopy(book)
        # Same business values, different admitted revision lineage must stale.
        newer["revision_lineage"] = {"predecessor": fingerprint(book), "intermediate": "ABA"}
        with self.assertRaisesRegex(revision.HistoricalRevisionError, "candidate_changed"):
            revision.validate_historical_revision_plan(plan, book=newer, capture=cap)
        changed_capture = late(amount="210000")
        with self.assertRaisesRegex(revision.HistoricalRevisionError, "candidate_changed"):
            revision.validate_historical_revision_plan(plan, book=book, capture=changed_capture)

    def test_quantity_frontier_resource_limits_and_unrelated_pending(self):
        book, _ = saved_book()
        cap = late(quantities={END: {("A", 1): 1100}})
        self.blocked(book, cap, "current_quantity_frontier_changed")
        with patch.object(revision, "MAX_DAYS", 2):
            self.blocked(book, late(), "historical_date_scope_limit")
        with patch.object(revision, "MAX_BOOK_BYTES", 1):
            self.blocked(book, late(), "book_size_limit")
        with patch.object(revision, "MAX_PLAN_BYTES", 1):
            self.blocked(book, late(), "plan_size_limit")
        with patch.object(revision, "MAX_DOCUMENTS", 2):
            self.blocked(book, late(), "receipt_identity_scope_limit", iter(("late", "extra", "excess")))
        bad = deepcopy(book)
        bad["state"]["pending_documents"] = [{"reason": "absorbed_document_missing_or_changed", "document_id": "late"}]
        self.blocked(bad, late(), "unrelated_pending_document")

    def test_exact_observed_late_pending_can_be_resolved_but_ordinary_guard_remains(self):
        book, _ = saved_book()
        cap = late()
        ordinary = evaluate_candidate(book["state"], cap)
        self.assertEqual(ordinary["periods"][DAY], book["state"]["periods"][DAY])
        self.assertEqual(ordinary["pending_documents"][0]["reason"], "late_closed_period_document")
        pending = deepcopy(book)
        pending["state"] = ordinary
        # Original saved tail quantities still match; planner clears ONLY the
        # selected exact late diagnostic by reconstructing its own suffix.
        self.ready(pending, cap)
        planned = self.ready(book, cap)
        from packages.application.fbs_accounting_runtime import _save_book, load
        with TemporaryDirectory(prefix="pure-revision-ordinary-guard-") as directory:
            version = _save_book(directory, book, expected=None, operation_id="synthetic-original")
            with self.assertRaisesRegex(ValueError, "closed_shared_day_is_immutable"):
                _save_book(directory, planned["candidate_book"], expected=version, operation_id="forbidden-revision")
            restored, current = load(directory, version=version)
            self.assertEqual((restored, current), (book, version))


if __name__ == "__main__":
    unittest.main()
