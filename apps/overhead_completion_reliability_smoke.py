"""Faults around all five durable native completion commits; never re-post."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import operator_ff_overhead_smoke as native
from apps.operator_ff_overhead_smoke import operations, accounting, warehouse_functional_job_lock, FfPoolDocumentService
from packages.application.dependency_proof import DependencyProof, CompletionProofError
from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry


class CompletionReliabilityTests(unittest.TestCase):
    def fixture(self, *, mode='DELETE', split=False):
        case=native.NativeCompletionTests();case.setUp();self.addCleanup(case.doCleanups)
        case.old_tail()
        raw=case.split_finance_raw() if split else case.runtime.db_path
        for file in {case.runtime.db_path,raw,accounting.path(case.root)}:
            with closing(sqlite3.connect(file)) as conn:conn.execute('PRAGMA journal_mode='+mode)
        return case,raw

    def test_continuous_unrelated_commits_do_not_starve_proof(self):
        for mode in ('DELETE','WAL'):
            case,_=self.fixture(mode=mode)
            with closing(sqlite3.connect(case.runtime.db_path)) as conn,conn:
                conn.execute('CREATE TABLE completion_unrelated_status(value INTEGER)')
                conn.execute('INSERT INTO completion_unrelated_status VALUES(0)')
            with warehouse_functional_job_lock(case.root):
                args=case.proof_inputs();stop=threading.Event();commits=[];errors=[]
                def mutate():
                    with closing(sqlite3.connect(case.runtime.db_path,timeout=.05)) as conn:
                        while not stop.is_set():
                            try:
                                conn.execute('UPDATE completion_unrelated_status SET value=value+1');conn.commit();commits.append(1)
                            except sqlite3.OperationalError as exc:
                                conn.rollback()
                                if 'locked' not in str(exc):errors.append(type(exc).__name__)
                            stop.wait(.002)
                thread=threading.Thread(target=mutate);thread.start()
                try:self.assertEqual(case.complete(*args)['processed_count'],1)
                finally:stop.set();thread.join(2)
            self.assertFalse(errors);self.assertGreater(len(commits),1)
            self.assertEqual(case.business_image(),case.before)
            self.assertEqual(case.native_terminal(case.identity),('complete','retained'))

    def test_relevant_mutation_between_retain_read_and_begin_is_rejected(self):
        case,_=self.fixture();original=WarehouseRecoveryRegistry._transition;mutated=[]
        def transition(registry,*args,**kwargs):
            if kwargs.get('next_state')=='retained' and not mutated:
                with closing(sqlite3.connect(case.runtime.db_path)) as conn,conn:
                    conn.execute("UPDATE wb_finance_weekly_aggregates SET classifier_version='foreign'")
                mutated.append(True)
            return original(registry,*args,**kwargs)
        with warehouse_functional_job_lock(case.root):
            args=case.proof_inputs()
            with patch.object(WarehouseRecoveryRegistry,'_transition',transition),self.assertRaises(CompletionProofError):case.complete(*args)
        self.assertEqual(mutated,[True]);self.assertEqual(case.native_terminal(case.identity),('replay','mutation_running'))
        self.assertEqual(case.business_image(),case.before)

    def test_last_check_to_commit_excludes_main_raw_book_and_manifest(self):
        from dataclasses import replace
        from packages.application.storage_registry import atomic_write_manifest,manifest_payload,parse_manifest,_sha256
        for mode in ('DELETE','WAL'):
            case,raw=self.fixture(mode=mode,split=True);checked=[];original=DependencyProof.assert_current
            def fence(proof,conn):
                result=original(proof,conn)
                if not conn.in_transaction or checked:return result
                state=conn.execute(f'SELECT state FROM {operations.TABLE} WHERE request_id=?',(case.identity,)).fetchone()[0]
                if state!='completed':return result
                outcomes=[]
                def mutate():
                    for file,sql in ((case.runtime.db_path,"UPDATE wb_finance_weekly_aggregates SET classifier_version='race'"),
                                     (raw,"UPDATE finance_raw_current_rows SET row_hash='race'"),
                                     (accounting.path(case.root),'UPDATE accounting_current SET version=version')):
                        with closing(sqlite3.connect(file,timeout=.01)) as other:
                            try:other.execute(sql);other.commit();outcomes.append('committed')
                            except sqlite3.OperationalError:other.rollback();outcomes.append('blocked')
                thread=threading.Thread(target=mutate);thread.start();thread.join(2)
                self.assertFalse(thread.is_alive());self.assertEqual(outcomes,['blocked']*3)
                manifest=case.finance.store_registry.load()
                payload=manifest_payload(replace(manifest,rollback_generation_id='racing'),include_digest=False)
                payload['manifest_sha256']=_sha256(payload)
                with self.assertRaises(BlockingIOError):atomic_write_manifest(case.finance.store_registry.manifest_path,parse_manifest(payload))
                checked.append(True)
                return result
            with warehouse_functional_job_lock(case.root):
                args=case.proof_inputs()
                with patch.object(DependencyProof,'assert_current',fence):self.assertEqual(case.complete(*args)['processed_count'],1)
            self.assertEqual(checked,[True]);self.assertEqual(case.business_image(),case.before)

    def test_own_progress_drift_before_acceptance_cannot_complete(self):
        from packages.application.ff_pool_documents import REQUESTS_TABLE,TARGETED_RECALC_QUEUE_TABLE
        mutations={
            'queue_status':f"UPDATE {TARGETED_RECALC_QUEUE_TABLE} SET economics_status='pending',finance_status='pending'",
            'queue_fingerprint':f"UPDATE {TARGETED_RECALC_QUEUE_TABLE} SET finance_source_fingerprint='foreign'",
            'receipt':f"UPDATE {operations.TABLE} SET receipt_json='{{\"native_completion\":{{\"foreign\":true}}}}'",
            'native_state':f"UPDATE {REQUESTS_TABLE} SET state='posted' WHERE document_kind='pool_overhead'",
            'checkpoint':"UPDATE sheet_vitrina_v1_recovery_operations SET checkpoint_digest='sha256:foreign' WHERE operation_kind='ff_pool_document_posting'",
            'scope':"UPDATE sheet_vitrina_v1_recovery_operations SET target_scope_json=json_set(target_scope_json,'$.foreign',1) WHERE operation_kind='ff_pool_document_posting'",
        }
        for label,sql in mutations.items():
            with self.subTest(mutation=label):
                case,_=self.fixture();original=operations._update;injected=[]
                def update(db_path,identity,state,reason,receipt=None,*,guard=None):
                    if state=='completed' and guard is not None:
                        with closing(sqlite3.connect(db_path)) as conn,conn:conn.execute(sql)
                        injected.append(True)
                    return original(db_path,identity,state,reason,receipt,guard=guard)
                with warehouse_functional_job_lock(case.root):
                    args=case.proof_inputs()
                    with patch.object(operations,'_update',update),self.assertRaises(CompletionProofError):case.complete(*args)
                self.assertEqual(injected,[True])
                self.assertNotEqual(operations.read_acceptance(case.runtime.db_path,case.identity)['state'],'completed')
                self.assertEqual(case.business_image(),case.before)
                case.doCleanups()

    def test_native_overhead_handoffs_never_scan_unrelated_fbo_providers(self):
        from packages.application import fbs_historical_fbo_sources,current_fbo_supply_cost_source
        case,_=self.fixture()
        with warehouse_functional_job_lock(case.root):
            args=case.proof_inputs()
            with patch.object(fbs_historical_fbo_sources,'capture_historical_fbo_documents',side_effect=AssertionError('unrelated historical FBO scan')),patch.object(current_fbo_supply_cost_source,'capture_documents',side_effect=AssertionError('unrelated FBO dispatch scan')):
                self.assertEqual(case.complete(*args)['processed_count'],1)
        self.assertEqual(case.business_image(),case.before)

    def test_expired_before_first_cohort_is_pending_failure(self):
        case,_=self.fixture()
        with warehouse_functional_job_lock(case.root):
            captured,finance,economics=case.proof_inputs()
            with self.assertRaises(CompletionProofError) as caught:
                operations.complete_current_cycle(case.runtime,captured,finance_block=case.finance,finance_receipt=finance,economics_receipt=economics,now=case.now,proof_budget_seconds=1e-12)
        self.assertEqual(caught.exception.diagnostic()['processed_count'],0)
        self.assertEqual(caught.exception.diagnostic()['pending_count'],1)
        self.assertEqual(case.native_terminal(case.identity),('posted','mutation_running'))

    def test_crash_before_and_after_every_commit_matrix(self):
        connect=sqlite3.connect
        for mode in ('DELETE','WAL'):
            for split in (False,True):
                for boundary in ('S1','S2','S3','S4','S5'):
                    for when in ('before','after'):
                        with self.subTest(mode=mode,split=split,boundary=boundary,when=when):
                            case,_=self.fixture(mode=mode,split=split)
                            with warehouse_functional_job_lock(case.root):
                                args=case.proof_inputs();faulted=[]
                                def open_fault(*args,**kwargs):
                                    factory=kwargs.get('factory',sqlite3.Connection)
                                    class FaultConnection(factory):
                                        stage=None
                                        def commit(self):
                                            trigger=self.stage==boundary and not faulted
                                            if trigger and when=='before':
                                                self.rollback();faulted.append((boundary,when));raise RuntimeError('injected_commit_loss')
                                            result=super().commit();self.stage=None
                                            if trigger and when=='after':
                                                faulted.append((boundary,when));raise RuntimeError('injected_commit_loss')
                                            return result
                                        def __exit__(self,kind,value,traceback):
                                            if kind is None:self.commit()
                                            else:self.rollback()
                                            return False
                                    kwargs['factory']=FaultConnection
                                    conn=connect(*args,**kwargs)
                                    def trace(sql):
                                        normalized=' '.join(sql.lower().split())
                                        if not normalized.startswith('update '):return
                                        if 'set economics_status=' in normalized:conn.stage='S1'
                                        elif 'ff_pool_document_requests' in normalized and "set state='replay'" in normalized:conn.stage='S2'
                                        elif 'recovery_operations' in normalized and "set lifecycle_state='retained'" in normalized:conn.stage='S3'
                                        elif 'ff_pool_document_requests' in normalized and "set state='complete'" in normalized:conn.stage='S4'
                                        elif 'ff_pool_overhead_confirmations' in normalized and "set state='completed'" in normalized:conn.stage='S5'
                                    conn.set_trace_callback(trace)
                                    return conn
                                with patch.object(sqlite3,'connect',open_fault),patch.object(FfPoolDocumentService,'_post_once_under_writer_lock',side_effect=AssertionError('repost')):
                                    with self.assertRaisesRegex(RuntimeError,'injected_commit_loss'):case.complete(*args)
                                self.assertEqual(faulted,[(boundary,when)])
                                self.assertEqual(case.business_image(),case.before)
                                with patch.object(FfPoolDocumentService,'_post_once_under_writer_lock',side_effect=AssertionError('repost')):
                                    case.complete(*case.proof_inputs())
                            self.assertEqual(case.native_terminal(case.identity),('complete','retained'))
                            self.assertEqual(case.business_image(),case.before)
                            with closing(sqlite3.connect(case.runtime.db_path)) as conn:
                                self.assertEqual(conn.execute("SELECT count(*) FROM sheet_vitrina_v1_recovery_transitions WHERE to_state='retained' AND operation_id=(SELECT recovery_operation_id FROM sheet_vitrina_v1_ff_pool_document_requests WHERE request_id=?)",(case.identity,)).fetchone()[0],1)
                            case.doCleanups()


if __name__=='__main__':unittest.main()
