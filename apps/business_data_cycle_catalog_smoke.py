"""Offline catalog race: real complete FBS generations and owned warehouse journal."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import wb_fbs_complete_snapshot_smoke as fixture
from apps import warehouse_cycle_recovery_retention_smoke as warehouse_fixture
from apps import fbs_snapshot_cost_sources_smoke as sources
from apps import sheet_vitrina_v1_cycle_smoke as cycle_fixture
from packages.application.cycle_fbs_catalog import CycleFbsCatalogGuard, CycleFbsCatalogChanged
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint as Entry
from packages.application.wb_fbs_warehouse_registry import WbFbsWarehouseRegistry
from packages.application.fbs_snapshot_cost import FbsSourceCaptureError, _capture
from packages.application.fbs_snapshot_cost_sources import capture_current
from packages.application.official_fbs_stock_read import read_complete_official_fbs_stock
from packages.application.ready_publication import readonly
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.business_time import current_business_date_iso


class Source(fixture.OfficialSource):
    def list_stocks(self, *, warehouse_id, chrt_ids):
        rows = super().list_stocks(warehouse_id=warehouse_id, chrt_ids=[v for v in chrt_ids if v != 9404])
        if 9404 in chrt_ids:
            rows.append(fixture.WbFbsStock(chrt_id=9404, amount=7))
        return rows


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'runtime.sqlite3'
        fixture._seed(self.db)
        self.clock = fixture.Clock()
        self.registry = WbFbsWarehouseRegistry(db_path=self.db, runtime_dir=self.db.parent,
            timestamp_factory=self.clock, source=Source(), catalog_source=fixture.CatalogSource())
        self.registry.collect()
        self.entry = SimpleNamespace(runtime=SimpleNamespace(db_path=self.db),
            wb_fbs_warehouse_registry=self.registry, now_factory=lambda: self.clock.value)
        self.entry._cycle_fbs_generation = lambda: Entry._cycle_fbs_generation(self.entry)
        self.initial = self.entry._cycle_fbs_generation().versions
        self.fbs = dict(self.initial)
        self.receipt = {'business_date': current_business_date_iso(self.clock.value),
            'final_versions': dict(self.initial), 'stages': [{'stage': 'fbs_generation', 'versions': self.initial}]}
        self.saved = []
        self.store = SimpleNamespace(write=lambda value: self.saved.append(deepcopy(value)))
        self.guard = CycleFbsCatalogGuard(self.entry, self.store, self.receipt, self.fbs)

    def mutate(self, sql):
        with sqlite3.connect(self.db) as conn: conn.execute(sql)

    def activate(self):
        self.mutate('UPDATE sheet_vitrina_v1_nomenclature_items SET is_active=1 WHERE nm_id=404')

    def test_unchanged_catalog_and_timestamp_only_do_not_collect(self):
        self.mutate("UPDATE sheet_vitrina_v1_nomenclature_items SET updated_at='tomorrow'")
        with patch.object(self.registry, 'collect', wraps=self.registry.collect) as collect:
            self.guard.before_plan(); self.guard.check(self.clock.value)
        collect.assert_not_called()
        self.assertEqual(self.saved, [])

    def test_activation_refreshes_one_complete_generation_before_plan(self):
        self.activate()
        with patch.object(self.registry, 'collect', wraps=self.registry.collect) as collect, \
             patch('packages.application.business_data_cycle_dispatch.selected', return_value=True), \
             heavy_admitted(self.db.parent, operation='cycle'), warehouse_functional_job_lock(self.db.parent):
            self.guard.before_plan(); self.guard.check(self.clock.value)
        self.assertEqual(collect.call_count, 1)
        self.assertNotEqual(self.fbs['fbs_generation'], self.initial['fbs_generation'])
        self.assertEqual(self.receipt['stages'][0]['versions'], self.initial)
        self.assertEqual(self.saved[0]['fbs_catalog_refresh']['status'], 'collecting')
        self.assertEqual(self.saved[1]['fbs_catalog_refresh']['prior_versions'], self.initial)
        self.assertEqual(self.receipt['final_versions'], self.fbs)
        with readonly(self.db) as conn:
            stock = read_complete_official_fbs_stock(conn, universe=None,
                day=self.receipt['business_date'], now=self.clock.value)
        self.assertEqual(stock['generation_id'], self.fbs['fbs_generation'])
        self.assertEqual(stock['sku_count'], 4)
        self.assertEqual(set(stock['skus'][404]['facilities'].values()), {7})

    def test_second_mutation_and_partial_attempt_fail_closed_without_replay(self):
        self.activate(); self.guard.before_plan()
        self.mutate('UPDATE sheet_vitrina_v1_nomenclature_items SET is_hidden=1 WHERE nm_id=101')
        with patch.object(self.registry, 'collect', wraps=self.registry.collect) as collect:
            with self.assertRaises(CycleFbsCatalogChanged): self.guard.check(self.clock.value)
            with self.assertRaises(CycleFbsCatalogChanged): self.guard.before_plan()
        collect.assert_not_called()

    def test_drift_during_refresh_does_not_collect_again(self):
        self.activate()
        original = self.entry._cycle_fbs_generation
        def collect():
            proof = original()
            self.mutate('UPDATE sheet_vitrina_v1_nomenclature_items SET is_hidden=1 WHERE nm_id=101')
            return proof
        with patch.object(self.entry, '_cycle_fbs_generation', side_effect=collect) as collected:
            with self.assertRaises(CycleFbsCatalogChanged): self.guard.before_plan()
        self.assertEqual(collected.call_count, 1)
        self.assertEqual(self.saved[-1]['fbs_catalog_refresh']['status'], 'collecting')

    def test_incomplete_recollection_never_reuses_last_good(self):
        from packages.application.sheet_vitrina_v1_cycle import FbsGenerationFailure
        self.activate()
        self.registry.source = fixture.StockFailureSource()
        with patch.object(self.registry, 'collect', wraps=self.registry.collect) as collect:
            with self.assertRaises(FbsGenerationFailure): self.guard.before_plan()
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(self.fbs, self.initial)
        self.assertEqual(self.receipt['fbs_catalog_refresh']['status'], 'collecting')

    def test_transactional_capture_reports_added_sku_reason_without_zero_fill(self):
        with TemporaryDirectory() as temp:
            db = Path(temp) / 'accounting.sqlite3'
            conn = sources.fixture(db)
            conn.execute("INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES('new',3,1,0,'today')")
            conn.commit(); conn.close()
            capture = capture_current(db, now=sources.NOW, include_baseline=False)
            self.assertFalse(capture['quantity_snapshot']['complete'])
            self.assertEqual(capture['quantity_snapshot']['rows'], [])
            with self.assertRaises(FbsSourceCaptureError) as raised: _capture(capture)
            self.assertEqual(raised.exception.diagnostic()['source_reason'], 'displayed_sku_outside_complete_catalog')
            capture['quantity_snapshot']['reason'] = 'Authorization=secret'
            self.assertNotIn('secret', json.dumps(FbsSourceCaptureError(capture).diagnostic()))


class WarehouseCatalogTests(warehouse_fixture.OwnedRetentionTests):
    def test_partial_functional_commit_is_preserved_without_accounting_retry(self):
        for failure in (CycleFbsCatalogChanged(), FbsSourceCaptureError({'documents_complete': True,
                'quantity_snapshot': {'reason': 'displayed_sku_outside_complete_catalog'}})):
            with self.subTest(kind=type(failure).__name__), ExitStack() as stack:
                self.ports(stack)
                accounting = stack.enter_context(patch('apps.warehouse_functional_runner._publish_fbs_snapshot_accounting', side_effect=failure))
                before_plan = Mock()
                stack.enter_context(heavy_admitted(self.root, operation='cycle'))
                owner = stack.enter_context(warehouse_functional_job_lock(self.root))
                before = self.applies
                with self.assertRaises(type(failure)):
                    Entry._handle_owned_warehouse_manual_sync_request(self.entry,
                        owner_token=owner['owner_token'], before_plan=before_plan)
                before_plan.assert_called_once()
                self.assertEqual(self.applies, before + 1)
                accounting.assert_called_once()
            journal = self.journal.public_status()
            publication = next(p for p in journal['phases'] if p['phase_key'] == 'functional_publication')
            self.assertEqual(publication['status'], 'failed')
            self.assertTrue(publication['details']['completed_effects']['functional_version'].startswith('fixture-'))
            self.assertEqual(publication['details']['fbs_source_failure'], failure.diagnostic())


class ReceiptTests(cycle_fixture.CycleTests):
    def test_typed_capture_terminal_preserves_original_stage_and_functional_commit(self):
        from packages.application.sheet_vitrina_v1_cycle import run_cycle
        receipt, lease = self.accepted(key='catalog-partial-functional')
        self.addCleanup(lease.close)
        entry = cycle_fixture.CycleFake(self.root)
        failure = FbsSourceCaptureError({'documents_complete': True,
            'quantity_snapshot': {'reason': 'displayed_sku_outside_complete_catalog'}})
        failure.completed_effects = {'functional_version': 'committed-fixture',
                                    'accounting_publication_not_started': True}
        def warehouse(store, receipt, fbs):
            # Mutating the active binding must not rewrite the completed stage.
            fbs['fbs_generation_fixture'] = 'refreshed-fixture'
            raise failure
        entry._cycle_warehouse = warehouse
        with heavy_admitted(self.root, operation='cycle'), self.assertRaises(FbsSourceCaptureError):
            run_cycle(entry, self.store, receipt, self.config, lambda _: None)
        saved = self.store.read(receipt['cycle_id'])
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['error_code'], 'incomplete_source_capture')
        self.assertEqual(saved['summary']['fbs_source_failure'], failure.diagnostic())
        self.assertEqual(saved['completed_effects']['functional_version'], 'committed-fixture')
        original = next(s for s in saved['stages'] if s['stage'] == 'fbs_generation')
        self.assertNotIn('fbs_generation_fixture', original['versions'])
        self.assertNotIn('daily_projection', entry.events)


if __name__ == '__main__':
    suite = unittest.TestLoader().loadTestsFromTestCase(CatalogTests)
    suite.addTest(WarehouseCatalogTests('test_partial_functional_commit_is_preserved_without_accounting_retry'))
    suite.addTest(ReceiptTests('test_typed_capture_terminal_preserves_original_stage_and_functional_commit'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
