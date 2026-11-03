"""History-only admission, one-submit fences and actual metadata finalization."""
from contextlib import closing,contextmanager,nullcontext
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application import historical_receipt_completion as tail
from packages.application import ready_publication as ready
from packages.application import fbs_accounting_runtime as accounting
from packages.application.business_data_procedure_admission import AdmissionLease,initialize_admission,admission_idle,already_admitted
from apps.production_apply_contract import AmbiguousSubmit


class TailGuardTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.request=dict(publication_operation_id='original-publication',publication_attempt_id='1')

    def test_cross_operation_claim_blocks_preview_and_apply_without_native_work(self):
        tail._exclusive_private(tail._claim_path(self.root,self.request),dict(contract=tail.CONTRACT,operation_id='tail-first-op',request=self.request))
        adapter=tail.HistoricalReceiptCompletionAdapter()
        with patch('packages.application.fbs_accounting_apply.target',return_value=self.root),patch.object(tail,'_snapshot') as snapshot,patch.object(tail,'_held',return_value=nullcontext({})),patch('packages.application.owned_history_worker.historical_receipt_history_worker') as worker:
            with self.assertRaisesRegex(ValueError,'publication_already_submitted'):
                adapter.preview(self.request,'tail-second-op')
            with self.assertRaisesRegex(ValueError,'publication_already_submitted'):
                adapter.apply(self.request,'tail-second-op',{})
            snapshot.assert_not_called();worker.assert_not_called()
        # Claim-before-marker crash is uncertain, never "not submitted".
        with patch('packages.application.fbs_accounting_apply.target',return_value=self.root):
            self.assertEqual(adapter.readback(self.request,'tail-first-op')['state'],'ambiguous')

    def test_held_admission_requires_live_registered_scope_and_actual_sh(self):
        initialize_admission(self.root)
        with self.assertRaisesRegex(ValueError,'tail_held_scope_required'):
            AdmissionLease.held_historical_receipt_completion(self.root)
        held={'window_id':'fixture-window'}
        token=tail._HELD.set((os.getpid(),self.root,self.request,held))
        try:
            with patch.object(tail,'_held_binding',return_value=held):
                with tail._admitted(self.root):
                    self.assertTrue(already_admitted(self.root))
                    self.assertFalse(admission_idle(self.root)['idle'])
                self.assertTrue(admission_idle(self.root)['idle'])
            with patch.object(tail,'_held_binding',return_value={'window_id':'changed'}):
                with self.assertRaisesRegex(ValueError,'tail_maintenance_changed'):
                    AdmissionLease.held_historical_receipt_completion(self.root)
        finally:tail._HELD.reset(token)
        self.assertTrue(admission_idle(self.root)['idle'])

    def test_original_cycle_config_uses_exact_native_paths_and_240_budget(self):
        from packages.application.sheet_vitrina_v1_cycle import CycleHistoryConfig
        from packages.application.owned_history_worker_capability import file_digest
        from packages.business_time import current_business_date_iso
        source=self.root/'installed-contract.json';source.write_text('{"formula_epoch":"source-epoch"}')
        retained=self.root/'retained-source-contract.json';retained.write_bytes(source.read_bytes())
        candidate=self.root/'candidate'
        native=CycleHistoryConfig(candidate,source,'source-epoch',budget_seconds=240,max_recomputes=31)
        cycle=dict(cycle_id='a'*32,owner_pid=123456,process_identity='dead-generation',
            business_date=current_business_date_iso(datetime.now(timezone.utc)),history_config_fingerprint=native.fingerprint(),
            stages=[dict(stage=name,status='running' if name=='rolling14' else 'complete',finished_at='fixture',versions={'native':'proof'}) for name in tail.STAGES])
        directory=self.root/'sheet-vitrina-cycles';directory.mkdir();path=directory/(cycle['cycle_id']+'.json');path.write_text(json.dumps(cycle))
        request=dict(original_cycle_id=cycle['cycle_id'],original_cycle_sha256='sha256:'+file_digest(path),
            source_runtime_contract=str(retained),source_runtime_contract_sha256='sha256:'+file_digest(retained),
            runtime_contract=str(source),source_runtime_contract_original_path=str(source),candidate_root=str(candidate),
            source_formula_epoch='source-epoch',source_history_budget_seconds=240,source_history_max_recomputes=31)
        with patch('packages.application.web_vitrina_snapshot_admission.process_identity',return_value=None):
            self.assertEqual(tail._cycle(self.root,request),cycle)
            with self.assertRaisesRegex(ValueError,'tail_original_history_config_changed'):
                tail._cycle(self.root,{**request,'source_history_budget_seconds':180})

    def test_wrong_owner_or_changed_marker_is_refused(self):
        owner=dict(contract=tail.CONTRACT,operation=tail.OPERATION,operation_id='fixture-operation',pid=os.getpid(),identity='generation',held={})
        tail._exclusive_private(tail._path(self.root,owner['operation_id']),dict(owner=owner,phase='submitted',request=self.request))
        with patch.object(tail,'process_generation',return_value='generation'),patch.object(tail,'_held_binding',return_value={}):
            tail.check_live_owner(self.root,owner)
            with self.assertRaisesRegex(ValueError,'tail_owner_invalid'):
                tail.check_live_owner(self.root,{**owner,'operation':'cycle'})
            with self.assertRaisesRegex(ValueError,'tail_owner_marker_changed'):
                tail.check_live_owner(self.root,{**owner,'held':{'changed':True}})

    def test_preserves_completed_overhead_and_native_request_bytes(self):
        with sqlite3.connect(':memory:') as conn:
            conn.execute('CREATE TABLE sheet_vitrina_v1_ff_pool_overhead_confirmations(request_id,state,receipt_json)')
            conn.execute('CREATE TABLE sheet_vitrina_v1_ff_pool_document_requests(request_id,state,source_file_blob)')
            for n in range(7):
                conn.execute('INSERT INTO sheet_vitrina_v1_ff_pool_overhead_confirmations VALUES(?,?,?)',(str(n),'completed','{"original":true}'))
                conn.execute('INSERT INTO sheet_vitrina_v1_ff_pool_document_requests VALUES(?,?,?)',(str(n),'complete',b'original'))
            request=dict(preserved_confirmation_request_ids=list(map(str,range(7))))
            before=tail._preservation(conn,request)
            self.assertEqual(len(before),7)
            conn.execute('UPDATE sheet_vitrina_v1_ff_pool_overhead_confirmations SET receipt_json=\'{}\' WHERE request_id=\'1\'')
            self.assertNotEqual(before,tail._preservation(conn,request))
            conn.execute('UPDATE sheet_vitrina_v1_ff_pool_document_requests SET state=\'ready\' WHERE request_id=\'2\'')
            with self.assertRaisesRegex(ValueError,'completed_confirmation_missing'):tail._preservation(conn,request)

    def test_finance_mutating_or_stale_receipt_cannot_reach_finalizer_writer(self):
        runtime=SimpleNamespace(runtime_dir=self.root)
        from packages.application.business_data_heavy_admission import heavy_admitted
        from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
        with heavy_admitted(self.root,operation=tail.OPERATION),warehouse_functional_job_lock(self.root),patch.object(accounting,'current_publication_receipt') as economics:
            with self.assertRaisesRegex(ValueError,'tail_readonly_finance_unproven'):
                tail._finalize(runtime,{},config=None,finance=dict(status='applied',runtime_mutation=True),now=datetime.now(timezone.utc),expected={})
            economics.assert_not_called()


