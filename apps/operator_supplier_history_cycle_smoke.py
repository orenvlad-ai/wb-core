"""Cycle priority seams and native financial CNY-companion late cost -> owned History -> Finance.

SeamTests isolate orchestration only; LinuxTests keep all native publication,
History, Finance and parent completion proofs real in a disposable database.
"""
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
import hashlib,json,sqlite3,sys,threading,unittest
from copy import deepcopy
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint,SHEET_OPERATOR_JOB_ID
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import require_warehouse_job_owner
from apps.operator_supplier_history_smoke import NOW,MOMENT,DAY,FIRST


def cycle_receipt():
    import os
    from packages.application.web_vitrina_snapshot_admission import process_identity
    return dict(job_id='supplier-cycle', owner_pid=os.getpid(), process_identity=process_identity(os.getpid()))

from packages.application.business_data_procedure_admission import admitted_write

def host(rt, *, verify=None):
    entry=object.__new__(RegistryUploadHttpEntrypoint)
    entry.runtime=rt;entry.now_factory=lambda:MOMENT
    entry.operator_jobs=SimpleNamespace(get=lambda identity:{'operation':'cycle','status':'running'},_threads={'supplier-cycle':threading.current_thread()})
    if verify is not None:entry._cycle_verify_ready=verify
    return entry


def finance_result():
    return dict(status='already_current',non_target_preserved=True,post_verify_stale_week_count=0,
        accounting_version_unchanged=True,source_advanced_after_apply=False,supplier_operation_completion={'status':'ok'})


class SeamTests(unittest.TestCase):
    def setUp(self):
        tmp=TemporaryDirectory(prefix='supplier-cycle-seam-');self.addCleanup(tmp.cleanup)
        self.rt=SimpleNamespace(runtime_dir=Path(tmp.name),db_path=Path(tmp.name)/'synthetic.db')
        self.entry=host(self.rt,verify=Mock());self.events=[]
        self.supplier=SimpleNamespace(publication_dates=lambda:['2026-07-10','2026-07-23','2026-07-24'],
            validate_sources_readonly=lambda **kw:self.events.append('validate') or 'exact-seam-source')
        self.supplier._read=lambda:({'manifest_digest':'seam-only'},{'supplier_history_ack':{'manifest_digest':'seam-only','native':dict.fromkeys(self.supplier.publication_dates())}})
        token=SHEET_OPERATOR_JOB_ID.set('supplier-cycle');self.addCleanup(SHEET_OPERATOR_JOB_ID.reset,token)
    def invoke(self, *, ff=None, supplier=None, finance=None, builder=None):
        def pending(*args,**kw):
            require_warehouse_job_owner(self.rt.runtime_dir);self.events.append('supplier');return supplier
        def finish(*args,**kw):
            require_warehouse_job_owner(self.rt.runtime_dir);self.events.append('finance');return finance or finance_result()
        # This is a seam-only owner fixture; the LinuxTests below bind real /proc identity.
        with patch('packages.application.web_vitrina_snapshot_admission.process_identity',return_value='synthetic-seam-process'),admitted_write(self.rt.runtime_dir),heavy_admitted(self.rt.runtime_dir,operation='cycle'),patch('packages.application.operator_policy_history.pending',return_value=None),patch('packages.application.operator_supplier_history.pending',side_effect=pending) as sp,patch('apps.web_vitrina_history_candidate_build.build_owned_cycle_history',side_effect=builder or (lambda **kw:self.events.append(('build',kw)) or {'status':'success'})),patch('apps.warehouse_functional_runner._recalculate_downstream_finance_cost',side_effect=finish),patch('packages.application.fbs_accounting_historical_cycle.finalize',return_value={'status':'complete'}) as finalize:
            result=self.entry._cycle_history(None,cycle_receipt(),{},backfill_dates=('2026-07-09',),historical_receipt=ff)
            return result,sp.call_count,finalize.call_count
    def test_ff_owner_excludes_supplier(self):
        result,sp,finalize=self.invoke(ff=object())
        self.assertEqual((sp,finalize),(0,1));self.assertEqual(self.entry._cycle_verify_ready.call_count,2)
    def test_supplier_publishes_under_owner_then_finance_after_child_return(self):
        result,sp,finalize=self.invoke(supplier=self.supplier)
        self.assertEqual((sp,finalize),(1,0));self.assertEqual(self.entry._cycle_verify_ready.call_count,1)
        options=next(v[1] for v in self.events if isinstance(v,tuple))
        self.assertIs(options['supplier_receipt'],self.supplier)
        self.assertEqual(options['backfill_dates'],('2026-07-09','2026-07-10'))
        self.assertLess(self.events.index(('build',options)),self.events.index('finance'))
        self.assertIn('history_supplier_completion',result.versions)
    def test_generic_finance_success_and_missing_exact_zero_cannot_pass(self):
        from packages.application.sheet_vitrina_v1_cycle import CycleStageFailure
        for bad in ({'status':'success'},dict(finance_result(),post_verify_stale_week_count=None),dict(finance_result(),accounting_version_unchanged=False)):
            with self.subTest(receipt=bad),self.assertRaisesRegex(CycleStageFailure,'supplier_history_finance_unproven'):
                self.invoke(supplier=self.supplier,finance=bad)
    def test_generic_history_success_without_own_ack_never_runs_finance(self):
        from packages.application.sheet_vitrina_v1_cycle import CycleStageFailure
        self.supplier._read=lambda:({'manifest_digest':'seam-only'}, {})
        with self.assertRaisesRegex(CycleStageFailure,'supplier_history_completion_ack_missing'):
            self.invoke(supplier=self.supplier)
        self.assertNotIn('finance',self.events)
    def test_no_dated_owner_retains_original_ready_recheck(self):
        result,sp,finalize=self.invoke()
        self.assertEqual((sp,finalize),(1,0));self.assertEqual(self.entry._cycle_verify_ready.call_count,2)
        self.assertNotIn('finance',self.events)


