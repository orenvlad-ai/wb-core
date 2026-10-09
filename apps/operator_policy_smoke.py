"""Policy acceptance and actual native consumers; synthetic sources only."""
from contextlib import ExitStack,closing
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json,sqlite3,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.warehouse_fbs_material_rematerialization_smoke import _seed,DAY,NOW
from apps.business_data_heavy_producers_smoke import busy
from apps.ready_publication_fixture import save_ready_fixture
from apps import vitrina_incident_rematerialization_smoke as incident_fixture
from packages.application import operator_policy as op
from packages.application.calculation_parameters import CalculationParametersBlock
from packages.application.calculation_parameters_v4 import ProxyV4ParametersBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sku_management import SkuManagementBlock
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.contracts.stocks_block import StocksSuccess,StocksItem,StocksWarehouseRow

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.runtime=_seed(Path(self.tmp.name),mixed=False)
        self.entry=SimpleNamespace(runtime=self.runtime,now_factory=lambda:datetime(2026,8,26,12,tzinfo=timezone.utc),activated_at_factory=lambda:NOW)
        self.entry.calculation_parameters_block=CalculationParametersBlock(runtime=self.runtime)
        self.entry.proxy_v4_parameters_block=ProxyV4ParametersBlock(runtime=self.runtime,now_factory=self.entry.now_factory)
        self.counter=0
    def identity(self):self.counter+=1;return 'oppolicy_'+format(self.counter,'032x')
    def accept(self,kind='legacy_proxy',**extra):
        payload=({'effective_date':DAY,'buyout_rate':'1','tax_rate':'0.1'} if kind=='legacy_proxy' else {'tax_rate':'0.1'})
        payload.update(extra)
        block=self.entry.calculation_parameters_block if kind=='legacy_proxy' else self.entry.proxy_v4_parameters_block
        preview=block.preview_version(payload) if kind=='legacy_proxy' else block.preview_tax_version(payload)
        payload['preview_fingerprint']=preview['preview_fingerprint'];identity=self.identity()
        return op.accept(self.entry,kind,payload,actor='operator',operation_id=identity),payload,identity
    def drain(self):
        with patch('packages.application.warehouse_functional_economics_backfill.current_business_date_iso',return_value=DAY),heavy_admitted(self.runtime.runtime_dir,operation='fixture'):
            return op.drain(self.entry)
    def read(self,identity):return op.read(self.runtime.db_path,identity,actor='operator')
    def count(self,kind):
        with sqlite3.connect(self.runtime.db_path) as conn:return conn.execute('SELECT COUNT(*) FROM '+op.NATIVE[kind]).fetchone()[0]
    def test_busy_final_acceptance_has_no_native_or_heavy_effect_then_real_proof(self):
        before=self.count('legacy_proxy')
        with busy(self.runtime.runtime_dir),patch.object(self.entry.calculation_parameters_block,'process_pending_targeted_recalculations',side_effect=AssertionError('HTTP heavy')):
            result,payload,identity=self.accept()
            again=op.accept(self.entry,'legacy_proxy',payload,actor='operator',operation_id=identity)
        self.assertEqual(again,result);self.assertEqual(self.count('legacy_proxy'),before)
        self.assertTrue(result['acceptance']['durable_saved']);self.assertIsNone(result['acceptance']['source_ref']['native_identity'])
        self.assertIsNone(op.read(self.runtime.db_path,identity,actor='foreign'))
        with self.assertRaisesRegex(ValueError,'identity_conflict'):op.accept(self.entry,'legacy_proxy',{**payload,'tax_rate':'0.2'},actor='operator',operation_id=identity)
        self.drain();receipt=self.read(identity)
        self.assertEqual(receipt['state'],'completed');self.assertEqual(receipt['processing_receipt']['publication']['consumer'],'native_functional_economics')
        self.assertEqual(receipt['processing_receipt']['publication']['dates'],[DAY]);self.assertEqual(self.count('legacy_proxy'),before+1)
    def test_current_v4_tax_exact_native_publication(self):
        result,payload,identity=self.accept('proxy_v4_tax');self.drain()
        receipt=self.read(identity);self.assertEqual(receipt['state'],'completed')
        proof=receipt['processing_receipt']['publication'];self.assertEqual(proof['parameter_dependencies'][DAY]['proxy4_version'],proof['exact_version'])
        self.assertTrue(proof['parameter_dependencies'][DAY]['proxy4_fingerprint'].startswith('sha256:'))
    def test_competing_command_and_native_source_drift_cannot_claim_old_version(self):
        first,payload,identity=self.accept()
        with self.assertRaisesRegex(ValueError,'pending_command_conflict'):self.accept(tax_rate='0.2')
        native=self.entry.calculation_parameters_block
        newer={**payload,'tax_rate':'0.3'};newer['preview_fingerprint']=native.preview_version(newer)['preview_fingerprint']
        with patch.object(native,'process_pending_targeted_recalculations',return_value={'status':'complete','request_count':1}):native.create_version(newer,preview_fingerprint=newer['preview_fingerprint'],created_by='foreign')
        count=self.count('legacy_proxy');self.drain();self.assertEqual(self.count('legacy_proxy'),count)
        self.assertEqual(self.read(identity)['state'],'needs_attention');self.assertEqual(self.read(identity)['reason_code'],'native_source_drift')
    def test_crash_after_native_source_binding_does_not_insert_second_version(self):
        first,payload,identity=self.accept();native=self.entry.calculation_parameters_block.create_version
        def lose(*args,**kwargs):native(*args,**kwargs);raise RuntimeError('source response lost')
        with patch.object(self.entry.calculation_parameters_block,'create_version',side_effect=lose):self.drain()
        count=self.count('legacy_proxy');self.assertIsNotNone(self.read(identity)['source_ref']['native_identity'])
        self.drain();self.assertEqual(self.count('legacy_proxy'),count);self.assertEqual(self.read(identity)['state'],'completed')
    def test_crash_after_actual_economics_commit_reconciles_exact_no_change(self):
        result,payload,identity=self.accept()
        with patch.object(op,'finish_publication',side_effect=RuntimeError('after native publication')):self.drain()
        count=self.count('legacy_proxy');self.assertEqual(self.read(identity)['state'],'processing')
        self.drain();self.assertEqual(self.count('legacy_proxy'),count);self.assertEqual(self.read(identity)['state'],'completed')
    def test_acceptance_after_cutoff_remains_for_next_owned_pass(self):
        first,payload,identity=self.accept();native=op._source_apply;late=[]
        def accept_late(entry,command):
            result=native(entry,command)
            if not late:late.append(self.accept('proxy_v4_tax'))
            return result
        with patch.object(op,'_source_apply',side_effect=accept_late):self.drain()
        late_id=late[0][2];self.assertIsNone(self.read(late_id)['source_ref']['native_identity']);self.assertEqual(self.read(late_id)['state'],'processing')
        self.drain();self.assertEqual(self.read(late_id)['state'],'completed')
    def test_immutable_final_intent_and_missing_proof_not_completed(self):
        result,payload,identity=self.accept();self.drain()
        with sqlite3.connect(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):conn.execute(f'UPDATE {op.TABLE} SET actor=? WHERE operation_id=?',('foreign',identity))
            with self.assertRaisesRegex(sqlite3.IntegrityError,'append-only'):conn.execute(f'DELETE FROM {op.TABLE} WHERE operation_id=?',(identity,))
            conn.execute('DROP TRIGGER operator_policy_binding_immutable')
            stored=json.loads(conn.execute(f"SELECT native_json FROM {op.BINDINGS} WHERE operation_id=? AND role='publication'",(identity,)).fetchone()[0]);stored.pop('readback_verified')
            conn.execute(f"UPDATE {op.BINDINGS} SET native_json=? WHERE operation_id=? AND role='publication'",(json.dumps(stored),identity));conn.commit()
        self.assertEqual(self.read(identity)['state'],'needs_attention')
    def test_source_and_native_binding_commit_atomically(self):
        first,payload,identity=self.accept();before=self.count('legacy_proxy')
        with patch.object(op,'bind_native',side_effect=RuntimeError('binding commit failed')):self.drain()
        self.assertEqual(self.count('legacy_proxy'),before);self.assertIsNone(self.read(identity)['source_ref']['native_identity'])
        self.drain();self.assertEqual(self.count('legacy_proxy'),before+1);self.assertEqual(self.read(identity)['state'],'completed')
    def test_late_source_race_inside_native_transaction_is_rejected(self):
        first,payload,identity=self.accept();before=self.count('legacy_proxy');guard=op.before_native
        def drift(conn,command):
            conn.execute('UPDATE '+op.NATIVE['legacy_proxy']+' SET fingerprint=?',('sha256:foreign-late',))
            guard(conn,command)
        with patch.object(op,'before_native',side_effect=drift):self.drain()
        self.assertEqual(self.count('legacy_proxy'),before);self.assertIsNone(self.read(identity)['source_ref']['native_identity'])
        self.assertEqual(self.read(identity)['state'],'needs_attention')
    def test_invalid_intrinsic_source_leaves_no_receipt(self):
        identity=self.identity()
        with self.assertRaises(ValueError):op.accept(self.entry,'legacy_proxy',{'effective_date':'2026-06-30','tax_rate':'-1'},actor='operator',operation_id=identity)
        self.assertTrue(op.source_not_saved(self.runtime.db_path,identity));self.assertIsNone(self.read(identity))
    def test_v4_delayed_apply_preserves_authorized_original_current_date(self):
        first,payload,identity=self.accept('proxy_v4_tax')
        self.entry.now_factory=lambda:datetime(2026,8,27,12,tzinfo=timezone.utc)
        self.drain();receipt=self.read(identity)
        self.assertEqual(receipt['fields'][0]['value'],DAY);self.assertEqual(receipt['source_ref']['native_identity'],receipt['processing_receipt']['native']['identity'])
        self.assertEqual(receipt['processing_receipt']['native']['row']['effective_date'],DAY)
    def test_existing_identity_never_gets_unsaved_rejection_evidence(self):
        first,payload,identity=self.accept();self.assertFalse(op.source_not_saved(self.runtime.db_path,identity))
        self.assertIsNone(op.read_bound(self.runtime.db_path,identity,actor='operator',kind='legacy_proxy',payload={**payload,'tax_rate':'0.5'}))

    def test_closed_date_keeps_source_date_and_requires_history_authority(self):
        first,payload,identity=self.accept()
        self.entry.now_factory=lambda:datetime(2026,9,1,12,tzinfo=timezone.utc)
        with patch('packages.application.warehouse_functional_economics_backfill.current_business_date_iso',return_value='2026-09-01'),heavy_admitted(self.runtime.runtime_dir,operation='fixture'):op.drain(self.entry)
        receipt=self.read(identity);self.assertEqual(receipt['state'],'needs_attention',receipt)
        self.assertEqual(receipt['reason_code'],'policy_historical_authority_required');self.assertEqual(receipt['fields'][0]['value'],DAY)
        self.assertIn(DAY,receipt['processing_receipt']['progress']['uncovered_dates'])

    def test_exact_queue_ack_and_completion_commit_together(self):
        first,payload,identity=self.accept()
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute("CREATE TRIGGER fixture_queue_ack_failure BEFORE UPDATE OF status ON sheet_vitrina_v1_proxy_targeted_recalc_queue WHEN NEW.status='complete' BEGIN SELECT RAISE(ABORT,'queue ack failed');END")
        self.drain();receipt=self.read(identity);self.assertNotEqual(receipt['state'],'completed');self.assertIsNone(receipt['processing_receipt']['publication']);count=self.count('legacy_proxy')
        with sqlite3.connect(self.runtime.db_path) as conn:conn.execute('DROP TRIGGER fixture_queue_ack_failure')
        self.drain();receipt=self.read(identity);self.assertEqual(receipt['state'],'completed');self.assertEqual(self.count('legacy_proxy'),count)
        native=receipt['processing_receipt']['native']
        with sqlite3.connect(self.runtime.db_path) as conn:self.assertEqual(conn.execute('SELECT status FROM sheet_vitrina_v1_proxy_targeted_recalc_queue WHERE request_id=? AND settings_version_id=?',(native['queue_id'],native['identity'])).fetchone()[0],'complete')

    def test_unknown_get_never_bootstraps(self):
        path=Path(self.tmp.name)/'missing.sqlite3';self.assertIsNone(op.read(path,'oppolicy_'+'f'*32,actor='operator'));self.assertFalse(path.exists())

