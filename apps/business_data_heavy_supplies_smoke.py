"""Offline Supplies heavy admission: real receipts/flocks, fake external services."""
from __future__ import annotations

from contextlib import closing, redirect_stdout
import io
import hashlib
import gc
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from pathlib import Path
import sys
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ci.fixture_process import checkpoint, fixture_process
from apps.business_data_heavy_producers_smoke import busy
from apps.business_data_async_workers_smoke import hold, resume, wait_until
from apps.wb_supplies_backfill_smoke import PagedWbSuppliesSource
from apps.wb_supplies_transit_cost_enrichment_smoke import FakeTransitCostSource
from apps import wb_supplies_backfill_live as cli
from apps import wb_supplies_goods_composition_diagnostics as detail_cli
from packages.application.business_data_heavy_admission import heavy_admitted, HeavyAdmissionBusy, heavy_admission_status, require_heavy_owner
from packages.application.business_data_procedure_admission import admission_idle, initialize_admission, MaintenanceAdmissionBlocked
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint as Entry
from packages.application.wb_supplies import WbSuppliesBlock

STAMP = '2026-10-06T09:00:00Z'
REQUEST = {'limit': 10, 'enrich': False, 'resume': False}


def fixture(root):
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=root)
    source = PagedWbSuppliesSource()
    block = WbSuppliesBlock(runtime=runtime, source=source,
        transit_cost_source=FakeTransitCostSource({'fixture': 123}), timestamp_factory=lambda: STAMP)
    return runtime, block, source


def crash_between_phases(channel, root):
    _, block, _ = fixture(root)
    # This uses the production combined seam and real source save. The fixed
    # tail boundary is gated only in the disposable crash fixture.
    allowed = threading.Event()
    def tail():
        assert allowed.wait(5)
        checkpoint(channel, 'tail')
    block.collect_all_due_transit_costs = tail
    block._start_combined_full_backfill(REQUEST)
    checkpoint(channel, 'accepted')
    allowed.set()
    threading.Event().wait(10)


class SuppliesAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='heavy-supplies-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        initialize_admission(self.root)
        with heavy_admitted(self.root, operation='fixture'):
            pass
        self.runtime, self.block, self.source = fixture(self.root)

    def idle(self):
        self.assertTrue(heavy_admission_status(self.root)['idle'])
        self.assertTrue(admission_idle(self.root)['idle'])

    def start(self, kind):
        if kind == 'backfill':
            return lambda: self.block.start_full_backfill(REQUEST)
        self.block._select_transit_cost_enrichment_candidates = lambda _: [{'supply_id': 'fixture'}]
        return self.block.start_transit_cost_enrichment

    def latest(self, kind):
        if kind != 'backfill':
            return self.runtime.load_latest_wb_supply_transit_cost_enrichment_run()
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            run_id = conn.execute('SELECT run_id FROM sheet_vitrina_v1_wb_supplies_sync_runs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        return self.runtime.load_wb_supplies_sync_run(run_id)

    def test_process_busy_precedes_domain_selection_receipt_and_effects(self):
        methods = [lambda: self.block.sync_supplies({}),
            lambda: self.block.sync_functional_sources(),
            lambda: self.block.run_full_backfill(REQUEST),
            lambda: self.block.collect_transit_costs(),
            lambda: self.block.collect_all_due_transit_costs(),
            self.start('backfill'), self.start('transit')]
        with busy(self.root), patch.object(self.runtime, 'create_wb_supplies_sync_run') as accept, \
                patch.object(self.runtime, 'create_wb_supply_transit_cost_enrichment_run') as transit_accept, \
                patch.object(self.block, '_select_transit_cost_enrichment_candidates') as select:
            for method in methods:
                with self.assertRaises(HeavyAdmissionBusy):
                    method()
            accept.assert_not_called(); transit_accept.assert_not_called(); select.assert_not_called()
        self.assertEqual(self.source.list_calls, []); self.idle()

    def test_owned_warehouse_reentry_and_other_thread_busy(self):
        with heavy_admitted(self.root, operation='warehouse'):
            run = self.block.run_full_backfill(REQUEST)
            self.assertEqual(run['status'], 'success')
            self.assertEqual(self.block.collect_all_due_transit_costs()['status'], 'complete')
            failures = []
            def other():
                try:
                    self.block.sync_supplies({})
                except HeavyAdmissionBusy:
                    failures.append('busy')
            thread = threading.Thread(target=other); thread.start(); thread.join(5)
            self.assertEqual(failures, ['busy'])
            # Async acceptance requires an independent owner, never joins the
            # parent's root authority and then self-deadlocks in another thread.
            with self.assertRaises(HeavyAdmissionBusy):
                self.block.start_full_backfill(REQUEST)
        self.idle()

    def test_cached_detail_enrichment_busy_before_checkpoint_debit_network(self):
        self.block.run_full_backfill({**REQUEST, 'enrich': True})
        expected = self.block.get_supply('9000')
        from packages.application.fulfillment_services import FulfillmentServicesBlock
        FulfillmentServicesBlock(runtime=self.runtime).approved_overlay_by_supply()
        with busy(self.root), patch.object(self.source, 'fetch_supply_details') as network, \
                patch.object(self.runtime, 'save_wb_supply_rows') as save, \
                patch.object(self.block, '_ensure_ff_stock_wb_auto_writeoff_checkpoint') as checkpoint_, \
                patch.object(self.block.ff_stock_ledger, 'record_wb_supply_debit') as debit:
            cached = self.block.get_supply('9000')
            self.assertEqual(cached['meta']['enrichment'],
                {'status': 'deferred', 'reason': 'heavy_busy', 'attempted': False})
            self.assertEqual({k: v for k, v in cached.items() if k != 'meta'},
                {k: v for k, v in expected.items() if k != 'meta'})
            with self.assertRaises(HeavyAdmissionBusy): self.block.get_supply('no-cache')
            self.assertEqual(len(self.block.list_supplies({'size_filter': 'all'})['rows']), 20)
            self.block.get_sync_status(); self.block.get_transit_cost_enrichment_status()
            for effect in (network, save, checkpoint_, debit): effect.assert_not_called()
        self.idle()

    def test_cached_fallback_is_query_only_without_loader_or_ff_writer_effects(self):
        from packages.application.fulfillment_services import FulfillmentServicesBlock
        self.block.run_full_backfill({**REQUEST, 'enrich': True})
        ff = FulfillmentServicesBlock(runtime=self.runtime)
        ff.approved_overlay_by_supply()  # fixture-only schema initialization
        gc.collect()
        with closing(sqlite3.connect(self.runtime.db_path)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("INSERT INTO sheet_vitrina_v1_fulfillment_service_uploads "
                "(upload_id,original_filename,stored_file_path,file_sha256,uploaded_at,validation_status,created_at,updated_at) "
                "VALUES('cached-proof','fixture','','hash',?,'ok',?,?)", (STAMP, STAMP, STAMP))
            writer.execute("INSERT INTO sheet_vitrina_v1_fulfillment_service_lines "
                "(upload_id,row_index,supply_id_input,match_status,amount_without_vat,vat_amount,amount_with_vat,raw_row_json,created_at) "
                "VALUES('cached-proof',1,'9000','ok',100,5,105,'{}',?)", (STAMP,))
            writer.commit()
            # Warm the existing readonly SQLite WAL read mark before comparing
            # the byte-identical DB/WAL/SHM family on repeated cached requests.
            original_overlay = FulfillmentServicesBlock.approved_overlay_in_connection
            expected = self.block._cached_supply_detail('9000', reason='heavy_busy')
            self.assertEqual(expected['supply']['fulfillment_amount_with_vat_total'], 105)
            self.assertIsNone(expected['package']['summary']['package_count'])
            def snapshot():
                return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.root.rglob('*') if p.is_file() and
                    ('.sqlite' in p.name or p.name.endswith(('-wal', '-shm', '-journal')))}
            before = snapshot()
            def readonly_overlay(conn):
                self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("UPDATE sheet_vitrina_v1_fulfillment_service_lines SET amount_with_vat=999")
                return original_overlay(conn)
            self.block.fulfillment_overlay_provider = Mock(side_effect=AssertionError('writer provider called'))
            for reason in ('heavy_busy', 'maintenance'):
                with patch.object(self.runtime, 'load_wb_supply_record', side_effect=AssertionError('legacy loader')), \
                        patch.object(self.runtime, 'load_wb_supply_transit_cost_enrichment', side_effect=AssertionError('legacy loader')), \
                        patch.object(self.block, '_ensure_supply_detail_record', side_effect=AssertionError('enrichment')), \
                        patch.object(self.block.ff_stock_ledger, 'record_wb_supply_debit', side_effect=AssertionError('debit')), \
                        patch.object(FulfillmentServicesBlock, 'approved_overlay_in_connection', readonly_overlay):
                    cached = self.block._cached_supply_detail('9000', reason=reason)
                self.assertEqual(cached['composition_last_enriched_at'], expected['composition_last_enriched_at'])
                self.assertEqual(cached['raw_diagnostics'], expected['raw_diagnostics'])
                self.assertEqual(cached['supply']['fulfillment_amount_with_vat_total'], 105)
                self.assertEqual(cached['meta']['enrichment']['reason'], reason)
                self.assertEqual(snapshot(), before)
            self.block.fulfillment_overlay_provider.assert_not_called()

    def test_cached_card_and_overlay_share_pinned_snapshot(self):
        from packages.application.fulfillment_services import FulfillmentServicesBlock
        from packages.application import registry_upload_db_backed_runtime as runtime_module
        self.block.run_full_backfill({**REQUEST, 'enrich': True})
        FulfillmentServicesBlock(runtime=self.runtime).approved_overlay_by_supply()
        with closing(sqlite3.connect(self.runtime.db_path)) as setup:
            setup.execute('PRAGMA journal_mode=WAL')
            setup.execute("INSERT INTO sheet_vitrina_v1_fulfillment_service_uploads "
                "(upload_id,original_filename,stored_file_path,file_sha256,uploaded_at,validation_status,created_at,updated_at) "
                "VALUES('coherence','fixture','','hash',?,'ok',?,?)", (STAMP, STAMP, STAMP))
            setup.execute("INSERT INTO sheet_vitrina_v1_fulfillment_service_lines "
                "(upload_id,row_index,supply_id_input,match_status,amount_without_vat,vat_amount,amount_with_vat,raw_row_json,created_at) "
                "VALUES('coherence',1,'9000','ok',100,5,105,'{}',?)", (STAMP,))
            setup.commit()
        original = runtime_module._wb_supply_record_from_row
        def change_after_record(row):
            record = original(row)
            with closing(sqlite3.connect(self.runtime.db_path)) as writer:
                writer.execute("UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=NULL WHERE wb_supply_id='9000'")
                writer.execute("UPDATE sheet_vitrina_v1_fulfillment_service_lines SET amount_with_vat=999")
                writer.commit()
            return record
        with patch.object(runtime_module, '_wb_supply_record_from_row', change_after_record):
            cached = self.block._cached_supply_detail('9000', reason='heavy_busy')
        self.assertIsNotNone(cached); self.assertEqual(cached['raw_diagnostics']['goods_count'], 1)
        self.assertEqual(cached['supply']['fulfillment_amount_with_vat_total'], 105)
        # A new window truthfully sees the now incomplete cache and refuses it.
        self.assertIsNone(self.block._cached_supply_detail('9000', reason='heavy_busy'))

    def test_cli_busy_before_constructors(self):
        args = SimpleNamespace(runtime_dir=str(self.root), live_fetch=True)
        with busy(self.root), patch.dict('os.environ', {'WB_API_TOKEN': 'offline-not-a-token'}), redirect_stdout(io.StringIO()), \
                patch.object(cli, '_parse_args', return_value=args), patch.object(cli, 'RegistryUploadDbBackedRuntime') as runtime:
            self.assertEqual(cli.main(), 0); runtime.assert_not_called()
        with busy(self.root), redirect_stdout(io.StringIO()), patch('sys.argv', ['detail', '--runtime-dir', str(self.root), '--live-fetch']), \
                patch.object(detail_cli, 'RegistryUploadDbBackedRuntime') as runtime:
            self.assertEqual(detail_cli.main(), 2); runtime.assert_not_called()
        self.idle()

    def test_maintenance_rejection_before_acceptance(self):
        hold(self.root)
        for kind in ('backfill', 'transit'):
            with self.assertRaises(MaintenanceAdmissionBlocked): self.start(kind)()
        self.assertIsNone(self.runtime.load_active_wb_supplies_sync_run())
        self.assertIsNone(self.runtime.load_active_wb_supply_transit_cost_enrichment_run())
        self.idle(); resume(self.root)

    def test_no_start_constructor_failure_and_cancellation_cleanup(self):
        for kind in ('backfill', 'transit'):
            for method in ('__init__', 'start'):
                for error in (RuntimeError, KeyboardInterrupt, SystemExit):
                    with self.subTest(kind=kind, method=method, error=error):
                        with patch('threading.Thread.' + method, side_effect=error('proven no native child')):
                            with self.assertRaises(error): self.start(kind)()
                        run = self.latest(kind)
                        self.assertEqual(run['status'], 'failed')
                        self.assertEqual(run['phase'], 'worker_start_failed')
                        self.assertFalse(self.block._transit_cost_threads)
                        self.idle()
        self.assertEqual(self.source.list_calls, [])
        self.assertEqual(self.block.transit_cost_source.calls, [])

    def test_native_start_uncertainty_preserves_both_leases_before_bootstrap(self):
        for kind in ('backfill', 'transit'):
            for error in (KeyboardInterrupt, SystemExit):
                with self.subTest(kind=kind, error=error):
                    bootstrap, entered, release = threading.Event(), threading.Event(), threading.Event()
                    captured = []
                    original_bootstrap, original_start = threading.Thread._bootstrap, threading.Thread.start
                    source_method = 'list_supplies' if kind == 'backfill' else 'fetch_costs'
                    source = self.source if kind == 'backfill' else self.block.transit_cost_source
                    original_fetch = getattr(source, source_method)
                    def fetch(*args, **kwargs):
                        require_heavy_owner(self.root); entered.set(); assert release.wait(5)
                        return original_fetch(*args, **kwargs)
                    def gate(thread):
                        assert bootstrap.wait(5); original_bootstrap(thread)
                    def interrupted(thread):
                        captured.append(thread)
                        thread._started.wait = lambda timeout=None: (_ for _ in ()).throw(error())
                        original_start(thread)
                    with patch.object(source, source_method, fetch), patch('threading.Thread._bootstrap', gate), patch('threading.Thread.start', interrupted):
                        try:
                            with self.assertRaises(error): self.start(kind)()
                            self.assertFalse(captured[0].is_alive())
                            self.assertFalse(heavy_admission_status(self.root)['idle'])
                            self.assertFalse(admission_idle(self.root)['idle'])
                            with self.assertRaises(HeavyAdmissionBusy): self.start(kind)()
                            bootstrap.set(); self.assertTrue(entered.wait(5))
                            with self.assertRaises(HeavyAdmissionBusy): self.start(kind)()
                        finally:
                            release.set(); bootstrap.set(); captured[0].join(7)
                    self.assertFalse(captured[0].is_alive()); self.assertFalse(self.block._transit_cost_threads); self.idle()

    def test_async_exception_terminal_and_lease_finally(self):
        for kind in ('backfill', 'transit'):
            entered, release = threading.Event(), threading.Event()
            name = '_run_full_backfill' if kind == 'backfill' else '_run_transit_cost_enrichment'
            def fail(*args, **kwargs):
                require_heavy_owner(self.root); entered.set(); assert release.wait(5)
                raise ValueError('offline controlled source failure')
            with patch.object(self.block, name, fail):
                result = self.start(kind)()
                try:
                    self.assertTrue(entered.wait(5)); hold(self.root)
                    self.assertFalse(admission_idle(self.root)['idle'])
                    self.assertFalse(heavy_admission_status(self.root)['idle'])
                finally: release.set()
                wait_until(lambda: heavy_admission_status(self.root)['idle'])
            self.assertEqual(self.latest(kind)['status'], 'failed')
            self.idle(); resume(self.root)

    def test_running_worker_cancellation_releases_without_resending_source(self):
        for kind in ('backfill', 'transit'):
            for error in (KeyboardInterrupt, SystemExit):
                observed = threading.Event()
                name = '_run_full_backfill' if kind == 'backfill' else '_run_transit_cost_enrichment'
                def cancel(*args, **kwargs):
                    require_heavy_owner(self.root)
                    raise error('offline child cancellation')
                with patch.object(self.block, name, cancel), patch('threading.excepthook', lambda args: observed.set()):
                    result = self.start(kind)()
                    self.assertTrue(observed.wait(5))
                    wait_until(lambda: heavy_admission_status(self.root)['idle'])
                self.idle()
                self.assertFalse(self.block._transit_cost_threads)
                self.assertEqual(self.source.list_calls, [])
                self.assertEqual(self.block.transit_cost_source.calls, [])
                # Existing interrupted records remain active, never auto-fetched
                # again. Retire ONLY disposable records so the next case is new.
                update = self.runtime.update_wb_supplies_sync_run if kind == 'backfill' else self.runtime.update_wb_supply_transit_cost_enrichment_run
                update(result['run_id'], status='failed', phase='fixture_retired', updated_at=STAMP, completed_at=STAMP)

    def test_http_incremental_keeps_same_owner_across_source_and_transit(self):
        events = []
        def phase(label):
            owner = require_heavy_owner(self.root)
            events.append((label, owner, threading.current_thread()))
            return {'status': 'complete'}
        fake = SimpleNamespace(runtime=self.runtime, wb_supplies_block=SimpleNamespace(
            sync_supplies=lambda _: phase('source'), collect_all_due_transit_costs=lambda: phase('transit')))
        Entry.handle_wb_supplies_sync_request(fake, {})
        self.assertEqual([v[0] for v in events], ['source', 'transit'])
        self.assertIs(events[0][1], events[1][1]); self.assertIs(events[0][2], events[1][2]); self.idle()

    def test_combined_http_preserves_original_sync_normalization(self):
        from packages.application.wb_supplies import _normalize_sync_request
        hook = Mock(return_value={'accepted': True})
        fake = SimpleNamespace(wb_supplies_block=SimpleNamespace(_start_combined_full_backfill=hook))
        payload = {'mode': 'full_backfill', 'limit': 5, 'enrich_details': False,
                   'start_offset': 10, 'resume': False, 'max_pages': 1}
        Entry.handle_wb_supplies_sync_request(fake, payload)
        hook.assert_called_once_with(_normalize_sync_request(payload))

    def test_combined_http_order_terminal_and_tail_error_preserve_source(self):
        for outcome in ('complete', 'degraded', 'error'):
            with self.subTest(outcome=outcome):
                # Reset only disposable fixture DB for each independent job.
                root = self.root / outcome; root.mkdir()
                runtime, block, source = fixture(root)
                entered, release = threading.Event(), threading.Event()
                owners, phases = [], []
                original_fetch = source.list_supplies
                def fetch(**kwargs):
                    owners.append(require_heavy_owner(root)); phases.append('source')
                    return original_fetch(**kwargs)
                def tail():
                    owners.append(require_heavy_owner(root)); phases.append('transit')
                    entered.set(); assert release.wait(5)
                    if outcome == 'error': raise ValueError('offline transit failure')
                    return {'status': outcome, 'batches': [{'run_id': 'tail-proof'}]}
                fake = SimpleNamespace(runtime=runtime, wb_supplies_block=block)
                with patch.object(source, 'list_supplies', fetch), patch.object(block, 'collect_all_due_transit_costs', tail):
                    result = Entry.handle_wb_supplies_sync_request(fake, {'mode': 'full_backfill', **REQUEST})
                    try:
                        self.assertTrue(entered.wait(5))
                        run = runtime.load_wb_supplies_sync_run(result['run_id'])
                        self.assertEqual(run['status'], 'running')
                        self.assertEqual(run['phase'], 'backfill_completed_awaiting_transit')
                        self.assertFalse(run['completed_at'])
                        self.assertTrue(runtime.load_wb_supplies_sync_state()['backfill_complete'])
                        with self.assertRaises(HeavyAdmissionBusy): block.start_full_backfill(REQUEST)
                    finally: release.set()
                    wait_until(lambda: heavy_admission_status(root)['idle'])
                final = runtime.load_wb_supplies_sync_run(result['run_id'])
                self.assertEqual(final['status'], {'complete': 'success', 'degraded': 'partial', 'error': 'failed'}[outcome])
                self.assertTrue(final['completed_at']); self.assertTrue(runtime.load_wb_supplies_sync_state()['backfill_complete'])
                self.assertEqual(phases, ['source'] * 3 + ['transit'])
                self.assertTrue(all(owner is owners[0] for owner in owners))
                self.assertEqual(len(runtime.list_wb_supplies()), 25)
                # Separate tail retry never goes back to the official source.
                before = list(source.list_calls)
                block.collect_all_due_transit_costs()
                self.assertEqual(source.list_calls, before)

    def test_http_busy_is_conflict_and_detail_maintenance_is_locked(self):
        from packages.adapters.registry_upload_http_entrypoint import build_registry_upload_http_server, DEFAULT_WB_SUPPLIES_PATH
        from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
        fixture_bundle = ROOT / 'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json'
        self.runtime.ingest_bundle(json.loads(fixture_bundle.read_text()), activated_at=STAMP)
        entry = Entry(runtime_dir=self.root, runtime=self.runtime, activated_at_factory=lambda: STAMP)
        entry.wb_supplies_block = self.block
        self.block.run_full_backfill({**REQUEST, 'enrich': True})
        # Populate existing overlay schema before testing a pure read.
        entry.fulfillment_services_block.approved_overlay_by_supply()
        config = RegistryUploadHttpEntrypointConfig(host='127.0.0.1', port=0, runtime_dir=self.root,
            upload_path='/v1/registry-upload', sheet_plan_path='/v1/sheet-vitrina-v1/plan',
            sheet_refresh_path='/v1/sheet-vitrina-v1/refresh', sheet_status_path='/v1/sheet-vitrina-v1/status',
            sheet_operator_ui_path='/sheet-vitrina-v1')
        server = build_registry_upload_http_server(config, entrypoint=entry)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def request(suffix, data=None):
            req = Request(f'http://127.0.0.1:{server.server_address[1]}' + DEFAULT_WB_SUPPLIES_PATH + suffix,
                data=None if data is None else json.dumps(data).encode(),
                headers={'Content-Type': 'application/json'})
            try:
                response = urlopen(req, timeout=5)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, json.load(response)
        try:
            with busy(self.root):
                for suffix, data in [('/sync', {}), ('/sync', {'mode': 'full_backfill'}),
                        ('/backfill', {}), ('/transit-cost/enrich', {}), ('/transit-cost/check', {}), ('/no-cache', None)]:
                    status, payload = request(suffix, data)
                    self.assertEqual(status, 409, (suffix, payload))
                    self.assertEqual(payload['status'], 'busy'); self.assertFalse(payload['accepted'])
                    self.assertFalse(payload['effects_started'])
                status, payload = request('/9000')
                self.assertEqual(status, 200)
                self.assertEqual(payload['meta']['enrichment']['reason'], 'heavy_busy')
                self.assertEqual(request('')[0], 200)
                self.assertEqual(request('/sync-status')[0], 200)
            hold(self.root)
            status, payload = request('/9000')
            self.assertEqual(status, 200); self.assertEqual(payload['meta']['enrichment']['reason'], 'maintenance')
            status, payload = request('/no-cache')
            self.assertEqual(status, 423); self.assertEqual(payload['code'], 'business_data_maintenance')
            self.assertEqual(request('')[0], 200)
            resume(self.root)
        finally:
            server.shutdown(); server.server_close(); thread.join(5)
        self.idle()

    def test_crash_after_backfill_does_not_automatically_replay_source(self):
        with fixture_process(crash_between_phases, self.root) as child:
            child.wait('accepted'); child.release('accepted'); child.wait('tail'); child.crash()
        runtime, restarted, source = fixture(self.root)
        run = runtime.load_active_wb_supplies_sync_run()
        self.assertEqual(run['phase'], 'backfill_completed_awaiting_transit')
        self.assertTrue(runtime.load_wb_supplies_sync_state()['backfill_complete'])
        result = restarted._start_combined_full_backfill(REQUEST)
        self.assertEqual(result['status'], 'unknown'); self.assertFalse(result['accepted'])
        self.assertEqual(result['run_id'], run['run_id']); self.assertEqual(source.list_calls, [])
        self.assertEqual(len(runtime.list_wb_supplies()), 25); self.idle()

    def test_existing_uncertain_transit_runs_are_not_replayed(self):
        for stamp in ('2026-07-25T00:00:00Z', '2026-10-03T00:00:00Z'):
            root = self.root / stamp[:10]; root.mkdir()
            runtime, block, source = fixture(root)
            runtime.create_wb_supply_transit_cost_enrichment_run(run_id='uncertain', status='running',
                phase='browser_network_json', started_at=stamp, candidate_count=1)
            before = runtime.load_wb_supply_transit_cost_enrichment_run('uncertain')
            with busy(root), self.assertRaises(HeavyAdmissionBusy): block.start_transit_cost_enrichment()
            self.assertEqual(runtime.load_wb_supply_transit_cost_enrichment_run('uncertain'), before)
            result = block.start_transit_cost_enrichment()
            self.assertEqual(result['status'], 'unknown'); self.assertFalse(result['accepted'])
            self.assertEqual(block.collect_transit_costs()['status'], 'unknown')
            self.assertEqual(block.transit_cost_source.calls, []); self.assertEqual(source.list_calls, [])


if __name__ == '__main__':
    unittest.main()