def setup_cny(case):
    from apps.operator_supplier_processing_smoke import seed,publish_functional,publish_accounting
    from apps.operator_supplier_history_smoke import install_native_saved_dates
    from apps.supplier_preparation_intents_smoke import HEADER,LINES
    from apps.cny_ledger_smoke import _save_payment
    from packages.application.cny_ledger import CnyLedgerBlock
    from packages.application import supplier_preparation_intents as preparation,operator_supplier_financial as financial,operator_supplier_shipments as source
    tmp=TemporaryDirectory(prefix='supplier-cycle-cny-');case.addCleanup(tmp.cleanup);rt=seed(tmp.name)
    bundle=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text())
    bundle['bundle_version']='supplier-cycle-one-sku';bundle['config_v2']=[{**bundle['config_v2'][0],'nm_id':1,'display_name':'Fixture'}]
    rt.ingest_bundle(bundle,activated_at=NOW)
    rt.save_supplier_shipment(header={**HEADER,'shipment_id':'source','created_at':NOW,'updated_at':NOW,'invoice_date':FIRST,'shipment_date':FIRST,'actual_shipment_date':FIRST,'invoice_no':'paid','expenses_complete':0,'invoice_amount_total':100,'product_amount_total':100,'product_qty_total':10},lines=[{**LINES[0],'internal_nm_id':1}])
    ledger=CnyLedgerBlock(runtime=rt,timestamp_factory=lambda:NOW)
    ledger.create_opening_balance({'operation_date':FIRST,'cny_amount':100,'rub_value':1000})
    _save_payment(rt,'initial-payment','source',FIRST+'T10:00:00Z','90');ledger.replay_ledger(reason='initial fixture')
    preparation.drain_supplier_preparation_intents(rt,shipment_ids=['source']);publish_functional(rt)
    original=install_native_saved_dates(rt)
    payment_date='2026-07-10T10:00:00Z'
    # Native03 bank transfer save owns its exact CNY companion in one source TX.
    result=financial.execute(rt,action='confirm_upload',payload={'request_id':'late-financial-cycle'},shipment_id='source',request_scope='alice-key',actor='alice',
        manifest=[{'child_key':'payment','kind':'financial','subject_id':'late-financial-payment'}],validate=lambda:['source'],
        write_child=lambda child:rt.save_supplier_financial_document(document=dict(document_id=child['subject_id'],supplier_order_id='source',
            document_type='bank_transfer_application',uploaded_at=NOW,updated_at=NOW,document_date='2026-07-10',parse_status='confirmed',
            total_amount=10,currency='CNY',file_sha256='a'*64,normalized_parse={'currency':'CNY','transfer_amount':'10','operation_date':'2026-07-10','operation_datetime':payment_date}),expense_lines=[]))
    case.assertTrue(result['acceptance']['durable_saved']);case.assertFalse(result['acceptance']['processing']['complete'])
    parent=result['results'][0]['acceptance']['operation_id']
    with closing(source.readonly(rt.db_path)) as conn:
        identity=conn.execute(f'SELECT operation_id FROM {financial.SCOPES} WHERE parent_operation_id=?',(parent,)).fetchone()[0]
    ledger.replay_ledger(reason='native deferred CNY consumer')
    preparation.drain_supplier_preparation_intents(rt,shipment_ids=['source']);publish_functional(rt);publish_accounting(rt)
    return rt,identity,parent,original


