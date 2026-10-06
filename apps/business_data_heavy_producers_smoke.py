"""Offline core-producer contention/lifetime tests. No WB or production effects."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ci.fixture_process import checkpoint, fixture_process
from apps import warehouse_functional_runner as cli
from apps.warehouse_current_sync_job_smoke import entry_fixture, wait_terminal
from apps.sheet_vitrina_v1_cycle_smoke import CycleFake, CycleHistoryConfig, SLOT, NOW, STAMP
from packages.application.business_data_heavy_admission import (
    HeavyAdmissionBusy, heavy_admitted, heavy_admission_status, require_heavy_owner,
)
from packages.application.business_data_procedure_admission import admission_idle, initialize_admission
from packages.application.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypoint as Entry, SheetVitrinaV1OperatorJobStore as Jobs,
)
from packages.application.sheet_vitrina_v1_cycle import CycleReceiptStore, CycleConflict
from packages.application.warehouse_update_journal import validate_warehouse_request


def hold_heavy(channel, root):
    with heavy_admitted(root, operation='fixture'):
        checkpoint(channel, 'heavy')


@contextmanager
def busy(root):
    with fixture_process(hold_heavy, root) as child:
        child.wait('heavy')
        try:
            yield
        finally:
            child.release('heavy')
            child.finish()


class HeavyProducersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='heavy-producers-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        initialize_admission(self.root)
        # Provision once; status probes stay read-only and distinguish idle.
        with heavy_admitted(self.root, operation='fixture'):
            pass

    def assert_idle(self):
        self.assertTrue(heavy_admission_status(self.root)['idle'])
        self.assertTrue(admission_idle(self.root)['idle'])

    def test_process_busy_sync_guards_precede_domain_locks_and_effects(self):
        entry = SimpleNamespace(runtime=SimpleNamespace(runtime_dir=self.root))
        methods = ['_run_sheet_refresh', '_run_sheet_auto_update',
            '_run_sheet_scheduled_auto_update', '_run_sheet_source_group_refresh',
            'run_sheet_temporal_closure_retry_cycle', 'handle_warehouse_manual_sync_request',
            'handle_sheet_auto_refresh_request']
        with busy(self.root):
            for name in methods:
                with self.subTest(method=name), self.assertRaises(HeavyAdmissionBusy):
                    # The minimal fixture intentionally has no effect/lock objects.
                    getattr(Entry, name)(entry)
            self.assertFalse((self.root / '.warehouse-functional-job.lock').exists())
        self.assert_idle()

    def test_process_busy_async_has_no_acceptance_job_marker_or_child(self):
        store = Jobs(lambda: STAMP, runtime_dir=self.root)
        accepted, target = Mock(), Mock()
        with busy(self.root):
            for operation in ('refresh', 'auto_update', 'refresh_group', 'cycle'):
                with self.subTest(operation=operation), self.assertRaises(HeavyAdmissionBusy):
                    store.start(operation=operation, runner=target, on_accept=accepted)
            self.assertEqual(store._jobs, {})
            self.assertEqual(store._threads, {})
            accepted.assert_not_called(); target.assert_not_called()
        self.assert_idle()

    def test_cycle_busy_before_durable_receipt_and_matching_request_stays_read_only(self):
        fake = CycleFake(self.root)
        contract = self.root / 'contract.json'; contract.write_text('{}')
        config = CycleHistoryConfig(self.root / 'candidate', contract, 'epoch')
        store = CycleReceiptStore(self.root, lambda: STAMP)
        with busy(self.root), self.assertRaises(HeavyAdmissionBusy):
            fake._start_sheet_cycle_job(request_key='new', slot_utc=SLOT, history_config=config)
        self.assertFalse(store.root.exists())
        with patch('packages.application.sheet_vitrina_v1_cycle.process_identity', return_value='offline:identity'):
            receipt, slot = store.accept(request_key='old', slot_utc=SLOT, config=config, now=NOW)
        receipt['status'] = 'complete'; store.write(receipt); slot.close()
        before = (store.root / (receipt['cycle_id'] + '.json')).read_bytes()
        with busy(self.root):
            same = fake._start_sheet_cycle_job(request_key='other-key', slot_utc=SLOT, history_config=config)
            self.assertEqual(same['cycle_id'], receipt['cycle_id'])
            with self.assertRaises(CycleConflict):
                fake._start_sheet_cycle_job(request_key='old', slot_utc='2026-09-29T12:00:00Z', history_config=config)
        self.assertEqual(before, (store.root / (receipt['cycle_id'] + '.json')).read_bytes())
        self.assertEqual(fake.events, []); self.assert_idle()

    def test_async_lease_holds_through_result_and_final_marker_cleanup(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                store = Jobs(lambda: STAMP, runtime_dir=self.root)
                entered, release = threading.Event(), threading.Event()
                def finish(_):
                    entered.set(); assert release.wait(5)
                store._snapshot_markers = SimpleNamespace(start=lambda *_: 'marker', finish=finish)
                def work(_):
                    require_heavy_owner(self.root)
                    # Same-worker nested canonical admission reenters.
                    with heavy_admitted(self.root, operation='nested'):
                        require_heavy_owner(self.root)
                    if fail:
                        raise ValueError('offline failure')
                    return {'ok': True}
                job = store.start(operation='refresh', runner=work)
                thread = store._threads[job['job_id']]
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(store.get(job['job_id'])['status'], 'error' if fail else 'success')
                    self.assertFalse(heavy_admission_status(self.root)['idle'])
                    self.assertFalse(admission_idle(self.root)['idle'])
                    with self.assertRaises(HeavyAdmissionBusy):
                        Jobs(lambda: STAMP, runtime_dir=self.root).start(operation='refresh', runner=Mock())
                finally:
                    release.set(); thread.join(5)
                self.assertFalse(thread.is_alive()); self.assert_idle()

    def test_operator_and_warehouse_no_start_failures_release_both_leases(self):
        entry, effects, _ = entry_fixture(self.root)
        for failure in (RuntimeError, KeyboardInterrupt, SystemExit):
            for method in ('__init__', 'start'):
                for warehouse in (False, True):
                    with self.subTest(failure=failure, method=method, warehouse=warehouse):
                        store = Jobs(lambda: STAMP, runtime_dir=self.root)
                        with patch('threading.Thread.' + method, side_effect=failure('no native spawn')):
                            with self.assertRaises(failure):
                                if warehouse:
                                    store.start_warehouse_if_idle(runtime_dir=self.root,
                                        journal=entry.warehouse_update_journal, runner=Mock())
                                else:
                                    store.start(operation='refresh', runner=Mock())
                        self.assertFalse(store.active_job(operations=('refresh',)))
                        self.assertFalse(entry.warehouse_update_journal.needs_pickup())
                        self.assert_idle()
        self.assertEqual(effects, [])

    def test_warehouse_busy_before_journal_recovery_or_accept(self):
        entry, effects, _ = entry_fixture(self.root)
        journal = entry.warehouse_update_journal
        with busy(self.root), patch.object(journal, 'recover_and_pick') as recover, patch.object(journal, 'accept') as accept:
            result = entry.handle_warehouse_manual_sync_start_request({'request_key': 'busy-request-key-0137'})
            self.assertEqual(result['status'], 'busy'); self.assertFalse(result['request_accepted'])
            recover.assert_not_called(); accept.assert_not_called()
        self.assertEqual(effects, []); self.assert_idle()

    def test_committed_pending_busy_restart_and_drain_same_job_once(self):
        entry, effects, _ = entry_fixture(self.root)
        key, fingerprint, body = validate_warehouse_request({'request_key': 'committed-request-0137'}, 'local_operator')
        pending, created = entry.warehouse_update_journal.accept(request_key=key,
            request_scope='local_operator', payload_fingerprint=fingerprint, request_payload_json=body)
        self.assertTrue(created)
        # Simulate process restart after durable acceptance, before claim/effects.
        entry.operator_jobs = Jobs(lambda: STAMP, runtime_dir=self.root)
        with busy(self.root):
            job, blocked = entry.operator_jobs.start_warehouse_if_idle(runtime_dir=self.root,
                journal=entry.warehouse_update_journal, runner=entry._run_warehouse_manual_sync_job)
            self.assertIsNone(job); self.assertTrue(blocked)
            same = entry.warehouse_update_journal.lookup(public_id=pending['job_id'], request_scope='local_operator')
            self.assertEqual(same['status'], 'accepted'); self.assertEqual(effects, [])
        # A fresh startup picker drains the existing intent, not another request.
        entry.operator_jobs = Jobs(lambda: STAMP, runtime_dir=self.root)
        with patch('packages.application.fbs_accounting_runtime.refresh', return_value={}):
            picker = entry.operator_jobs.resume_warehouse_pending(runtime_dir=self.root,
                journal=entry.warehouse_update_journal, runner=entry._run_warehouse_manual_sync_job)
            deadline = time.monotonic() + 5
            while pending['job_id'] not in entry.operator_jobs._threads and time.monotonic() < deadline:
                time.sleep(.01)
            final = wait_terminal(entry, pending['job_id'])
            picker.join(7)
        self.assertFalse(picker.is_alive()); self.assertEqual(final['status'], 'success')
        self.assertEqual(final['run_id'], pending['job_id'])
        for effect in ('network', 'cost', 'finance', 'apply'):
            self.assertEqual(effects.count(effect), 1)
        self.assertFalse(entry.warehouse_update_journal.needs_pickup()); self.assert_idle()

    def test_picker_no_start_and_uncertain_native_start_preserves_single_idle_loop(self):
        for failure in (KeyboardInterrupt, SystemExit):
            store = Jobs(lambda: STAMP, runtime_dir=self.root)
            journal = SimpleNamespace(needs_pickup=lambda: True)
            with patch('threading.Thread.start', side_effect=failure):
                with self.assertRaises(failure):
                    store.resume_warehouse_pending(runtime_dir=self.root, journal=journal, runner=Mock())
            self.assertIsNone(store._warehouse_pending_picker); self.assert_idle()
            bootstrap, entered, finish = threading.Event(), threading.Event(), threading.Event()
            original_bootstrap, original_start = threading.Thread._bootstrap, threading.Thread.start
            journal.needs_pickup = lambda: not finish.is_set()
            def idle_wait(**kwargs):
                entered.set(); assert finish.wait(5)
                return None, True
            store.start_warehouse_if_idle = idle_wait
            def gated(thread):
                assert bootstrap.wait(5); original_bootstrap(thread)
            def interrupted(thread):
                thread._started.wait = lambda timeout=None: (_ for _ in ()).throw(failure())
                original_start(thread)
            with patch('threading.Thread._bootstrap', gated), patch('threading.Thread.start', interrupted):
                with self.assertRaises(failure):
                    store.resume_warehouse_pending(runtime_dir=self.root, journal=journal, runner=Mock())
            thread = store._warehouse_pending_picker
            try:
                self.assertIsNotNone(thread); self.assertFalse(thread.is_alive())
                self.assertIs(store.resume_warehouse_pending(runtime_dir=self.root, journal=journal, runner=Mock()), thread)
                self.assert_idle()  # Perpetual wait owns neither SH nor heavy EX.
                bootstrap.set(); self.assertTrue(entered.wait(5))
                self.assertIs(store.resume_warehouse_pending(runtime_dir=self.root, journal=journal, runner=Mock()), thread)
                self.assert_idle()
            finally:
                finish.set(); bootstrap.set(); thread.join(7)
            self.assertIsNone(store._warehouse_pending_picker); self.assert_idle()

    def test_warehouse_native_start_uncertainty_keeps_lease_before_and_after_bootstrap(self):
        for failure in (KeyboardInterrupt, SystemExit):
            entry, effects, _ = entry_fixture(self.root)
            store = Jobs(lambda: STAMP, runtime_dir=self.root)
            bootstrap, entered, finish = threading.Event(), threading.Event(), threading.Event()
            original_bootstrap, original_start = threading.Thread._bootstrap, threading.Thread.start
            threads = []
            def gated(thread):
                assert bootstrap.wait(5); original_bootstrap(thread)
            def interrupted(thread):
                threads.append(thread)
                thread._started.wait = lambda timeout=None: (_ for _ in ()).throw(failure())
                original_start(thread)
            def recover():
                entered.set(); assert finish.wait(5)
                return None
            with patch.object(entry.warehouse_update_journal, 'recover_and_pick', side_effect=recover) as recovery:
                with patch('threading.Thread._bootstrap', gated), patch('threading.Thread.start', interrupted):
                    with self.assertRaises(failure):
                        store.start_warehouse_if_idle(runtime_dir=self.root,
                            journal=entry.warehouse_update_journal, runner=Mock())
                try:
                    self.assertFalse(threads[0].is_alive())
                    self.assertFalse(heavy_admission_status(self.root)['idle'])
                    self.assertFalse(admission_idle(self.root)['idle'])
                    self.assertEqual(store.start_warehouse_if_idle(runtime_dir=self.root,
                        journal=entry.warehouse_update_journal, runner=Mock()), (None, True))
                    recovery.assert_not_called()
                    bootstrap.set(); self.assertTrue(entered.wait(5))
                    self.assertEqual(store.start_warehouse_if_idle(runtime_dir=self.root,
                        journal=entry.warehouse_update_journal, runner=Mock()), (None, True))
                    self.assertEqual(recovery.call_count, 1)
                finally:
                    bootstrap.set(); finish.set(); threads[0].join(5)
            self.assertEqual(effects, []); self.assert_idle()

    def test_cli_guards_precede_constructors_and_readback_stays_available(self):
        commands = set(cli.build_parser()._subparsers._group_actions[0].choices) - {'readback'}
        with busy(self.root), patch.object(cli, '_run_admitted') as work:
            for command in commands:
                args = SimpleNamespace(command=command, runtime_dir=str(self.root))
                with self.subTest(command=command), self.assertRaises(HeavyAdmissionBusy):
                    cli._run(args, sqlite_busy_timeout_ms=None)
            work.assert_not_called()
            args.command = 'readback'; work.return_value = {'status': 'ready'}
            self.assertEqual(cli._run(args, sqlite_busy_timeout_ms=None), {'status': 'ready'})
            self.assertEqual(work.call_count, 1)
        self.assert_idle()

    def test_raw_auto_schedule_resolver_cannot_consume_due_while_busy(self):
        entry = SimpleNamespace(runtime=SimpleNamespace(runtime_dir=self.root),
            operator_jobs=Jobs(lambda: STAMP, runtime_dir=self.root),
            _resolve_auto_refresh_schedule_context=Mock())
        with busy(self.root), self.assertRaises(HeavyAdmissionBusy):
            Entry.start_sheet_auto_refresh_job(entry, trigger_source='scheduled')
        entry._resolve_auto_refresh_schedule_context.assert_not_called()
        self.assert_idle()

    def test_http_busy_is_conflict_without_success_or_source_failure(self):
        import json
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from packages.adapters.registry_upload_http_entrypoint import build_registry_upload_http_server
        from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
        entry, _, _ = entry_fixture(self.root)
        for method in ('start_sheet_refresh_job', 'handle_sheet_refresh_request',
                'start_sheet_source_group_refresh_job', 'handle_sheet_web_vitrina_health_recovery_start_request'):
            setattr(entry, method, Mock(side_effect=HeavyAdmissionBusy('heavy_producer_running')))
        config = RegistryUploadHttpEntrypointConfig(host='127.0.0.1', port=0, runtime_dir=self.root,
            upload_path='/v1/registry/upload', sheet_plan_path='/v1/sheet-vitrina-v1/plan',
            sheet_refresh_path='/v1/sheet-vitrina-v1/refresh', sheet_status_path='/v1/sheet-vitrina-v1/status',
            sheet_operator_ui_path='/v1/sheet-vitrina-v1/operator')
        server = build_registry_upload_http_server(config, entrypoint=entry)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            for path, body in [(config.sheet_refresh_path, {'async': True}),
                    (config.sheet_refresh_path, {}),
                    ('/v1/sheet-vitrina-v1/web-vitrina/group-refresh', {'source_group_id': 'wb_api'}),
                    ('/v1/sheet-vitrina-v1/web-vitrina/health/recovery/start', {})]:
                request = Request(f'http://127.0.0.1:{server.server_port}{path}',
                    data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}, method='POST')
                with self.subTest(path=path, body=body), self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=5)
                response = json.loads(error.exception.read())
                self.assertEqual(error.exception.code, 409)
                self.assertEqual(response['status'], 'busy'); self.assertTrue(response['retryable'])
                self.assertFalse(response['effects_started']); self.assertNotIn('job_id', response)
        finally:
            server.shutdown(); thread.join(5); server.server_close()
        self.assert_idle()

    def test_owned_history_rejects_token_without_actual_heavy_owner(self):
        from apps.web_vitrina_history_candidate_build import build_owned_cycle_history
        with self.assertRaisesRegex(RuntimeError, 'live heavy producer ownership'):
            build_owned_cycle_history(runtime=SimpleNamespace(runtime_dir=self.root),
                config=None, cycle_owner={'pid': 1, 'operation': 'cycle'}, now=NOW)
        with heavy_admitted(self.root, operation='refresh'), self.assertRaisesRegex(RuntimeError, 'owned cycle'):
            build_owned_cycle_history(runtime=SimpleNamespace(runtime_dir=self.root),
                config=None, cycle_owner={'operation': 'cycle'}, now=NOW)
        self.assert_idle()


if __name__ == '__main__':
    unittest.main()
