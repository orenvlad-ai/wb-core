"""Offline fixed-cycle faults, real source-free repair and actual worker admission."""
from __future__ import annotations
from contextlib import closing, ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
import json
import os
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.sheet_vitrina_v1_cycle import (
    CycleHistoryConfig, CycleReceiptStore, CycleStageFailure, CycleConflict,
    StageProof, STAGES, run_cycle, validate_collection, daily_report_proof,
)
from packages.application.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypoint as Entry, SheetVitrinaV1OperatorJobStore,
)
from packages.application.business_data_heavy_admission import heavy_admitted, heavy_admission_status
from packages.application.business_data_procedure_admission import initialize_admission, admission_idle, admitted_write, MaintenanceAdmissionBlocked
from packages.application.web_vitrina_snapshot_admission import ApiJobMarkers, api_jobs_admission
from packages.application.wb_finance_daily import WbFinanceDailyBlock
from packages.application import sheet_vitrina_v1_live_plan as live
from packages.application.sheet_vitrina_v1_cycle_sources import SheetVitrinaCycleSources, CollectedLivePlanSources
from apps import sheet_vitrina_v1_local_derive_smoke as local_fixture
from apps.wb_finance_daily_smoke import _rows, _seed_canonical_cost
from apps import web_vitrina_history_candidate_build as history

NOW = datetime(2026, 9, 29, 9, tzinfo=timezone.utc)
STAMP = NOW.isoformat()
IDENTITY = 'offline-boot:startticks'
SLOT = '2026-09-29T09:00:00+00:00'


def summary():
    return dict(scope_fingerprint='scope', provenance_fingerprint='provenance', bundle_version='bundle',
        business_date='2026-09-29', slots=[dict(source_key='stocks', temporal_slot='today_current',
        date='2026-09-29', kind='success', latest_attempt_kind='success', accepted=True,
        accepted_digest='digest', policy='accepted_complete')])


