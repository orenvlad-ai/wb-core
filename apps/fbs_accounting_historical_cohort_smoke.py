#!/usr/bin/env python3
"""Actual native mixed-cohort fixtures; saved dated book inputs are synthetic."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.fbs_accounting_historical_revision_smoke import native_writer_fixture, rehash_normalized, rehash_capture, END
from packages.application import fbs_accounting_historical_revision as revision
from packages.application import fbs_accounting_historical_sources as sources
from packages.application import ready_publication as ready
from packages.application.ff_pool_documents import REQUESTS_TABLE, DOCUMENTS_TABLE, _build_posting_plan, _apply_plan
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.fbs_snapshot_cost_sources import capture_current


def posted_requests(conn):
    """Native wrapper workflow headers, no constructed source monetary values."""
    for request in conn.execute(f"SELECT * FROM {REQUESTS_TABLE}").fetchall():
        row = conn.execute(f"SELECT posted_manifest_json,posted_at FROM {DOCUMENTS_TABLE} WHERE request_id=? ORDER BY document_id LIMIT 1", (request["request_id"],)).fetchone()
        posted = json.loads(row[0])
        original = {k: v for k, v in posted.items() if k not in {"document_id", "document_role", "lines"}}
        original["document_kind"] = request["document_kind"]
        conn.execute(f"UPDATE {REQUESTS_TABLE} SET state='posted',posted_document_id=?,posted_manifest_sha256=?,posted_at=? WHERE request_id=?",
                     (posted["primary_document_id"], fingerprint(original), row[1], request["request_id"]))


def post(conn, *, name, kind, manifest, day=END, at=None):
    at = at or day + "T14:01:00Z"
    old = dict(conn.execute(f"SELECT * FROM {REQUESTS_TABLE} ORDER BY request_id LIMIT 1").fetchone())
    fields = ("request_id", "request_identity", "client_request_id", "document_kind", "state", "source_system", "source_type", "source_id",
              "source_revision", "idempotency_epoch", "actor", "business_date", "source_filename", "source_content_type", "source_sha256",
              "template_fingerprint", "request_payload_json", "accepted_at", "updated_at")
    request = {k: old[k] for k in fields}
    request.update(request_id="native:" + name, request_identity=fingerprint(name), client_request_id=name, document_kind=kind,
                   state="accepted", source_type="fixture:" + kind, source_id=name, source_revision="revision:" + name,
                   business_date=day, request_payload_json=canonical(manifest), accepted_at=at, updated_at=at)
    conn.execute(f"INSERT INTO {REQUESTS_TABLE}({','.join(request)}) VALUES({','.join('?' for _ in request)})", tuple(request.values()))
    plan = _build_posting_plan(conn, request=request, manifest=manifest, epoch=1)
    _apply_plan(conn, request=request, plan=plan, epoch=1, posted_at=at)
    posted_requests(conn)
    return plan["primary_document_id"]


def mixed_fixture(db, *, end=END, transfers=False):
    book, cap, receipt, _ = native_writer_fixture(db, end=end)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.row_factory = sqlite3.Row
        posted_requests(conn)
        overhead = post(conn, name="current-overhead", kind="pool_overhead", day=end,
                        manifest=dict(facility_id="A", scope="FBS", amount_rub="100", category="other",
                                      comment="Local synthetic cohort", source_mode="manual"))
        additional = [overhead]
        if transfers:
            root = post(conn, name="current-root", kind="transfer_root", day=end,
                        manifest=dict(source=dict(facility_id="A", pool="FBS"), destination=dict(facility_id="B", pool="FBO")))
            shipment = post(conn, name="current-shipment", kind="transfer_shipment", day=end,
                            manifest=dict(root_document_id=root, items=[dict(nm_id=1,quantity=100)]))
            received = post(conn, name="current-receipt", kind="transfer_receipt", day=end,
                            manifest=dict(root_document_id=root,items=[dict(nm_id=1,quantity=50)]))
            additional += [root, shipment, received]
    now = datetime.fromisoformat(end + "T14:02:00+00:00")
    with ready.readonly(db) as conn:
        fresh = capture_current(db, now=now, include_baseline=False, connection=conn)
        fresh["posted_manifest_json_by_id"] = {r[0]:r[1] for r in conn.execute(f"SELECT document_id,posted_manifest_json FROM {DOCUMENTS_TABLE}")}
        fresh["current_dated_inputs"] = cap["current_dated_inputs"]
        fresh = sources.augment_native_requests(conn, fresh, document_ids=[receipt,*additional])
    return book, fresh, receipt, additional


class HistoricalCohortTests(unittest.TestCase):
    def fixture(self, **kwargs):
        temp = TemporaryDirectory(prefix="historical-native-cohort-")
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "fixture.sqlite3"
        return mixed_fixture(self.db, **kwargs)

    def plan(self, book, cap, receipt, current):
        result = revision.build_historical_revision_plan(book,cap,receipt_document_ids=[receipt],current_document_ids=current)
        self.assertEqual(result["status"], "ready", result.get("blocker"))
        self.assertEqual(revision.validate_historical_revision_plan(result,book=book,capture=cap),result)
        return result

    def test_late_receipt_and_current_native_overhead_original_dates_no_deadlock(self):
        book,cap,receipt,current=self.fixture()
        before=deepcopy(book)
        plan=self.plan(book,cap,receipt,current)
        periods=plan["candidate_book"]["state"]["periods"]
        self.assertEqual(periods["2026-09-08"]["rows"]["A:1"]["wac_rub"],"150.00")
        self.assertEqual(periods[END]["rows"]["A:1"]["wac_rub"],"150.10")
        self.assertNotIn(current[0],periods["2026-09-09"]["applied_documents"])
        self.assertIn(current[0],periods[END]["applied_documents"])
        self.assertEqual(book,before)
        self.assertEqual(plan["candidate_book"]["state"]["pending_documents"],[])

    def test_native_current_transfer_references_fbs_to_fbo_and_same_day_overhead(self):
        book,cap,receipt,current=self.fixture(transfers=True)
        plan=self.plan(book,cap,receipt,current)
        last=plan["candidate_book"]["state"]["periods"][END]
        self.assertEqual(last["document_cost_state"]["fbo_rows"]["B:FBO:1"]["quantity"],"50")
        self.assertEqual(last["document_cost_state"]["fbo_rows"]["B:FBO:1"]["wac_rub"],"150.10")
        self.assertEqual(set(current).issubset(last["applied_documents"]),True)

    def test_more_than_fourteen_dates_no_truncation_current_expense_not_backdated(self):
        end="2026-10-08"
        book,cap,receipt,current=self.fixture(end=end)
        plan=self.plan(book,cap,receipt,current)
        self.assertEqual(len(plan["scope"]["dates"]),31)
        self.assertEqual(plan["scope"]["dates"][0],"2026-09-08")
        self.assertEqual(plan["candidate_book"]["state"]["periods"]["2026-09-09"]["rows"]["A:1"]["wac_rub"],"150.00")
        self.assertEqual(plan["candidate_book"]["state"]["periods"][end]["rows"]["A:1"]["wac_rub"],"150.10")

    def test_expense_basis_file_metadata_tamper_equal_amount_recomputed_hashes_block(self):
        book,cap,receipt,current=self.fixture()
        for field,value in (("basis","Foreign"),("source_file_sha256","sha256:"+"a"*64),("metadata_json",'{"other":true}')):
            with self.subTest(field=field):
                forged=deepcopy(cap)
                doc=next(d for d in forged["documents"] if d["document_id"]==current[0])
                doc["cost_document"]["expense_lines"][0][field]=value
                rehash_normalized(doc);rehash_capture(forged)
                result=revision.build_historical_revision_plan(book,forged,receipt_document_ids=[receipt],current_document_ids=current)
                self.assertEqual(result["status"],"blocked")
                self.assertEqual(result["blocker"]["code"],"native_cohort_expense_operand_mismatch")

    def test_request_progress_is_observation_but_original_authority_is_material(self):
        book,cap,receipt,current=self.fixture()
        plan=self.plan(book,cap,receipt,current)
        observed=deepcopy(cap)
        observed["captured_at"]=(datetime.fromisoformat(cap["captured_at"].replace("Z","+00:00"))+timedelta(minutes=1)).isoformat().replace("+00:00","Z")
        for request in observed["native_requests_by_id"].values():request["state"]="replay"
        self.assertEqual(revision.validate_historical_revision_plan(plan,book=book,capture=observed),plan)
        observed["native_requests_by_id"]["native:current-overhead"]["actor"]="foreign"
        with self.assertRaises(ValueError):revision.validate_historical_revision_plan(plan,book=book,capture=observed)

    def test_unselected_late_source_never_implicitly_absorbed(self):
        book,cap,receipt,current=self.fixture()
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.row_factory=sqlite3.Row
            post(conn,name="unselected-old-overhead",kind="pool_overhead",day="2026-09-09",at=END+"T14:01:00Z",
                 manifest=dict(facility_id="A",scope="FBS",amount_rub="100",category="other",comment="Unselected",source_mode="manual"))
        with ready.readonly(self.db) as conn:
            fresh=capture_current(self.db,now=datetime.fromisoformat(END+"T14:02:00+00:00"),include_baseline=False,connection=conn)
            fresh["posted_manifest_json_by_id"]={r[0]:r[1] for r in conn.execute(f"SELECT document_id,posted_manifest_json FROM {DOCUMENTS_TABLE}")}
            fresh["current_dated_inputs"]=cap["current_dated_inputs"]
            fresh=sources.augment_native_requests(conn,fresh,document_ids=[receipt,*current])
        result=revision.build_historical_revision_plan(book,fresh,receipt_document_ids=[receipt],current_document_ids=current)
        self.assertEqual(result["blocker"]["code"],"unapproved_new_document_scope")


if __name__=="__main__":unittest.main()