def current_ready(rt):
    from packages.application import ready_publication as ready
    plan=rt.load_sheet_vitrina_ready_snapshot();state=rt.load_current_state()
    expected=rt.prepare_sheet_vitrina_ready_publication(bundle_version=state.bundle_version,as_of_date=plan.as_of_date)
    with ready.readonly(rt.db_path) as conn:
        publication=conn.execute("SELECT operation_id,attempt_id FROM sheet_vitrina_v1_ready_publications WHERE state='complete' AND after_digest=? ORDER BY rowid DESC LIMIT 1",(ready.digest(expected.plan_json),)).fetchone()
        row=conn.execute('SELECT refreshed_at FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',(state.bundle_version,plan.as_of_date)).fetchone()
    return dict(snapshot_id=plan.snapshot_id,plan_version=plan.plan_version,bundle_version=state.bundle_version,as_of_date=plan.as_of_date,
        ready_fingerprint=expected.fingerprint,refreshed_at=row[0],publication_operation_id=publication[0],publication_attempt_id=publication[1])


class FinanceNoChangeTests(unittest.TestCase):
    def test_actual_native_images_only_two_stamps_and_adversarial_operands(self):
        from packages.application import operator_supplier_history as history,fbs_accounting_runtime as accounting
        from packages.application.operator_supplier_cost_proof import _finance_business_image,_finance_retained_stamp_no_change
        from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        from apps.wb_finance_weekly_cost_cutover_smoke import _row
        rt,identity,parent,original=setup_cny(self)
        finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT);finance.ensure_schema()
        finance.ingest_week(date(2026,7,20),date(2026,7,26),[_row(1,DAY,nm_id=1)])
        with admitted_write(rt.runtime_dir),heavy_admitted(rt.runtime_dir,operation='cycle'),warehouse_functional_job_lock(rt.runtime_dir):
            history.publish(rt,history.prepare(rt,identity,now=MOMENT))
            self.assertEqual(WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT).recalculate_stale_cost_weeks()['status'],'already_current')
        probe=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical');targets={('canonical','2026-07-20','2026-07-26')}
        with closing(probe._connect_stale_cost_plan()) as conn:
            current=_finance_business_image(probe._finance_target_images(conn,targets))
            expected=_finance_business_image(probe._canonicalize_finance_target_images(conn,probe._build_week_target_projection(conn,week_start=date(2026,7,20),week_end=date(2026,7,26))['images']))
        version=accounting.load_shared(rt.runtime_dir).metadata()['version_id']
        differences=_finance_retained_stamp_no_change(current,expected,shared_version=version)
        self.assertTrue(differences)
        self.assertEqual({d['path'] for d in differences},{'quality_json.shared_cost.version_id','coverage_json.quality.shared_cost.version_id'})
        self.assertTrue(all(d['current_native_shared_version_id']==version and d['retained_finance_version_id']!=version for d in differences))
        coverage='wb_finance_weekly_cost_coverage';sku='wb_finance_weekly_sku_aggregates'
        sku_index=next(i for i,row in enumerate(current[sku]) if str(row['nm_id'])=='1')
        self.assertTrue(current[sku][sku_index]['coverage_json']['detail_rows'])
        mutations={
            'money':lambda c:c[coverage][0]['coverage_json'].__setitem__('cogs_rub','999'),
            'costhash':lambda c:c[sku][sku_index]['coverage_json'].__setitem__('cost_state_hash','foreign'),
            'sourceID':lambda c:c[sku][sku_index]['coverage_json']['detail_rows'][0].__setitem__('source_identity','foreign'),
            'sourceDigest':lambda c:c[sku][sku_index]['coverage_json']['detail_rows'][0].__setitem__('source_digest','sha256:'+'b'*64),
            'quality':lambda c:c[sku][sku_index]['coverage_json']['detail_rows'][0].__setitem__('source_quality','foreign'),
            'formula':lambda c:c[sku][sku_index]['coverage_json']['cost_economic_buckets'][0].__setitem__('formula_version','foreign'),
            'date':lambda c:c[sku][sku_index]['coverage_json']['cost_economic_buckets'][0].__setitem__('operation_date','2026-07-23'),
            'facility':lambda c:c[sku][sku_index]['coverage_json']['cost_economic_buckets'][0].__setitem__('facility_id','foreign'),
            'unknownMetadata':lambda c:c[coverage][0]['quality_json']['shared_cost'].__setitem__('foreign_version_id','sha256:'+'c'*64),
            'unknownVersionPath':lambda c:c[sku][sku_index]['coverage_json'].__setitem__('other',{'version_id':'sha256:'+'c'*64}),
            'invalidFingerprint':lambda c:c[coverage][0]['quality_json']['shared_cost'].__setitem__('version_id','not-a-fingerprint'),
            'partialCopies':lambda c:c[coverage][0]['coverage_json']['quality']['shared_cost'].__setitem__('version_id','sha256:'+'c'*64),
        }
        for name,mutate in mutations.items():
            with self.subTest(operand=name):
                changed=deepcopy(current);mutate(changed)
                with self.assertRaisesRegex(ValueError,'supplier_finance_'):
                    _finance_retained_stamp_no_change(changed,expected,shared_version=version)
        with self.assertRaisesRegex(ValueError,'supplier_finance_global_stamp_unproven'):
            _finance_retained_stamp_no_change(current,expected,shared_version='sha256:'+'d'*64)
        both_current,both_expected=deepcopy(current),deepcopy(expected)
        for image in (both_current,both_expected):
            image[coverage][0]['quality_json']['shared_cost']['unknown']='same foreign metadata'
            image[coverage][0]['coverage_json']['quality']['shared_cost']['unknown']='same foreign metadata'
        with self.assertRaisesRegex(ValueError,'supplier_finance_global_stamp_unproven'):
            _finance_retained_stamp_no_change(both_current,both_expected,shared_version=version)