class CycleFake:
    _start_sheet_cycle_job = Entry._start_sheet_cycle_job
    def __init__(self, root, fail='', blocking=None):
        self.runtime = SimpleNamespace(runtime_dir=root)
        self.now_factory = lambda: NOW
        self.activated_at_factory = lambda: STAMP
        self.operator_jobs = SheetVitrinaV1OperatorJobStore(lambda: STAMP, runtime_dir=root)
        self._sheet_cycle_lock = threading.RLock()
        self.events = []
        self.fail = fail
        self.blocking = blocking
        self.handle = object()
        self.sheet_plan_block = SimpleNamespace(collect_sources=self.collect,
            collected_source_summary=lambda handle: summary(), derive_collected=self.derive)
    def mark(self, stage):
        self.events.append(stage)
        if stage == self.fail:
            raise CycleStageFailure('offline_' + stage)
        if stage == 'api_sources' and self.blocking:
            self.blocking[0].set()
            assert self.blocking[1].wait(5)
        return StageProof({stage + '_version': 'verified'})
    def collect(self, **kwargs):
        assert set(kwargs) == {'as_of_date','log','execution_mode'}
        assert kwargs['execution_mode']=='auto_daily'
        self.mark('api_sources')
        return self.handle
    def derive(self, handle):
        assert handle is self.handle
        self.events.append('derive')
        return 'plan'
    def _cycle_sources(self):return self.sheet_plan_block
    def _cycle_as_of_date(self):return '2026-09-28'
    def _cycle_finance_sources(self):return self.mark('finance_sources')
    def _cycle_fbs_generation(self):return self.mark('fbs_generation')
    def _cycle_warehouse(self, store, receipt, fbs):
        assert fbs
        return self.mark('warehouse')
    def _cycle_daily_projection(self):return self.mark('daily_projection')
    def _cycle_validate_predecessors(self, finance, fbs, material):
        assert finance and fbs
        if self.fail=='source_drift':raise CycleStageFailure('source_drift')
    def _cycle_publish_ready(self, plan):
        assert plan=='plan'
        return {'snapshot_id':'ready'},self.mark('final_ready')
    def _cycle_history(self, config, receipt, ready):
        assert ready['snapshot_id']=='ready'
        return self.mark('rolling14')


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        initialize_admission(self.root)
        self.contract=self.root/'offline-contract.json';self.contract.write_text('{}')
        self.config=CycleHistoryConfig(self.root.parent/'offline-history',self.contract,'epoch')
        self.store=CycleReceiptStore(self.root,lambda:STAMP)
        self.identity=patch('packages.application.sheet_vitrina_v1_cycle.process_identity',return_value=IDENTITY)
        self.identity.start();self.addCleanup(self.identity.stop)
    def owned_history(self, runtime, config):
        with heavy_admitted(runtime.runtime_dir, operation='cycle'):
            return history.build_owned_cycle_history(runtime=runtime,config=config,cycle_owner={},now=NOW)
    def accepted(self, key='slot-request', config=None, slot=SLOT):
        return self.store.accept(request_key=key,slot_utc=slot,config=config or self.config,now=NOW)
    def test_fixed_order_and_no_repeat_after_finish(self):
        receipt,lock=self.accepted();fake=CycleFake(self.root)
        try:
            with heavy_admitted(self.root, operation='cycle'):
                result=run_cycle(fake,self.store,receipt,self.config,lambda _:None)
        finally:lock.close()
        self.assertEqual(fake.events,list(STAGES[:5])+['derive']+list(STAGES[5:]))
        self.assertEqual(result['status'],'complete')
        prior,lock=self.accepted();self.assertIsNone(lock)
        self.assertEqual(prior['cycle_id'],result['cycle_id'])
        # A different key for the same slot cannot repeat completed stages.
        same,lock=self.accepted(key='another-key');self.assertIsNone(lock)
        self.assertEqual(same['cycle_id'],result['cycle_id'])
    def test_every_stage_failure_stops_and_receipt_records_uncertain_boundary(self):
        for fail in STAGES:
            with self.subTest(stage=fail):
                # distinct UTC slot per subtest, no automatic continuation of the first.
                receipt,lock=self.accepted(key=fail,slot=f'2026-09-29T{10+STAGES.index(fail):02}:00:00+00:00')
                fake=CycleFake(self.root,fail)
                try:
                    with heavy_admitted(self.root, operation='cycle'), self.assertRaises(CycleStageFailure):run_cycle(fake,self.store,receipt,self.config,lambda _:None)
                finally:lock.close()
                self.assertEqual(receipt['status'],'failed')
                self.assertEqual(receipt['current_stage'],fail)
                self.assertTrue(all(i['status']=='pending' for i in receipt['stages'][STAGES.index(fail)+1:]))
                self.assertEqual(fake.events.count('warehouse'),int(STAGES.index(fail)>=3))
    def test_consumed_source_drift_never_derives_or_recollects(self):
        receipt,lock=self.accepted();fake=CycleFake(self.root,'source_drift')
        try:
            with heavy_admitted(self.root, operation='cycle'), self.assertRaises(CycleStageFailure):run_cycle(fake,self.store,receipt,self.config,lambda _:None)
        finally:lock.close()
        self.assertNotIn('derive',fake.events)
        self.assertEqual(fake.events.count('api_sources'),1)
    def test_cycle_lock_blocks_another_os_process(self):
        import subprocess
        receipt,lock=self.accepted()
        script="""import fcntl,sys
with open(sys.argv[1],'rb') as stream:
 try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:print('busy')
 else:print('idle')
"""
        try:
            result=subprocess.check_output([sys.executable,'-c',script,str(self.root/'.sheet-vitrina-cycle.lock')],text=True)
            self.assertEqual(result.strip(),'busy')
        finally:lock.close()
        result=subprocess.check_output([sys.executable,'-c',script,str(self.root/'.sheet-vitrina-cycle.lock')],text=True)
        self.assertEqual(result.strip(),'idle')

    def test_request_payload_conflict_and_lock_owned_until_worker_finally(self):
        receipt,lock=self.accepted()
        try:
            prior,other=self.accepted();self.assertIsNone(other);self.assertEqual(prior['cycle_id'],receipt['cycle_id'])
            with self.assertRaisesRegex(CycleConflict,'cycle_request_conflict'):
                self.accepted(config=replace(self.config,budget_seconds=60))
            with self.assertRaisesRegex(CycleConflict,'cycle_busy'):
                self.accepted(key='new',slot='2026-09-29T18:00:00+00:00')
        finally:lock.close()
        prior,other=self.accepted();self.assertIsNone(other)
        self.assertEqual(prior['status'],'interrupted')
    def test_status_after_dead_owner_is_read_only_and_never_replays(self):
        receipt,lock=self.accepted();receipt['status']='running';receipt['stages'][0]['status']='running'
        self.store.write(receipt);lock.close()
        before=(self.store.root/(receipt['cycle_id']+'.json')).read_bytes()
        with patch('packages.application.sheet_vitrina_v1_cycle.process_identity',return_value=None):
            result=self.store.read(receipt['cycle_id'])
        self.assertEqual(result['status'],'interrupted')
        self.assertEqual((self.store.root/(receipt['cycle_id']+'.json')).read_bytes(),before)
        result,lock=self.accepted();self.assertIsNone(lock)
        self.assertEqual(result['status'],'interrupted')
    def test_atomic_internal_start_and_shared_maintenance_lifetime(self):
        entered,finish=threading.Event(),threading.Event();fake=CycleFake(self.root,blocking=(entered,finish))
        result=fake._start_sheet_cycle_job(request_key='start',slot_utc=SLOT,history_config=self.config)
        self.assertTrue(entered.wait(5));self.assertFalse(admission_idle(self.root)['idle'])
        second=fake._start_sheet_cycle_job(request_key='start',slot_utc=SLOT,history_config=self.config)
        self.assertEqual(second['cycle_id'],result['cycle_id'])
        # Ordinary conflicting refresh is atomically excluded at the shared store gate.
        busy=fake.operator_jobs.start(operation='refresh',runner=lambda _:self.fail('duplicate refresh'))
        self.assertEqual(busy['job_id'],result['job_id'])
        finish.set();fake.operator_jobs._threads[result['job_id']].join(5)
        self.assertTrue(admission_idle(self.root)['idle'])
        self.assertEqual(fake.events.count('api_sources'),1)
        self.assertEqual(self.store.read(result['cycle_id'])['status'],'complete')
    def test_operator_constructor_and_proven_no_start_cancellation_release(self):
        for failure in (RuntimeError,KeyboardInterrupt,SystemExit):
            for method in ('__init__','start'):
                with self.subTest(failure=failure,method=method):
                    fake=CycleFake(self.root)
                    with patch('threading.Thread.'+method,side_effect=failure):
                        with self.assertRaises(failure):fake.operator_jobs.start(operation='cycle',runner=lambda _:None)
                    self.assertTrue(admission_idle(self.root)['idle'])
                    self.assertFalse(fake.operator_jobs.active_job(operations=('cycle',)))
    def test_internal_start_constructor_cancel_terminalizes_accepted_receipt(self):
        for index,failure in enumerate((RuntimeError,KeyboardInterrupt,SystemExit)):
            fake=CycleFake(self.root)
            key='constructor-'+str(index)
            slot=f'2026-09-29T{10+index:02}:00:00+00:00'
            with patch('threading.Thread.__init__',side_effect=failure):
                with self.assertRaises(failure):fake._start_sheet_cycle_job(request_key=key,slot_utc=slot,history_config=self.config)
            receipt,lock=self.accepted(key=key,slot=slot)
            self.assertIsNone(lock);self.assertEqual(receipt['status'],'interrupted')
            self.assertEqual(fake.events,[]);self.assertTrue(admission_idle(self.root)['idle'])

    def test_pause_rejects_before_durable_accept_and_child_admission_failure_is_terminal(self):
        fake=CycleFake(self.root)
        with patch('packages.application.business_data_procedure_admission.barrier_status',return_value={'active':True}):
            with self.assertRaises(MaintenanceAdmissionBlocked):
                fake._start_sheet_cycle_job(request_key='paused',slot_utc=SLOT,history_config=self.config)
        self.assertFalse(self.store.root.exists());self.assertEqual(fake.events,[])
        with patch('packages.application.business_data_procedure_admission.admitted_thread',side_effect=MaintenanceAdmissionBlocked('skipped_maintenance')):
            with self.assertRaises(MaintenanceAdmissionBlocked):
                fake._start_sheet_cycle_job(request_key='child-declined',slot_utc=SLOT,history_config=self.config)
        receipt,lock=self.accepted(key='child-declined')
        self.assertIsNone(lock);self.assertEqual(receipt['status'],'interrupted')
        self.assertFalse(fake.operator_jobs.active_job(operations=('cycle',)))
        self.assertTrue(admission_idle(self.root)['idle']);self.assertEqual(fake.events,[])

    def test_native_spawn_cancellation_preserves_busy_before_and_after_bootstrap(self):
        fake=CycleFake(self.root);bootstrap=threading.Event();finish=threading.Event();entered=threading.Event()
        original_bootstrap=threading.Thread._bootstrap
        original_start=threading.Thread.start
        def gated(thread):
            assert bootstrap.wait(5)
            original_bootstrap(thread)
        def interrupted_start(thread):
            thread._started.wait=lambda timeout=None: (_ for _ in ()).throw(KeyboardInterrupt())
            original_start(thread)
        def work(_):entered.set();assert finish.wait(5);return {}
        with patch('threading.Thread._bootstrap',gated),patch('threading.Thread.start',interrupted_start):
            with self.assertRaises(KeyboardInterrupt):fake.operator_jobs.start(operation='cycle',runner=work)
        active=fake.operator_jobs.active_job(operations=('cycle',));thread=fake.operator_jobs._threads[active['job_id']]
        self.assertFalse(admission_idle(self.root)['idle']);self.assertFalse(thread.is_alive())
        self.assertFalse(heavy_admission_status(self.root)['idle'])
        second=fake.operator_jobs.start(operation='cycle',runner=lambda _:self.fail('second child'))
        self.assertEqual(second['job_id'],active['job_id'])
        bootstrap.set();self.assertTrue(entered.wait(5))
        second=fake.operator_jobs.start(operation='cycle',runner=lambda _:self.fail('second child'))
        self.assertEqual(second['job_id'],active['job_id'])
        finish.set();thread.join(5);self.assertTrue(admission_idle(self.root)['idle'])
        self.assertTrue(heavy_admission_status(self.root)['idle'])
    def test_explicit_degradation_policy_rejects_unproved_operands(self):
        value=summary();slot=value['slots'][0]
        for policy in ('accepted_partial','accepted_retained','archive_only','temporal_role_unavailable'):
            slot['policy']=policy
            self.assertTrue(validate_collection(value).warnings)
        slot.update(policy='unavailable',accepted=False)
        with self.assertRaises(CycleStageFailure):validate_collection(value)
    def test_history_deadline_or_pending_is_failure(self):
        # Actual reviewed helper refuses a normally-returned pending result.
        runtime=SimpleNamespace(runtime_dir=self.root,db_path=self.root/'db')
        config=replace(self.config,candidate_root=Path(self.tmp.name+'-history').resolve())
        adapter=SimpleNamespace()
        from contextlib import contextmanager
        @contextmanager
        def slot(*_):yield 'idle'
        @contextmanager
        def acquired(*_):yield True
        with patch('packages.application.web_vitrina_snapshot_admission.api_jobs_admission',return_value='idle'), \
             patch('apps.web_vitrina_finished_snapshot_build.systemd_admission',return_value='idle'), \
             patch('apps.web_vitrina_finished_snapshot_build.lock_admission',return_value='idle'), \
             patch('packages.application.warehouse_functional_lock.warehouse_functional_job_is_busy',return_value=False), \
             patch.object(history,'runtime_storage_admission'),patch.object(history,'finished_builder_slot',slot), \
             patch.object(history,'candidate_singleflight',acquired),patch.object(history,'LiveNativeAdapter',return_value=adapter), \
             patch.object(history,'HistoryStore'),patch.object(history,'update_live_history',return_value={'status':'pending'}):
            with self.assertRaisesRegex(ValueError,'cycle_history_incomplete'):
                self.owned_history(runtime, config)

    def test_history_exact_final_vector_and_other_systemd_guard(self):
        runtime=SimpleNamespace(runtime_dir=self.root,db_path=self.root/'db')
        config=replace(self.config,candidate_root=Path(self.tmp.name+'-history').resolve())
        days=history.dates_between('2026-09-16','2026-09-29')
        vector={'epoch':'epoch','dates':{day:'token-'+day for day in days}}
        adapter=SimpleNamespace(fence='owned-material',capture=lambda:vector)
        edition={'days':dict.fromkeys(days)}
        proofs={day:{'epoch':'epoch','token':vector['dates'][day]} for day in days}
        store=SimpleNamespace(edition=lambda _:edition,_current=lambda:{'current':'edition'},
            day_proofs=lambda _:proofs,rolling_status=lambda *_:{'dirty_dates':[],
                'window_from':days[0],'business_date':days[-1]})
        @contextmanager
        def slot(*_):yield 'idle'
        @contextmanager
        def acquired(*_):yield True
        with ExitStack() as stack:
            stack.enter_context(patch('packages.application.web_vitrina_snapshot_admission.api_jobs_admission',return_value='idle'))
            systemd=stack.enter_context(patch('apps.web_vitrina_finished_snapshot_build.systemd_admission',return_value='idle'))
            stack.enter_context(patch('apps.web_vitrina_finished_snapshot_build.lock_admission',return_value='idle'))
            stack.enter_context(patch('packages.application.warehouse_functional_lock.warehouse_functional_job_is_busy',return_value=False))
            guard=stack.enter_context(patch.object(history,'runtime_storage_admission'))
            stack.enter_context(patch.object(history,'finished_builder_slot',slot))
            stack.enter_context(patch.object(history,'candidate_singleflight',acquired))
            stack.enter_context(patch.object(history,'LiveNativeAdapter',return_value=adapter))
            stack.enter_context(patch.object(history,'HistoryStore',return_value=store))
            build=stack.enter_context(patch.object(history,'update_live_history',return_value={'status':'published','edition_id':'edition'}))
            result=self.owned_history(runtime, config)
            self.assertEqual(result['edition_id'],'edition');self.assertEqual(result['window_from'],days[0])
            proofs[days[0]]['token']='another-source'
            with self.assertRaisesRegex(ValueError,'cycle_history_final_vector_changed'):
                self.owned_history(runtime, config)
            guard.reset_mock();build.reset_mock();systemd.return_value='busy'
            with self.assertRaisesRegex(ValueError,'cycle_history_admission_busy'):
                self.owned_history(runtime, config)
            guard.assert_not_called();build.assert_not_called()


