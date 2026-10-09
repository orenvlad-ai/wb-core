"""Snapshot scheduling: read-only GET, bounded manual refresh and retained cycle failure."""
from pathlib import Path
import json
import os
import threading
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.stock_monitor_jobs import StockMonitorJobs, period_days
from packages.application.business_data_heavy_admission import HeavyAdmissionBusy


class Jobs:
    def __init__(self):
        self.active = None
        self.runner = None
    def active_job(self, **kwargs):
        return self.active
    def start(self, *, operation, runner):
        assert operation == 'stock_monitor_refresh'
        self.runner = runner
        return {'job_id': 'test-job'}


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.published = []
        self.service = SimpleNamespace(runtime=SimpleNamespace(runtime_dir=self.root), refresh_snapshot=self.refresh)
        self.jobs = Jobs()
        self.controller = StockMonitorJobs(service=self.service, operator_jobs=self.jobs)
    def refresh(self, *, period_days):
        self.published.append(period_days)
        return {'source_fingerprint': f'period-{period_days}', 'generated_at': '2026-10-08T12:00:00Z'}
    def test_read_preference_never_creates_files(self):
        self.assertEqual(self.controller.preferred_period(), 14)
        self.assertEqual(list(self.root.iterdir()), [])
        for invalid in (0, -1, True, '14.5', 3651):
            with self.assertRaises(ValueError): period_days(invalid)
    def test_request_is_async_and_cycle_refreshes_default_and_preference(self):
        result = self.controller.request_refresh(30)
        self.assertEqual(result['status'], 'refreshing')
        self.assertEqual(self.published, [])
        self.assertEqual(self.controller.preferred_period(), 30)
        result = self.controller.refresh_cycle()
        self.assertEqual(self.published, [14, 30])
        self.assertTrue(all(x['status'] == 'published' for x in result['snapshots']))
        self.assertTrue(all(x['snapshot_id'] for x in result['snapshots']))
    def test_active_cycle_queues_preference_without_parallel_job(self):
        self.jobs.active = {'job_id': 'cycle'}
        self.assertEqual(self.controller.request_refresh(45)['status'], 'queued')
        self.assertIsNone(self.jobs.runner)
        self.assertEqual(self.controller.preferred_period(), 45)
    def test_failure_retains_snapshot_and_other_period_proceeds(self):
        self.controller.request_refresh(30)
        def refresh(*, period_days):
            if period_days == 14: raise ValueError('source unavailable')
            return self.refresh(period_days=period_days)
        self.service.refresh_snapshot = refresh
        result = self.controller.refresh_cycle()
        self.assertEqual([x['status'] for x in result['snapshots']], ['retained', 'published'])
    def test_manual_worker_obeys_heavy_exclusion(self):
        self.controller.request_refresh(30)
        with patch('packages.application.stock_monitor_jobs.heavy_admitted', side_effect=HeavyAdmissionBusy('cycle')):
            result = self.jobs.runner(lambda _: None)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(self.published, [])
    def test_preference_changed_during_build_gets_one_followup(self):
        self.controller.request_refresh(30)
        def refresh(*, period_days):
            result = self.refresh(period_days=period_days)
            if period_days == 30:
                (self.controller.root / 'settings.json').write_text(json.dumps({'period_days': 45}))
            return result
        self.service.refresh_snapshot = refresh
        from contextlib import nullcontext
        with patch('packages.application.stock_monitor_jobs.heavy_admitted', return_value=nullcontext()):
            result = self.jobs.runner(lambda _: None)
        self.assertEqual(self.published, [30, 45])
        self.assertEqual(result['period_days'], 45)

    def test_cycle_market_batch_reused_after_ttl_and_fresh_next_operation(self):
        from packages.application.stock_monitor import StockMonitorService
        for failed in (False, True):
            with self.subTest(failed=failed):
                ads = Mock(side_effect=RuntimeError('unavailable')) if failed else Mock(return_value={'index':{}})
                service = StockMonitorService(runtime=self.service.runtime,ads_loader=ads)
                controller = StockMonitorJobs(service=service,operator_jobs=self.jobs)
                controller.request_refresh(30)
                with patch('packages.application.stock_monitor.time.monotonic',return_value=0) as clock:
                    def refresh(*, period_days):
                        try: service._load_market_ads()
                        except RuntimeError: pass
                        clock.return_value += 121
                        return self.refresh(period_days=period_days)
                    service.refresh_snapshot = refresh
                    controller.refresh_cycle()
                    self.assertEqual(ads.call_count,1)
                    controller.refresh_cycle()
                    self.assertEqual(ads.call_count,2)

    def test_manual_followup_market_batch_reused_after_ttl(self):
        from contextlib import nullcontext
        from packages.application.stock_monitor import StockMonitorService
        ads = Mock(return_value={'index':{}})
        service = StockMonitorService(runtime=self.service.runtime,ads_loader=ads)
        controller = StockMonitorJobs(service=service,operator_jobs=self.jobs)
        controller.request_refresh(30)
        with patch('packages.application.stock_monitor.time.monotonic',return_value=0) as clock, \
             patch('packages.application.stock_monitor_jobs.heavy_admitted',return_value=nullcontext()):
            def refresh(*, period_days):
                service._load_market_ads()
                clock.return_value += 121
                if period_days == 30:
                    (controller.root/'settings.json').write_text(json.dumps({'period_days':45}))
                return self.refresh(period_days=period_days)
            service.refresh_snapshot = refresh
            self.jobs.runner(lambda _:None)
            self.assertEqual(self.published,[30,45])
            ads.assert_called_once_with()
            self.jobs.runner(lambda _:None)
            self.assertEqual(ads.call_count,2)

    def test_history_has_no_monitor_publication(self):
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint as Entry, SHEET_OPERATOR_JOB_ID
        from packages.application.business_data_heavy_admission import heavy_admitted
        from packages.application.web_vitrina_snapshot_admission import process_identity
        events = []
        jobs = SimpleNamespace(get=lambda _: {'operation': 'cycle', 'status': 'running'},
                               _threads={'cycle-job': threading.current_thread()})
        owner = SimpleNamespace(runtime=self.service.runtime, operator_jobs=jobs,
            now_factory=lambda: None, _cycle_verify_ready=lambda _: events.append('ready_verified'))
        receipt = {'job_id':'cycle-job', 'owner_pid':os.getpid(), 'process_identity':'offline-cycle-tail-identity'}
        token = SHEET_OPERATOR_JOB_ID.set('cycle-job')
        try:
            with heavy_admitted(self.root, operation='cycle'), \
                 patch('packages.application.web_vitrina_snapshot_admission.process_identity', return_value='offline-cycle-tail-identity'), \
                 patch('apps.web_vitrina_history_candidate_build.build_owned_cycle_history',
                    side_effect=lambda **kwargs: events.append('history_published') or {'edition':'history-proof'}), \
                 patch('packages.application.registry_upload_http_entrypoint.StockMonitorService', return_value=self.service):
                proof = Entry._cycle_history(owner, None, receipt, {'ready':'v1'})
            self.assertEqual(events, ['ready_verified', 'history_published', 'ready_verified'])
            self.assertEqual(proof.versions, {'history_edition':'history-proof'})
            self.assertEqual(self.published, [])
        finally:
            SHEET_OPERATOR_JOB_ID.reset(token)

    def cycle_worker(self, fail='', *, monitor_error=None, diagnostic_write_error=False):
        # Actual start/lease/job/receipt worker; only core source/domain effects
        # use the existing isolated fault harness. No database or provider runs.
        from apps.sheet_vitrina_v1_cycle_smoke import CycleFake, EmptyClosedFixture, NOW, SLOT, STAMP
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint as Entry, SHEET_OPERATOR_JOB_ID
        from packages.application.sheet_vitrina_v1_cycle import CycleHistoryConfig, CycleReceiptStore
        from packages.application.business_data_heavy_admission import require_heavy_owner
        from packages.application.business_data_procedure_admission import already_admitted
        fake = CycleFake(self.root, fail)
        contract = self.root / 'contract.json'; contract.write_text('{}')
        config = CycleHistoryConfig(self.root/'history',contract,'epoch')
        calls = []
        def refresh(*, period_days):
            job_id = SHEET_OPERATOR_JOB_ID.get()
            calls.append({'operation':require_heavy_owner(self.root).operation,
                'maintenance':already_admitted(self.root), 'job':fake.operator_jobs.get(job_id)['status'],
                'same_thread':fake.operator_jobs._threads.get(job_id) is threading.current_thread()})
            if monitor_error:
                raise monitor_error
            return self.refresh(period_days=period_days)
        self.service.refresh_snapshot = refresh
        original_write = CycleReceiptStore.write
        def write(store, receipt):
            if diagnostic_write_error and 'stock_monitor_tail' in receipt:
                raise OSError('private diagnostic failure')
            return original_write(store, receipt)
        with patch('packages.application.sheet_vitrina_v1_cycle.ClosedBacklog', EmptyClosedFixture), \
             patch('packages.application.sheet_vitrina_v1_cycle.process_identity', return_value='offline-cycle-tail-identity'), \
             patch('packages.application.web_vitrina_snapshot_admission.process_identity', return_value='offline-cycle-tail-identity'), \
             patch('packages.application.registry_upload_http_entrypoint.StockMonitorService', return_value=self.service), \
             patch.object(CycleReceiptStore,'write',write):
            result = fake._start_sheet_cycle_job(request_key='tail-fixture',slot_utc=SLOT,history_config=config)
            thread = fake.operator_jobs._threads[result['job_id']]; thread.join(10)
            self.assertFalse(thread.is_alive())
            store = CycleReceiptStore(self.root,lambda:STAMP)
            saved = store.read(result['cycle_id'])
            job = fake.operator_jobs.get(result['job_id'])
            # A terminal request read cannot enter another worker or source pass.
            previous_events = list(fake.events)
            duplicate = fake._start_sheet_cycle_job(request_key='tail-fixture',slot_utc=SLOT,history_config=config)
            self.assertEqual(duplicate['cycle_id'], saved['cycle_id'])
            self.assertEqual(fake.events, previous_events)
        return saved, job, calls

    def test_source_and_warehouse_failures_still_publish_once_under_live_ownership(self):
        for failed_stage in ('api_sources','warehouse'):
            with self.subTest(stage=failed_stage), tempfile.TemporaryDirectory() as root:
                old_root = self.root; old_service_runtime = self.service.runtime
                self.root = Path(root); self.service.runtime = SimpleNamespace(runtime_dir=self.root)
                try:
                    saved, job, calls = self.cycle_worker(failed_stage)
                    self.assertEqual(saved['status'], 'failed')
                    self.assertEqual(saved['error_code'], 'offline_'+failed_stage)
                    self.assertEqual(saved['stock_monitor_tail']['status'], 'published')
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0], {'operation':'cycle','maintenance':True,'job':'running','same_thread':True})
                    self.assertNotIn('stock_monitor_publication', saved['final_versions'])
                    self.assertEqual(job['status'], 'error')
                finally:
                    self.root = old_root; self.service.runtime = old_service_runtime

    def test_monitor_failure_cannot_mask_primary_cycle_failure(self):
        saved, job, calls = self.cycle_worker('warehouse',monitor_error=RuntimeError('private source payload'))
        self.assertEqual(saved['status'],'failed')
        self.assertEqual(saved['error_code'],'offline_warehouse')
        self.assertEqual(saved['stock_monitor_tail']['status'],'retained')
        self.assertEqual(saved['stock_monitor_tail']['snapshots'][0]['error_code'],'RuntimeError')
        self.assertEqual(job['status'],'error')
        self.assertEqual(len(calls),1)
        self.assertNotIn('private',json.dumps(saved['stock_monitor_tail']))

    def test_successful_cycle_publishes_only_once(self):
        saved, job, calls = self.cycle_worker()
        self.assertEqual(saved['status'],'complete')
        self.assertEqual(saved['stock_monitor_tail']['status'],'published')
        self.assertEqual(len(calls),1)
        self.assertNotIn('stock_monitor_publication',saved['final_versions'])
        self.assertEqual(job['status'],'success')

    def test_diagnostic_write_failure_keeps_primary_failed_receipt(self):
        saved, job, calls = self.cycle_worker('warehouse',diagnostic_write_error=True)
        self.assertEqual(saved['status'],'failed')
        self.assertEqual(saved['error_code'],'offline_warehouse')
        self.assertNotIn('stock_monitor_tail',saved)
        self.assertEqual(job['status'],'error')
        self.assertEqual(len(calls),1)

    def test_unowned_or_nonterminal_tail_cannot_write_or_publish(self):
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint as Entry, SHEET_OPERATOR_JOB_ID
        from packages.application.business_data_heavy_admission import heavy_admitted
        from packages.application.web_vitrina_snapshot_admission import process_identity
        owner = SimpleNamespace(runtime=self.service.runtime,now_factory=lambda:None,
            operator_jobs=SimpleNamespace(get=lambda _: {'operation':'cycle','status':'running'},
                _threads={'cycle-job':threading.current_thread()}))
        receipt = {'status':'failed','job_id':'cycle-job','owner_pid':os.getpid(),
            'process_identity':'offline-cycle-tail-identity'}
        store = Mock()
        token = SHEET_OPERATOR_JOB_ID.set('cycle-job')
        try:
            Entry._cycle_stock_monitor_tail(owner,store,receipt)
            with heavy_admitted(self.root,operation='stock_monitor_refresh'):
                Entry._cycle_stock_monitor_tail(owner,store,receipt)
            with heavy_admitted(self.root,operation='cycle'), patch('packages.application.web_vitrina_snapshot_admission.process_identity', return_value='offline-cycle-tail-identity'):
                Entry._cycle_stock_monitor_tail(owner,store,{**receipt,'status':'running'})
                owner.operator_jobs.get = lambda _: {'operation':'cycle','status':'success'}
                Entry._cycle_stock_monitor_tail(owner,store,receipt)
            store.write.assert_not_called()
            self.assertEqual(self.published,[])
        finally:
            SHEET_OPERATOR_JOB_ID.reset(token)


if __name__ == '__main__': unittest.main()