class NativeMetadataFinalizerTests(unittest.TestCase):
    def test_real_history_ack_and_native_metadata_transaction_preserve_business_rows(self):
        from apps.fbs_temporary_allocation_action_smoke import TemporaryAllocationFullActionTests,_connect
        from packages.application import fbs_temporary_allocation_action as native
        from packages.application.operator_ff_overhead import ensure_schema,TABLE
        from packages.application.ff_pool_documents import REQUESTS_TABLE
        from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
        from packages.application.business_data_heavy_admission import heavy_admitted
        fixture=TemporaryAllocationFullActionTests('test_actual_finalizer_accepts_only_the_six_closed_history_days')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        runtime,receipt,proof=fixture._published_history_fixture();manifest=receipt.manifest
        ack=dict(manifest_digest=manifest['manifest_digest'],native=proof['native'])
        with closing(_connect(runtime.db_path)) as conn:
            ensure_schema(conn)
            identities=[r[0] for r in conn.execute(f"SELECT request_id FROM {REQUESTS_TABLE} WHERE state='complete' ORDER BY request_id")]
            self.assertTrue(identities)
            for identity in identities:
                conn.execute(f'INSERT OR IGNORE INTO {TABLE}(request_id,source_json,source_digest,accepted_at,actor,state,updated_at,receipt_json) VALUES(?,\'{{}}\',\'fixture\',\'fixture\',\'test\',\'completed\',\'fixture\',\'{{}}\')',(identity,))
            row=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(receipt.operation_id,receipt.attempt_id)).fetchone()
            diagnostics=json.loads(row[0]);diagnostics['historical_history_ack']=ack
            conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=?',(tail.canonical(diagnostics),receipt.operation_id,receipt.attempt_id));conn.commit()
        request=dict(publication_operation_id=receipt.operation_id,publication_attempt_id=receipt.attempt_id,
            expected_book_version=accounting.load(runtime.runtime_dir)[1],preserved_confirmation_request_ids=identities,original_cycle_id='original-cycle')
        with ready.readonly(runtime.db_path) as conn:
            request['publication_inputs_sha256']=tail._publication_pin(conn,receipt.operation_id,receipt.attempt_id)['inputs_sha256']
            binding=manifest['plan']['effect_binding'];parent=tail._publication_pin(conn,binding['operation_id'],binding['attempt_id'])
            preserved=tail._preservation(conn,request)
            tables=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'") if any(s in r[0] for s in ('ff_pool_','warehouse_business_operations','wb_finance_'))]
            queries=[(f'SELECT * FROM "{name}" ORDER BY rowid',()) for name in sorted(tables)]
            business_before=tail.fingerprint(ready.pin_queries(conn,queries))
            from packages.application.fbs_accounting_historical_revision_writer import SOURCE_TABLES
            material=ready.capture_material(conn,tables=SOURCE_TABLES)
        book,version,books=native._completion_books(runtime,manifest=manifest);del book
        expected=dict(binding=receipt.binding(),sources=receipt.validate_sources_readonly(now=fixture.now),completion_books=books,
            economics=accounting.current_publication_receipt(runtime,now=fixture.now),parent=parent,material=material,
            preserved_confirmations=preserved,tail_operation_id='fixture-history-tail')
        finance=dict(status='already_current',read_only_completion_proof=True,runtime_mutation=False,recalculated_week_count=0,
            non_target_preserved=True,post_verify_stale_week_count=0,accounting_version_unchanged=True,
            accounting_version_before=version,accounting_version=version,source_advanced_after_apply=False)
        directory=native._directory(fixture.root,'full-local-temporary')
        config=SimpleNamespace(candidate_root=directory,formula_epoch='local-temporary-fbs')
        with heavy_admitted(runtime.runtime_dir,operation=tail.OPERATION),warehouse_functional_job_lock(runtime.runtime_dir),accounting.writer_lock(runtime.runtime_dir),patch.object(native,'finalize_scope',side_effect=AssertionError('ordinary finalizer forbidden')),patch.object(native,'_verify_original_publication',side_effect=AssertionError('duplicate manifest decode forbidden')):
            completion=tail._finalize(runtime,request,config=config,finance=finance,now=fixture.now,expected=expected)
        self.assertFalse(completion['original_cycle_replayed'])
        with ready.readonly(runtime.db_path) as conn:
            self.assertEqual(business_before,tail.fingerprint(ready.pin_queries(conn,queries)))
            self.assertEqual(accounting.load(runtime.runtime_dir)[1],version)
            self.assertEqual(conn.execute('SELECT state FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(binding['operation_id'],binding['attempt_id'])).fetchone()[0],'complete')
            self.assertEqual(tail._preservation(conn,request),preserved)
        self.assertTrue(completion['finance']['read_only_completion_proof'])


@unittest.skipUnless(sys.platform=='linux','actual inherited Linux FD authority required')
class NativeTailIntegrationTests(unittest.TestCase):
    def test_registered_apply_real_tail_owner_supervised_ack_and_native_finalizer(self):
        from apps.fbs_temporary_allocation_action_smoke import TemporaryAllocationFullActionTests,_connect
        from packages.application import fbs_temporary_allocation_action as native
        from packages.application import business_data_maintenance_pause as pause
        from packages.application import business_data_write_barrier as barrier
        from packages.application.operator_ff_overhead import ensure_schema,TABLE
        from packages.application.ff_pool_documents import REQUESTS_TABLE
        from packages.application.web_vitrina_snapshot_admission import ApiJobMarkers
        from packages.application.sheet_vitrina_v1_cycle import CycleHistoryConfig
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        from apps.production_apply_adapters import ADAPTERS
        from packages.application.owned_history_worker_capability import file_digest
        import hashlib
        fixture=TemporaryAllocationFullActionTests('test_actual_finalizer_accepts_only_the_six_closed_history_days')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        runtime,receipt,_=fixture._published_history_fixture();manifest=receipt.manifest
        root=runtime.runtime_dir.resolve();candidate=root.parent/'owned-tail-candidate';candidate.mkdir(mode=0o700)
        contract=json.loads((Path(__file__).resolve().parents[1]/'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json').read_text())
        repo=Path(__file__).resolve().parents[1]
        contract['formula_code_hashes']={relative:hashlib.sha256((repo/relative).read_bytes()).hexdigest() for relative in contract['formula_code_hashes']}
        contract['formula_epoch']='wbc0069k16-reviewed-native-v1:'+hashlib.sha256(json.dumps(contract['formula_code_hashes'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        contract['candidate_root']=str(candidate);contract_path=candidate/'contract.json';contract_path.write_text(json.dumps(contract))
        config=CycleHistoryConfig(candidate,contract_path,contract['formula_epoch'])
        for name in ('.web-vitrina-finished-builder.lock','.wb-finance-daily-worker.lock'):(root/name).touch(mode=0o600)
        initialize_admission(root);ApiJobMarkers(root)
        baseline=dict(units={},controls={});baseline_fingerprint=pause.fingerprint(baseline)
        barrier._atomic_write_private_json(root/pause.STATE_FILENAME,dict(schema_version=pause.SCHEMA,phase='held',window_id='native-tail-fixture',baseline=baseline,baseline_fingerprint=baseline_fingerprint))
        barrier._atomic_write_private_json(root/barrier.STATE_FILENAME,dict(schema_version=barrier.SCHEMA_VERSION,phase='held',window_id='native-tail-fixture',window_kind='maintenance_pause',hold_confirmed=True,plan_fingerprint=baseline_fingerprint))
        request=dict(publication_operation_id=receipt.operation_id,publication_attempt_id=receipt.attempt_id,window_id='native-tail-fixture',expected_baseline_fingerprint=baseline_fingerprint,
            expected_book_version=accounting.load(root)[1],original_cycle_id='a'*32)
        cycles=root/'sheet-vitrina-cycles';cycles.mkdir(mode=0o700);cycle=cycles/(request['original_cycle_id']+'.json');cycle.write_text('{"original":"unchanged"}')
        request['original_cycle_sha256']='sha256:'+file_digest(cycle)
        with closing(_connect(runtime.db_path)) as conn:
            ensure_schema(conn)
            identities=[row[0] for row in conn.execute(f"SELECT request_id FROM {REQUESTS_TABLE} WHERE state='complete' ORDER BY request_id")]
            self.assertTrue(identities)
            for identity in identities:
                conn.execute(f'INSERT OR IGNORE INTO {TABLE}(request_id,source_json,source_digest,accepted_at,actor,state,updated_at,receipt_json) VALUES(?,\'{{}}\',\'fixture\',\'fixture\',\'test\',\'completed\',\'fixture\',\'{{}}\')',(identity,))
            conn.commit()
        request['preserved_confirmation_request_ids']=identities
        with ready.readonly(runtime.db_path) as conn:
            request['publication_inputs_sha256']=tail._publication_pin(conn,receipt.operation_id,receipt.attempt_id)['inputs_sha256']
            binding=manifest['plan']['effect_binding'];parent=tail._publication_pin(conn,binding['operation_id'],binding['attempt_id'])
            request['parent_inputs_sha256']=parent['inputs_sha256']
            from packages.application.fbs_accounting_historical_revision_writer import SOURCE_TABLES
            material=ready.capture_material(conn,tables=SOURCE_TABLES);preserved=tail._preservation(conn,request)
            tables=[row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'") if any(part in row[0] for part in ('ff_pool_','warehouse_business_operations','wb_finance_'))]
            queries=[(f'SELECT * FROM "{name}" ORDER BY rowid',()) for name in sorted(tables)]
            before=tail.fingerprint(ready.pin_queries(conn,queries))
        book,version,books=native._completion_books(runtime,manifest=manifest);del book,manifest
        snapshot=dict(binding=receipt.binding(),sources=receipt.validate_sources_readonly(now=fixture.now),completion_books=books,
            economics=accounting.current_publication_receipt(runtime,now=fixture.now),parent=parent,parent_binding=binding,
            material=material,preserved_confirmations=preserved)
        with ready.readonly(runtime.db_path) as conn:snapshot['child']=tail._publication_pin(conn,receipt.operation_id,receipt.attempt_id)
        finance=dict(status='already_current',read_only_completion_proof=True,runtime_mutation=False,recalculated_week_count=0,
            non_target_preserved=True,post_verify_stale_week_count=0,accounting_version_unchanged=True,
            accounting_version_before=version,accounting_version=version,source_advanced_after_apply=False)
        held=tail._held_binding(root,request)
        @contextmanager
        def held_external_boundary(*args):
            token=tail._HELD.set((os.getpid(),root,request,held))
            try:yield held
            finally:tail._HELD.reset(token)
        class FixedClock(datetime):
            @classmethod
            def now(cls,tz=None):return fixture.now
        operation_id='native-owned-history-tail'
        adapter=ADAPTERS[tail.CONTRACT]
        with patch('packages.application.fbs_accounting_apply.target',return_value=root),patch.object(tail,'_held',held_external_boundary),patch.object(tail,'_config',return_value=config),patch.object(tail,'_snapshot',return_value=snapshot),patch.object(tail,'_finance',return_value=finance),patch.object(tail,'datetime',FixedClock),patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'),patch('apps.web_vitrina_finished_snapshot_build.systemd_admission',return_value='idle'),patch.object(WbFinanceWeeklyBlock,'recalculate_stale_cost_weeks',side_effect=AssertionError('Finance write forbidden')),patch.object(native,'finalize_scope',side_effect=AssertionError('ordinary finalizer forbidden')):
            preview=adapter.preview(request,operation_id)
            adapter.apply(request,operation_id,preview)
            result=adapter.readback(request,operation_id)
        self.assertEqual(result['state'],'applied');self.assertTrue(result['scope_complete']);self.assertFalse(result['full_cycle'])
        self.assertEqual(set(result['history_ack']['native']),set(receipt.dates))
        with ready.readonly(runtime.db_path) as conn:
            self.assertEqual(before,tail.fingerprint(ready.pin_queries(conn,queries)))
            self.assertEqual(accounting.load(root)[1],version)
            self.assertEqual(tail._preservation(conn,request),preserved)
            child=json.loads(conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(receipt.operation_id,receipt.attempt_id)).fetchone()[0])
            self.assertTrue(child['temporary_allocation_scope_complete'])
        self.assertEqual('sha256:'+file_digest(cycle),request['original_cycle_sha256'])


if __name__=='__main__':unittest.main()