class SourceAndAdmissionTests(unittest.TestCase):
    def test_daily_provisional_waiting_and_retained_failure_are_truthful_dated_proofs(self):
        day=date(2026,9,28)
        for retained in (False,True):
            with TemporaryDirectory() as temp:
                block=WbFinanceDailyBlock(Path(temp),seller_id='seller-1',now_factory=lambda:NOW)
                block.ensure_schema();_seed_canonical_cost(block.db_path)
                if retained:
                    raw=block._store_complete_raw(day,_rows(day),'sha256:fixture');block.project_pointer(day)
                    provisional=daily_report_proof(block)
                    self.assertEqual(provisional.warnings[0]['status'],'loaded_preliminary')
                    self.assertEqual(provisional.warnings[0]['policy'],'accepted_provisional')
                    self.assertEqual(provisional.warnings[0]['batch_id'],raw['batch_id'])
                block._record_failure(day,'waiting','official not complete')
                waiting=daily_report_proof(block).warnings[0]
                self.assertEqual(waiting['status'],'waiting')
                self.assertEqual(waiting['policy'],'accepted_retained_after_waiting' if retained else 'official_waiting')
                for status in ('error_loading','rate_limited'):
                    block._record_failure(day,status,'fixture failure')
                    self.assertNotIn(day,block.due_days(max_days=14))
                    self.assertEqual(block.repair_visible_projections()['status'],'ok')
                    entry=SimpleNamespace(runtime=SimpleNamespace(runtime_dir=Path(temp)),
                        wb_finance_daily_block=block,now_factory=lambda:NOW)
                    if not retained:
                        with self.assertRaisesRegex(CycleStageFailure,'daily_failed_operand_unavailable'):
                            Entry._cycle_daily_projection(entry)
                        continue
                    proof=Entry._cycle_daily_projection(entry)
                    warning=next(item for item in proof.warnings if item['date']==day.isoformat())
                    self.assertEqual(warning['status'],status)
                    self.assertEqual(warning['projection_status'],status)
                    self.assertEqual(warning['policy'],'accepted_retained_after_failed_attempt')
                    self.assertTrue(warning['accepted']);self.assertFalse(warning['attempted'])
                    self.assertEqual(warning['batch_id'],raw['batch_id'])
                    self.assertEqual(warning['content_hash'],raw['content_hash'])

    def test_daily_fresh_failure_and_backoff_require_retained_operand_before_weekly_effects(self):
        from apps.wb_finance_daily_smoke import _Client
        day=date(2026,9,28)
        for retained in (False,True):
            with TemporaryDirectory() as temp:
                root=Path(temp);block=WbFinanceDailyBlock(root,seller_id='seller-1',now_factory=lambda:NOW)
                block.ensure_schema();_seed_canonical_cost(block.db_path)
                with closing(sqlite3.connect(block.db_path)) as conn,conn:
                    conn.execute("UPDATE wb_finance_daily_config SET initial_day=?",(day.isoformat(),))
                if retained:
                    raw=block._store_complete_raw(day,_rows(day),'sha256:fixture');block.project_pointer(day)
                weekly_events=[]
                weekly=SimpleNamespace(seller_id='seller-1',recover_receipted_split_outbox=lambda:{'status':'clean'},
                    due_tick_week=lambda:None,refresh_recent_spp=lambda:weekly_events.append('spp') or {'status':'ok'})
                entry=SimpleNamespace(runtime=SimpleNamespace(runtime_dir=root,db_path=block.db_path),
                    wb_finance_daily_block=block,wb_finance_weekly_block=weekly)
                with patch('packages.adapters.wb_finance_api.WbFinanceApiClient',return_value=_Client(rate_limited=True)):
                    for index in range(2):  # First attempted failure, then backoff without refetch.
                        if not retained:
                            with self.assertRaisesRegex(CycleStageFailure,'daily_failed_operand_unavailable'):
                                Entry._cycle_finance_sources(entry)
                            self.assertEqual(weekly_events,[])
                            continue
                        proof=Entry._cycle_finance_sources(entry)
                        warning=proof.warnings[0]
                        self.assertEqual(warning['status'],'rate_limited')
                        self.assertEqual(warning['policy'],'accepted_retained_after_failed_attempt')
                        self.assertEqual(warning['attempted'],index==0)
                        self.assertEqual(warning['batch_id'],raw['batch_id'])
                self.assertEqual(len(weekly_events),2 if retained else 0)

    def test_daily_pointer_batch_mismatch_is_not_waiting_or_retained_success(self):
        with TemporaryDirectory() as temp:
            block=WbFinanceDailyBlock(Path(temp),seller_id='seller-1',now_factory=lambda:NOW)
            block.ensure_schema();_seed_canonical_cost(block.db_path);day=date(2026,9,28)
            block._store_complete_raw(day,_rows(day),'sha256:fixture');block.project_pointer(day)
            block._record_failure(day,'waiting','fixture waiting')
            with closing(sqlite3.connect(block.db_path)) as conn,conn:
                conn.execute("UPDATE wb_finance_daily_pointers SET content_hash='foreign'")
            with self.assertRaisesRegex(CycleStageFailure,'daily_accepted_operand_invalid'):
                daily_report_proof(block)

    def test_daily_proof_uses_canonical_split_raw_store(self):
        from apps.wb_finance_daily_smoke import _split_backup_contract
        original=WbFinanceDailyBlock.build_daily_payload
        observed=[]
        def read(block):
            payload=original(block)
            observed.append(daily_report_proof(block,payload=payload))
            return payload
        with patch.object(WbFinanceDailyBlock,'build_daily_payload',read):
            _split_backup_contract()
        self.assertTrue(observed)
        self.assertEqual(observed[-1].warnings[0]['policy'],'accepted_provisional')
        self.assertTrue(observed[-1].warnings[0]['batch_id'])

    def test_daily_readback_cannot_bind_old_metrics_to_a_new_equal_count_batch(self):
        with TemporaryDirectory() as temp:
            block=WbFinanceDailyBlock(Path(temp),seller_id='seller-1',now_factory=lambda:NOW)
            block.ensure_schema();_seed_canonical_cost(block.db_path);day=date(2026,9,28)
            block._store_complete_raw(day,_rows(day),'sha256:before');block.project_pointer(day)
            payload=block.build_daily_payload()
            changed=_rows(day);changed[0]['quantity']=int(changed[0]['quantity'])+3
            block._store_complete_raw(day,changed,'sha256:after')
            with self.assertRaisesRegex(CycleStageFailure,'daily_accepted_operand_invalid'):
                daily_report_proof(block,payload=payload)
            block.project_pointer(day)
            self.assertNotEqual(payload['days'][0]['metrics'],block.build_daily_payload()['days'][0]['metrics'])
            with self.assertRaisesRegex(CycleStageFailure,'daily_accepted_operand_invalid'):
                daily_report_proof(block,payload=payload)

    def test_daily_canonical_backlog_attempt_outside_visible14_keeps_dated_raw_binding(self):
        with TemporaryDirectory() as temp:
            block=WbFinanceDailyBlock(Path(temp),seller_id='seller-1',now_factory=lambda:NOW)
            block.ensure_schema();_seed_canonical_cost(block.db_path);day=date(2026,9,28)
            raw=block._store_complete_raw(day,_rows(day),'sha256:backlog')
            block.now_factory=lambda:NOW+timedelta(days=15)
            attempt=block.project_pointer(day)
            self.assertEqual(attempt['status'],'completed')
            self.assertEqual(block.build_daily_payload()['days'],[])
            proof=daily_report_proof(block,attempts=[attempt])
            item=json.loads(proof.versions['daily_report_proofs'])[0]
            self.assertEqual(item['date'],day.isoformat());self.assertEqual(item['batch_id'],raw['batch_id'])
            self.assertEqual(item['projection_status'],'outside_visible14');self.assertTrue(item['accepted'])

    def test_bound_canonical_finance_attempts_once_and_records_only_small_receipts(self):
        events=[]
        daily=SimpleNamespace(tick=lambda client,max_days:events.append(('daily',max_days)) or
            {'status':'ok','recovered':[],'days':[{'report_day':'2026-09-28','status':'waiting'}]})
        weekly=SimpleNamespace(
            recover_receipted_split_outbox=lambda:events.append('recovery') or {'status':'clean'},
            due_tick_week=lambda:('from','to'),
            sync_week=lambda start,end,client:events.append(('weekly',start,end)) or
                {'status':'completed','storage_outbox':{'batch_id':'batch','event_id':'event','sequence_no':7,'raw':'not-retained'}},
            refresh_recent_spp=lambda:events.append('spp') or {'status':'ok'})
        with TemporaryDirectory() as temp:
            entry=SimpleNamespace(runtime=SimpleNamespace(runtime_dir=Path(temp)),
                wb_finance_daily_block=daily,wb_finance_weekly_block=weekly)
            with patch('packages.adapters.wb_finance_api.WbFinanceApiClient',return_value=object()), \
                 patch('packages.application.sheet_vitrina_v1_cycle.finance_raw_proof',return_value={'daily_raw':'daily','weekly_raw':'weekly'}), \
                 patch('packages.application.sheet_vitrina_v1_cycle.daily_report_proof',return_value=StageProof({'daily_report_proofs':'[]'},
                    ({'source_key':'canonical_finance_daily','status':'waiting','policy':'official_waiting'},))):
                proof=Entry._cycle_finance_sources(entry)
        self.assertEqual(events,[('daily',2),'recovery',('weekly','from','to'),'spp'])
        self.assertEqual(json.loads(proof.versions['finance_outbox_receipt']),
            {'batch_id':'batch','event_id':'event','sequence_no':7})
        self.assertNotIn('not-retained',repr(proof));self.assertEqual(proof.warnings[0]['policy'],'official_waiting')

    def test_bound_fbs_requires_a_new_full_generation_and_never_certifies_last_good(self):
        from apps import wb_fbs_complete_snapshot_smoke as fbs_fixture
        from packages.application.wb_fbs_warehouse_registry import WbFbsWarehouseRegistry
        with TemporaryDirectory() as temp:
            root=Path(temp);db=root/'operational.sqlite3';fbs_fixture._seed(db)
            clock=fbs_fixture.Clock()
            registry=WbFbsWarehouseRegistry(db_path=db,timestamp_factory=clock,
                source=fbs_fixture.OfficialSource(),catalog_source=fbs_fixture.CatalogSource())
            registry.collect()
            entry=SimpleNamespace(runtime=SimpleNamespace(db_path=db),
                wb_fbs_warehouse_registry=registry,now_factory=lambda:clock.value)
            proof=Entry._cycle_fbs_generation(entry)
            self.assertEqual(proof.versions['fbs_run_sequence'],'2')
            self.assertTrue(proof.versions['fbs_catalog'].startswith('sha256:'))
            self.assertTrue(proof.versions['fbs_mapping'].startswith('sha256:'))
            with patch.object(registry,'collect',return_value={'status':'last_good'}):
                with self.assertRaisesRegex(CycleStageFailure,'fbs_new_complete_generation_missing'):
                    Entry._cycle_fbs_generation(entry)
            bad=WbFbsWarehouseRegistry(db_path=db,timestamp_factory=clock,
                source=fbs_fixture.OfficialSource(),catalog_source=fbs_fixture.CatalogSource(drift=True))
            entry.wb_fbs_warehouse_registry=bad
            with self.assertRaisesRegex(CycleStageFailure,'fbs_new_complete_generation_missing'):
                Entry._cycle_fbs_generation(entry)

    def test_owned_summary_copies_and_real_source_failure_policy(self):
        case=local_fixture.LocalDeriveTests();case.setUp()
        try:
            handle=case.sources.collect_sources(**case.kwargs)
            proof=case.sources.collected_source_summary(handle)
            self.assertTrue(all(item['accepted_digest'] for item in proof['slots']))
            proof['slots'].clear()
            self.assertEqual(len(case.sources.collected_source_summary(handle)['slots']),2)
            self.assertNotIn('payload',str(case.sources.collected_source_summary(handle)))
            with self.assertRaises(ValueError):case.sources.collected_source_summary(CollectedLivePlanSources())
        finally:case.doCleanups()
    def test_actual_partial_ad_contract_and_partial_finance_fail_closed(self):
        # Use the production candidate predicate via the owned summary, not hand-authored accepted=True.
        case=local_fixture.LocalDeriveTests();case.setUp()
        try:
            handle=case.sources.collect_sources(**case.kwargs)
            collected=case.sources._owned_collection(handle).sources
            status,payload=next(iter(collected.slots.values()))
            status=replace(status,source_key='ads_compact',kind='incomplete',column_date='2026-09-28',snapshot_date='2026-09-28',covered_count=1)
            data=SimpleNamespace(kind='incomplete',temporal_snapshot_acceptable=True,snapshot_date='2026-09-28',diagnostics=dict(partial_observation_contract='ads_partial_observed_v1',
                completeness_state='partial',zero_fill_applied=False,source_date='2026-09-28',source_observed_at=STAMP,
                observed_campaign_ids=[1],dated_roster_state='unqualified'))
            collected.slots={('offline',):(status,data)}
            proof=case.sources.collected_source_summary(handle)
            self.assertTrue(validate_collection(proof).warnings)
            data.diagnostics['zero_fill_applied']=True
            with self.assertRaises(CycleStageFailure):validate_collection(case.sources.collected_source_summary(handle))
            collected.slots={('offline',):(replace(status,source_key='fin_report_daily',kind='success'),data)}
            with self.assertRaises(CycleStageFailure):validate_collection(case.sources.collected_source_summary(handle))
        finally:case.doCleanups()
    def test_source_free_daily_repair_reads_current_cost_and_no_fetch(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);block=WbFinanceDailyBlock(root,seller_id='seller-1',now_factory=lambda:NOW)
            block.ensure_schema();_seed_canonical_cost(block.db_path)
            day=date(2026,9,28);block._store_complete_raw(day,_rows(day),'sha256:fixture');block.project_pointer(day)
            before=block.build_daily_payload()['days'][-1]['metrics']['cogs']
            with sqlite3.connect(block.db_path) as conn:
                conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost SET wac_rub='222',fingerprint='changed' WHERE nm_id=101")
            with patch.object(block,'sync_day',side_effect=AssertionError('repair fetch')), \
                 patch('packages.adapters.wb_finance_api.WbFinanceApiClient',side_effect=AssertionError('repair creates client')):
                result=block.repair_visible_projections()
            self.assertEqual(result['status'],'ok')
            self.assertNotEqual(result['days'][-1]['metrics']['cogs'],before)
            self.assertEqual(result['days'][-1]['status'],'loaded_preliminary')
    def test_publication_only_tail_uses_exact_receipt_and_never_builds_sources(self):
        from apps.sheet_vitrina_v1_onec_zero_stock_empty_bucket_smoke import TARGET_DATE
        from packages.application import ready_publication as publication
        case=local_fixture.LocalDeriveTests();case.setUp()
        try:
            entry=Entry(runtime_dir=case.runtime.runtime_dir,runtime=case.runtime,
                now_factory=lambda:local_fixture.NOW,activated_at_factory=lambda:'2026-05-20T08:02:00Z',
                refreshed_at_factory=lambda:'2026-05-20T08:02:00Z')
            entry.sheet_plan_block=case.block
            handle=case.sources.collect_sources(**case.kwargs)
            plan=case.sources.derive_collected(handle)
            with patch.object(case.block,'build_plan',side_effect=AssertionError('full recollection')):
                versions,proof=entry._cycle_publish_ready(plan)
            self.assertEqual(versions['publication_operation_id'],proof.versions['publication_operation_id'])
            entry._cycle_verify_ready(versions)
            with closing(sqlite3.connect(case.runtime.db_path)) as conn,conn:
                conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=json_set(plan_json,'$.metadata.other_owner','changed')")
            with self.assertRaisesRegex(CycleStageFailure,'cycle_ready_receipt_changed'):
                entry._cycle_verify_ready(versions)
        finally:case.doCleanups()

    def test_exact_owner_marker_exemption_and_other_actor_block(self):
        with TemporaryDirectory() as temp,patch('packages.application.web_vitrina_snapshot_admission.process_identity',return_value=IDENTITY):
            root=Path(temp);initialize_admission(root);markers=ApiJobMarkers(root)
            own=markers.start('cycle-1','cycle')
            owner={'pid':os.getpid(),'identity':IDENTITY,'job_id':'cycle-1','operation':'cycle'}
            self.assertEqual(api_jobs_admission(root),'busy')
            self.assertEqual(api_jobs_admission(root,cycle_owner=owner),'unknown')
            with admitted_write(root):
                self.assertEqual(api_jobs_admission(root,cycle_owner=owner),'idle')
                self.assertEqual(api_jobs_admission(root,cycle_owner={**owner,'job_id':'wrong'}),'busy')
                other=markers.start('refresh-2','refresh')
                self.assertEqual(api_jobs_admission(root,cycle_owner=owner),'busy');markers.finish(other)
            markers.finish(own)


