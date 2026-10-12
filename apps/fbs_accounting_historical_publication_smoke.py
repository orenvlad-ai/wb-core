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
from unittest.mock import patch
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
            advancing_receipt_clock=getattr(self,'advancing_receipt_clock',False),confirmation_actor=getattr(self,'confirmation_actor',None),
            saved_last=getattr(self,'saved_last',None))
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
            conn.execute('CREATE TABLE IF NOT EXISTS registry_upload_config_v2(bundle_version,nm_id,display_name,display_order)')
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
            self.assertEqual({r[0] for r in conn.execute("SELECT nm_id FROM sheet_vitrina_v1_warehouse_business_projection_current_rows WHERE as_of_date='2026-09-08'")},{0,1})
        revised=accounting.load(self.runtime)[0]
        self.assertEqual(set(revised['presentations']['2026-09-08']['rows']),{'1','2'})
        self.assertEqual(revised['presentations']['2026-09-08']['rows']['2']['stages']['WB']['quantity'],'0')
        self.assertIsNone(self.book['retained_days']['2026-09-08']['rows']['2']['stages']['WB']['quantity'])
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
            self.manifest=publication.expand_manifest(json.loads(conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id='local-late-publication'").fetchone()[0]))
        result=self.publish();self.assertTrue(result["native_dated_stages"])
        self.assertEqual(publication.publication_receipt(self.manifest,runtime_dir=self.runtime)["manifest_digest"],result["manifest_digest"])

    def parameters_and_book_crash(self):
        from packages.application.calculation_parameters import ensure_calculation_parameters_schema,DEFAULT_PROXY_PARAMETERS,PROXY_BLOCK_KEY
        from packages.application.calculation_parameters_v4 import ensure_proxy_v4_schema,PROXY_V4_BLOCK_KEY,AUTOMATIC_RATE_FIELDS,PROXY_V4_FORMULA_VERSION
        with closing(sqlite3.connect(self.db)) as conn,conn:
            ensure_calculation_parameters_schema(conn);ensure_proxy_v4_schema(conn)
            conn.execute('INSERT INTO sheet_vitrina_v1_calculation_parameter_versions VALUES(?,?,?,?,?,?,?,?,?)',
                ('local-proxy3',PROXY_BLOCK_KEY,1,'2026-07-01',canonical(DEFAULT_PROXY_PARAMETERS.public()),'local-fingerprint','fixture','fixture',NOW.isoformat()))
            values={field:'0' for field in AUTOMATIC_RATE_FIELDS}
            values.update(buyout_rate='0.91',tax_rate='0.06',agent_remuneration_rate='0.38',
                source_week_ranges=[['2026-08-24','2026-08-30']],buyout_order_count_weight='123.4500',
                finance_net_revenue_weight='456.7800',formula_version=PROXY_V4_FORMULA_VERSION)
            conn.execute('INSERT INTO sheet_vitrina_v1_proxy_v4_parameter_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('local-proxy4',PROXY_V4_BLOCK_KEY,1,'2026-07-01','2026-08-24','2026-08-30','local-window',canonical(values),'local-fingerprint','fixture','fixture',NOW.isoformat()))
        self.manifest=self.prepare()
        proof=self.manifest['parameter_proof']['2026-09-08']
        self.assertEqual(proof[0]['buyout_rate'],'0.91')
        self.assertEqual(proof[1]['buyout_order_count_weight'],'123.4500')
        self.assertEqual(proof[1]['source_week_ranges'],[['2026-08-24','2026-08-30']])
        def crash(boundary):
            if boundary=='after_book':raise RuntimeError('parameters book crash')
        with self.assertRaisesRegex(RuntimeError,'parameters book crash'):self.publish(fault_injector=crash)
        with ready.readonly(self.db) as conn:
            self.manifest=publication.expand_manifest(json.loads(conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id='local-late-publication'").fetchone()[0]))

    def test_native_decimal_parameters_survive_durable_recovery(self):
        self.parameters_and_book_crash()
        self.assertTrue(self.publish()['ready'])

    def test_native_parameter_change_blocks_book_crash_recovery(self):
        self.parameters_and_book_crash()
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.execute("UPDATE sheet_vitrina_v1_proxy_v4_parameter_versions SET parameters_json=json_set(parameters_json,'$.buyout_order_count_weight','124.4500')")
        with self.assertRaisesRegex(ValueError,'historical_original_dated_parameters_changed'):self.publish()

    def test_shared_book_and_before_operands_roundtrip_without_source_mutation(self):
        before=canonical(self.manifest)
        stored=json.loads(canonical(publication.compact_manifest(self.manifest)))
        self.assertNotIn('candidate_book',stored['candidate'])
        self.assertNotIn('before_envelope_json',stored['targets'][0])
        restored=publication.expand_manifest(stored)
        self.assertEqual(restored,self.manifest)
        self.assertEqual(canonical(self.manifest),before)
        self.assertEqual(publication._json_size(stored,publication.MAX_BYTES,'test-limit'),len(canonical(stored).encode()))
        publication._verify(restored)
        with warehouse_functional_job_lock(self.runtime):
            self.assertTrue(publication.publish(stored,runtime_dir=self.runtime)['ready'])

    def test_compact_book_preserves_json_numeric_types(self):
        from copy import deepcopy
        for left,right in ((1,True),(1,1.0),(0,False),(-0.0,0.0)):
            manifest=deepcopy(self.manifest)
            manifest['plan']['candidate_book']['test_scalar']=left
            manifest['candidate']['candidate_book']['test_scalar']=right
            restored=publication.expand_manifest(json.loads(canonical(publication.compact_manifest(manifest))))
            self.assertEqual(canonical(restored),canonical(manifest))

    def test_prepare_refuses_intent_that_its_recovery_cannot_admit(self):
        # Admission uses the same conservative expanded-reference budget as
        # recovery, including overlay overhead, before persisting any intent.
        limit=len(canonical(self.manifest).encode())+1
        with patch.object(publication,'MAX_EXPANDED_BYTES',limit):
            with self.assertRaisesRegex(ValueError,'expanded_size_limit'):self.prepare()
        self.assertEqual(accounting.load(self.runtime)[1],self.before)
        with ready.readonly(self.db) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications').fetchone()[0],0)

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
            self.manifest=publication.expand_manifest(json.loads(conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id='local-late-publication'").fetchone()[0]))
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
            self.manifest=publication.expand_manifest(json.loads(conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id='local-late-publication'").fetchone()[0]))
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
            manifest=publication.expand_manifest(json.loads(conn.execute('SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=?',(result['operation_id'],)).fetchone()[0]))
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


class ReadySelectionBudget(unittest.TestCase):
    def setUp(self):
        self.fixture=HistoricalPublication();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.db=self.fixture.db

    def select(self, **kwargs):
        with ready.readonly(self.db) as conn:
            return publication._selected_ready_rows(conn,plan=self.fixture.plan,
                authority=ready.capture_authority(self.fixture.runtime),current_envelope=kwargs.get('overlay'),now=NOW)

    def insert(self,day,*,dates=None,padding='',bundle='fixture-v1',metadata=None):
        from dataclasses import replace
        from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
        with closing(sqlite3.connect(self.db)) as conn,conn:
            source=conn.execute(f'SELECT plan_json FROM {ready.TABLE} LIMIT 1').fetchone()[0]
            old=_deserialize_sheet_vitrina_plan(source);dates=dates or [day]
            sheets=[replace(s,header=['label','key',*dates],rows=[r[:2]+[999]*len(dates) for r in s.rows],
                            column_count=2+len(dates)) if s.sheet_name=='DATA_VITRINA' else s for s in old.sheets]
            env=replace(old,as_of_date=day,date_columns=dates,sheets=sheets,
                        temporal_slots=[SheetVitrinaV1TemporalSlot(d,d,d) for d in dates],
                        metadata=metadata or {'local_archive_padding':padding})
            _deserialize_sheet_vitrina_plan(_serialize_sheet_vitrina_plan(env))
            conn.execute(f'INSERT INTO {ready.TABLE} VALUES(?,?,?,?,?,?,?)',
                         (bundle,NOW.isoformat(),day,env.snapshot_id,env.plan_version,NOW.isoformat(),_serialize_sheet_vitrina_plan(env)))

    def test_real_large_unselected_archive_no_longer_blocks_publication(self):
        from datetime import date,timedelta
        # More than the real guard, not a patched-down limit. 161 inactive-date
        # rows reproduce the archive-size failure; only the native target loads.
        for n in range(161):self.insert((date(2025,1,1)+timedelta(days=n)).isoformat(),padding='x'*(1024**2+1000))
        with ready.readonly(self.db) as conn:
            old_size=conn.execute(f'SELECT sum(length(plan_json)) FROM {ready.TABLE}').fetchone()[0]
        self.assertGreater(old_size,publication.MAX_BYTES)
        self.fixture.manifest=self.fixture.prepare()
        self.assertEqual(len(self.fixture.manifest['targets']),1)
        self.assertLess(len(canonical(self.fixture.manifest).encode()),publication.MAX_BYTES)
        self.assertTrue(self.fixture.publish()['ready'])
        self.assertEqual(self.fixture.publish()['manifest_digest'],self.fixture.manifest['manifest_digest'])

    def test_gap_candidate_real_publication_crash_recovery(self):
        fixture=HistoricalPublication();fixture.fixture_now=datetime.fromisoformat('2026-09-12T14:00:00+00:00')
        fixture.saved_last='2026-09-09';fixture.setUp();self.addCleanup(fixture.doCleanups)
        def crash(boundary):
            if boundary=='after_book':raise RuntimeError('local gap after_book crash')
        with self.assertRaisesRegex(RuntimeError,'local gap'):fixture.publish(fault_injector=crash)
        with ready.readonly(fixture.db) as conn:
            fixture.manifest=publication.expand_manifest(json.loads(conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id='local-late-publication'").fetchone()[0]))
        self.assertTrue(fixture.publish()['ready'])
        revised=accounting.load(fixture.runtime)[0]
        for day in ('2026-09-10','2026-09-11'):
            self.assertEqual(revised['state']['periods'][day]['snapshot'],fixture.cap['archived_dated_inputs'][day]['quantity_snapshot'])
        self.assertEqual(accounting.load(fixture.runtime,version=fixture.before)[0],fixture.book)

    def test_exact_overlapping_and_inactive_bundle_selection(self):
        self.insert('2026-09-08',dates=['2026-09-08','2026-09-09'])
        self.insert('2026-09-09',bundle='unrelated',padding='x'*10000)
        rows,selected=self.select()
        self.assertEqual(selected,{'2026-09-08':('fixture-v1','2026-09-08'),
                                  '2026-09-09':('fixture-v1','2026-09-10'),
                                  '2026-09-10':('fixture-v1','2026-09-10')})
        self.assertEqual(len(rows),2)

    def test_selected_utf8_budget_fails_before_body_load(self):
        self.insert('2026-09-08',padding='я'*2000)
        with ready.readonly(self.db) as conn:
            text,encoded=conn.execute(f'SELECT length(plan_json),length(CAST(plan_json AS BLOB)) FROM {ready.TABLE} WHERE as_of_date=?',('2026-09-08',)).fetchone()
        self.assertGreater(encoded,text)
        with patch.object(publication,'MAX_BYTES',(text+encoded)//2):
            with self.assertRaisesRegex(ValueError,'historical_ready_source_size_limit'):self.select()

    def test_header_budget_is_independent_of_body_budget(self):
        with patch.object(publication,'MAX_READY_HEADER_BYTES',10):
            with self.assertRaisesRegex(ValueError,'historical_ready_header_size_limit'):self.select()

    def test_header_count_limit(self):
        self.insert('2026-09-08')
        with patch.object(publication,'MAX_READY_HEADERS',1):
            with self.assertRaisesRegex(ValueError,'historical_ready_header_size_limit'):self.select()

    def test_missing_dates_fail_closed(self):
        self.fixture.plan['scope']['dates'].append('2025-01-01')
        with self.assertRaisesRegex(ValueError,'historical_exact_ready_dates_missing'):self.select()

    def test_recovery_metadata_budget(self):
        # An older covering source reaches recovery checking only when today's
        # default does not cover it. This otherwise hidden read is also bounded.
        self.insert('2026-09-07',dates=['2025-01-01'],metadata={'server_cell_presentation':{'padding':'я'*1000}})
        self.fixture.plan['scope']['dates']=['2025-01-01']
        with patch.object(publication,'MAX_BYTES',1000):
            with self.assertRaisesRegex(ValueError,'historical_ready_recovery_size_limit'):self.select()

    def test_one_recovery_payload_budgeted_once_for_multiple_dates(self):
        from packages.application.web_vitrina_management_history import FACT_SOURCE,FACT_GROUPS
        dates=['2025-01-01','2025-01-02']
        meta={'server_cell_presentation':{'row':{d:{'source':FACT_SOURCE,'source_as_of_date':d,
             'complete_source_groups':sorted(FACT_GROUPS),'padding':'я'*1500} for d in dates}}}
        self.insert('2026-09-07',dates=dates,metadata=meta)
        self.fixture.plan['scope']['dates']=dates
        with ready.readonly(self.db) as conn:
            size=conn.execute(f'SELECT length(CAST(plan_json AS BLOB)) FROM {ready.TABLE} WHERE as_of_date=?',('2026-09-07',)).fetchone()[0]
        with patch.object(publication,'MAX_BYTES',size+1):
            rows,selected=self.select()
        self.assertEqual(len(rows),1)
        self.assertEqual(set(selected.values()),{('fixture-v1','2026-09-07')})

    def test_legacy_dates_are_inferred_from_native_header(self):
        for legacy in ('missing','null'):
            with closing(sqlite3.connect(self.db)) as conn,conn:
                raw=json.loads(conn.execute(f'SELECT plan_json FROM {ready.TABLE}').fetchone()[0])
                if legacy=='missing':raw.pop('date_columns',None)
                else:raw['date_columns']=None
                conn.execute(f'UPDATE {ready.TABLE} SET plan_json=?',(canonical(raw),))
            rows,selected=self.select()
            self.assertEqual(len(rows),1)
            self.assertEqual(set(selected),set(self.fixture.plan['scope']['dates']))

    def rollover_fixture(self):
        from dataclasses import replace
        from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
        self.fixture=HistoricalPublication();self.fixture.fixture_now=datetime.fromisoformat('2026-09-12T14:00:00+00:00')
        self.fixture.saved_last='2026-09-09';self.fixture.setUp();self.addCleanup(self.fixture.doCleanups);self.db=self.fixture.db
        self.insert('2026-09-09',dates=['2026-09-08','2026-09-09','2026-09-10'],
                    metadata={'server_cell_presentation':{'SKU:1|sales':{'2026-09-10':{'quality':'partial','value':None,'source':'saved-only','reason':'missing_input'}}}})
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.execute(f'DELETE FROM {ready.TABLE} WHERE as_of_date=?',('2026-09-12',))
            raw=json.loads(conn.execute(f'SELECT plan_json FROM {ready.TABLE}').fetchone()[0])
            sheet=next(s for s in raw['sheets'] if s['sheet_name']=='DATA_VITRINA')
            sheet['rows'].append(['sales','SKU:1|sales',4,5,None]);sheet['row_count']=len(sheet['rows'])
            conn.execute(f'UPDATE {ready.TABLE} SET plan_json=?',(canonical(raw),))
        authority=ready.capture_authority(self.fixture.runtime)
        with ready.readonly(self.db) as conn:
            donor=ready.capture_expected(conn,bundle_version='fixture-v1',as_of_date='2026-09-09',authority=authority)
            expected=ready.capture_expected(conn,bundle_version='fixture-v1',as_of_date='2026-09-11',authority=authority)
        env=_deserialize_sheet_vitrina_plan(donor.plan_json)
        dates=['2026-09-11','2026-09-12'];sheets=[replace(s,header=['label','key',*dates],rows=[r[:2]+[999]*len(dates) for r in s.rows],column_count=2+len(dates)) if s.sheet_name=='DATA_VITRINA' else s for s in env.sheets]
        overlay=replace(env,as_of_date='2026-09-11',date_columns=dates,sheets=sheets,
                        temporal_slots=[SheetVitrinaV1TemporalSlot(d,d,d) for d in dates],metadata={'publication_inputs':{}})
        self.fixture.manifest=publication.prepare_publication(self.fixture.plan,runtime_dir=self.fixture.runtime,
            owner_generation='local-owned-generation',operation_id='local-late-publication',now=self.fixture.now,current_envelope=(overlay,expected))
        return donor

    def test_rollover_promotes_accepted_partial_column_and_recovers_exact_intent(self):
        donor=self.rollover_fixture()
        promoted=next(t for t in self.fixture.manifest['targets'] if t['expected']['as_of_date']=='2026-09-10')
        self.assertEqual(promoted['donor_expected']['plan_json'],donor.plan_json)
        self.assertIsNone(promoted['expected']['plan_json'])
        before=json.loads(donor.plan_json);after=json.loads(promoted['after_json'])
        self.assertEqual(after['metadata']['server_cell_presentation']['SKU:1|sales'],before['metadata']['server_cell_presentation']['SKU:1|sales'])
        def row(p):return next(r for s in p['sheets'] if s['sheet_name']=='DATA_VITRINA' for r in s['rows'] if r[1]=='SKU:1|sales')
        self.assertEqual(row(after),row(before))
        stored=json.loads(canonical(publication.compact_manifest(self.fixture.manifest)))
        self.assertEqual(publication.expand_manifest(stored),self.fixture.manifest)
        stored_promoted=next(t for t in stored['targets'] if t['expected']['as_of_date']=='2026-09-10')
        self.assertEqual(stored['targets'][stored_promoted['donor_expected_target']]['expected'],promoted['donor_expected'])
        self.assertEqual(stored_promoted['before_envelope_source'],'donor_promoted')
        def crash(boundary):
            if boundary=='after_book':raise RuntimeError('LOCAL promotion crash')
        with self.assertRaisesRegex(RuntimeError,'promotion crash'):self.fixture.publish(fault_injector=crash)
        with ready.readonly(self.db) as conn:
            self.fixture.manifest=publication.expand_manifest(json.loads(conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id='local-late-publication'").fetchone()[0]))
        self.assertTrue(self.fixture.publish()['ready'])
        self.assertTrue(self.fixture.publish()['ready'])
        with ready.readonly(self.db) as conn:
            saved=json.loads(ready.capture_expected(conn,bundle_version='fixture-v1',as_of_date='2026-09-10').plan_json)
        self.assertEqual(row(saved),row(before))

    def test_rollover_donor_change_blocks_before_book(self):
        self.rollover_fixture()
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.execute(f'UPDATE {ready.TABLE} SET plan_json=plan_json||? WHERE as_of_date=?',(' ','2026-09-09'))
        with self.assertRaisesRegex(ValueError,'ready_target_changed'):self.fixture.publish()
        self.assertEqual(accounting.load(self.fixture.runtime)[1],self.fixture.before)

    def test_compact_intent_rejects_ambiguous_or_recursive_references(self):
        from copy import deepcopy
        self.rollover_fixture()
        stored=json.loads(canonical(publication.compact_manifest(self.fixture.manifest)))
        promoted_index=next(i for i,t in enumerate(stored['targets']) if 'donor_expected_target' in t)
        for index in (-1,True,promoted_index,len(stored['targets'])):
            corrupt=deepcopy(stored);corrupt['targets'][promoted_index]['donor_expected_target']=index
            with self.subTest(index=index),self.assertRaisesRegex(ValueError,'donor_reference_invalid'):
                publication.expand_manifest(corrupt)
        corrupt=deepcopy(stored);corrupt['storage_encoding']='unknown'
        with self.assertRaisesRegex(ValueError,'encoding_unknown'):publication.expand_manifest(corrupt)
        corrupt=deepcopy(stored);corrupt['candidate']['candidate_book']={}
        with self.assertRaisesRegex(ValueError,'duplicate_book'):publication.expand_manifest(corrupt)
        corrupt=deepcopy(stored);corrupt['targets'][0]['before_envelope_source']='unknown'
        with self.assertRaisesRegex(ValueError,'before_reference_invalid'):publication.expand_manifest(corrupt)
        corrupt=deepcopy(stored);corrupt['candidate']['candidate_book_overlay']['fields']['historical_revision']['native_stage_editions']['2026-09-08']='forged'
        with self.assertRaisesRegex(ValueError,'manifest_corrupt'):publication._verify(publication.expand_manifest(corrupt))

    def test_compact_reference_amplification_is_bounded_before_reconstruction(self):
        from copy import deepcopy
        self.rollover_fixture()
        stored=json.loads(canonical(publication.compact_manifest(self.fixture.manifest)))
        promoted=next(t for t in stored['targets'] if 'donor_expected_target' in t)
        donor=stored['targets'][promoted['donor_expected_target']]
        promoted['donor_expected_target']=0
        stored['targets']=[donor,*[deepcopy(promoted) for _ in range(40)]]
        limit=len(canonical(stored).encode())+len(canonical(stored['plan']['candidate_book']).encode())+1
        with patch.object(publication,'MAX_EXPANDED_BYTES',limit),patch.object(publication,'_promoted_envelope',side_effect=AssertionError('large reconstruction before budget')):
            with self.assertRaisesRegex(ValueError,'expanded_size_limit'):publication.expand_manifest(stored)

    def test_final_manifest_budget_still_applies(self):
        limit=len(canonical(self.fixture.manifest).encode())//2
        with patch.object(publication,'MAX_BYTES',limit):
            with self.assertRaisesRegex(ValueError,'historical_publication_size_limit'):self.fixture.prepare()
        self.assertEqual(accounting.load(self.fixture.runtime)[1],self.fixture.before)

    def test_overlay_replaces_header_but_preserves_persisted_before(self):
        from dataclasses import replace
        from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
        authority=ready.capture_authority(self.fixture.runtime)
        with ready.readonly(self.db) as conn:expected=ready.capture_expected(conn,bundle_version='fixture-v1',as_of_date='2026-09-10',authority=authority)
        env=_deserialize_sheet_vitrina_plan(expected.plan_json)
        env=replace(env,metadata={**env.metadata,'publication_inputs':{}},snapshot_id='new-overlay')
        rows,_=self.select(overlay=(env,expected))
        self.assertEqual(rows[0]['_expected']['plan_json'],expected.plan_json)
        self.assertNotEqual(rows[0]['plan_json'],expected.plan_json)
        self.assertEqual(json.loads(rows[0]['plan_json'])['snapshot_id'],'new-overlay')
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.execute(f'UPDATE {ready.TABLE} SET plan_json=plan_json||? WHERE as_of_date=?',(' ','2026-09-10'))
        with self.assertRaisesRegex(ValueError,'ready_target_changed'):self.select(overlay=(env,expected))

    def test_first_overlay_has_absent_before_and_preserves_all_dates(self):
        from dataclasses import replace
        from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
        authority=ready.capture_authority(self.fixture.runtime)
        with ready.readonly(self.db) as conn:
            old=ready.capture_expected(conn,bundle_version='fixture-v1',as_of_date='2026-09-10')
            expected=ready.capture_expected(conn,bundle_version='fixture-v1',as_of_date='2026-09-11',authority=authority)
        env=replace(_deserialize_sheet_vitrina_plan(old.plan_json),as_of_date='2026-09-11',metadata={'publication_inputs':{}})
        rows,selected=self.select(overlay=(env,expected))
        row=next(r for r in rows if r['as_of_date']=='2026-09-11')
        self.assertIsNone(row['_expected']['plan_json'])
        self.assertEqual(selected['2026-09-10'],('fixture-v1','2026-09-10'))


if __name__=="__main__":unittest.main()