class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(self.tmp.name))
        self.runtime.ingest_bundle(json.loads(incident_fixture.BUNDLE_FIXTURE.read_text()),activated_at=NOW)
        current=self.runtime.load_current_state();self.ids=[int(item.nm_id) for item in current.config_v2 if item.enabled][:2]
        with patch.object(incident_fixture,'TARGET_DATE',DAY):plan=incident_fixture._plan(self.ids)
        save_ready_fixture(self.runtime,current_state=current,refreshed_at=NOW,plan=plan)
        self.entry=SimpleNamespace(runtime=self.runtime,now_factory=lambda:datetime(2026,8,26,12,tzinfo=timezone.utc),activated_at_factory=lambda:NOW)
        self.entry.sku_management_block=SkuManagementBlock.__new__(SkuManagementBlock)
        block=self.entry.sku_management_block;block.runtime=self.runtime;block.now_factory=self.entry.now_factory;block.timestamp_factory=lambda:NOW
        block.stocks_block=None
        self.save_stocks();self.identity='oppolicy_'+'a'*32
    def save_stocks(self):
        items=[StocksItem(nm_id=nm,stock_total=15,stock_ru_central=15,stock_ru_northwest=0,stock_ru_volga=0,stock_ru_ural=0,stock_ru_south_caucasus=0,stock_ru_far_siberia=0) for nm in self.ids]
        rows=[StocksWarehouseRow(nm_id=nm,warehouse_id=wid,warehouse_name=name,region_name='Центральный',quantity=qty,planning_zone_key='central_north',classification_status='mapped',classification_source='fixture') for nm in self.ids for wid,name,qty in [(101,'Альфа',10),(102,'Бета',5)]]
        success=StocksSuccess(kind='success',count=len(items),items=items,warehouse_rows=rows,snapshot_date=DAY,fetched_at=NOW,pagination_complete=True,raw_rows_digest='sha256:fixture')
        self.runtime.save_temporal_source_snapshot(source_key='stocks',snapshot_date=DAY,captured_at=NOW,payload=success)
    def accept(self):
        payload={'base_revision':0,'active':True,'excluded_wb_warehouse_ids':[101],'reason':'fixture','effective_from':DAY,'effective_to':'','status':'active'}
        return op.accept(self.entry,'wb_incident_policy',payload,actor='operator',operation_id=self.identity)
    def drain(self):
        with heavy_admitted(self.runtime.runtime_dir,operation='fixture'):return op.drain(self.entry)
    def read(self):return op.read(self.runtime.db_path,self.identity,actor='operator')
    def test_acceptance_then_native_revision_and_exact_actual_projection(self):
        accepted=self.accept();self.assertIsNone(accepted['acceptance']['source_ref']['native_identity'])
        self.drain();receipt=self.read();self.assertEqual(receipt['state'],'completed',receipt['processing_receipt']['progress'])
        self.assertEqual(receipt['processing_receipt']['publication']['consumer'],'native_vitrina_incident_rematerialization')
        with sqlite3.connect(self.runtime.db_path) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_wb_incident_policy_revisions').fetchone()[0],1)
    def test_failure_restores_last_good_and_retries_only_own_recovery(self):
        self.accept()
        with patch('packages.application.vitrina_incident_rematerialization.apply_vitrina_incident_rematerialization',side_effect=RuntimeError('publisher failed')):self.drain()
        self.assertEqual(self.read()['reason_code'],'native_last_good_restored')
        from packages.application.wb_incident_policy import canonical_seller_id
        policy=self.runtime.load_latest_wb_incident_policy(seller_id=canonical_seller_id());self.assertFalse(policy['active'])
        self.drain();self.assertEqual(self.read()['state'],'completed',self.read()['processing_receipt']['progress'])
    def test_crash_after_actual_publication_never_restores_just_for_lost_ack(self):
        self.accept()
        with patch.object(op,'finish_publication',side_effect=RuntimeError('ack lost')):self.drain()
        with sqlite3.connect(self.runtime.db_path) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_wb_incident_policy_revisions').fetchone()[0],1)
        self.drain();self.assertEqual(self.read()['state'],'completed',self.read()['processing_receipt']['progress'])

    def test_options_not_ready_accepts_without_provider_then_native_continuation(self):
        with sqlite3.connect(self.runtime.db_path) as conn:conn.execute("DELETE FROM temporal_source_snapshots WHERE source_key='stocks'");conn.commit()
        with patch.object(op,'incident_options',return_value=None):self.accept()
        self.assertIsNone(self.read()['source_ref']['native_identity']);self.drain()
        self.assertEqual(self.read()['state'],'processing');self.assertEqual(self.read()['reason_code'],'native_apply_retry_pending')
        self.save_stocks();self.drain();self.assertEqual(self.read()['state'],'completed')
    def test_foreign_revision_after_own_recovery_cannot_be_overwritten(self):
        self.accept()
        with patch('packages.application.vitrina_incident_rematerialization.apply_vitrina_incident_rematerialization',side_effect=RuntimeError('publisher failed')):self.drain()
        from packages.application.wb_incident_policy import canonical_seller_id,save_policy_revision
        latest=self.runtime.load_latest_wb_incident_policy(seller_id=canonical_seller_id())
        save_policy_revision(self.runtime,payload={'base_revision':latest['revision'],'active':True,'excluded_wb_warehouse_ids':[102],'reason':'foreign','effective_from':DAY,'effective_to':'','status':'active'},actor='foreign',warehouse_options=op.incident_options(self.runtime,DAY),timestamp=NOW)
        before=self.runtime.load_latest_wb_incident_policy(seller_id=canonical_seller_id())['revision'];self.drain()
        self.assertEqual(self.runtime.load_latest_wb_incident_policy(seller_id=canonical_seller_id())['revision'],before)
        self.assertEqual(self.read()['state'],'needs_attention')

    def test_older_native_incident_dates_are_not_silently_clamped_completed(self):
        payload={'base_revision':0,'active':True,'excluded_wb_warehouse_ids':[101],'reason':'fixture','effective_from':'2026-08-01','effective_to':'','status':'active'}
        op.accept(self.entry,'wb_incident_policy',payload,actor='operator',operation_id=self.identity);self.drain()
        receipt=self.read();self.assertEqual(receipt['state'],'needs_attention');self.assertEqual(receipt['reason_code'],'policy_historical_authority_required')
        self.assertEqual(receipt['fields'][0]['value'],'2026-08-01');self.assertEqual(receipt['processing_receipt']['progress']['date_from_requested'],'2026-08-01')

if __name__=='__main__':unittest.main()
