"""Disposable full source/ready cycle through its actual owned history child.

Finance/FBS/warehouse callbacks use the existing fixed domain fixture. Actual
domain acceptance is tested separately; this checks their orchestration seam.
Only fixture setup ownership and unavailable host mount/systemd probes differ
from production. No provider or production operation is invoked.
"""
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
import sys
import threading
import traceback
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps import sheet_vitrina_v1_cycle_wiring_smoke as wiring
from apps import sheet_vitrina_v1_closed_backlog_smoke as fixture
from packages.application.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypoint as Entry, SheetVitrinaV1OperatorJobStore,
)
from packages.application.sheet_vitrina_v1_cycle import CycleReceiptStore, STAGES
from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog
from packages.application.business_data_heavy_admission import heavy_admission_status
from packages.application.business_data_procedure_admission import admission_idle
from packages.application.web_vitrina_history_store import HistoryStore


class OwnedEntry(wiring.FixedDomainEntry):
    _start_sheet_cycle_job = Entry._start_sheet_cycle_job
    _cycle_verify_ready = Entry._cycle_verify_ready

    def __init__(self, case):
        super().__init__(case)
        self.activated_at_factory = lambda: fixture.STAMP
        self.refreshed_at_factory = lambda: fixture.STAMP
        self.operator_jobs = SheetVitrinaV1OperatorJobStore(
            self.activated_at_factory, runtime_dir=self.runtime.runtime_dir)
        self.operator_jobs.enable_snapshot_admission(self.runtime.runtime_dir)
        self._sheet_cycle_lock = threading.RLock()

    def _publish_exact_ready(self, plan):
        self.events.append('ready:' + plan.as_of_date)
        # The current result uses the same exact canonical CAS/complete receipt;
        # unrelated ordinary refresh notification/tail behavior is not exercised.
        return Entry._cycle_publish_dated_ready(self, plan)

    _cycle_publish_dated_ready = _publish_exact_ready
    _cycle_publish_ready = _publish_exact_ready

    def _cycle_history(self, config, receipt, ready, **kwargs):
        self.events.append('rolling14')
        self.backfill = kwargs['backfill_dates']
        try:
            return Entry._cycle_history(self, config, receipt, ready, **kwargs)
        except BaseException:
            self.history_error = traceback.format_exc()
            raise


@unittest.skipUnless(sys.platform == 'linux', 'actual Linux child ownership required')
class OwnedCycleIntegration(unittest.TestCase):
    def test_actual_operator_thread_full_sources_ready_owned_child_and_exact_readback(self):
        case = wiring.WiringTests()
        # Seed disposable sources without retaining setup's main-thread heavy
        # lease. The actual operator thread must acquire and own the real lease.
        with patch.object(fixture, 'heavy_admitted', return_value=nullcontext()):
            case.setUp()
        self.addCleanup(case.doCleanups)
        case.identity.stop()  # Linux uses the actual kernel process generation.
        for name in ('.web-vitrina-finished-builder.lock', '.wb-finance-daily-worker.lock'):
            (case.case.runtime.runtime_dir / name).touch(mode=0o600)
        entry = OwnedEntry(case.case)
        config = replace(case.config, max_recomputes=2, budget_seconds=60)
        with patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'), \
             patch('apps.web_vitrina_finished_snapshot_build.systemd_admission', return_value='idle'), \
             patch.object(entry.source_adapter, 'collect_sources', wraps=entry.source_adapter.collect_sources) as collect:
            accepted = entry._start_sheet_cycle_job(request_key='owned-union-fixture',
                slot_utc=fixture.NOW.isoformat(), history_config=config)
            thread = entry.operator_jobs._threads[accepted['job_id']]
            thread.join(240)
            self.assertFalse(thread.is_alive(), 'actual owned cycle did not terminate')
            receipt = CycleReceiptStore(entry.runtime.runtime_dir, lambda: fixture.STAMP).read(accepted['cycle_id'])
            self.assertIn(receipt['status'], {'complete', 'degraded'},
                getattr(entry, 'history_error', receipt))
            self.assertEqual(tuple(item['stage'] for item in receipt['stages']), STAGES)
            self.assertEqual(collect.call_count, 1)
            self.assertEqual(collect.call_args.kwargs['execution_mode'], 'auto_daily')
            self.assertEqual(set(collect.call_args.kwargs), {'as_of_date', 'log', 'execution_mode'})
            self.assertEqual(entry.backfill, wiring.OLD)
            self.assertEqual(entry.events, ['finance_sources', 'fbs_generation', 'warehouse',
                'daily_projection', 'ready:' + wiring.OLD[0], 'ready:' + wiring.OLD[1],
                'ready:2026-04-19', 'rolling14'])
            native = HistoryStore(config.candidate_root / 'history')
            self.assertEqual(len(native.edition()['days']), 16)
            self.assertEqual(entry.runtime.load_sheet_vitrina_ready_snapshot().as_of_date, '2026-04-19')
            self.assertTrue(all(item['state'] == 'acknowledged' for item in
                ClosedBacklog(entry.source_adapter).status()['dates'].values()))
            self.assertEqual(case.case.counters['seller_funnel_block'].request_dates,
                [*wiring.OLD, '2026-04-20'])
            before = list(entry.events)
            repeated = entry._start_sheet_cycle_job(request_key='owned-union-fixture',
                slot_utc=fixture.NOW.isoformat(), history_config=config)
            self.assertEqual(repeated['cycle_id'], receipt['cycle_id'])
            self.assertEqual(entry.events, before)
        self.assertTrue(heavy_admission_status(entry.runtime.runtime_dir)['idle'])
        self.assertTrue(admission_idle(entry.runtime.runtime_dir)['idle'])


if __name__ == '__main__':
    unittest.main()
