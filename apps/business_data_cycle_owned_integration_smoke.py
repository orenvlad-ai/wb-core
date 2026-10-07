"""Disposable full source/ready cycle through its actual owned history child.

Finance/FBS/warehouse callbacks use the existing fixed domain fixture. Actual
domain acceptance is tested separately; this checks their orchestration seam.
Only fixture setup ownership and unavailable host mount/systemd probes differ
from production. No provider or production operation is invoked.
"""
from contextlib import nullcontext
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sys
import json
import sqlite3
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
    def test_unavailable_spp_reaches_domains_exact_current_ready_and_native_as_degraded(self):
        case = wiring.WiringTests()
        with patch.object(fixture, 'heavy_admitted', return_value=nullcontext()):
            case.setUp()
        self.addCleanup(case.doCleanups)
        case.identity.stop()
        runtime = case.case.runtime
        # This regression is the current cycle, not relaxed old-date ack.
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("DELETE FROM temporal_source_closure_state WHERE target_date<'2026-04-19'")
            conn.execute("DELETE FROM temporal_source_slot_snapshots WHERE source_key='spp' AND snapshot_date IN ('2026-04-19','2026-04-20')")
        for name in ('.web-vitrina-finished-builder.lock', '.wb-finance-daily-worker.lock'):
            (runtime.runtime_dir / name).touch(mode=0o600)
        entry = OwnedEntry(case.case)
        publisher = Entry(runtime_dir=runtime.runtime_dir, runtime=runtime, now_factory=lambda:fixture.NOW,
            activated_at_factory=lambda:fixture.STAMP, refreshed_at_factory=lambda:fixture.STAMP)
        entry._cycle_publish_ready = publisher._cycle_publish_ready
        config = replace(case.config, max_recomputes=2, budget_seconds=60)
        with patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'), \
             patch('apps.web_vitrina_finished_snapshot_build.systemd_admission', return_value='idle'), \
             patch.object(case.case.counters['spp_block'], 'execute', side_effect=RuntimeError('private provider text')) as spp, \
             patch.object(entry.source_adapter, 'collect_sources', wraps=entry.source_adapter.collect_sources) as collect:
            accepted = entry._start_sheet_cycle_job(request_key='owned-missing-spp-fixture',
                slot_utc=fixture.NOW.isoformat(), history_config=config)
            thread = entry.operator_jobs._threads[accepted['job_id']]
            thread.join(240)
            self.assertFalse(thread.is_alive(), 'actual owned cycle did not terminate')
            receipt = CycleReceiptStore(runtime.runtime_dir, lambda:fixture.STAMP).read(accepted['cycle_id'])
            self.assertEqual(receipt['status'], 'degraded', getattr(entry, 'history_error', receipt))
            self.assertEqual(collect.call_count, 1)
            self.assertEqual(set(collect.call_args.kwargs), {'as_of_date','log','execution_mode'})
            self.assertEqual(spp.call_count, 1)
            self.assertEqual(entry.events, ['finance_sources','fbs_generation','warehouse','daily_projection','rolling14'])
            api = next(item for item in receipt['stages'] if item['stage']=='api_sources')
            outcomes = json.loads(api['versions']['source_capture_outcomes'])
            self.assertEqual(len(outcomes), 30)
            self.assertEqual([slot['kind'] for slot in outcomes if slot['source_key']=='spp'], ['missing','error'])
            self.assertTrue(all(not slot['accepted'] and not slot['accepted_digest'] for slot in outcomes if slot['source_key']=='spp'))
            self.assertNotIn('private provider',json.dumps(receipt))
            ready = next(item for item in receipt['stages'] if item['stage']=='final_ready')
            self.assertEqual(ready['status'],'degraded')
            self.assertEqual(ready['versions']['ready_semantic_status'],'error')
            publisher._cycle_verify_ready(ready['versions'])
            native = HistoryStore(config.candidate_root / 'history')
            self.assertEqual(native._current()['current'],receipt['final_versions']['history_edition_id'])
            self.assertEqual(len(native.edition()['days']),14)
            self.assertEqual(entry.backfill,())
        self.assertTrue(heavy_admission_status(runtime.runtime_dir)['idle'])
        self.assertTrue(admission_idle(runtime.runtime_dir)['idle'])

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
