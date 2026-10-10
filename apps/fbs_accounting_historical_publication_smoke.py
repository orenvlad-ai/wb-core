#!/usr/bin/env python3
"""Local real special book/ready publication, exact retry and crash recovery."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.fbs_accounting_historical_stages_smoke import native_stages_fixture
from packages.application import fbs_accounting_historical_publication as publication
from packages.application import fbs_accounting_historical_revision_writer as staging
from packages.application import fbs_accounting_runtime as accounting
from packages.application import ready_publication as ready
from packages.application import operator_warehouse_documents as operations
from packages.application.ff_pool_documents import REQUESTS_TABLE,_build_posting_plan
from packages.application.fbs_snapshot_cost import fingerprint,canonical
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.registry_upload_db_backed_runtime import _serialize_sheet_vitrina_plan
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope,SheetVitrinaWriteTarget,SheetVitrinaV1TemporalSlot

NOW=datetime.fromisoformat("2026-09-10T14:00:00+00:00")


class HistoricalPublication(unittest.TestCase):
    def setUp(self):
        self.now=getattr(self,'fixture_now',NOW)
        temp=TemporaryDirectory(prefix="historical-real-publication-");self.addCleanup(temp.cleanup)
        self.runtime=Path(temp.name);self.db=self.runtime/"registry_upload_runtime.sqlite3"
        self.book,self.cap,self.plan,self.dated=native_stages_fixture(self.db,end=self.now.date().isoformat(),service_receipt=getattr(self,'service_receipt',False),
            advancing_receipt_clock=getattr(self,'advancing_receipt_clock',False),confirmation_actor=getattr(self,'confirmation_actor',None))
        self.before=accounting._save_book(self.runtime,self.book,expected=None,operation_id="initial-local-book")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory=sqlite3.Row
            from packages.application.sheet_vitrina_v1_inventory_history import ensure_inventory_history_schema
            ensure_inventory_history_schema(conn)
            ready.ensure_publication_schema(conn);conn.commit();staging.ensure_staging_schema(conn);operations.ensure_schema(conn)
            conn.execute(f"CREATE TABLE IF NOT EXISTS {ready.TABLE}(bundle_version TEXT NOT NULL REFERENCES registry_upload_versions(bundle_version) ON DELETE CASCADE,activated_at TEXT NOT NULL,as_of_date TEXT NOT NULL,snapshot_id TEXT NOT NULL,plan_version TEXT NOT NULL,refreshed_at TEXT NOT NULL,plan_json TEXT NOT NULL,PRIMARY KEY(bundle_version,as_of_date))")
            ready.ensure_publication_schema(conn)
            days=["2026-09-07",*self.plan["scope"]["dates"]]
            envelope=SheetVitrinaV1Envelope("fixture-v1","native-local-ready",days[-1],days,
                [SheetVitrinaV1TemporalSlot(d,d,d) for d in days],{},
                [SheetVitrinaWriteTarget("DATA_VITRINA","A1","A1:F2","A:F","replace",False,["label","key",*days],
                    [["capital","SKU:1|own_capital_FF_capital_rub",123,*([999]*len(self.plan["scope"]["dates"]))]],1,2+len(days)),
                 SheetVitrinaWriteTarget("STATUS","A1","A1:B1","A:B","replace",False,["key","value"],[],0,2)])
            conn.execute("CREATE TABLE IF NOT EXISTS registry_upload_current_state(slot,bundle_version,activated_at)")
            conn.execute("INSERT INTO registry_upload_current_state VALUES(1,'fixture-v1','local-active')")
            conn.execute(f"INSERT INTO {ready.TABLE}(bundle_version,as_of_date,plan_json,activated_at,snapshot_id,plan_version,refreshed_at) VALUES(?,?,?,?,?,?,?)",("fixture-v1",days[-1],_serialize_sheet_vitrina_plan(envelope),NOW.isoformat(),"fixture-snapshot","fixture-plan",NOW.isoformat()))
            request=conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id='native:new-late-receipt'").fetchone()
            native=_build_posting_plan(conn,request=request,manifest=json.loads(request["request_payload_json"]),epoch=1,intrinsic_only=True)
            source={k:request[k] for k in operations.SOURCE_KEYS}
            source.update(manifest=json.loads(request["request_payload_json"]),effect=operations.effect(native),effect_digest=fingerprint(operations.effect(native)),preview_actor=request['actor'])
            # Actual immutable accepted-source schema; no service recovery or
            # guided legacy completion is claimed by this local fixture.
            conn.execute(f"INSERT OR IGNORE INTO {operations.TABLE}(request_id,source_json,source_digest,accepted_at,actor,state,updated_at) VALUES(?,?,?,?,?,'processing',?)",
                         (request["request_id"],canonical(source),fingerprint(source),request["accepted_at"],request["actor"],request["accepted_at"]))
            conn.commit()
            staging.ensure_staging_schema(conn)
        self.manifest=self.prepare()

    def prepare(self):
        return publication.prepare_publication(self.plan,runtime_dir=self.runtime,owner_generation="local-owned-generation",operation_id="local-late-publication",now=self.now)

    def publish(self,**kwargs):
        with warehouse_functional_job_lock(self.runtime):return publication.publish(self.manifest,runtime_dir=self.runtime,**kwargs)

    def test_real_book_ready_and_dated_native_projection_immutable_prior(self):
        result=self.publish()
        self.assertEqual(result["status"],"published");self.assertTrue(result["ready"])
        self.assertFalse(result["operator_complete"])
        self.assertEqual(accounting.load(self.runtime,version=self.before),(self.book,self.before))
        with ready.readonly(self.db) as conn:
            saved=json.loads(ready.capture_expected(conn,bundle_version="fixture-v1",as_of_date="2026-09-10").plan_json)
            row=next(r for s in saved["sheets"] if s["sheet_name"]=="DATA_VITRINA" for r in s["rows"] if r[1]=="SKU:1|own_capital_FF_capital_rub")
            self.assertEqual(row[2],123);self.assertEqual(row[3],350000.0)
            self.assertEqual(saved["metadata"]["fbs_accounting_bindings"]["2026-09-08"]["book_version"],result["book_version"])
            self.assertEqual(conn.execute("SELECT count(*) FROM sheet_vitrina_v1_warehouse_business_projection_current_rows WHERE as_of_date='2026-09-08'").fetchone()[0],3)
        self.assertEqual(self.publish()["manifest_digest"],result["manifest_digest"])

    def test_live_owner_required(self):
        with self.assertRaises(Exception):publication.publish(self.manifest,runtime_dir=self.runtime)
        self.assertEqual(accounting.load(self.runtime)[1],self.before)

    def test_crash_after_book_exact_same_identity_recovers_ready(self):
        def fail(boundary):
            if boundary=="after_book":raise RuntimeError("local simulated process death")
        with self.assertRaisesRegex(RuntimeError,"local simulated process death"):self.publish(fault_injector=fail)
        self.assertEqual(accounting.load(self.runtime)[1],self.manifest["after_book"])
        with ready.readonly(self.db) as conn:
            # Actual process recovery consumes only the durable JSON intent,
            # not the original caller's Python tuple/object identities.
            self.manifest=json.loads(conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id='local-late-publication'").fetchone()[0])
        result=self.publish();self.assertTrue(result["native_dated_stages"])
        self.assertEqual(publication.publication_receipt(self.manifest,runtime_dir=self.runtime)["manifest_digest"],result["manifest_digest"])

    def mixed_current_without_operator_confirmation(self):
        # Actual native request/_build_posting_plan/_apply_plan overhead fixture,
        # deliberately ordinary current authority without operator confirmation.
        from datetime import timedelta
        from apps.fbs_accounting_historical_cohort_smoke import post
        from packages.application import fbs_accounting_historical_sources as sources
        from packages.application.fbs_accounting_historical_revision import build_historical_revision_plan
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.row_factory=sqlite3.Row
            current=post(conn,name='mixed-current-native',kind='pool_overhead',day=self.now.date().isoformat(),
                manifest=dict(facility_id='A',scope='FBS',amount_rub='100',category='other',comment='Native current cohort',source_mode='manual'))
            request_id=conn.execute('SELECT request_id FROM sheet_vitrina_v1_ff_pool_documents WHERE document_id=?',(current,)).fetchone()[0]
            self.assertIsNone(conn.execute(f'SELECT 1 FROM {operations.TABLE} WHERE request_id=?',(request_id,)).fetchone())
        self.now+=timedelta(minutes=2)
        with ready.readonly(self.db) as conn:
            capture=staging._capture(conn,self.db,self.plan,self.now,self.book)
            capture=sources.augment_native_requests(conn,capture,document_ids=[*self.plan['receipt_document_ids'],current,*self.plan.get('auxiliary_document_ids',[])])
        self.plan=build_historical_revision_plan(self.book,capture,receipt_document_ids=self.plan['receipt_document_ids'],
            current_document_ids=[current],auxiliary_document_ids=self.plan.get('auxiliary_document_ids',[]))
        self.assertEqual(self.plan['status'],'ready',self.plan.get('blocker'))
        self.manifest=self.prepare()
        self.assertNotIn(request_id,self.manifest['operator_confirmation'])
        self.assertIn(request_id,self.manifest['original_capture']['native_requests_by_id'])
        return current,request_id

    def crash_and_reload_mixed_current(self):
        current,request_id=self.mixed_current_without_operator_confirmation()
        def crash(boundary):
            if boundary=='after_book':raise RuntimeError('native mixed cohort after book crash')
        with self.assertRaisesRegex(RuntimeError,'after book crash'):self.publish(fault_injector=crash)
        with ready.readonly(self.db) as conn:
            self.manifest=json.loads(conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id='local-late-publication'").fetchone()[0])
        self.assertEqual(accounting.load(self.runtime)[1],self.manifest['after_book'])
        return current,request_id

    def test_unconfirmed_native_current_cohort_exact_book_crash_recovery(self):
        current,request_id=self.crash_and_reload_mixed_current()
        result=self.publish();self.assertTrue(result['native_dated_stages']);self.assertTrue(result['ready'])
        book=accounting.load(self.runtime)[0]
        self.assertEqual(book,self.manifest['candidate']['candidate_book'])
        self.assertEqual(book['state']['periods']['2026-09-10']['rows']['A:1']['wac_rub'],'150.10')
        self.assertNotIn(current,book['state']['periods']['2026-09-09']['applied_documents'])
        self.assertIn(current,book['state']['periods']['2026-09-10']['applied_documents'])
        with ready.readonly(self.db) as conn:
            self.assertIsNone(conn.execute(f'SELECT 1 FROM {operations.TABLE} WHERE request_id=?',(request_id,)).fetchone())
        self.assertEqual(accounting.load(self.runtime,version=self.before),(self.book,self.before))
        self.assertEqual(self.publish()['manifest_digest'],result['manifest_digest'])

    def test_mixed_recovery_still_requires_exact_original_current_actor(self):
        _,request_id=self.crash_and_reload_mixed_current()
        with closing(sqlite3.connect(self.db)) as conn,conn:
            # Adversarial corruption only of this local synthetic DB. Removing
            # native immutability is itself included in the query fence too.
            for trigger in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(REQUESTS_TABLE,)).fetchall():
                conn.execute('DROP TRIGGER '+trigger[0])
            conn.execute(f'UPDATE {REQUESTS_TABLE} SET actor=? WHERE request_id=?',('foreign-actor',request_id))
        with self.assertRaisesRegex(ValueError,'historical_original_native_request_changed'):self.publish()
        self.assertEqual(accounting.load(self.runtime)[1],self.manifest['after_book'])
        self.assertEqual(ready.publication_status(self.db,operation_id='local-late-publication',attempt_id='1')['state'],'prepared')

    def test_native_source_change_before_cas_does_not_overwrite(self):
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SET amount=amount+1 WHERE nm_id=1")
        with self.assertRaises(ValueError):self.publish()
        self.assertEqual(accounting.load(self.runtime)[1],self.before)

    def test_partial_book_commit_recovers_original_date_after_next_day_new_cohort(self):
        def fail(boundary):
            if boundary=='after_book':raise RuntimeError('LOCAL crash after durable book')
        with self.assertRaises(RuntimeError):self.publish(fault_injector=fail)
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.row_factory=sqlite3.Row
            # LOCAL next-day archived generation and actual native new expense.
            # Original D inputs are retained; the recovery must not consume D+1.
            day='2026-09-11'
            old=dict(conn.execute('SELECT * FROM sheet_vitrina_v1_wb_fbs_warehouse_registry_runs ORDER BY run_sequence DESC LIMIT 1').fetchone())
            runs=[dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_runs WHERE registry_run_id=?',(old['run_id'],))]
            generation={**old,'run_id':'LOCAL recovery next-day generation','run_sequence':old['run_sequence']+1,'generation_digest':fingerprint(day),'completed_at':day+'T13:55:00Z','started_at':day+'T13:50:00Z'}
            conn.execute('INSERT INTO sheet_vitrina_v1_wb_fbs_warehouse_registry_runs('+','.join(generation)+') VALUES('+','.join('?' for _ in generation)+')',tuple(generation.values()))
            for old_run in runs:
                stock={**old_run,'registry_run_id':generation['run_id'],'run_id':'LOCAL next-day '+old_run['run_id'],'snapshot_at':day+'T13:55:00Z','source_digest':fingerprint([day,old_run])}
                conn.execute('INSERT INTO sheet_vitrina_v1_wb_fbs_stock_snapshot_runs('+','.join(stock)+') VALUES('+','.join('?' for _ in stock)+')',tuple(stock.values()))
                for raw in conn.execute('SELECT * FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_rows WHERE run_id=?',(old_run['run_id'],)).fetchall():
                    row={**dict(raw),'run_id':stock['run_id'],'amount':raw['amount']+17}
                    conn.execute('INSERT INTO sheet_vitrina_v1_wb_fbs_stock_snapshot_rows('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
            from apps.fbs_accounting_historical_cohort_smoke import post
            foreign=post(conn,name='LOCAL new current expense after crash',kind='pool_overhead',day=day,manifest=dict(facility_id='A',scope='FBS',amount_rub='100',category='other',comment='LOCAL',source_mode='manual'))
            self.manifest=json.loads(conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id='local-late-publication'").fetchone()[0])
        result=self.publish()
        self.assertEqual(result['dates'],['2026-09-08','2026-09-09','2026-09-10'])
        recovered=accounting.load(self.runtime)[0]
        self.assertEqual(recovered,self.manifest['candidate']['candidate_book'])
        self.assertNotIn(foreign,self.manifest['plan']['source_proof']['document_manifest'])
        self.assertEqual(accounting.load(self.runtime,version=self.before),(self.book,self.before))

    def test_existing_owned_refresh_selects_exact_overlapping_ready_dates_once(self):
        from dataclasses import replace
        from apps.fbs_accounting_historical_history_smoke import complete_local_metadata
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime,_deserialize_sheet_vitrina_plan
        from packages.application.fbs_accounting_historical_cycle import refresh
        from packages.business_time import default_business_as_of_date
        complete_local_metadata(self.db)
        day=default_business_as_of_date(self.now)
        with ready.readonly(self.db) as conn:
            original=conn.execute(f'SELECT plan_json FROM {ready.TABLE} WHERE as_of_date=?',(self.now.date().isoformat(),)).fetchone()[0]
        envelope=replace(_deserialize_sheet_vitrina_plan(original),as_of_date=day)
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.execute(f'INSERT INTO {ready.TABLE}(bundle_version,as_of_date,plan_json,activated_at,snapshot_id,plan_version,refreshed_at) VALUES(?,?,?,?,?,?,?)',
                ('fixture-v1',day,_serialize_sheet_vitrina_plan(envelope),self.now.isoformat(),envelope.snapshot_id,envelope.plan_version,self.now.isoformat()))
        runtime=RegistryUploadDbBackedRuntime(self.runtime)
        with warehouse_functional_job_lock(self.runtime):result=refresh(runtime,now=self.now)
        self.assertEqual(result['status'],'published')
        with ready.readonly(self.db) as conn:
            manifest=json.loads(conn.execute('SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=?',(result['operation_id'],)).fetchone()[0])
            assigned=[d for t in manifest['targets'] for d in t['dates']]
            self.assertEqual(sorted(assigned),manifest['plan']['scope']['dates'])
            self.assertEqual(len(assigned),len(set(assigned)))
            self.assertEqual(conn.execute('SELECT count(*) FROM sheet_vitrina_v1_inventory_history_finalizations').fetchone()[0],2)
        self.assertEqual(accounting.current_publication_receipt(runtime,now=self.now)['accounting_version'],result['book_version'])

    def test_forged_numerical_candidate_and_recomputed_hashes_cannot_authorize(self):
        from copy import deepcopy
        forged=deepcopy(self.manifest)
        forged["candidate"]["candidate_book"]["state"]["periods"]["2026-09-08"]["rows"]["A:1"]["wac_rub"]="999"
        forged["candidate"]["after_book"]=fingerprint(forged["candidate"]["candidate_book"])
        forged["candidate"]["manifest_digest"]=fingerprint({k:v for k,v in forged["candidate"].items() if k!="manifest_digest"})
        forged["after_book"]=forged["candidate"]["after_book"]
        forged["manifest_digest"]=fingerprint({k:v for k,v in forged.items() if k not in {"manifest_digest","prepare_seconds"}})
        with warehouse_functional_job_lock(self.runtime):
            with self.assertRaisesRegex(ValueError,"historical_numeric_or_stage_rebuild_mismatch"):
                publication.publish(forged,runtime_dir=self.runtime)
        self.assertEqual(accounting.load(self.runtime)[1],self.before)


if __name__=="__main__":unittest.main()
