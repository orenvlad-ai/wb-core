"""Disposable native supplier cost, dated stages/book/ready and exact authority."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,date,timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import json,sqlite3,sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_processing_smoke import seed,publish_functional,publish_accounting,NOW,MOMENT,DAY
from apps.operator_supplier_financial_native_smoke import execute
from apps.supplier_preparation_intents_smoke import HEADER,LINES
from apps.cny_ledger_smoke import _save_payment
from apps.ready_publication_smoke import make_plan,save
from packages.application import operator_supplier_history as h,operator_supplier_history_candidate as candidate
from packages.application import operator_supplier_shipments as source,operator_supplier_financial as financial,operator_supplier_processing as processing,supplier_preparation_intents as intents
from packages.application import fbs_accounting_runtime as accounting, fbs_accounting_historical_stages as stages,ready_publication as ready
from packages.application.fbs_snapshot_cost import fingerprint,canonical
from packages.application.cny_ledger import CnyLedgerBlock
from packages.application.registry_upload_db_backed_runtime import _connect,_deserialize_sheet_vitrina_plan
from packages.application.shared_sku_cost_sources import capture_wb_component
from packages.application.fbs_inventory_presentation import capture_retained_stages,FbsInventorySnapshot
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application.warehouse_functional import _materialize_compact_warehouse_read_models
from packages.application.warehouse_business_projection import publish_functional_version_business_projection
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
FIRST='2026-07-05'


def install_native_saved_dates(rt):
    """Saved dated fixtures use real immutable schemas/readers/presentations.

    Their initial money is copied from the actual native supplier publisher,
    with explicit same-date official quantity snapshots and closed zero-FBS
    periods. No historical source is silently borrowed during production reads.
    """
    book,_=accounting.prepare(rt.runtime_dir,now=MOMENT,opening=True)
    dates=h.sources.dates_between(FIRST,DAY)
    current=book['wb_days'][DAY]['version_id']
    with _connect(rt.db_path) as conn:
        version=dict(conn.execute(f'SELECT * FROM {stages.P}functional_versions WHERE version_id=?',(current,)).fetchone())
        snapshot=dict(conn.execute(f'SELECT * FROM {stages.P}wb_snapshots WHERE version_id=?',(current,)).fetchone())
        balances=stages._version_balances(conn,version_id=current)
        companions={table:[dict(r) for r in conn.execute(f'SELECT * FROM {stages.P}{table} WHERE version_id=?',(current,))] for table in ('functional_ff_reservations','supplier_cost_states','unmatched_doprinato')}
        for day in dates[:-1]:
            stamp=day+'T12:00:00Z';vid='fixture-dated:'+day
            v,s=stages._clone_rows({'version':version,'snapshot':snapshot},version_id=vid,day=day,published_at=stamp,balances=balances,source_digest=fingerprint(day))
            v.update(version_kind='hourly_wb_sync',business_effective_date=day,effective_at=stamp)
            water=json.loads(v['source_watermarks_json']);water['captured_at']=stamp;water['wb_snapshot']['snapshot_id']='official:'+day;water['wb_snapshot']['fetched_at']=stamp;v['source_watermarks_json']=canonical(water)
            s.update(snapshot_date=day,fetched_at=stamp,snapshot_id='official:'+day)
            for table,row in (('functional_versions',v),('wb_snapshots',s)):
                conn.execute(f'INSERT INTO {stages.P}{table}({",".join(row)}) VALUES({",".join("?" for _ in row)})',tuple(row.values()))
            lines=[]
            for row in balances:
                line={**deepcopy(row),'version_id':vid};line['provenance']['fixture_saved_date']=day
                if line['warehouse_key']=='wb':
                    for record in line['provenance'].get('source_records',[]):
                        if record.get('source')=='official_wb_snapshot':record.update(snapshot_id='official:'+day,snapshot_date=day,fetched_at=stamp)
                lines.append(line)
                stored={**line,'provenance_json':canonical(line['provenance'])};stored.pop('provenance')
                conn.execute(f'INSERT INTO {stages.P}functional_balances({",".join(stored)}) VALUES({",".join("?" for _ in stored)})',tuple(stored.values()))
            for table,rows in companions.items():
                for row in rows:
                    row={**row,'version_id':vid};conn.execute(f'INSERT INTO {stages.P}{table}({",".join(row)}) VALUES({",".join("?" for _ in row)})',tuple(row.values()))
            _materialize_compact_warehouse_read_models(conn,version_id=vid,plan={'plan_kind':'hourly_wb_sync','plan_fingerprint':v['plan_fingerprint'],'lines':lines,'ff_reservations':[],'unmatched_doprinato':[]},created_at=stamp,effective_at=stamp,business_effective_date=day)
            publish_functional_version_business_projection(conn,published_version_id=vid,business_effective_date=day,published_at=stamp,source_revision=vid)
        conn.commit()
    # The fixture's FBS zero observation is separately saved for each date;
    # cost-only supplier authority never changes any of these official facts.
    original=deepcopy(book['state']['periods'][DAY])
    for day in dates[:-1]:
        period=deepcopy(original);period['status']='closed';period['snapshot'].update(date=day,id='fixture-stock:'+day,captured_at=day+'T12:00:00Z')
        for evidence in period['snapshot'].get('facility_evidence',{}).values():evidence['captured_at']=day+'T12:00:00Z'
        book['state']['periods'][day]=period
    from packages.application.fbs_snapshot_cost import _last_closed
    book['state']['periods'][DAY]['opening_rows']=deepcopy(_last_closed(book['state'])[1])
    book['effective_date']=FIRST
    book['state']['baseline']['business_date']=FIRST
    book['state']['baseline']['snapshot']=deepcopy(book['state']['periods'][FIRST]['snapshot'])
    book['state']['baseline']['id']=fingerprint({k:v for k,v in book['state']['baseline'].items() if k!='id'})
    with ready.readonly(rt.db_path) as conn:
        for day in dates[:-1]:
            wb=capture_wb_component(rt.db_path,day=day,nm_ids=[1],version_id='fixture-dated:'+day,connection=conn)
            assert wb['complete'],wb
            retained=capture_retained_stages(rt.db_path,day=day,wb_version_id=wb['version_id'],nm_ids=[1],connection=conn)
            book['wb_days'][day]=wb;book['retained_days'][day]=retained
            book['shared_days'][day]=build_shared_cost_day(book['state'],wb,day)
            book['presentations'][day]=FbsInventorySnapshot(fbs_state=book['state'],wb_capture=wb,retained=retained,day=day).payload()
    with ready.readonly(rt.db_path) as conn:book['publication_inputs']=ready.capture_material(conn)
    plan=asdict(make_plan(as_of_date=DAY,day=DAY));plan['date_columns']=dates;plan['temporal_slots']=[dict(slot_key=d,slot_label=d,column_date=d) for d in dates]
    data=plan['sheets'][0];data['header']=['label','key',*dates];data['rows']=[[r[0],r[1],*[r[-1] for _ in dates]] for r in data['rows']];data['column_count']=len(data['header'])
    plan=_deserialize_sheet_vitrina_plan(canonical(plan));save(rt,plan,prepared=(book,None),now=MOMENT)
    return book


class Tests(unittest.TestCase):
    def setUp(self):
        tmp=TemporaryDirectory(prefix='supplier-history-real-');self.addCleanup(tmp.cleanup)
        self.runtime=rt=seed(tmp.name)
        # This disposable native book proves one SKU, so its registry roster
        # also declares exactly that SKU on every saved historical date.
        bundle=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text())
        bundle['bundle_version']='supplier-dated-one-sku'
        bundle['config_v2']=[{**bundle['config_v2'][0],'nm_id':1,'display_name':'Fixture'}]
        rt.ingest_bundle(bundle,activated_at=NOW)
        header={**HEADER,'shipment_id':'source','created_at':NOW,'updated_at':NOW,'invoice_date':FIRST,'shipment_date':FIRST,'actual_shipment_date':FIRST,'invoice_no':'paid','expenses_complete':0}
        rt.save_supplier_shipment(header=header,lines=[{**LINES[0],'internal_nm_id':1}])
        ledger=CnyLedgerBlock(runtime=rt,timestamp_factory=lambda:NOW)
        second=getattr(self,'SECOND_SOURCE',False)
        ledger.create_opening_balance({'operation_date':FIRST,'cny_amount':200 if second else 100,'rub_value':2000 if second else 1000})
        _save_payment(rt,'fixture-payment','source',FIRST+'T10:00:00Z','100');ledger.replay_ledger(reason='synthetic-dated')
        rt.save_supplier_financial_document(document=dict(document_id='logistics',supplier_order_id='source',document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date=FIRST,parse_status='confirmed',total_amount_rub=200),expense_lines=[dict(line_id='expense',amount=200,amount_rub=200,currency='RUB',category='logistics',status='confirmed')])
        owners=['source']
        if second:
            owners.append('second');self.second_header={**header,'shipment_id':'second','invoice_no':'second'};self.second_lines=[{**LINES[0],'line_id':'second-product','internal_nm_id':1}]
            rt.save_supplier_shipment(header=self.second_header,lines=self.second_lines)
            _save_payment(rt,'second-payment','second',FIRST+'T11:00:00Z','100');ledger.replay_ledger(reason='synthetic-second-dated')
        intents.drain_supplier_preparation_intents(rt,shipment_ids=owners);publish_functional(rt)
        self.before=install_native_saved_dates(rt)
        result=execute(rt,'financial-historical-cost',[{'child_key':'expense','kind':'financial','subject_id':'extra-expense'}],lambda c:rt.save_supplier_financial_document(document=dict(document_id='extra-expense',supplier_order_id='source',document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date=FIRST,parse_status='confirmed',total_amount_rub=120),expense_lines=[dict(line_id='extra-line',amount=120,amount_rub=120,currency='RUB',category='logistics',status='confirmed')]))
        self.parent=result['results'][0]['acceptance']['operation_id']
        with ready.readonly(rt.db_path) as conn:self.identity=conn.execute(f'SELECT operation_id FROM {financial.SCOPES} WHERE parent_operation_id=?',(self.parent,)).fetchone()[0]
        if second:
            if getattr(self,'SECOND_NO_CHANGE',False):
                payload={'request_id':'second-invoice-label','invoice_no':'same-cost'}
                result=source.execute(rt,action='edit',payload=payload,shipment_id='second',actor='alice',request_scope='alice-key',write=lambda context:rt.save_supplier_shipment(header={**self.second_header,'invoice_no':'same-cost'},lines=self.second_lines,operator_request=context))
                self.second_identity=result['acceptance']['operation_id']
            else:
                result=execute(rt,'second-historical-cost',[{'child_key':'expense','kind':'financial','subject_id':'second-expense'}],lambda c:rt.save_supplier_financial_document(document=dict(document_id='second-expense',supplier_order_id='second',document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date=FIRST,parse_status='confirmed',total_amount_rub=60),expense_lines=[dict(line_id='second-extra-line',amount=60,amount_rub=60,currency='RUB',category='logistics',status='confirmed')]),owners=('second',))
                with ready.readonly(rt.db_path) as conn:self.second_identity=conn.execute(f'SELECT operation_id FROM {financial.SCOPES} WHERE parent_operation_id=?',(result['results'][0]['acceptance']['operation_id'],)).fetchone()[0]
        intents.drain_supplier_preparation_intents(rt,shipment_ids=owners);publish_functional(rt);publish_accounting(rt)
    def prepare(self):return h.prepare(self.runtime,self.identity,now=MOMENT)
    def publish(self,manifest,**kw):
        with heavy_admitted(self.runtime.runtime_dir,operation='cycle'),warehouse_functional_job_lock(self.runtime.runtime_dir):return h.publish(self.runtime,manifest,**kw)
    def test_actual_source_native_consumer_twenty_dates_append_only(self):
        manifest=self.prepare();self.assertEqual(len(manifest['candidate']['effect_dates']),19)
        self.assertTrue(all(manifest['candidate']['evaluation'][d]['before']!=manifest['candidate']['evaluation'][d]['after'] for d in manifest['candidate']['effect_dates']))
        old=manifest['expected_book'];receipt=self.publish(manifest)
        read=receipt.validate_sources_readonly(now=MOMENT);self.assertEqual(len(read['dated']),19)
        self.assertEqual(accounting.load(self.runtime.runtime_dir,version=old)[1],old)
        for day in receipt.dates:
            before=self.before['retained_days'][day]['rows']['1']['stages']['PRODUCTION_TO_FF'];after=manifest['candidate']['candidate_book']['retained_days'][day]['rows']['1']['stages']['PRODUCTION_TO_FF']
            self.assertEqual(Decimal(after['capital_rub'])-Decimal(before['capital_rub']),120);self.assertEqual(Decimal(str(after['quantity'])),Decimal(str(before['quantity'])))
        self.assertNotIn('supplier_history_ack',receipt._read()[1])
        with ready.readonly(self.runtime.db_path) as conn:self.assertIsNone(h.completed_proof(conn,manifest['source_ref']))
        again=self.publish(manifest);self.assertEqual(again.binding(),receipt.binding())
    def test_crash_after_book_exact_recovery(self):
        manifest=self.prepare()
        def crash(point):
            if point=='after_book':raise RuntimeError('synthetic process exit after own book')
        with self.assertRaisesRegex(RuntimeError,'synthetic'):self.publish(manifest,inject=crash)
        self.assertEqual(accounting.load(self.runtime.runtime_dir)[1],manifest['after_book'])
        receipt=self.publish(manifest);self.assertEqual(receipt.manifest,manifest)
    def test_rehashed_numeric_candidate_rejected(self):
        manifest=self.prepare();manifest['candidate']['editions'][FIRST]['balances'][0]['capital_rub']='999999'
        manifest['candidate']['manifest_digest']=fingerprint({k:v for k,v in manifest['candidate'].items() if k!='manifest_digest'});manifest['manifest_digest']=fingerprint({k:v for k,v in manifest.items() if k!='manifest_digest'})
        with self.assertRaisesRegex(ValueError,'independent_rebuild_changed'):self.publish(manifest)
    def test_source_race_before_writer_keeps_source_accepted(self):
        manifest=self.prepare()
        with _connect(self.runtime.db_path) as conn:conn.execute("UPDATE sheet_vitrina_v1_supplier_financial_expense_lines SET amount_rub=999 WHERE line_id='extra-line'");conn.commit()
        with self.assertRaises(ValueError):self.publish(manifest)
        receipt=financial.read_request(self.runtime.runtime_dir,self.runtime.db_path,'financial-historical-cost',request_scope='alice-key')['results'][0]['acceptance']
        self.assertTrue(receipt['durable_saved']);self.assertFalse(receipt['processing']['complete'])
    def test_generic_history_success_cannot_ack(self):
        receipt=self.publish(self.prepare())
        with self.assertRaisesRegex(Exception,'not_supervised'):receipt._acknowledge_verified_native({'completed':True})


class CohortTests(unittest.TestCase):
    def fixture(self,*,no_change=False):
        class Fixture(Tests):SECOND_SOURCE=True;SECOND_NO_CHANGE=no_change
        case=Fixture();case.setUp();self.addCleanup(case.doCleanups);return case
    def test_two_actual_sources_same_sku_one_publication_and_restart(self):
        case=self.fixture();manifest=case.prepare();refs=manifest['candidate']['cohort_refs']
        self.assertEqual({r['operation_id'] for r in refs},{case.identity,case.second_identity})
        self.assertEqual(h.prepare(case.runtime,case.second_identity,now=MOMENT),manifest)
        receipt=case.publish(manifest);self.assertEqual(case.publish(manifest).binding(),receipt.binding())
        for day in receipt.dates:
            old=case.before['retained_days'][day]['rows']['1']['stages']['PRODUCTION_TO_FF'];new=manifest['candidate']['candidate_book']['retained_days'][day]['rows']['1']['stages']['PRODUCTION_TO_FF']
            self.assertEqual(Decimal(new['capital_rub'])-Decimal(old['capital_rub']),180);self.assertEqual(Decimal(str(old['quantity'])),Decimal(str(new['quantity'])))
        with ready.readonly(case.runtime.db_path) as conn:
            for ref in refs:self.assertEqual(h.publication_for(conn,ref)['operation_id'],receipt.operation_id)
            foreign={**refs[0],'operation_id':'foreign-source'};self.assertIsNone(h.publication_for(conn,foreign))
    def test_no_change_member_has_own_dated_and_current_dependency_proof(self):
        case=self.fixture(no_change=True);manifest=case.prepare()
        own=manifest['candidate']['member_evaluation'][case.second_identity]
        self.assertTrue(own['independent_supplier_contributions']);self.assertTrue(own['current_no_change'])
        self.assertTrue(all(r['before']==r['after'] for r in own['dated'].values()))
        self.assertFalse(manifest['candidate']['member_evaluation'][case.identity]['current_no_change'])
        case.publish(manifest)
    def test_second_source_change_after_plan_rejects_entire_cohort(self):
        case=self.fixture();manifest=case.prepare()
        with _connect(case.runtime.db_path) as conn:conn.execute("UPDATE sheet_vitrina_v1_supplier_financial_expense_lines SET amount_rub=999 WHERE line_id='second-extra-line'");conn.commit()
        with self.assertRaises(ValueError):case.publish(manifest)
        self.assertNotEqual(accounting.load(case.runtime.runtime_dir)[1],manifest['after_book'])

    def test_more_than_batch_superseded_sources_do_not_starve_actual_publication(self):
        case=Tests();case.setUp();self.addCleanup(case.doCleanups);rt=case.runtime
        old=case.publish(case.prepare())
        with ready.readonly(rt.db_path) as conn:
            header=dict(conn.execute("SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id='source'").fetchone())
            lines=[dict(r) for r in conn.execute("SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines WHERE shipment_id='source'")]
        from apps.operator_supplier_processing_smoke import edit
        obsolete=[]
        for n in range(processing.COHORT_LIMIT+1):
            obsolete.append(edit(rt,header,lines,'obsolete-label-'+str(n),'label-'+str(n))['acceptance']['operation_id'])
        accepted=execute(rt,'latest-history-cost',[{'child_key':'expense','kind':'financial','subject_id':'latest-expense'}],lambda c:rt.save_supplier_financial_document(document=dict(document_id='latest-expense',supplier_order_id='source',document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date=FIRST,parse_status='confirmed',total_amount_rub=90),expense_lines=[dict(line_id='latest-line',amount=90,amount_rub=90,currency='RUB',category='logistics',status='confirmed')]))
        with ready.readonly(rt.db_path) as conn:
            latest=conn.execute(f'SELECT operation_id FROM {financial.SCOPES} WHERE parent_operation_id=?',(accepted['results'][0]['acceptance']['operation_id'],)).fetchone()[0]
        intents.drain_supplier_preparation_intents(rt,shipment_ids=['source']);publish_functional(rt);publish_accounting(rt)
        with heavy_admitted(rt.runtime_dir,operation='cycle'),warehouse_functional_job_lock(rt.runtime_dir):
            self.assertIsNone(h.pending(rt,now=MOMENT))
            receipt=h.pending(rt,now=MOMENT+timedelta(seconds=1))
        self.assertIsNotNone(receipt)
        self.assertIn(latest,{r['operation_id'] for r in receipt.manifest['candidate']['cohort_refs']})
        self.assertNotEqual(receipt.binding(),old.binding())
        with ready.readonly(rt.db_path) as conn:
            self.assertTrue(all(conn.execute(f'SELECT reason FROM {processing.ATTEMPTS} WHERE operation_id=?',(identity,)).fetchone()[0]==processing.SUPERSEDED for identity in obsolete))


class ReplanTests(unittest.TestCase):
    def fixture(self):
        holder=CohortTests();case=holder.fixture();self.addCleanup(holder.doCleanups);return case
    @staticmethod
    def followup(case,old,*,late=True):
        import hashlib
        owner='second' if old['source_ref']['shipment_id']=='source' else 'source'
        request=next('later-source-'+str(n) for n in range(100) if 'supplier_financial_'+hashlib.sha256(('alice-key|later-source-'+str(n)+'|expense').encode()).hexdigest()[:32]>old['source_ref']['operation_id'])
        rt=case.runtime;day='2026-07-20' if late else FIRST
        accepted=execute(rt,request,[{'child_key':'expense','kind':'financial','subject_id':'followup-expense'}],lambda context:rt.save_supplier_financial_document(document=dict(document_id='followup-expense',supplier_order_id=owner,document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date=day,parse_status='confirmed',total_amount_rub=90),expense_lines=[dict(line_id='followup-line',amount=90,amount_rub=90,currency='RUB',category='logistics',status='confirmed')]),owners=(owner,))
        with ready.readonly(rt.db_path) as conn:
            identity=conn.execute(f'SELECT operation_id FROM {financial.SCOPES} WHERE parent_operation_id=?',(accepted['results'][0]['acceptance']['operation_id'],)).fetchone()[0]
        intents.drain_supplier_preparation_intents(rt,shipment_ids=[owner]);publish_functional(rt);publish_accounting(rt)
        return h.prepare(rt,identity,now=MOMENT),identity,request
    def test_actual_followup_keeps_root_new_attempt_all_original_dates(self):
        case=self.fixture();old=case.prepare();case.publish(old)
        new,identity,request=self.followup(case,old)
        self.assertEqual(new['operation_id'],old['operation_id']);self.assertNotEqual(new['attempt_id'],old['attempt_id'])
        self.assertEqual(new['candidate']['owed_dates'],old['candidate']['effect_dates'])
        self.assertEqual(new['candidate']['effect_dates'],old['candidate']['effect_dates'])
        early='2026-07-10';self.assertEqual(new['candidate']['evaluation'][early]['before'],new['candidate']['evaluation'][early]['after'])
        with ready.readonly(case.runtime.db_path) as conn:self.assertIsNone(h.completed_proof(conn,old['source_ref']))
        receipt=case.publish(new);self.assertEqual(set(receipt.dates),set(old['candidate']['effect_dates']))
        with ready.readonly(case.runtime.db_path) as conn:
            prior=conn.execute('SELECT inputs_json,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(old['operation_id'],old['attempt_id'])).fetchone()
            self.assertEqual(json.loads(prior[0]),old);self.assertEqual(json.loads(prior[1])['supplier_history_replaced_by'],h.replacement_link(new))
            self.assertEqual(h.publication_for(conn,old['source_ref'])['attempt_id'],new['attempt_id']);self.assertIsNone(h.completed_proof(conn,old['source_ref']))
        original=financial.read_request(case.runtime.runtime_dir,case.runtime.db_path,'financial-historical-cost',request_scope='alice-key')['results'][0]['acceptance']
        self.assertTrue(original['durable_saved']);self.assertFalse(original['processing']['complete'])
    def test_prepared_after_own_book_uses_original_stages(self):
        case=self.fixture();old=case.prepare()
        def crash(point):
            if point=='after_book':raise RuntimeError('old own book crash')
        with self.assertRaisesRegex(RuntimeError,'old own'):case.publish(old,inject=crash)
        new,_,_=self.followup(case,old)
        self.assertEqual(len(new['candidate']['recovery_inputs']),19)
        for day,edition in new['candidate']['editions'].items():self.assertEqual(edition['saved'],old['candidate']['editions'][day]['saved'])
        case.publish(new)
    def test_same_prepared_attempt_clock_and_lost_response_resume_exactly(self):
        case=self.fixture();old=case.prepare()
        def crash(point):
            if point=='after_intent':raise RuntimeError('intent saved response lost')
        with self.assertRaisesRegex(RuntimeError,'response lost'):case.publish(old,inject=crash)
        with heavy_admitted(case.runtime.runtime_dir,operation='cycle'),warehouse_functional_job_lock(case.runtime.runtime_dir):receipt=h.pending(case.runtime,now=MOMENT+timedelta(minutes=1))
        self.assertEqual(receipt.manifest,old)
        with ready.readonly(case.runtime.db_path) as conn:self.assertEqual(conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE kind=?',(h.CONTRACT,)).fetchone()[0],1)
    def test_original_stage_hash_drift_cannot_recover_ghost(self):
        case=self.fixture();old=case.prepare()
        def crash(point):
            if point=='after_book':raise RuntimeError('ghost')
        with self.assertRaisesRegex(RuntimeError,'ghost'):case.publish(old,inject=crash)
        original=old['candidate']['editions'][FIRST]['saved']['version']['version_id']
        with _connect(case.runtime.db_path) as conn:
            row=conn.execute('SELECT warehouse_key,nm_id,provenance_json FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? LIMIT 1',(original,)).fetchone();value=json.loads(row[2]);value['adversarial_original_input']='drift'
            conn.execute('UPDATE sheet_vitrina_v1_warehouse_functional_balances SET provenance_json=? WHERE version_id=? AND warehouse_key=? AND nm_id=?',(canonical(value),original,row[0],row[1]));conn.commit()
        with self.assertRaisesRegex(ValueError,'ghost_original_stage_changed|historical_saved_wb_authority_changed'):self.followup(case,old)
    def test_corrupt_ack_or_foreign_retirement_does_not_erase_dates(self):
        for bad in ({'supplier_history_ack':{}},{'supplier_history_replaced_by':{'foreign':'authority'}}):
            with self.subTest(bad=bad):
                case=self.fixture();old=case.prepare();case.publish(old)
                with _connect(case.runtime.db_path) as conn:conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=?',(canonical(bad),old['operation_id'],old['attempt_id']));conn.commit()
                with self.assertRaisesRegex(ValueError,'history_ack_corrupt|retirement_link_corrupt'):self.followup(case,old)
    def test_new_book_crash_before_retirement_recovers_same_attempt(self):
        case=self.fixture();old=case.prepare();case.publish(old);new,_,_=self.followup(case,old)
        def crash(point):
            if point=='before_retirement':raise RuntimeError('new own book before retirement')
        with self.assertRaisesRegex(RuntimeError,'new own'):case.publish(new,inject=crash)
        with ready.readonly(case.runtime.db_path) as conn:
            prior=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(old['operation_id'],old['attempt_id'])).fetchone()
            self.assertNotIn('supplier_history_replaced_by',json.loads(prior[0]))
        recovered=case.publish(new);self.assertEqual(recovered.attempt_id,new['attempt_id']);self.assertEqual(recovered.manifest['candidate']['owed_dates'],old['candidate']['effect_dates'])
        self.assertEqual(case.publish(new).binding(),recovered.binding())
    def test_followup_source_race_preserves_old_attempt(self):
        case=self.fixture();old=case.prepare();case.publish(old);new,_,_=self.followup(case,old)
        with _connect(case.runtime.db_path) as conn:conn.execute("UPDATE sheet_vitrina_v1_supplier_financial_expense_lines SET amount_rub=999 WHERE line_id='followup-line'");conn.commit()
        with self.assertRaises(ValueError):case.publish(new)
        with ready.readonly(case.runtime.db_path) as conn:
            prior=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(old['operation_id'],old['attempt_id'])).fetchone()
            self.assertNotIn('supplier_history_replaced_by',json.loads(prior[0]))
    def test_ready_or_code_drift_does_not_grant_new_key(self):
        case=self.fixture();old=case.prepare();case.publish(old)
        with patch.object(candidate,'code_authority',return_value={'foreign':'code'}),heavy_admitted(case.runtime.runtime_dir,operation='cycle'),warehouse_functional_job_lock(case.runtime.runtime_dir):self.assertIsNone(h.pending(case.runtime,now=MOMENT))
        with ready.readonly(case.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE kind=?',(h.CONTRACT,)).fetchone()[0],1)
            reason=conn.execute(f'SELECT reason FROM {processing.ATTEMPTS} WHERE operation_id=?',(case.identity,)).fetchone()[0];self.assertEqual(reason,'supplier_history_formula_changed')
        with _connect(case.runtime.db_path) as conn:
            row=conn.execute('SELECT bundle_version,as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots LIMIT 1').fetchone();plan=json.loads(row[2]);plan['sheets'][0]['rows'][0][2]=999
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE bundle_version=? AND as_of_date=?',(canonical(plan),row[0],row[1]));conn.commit()
        with heavy_admitted(case.runtime.runtime_dir,operation='cycle'),warehouse_functional_job_lock(case.runtime.runtime_dir):self.assertIsNone(h.pending(case.runtime,now=MOMENT))
        with ready.readonly(case.runtime.db_path) as conn:self.assertEqual(conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE kind=?',(h.CONTRACT,)).fetchone()[0],1)
    def test_foreign_ghost_book_tuple_and_original_stage_drift_fail_closed(self):
        case=self.fixture();old=case.prepare()
        def crash(point):
            if point=='after_book':raise RuntimeError('ghost')
        with self.assertRaisesRegex(RuntimeError,'ghost'):case.publish(old,inject=crash)
        current,vid=accounting.load(case.runtime.runtime_dir);changed=deepcopy(current);changed['retained_days'][FIRST]['rows']['1']['stages']['PRODUCTION_TO_FF']['capital_rub']='9999'
        # Real private accounting save creates a foreign tuple, never a fake
        # override of the original immutable revision.
        accounting.save(case.runtime.runtime_dir,changed,expected=vid,operation_id='synthetic-foreign-book')
        with self.assertRaisesRegex(ValueError,'ghost_stage_owner_unproved'):self.followup(case,old)
    def test_attempt_limit_explicit_not_truncated(self):
        case=self.fixture();old=case.prepare();case.publish(old)
        with _connect(case.runtime.db_path) as conn:
            row=dict(conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(old['operation_id'],old['attempt_id'])).fetchone())
            for n in range(candidate.MAX_ATTEMPTS):
                copy={**row,'attempt_id':'synthetic-overflow-'+str(n)};conn.execute('INSERT INTO sheet_vitrina_v1_ready_publications('+','.join(copy)+') VALUES('+','.join('?' for _ in copy)+')',tuple(copy.values()))
            conn.commit()
        with self.assertRaisesRegex(ValueError,'attempt_scope_limit'):self.followup(case,old)


class SelectionTests(unittest.TestCase):
    """Native SQL selection with isolated correlated-proof test operands."""
    def cohort(self,sets,*,completed=(),superseded=()):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;self.addCleanup(conn.close)
        processing.ensure_schema(conn)
        values={}
        for key,ids in sets.items():
            functional={'version_id':'same-native-version','queue_ref':{'affected_nm_ids':ids}}
            op={'operation_id':key};values[key]={'operation':op,'functional':functional}
            conn.execute(f'INSERT INTO {processing.FUNCTIONAL} VALUES(?,?,?,?)',(key,'same-native-version',canonical(functional),'digest'))
            if key in completed:conn.execute(f'INSERT INTO {processing.COMPLETIONS} VALUES(?,?,?,?)',(key,'{}','digest','time'))
            if key in superseded:conn.execute(f'INSERT INTO {processing.ATTEMPTS} VALUES(?,?,?,?)',(key,'needs_attention',processing.SUPERSEDED,'time'))
        return conn,values
    def select(self,conn,values,root,**kw):
        with patch('packages.application.operator_supplier_cost_proof.read_correlated_native',side_effect=lambda conn,key:values[key]),patch.object(processing,'operation',side_effect=lambda conn,key:values[key]['operation']),patch.object(processing,'current',return_value=True),patch.object(processing,'ref',side_effect=lambda op:op):
            return candidate.native_cohort(conn,root,**kw)
    def test_transitive_overlap_foreign_and_completed_excluded(self):
        conn,values=self.cohort({'a':[1],'b':[1,2],'c':[2,3],'foreign':[9],'done':[3,9]},completed=('done',))
        self.assertEqual([m['operation']['operation_id'] for m in self.select(conn,values,'a')],['a','b','c'])
        conn.execute(f'INSERT INTO {processing.ATTEMPTS} VALUES(?,?,?,?)',('b','needs_attention',processing.SUPERSEDED,'time'))
        self.assertEqual([m['operation']['operation_id'] for m in self.select(conn,values,'a')],['a'])
    def test_limit_never_truncates_and_pinned_membership_cannot_expand(self):
        conn,values=self.cohort({str(n):[1] for n in range(candidate.MAX_COHORT+1)})
        with self.assertRaisesRegex(ValueError,'cohort_size_limit'):self.select(conn,values,'0')
        refs=[values['0']['operation'],values['1']['operation']]
        self.assertEqual(len(self.select(conn,values,'0',pinned=refs)),2)
        with self.assertRaisesRegex(ValueError,'date_scope_unavailable'):h.sources.dates_between('2025-01-01','2026-01-02')
    def test_expected_formula_drift_does_not_stop_following_source(self):
        from types import SimpleNamespace
        with TemporaryDirectory(prefix='supplier-history-fair-') as raw:
            runtime=SimpleNamespace(db_path=str(Path(raw)/'source.sqlite'))
            with sqlite3.connect(runtime.db_path) as conn:processing.ensure_schema(conn)
            ref={'operation_id':'old','source_digest':'old-source'}
            corrupt={'contract':h.CONTRACT,'operation_id':'supplier-history:old','source_ref':ref,'candidate':{'code_authority':{'old':'code'}}}
            corrupt['manifest_digest']=fingerprint(corrupt)
            prior={'inputs_json':canonical(corrupt)}
            receipt=object()
            with patch.object(processing,'captured_cohort',return_value=['old','valid']),patch.object(processing,'operation',side_effect=lambda conn,key:{'operation_id':key,'action':'edit'}),patch.object(processing,'current',return_value=True),patch.object(processing,'ref',side_effect=lambda op:ref if op['operation_id']=='old' else {'operation_id':'valid'}),patch.object(h,'publication_for',side_effect=lambda conn,source:prior if source['operation_id']=='old' else None),patch.object(h,'completed_proof',return_value=None),patch.object(h,'_checked') as checked,patch.object(candidate,'prepare',return_value={'effect_dates':[FIRST]}),patch.object(h,'prepare',return_value={'new':'exact'}),patch.object(h,'publish',return_value=receipt):
                self.assertIs(h.pending(runtime,now=MOMENT),receipt)
            self.assertEqual(checked.call_args.args[2],'supplier_history_formula_changed')
            self.assertEqual(checked.call_args.kwargs['state'],'needs_attention')


@unittest.skipUnless(sys.platform=='linux','actual kernel History owner descriptors required')
class LinuxTests(unittest.TestCase):
    def test_native_source_dated_history_finance_exact_completed(self):self.completion()
    def test_source_race_after_consume_rejects_operator_complete(self):self.completion(race='source')
    def test_ready_race_after_consume_rejects_operator_complete(self):self.completion(race='ready')
    def test_native_two_source_cohort_history_finance_each_exact_completed(self):self.completion(second=True)
    def test_followup_owed_dates_actual_history_each_parent_completed(self):self.completion(second=True,replan=True)
    def test_after_book_peer_and_new_book_crash_exact_history_recovery(self):self.completion(second=True,replan=True,crash=True)
    def completion(self,*,race=None,second=False,replan=False,crash=False):
        from types import SimpleNamespace
        import hashlib
        from packages.application import owned_history_worker as supervisor,owned_history_native_ack as ack
        from packages.application.web_vitrina_snapshot_admission import ApiJobMarkers
        from packages.application.web_vitrina_history_store import HistoryStore
        from packages.application.own_product_capital import OwnProductCapitalBlock
        from packages.application.calculation_parameters import CalculationParametersBlock
        from packages.application.calculation_parameters_v4 import ProxyV4ParametersBlock
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        from apps.wb_finance_weekly_cost_cutover_smoke import _row
        class Fixture(Tests):SECOND_SOURCE=second
        case=Fixture();case.setUp();self.addCleanup(case.doCleanups)
        rt=case.runtime
        OwnProductCapitalBlock(runtime=rt)
        legacy=CalculationParametersBlock(runtime=rt);legacy.ensure_initial_version(created_at=NOW,created_by='synthetic supplier fixture')
        ProxyV4ParametersBlock(runtime=rt,now_factory=lambda:MOMENT)
        second_request='second-historical-cost'
        old=case.prepare()
        if crash:
            def stop(point):
                if point=='after_book':raise RuntimeError('old supplier after book')
            with self.assertRaisesRegex(RuntimeError,'old supplier'):case.publish(old,inject=stop)
        else:receipt=case.publish(old)
        if replan:
            fresh,case.second_identity,second_request=ReplanTests.followup(case,old)
            if crash:
                def stop_new(point):
                    if point=='before_retirement':raise RuntimeError('new supplier before retirement')
                with self.assertRaisesRegex(RuntimeError,'new supplier'):case.publish(fresh,inject=stop_new)
            receipt=case.publish(fresh)
            self.assertEqual(set(receipt.dates),set(old['candidate']['effect_dates']))
        finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT);finance.ensure_schema()
        finance.ingest_week(date(2026,7,20),date(2026,7,26),[_row(1,DAY,nm_id=1)])
        # Real cost proof still refuses to call this completed before History.
        from packages.application.operator_supplier_cost_proof import read_native_proof
        with self.assertRaisesRegex(ValueError,'history_exact_ack_pending'):read_native_proof(rt,case.identity,now=MOMENT)
        with TemporaryDirectory(prefix='supplier-supervisor-') as raw,closing(sqlite3.connect(rt.db_path)) as keeper:
            keeper.execute('PRAGMA journal_mode=WAL');keeper.commit();keeper.execute('SELECT 1 FROM sqlite_master').fetchone()
            root=Path(raw)/'candidate';root.mkdir(mode=0o700)
            for name in ('.web-vitrina-finished-builder.lock','.wb-finance-daily-worker.lock'):(rt.runtime_dir/name).touch(mode=0o600)
            markers=ApiJobMarkers(rt.runtime_dir);marker=markers.start('supplier-history-fixture','cycle');cycle={**markers.owner,'job_id':'supplier-history-fixture','operation':'cycle'}
            self.addCleanup(lambda:markers.finish(marker))
            contract=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json').read_text())
            contract['candidate_root']=str(root);contract['formula_code_hashes']={relative:hashlib.sha256((ROOT/relative).read_bytes()).hexdigest() for relative in contract['formula_code_hashes']}
            contract['formula_epoch']='wbc0069k16-reviewed-native-v1:'+hashlib.sha256(json.dumps(contract['formula_code_hashes'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
            path=root/'contract.json';path.write_text(json.dumps(contract))
            config=SimpleNamespace(candidate_root=root,runtime_contract=path,formula_epoch=contract['formula_epoch'],budget_seconds=120,max_recomputes=4)
            backfill=tuple(day for day in receipt.dates if day<'2026-07-23')
            with heavy_admitted(rt.runtime_dir,operation='cycle'),patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'),patch('apps.web_vitrina_finished_snapshot_build.systemd_admission',return_value='idle'):
                with supervisor.owned_history_worker(runtime=rt,config=config,cycle_owner=cycle) as worker:
                    invoke=worker._invoke
                    def diagnostic(mode,*args):
                        result=invoke(mode,*args)
                        if result.get('result',{}).get('status')=='failed':raise AssertionError('actual supplier History '+mode+' failed: '+str(result['result']))
                        return result
                    consume=ack.consume_supplier_history_ack
                    def consume_then_race(*args,**kwargs):
                        native=consume(*args,**kwargs)
                        if race:
                            with _connect(rt.db_path) as conn:
                                if race=='source':conn.execute("UPDATE sheet_vitrina_v1_supplier_financial_expense_lines SET amount_rub=999 WHERE line_id='extra-line'")
                                else:
                                    row=conn.execute('SELECT bundle_version,as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots LIMIT 1').fetchone();payload=json.loads(row[2]);payload['sheets'][0]['rows'][0][2]=99999
                                    conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE bundle_version=? AND as_of_date=?',(canonical(payload),row[0],row[1]))
                                conn.commit()
                        return native
                    with patch.object(worker,'_invoke',side_effect=diagnostic),patch.object(ack,'consume_supplier_history_ack',side_effect=consume_then_race):
                        if race:
                            with self.assertRaisesRegex(Exception,'ready_repair_source_changed|query_input_changed|dated_ready_changed|native_source_changed'):worker.complete(MOMENT,backfill_dates=backfill,supplier_receipt=receipt,total_seconds=300)
                        else:result=worker.complete(MOMENT,backfill_dates=backfill,supplier_receipt=receipt,total_seconds=300)
                    self.assertIsNone(worker._process)
                    if not race:self.assertTrue(result['supplier_ack'])
            if race:
                self.assertNotIn('supplier_history_ack',receipt._read()[1]);return
            with ready.readonly(rt.db_path) as conn:proof=h.completed_proof(conn,receipt.manifest['source_ref'])
            self.assertEqual(set(proof['ack']['native']),set(receipt.dates))
            edition=HistoryStore(root/'history').edition();self.assertTrue(all(proof['ack']['native'][d]['object_digest']==edition['days'][d] for d in receipt.dates))
            # Existing Finance cost consumer, then its actual independent proof.
            with heavy_admitted(rt.runtime_dir,operation='cycle'),warehouse_functional_job_lock(rt.runtime_dir):
                finance.recalculate_stale_cost_weeks()
                self.assertTrue(processing.record_completion(rt,case.identity,now=MOMENT)['complete'])
                if second:self.assertTrue(processing.record_completion(rt,case.second_identity,now=MOMENT)['complete'])
            saved=financial.read_request(rt.runtime_dir,rt.db_path,'financial-historical-cost',request_scope='alice-key')['results'][0]['acceptance']
            self.assertTrue(saved['processing']['complete']);self.assertEqual(saved['state'],'completed')
            if second:
                saved=financial.read_request(rt.runtime_dir,rt.db_path,second_request,request_scope='alice-key')['results'][0]['acceptance']
                self.assertTrue(saved['processing']['complete']);self.assertEqual(saved['state'],'completed')


if __name__=='__main__':unittest.main()
