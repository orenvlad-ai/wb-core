#!/usr/bin/env python3
"""Offline lifetime/regression checks for finite business-data continuations.

Real private SQLite job state and real admission flocks; all acquisition/apply
boundaries use local fake services. No HTTP, WB, systemd or production paths.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps import change_registry_observer_smoke as observer_fixture
from apps import ff_inventory_reconciliation_smoke as ff_fixture
from apps import sku_inventory_balance_live_apply_smoke as live_fixture
from apps import sku_inventory_balance_smoke as calculation_fixture
from apps import wb_supplies_backfill_smoke as supply_fixture
from apps import wb_supplies_transit_cost_enrichment_smoke as transit_fixture
from packages.application.business_data_procedure_admission import (
    MaintenanceAdmissionBlocked, admitted_thread, admission_idle,
    already_admitted, initialize_admission,
)
from packages.application.business_data_write_barrier import (
    acquire_barrier, confirm_barrier_hold, release_barrier,
)
from packages.application.ff_document_workflow import (
    FfDocumentWorkflow, INVENTORY_ACTION, INVENTORY_TABLE, OVERHEAD_ACTION, OVERHEAD_TABLE,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sku_inventory_balance import SkuInventoryBalanceBlock
from packages.application.wb_supplies import WbSuppliesBlock

FINGERPRINT = 'sha256:' + 'a' * 64
NOW = '2026-08-29T13:00:00Z'


@contextmanager
def connect(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def hold(runtime):
    acquire_barrier(runtime, window_id='async-worker-smoke', window_kind='maintenance_pause',
                    plan_fingerprint=FINGERPRINT, approval_reference='offline-fixture',
                    actor='test', reason='offline race')


def resume(runtime):
    assert admission_idle(runtime)['idle']
    confirm_barrier_hold(runtime, window_id='async-worker-smoke', plan_fingerprint=FINGERPRINT,
                         maintenance_state={'schema_version': 'business_data_maintenance_pause_v1',
                                            'phase': 'held', 'hold_readback': {'quiet': True}})
    release_barrier(runtime, window_id='async-worker-smoke', plan_fingerprint=FINGERPRINT,
                    actor='test', reason='offline fixture complete',
                    restore_readback={'status': 'restored', 'exact_prior_state_restored': True})


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('bounded worker did not reach expected state')


class WaitingEvent(threading.Event):
    """Expose the actual poll wait so tests prove it owns no SH lease."""
    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()

    def wait(self, timeout=None):
        self.waiting.set()
        return super().wait(timeout)


class AsyncWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='business-async-smoke-')
        self.root = Path(self.temp.name)
        initialize_admission(self.root)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.threads = []

    def tearDown(self):
        self.release.set()
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.temp.cleanup()

    def boundary(self, *, fail=False, result=None):
        def work(*args, **kwargs):
            self.assertTrue(already_admitted(self.root))
            self.entered.set()
            self.assertTrue(self.release.wait(5))
            if fail:
                raise ValueError('offline acquisition failure')
            return result
        return work

    def prove_lifetime(self, done):
        self.assertTrue(self.entered.wait(5))
        hold(self.root)
        self.assertFalse(admission_idle(self.root)['idle'])
        self.release.set()
        wait_until(done)
        wait_until(lambda: admission_idle(self.root)['idle'])

    def ff(self):
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=self.root)
        inventory = SimpleNamespace(build_plan=lambda **kw: {'apply_allowed': True},
                                    get_preview=lambda **kw: {})
        workflow = FfDocumentWorkflow(runtime=runtime, inventory=inventory,
                                      overhead=SimpleNamespace(), timestamp_factory=lambda: NOW,
                                      start_workers=False)
        receipt = workflow.accept_inventory(source_bytes=ff_fixture._workbook([(101, 'SKU', 1)]),
                                            source_filename='fixture.xlsx', business_date=ff_fixture.BUSINESS_DATE,
                                            request_id='request-fixture-001', actor='test')
        workflow.start_workers = True
        preview_id = receipt['preview_id']
        def state():
            with connect(runtime.db_path) as conn:
                return conn.execute(f'SELECT status FROM {INVENTORY_TABLE} WHERE preview_id=?',
                                    (preview_id,)).fetchone()[0]
        return workflow, preview_id, state

    def calculation(self):
        runtime = calculation_fixture.FakeRuntime(self.root / 'runtime.sqlite3')
        sku = calculation_fixture.FakeSkuManagement()
        block = SkuInventoryBalanceBlock(runtime=runtime, sku_management_block=sku)
        payload = {'operation_id': 'ibop_async_fixture_0001', 'idempotency_key': 'ibkey_async_fixture_0001',
                   'calculation': {'sales_period_days': 7}}
        def start():
            return block.start_calculation_operation(payload, user_key='operator', actor='test')
        def state():
            with connect(runtime.db_path) as conn:
                return conn.execute('SELECT state,active_slot FROM sheet_vitrina_v1_inventory_balance_operations').fetchall()
        return block, sku, start, state

    def supplies(self, kind):
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=self.root)
        block = WbSuppliesBlock(runtime=runtime, source=supply_fixture.PagedWbSuppliesSource(),
                                transit_cost_source=transit_fixture.FakeTransitCostSource({'fixture': 123}), timestamp_factory=lambda: NOW)
        if kind == 'backfill':
            start = lambda: block.start_full_backfill({'limit': 10, 'enrich': False})
            active = runtime.load_active_wb_supplies_sync_run
        else:
            # Candidate selection is read-only; the job/status writes remain real.
            block._select_transit_cost_enrichment_candidates = lambda request: [{'supply_id': 'fixture'}]
            start = block.start_transit_cost_enrichment
            active = runtime.load_active_wb_supply_transit_cost_enrichment_run
        return block, runtime, start, active

    def assert_observer_released(self, observer):
        with connect(observer.store_registry.resolve('operational')) as conn:
            self.assertEqual(conn.execute(
                f'SELECT owner_job_id FROM {observer_fixture.OBSERVER_LEASES_TABLE}'
            ).fetchone(), ('',))

    def test_ff_lifetime_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                workflow, preview_id, state = self.ff()
                workflow.inventory.build_plan = self.boundary(fail=fail, result={'apply_allowed': True})
                workflow._dispatch(INVENTORY_ACTION, preview_id)
                self.prove_lifetime(lambda: not workflow._inflight)
                self.assertEqual(state(), 'failed' if fail else 'previewed')
                if not fail:
                    # Start the second case with a new independent private store.
                    self.temp.cleanup()
                    self.setUp()

    def test_ff_pending_start_failures_and_startup_recovery(self):
        workflow, preview_id, state = self.ff()
        hold(self.root)
        workflow.resume_incomplete()
        self.assertEqual(state(), 'accepted')
        self.assertFalse(workflow._inflight)
        self.assertTrue(admission_idle(self.root)['idle'])
        resume(self.root)
        with patch('threading.Thread.start', side_effect=RuntimeError('start refused')):
            with self.assertRaises(RuntimeError):
                workflow.resume_incomplete()
        self.assertFalse(workflow._inflight)
        self.assertEqual(state(), 'accepted')
        self.assertTrue(admission_idle(self.root)['idle'])
        # Preserve existing restart processing->accepted recovery, no new picker.
        with connect(workflow.runtime.db_path) as conn:
            conn.execute(f"UPDATE {INVENTORY_TABLE} SET status='processing'")
        workflow.inventory.build_plan = self.boundary(result={'apply_allowed': True})
        restored = FfDocumentWorkflow(runtime=workflow.runtime, inventory=workflow.inventory,
                                     overhead=workflow.overhead, timestamp_factory=lambda: NOW)
        self.prove_lifetime(lambda: not restored._inflight)
        self.assertEqual(state(), 'previewed')

    def test_ff_overhead_worker_lifetime(self):
        workflow, preview_id, state = self.ff()
        workflow.start_workers = False
        receipt = workflow.accept_overhead(business_date=ff_fixture.BUSINESS_DATE, amount_rub='123',
                                           reason='fixture', request_id='overhead-fixture-001', actor='test')
        workflow.start_workers = True
        workflow.overhead.build_plan = self.boundary(result={'apply_allowed': True})
        workflow._dispatch(OVERHEAD_ACTION, receipt['preview_id'])
        self.prove_lifetime(lambda: not workflow._inflight)
        with connect(workflow.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT status FROM {OVERHEAD_TABLE}').fetchone(), ('previewed',))

    def test_calculation_lifetime_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                block, sku, start, state = self.calculation()
                original = sku.build_inventory_balance_evidence
                boundary = self.boundary(fail=fail)
                def evidence(**kwargs):
                    boundary()
                    return original(**kwargs)
                sku.build_inventory_balance_evidence = evidence
                start()
                self.prove_lifetime(lambda: state() and state()[0][1] is None)
                self.assertEqual(state()[0], ('failed' if fail else 'succeeded', None))
                if not fail:
                    self.temp.cleanup()
                    self.setUp()

    def test_pause_after_calculation_acceptance_before_handoff(self):
        block, sku, start, state = self.calculation()
        original = sku.build_inventory_balance_evidence
        boundary = self.boundary()
        def evidence(**kwargs):
            boundary()
            return original(**kwargs)
        sku.build_inventory_balance_evidence = evidence
        def raced(*args, **kwargs):
            self.assertEqual(state(), [('accepted', 1)])
            self.assertTrue(already_admitted(self.root))
            hold(self.root)
            return admitted_thread(*args, **kwargs)
        with patch('packages.application.sku_inventory_balance.admitted_thread', side_effect=raced):
            start()
        self.prove_lifetime(lambda: state()[0][1] is None)
        self.assertEqual(state()[0], ('succeeded', None))

    def test_calculation_blocked_before_acceptance(self):
        block, sku, start, state = self.calculation()
        hold(self.root)
        with self.assertRaises(MaintenanceAdmissionBlocked):
            start()
        self.assertEqual(state(), [])
        self.assertTrue(admission_idle(self.root)['idle'])

    def test_calculation_handoff_rejection_and_start_failure(self):
        for stage in ('constructor', 'start'):
            with self.subTest(stage=stage):
                block, sku, start, state = self.calculation()
                def refused(*args, **kwargs):
                    self.assertTrue(already_admitted(self.root))
                    self.assertEqual(state(), [('accepted', 1)])
                    hold(self.root)
                    raise MaintenanceAdmissionBlocked('independent handoff refused')
                context = patch('packages.application.sku_inventory_balance.admitted_thread', side_effect=refused) if stage == 'constructor' else patch('threading.Thread.start', side_effect=RuntimeError('start refused'))
                with context:
                    start()
                self.assertEqual(state(), [('failed', None)])
                self.assertIsNone(block._calculation_worker_thread)
                self.assertTrue(admission_idle(self.root)['idle'])
                if stage == 'constructor':
                    resume(self.root)
                # A different identity must be able to take the released slot.
                with connect(block.runtime.db_path) as conn:
                    conn.execute('DELETE FROM sheet_vitrina_v1_inventory_balance_operations')

    def test_supplies_blocked_before_durable_acceptance(self):
        for kind in ('backfill', 'transit'):
            with self.subTest(kind=kind):
                block, runtime, start, active = self.supplies(kind)
                with patch.object(runtime, 'create_wb_supplies_sync_run') as create_backfill, patch.object(runtime, 'create_wb_supply_transit_cost_enrichment_run') as create_transit:
                    if kind == 'backfill':
                        hold(self.root)
                    with self.assertRaises(MaintenanceAdmissionBlocked):
                        start()
                    create_backfill.assert_not_called()
                    create_transit.assert_not_called()
                self.assertTrue(admission_idle(self.root)['idle'])

    def test_supplies_handoff_and_start_failure(self):
        for kind in ('backfill', 'transit'):
            for stage in ('constructor', 'start'):
                with self.subTest(kind=kind, stage=stage):
                    block, runtime, start, active = self.supplies(kind)
                    def refused(*args, **kwargs):
                        self.assertTrue(already_admitted(self.root))
                        self.assertIsNotNone(active())
                        hold(self.root)
                        raise MaintenanceAdmissionBlocked('independent handoff refused')
                    context = patch('packages.application.wb_supplies.admitted_thread', side_effect=refused) if stage == 'constructor' else patch('threading.Thread.start', side_effect=RuntimeError('start refused'))
                    with context, self.assertRaises(RuntimeError):
                        start()
                    self.assertIsNone(active())
                    self.assertFalse(block._transit_cost_threads)
                    self.assertTrue(admission_idle(self.root)['idle'])
                    if stage == 'constructor':
                        resume(self.root)

    def test_backfill_lifetime_and_controlled_error(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                block, runtime, start, active = self.supplies('backfill')
                block.source.rows = []
                original = block.source.list_supplies
                boundary = self.boundary(fail=fail)
                def fetch(**kwargs):
                    boundary()
                    return original(**kwargs)
                block.source.list_supplies = fetch
                receipt = start()
                self.prove_lifetime(lambda: active() is None)
                self.assertEqual(runtime.load_wb_supplies_sync_run(receipt['run_id'])['status'],
                                 'failed' if fail else 'success')
                if not fail:
                    self.temp.cleanup()
                    self.setUp()

    def test_transit_lifetime_and_controlled_error(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                block, runtime, start, active = self.supplies('transit')
                original = block.transit_cost_source.fetch_costs
                boundary = self.boundary(fail=fail)
                def fetch(*args, **kwargs):
                    boundary()
                    return original(*args, **kwargs)
                block.transit_cost_source.fetch_costs = fetch
                receipt = start()
                self.prove_lifetime(lambda: not block._transit_cost_threads)
                self.assertEqual(runtime.load_wb_supply_transit_cost_enrichment_run(receipt['run_id'])['status'],
                                 'failed' if fail else 'success')
                if not fail:
                    self.temp.cleanup()
                    self.setUp()

    def test_observer_lifetime_and_controlled_error(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                observer, acquirer = observer_fixture._observer(self.root, observer_fixture._snapshot(13 * 60), NOW)
                acquirer.acquire = self.boundary(fail=fail, result=acquirer.snapshot)
                observer.submit_manual(requested_by='operator', job_id='async-lifetime')
                terminal = 'failed' if fail else 'complete'
                self.prove_lifetime(lambda: observer.read_job('async-lifetime')['events'][-1]['state'] == terminal)
                self.assert_observer_released(observer)
                if not fail:
                    self.temp.cleanup()
                    self.setUp()

    def test_observer_handoff_rejection_and_start_failure(self):
        for stage in ('constructor', 'start'):
            with self.subTest(stage=stage):
                observer, acquirer = observer_fixture._observer(self.root, observer_fixture._snapshot(13 * 60), NOW)
                job_id = 'async-failed-' + stage
                def refused(*args, **kwargs):
                    self.assertTrue(already_admitted(self.root))
                    self.assertEqual(observer.read_job(job_id)['events'][-1]['state'], 'running')
                    hold(self.root)
                    raise MaintenanceAdmissionBlocked('independent handoff refused')
                context = patch('packages.application.change_registry_observer.admitted_thread', side_effect=refused) if stage == 'constructor' else patch('threading.Thread.start', side_effect=RuntimeError('start refused'))
                with context, self.assertRaises(RuntimeError):
                    observer.submit_manual(requested_by='operator', job_id=job_id)
                self.assertEqual(observer.read_job(job_id)['events'][-1]['state'], 'failed')
                self.assert_observer_released(observer)
                self.assertEqual(acquirer.acquire_calls, 0)
                self.assertTrue(admission_idle(self.root)['idle'])
                if stage == 'constructor':
                    with patch.object(observer, '_admit') as durable:
                        with self.assertRaises(MaintenanceAdmissionBlocked):
                            observer.submit_manual(requested_by='operator', job_id='blocked-before-admit')
                        durable.assert_not_called()
                    resume(self.root)

    def cancellation_case(self, kind):
        if kind == 'ff':
            workflow, preview_id, state = self.ff()
            workflow.inventory.build_plan = self.boundary(result={'apply_allowed': True})
            start = lambda: workflow._dispatch(INVENTORY_ACTION, preview_id)
            def check_no_start():
                self.assertEqual(state(), 'accepted')
                self.assertFalse(workflow._inflight)
            done = lambda: not workflow._inflight
        elif kind == 'calculation':
            block, sku, start, state = self.calculation()
            original = sku.build_inventory_balance_evidence
            boundary = self.boundary()
            def evidence(**kwargs):
                boundary()
                return original(**kwargs)
            sku.build_inventory_balance_evidence = evidence
            def check_no_start():
                self.assertEqual(state(), [('failed', None)])
                self.assertIsNone(block._calculation_worker_thread)
                self.assertEqual(block._calculation_worker_operation_id, '')
            done = lambda: state() and state()[0][1] is None
        elif kind in ('backfill', 'transit'):
            block, runtime, start, active = self.supplies(kind)
            if kind == 'backfill':
                block.source.rows = []
                original = block.source.list_supplies
                boundary = self.boundary()
                def fetch(**kwargs):
                    boundary()
                    return original(**kwargs)
                block.source.list_supplies = fetch
            else:
                original = block.transit_cost_source.fetch_costs
                boundary = self.boundary()
                def fetch(*args, **kwargs):
                    boundary()
                    return original(*args, **kwargs)
                block.transit_cost_source.fetch_costs = fetch
            def check_no_start():
                self.assertIsNone(active())
                self.assertFalse(block._transit_cost_threads)
            done = lambda: active() is None
        else:
            observer, acquirer = observer_fixture._observer(self.root, observer_fixture._snapshot(13 * 60), NOW)
            acquirer.acquire = self.boundary(result=acquirer.snapshot)
            start = lambda: observer.submit_manual(requested_by='operator', job_id='cancel-fixture')
            def check_no_start():
                self.assertEqual(observer.read_job('cancel-fixture')['events'][-1]['state'], 'failed')
                self.assert_observer_released(observer)
            done = lambda: observer.read_job('cancel-fixture')['events'][-1]['state'] == 'complete'
        return start, check_no_start, done

    def test_no_start_cancellation_cleans_finite_jobs_and_reraises(self):
        modules = {
            'ff': 'ff_document_workflow', 'calculation': 'sku_inventory_balance',
            'backfill': 'wb_supplies', 'transit': 'wb_supplies',
            'observer': 'change_registry_observer',
        }
        for kind, module in modules.items():
            for error in (KeyboardInterrupt, SystemExit):
                for stage in ('constructor', 'start'):
                    with self.subTest(kind=kind, error=error, stage=stage):
                        start, check_no_start, done = self.cancellation_case(kind)
                        cancelled = error('offline interruption before native spawn')
                        target = 'packages.application.' + module + '.admitted_thread' if stage == 'constructor' else 'threading.Thread.start'
                        with patch(target, side_effect=cancelled):
                            with self.assertRaises(error) as caught:
                                start()
                        self.assertIs(caught.exception, cancelled)
                        check_no_start()
                        self.assertFalse(self.entered.is_set())
                        self.assertTrue(admission_idle(self.root)['idle'])
                        self.tearDown()
                        self.setUp()

    def test_native_spawn_before_started_wait_preserves_finite_jobs(self):
        original_start = threading.Thread.start
        original_bootstrap = threading.Thread._bootstrap
        for kind in ('ff', 'calculation', 'backfill', 'transit', 'observer'):
            for error in (KeyboardInterrupt, SystemExit):
                with self.subTest(kind=kind, error=error):
                    start, check_no_start, done = self.cancellation_case(kind)
                    native_spawn = threading.Event()
                    bootstrap_release = threading.Event()
                    cancelled = error('offline interruption after native spawn')
                    def bootstrap(thread):
                        native_spawn.set()
                        assert bootstrap_release.wait(5)
                        original_bootstrap(thread)
                    def interrupted_start(thread):
                        self.threads.append(thread)
                        # Native child exists but has not set _started yet.
                        with patch.object(thread._started, 'wait', side_effect=cancelled):
                            original_start(thread)
                    try:
                        with patch('threading.Thread._bootstrap', new=bootstrap), patch('threading.Thread.start', new=interrupted_start):
                            with self.assertRaises(error) as caught:
                                start()
                        self.assertIs(caught.exception, cancelled)
                        self.assertTrue(native_spawn.wait(5))
                        self.assertFalse(self.threads[0]._started.is_set())
                        self.assertFalse(self.threads[0].abort_if_unstarted())
                        self.assertFalse(admission_idle(self.root)['idle'])
                        hold(self.root)
                        bootstrap_release.set()
                        self.prove_lifetime(done)
                    finally:
                        bootstrap_release.set()
                        self.tearDown()
                        self.setUp()

    def live(self, adapter):
        # Queue through the real public operation but control when pickup starts.
        with patch.object(SkuInventoryBalanceBlock, '_start_apply_worker_if_needed', return_value=False):
            block, sku, writer = live_fixture._build_runtime(self.root, adapter)
            calculation = live_fixture._insert_calculation(block, 1, 'admission')
            receipt = live_fixture._start(block, calculation)
        block._apply_worker_wakeup = WaitingEvent()
        return block, receipt['job_id']

    def test_live_job_holds_through_readback_without_duplicate_release(self):
        adapter = live_fixture.FakeLiveAdapter()
        block, job_id = self.live(adapter)
        original = adapter.readback
        boundary = self.boundary()
        def readback(*args, **kwargs):
            boundary()
            return original(*args, **kwargs)
        adapter.readback = readback
        with patch.object(block, '_release_job_lease', wraps=block._release_job_lease) as release_job:
            block._start_apply_worker_if_needed()
            self.threads.append(block._apply_worker_thread)
            self.prove_lifetime(lambda: block._apply_worker_thread is None)
            self.assertEqual(release_job.call_count, 1)
        self.assertEqual(block.get_apply_job(job_id)['state'], 'completed')
        self.assertEqual(len(adapter.submit_attempts), 1)

    def test_live_pending_during_pause_and_startup_recovery(self):
        adapter = live_fixture.FakeLiveAdapter()
        block, job_id = self.live(adapter)
        # Existing process stops before pickup, then constructor resumes it.
        hold(self.root)
        recovered, sku, writer = live_fixture._build_runtime(self.root, adapter)
        recovered._apply_worker_wakeup = WaitingEvent()
        self.threads.append(recovered._apply_worker_thread)
        wait_until(lambda: recovered._apply_worker_wakeup.waiting.is_set())
        with connect(block.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT state,worker_token FROM sheet_vitrina_v1_inventory_balance_apply_jobs').fetchone(), ('pending', ''))
        self.assertTrue(admission_idle(self.root)['idle'])
        self.assertEqual(adapter.submit_attempts, [])
        resume(self.root)
        recovered._apply_worker_wakeup.set()
        wait_until(lambda: recovered._apply_worker_thread is None)
        self.assertEqual(recovered.get_apply_job(job_id)['state'], 'completed')
        self.assertEqual(len(adapter.submit_attempts), 1)

    def test_live_containment_releases_and_idle_poll_has_no_lease(self):
        adapter = live_fixture.FakeLiveAdapter()
        block, job_id = self.live(adapter)
        original_mark = block._mark_job_worker_error
        def mark(*args):
            self.assertFalse(admission_idle(self.root)['idle'])
            return original_mark(*args)
        block._mark_job_worker_error = mark
        block._run_live_job = self.boundary(fail=True)
        block._start_apply_worker_if_needed()
        self.threads.append(block._apply_worker_thread)
        self.prove_lifetime(lambda: block._apply_worker_thread is None)
        self.assertEqual(block.get_apply_job(job_id)['state'], 'stalled')
        with connect(block.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT worker_token FROM sheet_vitrina_v1_inventory_balance_apply_jobs').fetchone(), ('',))
        self.assertEqual(adapter.submit_attempts, [])

    def test_live_idle_wait_preserves_other_worker_token(self):
        adapter = live_fixture.FakeLiveAdapter()
        block, job_id = self.live(adapter)
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with connect(block.runtime.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_inventory_balance_apply_jobs "
                         "SET worker_token='other-worker',lease_expires_at=?", (future,))
        block._start_apply_worker_if_needed()
        self.threads.append(block._apply_worker_thread)
        self.assertTrue(block._apply_worker_wakeup.waiting.wait(5))
        self.assertTrue(admission_idle(self.root)['idle'])
        self.assertEqual(adapter.submit_attempts, [])
        block._apply_worker_stop.set()
        block._apply_worker_wakeup.set()
        wait_until(lambda: block._apply_worker_thread is None)
        with connect(block.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT worker_token FROM sheet_vitrina_v1_inventory_balance_apply_jobs').fetchone(), ('other-worker',))

    def test_raw_apply_no_start_cancellation_clears_only_dead_reference(self):
        for error in (KeyboardInterrupt, SystemExit):
            with self.subTest(error=error):
                adapter = live_fixture.FakeLiveAdapter()
                block, job_id = self.live(adapter)
                cancelled = error('offline raw no-start')
                with patch('threading.Thread.start', side_effect=cancelled):
                    with self.assertRaises(error) as caught:
                        block._start_apply_worker_if_needed()
                self.assertIs(caught.exception, cancelled)
                self.assertIsNone(block._apply_worker_thread)
                self.assertTrue(admission_idle(self.root)['idle'])
                self.assertEqual(adapter.submit_attempts, [])
                self.assertEqual(block.get_apply_job(job_id)['state'], 'pending')
                block._start_apply_worker_if_needed()
                thread = block._apply_worker_thread
                if thread is not None:
                    self.threads.append(thread)
                wait_until(lambda: block._apply_worker_thread is None)
                self.assertEqual(block.get_apply_job(job_id)['state'], 'completed')
                self.tearDown()
                self.setUp()

    def test_raw_apply_native_spawn_preserves_one_loop_before_and_after_bootstrap(self):
        original_start = threading.Thread.start
        original_bootstrap = threading.Thread._bootstrap
        for error in (KeyboardInterrupt, SystemExit):
            with self.subTest(error=error):
                adapter = live_fixture.FakeLiveAdapter()
                block, job_id = self.live(adapter)
                hold(self.root)
                cancelled = error('offline raw ambiguous start')
                bootstrap_release = threading.Event()
                native_spawn = threading.Event()
                stats = {'entries': 0, 'concurrent': 0, 'maximum': 0}
                stats_lock = threading.Lock()
                original_loop = block._apply_worker_loop
                def counted_loop():
                    with stats_lock:
                        stats['entries'] += 1
                        stats['concurrent'] += 1
                        stats['maximum'] = max(stats['maximum'], stats['concurrent'])
                    try:
                        original_loop()
                    finally:
                        with stats_lock:
                            stats['concurrent'] -= 1
                block._apply_worker_loop = counted_loop
                def bootstrap(thread):
                    native_spawn.set()
                    assert bootstrap_release.wait(5)
                    original_bootstrap(thread)
                def interrupted_start(thread):
                    self.threads.append(thread)
                    with patch.object(thread._started, 'wait', side_effect=cancelled):
                        original_start(thread)
                try:
                    with patch('threading.Thread._bootstrap', new=bootstrap), patch('threading.Thread.start', new=interrupted_start):
                        with self.assertRaises(error) as caught:
                            block._start_apply_worker_if_needed()
                        self.assertIs(caught.exception, cancelled)
                        self.assertTrue(native_spawn.wait(5))
                        owner = block._apply_worker_thread
                        self.assertIs(owner, self.threads[0])
                        self.assertFalse(owner.is_alive())  # Child is still in _limbo.
                        self.assertTrue(block._start_apply_worker_if_needed())
                        self.assertIs(block._apply_worker_thread, owner)
                        self.assertEqual(len(self.threads), 1)
                    bootstrap_release.set()
                    self.assertTrue(block._apply_worker_wakeup.waiting.wait(5))
                    self.assertTrue(block._start_apply_worker_if_needed())
                    self.assertIs(block._apply_worker_thread, owner)
                    self.assertEqual(stats['entries'], 1)
                    self.assertTrue(admission_idle(self.root)['idle'])
                    self.assertEqual(adapter.submit_attempts, [])
                    resume(self.root)
                    block._apply_worker_wakeup.set()
                    wait_until(lambda: block._apply_worker_thread is None)
                    self.assertEqual(block.get_apply_job(job_id)['state'], 'completed')
                    self.assertEqual(stats['maximum'], 1)
                    self.assertEqual(len(adapter.submit_attempts), 1)
                finally:
                    bootstrap_release.set()
                    self.tearDown()
                    self.setUp()

    def test_live_cancel_releases_claim_for_startup_recovery(self):
        adapter = live_fixture.FakeLiveAdapter()
        block, job_id = self.live(adapter)
        def cancel(*args):
            self.assertTrue(already_admitted(self.root))
            self.entered.set()
            self.assertTrue(self.release.wait(5))
            raise SystemExit('offline cancellation before external submit')
        block._run_live_job = cancel
        block._start_apply_worker_if_needed()
        self.threads.append(block._apply_worker_thread)
        self.prove_lifetime(lambda: block._apply_worker_thread is None)
        with connect(block.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT state,worker_token FROM sheet_vitrina_v1_inventory_balance_apply_jobs').fetchone(), ('running', ''))
        self.assertEqual(adapter.submit_attempts, [])
        with patch.object(SkuInventoryBalanceBlock, '_start_apply_worker_if_needed', return_value=False):
            recovered, sku, writer = live_fixture._build_runtime(self.root, adapter)
        resume(self.root)
        recovered._start_apply_worker_if_needed()
        thread = recovered._apply_worker_thread
        if thread is not None:
            self.threads.append(thread)
        wait_until(lambda: recovered._apply_worker_thread is None)
        self.assertEqual(recovered.get_apply_job(job_id)['state'], 'completed')
        self.assertEqual(len(adapter.submit_attempts), 1)


if __name__ == '__main__':
    unittest.main()