class LinuxTests(unittest.TestCase):
    def test_actual_late_cny_same_cycle_owned_history_finance_parent_completed(self):self.run_native()
    def test_source_race_after_history_blocks_same_cycle_completion(self):self.run_native(race='source')
    def test_ready_race_after_history_blocks_same_cycle_completion(self):self.run_native(race='ready')
    def test_unchanged_sale_exact_no_change_completes_without_finance_stamp_write(self):self.run_native(unchanged_sale=True)
    def run_native(self,*,race=None,unchanged_sale=False):
        from packages.application import operator_supplier_history as history,operator_supplier_financial as financial,operator_supplier_processing as processing,ready_publication as ready
        from packages.application.registry_upload_db_backed_runtime import _connect
        from packages.application.web_vitrina_snapshot_admission import ApiJobMarkers
        from packages.application.own_product_capital import OwnProductCapitalBlock
        from packages.application.calculation_parameters import CalculationParametersBlock
        from packages.application.calculation_parameters_v4 import ProxyV4ParametersBlock
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        from apps.wb_finance_weekly_cost_cutover_smoke import _row
        from apps.web_vitrina_history_candidate_build import build_owned_cycle_history
        rt,identity,parent,original=setup_cny(self)
        OwnProductCapitalBlock(runtime=rt);CalculationParametersBlock(runtime=rt).ensure_initial_version(created_at=NOW,created_by='synthetic CNY cycle')
        ProxyV4ParametersBlock(runtime=rt,now_factory=lambda:MOMENT)
        finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT);finance.ensure_schema()
        finance.ingest_week(date(2026,7,20),date(2026,7,26),[_row(1,DAY if unchanged_sale else '2026-07-20',nm_id=1)])
        from packages.application.operator_supplier_cost_proof import _finance_business_image
        finance_targets={('canonical','2026-07-20','2026-07-26')}
        with closing(finance._connect_stale_cost_plan()) as conn:
            original_finance=_finance_business_image(finance._finance_target_images(conn,finance_targets))
        before=current_ready(rt)
        with TemporaryDirectory(prefix='supplier-cycle-supervisor-') as raw,closing(sqlite3.connect(rt.db_path)) as keeper:
            keeper.execute('PRAGMA journal_mode=WAL');keeper.commit();keeper.execute('SELECT 1 FROM sqlite_master').fetchone()
            root=Path(raw)/'candidate';root.mkdir(mode=0o700)
            for name in ('.web-vitrina-finished-builder.lock','.wb-finance-daily-worker.lock'):(rt.runtime_dir/name).touch(mode=0o600)
            markers=ApiJobMarkers(rt.runtime_dir);marker=markers.start('supplier-cycle','cycle');self.addCleanup(lambda:markers.finish(marker))
            contract=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json').read_text());contract['candidate_root']=str(root)
            contract['formula_code_hashes']={relative:hashlib.sha256((ROOT/relative).read_bytes()).hexdigest() for relative in contract['formula_code_hashes']}
            contract['formula_epoch']='wbc0069k16-reviewed-native-v1:'+hashlib.sha256(json.dumps(contract['formula_code_hashes'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
            path=root/'contract.json';path.write_text(json.dumps(contract))
            config=SimpleNamespace(candidate_root=root,runtime_contract=path,formula_epoch=contract['formula_epoch'],budget_seconds=120,max_recomputes=4)
            entry=host(rt);token=SHEET_OPERATOR_JOB_ID.set('supplier-cycle');self.addCleanup(SHEET_OPERATOR_JOB_ID.reset,token)
            captured=[]
            def actual_build(**kwargs):
                captured.append(kwargs['supplier_receipt']);value=build_owned_cycle_history(**kwargs)
                if race:
                    with _connect(rt.db_path) as conn:
                        if race=='source':conn.execute("UPDATE sheet_vitrina_v1_supplier_financial_documents SET total_amount='11' WHERE document_id='late-financial-payment'")
                        else:
                            row=conn.execute('SELECT bundle_version,as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?',(DAY,)).fetchone();payload=json.loads(row[2])
                            column=2+payload['date_columns'].index(kwargs['supplier_receipt'].dates[0]);payload['sheets'][0]['rows'][0][column]=99999
                            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE bundle_version=? AND as_of_date=?',(json.dumps(payload,separators=(',',':')),row[0],row[1]))
                        conn.commit()
                return value
            # Match the production helper's fresh native Finance factory after
            # History publication; an ingest-time block retains old cost cache.
            with admitted_write(rt.runtime_dir),heavy_admitted(rt.runtime_dir,operation='cycle'),patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'),patch('apps.web_vitrina_finished_snapshot_build.systemd_admission',return_value='idle'),patch('apps.web_vitrina_history_candidate_build.build_owned_cycle_history',side_effect=actual_build),patch('apps.warehouse_functional_runner.block_from_env',side_effect=lambda path:WbFinanceWeeklyBlock(path,seller_id='canonical',now_factory=lambda:MOMENT)):
                if race:
                    with self.assertRaisesRegex(Exception,'query_input_changed|dated_ready_changed|native_source_changed|source_changed'):entry._cycle_history(config,cycle_receipt(),before)
                else:result=entry._cycle_history(config,cycle_receipt(),before)
            self.assertEqual(len(captured),1);receipt=captured[0]
            self.assertIn('2026-07-10',receipt.dates);self.assertIn('2026-07-11',receipt.dates)
            with ready.readonly(rt.db_path) as conn:self.assertIsNotNone(history.completed_proof(conn,receipt.manifest['source_ref']))
            saved=financial.read_request(rt.runtime_dir,rt.db_path,'late-financial-cycle',request_scope='alice-key')
            acceptance=saved['results'][0]['acceptance']
            self.assertEqual(acceptance['operation_id'],parent);self.assertEqual(acceptance['domain'],financial.DOMAIN)
            if race:
                self.assertFalse(acceptance['processing']['complete']);self.assertNotEqual(acceptance['state'],'completed')
            else:
                completion=json.loads(result.versions['history_supplier_completion'])
                self.assertTrue(acceptance['financial_applied']);self.assertTrue(acceptance['processing']['complete'],json.dumps({'acceptance':acceptance,'completion':completion},ensure_ascii=False));self.assertEqual(acceptance['state'],'completed')
                self.assertEqual(completion['post_verify_stale_week_count'],0);self.assertTrue(completion['accounting_version_unchanged'])
                self.assertTrue(next(v for v in completion['supplier_operation_completion']['operations'] if v['operation_id']==identity)['complete'])
                with ready.readonly(rt.db_path) as conn:
                    retained=json.loads(conn.execute(f'SELECT proof_json FROM {processing.COMPLETIONS} WHERE operation_id=?',(identity,)).fetchone()[0])
                if unchanged_sale:
                    self.assertEqual(completion['status'],'already_current');self.assertEqual(completion['recalculated_week_count'],0)
                    self.assertEqual(retained['finance']['projection_outcome'],'derived_no_change')
                    self.assertTrue(retained['finance']['retained_global_stamp_differences'])
                    self.assertNotEqual(retained['finance']['target_digest'],retained['finance']['current_native_expected_target_digest'])
                    with closing(finance._connect_stale_cost_plan()) as conn:
                        self.assertEqual(_finance_business_image(finance._finance_target_images(conn,finance_targets)),original_finance)
                else:
                    self.assertEqual(completion['status'],'applied');self.assertEqual(completion['recalculated_week_count'],1)
                with self.assertRaises(Exception):entry._cycle_verify_ready(before)


if __name__=='__main__':
    from ci.parallel_unittest import main
    main(sys.modules[__name__])
