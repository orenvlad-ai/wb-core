#!/usr/bin/env python3
"""Fully real disposable native/fee/stage/book/Ready recovery contour."""
from contextlib import closing
from copy import deepcopy
from datetime import date,datetime,timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import fbs_historical_fbo_stage_sources_smoke as source_fixture
from apps import fbs_historical_fbo_publication_smoke as publication_fixture
from apps.fbs_historical_fbo_gap_smoke import cap
from apps.fbs_historical_fbo_dispatch_smoke import seal
from packages.application import fbs_historical_fbo_publication as typed
from packages.application import fbs_accounting_historical_publication as publication
from packages.application import fbs_accounting_runtime as accounting
from packages.application import fbs_historical_fbo_native as native
from packages.application import ready_publication as ready
from packages.application import fbs_accounting_historical_revision_writer as staging
from packages.application.fbs_accounting_historical_sources import archived_quantity_snapshot
from packages.application.fbs_snapshot_cost import initialize_candidate,evaluate_candidate,close_candidate_period,fingerprint,canonical,cost_continuity_missing_dates
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot,capture_retained_stages
from packages.application.fbs_historical_fbo_dispatch import prepare_historical_fbo_dispatch_plan
from packages.application.registry_upload_db_backed_runtime import _connect,_serialize_sheet_vitrina_plan
from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE,STOCK_RUNS_TABLE,STOCK_ROWS_TABLE,WAREHOUSE_MAPPINGS_TABLE,COMPLETE_CATALOG_OMISSION_ZERO_POLICY
from packages.application.warehouse_functional import _materialize_compact_warehouse_read_models
from packages.application.warehouse_business_projection import publish_functional_version_business_projection
from packages.application.sheet_vitrina_v1_inventory_history import ensure_inventory_history_schema
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope,SheetVitrinaWriteTarget,SheetVitrinaV1TemporalSlot

NOW=datetime.fromisoformat('2026-11-02T15:00:00+00:00')
STAMP=NOW.isoformat().replace('+00:00','Z')
P='sheet_vitrina_v1_warehouse_'