class BoundWarehouseTests(unittest.TestCase):
    def test_owned_handler_runs_all_tails_once_and_checks_exact_journal(self):
        from packages.application.warehouse_update_journal import WarehouseUpdateJournal, PHASES
        from unittest.mock import Mock
        with TemporaryDirectory() as temp:
            root=Path(temp);db=root/'operational.sqlite3'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(slot INTEGER,version_id TEXT)')
                conn.execute('CREATE TABLE sheet_vitrina_v1_warehouse_wb_snapshots(snapshot_id TEXT,version_id TEXT,pagination_complete INTEGER,raw_rows_digest TEXT)')
            entry=Entry.__new__(Entry)
            entry.runtime=SimpleNamespace(runtime_dir=root,db_path=db,finalize_completed_wb_transit_cost_recalculations=Mock(return_value={}))
            entry.activated_at_factory=lambda:STAMP
            entry.warehouse_update_journal=WarehouseUpdateJournal(db_path=db,runtime_dir=root,timestamp_factory=lambda:STAMP)
            entry.wb_supplies_block=SimpleNamespace(sync_functional_sources=Mock(return_value={'sync':{'run_id':'supply'}}),
                collect_all_due_transit_costs=Mock(return_value={}),reconcile_functional_ff_state=Mock(return_value={}))
            entry.our_wb_cost_block=SimpleNamespace(materialize_wb_supply_cost_layers=Mock(return_value=1))
            entry.calculation_parameters_block=SimpleNamespace(prepare_functional_economics_backup=Mock(return_value={}),
                process_pending_targeted_recalculations=Mock(return_value={'request_count':0}),
                publish_current_functional_economics=Mock(return_value={'plan_fingerprint':'economics'}))
            entry.wb_finance_weekly_block=SimpleNamespace(recalculate_stale_cost_weeks=Mock(return_value={'status':'applied','fingerprint':'weekly-cost'}))
            def apply(plan,**kwargs):
                with sqlite3.connect(db) as conn:
                    conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,'functional')")
                    conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots VALUES('fbo','functional',1,'fbo-digest')")
                return {'active_version':{'version_id':'functional'}}
            entry.warehouse_functional_block=SimpleNamespace(build_sync_plan=Mock(return_value={'plan_fingerprint':'plan','diff':{}}),
                apply_plan=Mock(side_effect=apply),record_failed_sync=Mock())
            entry.inventory_planning=SimpleNamespace(current=lambda:{})
            store=CycleReceiptStore(root,lambda:STAMP);store.root.mkdir()
            receipt=dict(cycle_id='a'*32,status='running',slot_utc=SLOT,business_date='2026-09-29',stages=[{'stage':'warehouse','status':'running'}])
            accounting={'status':'published','ready_obligation':'complete','version':'book','operation_id':'book-ready'}
            book={'state':{'periods':{'2026-09-29':{'snapshot':{'id':'fbs','digest':'fbs-digest'}}}}}
            with patch('packages.application.fbs_accounting_runtime.refresh',return_value=accounting) as refresh, \
                 patch('packages.application.fbs_accounting_runtime.load',return_value=(book,'book')), \
                 heavy_admitted(root, operation='cycle'):
                proof=entry._cycle_warehouse(store,receipt,{'fbs_generation':'fbs','fbs_digest':'fbs-digest'})
            self.assertEqual(proof.versions['fbs_book'],'book')
            self.assertEqual(refresh.call_count,1)
            self.assertEqual(entry.wb_finance_weekly_block.recalculate_stale_cost_weeks.call_count,1)
            self.assertEqual(entry.calculation_parameters_block.publish_current_functional_economics.call_count,1)
            self.assertEqual(entry.warehouse_functional_block.build_sync_plan.call_count,1)
            self.assertEqual(entry.warehouse_functional_block.apply_plan.call_count,1)
            with closing(sqlite3.connect(db)) as conn:
                rows=conn.execute('SELECT run_id,status FROM sheet_vitrina_v1_warehouse_update_runs').fetchall()
                self.assertEqual(rows,[(receipt['stages'][0]['durable_ref'],'success')])
                phases=conn.execute('SELECT phase_key,status FROM sheet_vitrina_v1_warehouse_update_phases').fetchall()
                self.assertEqual(set(phases),{(name,'success') for name in PHASES})


if __name__=='__main__':unittest.main()
