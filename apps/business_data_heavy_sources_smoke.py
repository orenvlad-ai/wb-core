"""Offline heavy Finance/FBS admission and committed-source recovery proofs."""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date, timedelta
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.business_data_heavy_producers_smoke import busy
from ci.fixture_process import checkpoint, fixture_process
from apps import wb_finance_daily as daily_cli, wb_finance_weekly as weekly_cli
from apps import wb_fbs_warehouse_registry as fbs_cli, ff_pool_overhead_backfill as ff_cli
from apps.wb_finance_daily_smoke import NOW, _rows, _Client
from apps.wb_finance_weekly_smoke import _seed_canonical_cost
from packages.application.business_data_heavy_admission import (
    HeavyAdmissionBusy, heavy_admitted, heavy_admission_status,
)
from packages.application.business_data_procedure_admission import admission_idle, MaintenanceAdmissionBlocked
from packages.application.wb_finance_daily import WbFinanceDailyBlock
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
from packages.application.wb_fbs_warehouse_registry import WbFbsWarehouseRegistry
from packages.application.ff_pool_overhead_backfill import FfPoolOverheadBackfill
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint as Entry

DAY = date(2026, 9, 28)


def repair_after_restart(channel, root):
    from apps.warehouse_functional_runner import _recalculate_downstream_finance_cost
    runtime = SimpleNamespace(runtime_dir=root)
    weekly = WbFinanceWeeklyBlock(root, seller_id='seller-1', now_factory=lambda: NOW)
    with patch('apps.warehouse_functional_runner.block_from_env', return_value=weekly):
        assert _recalculate_downstream_finance_cost(runtime)['status'] == 'applied'
    assert weekly.recalculate_stale_cost_weeks()['status'] == 'already_current'
    daily = WbFinanceDailyBlock(root, seller_id='seller-1', now_factory=lambda: NOW)
    assert daily.repair_visible_projections()['status'] == 'ok'
    payload = daily.build_daily_payload()['days'][-1]
    assert payload['status'] != 'stale_projection'
    assert payload['metrics']['cogs'] != '300.0000'
    checkpoint(channel, 'repaired')


class HeavySourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='heavy-sources-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with heavy_admitted(self.root, operation='fixture'):
            pass

    def idle(self):
        self.assertTrue(heavy_admission_status(self.root)['idle'])
        self.assertTrue(admission_idle(self.root)['idle'])

    def test_all_finance_lower_seams_reject_before_schema_or_scan(self):
        weekly = WbFinanceWeeklyBlock(self.root)
        daily = WbFinanceDailyBlock(self.root)
        methods = ['sync_week', 'ingest_week', 'recover_receipted_split_outbox',
            'recalculate_week', 'preview_candidate_week', 'refresh_recent_spp',
            'run_backfill', 'recalculate_all_weeks', 'plan_canonical_finance_backfill',
            'apply_canonical_finance_backfill', 'plan_stale_cost_weeks',
            'apply_stale_cost_weeks', 'recalculate_stale_cost_weeks', 'repair_orphan_derived_rows']
        with busy(self.root):
            for block, names in ((weekly, methods), (daily,
                    ['project_pointer', 'sync_day', 'tick', 'repair_visible_projections'])):
                for name in names:
                    with self.subTest(name=name), self.assertRaises(HeavyAdmissionBusy):
                        # Missing required arguments also prove admission precedes body.
                        getattr(block, name)()
            self.assertFalse(weekly.db_path.exists())
        self.idle()

    def test_finance_cli_busy_precedes_constructor_lock_and_before_image(self):
        cases = [(daily_cli, 'daily_block_from_env', ['ensure-schema', 'tick', 'bootstrap', 'sync-day']),
            (weekly_cli, 'block_from_env', ['ensure-schema', 'backfill', 'sync-week',
                'recalculate', 'recalculate-all', 'recalculate-stale-cost',
                'canonical-cost-backfill', 'repair-derived-orphans', 'tick', 'refresh-spp'])]
        with busy(self.root):
            for module, constructor, commands in cases:
                for command in commands:
                    with self.subTest(command=command, module=module.__name__), \
                            patch.object(module, constructor) as factory, redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(module.main([command, '--runtime-dir', str(self.root),
                            '--env-file', str(self.root / 'absent')]), 0)
                        result = json.loads(output.getvalue())
                        self.assertEqual(result['status'], 'busy')
                        self.assertFalse(result['effects_started']); factory.assert_not_called()
            self.assertFalse((self.root / '.wb-finance-daily-worker.lock').exists())
        self.idle()

    def test_readback_bypasses_heavy_and_does_not_create_business_data(self):
        with busy(self.root):
            for module, constructor, method in ((daily_cli, 'daily_block_from_env', 'build_daily_payload'),
                    (weekly_cli, 'block_from_env', 'build_payload')):
                block = Mock(); getattr(block, method).return_value = {'status': 'ok'}
                with patch.object(module, constructor, return_value=block), redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(['status', '--runtime-dir', str(self.root),
                        '--env-file', str(self.root / 'absent')]), 0)
            with patch.object(fbs_cli, 'WbFbsWarehouseRegistry') as factory:
                fbs_cli.run(SimpleNamespace(runtime_dir=self.root, env_file='', command='readback'))
                factory.return_value.collect.assert_not_called()
                factory.return_value.read_model.assert_called_once()
        self.idle()

    def test_new_writer_runtime_and_same_owner_reentry(self):
        nested = self.root / 'new-runtime'
        block = WbFinanceDailyBlock(nested, seller_id='seller-1', now_factory=lambda: NOW)
        self.assertEqual(block.sync_day(DAY, _Client())['status'], 'waiting')
        self.assertTrue(block.db_path.exists())
        with heavy_admitted(self.root, operation='cycle'):
            weekly = WbFinanceWeeklyBlock(self.root, now_factory=lambda: NOW)
            daily = WbFinanceDailyBlock(self.root, now_factory=lambda: NOW)
            self.assertEqual(weekly.sync_week(DAY, DAY + timedelta(days=6),
                SimpleNamespace(fetch_week=lambda *_: []))['status'], 'waiting')
            self.assertEqual(daily.sync_day(DAY, _Client())['status'], 'waiting')
            self.assertFalse(heavy_admission_status(self.root)['idle'])
        self.idle()

    def test_source_fetch_and_error_cleanup_keep_full_lifetime(self):
        for fail in (False, True):
            block = WbFinanceDailyBlock(self.root, seller_id='seller-1', now_factory=lambda: NOW)
            block.ensure_schema()
            entered, release = threading.Event(), threading.Event()
            calls, results = [], []
            def fetch(**kwargs):
                calls.append(kwargs); entered.set()
                if not release.wait(5): raise AssertionError('fixture wait expired')
                if fail: raise ValueError('offline source failure')
                return _Client().fetch_report(**kwargs)
            def work():
                try:
                    results.append(block.sync_day(DAY, SimpleNamespace(fetch_report=fetch)))
                except ValueError as exc:
                    results.append(exc)
            thread = threading.Thread(target=work)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(heavy_admission_status(self.root)['idle'])
                self.assertFalse(admission_idle(self.root)['idle'])
                with self.assertRaises(HeavyAdmissionBusy):
                    block.repair_visible_projections()
            finally:
                release.set(); thread.join(5)
            self.assertFalse(thread.is_alive()); self.assertEqual(len(calls), 1)
            if fail:
                self.assertIsInstance(results[0], ValueError)
            else:
                self.assertEqual(results[0]['status'], 'waiting')
            self.idle()

    def test_fbs_explicit_root_and_busy_never_records_failed_attempt(self):
        registry = WbFbsWarehouseRegistry(db_path=self.root / 'split' / 'operational.sqlite3',
            runtime_dir=self.root, source=Mock(), catalog_source=Mock())
        with busy(self.root), self.assertRaises(HeavyAdmissionBusy):
            registry.collect()
        registry.source.list_seller_warehouses.assert_not_called()
        self.assertFalse(registry.db_path.exists())
        registry = WbFbsWarehouseRegistry(db_path=self.root / 'readonly.sqlite3')
        with self.assertRaisesRegex(RuntimeError, 'explicit runtime_dir'):
            registry.collect()
        self.assertFalse(registry.db_path.exists()); self.idle()

    def test_fbs_success_and_failed_attempt_hold_lease_through_commit(self):
        from apps.wb_fbs_complete_snapshot_smoke import _seed, OfficialSource, CatalogSource, Clock
        for fail in (False, True):
            db = self.root / ('failure.sqlite3' if fail else 'success.sqlite3')
            _seed(db)
            source = OfficialSource()
            if fail:
                source.list_seller_warehouses = Mock(side_effect=ValueError('fixture'))
            registry = WbFbsWarehouseRegistry(db_path=db, runtime_dir=self.root,
                source=source, catalog_source=CatalogSource(), timestamp_factory=Clock())
            persist = registry._persist
            entered, release = threading.Event(), threading.Event()
            outcome = []
            def gated(**kwargs):
                entered.set()
                if not release.wait(5): raise AssertionError('fixture wait expired')
                return persist(**kwargs)
            with patch.object(registry, '_persist', side_effect=gated):
                worker = threading.Thread(target=lambda: outcome.append(registry.collect()))
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertFalse(heavy_admission_status(self.root)['idle'])
                    self.assertFalse(admission_idle(self.root)['idle'])
                    with self.assertRaises(HeavyAdmissionBusy): registry.collect()
                finally:
                    release.set(); worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcome[0]['latest_attempt']['status'], 'failed' if fail else 'success')
            self.idle()

    def test_committed_daily_raw_keeps_admission_until_projection_finishes(self):
        block = WbFinanceDailyBlock(self.root, seller_id='seller-1', now_factory=lambda: NOW)
        block.ensure_schema(); _seed_canonical_cost(block.db_path)
        project = block.project_pointer
        entered, release = threading.Event(), threading.Event()
        results = []
        def gated(*args, **kwargs):
            entered.set()
            if not release.wait(5): raise AssertionError('fixture wait expired')
            return project(*args, **kwargs)
        client = _Client(_rows(DAY))
        with patch.object(block, 'project_pointer', side_effect=gated):
            worker = threading.Thread(target=lambda: results.append(block.sync_day(DAY, client)))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with sqlite3.connect(block.db_path) as conn:
                    raw_before = conn.execute('SELECT COUNT(*) FROM wb_finance_daily_raw_rows').fetchone()[0]
                self.assertGreater(raw_before, 0)
                with self.assertRaises(HeavyAdmissionBusy): block.repair_visible_projections()
            finally:
                release.set(); worker.join(5)
        self.assertFalse(worker.is_alive()); self.assertEqual(len(client.calls), 1)
        self.assertEqual(results[0]['status'], 'loaded_preliminary')
        with sqlite3.connect(block.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM wb_finance_daily_raw_rows').fetchone()[0], raw_before)
        self.idle()

    def test_projection_failure_retains_raw_and_restart_repairs_without_refetch(self):
        block = WbFinanceDailyBlock(self.root, seller_id='seller-1', now_factory=lambda: NOW)
        block.ensure_schema(); _seed_canonical_cost(block.db_path)
        client = _Client(_rows(DAY))
        with patch.object(block, 'project_pointer', side_effect=ValueError('projection fixture')):
            with self.assertRaises(ValueError): block.sync_day(DAY, client)
        self.assertEqual(len(client.calls), 1)
        with sqlite3.connect(block.db_path) as conn:
            before = conn.execute('SELECT COUNT(*) FROM wb_finance_daily_raw_rows').fetchone()[0]
        self.assertGreater(before, 0); self.idle()
        fresh = WbFinanceDailyBlock(self.root, seller_id='seller-1', now_factory=lambda: NOW)
        self.assertEqual(fresh.repair_visible_projections()['status'], 'ok')
        self.assertTrue(fresh.build_daily_payload()['days'][-1]['metrics'])
        with sqlite3.connect(block.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM wb_finance_daily_raw_rows').fetchone()[0], before)
        self.assertEqual(len(client.calls), 1); self.idle()

    def test_cancellation_unwinds_finance_admission_without_resending(self):
        block = WbFinanceDailyBlock(self.root, now_factory=lambda: NOW)
        fetch = Mock(side_effect=SystemExit('cancelled'))
        with self.assertRaises(SystemExit):
            block.sync_day(DAY, SimpleNamespace(fetch_report=fetch))
        fetch.assert_called_once(); self.idle()

    def test_fbs_cli_and_ff_plan_apply_reject_before_constructor_or_effect(self):
        with busy(self.root), patch.object(fbs_cli, 'WbFbsWarehouseRegistry') as factory:
            with self.assertRaises(HeavyAdmissionBusy):
                fbs_cli.run(SimpleNamespace(runtime_dir=self.root, env_file='', command='collect'))
            factory.assert_not_called()
        ff = SimpleNamespace(runtime=SimpleNamespace(runtime_dir=self.root))
        with busy(self.root):
            for method in ('build_plan', 'apply'):
                with self.subTest(method=method), self.assertRaises(HeavyAdmissionBusy):
                    getattr(FfPoolOverheadBackfill, method)(ff)
            for command in ('dry-run', 'apply'):
                with patch.object(ff_cli, 'FfPoolOverheadBackfill') as factory, self.assertRaises(HeavyAdmissionBusy):
                    ff_cli.run(SimpleNamespace(runtime_dir=self.root, command=command))
                factory.assert_not_called()
        self.idle()

    def test_explicit_cost_request_busy_before_first_rebuild(self):
        entry = SimpleNamespace(runtime=SimpleNamespace(runtime_dir=self.root), our_wb_cost_block=Mock())
        with busy(self.root), self.assertRaises(HeavyAdmissionBusy):
            Entry.handle_our_wb_cost_recalculate_request(entry)
        entry.our_wb_cost_block.rebuild_all.assert_not_called(); self.idle()

    def test_failed_derived_tail_preserves_accepted_source_without_fake_ack(self):
        for exception, status in ((ValueError('fixture'), 'failed'), (HeavyAdmissionBusy('busy'), 'deferred'),
                (MaintenanceAdmissionBlocked('paused'), 'deferred')):
            entry = SimpleNamespace(wb_finance_weekly_block=SimpleNamespace(
                recalculate_stale_cost_weeks=Mock(side_effect=exception)))
            source = {'status': 'ok', 'item': {'item_id': 'saved', 'updated_at': 'v2'}}
            result = Entry._attach_wb_finance_cost_recalculation(entry, source)
            self.assertEqual(result['status'], 'ok'); self.assertEqual(result['item'], source['item'])
            tail = result['wb_finance_cost_recalculation']
            self.assertEqual(tail['status'], status); self.assertFalse(tail['exact_revision_acked'])
            self.assertEqual(tail['source_revision_count'], 1)
            self.assertTrue(tail['source_revision_fingerprint'].startswith('sha256:'))
        failed = Entry._attach_wb_finance_cost_recalculation(entry, {'status': 'error'})
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['wb_finance_cost_recalculation']['status'], 'not_required')

    def test_committed_nomenclature_busy_restart_repairs_week_and_day_without_resave(self):
        from apps.nomenclature_activation_intents_smoke import fixture
        from apps.ff_pool_dense_fbs_smoke import _sku
        from packages.application.supplier_shipments import SupplierShipmentsBlock
        runtime = fixture(self.root, facilities=0)
        item = {**_sku(101, updated_at=NOW.isoformat().replace('+00:00', 'Z')), 'vendor_code': 'VC101',
            'barcode': '4600000000101', 'barcodes': ['4600000000101'], 'product_type': 'clear', 'match_key': 'clear|iphone16'}
        runtime.save_nomenclature_item(item)
        # Copy only canonical cost fixture tables into the real runtime schema.
        seed = self.root / 'seed.sqlite3'; _seed_canonical_cost(seed)
        with sqlite3.connect(runtime.db_path) as conn, sqlite3.connect(seed) as source:
            for table in ('sheet_vitrina_v1_warehouse_functional_cutovers',
                    'sheet_vitrina_v1_warehouse_wb_daily_cost'):
                sql = source.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
                conn.execute(sql.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1))
                rows = source.execute('SELECT * FROM ' + table).fetchall()
                conn.executemany('INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in rows[0]) + ')', rows)
        weekly = WbFinanceWeeklyBlock(self.root, seller_id='seller-1', now_factory=lambda: NOW)
        daily = WbFinanceDailyBlock(self.root, seller_id='seller-1', now_factory=lambda: NOW)
        row = dict(_rows(DAY)[0], nmId=0, vendorCode='VC101', sku='', saleDt='2026-07-01')
        weekly.ingest_week(DAY, DAY + timedelta(days=6), [row])
        daily.sync_day(DAY, _Client([row]))
        self.assertEqual(daily.build_daily_payload()['days'][-1]['metrics']['cogs'], '300.0000')
        supplier = SupplierShipmentsBlock.__new__(SupplierShipmentsBlock)
        supplier.runtime = runtime; supplier.timestamp_factory = lambda: '2026-09-29T09:01:00Z'
        supplier.create_sku_group({'group_key':'clear','label':'Clean'})
        supplier._validate_nomenclature_group = lambda *_, **kw: None
        supplier._validate_nomenclature_unique = lambda *_, **kw: None
        supplier._sync_nomenclature_barcode_item = lambda item, **kw: (item, {})
        entry = Entry.__new__(Entry); entry.runtime = runtime
        entry.supplier_shipments_block = supplier; entry.wb_finance_weekly_block = weekly
        with busy(self.root):
            saved = entry.handle_nomenclature_patch_request(item['item_id'], {'vendor_code': 'VC999'})
        self.assertEqual(saved['status'], 'ok')
        self.assertEqual(saved['acceptance']['state'], 'processing')
        self.assertEqual(saved['acceptance']['reason_code'], 'native_finance_pending')
        committed = runtime.load_nomenclature_item(item['item_id'])
        self.assertEqual(committed['vendor_code'], 'VC999')
        self.assertEqual(daily.build_daily_payload()['days'][-1]['status'], 'stale_projection')
        with sqlite3.connect(runtime.db_path) as conn:
            counts = tuple(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('wb_finance_weekly_raw_rows', 'wb_finance_daily_raw_rows'))
        # A spawned process consumes durable source and existing raw receipts;
        # it runs the actual next warehouse Finance tail, without source resend.
        with fixture_process(repair_after_restart, self.root) as child:
            child.wait('repaired'); child.release('repaired'); child.finish()
        self.assertEqual(runtime.load_nomenclature_item(item['item_id']), committed)
        from packages.application.operator_nomenclature import read
        self.assertEqual(read(runtime.db_path,saved['acceptance']['operation_id'],actor='local_operator')['state'],'completed')
        with sqlite3.connect(runtime.db_path) as conn:
            self.assertEqual(tuple(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('wb_finance_weekly_raw_rows', 'wb_finance_daily_raw_rows')), counts)
        self.assertEqual(WbFinanceWeeklyBlock(self.root, seller_id='seller-1', now_factory=lambda: NOW)
            .recalculate_stale_cost_weeks()['status'], 'already_current')
        self.idle()


if __name__ == '__main__':
    unittest.main()
