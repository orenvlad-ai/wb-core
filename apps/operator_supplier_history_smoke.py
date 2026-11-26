"""Disposable native supplier cost, dated stages/book/ready and exact authority."""
from contextlib import closing,contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,date,timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import json,sqlite3,sys,unittest,weakref
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


def install_native_saved_dates(rt,*,long_wb_money=False):
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
        if long_wb_money:
            from decimal import localcontext
            with localcontext() as context:
                context.prec=28  # Existing published WB WAC serialization contract.
                for balance in balances:
                    if balance['warehouse_key']=='wb':
                        balance['capital_rub']='35673.01176443006162885432398'
                        balance['wac_rub']=str(Decimal(balance['capital_rub'])/Decimal(balance['quantity']))
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
        self.before=install_native_saved_dates(rt,long_wb_money=getattr(self,'LONG_WB_MONEY',False))
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
    def test_new_native_acceptance_after_prepare_refuses_empty_receipt_fence(self):
        manifest=self.prepare();before=accounting.load(self.runtime.runtime_dir)[1]
        self.assertEqual(manifest['candidate']['members'][0]['receipt_dependency']['kind'],'unaccepted')
        self.runtime.create_ff_stock_operation(operation_id='LOCAL later acceptance',operation_type='receipt',
            source_type='supplier_shipment',source_key='LOCAL actual receipt',source_object_id='source',
            created_at=NOW,business_effective_date=DAY,lines=[dict(nm_id=1,quantity_delta=1)])
        with ready.readonly(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(ready.ReadyPublicationConflict,'ready_repair_source_changed'):
                stages.check_query_fence(conn,manifest['candidate']['source_inputs'])
        with self.assertRaises(ValueError):self.publish(manifest)
        self.assertEqual(accounting.load(self.runtime.runtime_dir)[1],before)

    def test_acceptance_header_after_prepare_refuses_source_fence(self):
        manifest=self.prepare();before=accounting.load(self.runtime.runtime_dir)[1]
        with _connect(self.runtime.db_path) as conn:
            conn.execute('UPDATE sheet_vitrina_v1_supplier_shipments SET actual_ff_acceptance_date=? WHERE shipment_id=?',(DAY,'source'));conn.commit()
        with ready.readonly(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(ready.ReadyPublicationConflict,'ready_repair_source_changed'):
                stages.check_query_fence(conn,manifest['candidate']['source_inputs'])
        with self.assertRaises(ValueError):self.publish(manifest)
        self.assertEqual(accounting.load(self.runtime.runtime_dir)[1],before)

    def initial_admission_manifest(self):
        rt=self.runtime;ledger=CnyLedgerBlock(runtime=rt,timestamp_factory=lambda:NOW)
        ledger.create_opening_balance({'operation_date':'2026-07-04','cny_amount':100,'rub_value':1000})
        header={**HEADER,'shipment_id':'new-admission','created_at':NOW,'updated_at':NOW,'invoice_date':FIRST,
            'shipment_date':FIRST,'actual_shipment_date':'','actual_ff_acceptance_date':'','invoice_no':'late-native-create','expenses_complete':0}
        lines=[{**LINES[0],'line_id':'new-product','internal_nm_id':1}]
        accepted=source.execute(rt,action='create',payload={'request_id':'late-native-create'},shipment_id='new-admission',
            actor='alice',request_scope='alice-key',write=lambda context:rt.save_supplier_shipment(header=header,lines=lines,operator_request=context))
        identity=accepted['acceptance']['operation_id']
        _save_payment(rt,'new-payment','new-admission',FIRST+'T12:00:00Z','100');ledger.replay_ledger(reason='late-native-create')
        intents.drain_supplier_preparation_intents(rt,shipment_ids=['source','new-admission']);publish_functional(rt);publish_accounting(rt)
        return h.prepare(rt,identity,now=MOMENT)

    def test_native_late_create_materializes_initial_source_and_companions(self):
        rt=self.runtime;manifest=self.initial_admission_manifest()
        admissions=[(day,p['proof']) for day,e in manifest['candidate']['editions'].items()
            for p in e['source_dependency']['supplier'] if p['proof']['kind']=='native_initial_pre_ff_supplier_admission']
        self.assertTrue(admissions)
        for day,proof in admissions:
            edition=manifest['candidate']['editions'][day]
            self.assertEqual(proof['source_ref']['action'],'create');self.assertEqual(proof['quantity_by_nm'],{'1':'10'})
            state=next(r for r in edition['companions']['supplier_cost_states'] if r['shipment_id']=='new-admission')
            self.assertEqual(state['version_id'],edition['version_id'])
            retained=manifest['candidate']['candidate_book']['retained_days'][day]['rows']['1']['stages']['PRODUCTION']
            self.assertEqual(Decimal(str(retained['quantity'])),10);self.assertEqual(Decimal(str(retained['capital_rub'])),1000)
            before_wb=[{k:v for k,v in r.items() if k!='version_id'} for r in edition['saved']['balances'] if r['warehouse_key']=='wb']
            after_wb=[{k:v for k,v in r.items() if k!='version_id'} for r in edition['balances'] if r['warehouse_key']=='wb']
            self.assertEqual(after_wb,before_wb)
            if getattr(self,'LONG_WB_MONEY',False):
                self.assertEqual(after_wb[0]['capital_rub'],'35673.01176443006162885432398')
        receipt=self.publish(manifest);self.assertTrue(receipt.validate_sources_readonly(now=MOMENT)['dated'])
        with ready.readonly(rt.db_path) as conn:
            for day,_ in admissions:
                version=manifest['candidate']['editions'][day]['version_id']
                self.assertEqual(conn.execute('SELECT count(*) FROM '+stages.P+'supplier_cost_states WHERE version_id=? AND shipment_id=?',(version,'new-admission')).fetchone()[0],1)
            stages.capture_dated_stages(conn,db_path=rt.db_path,book=manifest['candidate']['candidate_book'],dates=manifest['candidate']['effect_dates'],read_model_mode=stages.READ_MODEL_PROOFS)

    def test_native_decimal_parameters_json_proof_rebuild_and_exact_fence(self):
        from packages.application.calculation_parameters import ensure_calculation_parameters_schema,DEFAULT_PROXY_PARAMETERS,PROXY_BLOCK_KEY
        from packages.application.calculation_parameters_v4 import ensure_proxy_v4_schema,PROXY_V4_BLOCK_KEY,AUTOMATIC_RATE_FIELDS,PROXY_V4_FORMULA_VERSION
        with _connect(self.runtime.db_path) as conn:
            ensure_calculation_parameters_schema(conn);ensure_proxy_v4_schema(conn)
            conn.execute('INSERT INTO sheet_vitrina_v1_calculation_parameter_versions VALUES(?,?,?,?,?,?,?,?,?)',
                ('source-proxy3',PROXY_BLOCK_KEY,999,FIRST,canonical(DEFAULT_PROXY_PARAMETERS.public()),'fixture3','fixture','fixture',NOW))
            values={field:'0' for field in AUTOMATIC_RATE_FIELDS}
            values.update(buyout_rate='0.91',tax_rate='0.06',agent_remuneration_rate='0.38',source_week_ranges=[['2026-06-22','2026-06-28']],
                buyout_order_count_weight='123.4500',finance_net_revenue_weight='456.7800',formula_version=PROXY_V4_FORMULA_VERSION)
            conn.execute('INSERT INTO sheet_vitrina_v1_proxy_v4_parameter_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('source-proxy4',PROXY_V4_BLOCK_KEY,999,FIRST,'2026-06-22','2026-06-28','fixture-window',canonical(values),'fixture4','fixture','fixture',NOW));conn.commit()
        manifest=self.prepare();proof=manifest['parameter_proof'][FIRST]
        self.assertEqual(proof[0]['buyout_rate'],'0.91');self.assertEqual(proof[1]['buyout_order_count_weight'],'123.4500')
        self.assertEqual(proof[1]['source_week_ranges'],[['2026-06-22','2026-06-28']])
        self.assertEqual(manifest,json.loads(canonical(manifest)))
        self.assertEqual(h._large_fingerprint(manifest),fingerprint(manifest))
        self.assertEqual(h._publication_json_size(manifest),len(canonical(manifest).encode()))
        targets,parameters=h._targets(self.runtime,manifest['candidate'],now=MOMENT)
        self.assertEqual(targets,manifest['targets']);self.assertEqual(parameters,manifest['parameter_proof'])
        with ready.readonly(self.runtime.db_path) as conn:h._fence(conn,manifest)
        values['buyout_order_count_weight']='123.4501'
        with _connect(self.runtime.db_path) as conn:
            conn.execute('UPDATE sheet_vitrina_v1_proxy_v4_parameter_versions SET parameters_json=? WHERE version_id=?',(canonical(values),'source-proxy4'));conn.commit()
        with ready.readonly(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(ValueError,'dated_parameters_changed'):h._fence(conn,manifest)
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
    def test_legacy_payload_ghost_recovers_into_validated_model_proofs(self):
        case=self.fixture();capture=stages.capture_dated_stages
        def legacy(*args,**kwargs):
            kwargs['read_model_mode']='payloads'
            return capture(*args,**kwargs)
        def crash(point):
            if point=='after_book':raise RuntimeError('legacy own book crash')
        with patch.object(stages,'capture_dated_stages',side_effect=legacy):
            old=case.prepare()
            with self.assertRaisesRegex(RuntimeError,'legacy own'):case.publish(old,inject=crash)
        self.assertTrue(all('read_models' in e['saved'] for e in old['candidate']['editions'].values()))
        new,_,_=self.followup(case,old)
        self.assertEqual(len(new['candidate']['recovery_inputs']),19)
        self.assertTrue(all('read_model_proofs' in e['saved'] for e in new['candidate']['editions'].values()))
        for day,edition in new['candidate']['editions'].items():
            self.assertEqual(edition['saved']['balances'],old['candidate']['editions'][day]['saved']['balances'])
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
            with patch.object(processing,'captured_cohort',return_value=['old','valid']),patch.object(processing,'operation',side_effect=lambda conn,key:{'operation_id':key,'action':'edit'}),patch.object(processing,'current',return_value=True),patch.object(processing,'ref',side_effect=lambda op:ref if op['operation_id']=='old' else {'operation_id':'valid'}),patch.object(h,'publication_for',side_effect=lambda conn,source:prior if source['operation_id']=='old' else None),patch.object(h,'completed_proof',return_value=None),patch.object(h,'_checked') as checked,patch.object(candidate,'prepare',return_value={'effect_dates':[FIRST]}),patch.object(h,'_prepare_publication',return_value={'new':'exact'}),patch.object(h,'publish',return_value=receipt):
                self.assertIs(h.pending(runtime,now=MOMENT),receipt)
            self.assertEqual(checked.call_args.args[2],'supplier_history_formula_changed')
            self.assertEqual(checked.call_args.kwargs['state'],'needs_attention')

    def test_exact_queue_pending_keeps_history_fairness_and_unknown_errors_propagate(self):
        from types import SimpleNamespace
        with TemporaryDirectory(prefix='supplier-history-native-queue-') as raw:
            runtime=SimpleNamespace(db_path=Path(raw)/'source.sqlite')
            with sqlite3.connect(runtime.db_path) as conn:
                processing.ensure_schema(conn);ready.ensure_publication_schema(conn)
                conn.execute(f'CREATE TABLE {processing.QUEUE}(queue_id,stable_source_id,source_revision,affected_nm_ids_json,effective_date,status,error)')
            op=dict(operation_id='waiting',shipment_id='shipment',action='edit',domain='supplier_shipment',revision=1,source_digest='saved',
                intent=dict(revision=1,source_fingerprint='native',affected_nm_ids_json='[1]',effective_date=FIRST))
            receipt=object()
            def numerical(rt,identity,*,now):
                if identity=='valid':return {'effect_dates':[FIRST]}
                with ready.readonly(rt.db_path) as conn:return candidate.native_cohort(conn,identity)
            with patch.object(processing,'captured_cohort',return_value=['waiting','valid']),patch.object(processing,'operation',side_effect=lambda conn,key:{**op,'operation_id':key}),patch.object(processing,'current',return_value=True),patch.object(processing,'superseded',return_value=False),patch.object(candidate,'prepare',side_effect=numerical),patch.object(h,'_prepare_publication',return_value={'new':'exact'}),patch.object(h,'publish',return_value=receipt):
                self.assertIs(h.pending(runtime,now=MOMENT),receipt)
                with sqlite3.connect(runtime.db_path) as conn:
                    self.assertEqual(conn.execute(f'SELECT state,reason FROM {processing.ATTEMPTS}').fetchall(),[('processing','supplier_exact_queue_pending')])
                    conn.execute(f'INSERT INTO {processing.QUEUE} VALUES(?,?,?,?,?,?,?)',('q','supplier_shipment:shipment','supplier-preparation:1:native','[1]',FIRST,'pending',''))
                self.assertIs(h.pending(runtime,now=MOMENT),receipt)
                with sqlite3.connect(runtime.db_path) as conn:
                    self.assertEqual(conn.execute(f'SELECT state,reason FROM {processing.ATTEMPTS}').fetchall(),[('processing','supplier_exact_functional_publication_pending')])
                with patch.object(processing,'queue_for',side_effect=ValueError('supplier_unexpected_queue_corruption')):
                    with self.assertRaisesRegex(ValueError,'supplier_unexpected_queue_corruption'):h.pending(runtime,now=MOMENT)
                with sqlite3.connect(runtime.db_path) as conn:
                    self.assertEqual(conn.execute(f'SELECT state,reason FROM {processing.ATTEMPTS}').fetchall(),[('processing','supplier_exact_functional_publication_pending')])


class PendingTransactionTests(unittest.TestCase):
    """Real native readers/attempt writer on disposable DELETE, not WAL seams.

    Minimal relational operands exercise the native decision/ACK readers. The
    seeded publication is synthetic; these tests claim no monetary execution.
    """
    def fixture(self,kind='source',*,mode='DELETE'):
        from types import SimpleNamespace
        from packages.application import operator_supplier_factual_dates as factual
        from packages.application.supplier_shipment_factual_correction import _ensure_correction_schema
        from packages.application.warehouse_targeted_replay import TARGETED_PUBLICATION_TABLE
        tmp=TemporaryDirectory(prefix='supplier-pending-delete-');self.addCleanup(tmp.cleanup)
        rt=SimpleNamespace(db_path=Path(tmp.name)/'operational.sqlite3',runtime_dir=Path(tmp.name))
        with closing(sqlite3.connect(rt.db_path)) as conn,conn:
            conn.row_factory=sqlite3.Row
            self.assertEqual(conn.execute('PRAGMA journal_mode='+mode).fetchone()[0],mode.lower())
            source.ensure_schema(conn);intents.ensure_schema(conn);ready.ensure_publication_schema(conn)
            conn.execute('CREATE TABLE sheet_vitrina_v1_supplier_shipments(shipment_id TEXT PRIMARY KEY,invoice_date TEXT)')
            conn.execute('CREATE TABLE sheet_vitrina_v1_supplier_shipment_lines(line_id TEXT,shipment_id TEXT)')
            conn.execute('CREATE TABLE sheet_vitrina_v1_supplier_financial_documents(document_id TEXT,supplier_order_id TEXT,document_type TEXT)')
            conn.execute('CREATE TABLE sheet_vitrina_v1_supplier_financial_expense_lines(line_id TEXT,supplier_order_id TEXT,financial_document_id TEXT)')
            conn.execute('CREATE TABLE registry_upload_current_state(value TEXT)')
            conn.execute('CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version TEXT,as_of_date TEXT,plan_json TEXT)')
            conn.execute('INSERT INTO sheet_vitrina_v1_supplier_shipments VALUES(?,?)',('local',FIRST))
            captured=intents.capture_source(conn,'local');digest=intents._fingerprint(captured)
            conn.execute(f'INSERT INTO {intents.TABLE}(shipment_id,revision,source_fingerprint,affected_nm_ids_json,effective_date,document_ids_json,status,requested_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',('local',1,digest,'[1]',FIRST,'[]','delivered',NOW,NOW))
            intent=dict(conn.execute(f'SELECT * FROM {intents.TABLE}').fetchone())
            saved={'preparation_source':captured};identity='local-operation'
            if kind=='factual':
                factual.ensure_schema(conn);_ensure_correction_schema(conn)
                conn.execute(f'CREATE TABLE {TARGETED_PUBLICATION_TABLE}(publication_id TEXT PRIMARY KEY,status TEXT,version_id TEXT,plan_fingerprint TEXT)')
                conn.execute(f'INSERT INTO {TARGETED_PUBLICATION_TABLE} VALUES(?,?,?,?)',('local-publication','complete','local-version','local-plan'))
                conn.execute(f'INSERT INTO {factual.JOB}(correction_id,request_fingerprint,shipment_id,actor,source,reason,status,phase,progress_text,requested_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(identity,'local-request','local','alice','fixture','fixture','success','complete','fixture',NOW,NOW))
                request=dict(request_scope='alice',request_id='local-request',shipment_id='local',confirmation_token='fixture',payload_digest='fixture',wire_digest='',actor='alice',accepted_at=NOW,status='accepted',reason='',correction_id=identity,request_fingerprint='local-request',source_digest='before',source_json='{}')
                factual._insert(conn,request)
                proof=dict(correction_id=identity,request_fingerprint='local-request',source_before_digest='before',source_after={'supplier':saved},version_id='local-version',plan_fingerprint='local-plan',publication_id='local-publication',preparation_intent=intent)
                conn.execute(f'INSERT INTO {factual.APPLIED} VALUES(?,?,?,?,?)',(identity,'local-version','local-plan',source.digest(proof),canonical(proof)))
            else:
                conn.execute(f'INSERT INTO {source.TABLE}(operation_id,request_scope,request_id,action,payload_digest,shipment_id,revision,source_digest,source_json,accepted_at,actor,intent_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(identity,'alice','local-request','edit','fixture','local',1,source.digest(saved),canonical(saved),NOW,'alice',canonical(intent)))
            op=processing.operation(conn,identity)
            if kind=='source':conn.execute(f'UPDATE {intents.TABLE} SET source_fingerprint=?',('unknown-source',))
            if kind=='finance':
                numerical=dict(code_authority=candidate.code_authority(),effect_dates=[FIRST],functional={'business_date':FIRST},candidate_book={},cohort_refs=[processing.ref(op)],editions={FIRST:{'version_id':'local-version'}})
                manifest=dict(contract=h.CONTRACT,storage_encoding=h.READY_JSON_ENCODING,operation_id='supplier-history:'+identity,attempt_id='local-attempt',source_ref=processing.ref(op),candidate=numerical,targets=[{'dates':[FIRST],'expected':asdict(ready.ExpectedReady('local-bundle',FIRST,None)),'after_json':h._pack_ready_json('{}')}],after_book=fingerprint({}))
                manifest['manifest_digest']=fingerprint(manifest)
                ack={'manifest_digest':manifest['manifest_digest'],'native':{FIRST:{'object_digest':'synthetic-object','edition_id':'synthetic-edition','functional_version':'local-version'}}}
                ready.record_intent(conn,operation_id=manifest['operation_id'],attempt_id=manifest['attempt_id'],kind=h.CONTRACT,expected=None,inputs=manifest,expected_book='synthetic-before',book_required=True,ready_required=True,created_at=NOW)
                ready.complete_publication(conn,operation_id=manifest['operation_id'],attempt_id=manifest['attempt_id'],book_version=manifest['after_book'],after_digest=ready.digest('{}'),finished_at=NOW)
                conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=?',(canonical({'supplier_history_ack':ack}),))
        return rt,identity,digest
    def image(self,rt):
        with closing(sqlite3.connect(rt.db_path)) as conn:
            return {name:list(conn.execute('SELECT * FROM "'+name+'" ORDER BY rowid')) for name, in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>? ORDER BY name",(processing.ATTEMPTS,))}
    def attempts(self,rt):
        with closing(sqlite3.connect(rt.db_path)) as conn:return list(conn.execute(f'SELECT * FROM {processing.ATTEMPTS}'))
    def invoke(self,rt,*,gap=lambda conn:None):
        native=ready.readonly;readers=[];after_gap=[]
        @contextmanager
        def observed(path):
            with native(path) as conn:
                readers.append(conn);self.assertTrue(conn.in_transaction);self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0],1)
                yield conn
            with self.assertRaises(sqlite3.ProgrammingError):conn.execute('SELECT 1')
            if len(readers)==2:
                with closing(sqlite3.connect(rt.db_path)) as writer,writer:
                    writer.row_factory=sqlite3.Row;writer.execute('BEGIN IMMEDIATE');gap(writer)
                after_gap.append(self.image(rt))
        try:
            with patch.object(ready,'readonly',side_effect=observed):return h.pending(rt,now=MOMENT)
        finally:
            self.assertEqual(len(readers),2)
            if after_gap:self.assertEqual(self.image(rt),after_gap[0])
    def test_delete_all_three_early_branches_close_physical_reader(self):
        for kind,reason in [('source','native_source_pending'),('factual','native_factual_owner_pending'),('finance','finance_pending')]:
            with self.subTest(kind=kind):
                rt,identity,_=self.fixture(kind);before=self.image(rt)
                self.assertIsNone(self.invoke(rt))
                self.assertEqual(self.attempts(rt),[(identity,'processing','supplier_history_'+reason,MOMENT.isoformat())])
                self.assertEqual(self.image(rt),before)
    def test_wal_control_still_uses_real_attempt_writer(self):
        rt,identity,_=self.fixture(mode='WAL');self.assertIsNone(self.invoke(rt));self.assertEqual(len(self.attempts(rt)),1)
    def test_external_delete_reader_exhaustion_propagates_without_attempt(self):
        rt,identity,_=self.fixture()
        from packages.application.sqlite_contention import connect_sqlite,SQLiteContentionExhausted
        with ready.readonly(rt.db_path) as external,patch.object(h,'connect_sqlite',side_effect=lambda path:connect_sqlite(path,timeout_ms=350)):
            external.execute('SELECT * FROM '+source.TABLE).fetchall()
            with self.assertRaises(SQLiteContentionExhausted) as caught:h.pending(rt,now=MOMENT)
            self.assertEqual(caught.exception.phase,'commit')
            self.assertTrue(external.in_transaction);self.assertEqual(self.attempts(rt),[])
        self.assertIsNone(self.invoke(rt));self.assertEqual(len(self.attempts(rt)),1)
    def test_current_and_monotonic_successor_gap_refuse_stale_reason(self):
        for successor in (False,True):
            with self.subTest(successor=successor):
                rt,_,digest=self.fixture()
                def gap(conn):conn.execute(f'UPDATE {intents.TABLE} SET source_fingerprint=?,revision=?',(digest,2 if successor else 1))
                with self.assertRaisesRegex(h.SupplierHistoryCheckConflict,'pending_check_changed'):self.invoke(rt,gap=gap)
                self.assertEqual(self.attempts(rt),[])
    def test_already_proven_supersession_preserves_native_priority(self):
        rt,identity,digest=self.fixture()
        with closing(sqlite3.connect(rt.db_path)) as conn,conn:conn.execute(f'UPDATE {intents.TABLE} SET source_fingerprint=?,revision=2',(digest,))
        self.assertIsNone(self.invoke(rt));self.assertEqual(self.attempts(rt)[0][1:3],('needs_attention',processing.SUPERSEDED))
        with ready.readonly(rt.db_path) as conn:self.assertEqual(processing.captured_cohort(conn),[])
    def test_completion_arriving_after_reader_prevents_processing_stamp(self):
        rt,identity,_=self.fixture()
        def gap(conn):conn.execute(f'INSERT INTO {processing.COMPLETIONS} VALUES(?,?,?,?)',(identity,'{}','fixture-completion',NOW))
        with self.assertRaisesRegex(h.SupplierHistoryCheckConflict,'pending_check_completed'):self.invoke(rt,gap=gap)
        self.assertEqual(self.attempts(rt),[])
    def test_finance_publication_and_exact_ack_gap_never_write_old_reason(self):
        for drift in ('missing','prepared','ack','corrupt','retired'):
            with self.subTest(drift=drift):
                rt,_,_=self.fixture('finance')
                def gap(conn):
                    if drift=='missing':conn.execute('DELETE FROM sheet_vitrina_v1_ready_publications')
                    elif drift=='prepared':conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET state='prepared'")
                    else:
                        row=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications').fetchone();d=json.loads(row[0])
                        if drift=='ack':d['supplier_history_ack']['native'][FIRST]['edition_id']='different-native-edition'
                        elif drift=='corrupt':d['supplier_history_ack']={}
                        else:d['supplier_history_replaced_by']={'foreign':'unproved'}
                        conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=?',(canonical(d),))
                with self.assertRaises(ValueError):self.invoke(rt,gap=gap)
                self.assertEqual(self.attempts(rt),[])
    def test_immutable_ref_or_source_port_drift_never_falls_back_to_checked(self):
        for field in ('revision','source_digest','saved_source'):
            with self.subTest(field=field):
                rt,_,_=self.fixture();native=processing.operation
                def operation(conn,key):
                    op=native(conn,key)
                    if not conn.execute('PRAGMA query_only').fetchone()[0]:
                        op[field]={'unknown':'source'} if field=='saved_source' else 'foreign-ref'
                    return op
                with patch.object(processing,'operation',side_effect=operation),self.assertRaises(ValueError):self.invoke(rt)
                self.assertEqual(self.attempts(rt),[])
    def test_borrowed_delete_outer_pin_survives_rejection(self):
        from packages.application.web_vitrina_window_read_context import window_read_context,active_window_read_context
        rt,identity,_=self.fixture();before=self.image(rt)
        with window_read_context(rt.db_path) as outer:
            reader=outer.borrow(rt.db_path);physical=outer._physical
            with self.assertRaisesRegex(h.SupplierHistoryCheckConflict,'borrowed_read_context'):h.pending(rt,now=MOMENT)
            with self.assertRaisesRegex(h.SupplierHistoryCheckConflict,'borrowed_read_context'):h._checked(rt,identity,'fixture',MOMENT)
            self.assertIs(active_window_read_context(),outer);self.assertIs(outer._physical,physical);self.assertTrue(reader.in_transaction)
            self.assertEqual(reader.execute('SELECT COUNT(*) FROM '+processing.ATTEMPTS).fetchone()[0],0)
            self.assertEqual(reader.execute('PRAGMA query_only').fetchone()[0],1)
        with self.assertRaises(sqlite3.ProgrammingError):physical.execute('SELECT 1')
        self.assertEqual(self.image(rt),before);self.assertIsNone(self.invoke(rt));self.assertEqual(len(self.attempts(rt)),1)
    def test_native_cohort_limit_and_oldest_attempt_fairness_unchanged(self):
        rt,identity,_=self.fixture('factual')
        with closing(sqlite3.connect(rt.db_path)) as conn,conn:
            conn.row_factory=sqlite3.Row
            # Native shipment receipts provide independent admitted cohort IDs.
            saved={'preparation_source':intents.capture_source(conn,'local')};intent=dict(conn.execute(f'SELECT * FROM {intents.TABLE}').fetchone())
            for n in range(processing.COHORT_LIMIT+2):
                conn.execute(f'INSERT INTO {source.TABLE}(operation_id,request_scope,request_id,action,payload_digest,shipment_id,revision,source_digest,source_json,accepted_at,actor,intent_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('other-'+str(n),'alice','request-'+str(n),'edit','fixture','local',n+2,source.digest(saved),canonical(saved),NOW,'alice',canonical(intent)))
            conn.row_factory=sqlite3.Row;before=processing.captured_cohort(conn)
        self.assertEqual(len(before),processing.COHORT_LIMIT);self.assertIn(identity,before)
        # Each nonfactual operation is pending rather than a numerical candidate.
        with closing(sqlite3.connect(rt.db_path)) as conn,conn:conn.execute(f"UPDATE {intents.TABLE} SET source_fingerprint='unknown-source'")
        self.assertIsNone(h.pending(rt,now=MOMENT+timedelta(seconds=1)));self.assertEqual(len(self.attempts(rt)),processing.COHORT_LIMIT)
        with ready.readonly(rt.db_path) as conn:after=processing.captured_cohort(conn)
        self.assertEqual(len(after),processing.COHORT_LIMIT);self.assertNotEqual(before,after)


@unittest.skipUnless(sys.platform=='linux','actual kernel History owner descriptors required')
class LinuxTests(unittest.TestCase):
    def test_native_source_dated_history_finance_exact_completed(self):self.completion()
    def test_source_race_after_consume_rejects_operator_complete(self):self.completion(race='source')
    def test_ready_race_after_consume_rejects_operator_complete(self):self.completion(race='ready')
    def test_native_two_source_cohort_history_finance_each_exact_completed(self):self.completion(second=True)
    def test_followup_owed_dates_actual_history_each_parent_completed(self):self.completion(second=True,replan=True)
    def test_after_book_peer_and_new_book_crash_exact_history_recovery(self):self.completion(second=True,replan=True,crash=True)
    def test_unchanged_money_ghost_new_native_generation_gets_exact_history_ack(self):self.completion(crash=True,unchanged=True)
    def completion(self,*,race=None,second=False,replan=False,crash=False,unchanged=False):
        moment=MOMENT
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
        ProxyV4ParametersBlock(runtime=rt,now_factory=lambda:moment)
        second_request='second-historical-cost'
        old=case.prepare()
        if crash:
            def stop(point):
                if point=='after_book':raise RuntimeError('old supplier after book')
            with self.assertRaisesRegex(RuntimeError,'old supplier'):case.publish(old,inject=stop)
        else:receipt=case.publish(old)
        if unchanged:
            # A fresh native version, within the fixture's official stock age.
            moment+=timedelta(seconds=10);SupplierNativeEditionIdentityTests.next_native(case,moment)
            prepared=accounting.prepare(rt.runtime_dir,now=moment)
            save(rt,rt.load_sheet_vitrina_ready_snapshot(),prepared=prepared,now=moment)
            fresh=h.prepare(rt,case.identity,now=moment);receipt=case.publish(fresh)
            self.assertEqual(set(receipt.dates),set(old['candidate']['effect_dates']))
            self.assertTrue(all(r['ghost_version']!=fresh['candidate']['editions'][r['day']]['version_id']
                for r in fresh['candidate']['recovery_inputs']))
        if replan:
            fresh,case.second_identity,second_request=ReplanTests.followup(case,old)
            if crash:
                def stop_new(point):
                    if point=='before_retirement':raise RuntimeError('new supplier before retirement')
                with self.assertRaisesRegex(RuntimeError,'new supplier'):case.publish(fresh,inject=stop_new)
            receipt=case.publish(fresh)
            self.assertEqual(set(receipt.dates),set(old['candidate']['effect_dates']))
        finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:moment);finance.ensure_schema()
        finance.ingest_week(date(2026,7,20),date(2026,7,26),[_row(1,DAY,nm_id=1)])
        # Real cost proof still refuses to call this completed before History.
        from packages.application.operator_supplier_cost_proof import read_native_proof
        with self.assertRaisesRegex(ValueError,'history_exact_ack_pending'):read_native_proof(rt,case.identity,now=moment)
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
                    phase_grants = []
                    original_send = supervisor.send_message

                    def record_grant(channel, message):
                        if "inner_deadline" in message:
                            phase_grants.append((message["invocation"], message["phase"]))
                        return original_send(channel, message)

                    with (
                        patch.object(worker, "_invoke", side_effect=diagnostic),
                        patch.object(ack, "consume_supplier_history_ack", side_effect=consume_then_race),
                        patch.object(supervisor, "send_message", side_effect=record_grant),
                    ):
                        if race:
                            with self.assertRaisesRegex(Exception,'ready_repair_source_changed|query_input_changed|dated_ready_changed|native_source_changed'):worker.complete(moment,backfill_dates=backfill,supplier_receipt=receipt,total_seconds=300)
                        else:result=worker.complete(moment,backfill_dates=backfill,supplier_receipt=receipt,total_seconds=300)
                    self.assertIsNone(worker._process)
                    if not race:
                        self.assertTrue(result['supplier_ack'])
                        native_proofs = [inv for inv, phase in phase_grants if phase == "supplier_native_proof"]
                        self.assertEqual(len(native_proofs), 1)
                        dated = [inv for inv, phase in phase_grants if phase == "dated_write"]
                        self.assertTrue(dated)
                        for invocation in dated:
                            self.assertEqual([phase for inv, phase in phase_grants if inv == invocation],
                                             ["derived_work", "dated_write"])
                        self.assertEqual([phase for inv, phase in phase_grants if inv == native_proofs[0]],
                                         ["derived_work", "supplier_native_proof"])
            if race:
                self.assertNotIn('supplier_history_ack',receipt._read()[1]);return
            with ready.readonly(rt.db_path) as conn:proof=h.completed_proof(conn,receipt.manifest['source_ref'])
            self.assertEqual(set(proof['ack']['native']),set(receipt.dates))
            edition=HistoryStore(root/'history').edition();self.assertTrue(all(proof['ack']['native'][d]['object_digest']==edition['days'][d] for d in receipt.dates))
            # Existing Finance cost consumer, then its actual independent proof.
            with heavy_admitted(rt.runtime_dir,operation='cycle'),warehouse_functional_job_lock(rt.runtime_dir):
                finance.recalculate_stale_cost_weeks()
                self.assertTrue(processing.record_completion(rt,case.identity,now=moment)['complete'])
                if second:self.assertTrue(processing.record_completion(rt,case.second_identity,now=moment)['complete'])
            saved=financial.read_request(rt.runtime_dir,rt.db_path,'financial-historical-cost',request_scope='alice-key')['results'][0]['acceptance']
            self.assertTrue(saved['processing']['complete']);self.assertEqual(saved['state'],'completed')
            if second:
                saved=financial.read_request(rt.runtime_dir,rt.db_path,second_request,request_scope='alice-key')['results'][0]['acceptance']
                self.assertTrue(saved['processing']['complete']);self.assertEqual(saved['state'],'completed')


class InventoryTimestampRecoveryTests(unittest.TestCase):
    def test_same_prepared_attempt_recovers_activated_book_without_changing_intent(self):
        from packages.application import inventory_quantity
        from packages.application import sheet_vitrina_v1_inventory_history as history
        case=Tests();case.setUp();self.addCleanup(case.doCleanups)
        # This July source fixture predates the September rollout. Admit its
        # genuine native quantities for the test instead of skipping inventory.
        with patch.object(inventory_quantity,'EFFECTIVE_DATE',FIRST):
            manifest=case.prepare();encoded=canonical(manifest);authority=candidate.code_authority()
            self.assertNotIn('sheet_vitrina_v1_inventory_history.py',authority)
            inventory=[i for t in manifest['targets'] for i in t['inventory']]
            self.assertTrue(inventory);self.assertTrue(any(i['closed'] for i in inventory))
            self.assertTrue(all(i['captured_at'].endswith('+00:00') for i in inventory))
            # Reproduce the deployed writer's suffix failure, after the
            # immutable book commit and before the registry transaction commits.
            with patch.object(history,'_utc_storage_timestamp',side_effect=lambda value:value):
                with self.assertRaisesRegex(RuntimeError,'inventory history idempotent capture readback failed'):
                    case.publish(manifest)
            self.assertEqual(accounting.load(case.runtime.runtime_dir)[1],manifest['after_book'])
            with ready.readonly(case.runtime.db_path) as conn:
                saved=conn.execute('SELECT inputs_json,state FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                    (manifest['operation_id'],manifest['attempt_id'])).fetchone()
                self.assertEqual(saved['inputs_json'],encoded);self.assertEqual(saved['state'],'prepared')
            receipt=case.publish(json.loads(encoded))
            self.assertEqual(receipt.manifest,manifest);self.assertEqual(candidate.code_authority(),authority)
            self.assertEqual(canonical(manifest),encoded)
            with ready.readonly(case.runtime.db_path) as conn:
                saved=conn.execute('SELECT inputs_json,state FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                    (manifest['operation_id'],manifest['attempt_id'])).fetchone()
                self.assertEqual(saved['inputs_json'],encoded);self.assertEqual(saved['state'],'complete')
                for table,column in ((history.CAPTURES_TABLE,'captured_at'),(history.COMPONENTS_TABLE,'captured_at'),
                                     (history.FINALIZATIONS_TABLE,'finalized_at')):
                    stamps=[r[0] for r in conn.execute(f'SELECT DISTINCT {column} FROM {table}')]
                    self.assertTrue(stamps);self.assertTrue(all(s.endswith('Z') for s in stamps))
            self.assertEqual(case.publish(manifest).binding(),receipt.binding())

    def test_next_day_new_cohort_keeps_the_prepared_inventory_obligation(self):
        from packages.application import inventory_quantity
        helper=InitialAdmissionRecoveryTests();self.addCleanup(helper.doCleanups)
        with patch.object(inventory_quantity,'EFFECTIVE_DATE',FIRST):
            case,old=helper.fixture();encoded=canonical(old)
            self.assertTrue(any(t['inventory'] for t in old['targets']))
            new,tomorrow=helper.followup(case)
        self.assertNotEqual(new['attempt_id'],old['attempt_id'])
        self.assertNotEqual(new['cohort_refs'],old['candidate']['cohort_refs'])
        self.assertEqual(new['functional']['business_date'],tomorrow.date().isoformat())
        self.assertEqual(new['owed_dates'],old['candidate']['effect_dates'])
        matching = [item for item in new['obligations'] if item['attempt_id'] == old['attempt_id']]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['inputs_ref'], h.retained_inputs_ref(encoded))
        with ready.readonly(case.runtime.db_path) as conn:
            self.assertEqual(canonical(h.load_obligation(conn, matching[0])), encoded)
        self.assertEqual(len(new['recovery_inputs']),len(old['candidate']['effect_dates']))


class NumericalDigestTests(unittest.TestCase):
    def test_stream_digest_matches_exact_canonical_normalized_json(self):
        from packages.application.fbs_accounting_historical_history import _stream_fingerprint
        value=json.loads(canonical({'utf8':('Кириллица 😀 \\"\n'*6000),
            'types':[None,True,False,0,1,1.0,-0.0,'0.000000000000000000001'],
            'rows':{'я':{'nm_id':1},'a':[]}}))
        self.assertEqual(_stream_fingerprint(value),fingerprint(value))

    def test_native_prepare_and_validate_never_use_full_candidate_serialization_for_digest(self):
        case=Tests();case.setUp();self.addCleanup(case.doCleanups)
        original=candidate.fingerprint
        def small_fingerprint(value):
            if isinstance(value,dict) and value.get('contract')==candidate.CONTRACT and 'candidate_book' in value:
                self.fail('whole candidate must use the exact streaming digest')
            return original(value)
        with patch.object(candidate,'fingerprint',side_effect=small_fingerprint):
            numerical=candidate.prepare(case.runtime,case.identity,now=MOMENT)
            self.assertEqual(candidate.validate(numerical,runtime=case.runtime,now=MOMENT),numerical)
        operands={k:v for k,v in numerical.items() if k!='manifest_digest'}
        self.assertEqual(numerical['manifest_digest'],original(operands))
        self.assertEqual(numerical,json.loads(canonical(numerical)))
        corrupt=deepcopy(numerical);corrupt['manifest_digest']='sha256:'+'0'*64
        with self.assertRaisesRegex(ValueError,'candidate_corrupt'):
            candidate.validate(corrupt,runtime=case.runtime,now=MOMENT)


class InitialAdmissionRecoveryTests(unittest.TestCase):
    def fixture(self):
        case=Tests();case.SECOND_SOURCE=True;case.setUp();self.addCleanup(case.doCleanups)
        old=case.initial_admission_manifest()
        self.assertTrue(any(p['proof']['kind']=='native_initial_pre_ff_supplier_admission'
            for e in old['candidate']['editions'].values() for p in e['source_dependency']['supplier']))
        def crash(point):
            if point=='after_book':raise RuntimeError('initial admission book ghost')
        with self.assertRaisesRegex(RuntimeError,'initial admission book ghost'):case.publish(old,inject=crash)
        return case,old

    def followup(self,case):
        from packages.application.warehouse_functional import WarehouseFunctionalBlock
        rt=case.runtime;tomorrow=MOMENT+timedelta(days=1)
        accepted=execute(rt,'initial-ghost-next-day',[{'child_key':'expense','kind':'financial','subject_id':'initial-ghost-next-day'}],
            lambda context:rt.save_supplier_financial_document(document=dict(document_id='initial-ghost-next-day',supplier_order_id='source',
                document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date=FIRST,parse_status='confirmed',total_amount_rub=90),
                expense_lines=[dict(line_id='initial-ghost-next-day',amount=90,amount_rub=90,currency='RUB',category='logistics',status='confirmed')]),owners=('source',))
        with ready.readonly(rt.db_path) as conn:
            identity=conn.execute(f'SELECT operation_id FROM {financial.SCOPES} WHERE parent_operation_id=?',(accepted['results'][0]['acceptance']['operation_id'],)).fetchone()[0]
        intents.drain_supplier_preparation_intents(rt,shipment_ids=['source','second','new-admission'])
        native=WarehouseFunctionalBlock(runtime=rt,timestamp_factory=lambda:tomorrow.isoformat())
        # A new business day first owns a fresh fixture official observation;
        # the targeted publisher must not bypass its stale-snapshot guard.
        payload=deepcopy(native._last_good_wb_payload())
        payload['snapshot_date']=tomorrow.date().isoformat();payload['data']['fetched_at']=tomorrow.isoformat()
        sync=native._build_plan(kind='hourly_wb_sync',wb_payload=payload)
        native.apply_plan(sync,confirm_fingerprint=sync['plan_fingerprint'])
        return candidate.prepare(rt,identity,now=tomorrow),tomorrow

    def test_initial_admission_book_ghost_recovers_new_attempt_next_day(self):
        case,old=self.fixture();new,tomorrow=self.followup(case)
        self.assertNotEqual(new['attempt_id'],old['attempt_id'])
        self.assertEqual(new['functional']['business_date'],tomorrow.date().isoformat())
        self.assertEqual(len(new['recovery_inputs']),len(old['candidate']['effect_dates']))
        for day in old['candidate']['effect_dates']:
            self.assertEqual(new['editions'][day]['saved'],old['candidate']['editions'][day]['saved'])
            owned=[s for r in new['editions'][day]['balances'] for s in r['provenance'].get('source_records',[])
                   if s.get('shipment_id')=='new-admission']
            self.assertEqual(sum((Decimal(s['flow_quantity']) for s in owned),Decimal(0)),10)
        self.assertEqual(candidate.validate(new,runtime=case.runtime,now=tomorrow),new)

    def test_only_exact_prior_initial_deltas_can_explain_ghost_quantities(self):
        case,old=self.fixture();day=old['candidate']['effect_dates'][0]
        def admission(value):
            return next(p['proof'] for p in value['candidate']['editions'][day]['source_dependency']['supplier']
                        if p['proof']['kind']=='native_initial_pre_ff_supplier_admission')
        changes=[]
        value=deepcopy(old);admission(value)['quantity_by_nm']['1']='11';changes.append(value)
        value=deepcopy(old);admission(value)['capital_by_nm']['1']='1001';changes.append(value)
        value=deepcopy(old);admission(value)['source_records'][0]['flow_quantity']='11';changes.append(value)
        value=deepcopy(old);admission(value)['kind']='not_an_initial_admission';changes.append(value)
        for stage in ('production','wb'):
            value=deepcopy(old);row=next(r for r in value['candidate']['editions'][day]['balances'] if r['warehouse_key']==stage)
            row['quantity']=str(Decimal(row['quantity'])+1);changes.append(value)
        for index,value in enumerate(changes):
            # A consistent forged stored envelope must still fail the native
            # initial-admission/quantity proof after reference rehydration.
            value['candidate']['manifest_digest']=h._large_fingerprint({k:v for k,v in value['candidate'].items() if k!='manifest_digest'})
            value['manifest_digest']=h._large_fingerprint({k:v for k,v in value.items() if k!='manifest_digest'})
            encoded=canonical(value)
            with _connect(case.runtime.db_path) as conn:
                conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET inputs_json=? WHERE operation_id=? AND attempt_id=?',
                    (encoded,old['operation_id'],old['attempt_id']));conn.commit()
            with self.subTest(change=index),ready.readonly(case.runtime.db_path) as conn:
                row=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                    (old['operation_id'],old['attempt_id'])).fetchone()
                item={key:row[key] for key in h.OBLIGATION_FIELDS}
                item.update(inputs_ref=h.retained_inputs_ref(encoded),manifest_digest=value['manifest_digest'],dates=value['candidate']['effect_dates'],diagnostics_json=row['diagnostics_json'])
                with self.assertRaisesRegex(ValueError,'ghost_initial_admission_changed|ghost_quantity_changed'):
                    candidate.recovery_stage_book(conn,case.runtime,old['candidate']['candidate_book'],[item],[day])

    def test_replayed_native_admission_proof_remains_in_writer_fence(self):
        case,old=self.fixture();new,_=self.followup(case)
        member=next(m for m in old['candidate']['members'] if m['source_ref']['shipment_id']=='new-admission')
        with _connect(case.runtime.db_path) as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):
                conn.execute(f'UPDATE {processing.FUNCTIONAL} SET proof_digest=? WHERE operation_id=? AND version_id=?',
                             ('changed-after-prepare',member['source_ref']['operation_id'],member['functional']['version_id']))
            row=conn.execute(f'SELECT warehouse_key,nm_id,provenance_json FROM {stages.P}functional_balances WHERE version_id=? LIMIT 1',
                             (member['functional']['version_id'],)).fetchone()
            provenance=json.loads(row['provenance_json']);provenance['changed_after_replay']=True
            conn.execute(f'UPDATE {stages.P}functional_balances SET provenance_json=? WHERE version_id=? AND warehouse_key=? AND nm_id=?',
                         (canonical(provenance),member['functional']['version_id'],row['warehouse_key'],row['nm_id']));conn.commit()
        with ready.readonly(case.runtime.db_path) as conn:
            with self.assertRaisesRegex(ready.ReadyPublicationConflict,'ready_repair_source_changed'):
                stages.check_query_fence(conn,new['source_inputs'])


class SupplierWbSourceAuthorityTests(unittest.TestCase):
    def member(self,kind='unaccepted'):
        return dict(source_ref=dict(operation_id='supplier-own',shipment_id='own',source_digest='sha256:own'),
            shipment=dict(shipment_id='own',actual_ff_acceptance_date=''),
            receipt_dependency=dict(kind=kind,operations=[],lines=[],documents=[]))

    def test_unaccepted_source_preserves_all_wb_bytes_despite_foreign_daily_drift(self):
        rows=[dict(warehouse_key='wb',nm_id=1,quantity='379',capital_rub='35672.00000000000000000000001',wac_rub='94.12',
            provenance=dict(source_records=[dict(snapshot_id='saved-morning')]),quality='saved'),
            dict(warehouse_key='wb',nm_id=2,quantity='5',capital_rub='25',wac_rub='5',provenance={'foreign':True})]
        daily=[dict(as_of_date=DAY,nm_id=1,quantity='373',wac_rub='9999',capital_rub=str(373*9999),quality='foreign-evening')]
        before=deepcopy((rows,daily));member=self.member()
        after,proof=candidate._wb_rows(rows,daily=daily,day=DAY,ids=[1],members=[member])
        self.assertEqual(canonical(after),canonical(rows));self.assertEqual((rows,daily),before)
        self.assertEqual(proof[0]['kind'],'unaccepted_supplier_has_no_wb_cost_authority')
        self.assertEqual(proof[0]['source_refs'],[member['source_ref']])
        self.assertEqual(proof[0]['receipt_absence'][0]['dependency'],member['receipt_dependency'])

    def test_completion_consumes_only_native_no_wb_authority_for_its_date_and_cohort(self):
        from packages.application.operator_supplier_cost_proof import _supplier_has_no_dated_wb_authority
        member = self.member()
        other = {**self.member(), 'source_ref': {**member['source_ref'], 'operation_id': 'other'}}
        _, proof = candidate._wb_rows([], daily=[], day=DAY, ids=[], members=[member, other])
        refs = [member['source_ref'], other['source_ref']]
        evaluation = {'cohort_refs': refs, 'evaluation': {DAY: {'wb': proof}}}
        self.assertTrue(_supplier_has_no_dated_wb_authority(evaluation, DAY))
        self.assertFalse(_supplier_has_no_dated_wb_authority(evaluation, FIRST))
        for wrong in (None, {}, {**evaluation, 'cohort_refs': []},
                      {**evaluation, 'cohort_refs': refs[:1]}):
            self.assertFalse(_supplier_has_no_dated_wb_authority(wrong, DAY))
        for invalid in ([], [*proof, *proof], [{**proof[0], 'date': FIRST}],
                        [{**proof[0], 'kind': 'native_wb_cost'}]):
            self.assertFalse(_supplier_has_no_dated_wb_authority(
                {**evaluation, 'evaluation': {DAY: {'wb': invalid}}}, DAY))

    def test_accepted_unknown_mixed_and_unproven_cohorts_keep_strict_quantity_guard(self):
        rows=[dict(warehouse_key='wb',nm_id=1,quantity='379',capital_rub='379',provenance={})]
        daily=[dict(as_of_date=DAY,nm_id=1,quantity='373',wac_rub='1',capital_rub='373',quality='daily')]
        invalid=self.member();invalid['shipment']['actual_ff_acceptance_date']=DAY
        incomplete=self.member();incomplete['receipt_dependency'].pop('lines')
        changed=self.member();changed['receipt_dependency']['operations']=[{'operation_id':'new-receipt'}]
        cases=[None,[],[self.member('legacy_supplier_allocation')],[self.member('immutable_posted_supplier_receipt')],
            [self.member('unknown')],[self.member(),self.member('legacy_supplier_allocation')],[invalid],[incomplete],[changed]]
        for members in cases:
            with self.subTest(members=members),self.assertRaisesRegex(ValueError,'dated_wb_quantity_changed'):
                candidate._wb_rows(rows,daily=daily,day=DAY,ids=[1],members=members)

    def test_real_initial_admission_long_wb_money_publish_receipt_and_recapture(self):
        fixture=Tests();fixture.LONG_WB_MONEY=True;fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.test_native_late_create_materializes_initial_source_and_companions()


class SupplierReadySelectionTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''CREATE TABLE registry_upload_current_state(slot,bundle_version);
            INSERT INTO registry_upload_current_state VALUES(1,'fixture');
            CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version,as_of_date,snapshot_id,
                activated_at,refreshed_at,plan_json,PRIMARY KEY(bundle_version,as_of_date));''')
        self.body_keys=[]

    def add(self,day,next_day,*,recovery=None,inferred=False,padding=''):
        payload=asdict(make_plan(as_of_date=day,day=next_day))
        if inferred:payload.pop('date_columns')
        payload['metadata']={'local_padding':padding,**(recovery or {})}
        body=canonical(payload)
        self.conn.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?,?)',
            ('fixture',day,payload['snapshot_id'],day+'T12:00:00Z',day+'T12:00:00Z',body))
        return len(body.encode())

    def readonly(self):
        self.conn.commit();self.conn.execute('PRAGMA query_only=ON');self.conn.execute('BEGIN')
        owner=self
        class Observer:
            def execute(self,sql,args=()):
                if sql.startswith('SELECT * FROM '+ready.TABLE):owner.body_keys.append(tuple(args))
                return owner.conn.execute(sql,args)
        return Observer()

    def parity(self,dates,today):
        from packages.application.historical_dated_inputs import selected
        conn=self.readonly();before=self.conn.total_changes
        self.assertEqual(h._selected_ready(conn,dates,today),selected(self.conn,dates,today))
        self.assertEqual(self.conn.total_changes,before)
        self.assertEqual(self.conn.execute('PRAGMA query_only').fetchone()[0],1)
        self.assertTrue(self.conn.in_transaction)

    def test_exact_inferred_columns_and_original_source_order(self):
        self.add('2026-09-07','2026-09-08',inferred=True)
        self.add('2026-09-08','2026-09-09')
        self.parity(['2026-09-07','2026-09-08','2026-09-09'],'2026-09-08')
        self.assertEqual(self.body_keys,[('fixture','2026-09-08'),('fixture','2026-09-07')])

    def test_functional_today_default_is_not_latest_or_wall_clock(self):
        self.add('2026-09-07','2026-09-08');self.add('2026-09-09','2026-09-10')
        self.parity(['2026-09-08'],'2026-09-07')
        self.assertEqual(self.body_keys,[('fixture','2026-09-07')])

    def test_covering_recovery_matches_native_selection(self):
        from packages.application.web_vitrina_management_history import FACT_SOURCE,FACT_GROUPS
        proof={'server_cell_presentation':{'SKU:1|ordersCount':{'2026-09-08':{
            'source':FACT_SOURCE,'source_as_of_date':'2026-09-08','complete_source_groups':sorted(FACT_GROUPS)}}}}
        self.add('2026-09-07','2026-09-08',recovery=proof)
        self.add('2026-09-09','2026-09-10')
        self.parity(['2026-09-08'],'2026-09-09')

    def test_missing_and_unproven_covering_sources_still_fail_closed(self):
        from packages.application.historical_dated_inputs import selected
        self.add('2026-09-07','2026-09-08');self.add('2026-09-09','2026-09-10')
        conn=self.readonly()
        for dates in (['1900-01-01'],['2026-09-08']):
            for selector in (h._selected_ready,selected):
                with self.assertRaisesRegex(ValueError,'policy_history_dated_ready_inputs_missing'):
                    selector(conn,dates,'2026-09-09')
        self.assertEqual(self.body_keys,[])

    def test_unrelated_archive_over_real_limit_never_loads_full_bodies(self):
        selected_bytes=self.add('2026-09-07','2026-09-08')
        padding='x'*(h.MAX_READY_SOURCE_BYTES//2+1024)
        self.add('2025-01-01','2025-01-02',padding=padding)
        self.add('2025-01-03','2025-01-04',padding=padding)
        conn=self.readonly()
        self.assertGreater(self.conn.execute('SELECT sum(length(CAST(plan_json AS BLOB))) FROM '+ready.TABLE).fetchone()[0],h.MAX_READY_SOURCE_BYTES)
        result=h._selected_ready(conn,['2026-09-07'],'2026-09-07')
        self.assertEqual(len(result),1);self.assertEqual(len(result[0][0]['plan_json'].encode()),selected_bytes)
        self.assertEqual(self.body_keys,[('fixture','2026-09-07')])

    def test_selected_body_and_header_limits_refuse_before_body_fetch(self):
        selected_bytes=self.add('2026-09-07','2026-09-08')
        conn=self.readonly()
        with patch.object(h,'MAX_READY_SOURCE_BYTES',selected_bytes-1):
            with self.assertRaisesRegex(ValueError,'policy_history_source_size_limit'):
                h._selected_ready(conn,['2026-09-07'],'2026-09-07')
        with patch.object(h,'MAX_READY_HEADER_BYTES',1):
            with self.assertRaisesRegex(ValueError,'ready_header_size_limit'):
                h._selected_ready(conn,['2026-09-07'],'2026-09-07')
        self.assertEqual(self.body_keys,[])


class PendingCandidateBoundaryTests(unittest.TestCase):
    def test_native_pending_replan_discovers_once_and_still_validates_independently(self):
        helper=ReplanTests();self.addCleanup(helper.doCleanups);case=helper.fixture()
        old=case.prepare();case.publish(old)
        new,_,_=helper.followup(case,old);attempt=new['attempt_id'];del new
        prepare=candidate.prepare;calls=[]
        def observed(*args,**kwargs):
            calls.append('validation' if 'cohort_refs' in kwargs else 'discovery')
            return prepare(*args,**kwargs)
        with patch.object(candidate,'prepare',observed), \
             heavy_admitted(case.runtime.runtime_dir,operation='cycle'),warehouse_functional_job_lock(case.runtime.runtime_dir):
            receipt=h.pending(case.runtime,now=MOMENT)
        self.assertIsNotNone(receipt);self.assertEqual(receipt.attempt_id,attempt)
        self.assertEqual(calls.count('discovery'),1);self.assertGreaterEqual(calls.count('validation'),1)
        self.assertEqual(receipt.manifest['candidate']['owed_dates'],old['candidate']['effect_dates'])

    def test_one_candidate_for_fresh_and_replan_without_old_exception_operands(self):
        from types import SimpleNamespace
        class Tracked(dict):pass
        runtime=SimpleNamespace(db_path=Path('/isolated/op.sqlite3'))
        @contextmanager
        def readonly(path):yield object()
        for fail_at in (None,'publish','validation'):
            with self.subTest(fail_at=fail_at):
                refs={};result=object()
                numerical={'effect_dates':[FIRST],'attempt_id':'new','obligations':[{'old':'owed'}]}
                def pending_check(*args):
                    if fail_at is None:return None,None
                    row=Tracked(inputs_json='old-json');refs['prior']=weakref.ref(row)
                    return None,row
                def load_old(value):
                    self.assertEqual(value,'old-json')
                    old=Tracked(attempt_id='old');refs['manifest']=weakref.ref(old)
                    return old
                def fail():
                    large=Tracked(payload='old publisher operand');refs['failed_frame']=weakref.ref(large)
                    raise ready.ReadyPublicationConflict('ready_target_changed:fixture')
                class OldReceipt:
                    def __init__(self,manifest):self.manifest=manifest
                    def validate_sources_readonly(self,**kwargs):fail()
                def publish(rt,manifest):
                    if manifest.get('attempt_id')=='old':
                        if fail_at=='publish':fail()
                        receipt=OldReceipt(manifest);refs['receipt']=weakref.ref(receipt);return receipt
                    self.assertIs(manifest['candidate'],numerical);return result
                def prepare_candidate(*args,**kwargs):
                    self.assertIsNone(sys.exception(),'replan stayed inside failed publisher except')
                    for name,ref in refs.items():self.assertIsNone(ref(),name+' survived into numerical rebuild')
                    return numerical
                with patch.object(ready,'readonly',readonly),patch.object(source,'_exists',return_value=True), \
                     patch.object(processing,'captured_cohort',return_value=['source']), \
                     patch.object(processing,'operation',return_value={'operation_id':'source'}), \
                     patch.object(h,'_pending_check',side_effect=pending_check),patch.object(h,'_verify',lambda manifest,**kwargs:None), \
                     patch.object(h.json,'loads',side_effect=load_old),patch.object(h,'publish',publish), \
                     patch.object(candidate,'prepare',side_effect=prepare_candidate) as prepared, \
                     patch.object(h,'_prepare_publication',side_effect=lambda rt,value,**kw:{'candidate':value}) as publication:
                    self.assertIs(h.pending(runtime,now=MOMENT),result)
                    self.assertEqual(prepared.call_count,1);self.assertEqual(publication.call_count,1)
                    self.assertIs(publication.call_args.args[1],numerical)

    def test_no_dated_effect_skips_publication_preparation(self):
        from types import SimpleNamespace
        @contextmanager
        def readonly(path):yield object()
        with patch.object(ready,'readonly',readonly),patch.object(source,'_exists',return_value=True), \
             patch.object(processing,'captured_cohort',return_value=['source']), \
             patch.object(processing,'operation',return_value={'operation_id':'source'}), \
             patch.object(h,'_pending_check',return_value=(None,None)), \
             patch.object(candidate,'prepare',return_value={'effect_dates':[]}) as prepared, \
             patch.object(h,'_prepare_publication',side_effect=AssertionError('no-op prepared publication')), \
             patch.object(h,'_checked') as checked:
            self.assertIsNone(h.pending(SimpleNamespace(db_path=Path('/isolated/op.sqlite3')),now=MOMENT))
            self.assertEqual(prepared.call_count,1)
            self.assertEqual(checked.call_args.args[2],'supplier_history_no_dated_effect')


class ReceiptMemoryBoundaryTests(unittest.TestCase):
    def test_full_active_book_and_last_ready_operands_release_before_receipt_reread(self):
        from types import SimpleNamespace
        from packages.application import historical_dated_inputs as dated
        class Tracked(dict):pass
        refs={}
        fields=('wb_days','retained_days','shared_days','presentations')
        expected={field:{FIRST:{'value':field}} for field in fields}
        receipt=object.__new__(h.SupplierHistory);receipt.runtime=SimpleNamespace(runtime_dir=Path('/isolated-fixture'))
        receipt.dates=(FIRST,)
        receipt.manifest={'candidate':{'candidate_book':expected,'functional':{'business_date':FIRST},
            'editions':{FIRST:{'version_id':'native','version':{'version_id':'native'},'balances':[]}}},
            'targets':[{'dates':[FIRST],'dated_outputs':{FIRST:fingerprint({})}}]}
        def active_book(*args,**kwargs):
            book=Tracked(deepcopy(expected));refs['active']=weakref.ref(book)
            return book,'active-version'
        def selected(*args):
            row=Tracked(bundle_version='fixture',as_of_date=FIRST,plan_json='{}');refs['row']=weakref.ref(row)
            return [(row,[FIRST])]
        def lineage(*args,**kwargs):
            value=Tracked({field:deepcopy(expected[field][FIRST]) for field in fields});refs['lineage']=weakref.ref(value)
            return value
        @contextmanager
        def prepared(*args):
            class Books:
                cache={}
                observer=SimpleNamespace(execute=lambda *args:SimpleNamespace(fetchone=lambda:('active-version',)))
                def guard(self):pass
            books=Books();refs['prepared']=weakref.ref(books)
            yield books
        def binding():
            for name,ref in refs.items():self.assertIsNone(ref(),name+' retained during receipt reread')
            return {'verified':'binding'}
        conn=SimpleNamespace(execute=lambda *args:SimpleNamespace(fetchone=lambda:{'version_id':'native'}))
        with patch.object(h,'_source_fence'),patch.object(receipt,'_read',return_value=(receipt.manifest,{})), \
             patch.object(dated,'prepared_book_lineage',prepared),patch.object(accounting,'load',side_effect=active_book), \
             patch.object(h,'_selected_ready',side_effect=selected),patch.object(dated,'dated_slice',return_value={}), \
             patch.object(dated,'book_lineage',lineage),patch.object(stages,'_version_balances',return_value=[]), \
             patch.object(receipt,'binding',side_effect=binding):
            result=receipt._validate_sources_conn(conn)
        self.assertEqual(result['binding'],{'verified':'binding'});self.assertEqual(result['active_book'],'active-version')
        self.assertEqual(set(result['dated']),{FIRST})

    def test_receipt_blob_utf8_size_guard_precedes_full_text_fetch_without_python_encode(self):
        from types import SimpleNamespace
        with TemporaryDirectory(prefix='supplier-receipt-size-') as raw:
            runtime=SimpleNamespace(runtime_dir=Path(raw),db_path=Path(raw)/'op.sqlite3')
            manifest=dict(contract=h.CONTRACT,storage_encoding=h.READY_JSON_ENCODING,operation_id='supplier-history:source',
                attempt_id='attempt',source_ref={'operation_id':'source'},
                candidate=dict(code_authority=candidate.code_authority(),effect_dates=[FIRST],functional={'business_date':FIRST},candidate_book={}),
                targets=[dict(dates=[FIRST],expected=asdict(ready.ExpectedReady('fixture',FIRST,None)),after_json=h._pack_ready_json('{}'))],
                after_book=h._large_fingerprint({}),unicode_padding='Ж😀'*100)
            manifest['manifest_digest']=h._large_fingerprint(manifest)
            encoded=canonical(manifest);raw_bytes=encoded.encode();size=len(raw_bytes)
            self.assertGreater(size,len(encoded))
            with closing(sqlite3.connect(runtime.db_path)) as conn,conn:
                conn.execute('CREATE TABLE sheet_vitrina_v1_ready_publications(operation_id,attempt_id,kind,state,inputs_json,diagnostics_json,book_version,after_digest)')
                conn.execute('INSERT INTO sheet_vitrina_v1_ready_publications VALUES(?,?,?,?,?,?,?,?)',
                    (manifest['operation_id'],manifest['attempt_id'],h.CONTRACT,'complete',encoded,'{}',manifest['after_book'],ready.digest('{}')))
            receipt=object.__new__(h.SupplierHistory);receipt.runtime=runtime
            receipt.operation_id=manifest['operation_id'];receipt.attempt_id=manifest['attempt_id']
            native=ready.readonly;fetched=[];statements=[]
            class NoEncode(str):
                def encode(self,*args,**kwargs):raise AssertionError('receipt allocated an extra UTF-8 publication copy')
            def text(value):
                if value==raw_bytes:fetched.append('publication')
                return NoEncode(value.decode())
            @contextmanager
            def readonly(path):
                with native(path) as conn:
                    self.assertTrue(conn.in_transaction)
                    conn.text_factory=text;conn.set_trace_callback(statements.append)
                    yield conn
            with patch.object(ready,'readonly',readonly):
                with patch.object(h,'MAX_PUBLICATION_BYTES',size):self.assertEqual(receipt._read()[0],manifest)
                self.assertEqual(fetched,['publication']);fetched.clear()
                with patch.object(h,'MAX_PUBLICATION_BYTES',size-1),self.assertRaisesRegex(ValueError,'publication_not_complete'):
                    receipt._read()
                self.assertEqual(fetched,[],'oversize publication crossed the actual-byte guard')
            self.assertEqual(sum(sql.startswith('SELECT inputs_json FROM ') for sql in statements),1)


class SupplierReceiptReadbackTests(unittest.TestCase):
    def fixture(self):
        case=Tests();case.setUp();self.addCleanup(case.doCleanups)
        manifest=case.prepare();case.publish(manifest)
        return case,manifest

    def test_cold_verify_once_warm_exact_readback_and_one_native_book_load(self):
        case,manifest=self.fixture()
        with patch.object(h,'_verify',wraps=h._verify) as verify:
            receipt=h.SupplierHistory(case.runtime,manifest['operation_id'],manifest['attempt_id'])
            receipt.binding()
            with patch.object(accounting,'load',wraps=accounting.load) as load:
                first=receipt.validate_sources_readonly(now=MOMENT)
                self.assertEqual(load.call_count,1)
            self.assertEqual(receipt.validate_sources_readonly(now=MOMENT),first)
            self.assertEqual(verify.call_count,1)

    def test_mutable_dates_cannot_narrow_or_reorder_the_verified_manifest_scope(self):
        case,manifest=self.fixture();receipt=h.SupplierHistory(case.runtime,manifest['operation_id'],manifest['attempt_id'])
        original=receipt.dates
        for changed in ((),original[:-1],tuple(reversed(original)),list(original)):
            receipt.dates=changed
            for method in (receipt.binding,receipt.publication_dates,lambda:receipt.validate_sources_readonly(now=MOMENT)):
                with self.subTest(dates=changed),self.assertRaisesRegex(ValueError,'receipt_scope_changed'):method()
        receipt.dates=original;self.assertEqual(receipt.publication_dates(),original)

    def test_retained_projection_reuses_only_verified_identity_with_fresh_rows_and_books(self):
        import zlib
        helper=ReplanTests();self.addCleanup(helper.doCleanups);case=helper.fixture();old=case.prepare()
        def crash(point):
            if point=='after_book':raise RuntimeError('old ghost')
        with self.assertRaisesRegex(RuntimeError,'old ghost'):case.publish(old,inject=crash)
        new,_,_=helper.followup(case,old);case.publish(new)
        receipt=h.SupplierHistory(case.runtime,new['operation_id'],new['attempt_id'])
        with patch.object(h,'load_obligation',wraps=h.load_obligation) as loaded:
            first=receipt.validate_sources_readonly(now=MOMENT)
            self.assertEqual(receipt.validate_sources_readonly(now=MOMENT),first)
            self.assertEqual(loaded.call_count,1)
        self.assertEqual(len(receipt._retained_proofs),1)
        key,entry=next(iter(receipt._retained_proofs.items()));projection=entry[0]
        # Even a recomputed memo seal cannot grant a different operation,
        # original scope, or book relation than the pinned owner proof.
        for index,value in ((0,'different-operation'),(3,projection[3][:-1]),(5,old['expected_book'])):
            forged=tuple(value if n==index else original for n,original in enumerate(projection))
            receipt._retained_proofs[key]=(forged,h._large_fingerprint(forged))
            with self.assertRaisesRegex(ValueError,'ghost_book_revision_unproved'):receipt.validate_sources_readonly(now=MOMENT)
        receipt._retained_proofs[key]=entry
        with _connect(case.runtime.db_path) as conn:
            row=conn.execute('SELECT rowid,inputs_json,diagnostics_json,book_version FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                (old['operation_id'],old['attempt_id'])).fetchone()
        altered=row['inputs_json'].replace('sha256:','sha257:',1);self.assertEqual(len(altered.encode()),len(row['inputs_json'].encode()))
        for field,value,error in (('inputs_json',altered,'prior_attempt_changed'),('book_version','changed','prior_attempt_changed|ready_repair_source_changed'),
                ('diagnostics_json',canonical({'supplier_history_ack':{}}),'prior_history_ack_changed'),
                ('diagnostics_json',canonical({'supplier_history_replaced_by':{}}),'prior_attempt_retirement_changed')):
            with _connect(case.runtime.db_path) as conn:
                conn.execute(f'UPDATE sheet_vitrina_v1_ready_publications SET {field}=? WHERE rowid=?',(value,row['rowid']));conn.commit()
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,error):receipt.validate_sources_readonly(now=MOMENT)
            with _connect(case.runtime.db_path) as conn:
                conn.execute(f'UPDATE sheet_vitrina_v1_ready_publications SET {field}=? WHERE rowid=?',(row[field],row['rowid']));conn.commit()
        with sqlite3.connect(accounting.path(case.runtime.runtime_dir)) as conn:
            payload=conn.execute('SELECT payload FROM accounting_revisions WHERE version=?',(old['after_book'],)).fetchone()[0]
            blob=json.loads(payload)['state']['baseline']
            compressed=conn.execute('SELECT payload FROM accounting_blobs WHERE digest=?',(blob,)).fetchone()[0]
            changed=json.loads(zlib.decompress(compressed));changed['changed-after-validation']=True
            conn.execute('UPDATE accounting_blobs SET payload=? WHERE digest=?',(zlib.compress(canonical(changed).encode()),blob));conn.commit()
        with self.assertRaisesRegex(ValueError,'fbs_accounting_blob_corrupt'):receipt.validate_sources_readonly(now=MOMENT)

    def test_warm_read_rejects_memory_code_row_metadata_retirement_and_stored_byte_tamper(self):
        case,manifest=self.fixture();receipt=h.SupplierHistory(case.runtime,manifest['operation_id'],manifest['attempt_id'])
        stamp=receipt.manifest['created_at'];receipt.manifest['created_at']='in-memory-tamper'
        with self.assertRaisesRegex(ValueError,'receipt_changed'):receipt.binding()
        receipt.manifest['created_at']=stamp
        ids=receipt.manifest['candidate']['queue_ref']['affected_nm_ids'];original=ids[0]
        for aliased in (bool(original),float(original)):
            ids[0]=aliased
            with self.subTest(aliased_type=type(aliased).__name__),self.assertRaisesRegex(ValueError,'receipt_changed'):receipt.binding()
            ids[0]=original
        with patch.object(candidate,'code_authority',return_value={'changed':'code'}),self.assertRaisesRegex(ValueError,'formula_changed'):
            receipt.binding()
        with _connect(case.runtime.db_path) as conn:
            saved=conn.execute('SELECT rowid,inputs_json,diagnostics_json,after_digest FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                (manifest['operation_id'],manifest['attempt_id'])).fetchone();rowid=saved['rowid']
        for field,value,error in (('after_digest','changed','receipt_changed'),
                ('diagnostics_json',canonical({'supplier_history_replaced_by':{}}),'publication_superseded'),
                ('inputs_json',' '+saved['inputs_json'],'receipt_changed')):
            with _connect(case.runtime.db_path) as conn:
                conn.execute(f'UPDATE sheet_vitrina_v1_ready_publications SET {field}=? WHERE rowid=?',(value,rowid));conn.commit()
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,error):receipt.binding()
            with _connect(case.runtime.db_path) as conn:
                conn.execute(f'UPDATE sheet_vitrina_v1_ready_publications SET {field}=? WHERE rowid=?',(saved[field],rowid));conn.commit()

    def test_cold_and_warm_equivalent_noncanonical_stored_json_remain_supported(self):
        case,manifest=self.fixture()
        with _connect(case.runtime.db_path) as conn:
            conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET inputs_json=? WHERE operation_id=? AND attempt_id=?',
                (json.dumps(manifest,ensure_ascii=True,indent=1),manifest['operation_id'],manifest['attempt_id']));conn.commit()
        receipt=h.SupplierHistory(case.runtime,manifest['operation_id'],manifest['attempt_id'])
        self.assertEqual(receipt.manifest,manifest);self.assertEqual(receipt.binding()['digest'],manifest['manifest_digest'])

    def test_warm_validation_keeps_ready_stage_source_and_current_book_fences(self):
        case,manifest=self.fixture();receipt=h.SupplierHistory(case.runtime,manifest['operation_id'],manifest['attempt_id'])
        receipt.validate_sources_readonly(now=MOMENT)
        day=receipt.dates[0];version=manifest['candidate']['editions'][day]['version_id']
        with _connect(case.runtime.db_path) as conn:
            stamp=conn.execute(f'SELECT published_at FROM {stages.P}functional_versions WHERE version_id=?',(version,)).fetchone()[0]
            conn.execute(f'UPDATE {stages.P}functional_versions SET published_at=? WHERE version_id=?',('changed',version));conn.commit()
        with self.assertRaisesRegex(ValueError,'dated_stage_readback_changed'):receipt.validate_sources_readonly(now=MOMENT)
        with _connect(case.runtime.db_path) as conn:
            conn.execute(f'UPDATE {stages.P}functional_versions SET published_at=? WHERE version_id=?',(stamp,version));conn.commit()
            row=conn.execute('SELECT bundle_version,as_of_date,plan_json FROM '+ready.TABLE+' LIMIT 1').fetchone()
            payload=json.loads(row['plan_json']);sheet=next(s for s in payload['sheets'] if s['sheet_name']=='DATA_VITRINA')
            sheet['rows'][0][sheet['header'].index(day)]='333'
            conn.execute('UPDATE '+ready.TABLE+' SET plan_json=? WHERE bundle_version=? AND as_of_date=?',(canonical(payload),row['bundle_version'],row['as_of_date']));conn.commit()
        with self.assertRaisesRegex(ValueError,'dated_ready_changed'):receipt.validate_sources_readonly(now=MOMENT)
        with _connect(case.runtime.db_path) as conn:
            conn.execute('UPDATE '+ready.TABLE+' SET plan_json=? WHERE bundle_version=? AND as_of_date=?',(row['plan_json'],row['bundle_version'],row['as_of_date']));conn.commit()
            conn.execute("UPDATE sheet_vitrina_v1_supplier_shipments SET invoice_no=invoice_no||'changed' WHERE shipment_id='source'");conn.commit()
        with self.assertRaisesRegex(ValueError,'ready_repair_source_changed'):receipt.validate_sources_readonly(now=MOMENT)
        with sqlite3.connect(accounting.path(case.runtime.runtime_dir)) as conn:
            conn.execute('UPDATE accounting_current SET version=? WHERE singleton=1',(manifest['expected_book'],));conn.commit()
        # Restore the supplier header so this refusal specifically reaches the current book guard.
        with _connect(case.runtime.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_supplier_shipments SET invoice_no=substr(invoice_no,1,length(invoice_no)-7) WHERE shipment_id='source'");conn.commit()
        with self.assertRaisesRegex(ValueError,'book_dates_changed'):receipt.validate_sources_readonly(now=MOMENT)

    def test_prepared_book_observer_rejects_concurrent_book_commit(self):
        from packages.application import historical_dated_inputs as dated
        case,manifest=self.fixture();receipt=h.SupplierHistory(case.runtime,manifest['operation_id'],manifest['attempt_id'])
        original=dated.book_lineage;changed=[]
        def race(*args,**kwargs):
            result=original(*args,**kwargs)
            if not changed:
                import zlib
                with sqlite3.connect(accounting.path(case.runtime.runtime_dir)) as conn:
                    value={'concurrent':'native fixture blob'}
                    conn.execute('INSERT INTO accounting_blobs VALUES(?,?)',(fingerprint(value),zlib.compress(canonical(value).encode())));conn.commit()
                changed.append(True)
            return result
        with patch.object(dated,'book_lineage',side_effect=race),self.assertRaisesRegex(ValueError,'dated_book_observer_changed'):
            receipt.validate_sources_readonly(now=MOMENT)


class SupplierNativeEditionIdentityTests(unittest.TestCase):
    @staticmethod
    def next_native(case,now):
        from packages.application.warehouse_functional import WarehouseFunctionalBlock
        native=WarehouseFunctionalBlock(runtime=case.runtime,timestamp_factory=lambda:now.isoformat())
        payload=deepcopy(native._last_good_wb_payload())
        payload['snapshot_date']=now.date().isoformat();payload['data']['fetched_at']=now.isoformat()
        plan=native._build_plan(kind='hourly_wb_sync',wb_payload=payload)
        native.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'])

    def fixture(self):
        case=Tests();case.setUp();self.addCleanup(case.doCleanups);old=case.prepare()
        def crash(point):
            if point=='after_book':raise RuntimeError('own book ghost')
        with self.assertRaisesRegex(RuntimeError,'own book ghost'):case.publish(old,inject=crash)
        later=MOMENT+timedelta(hours=1);self.next_native(case,later)
        new=h.prepare(case.runtime,case.identity,now=later)
        self.assertNotEqual(new['attempt_id'],old['attempt_id'])
        self.assertTrue(new['candidate']['recovery_inputs'])
        for recovery in new['candidate']['recovery_inputs']:
            day=recovery['day']
            self.assertNotEqual(recovery['ghost_version'],new['candidate']['editions'][day]['version_id'])
            self.assertEqual([{k:v for k,v in r.items() if k!='version_id'} for r in new['candidate']['editions'][day]['balances']],
                [{k:v for k,v in r.items() if k!='version_id'} for r in old['candidate']['editions'][day]['balances']])
        return case,old,new,later

    def test_same_money_new_native_attempt_keeps_all_source_fences(self):
        case,old,new,later=self.fixture()
        with ready.readonly(case.runtime.db_path) as conn:stages.check_query_fence(conn,new['candidate']['source_inputs'])
        receipt=case.publish(new)
        self.assertEqual(set(receipt.validate_sources_readonly(now=later)['dated']),set(old['candidate']['effect_dates']))
        with ready.readonly(case.runtime.db_path) as conn:
            stages.check_query_fence(conn,new['candidate']['source_inputs'])
            h._source_fence(conn,new,ready_before=False)
            self.assertTrue(h.verified_retirement(conn,h.obligation_row(conn,new['candidate']['obligations'][0])))
        recovery=new['candidate']['recovery_inputs'][-1];edition=new['candidate']['editions'][recovery['day']]
        version=edition['version_id']
        for sql,args in (
            (f'UPDATE {stages.P}functional_versions SET created_at=? WHERE version_id=?',('changed',version)),
            (f'UPDATE {stages.P}functional_balances SET capital_rub=? WHERE version_id=?',('999999',version))):
            with self.subTest(sql=sql),closing(_connect(case.runtime.db_path)) as conn:
                conn.execute(sql,args);conn.commit()
                with self.assertRaisesRegex(ValueError,'dated_stage_readback_changed'):receipt.validate_sources_readonly(now=later)
                conn.rollback()
                # Restore the exact native fixture rows before the next case.
                if 'functional_versions' in sql:conn.execute(f'UPDATE {stages.P}functional_versions SET created_at=? WHERE version_id=?',(edition['version']['created_at'],version))
                else:
                    for row in edition['balances']:
                        conn.execute(f'UPDATE {stages.P}functional_balances SET capital_rub=? WHERE version_id=? AND warehouse_key=? AND nm_id=?',(row['capital_rub'],version,row['warehouse_key'],row['nm_id']))
                conn.commit()
        with closing(_connect(case.runtime.db_path)) as conn:
            row={**edition['version'],'version_id':recovery['ghost_version'],'plan_fingerprint':fingerprint(['foreign',recovery['ghost_version']])}
            conn.execute(f'INSERT INTO {stages.P}functional_versions({",".join(row)}) VALUES({",".join("?" for _ in row)})',tuple(row.values()));conn.commit()
        with self.assertRaisesRegex(ValueError,'ready_repair_source_changed'):receipt.validate_sources_readonly(now=later)

    def test_business_day_rollover_keeps_exact_prior_date_obligations(self):
        case,old,new,later=self.fixture();case.publish(new)
        tomorrow=MOMENT+timedelta(days=1);self.next_native(case,tomorrow)
        numerical=candidate.prepare(case.runtime,case.identity,now=tomorrow)
        self.assertEqual(numerical['functional']['business_date'],tomorrow.date().isoformat())
        self.assertEqual(numerical['owed_dates'],new['candidate']['effect_dates'])
        self.assertEqual(candidate.validate(numerical,runtime=case.runtime,now=tomorrow),numerical)
        for day in new['candidate']['effect_dates']:
            self.assertNotEqual(numerical['editions'][day]['version_id'],new['candidate']['editions'][day]['version_id'])
            self.assertEqual(candidate.business_image(numerical['editions'][day]['balances']),candidate.business_image(new['candidate']['editions'][day]['balances']))

    def test_new_committed_validation_failure_stops_pending(self):
        case,old,new,later=self.fixture()
        with heavy_admitted(case.runtime.runtime_dir,operation='cycle'),warehouse_functional_job_lock(case.runtime.runtime_dir), \
                patch.object(h.SupplierHistory,'validate_sources_readonly',side_effect=ready.ReadyPublicationConflict('ready_repair_source_changed')), \
                patch.object(h,'_checked',side_effect=AssertionError('committed failure swallowed')):
            with self.assertRaisesRegex(RuntimeError,'supplier_history_committed_validation_failed'):
                h.pending(case.runtime,now=later)
        with ready.readonly(case.runtime.db_path) as conn:
            row=conn.execute('SELECT state,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                (new['operation_id'],new['attempt_id'])).fetchone()
            self.assertEqual(row['state'],'complete');self.assertNotIn('supplier_history_ack',json.loads(row['diagnostics_json']))

    def test_complete_known_prior_algorithm_replans_after_new_native_generation(self):
        case,old,new,later=self.fixture();case.publish(new)
        prior=deepcopy(new);prior['candidate']['code_authority']=h.RETAINED_COMPLETE_CODE_AUTHORITY
        prior['candidate']['manifest_digest']=h._large_fingerprint({k:v for k,v in prior['candidate'].items() if k!='manifest_digest'})
        prior['manifest_digest']=h._large_fingerprint({k:v for k,v in prior.items() if k!='manifest_digest'})
        with closing(_connect(case.runtime.db_path)) as conn:
            conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET inputs_json=? WHERE operation_id=? AND attempt_id=?',
                (canonical(prior),prior['operation_id'],prior['attempt_id']))
            conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=?',
                (canonical({'supplier_history_replaced_by':h.replacement_link(prior)}),old['operation_id'],old['attempt_id']));conn.commit()
        h._verify(prior,retained=True)
        with self.assertRaisesRegex(ValueError,'formula_changed'):h._verify(prior)
        after=later+timedelta(hours=1);self.next_native(case,after)
        with heavy_admitted(case.runtime.runtime_dir,operation='cycle'),warehouse_functional_job_lock(case.runtime.runtime_dir):receipt=h.pending(case.runtime,now=after)
        self.assertIsNotNone(receipt);successor=receipt.manifest
        self.assertNotEqual(successor['attempt_id'],prior['attempt_id'])
        self.assertEqual(successor['candidate']['owed_dates'],prior['candidate']['effect_dates'])
        for day in prior['candidate']['effect_dates']:
            self.assertNotEqual(successor['candidate']['editions'][day]['version_id'],prior['candidate']['editions'][day]['version_id'])
            self.assertEqual(candidate.business_image(successor['candidate']['editions'][day]['balances']),candidate.business_image(prior['candidate']['editions'][day]['balances']))
        self.assertEqual(set(receipt.validate_sources_readonly(now=after)['dated']),set(prior['candidate']['effect_dates']))
        with ready.readonly(case.runtime.db_path) as conn:
            rows=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(prior['operation_id'],prior['attempt_id'])).fetchone()
            self.assertTrue(h.verified_retirement(conn,rows));self.assertNotIn('supplier_history_ack',json.loads(rows['diagnostics_json']))


if __name__=='__main__':unittest.main()