class FullTypedReadyTests(unittest.TestCase):
    force_pending_components=True
    acceptance_dates={'101':'2026-10-20','102':'2026-10-21','103':'2026-10-25'}
    seed_fee=source_fixture.HistoricalFboStageSources.seed_fee
    seed_version=source_fixture.HistoricalFboStageSources.seed_version
    snapshot=publication_fixture.TypedFboPublicationTests.snapshot
    record=publication_fixture.TypedFboPublicationTests.record
    apply_native=publication_fixture.TypedFboPublicationTests.apply_native
    live_workbooks=publication_fixture.TypedFboPublicationTests.live_workbooks
    def apply(self,**kw):return None # Build the original source fixture BEFORE any effects.

    def setUp(self):
        source_fixture.HistoricalFboStageSources.setUp(self)
        target=self.runtime.runtime_dir/'registry_upload_runtime.sqlite3'
        self.runtime.db_path.rename(target);self.runtime.db_path=target
        dates=[date(2026,10,15)]+[date(2026,10,21)+timedelta(days=n) for n in range(13)]
        days=[day.isoformat() for day in dates]
        with closing(_connect(target)) as conn:
            # The upstream source fixture includes synthetic preexisting fees;
            # this end-to-end contour instead exercises three actual uploads.
            conn.execute('DELETE FROM sheet_vitrina_v1_fulfillment_service_lines')
            conn.execute('DELETE FROM sheet_vitrina_v1_fulfillment_service_uploads')
            conn.execute(f"DELETE FROM {P}functional_events WHERE event_id='selected'")
            conn.execute("INSERT INTO sheet_vitrina_v1_nomenclature_items(item_id,is_active,is_hidden,nm_id,nomenclature_name,product_type,match_key,aliases_json,created_at,updated_at) VALUES('synthetic-item',1,0,1,'Synthetic','test','synthetic','[]',?,?)",(STAMP,STAMP))
            conn.execute(f'INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,created_at,created_by) VALUES(?,?,?,?,?,?,?)',('synthetic-mapping',123,'synthetic-moscow','synthetic-mapping',1,STAMP,'test'))
            for day in days:
                if day>'2026-10-24':self.seed_version(conn,day,'saved:'+day)
                warehouse=dict(facility_id='synthetic-moscow',mapping_id='synthetic-mapping',seller_warehouse_id=123)
                catalog=dict(complete=True,requested_chrt_count=1,active_nm_id_count=1)
                scope=dict(complete=True,warehouse_count=1,warehouses=[warehouse])
                gid='official:'+day;sid='stock:'+day;moment=day+'T14:50:00Z'
                conn.execute(f'INSERT INTO {REGISTRY_RUNS_TABLE}(run_id,status,complete,started_at,completed_at,warehouse_count,office_count,source_digest,policy_version,catalog_scope_json,warehouse_scope_json,generation_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (gid,'success',1,moment,day+'T14:51:00Z',1,1,fingerprint(gid),COMPLETE_CATALOG_OMISSION_ZERO_POLICY,canonical(catalog),canonical(scope),fingerprint([gid,'complete'])))
                conn.execute(f'INSERT INTO {STOCK_RUNS_TABLE}(run_id,registry_run_id,seller_warehouse_id,status,complete,snapshot_at,requested_chrt_count,returned_chrt_count,identity_scope_json,source_digest,policy_version,explicit_chrt_count,omitted_zero_count,dense_row_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (sid,gid,123,'success',1,moment,1,1,'{}',fingerprint(sid),COMPLETE_CATALOG_OMISSION_ZERO_POLICY,1,0,1))
                conn.execute(f'INSERT INTO {STOCK_ROWS_TABLE}(run_id,seller_warehouse_id,chrt_id,nm_id,amount,evidence_digest,provenance) VALUES(?,?,?,?,?,?,?)',
                    (sid,123,1,1,90,fingerprint([sid,90]),'explicit_wb_row'))
            for day in days[1:]:
                vid='saved:'+day
                locations=[dict(facility_id='synthetic-moscow',pool=pool,nm_id=1,quantity=str(q),capital_rub=c) for pool,q,c in [('FBS',100,'10000'),('FBO',100,'6000')]]
                conn.execute(f'INSERT INTO {P}functional_balances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (vid,'ff',1,'200','80','16000','200','synthetic_exact_physical',0,'0','0','0',canonical(dict(source_records=[dict(locations=locations)]))))
                rows=[]
                for raw in conn.execute(f'SELECT * FROM {P}functional_balances WHERE version_id=?',(vid,)):
                    row=dict(raw);row['provenance']=json.loads(row.pop('provenance_json'));rows.append(row)
                _materialize_compact_warehouse_read_models(conn,version_id=vid,plan=dict(lines=rows),created_at=day+'T12:00:00Z',effective_at=day+'T12:00:00Z',business_effective_date=day)
                publish_functional_version_business_projection(conn,published_version_id=vid,business_effective_date=day,published_at=day+'T12:00:00Z',source_revision=fingerprint([vid,'with FF']))
            conn.commit();conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
            captures={}
            for day in days:
                capture=cap(day);capture['captured_at']='2026-11-02T14:55:00Z'
                capture['quantity_snapshot']=archived_quantity_snapshot(conn,day=day,observed_at=capture['captured_at'],generation_id='official:'+day)
                capture['source_digest']=fingerprint({k:capture[k] for k in ('contract','business_date','quantity_snapshot','documents_complete','documents','documents_reason')})
                captures[day]=capture
            state=initialize_candidate(captures[days[0]])
            state['baseline']['document_cost_state']['fbo_rows']=deepcopy(self.capture['opening']['fbo_rows'])
            state['baseline']['id']=fingerprint({k:v for k,v in state['baseline'].items() if k!='id'})
            for day in days[1:]:
                state=evaluate_candidate(state,captures[day],allow_cost_continuity_gap=day=='2026-10-21')
                if day!=days[-1]:state=close_candidate_period(state,day,today=days[-1])
            self.book['state']=state;self.book['effective_date']='2026-10-15'
            from packages.application.shared_sku_cost_sources import capture_wb_component
            for day in days[1:]:
                wb=capture_wb_component(None,day=day,nm_ids=[1],version_id='saved:'+day,connection=conn)
                retained=capture_retained_stages(None,day=day,wb_version_id='saved:'+day,nm_ids=[1],connection=conn)
                self.book['wb_days'][day]=wb;self.book['retained_days'][day]=retained
                self.book['shared_days'][day]=build_shared_cost_day(state,wb,day)
                self.book['presentations'][day]=FbsInventorySnapshot(fbs_state=state,wb_capture=wb,retained=retained,day=day).payload()
        version=accounting._save_book(self.runtime.runtime_dir,self.book,expected=None,operation_id='original-local-book')
        self.capture['book_version']=version;seal(self.capture);self.plan=prepare_historical_fbo_dispatch_plan(self.capture,self.scope)
        native.load.return_value=(deepcopy(self.book),version)
        self.fees=[dict(request_scope='synthetic-fbo',request_id='synthetic-fee-'+str(i),file_sha256=str(i)*64,wb_supply_id=supply,amount_with_vat_rub=amount) for i,(supply,amount) in enumerate(zip(('101','102','103'),('81885.05','41430.50','110806.50')),1)]
        with closing(_connect(target)) as conn:
            ensure_inventory_history_schema(conn);ready.ensure_publication_schema(conn);conn.commit();staging.ensure_staging_schema(conn)
        with closing(_connect(target)) as conn:
            conn.execute('INSERT INTO registry_upload_versions VALUES(?,?,?)',('local-bundle',STAMP,STAMP))
            conn.execute('INSERT INTO registry_upload_current_state VALUES(1,?,?)',('local-bundle',STAMP))
            conn.execute('INSERT INTO registry_upload_config_v2 VALUES(?,?,?,?,?,?)',('local-bundle',1,1,'Synthetic','Synthetic',1))
            envelope=SheetVitrinaV1Envelope('local-bundle','original-ready','2026-11-02',days,
                [SheetVitrinaV1TemporalSlot(d,d,d) for d in days],{},[
                SheetVitrinaWriteTarget('DATA_VITRINA','A1','A1:P2','A:P','replace',False,['label','key',*days],
                    [['capital','SKU:1|own_capital_FF_capital_rub',*([999]*len(days))]],1,2+len(days)),
                SheetVitrinaWriteTarget('STATUS','A1','A1:B1','A:B','replace',False,['key','value'],[],0,2)])
            conn.execute(f'INSERT INTO {ready.TABLE}(bundle_version,as_of_date,plan_json,activated_at,snapshot_id,plan_version,refreshed_at) VALUES(?,?,?,?,?,?,?)',
                ('local-bundle','2026-11-02',_serialize_sheet_vitrina_plan(envelope),STAMP,'original-ready','original-plan',STAMP));conn.commit()
        self.workbooks=self.live_workbooks();self.binding=self.record()['binding'];self.apply_native(self.binding)
        from packages.application.our_wb_costs import OurWbCostBlock
        with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
            self.accepted=typed.apply_fees_from_effect_intent(self.binding,runtime=self.runtime,workbooks=self.workbooks,owner_token=owner['owner_token'])
            OurWbCostBlock(runtime=self.runtime,timestamp_factory=lambda:STAMP).materialize_wb_supply_cost_layers(historical_fbo_effect_binding=self.binding)
        self.manifest=typed.prepare_publication(self.plan,fee_scope=self.accepted['fee_scope'],effect_binding=self.binding,
            runtime_dir=self.runtime.runtime_dir,owner_generation='local-owner',operation_id=self.recipe['publication_operation_id'],now=NOW)

    def publish(self,**kwargs):
        with warehouse_functional_job_lock(self.runtime.runtime_dir):return publication.publish(self.manifest,runtime_dir=self.runtime.runtime_dir,**kwargs)

    def test_real_typed_stages_book_ready_and_exact_retry(self):
        result=self.publish();self.assertEqual(result['status'],'published');self.assertFalse(result['operator_complete'])
        book,version=accounting.load(self.runtime.runtime_dir)
        self.assertEqual(version,self.manifest['after_book']);self.assertEqual(book,self.manifest['candidate']['candidate_book'])
        self.assertEqual(cost_continuity_missing_dates(book['state']),{'2026-10-'+str(i) for i in range(16,21)})
        self.assertEqual(self.publish()['manifest_digest'],result['manifest_digest'])
        from packages.application.our_wb_costs import OurWbCostBlock
        from unittest.mock import patch
        with warehouse_functional_job_lock(self.runtime.runtime_dir),patch.object(OurWbCostBlock,'_load_current_supplier_ff_cost_lines_by_nm',side_effect=AssertionError('current fallback forbidden')):
            self.assertEqual(OurWbCostBlock(runtime=self.runtime,timestamp_factory=lambda:STAMP).materialize_wb_supply_cost_layers(),0)
        from packages.application.fbs_accounting_historical_history import HistoricalReceipt
        receipt=HistoricalReceipt(self.runtime,self.manifest['operation_id'],self.manifest['attempt_id'])
        proof=receipt.validate_sources_readonly(now=NOW)
        self.assertEqual(set(proof['dated']),set(self.manifest['plan']['scope']['dates'])-{'2026-11-02'})
        self.assertEqual(self.manifest['candidate']['quality_proof']['wb_cost'],'provisional')
        with ready.readonly(self.runtime.db_path) as conn:
            events=[dict(row) for row in conn.execute(f"SELECT * FROM {P}functional_events WHERE event_id LIKE 'whfe_%'")]
            self.assertEqual(len(events),3)
            native_capital=sum(Decimal(line['capital_delta_rub']).copy_negate() for effect in self.plan['effects'] for line in effect['aggregate_movements'])
            accepted_capital=sum(Decimal(event['capital_rub']) for event in events)
            self.assertEqual((accepted_capital-native_capital).quantize(Decimal('.01')),Decimal('234122.05'))
            transit=sum(Decimal(row['capital_rub']) for row in self.manifest['candidate']['editions']['2026-10-23']['balances'] if row['warehouse_key']=='ff_to_wb')
            earlier=sum(Decimal(event['capital_rub']) for event in events if event['business_date']<='2026-10-23')
            self.assertEqual((earlier+transit-native_capital).quantize(Decimal('.01')),Decimal('234122.05'))
            for event in events:
                quality=json.loads(event['provenance_json'])
                self.assertEqual(quality['cost_freshness'],'preliminary')
                self.assertTrue(quality['missing_cost_components'])
            for day in ('2026-10-21','2026-11-02'):
                wb=book['wb_days'][day]
                self.assertIn('provisional',canonical(wb))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ready_publications WHERE kind=? AND state='prepared'",(typed.EFFECT_CONTRACT,)).fetchone()[0],1)

    def test_native_history_compiles_real_typed_ready_dates(self):
        self.publish()
        from packages.application.fbs_accounting_historical_history import HistoricalReceipt
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter,update_live_history
        from packages.application.web_vitrina_history_store import HistoryStore
        from packages.application.own_product_capital import OwnProductCapitalBlock
        OwnProductCapitalBlock(runtime=self.runtime)
        receipt=HistoricalReceipt(self.runtime,self.manifest['operation_id'],self.manifest['attempt_id'])
        private=TemporaryDirectory(prefix='local-typed-history-');self.addCleanup(private.cleanup)
        root=Path(private.name)
        store=HistoryStore(root/'history')
        adapter=LiveNativeAdapter(db_path=self.runtime.db_path,runtime_dir=self.runtime.runtime_dir,cache_dir=root/'proofs',
            now=NOW,date_from='2026-10-21',date_to='2026-11-02',formula_epoch='local-typed-fbo')
        result=update_live_history(adapter=adapter,runtime=self.runtime,store=store,max_recomputes=31,
            deadline_monotonic=time.monotonic()+45,rolling14=False,group_blocks=True,backfill_dates=list(receipt.dates))
        self.assertIn(result['status'],{'published','unchanged'})
        proof=receipt.validate_native_readonly(adapter=adapter,store=store,now=NOW)
        self.assertEqual(set(proof['native']),set(receipt.dates))

    def test_native_finance_recalculates_the_revised_typed_book(self):
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        from apps.wb_finance_weekly_cost_cutover_smoke import _row
        finance=WbFinanceWeeklyBlock(self.runtime.runtime_dir,now_factory=lambda:NOW)
        before=finance.ingest_week(date(2026,10,19),date(2026,10,25),[
            _row(901,'2026-10-21',nm_id=1,quantity=2),_row(902,'2026-10-23',nm_id=1,returned=True)])['aggregate']
        self.publish()
        receipt=finance.recalculate_stale_cost_weeks()
        self.assertIn(receipt['status'],{'applied','already_current'})
        self.assertTrue(receipt['non_target_preserved']);self.assertEqual(receipt['post_verify_stale_week_count'],0)
        self.assertEqual(accounting.load(self.runtime.runtime_dir)[1],self.manifest['after_book'])
        self.assertNotEqual(receipt['weeks'][0]['cogs'],before['cogs'])
        economics=accounting.current_publication_receipt(self.runtime,now=NOW)
        self.assertTrue(economics)

    def test_scope_finalizer_requires_real_ack_and_never_completes_fee_receipts_early(self):
        self.publish()
        from packages.application.business_data_heavy_admission import heavy_admitted
        from packages.application import operator_fulfillment_services as receipts
        from unittest.mock import patch
        economics=accounting.current_publication_receipt(self.runtime,now=NOW)
        flags=dict(status='already_current',non_target_preserved=True,post_verify_stale_week_count=0,accounting_version_unchanged=True)
        with heavy_admitted(self.runtime.runtime_dir,operation='cycle'),warehouse_functional_job_lock(self.runtime.runtime_dir):
            with self.assertRaisesRegex(ValueError,'native_proof_required'):
                typed.finalize_scope(self.runtime,manifest=self.manifest,history_ack=None,finance=flags,economics=economics,now=NOW)
            with self.assertRaisesRegex(ValueError,'economics_unproven'):
                typed.finalize_scope(self.runtime,manifest=self.manifest,history_ack={},finance=flags,economics={},now=NOW)
            original=accounting.load(self.runtime.runtime_dir)
            with patch.object(typed.accounting,'load',return_value=({**original[0],'publication_error':{'reason':'synthetic failure'}},original[1])):
                with self.assertRaisesRegex(ValueError,'healthy_book_required'):
                    typed.finalize_scope(self.runtime,manifest=self.manifest,history_ack={},finance=flags,economics=economics,now=NOW)
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {receipts.COMPLETIONS}').fetchone()[0],0)
            row=conn.execute("SELECT state,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?",(self.binding['operation_id'],self.binding['attempt_id'])).fetchone()
            self.assertEqual(row['state'],'prepared');self.assertNotIn('historical_fbo_completion',json.loads(row['diagnostics_json']))

    def test_crash_after_book_resumes_same_child_without_duplicate_native_or_fees(self):
        def crash(boundary):
            if boundary=='after_book':raise RuntimeError('synthetic afterbook crash')
        with self.assertRaisesRegex(RuntimeError,'afterbook crash'):self.publish(fault_injector=crash)
        self.assertEqual(accounting.load(self.runtime.runtime_dir)[1],self.manifest['after_book'])
        result=self.publish();self.assertEqual(result['status'],'published')
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_fulfillment_service_uploads').fetchone()[0],3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations WHERE source_system=\'historical_fbo_dispatch\'').fetchone()[0],4)


if __name__=='__main__':unittest.main()
