"""Snapshot scheduling: read-only GET, bounded manual refresh and retained cycle failure."""
from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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

    def test_real_cycle_tail_publishes_after_history_and_ready_verification(self):
        import threading
        from packages.application.registry_upload_http_entrypoint import (
            RegistryUploadHttpEntrypoint, SHEET_OPERATOR_JOB_ID,
        )
        events = []
        jobs = SimpleNamespace(get=lambda _: {'operation': 'cycle', 'status': 'running'},
                               _threads={'cycle-job': threading.current_thread()})
        owner = SimpleNamespace(runtime=self.service.runtime, operator_jobs=jobs,
            now_factory=lambda: None, _cycle_verify_ready=lambda _: events.append('ready_verified'))
        def history(**kwargs):
            events.append('history_published')
            return {'edition': 'history-proof'}
        def refresh(*, period_days):
            events.append('monitor_published')
            return self.refresh(period_days=period_days)
        self.service.refresh_snapshot = refresh
        token = SHEET_OPERATOR_JOB_ID.set('cycle-job')
        try:
            with patch('packages.application.registry_upload_http_entrypoint.require_heavy_owner'), \
                 patch('apps.web_vitrina_history_candidate_build.build_owned_cycle_history', side_effect=history), \
                 patch('packages.application.registry_upload_http_entrypoint.StockMonitorService', return_value=self.service):
                proof = RegistryUploadHttpEntrypoint._cycle_history(owner, None, {'job_id':'cycle-job'}, {'ready':'v1'})
            self.assertEqual(events, ['history_published', 'ready_verified', 'monitor_published'])
            self.assertEqual(proof.versions['history_edition'], 'history-proof')
            self.assertIn('stock_monitor_publication', proof.versions)
            self.assertEqual(proof.warnings, ())
        finally:
            SHEET_OPERATOR_JOB_ID.reset(token)


if __name__ == '__main__': unittest.main()
