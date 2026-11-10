"""GET/export preserve catalog source revisions, including legacy metadata."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.nomenclature_activation_intents_smoke import fixture
from packages.application.supplier_shipments import SupplierShipmentsBlock
from packages.application import supplier_shipments as suppliers
from packages.application import registry_upload_db_backed_runtime as database


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = fixture(Path(self.temp.name), facilities=1)
        self.block = SupplierShipmentsBlock(runtime=self.runtime)

    def snapshot(self):
        with closing(sqlite3.connect(self.runtime.db_path.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.execute('PRAGMA query_only=ON')
            return list(conn.iterdump())

    def assert_reads_do_not_write(self):
        before = self.snapshot()
        with patch.object(database, '_connect', side_effect=AssertionError('GET opened writer')):
            first = self.block.list_nomenclature()
            self.block.export_nomenclature_xlsx()
            self.block.list_sku_groups()
            self.assertEqual(first, self.block.list_nomenclature())
        self.assertEqual(before, self.snapshot())
        return first

    def test_empty_catalog_default_groups_are_readonly(self):
        self.assertEqual([], self.assert_reads_do_not_write()['items'])
        self.assertTrue(self.block.list_sku_groups()['groups'])
        self.assertEqual([], self.runtime.list_sku_groups(read_only=True))

    def test_legacy_models_display_without_backfill_or_source_drift(self):
        self.runtime.save_nomenclature_item(dict(item_id='legacy', nm_id=101,
            nomenclature_name='Clean iPhone 16 Pro', product_type='clean',
            match_key='clean|iphone_16_pro', is_active=False,
            compatible_model_keys=[], compatible_models_text='',
            created_at='2026-11-01T00:00:00Z', updated_at='2026-11-01T00:00:00Z'))
        before = self.runtime.list_nomenclature_items(read_only=True)[0]
        view = self.assert_reads_do_not_write()['items'][0]
        self.assertTrue(view['compatible_model_keys'])
        self.assertEqual(before['operator_source_revision'], view['operator_source_revision'])
        self.assertEqual([], self.runtime.list_nomenclature_items(read_only=True)[0]['compatible_model_keys'])

    def test_config_seed_is_virtual_until_explicit_write(self):
        from apps.registry_upload_db_backed_runtime_smoke import INPUT_BUNDLE_FIXTURE, ACTIVATED_AT
        self.runtime.ingest_bundle_from_path(INPUT_BUNDLE_FIXTURE, activated_at=ACTIVATED_AT)
        view = self.assert_reads_do_not_write()['items']
        self.assertTrue(view)
        self.assertEqual([], self.runtime.list_nomenclature_items(read_only=True))
        first = view[0]
        updated = self.block.update_nomenclature_item(first['item_id'], {'comment':'explicit edit'})
        self.assertEqual('explicit edit', updated['item']['comment'])
        self.assertEqual(len(view), len(self.runtime.list_nomenclature_items(read_only=True)))

    def test_new_and_existing_wb_cards_fill_models_in_write_candidate(self):
        card = dict(nm_id=101, vendor_code='Clean iPhone 16 Pro', title='Glass', barcodes=[])
        item = suppliers._new_nomenclature_item_from_wb_card(card, groups=list(suppliers.DEFAULT_SKU_GROUPS),
            created_at='2026-11-01T00:00:00Z', updated_at='2026-11-01T00:00:00Z')
        self.assertTrue(item['compatible_model_keys'])
        item.update(compatible_model_keys=[], compatible_models_text='', nomenclature_name='iPhone 16 Pro')
        updated = suppliers._apply_wb_card_to_existing_nomenclature(item, card=card, match_type='nm_id', synced_at='2026-11-02T00:00:00Z')
        self.assertTrue(updated['compatible_model_keys'])

    def seed_config(self):
        from apps.registry_upload_db_backed_runtime_smoke import INPUT_BUNDLE_FIXTURE, ACTIVATED_AT
        self.runtime.ingest_bundle_from_path(INPUT_BUNDLE_FIXTURE, activated_at=ACTIVATED_AT)
        return self.assert_reads_do_not_write()['items']

    def test_create_preserves_entire_virtual_catalog_without_immediate_activation(self):
        before = self.seed_config()
        with patch('packages.application.nomenclature_activation_intents.drain_nomenclature_activation_intents',
                   side_effect=AssertionError('HTTP bootstrap ran heavy activation')):
            added = self.block.create_nomenclature_item(dict(nm_id=999999, is_active=False,
                product_type='clean', nomenclature_name='Synthetic iPhone 16',
                match_key='clean|synthetic-extra', barcode='4600000999999'))['item']
        actual = self.runtime.list_nomenclature_items(read_only=True)
        self.assertEqual({r['item_id'] for r in before} | {added['item_id']}, {r['item_id'] for r in actual})
        self.assertEqual(len(before) + 1, len(actual))

    def test_single_barcode_http_bootstrap_callback_and_ordinary_activation(self):
        from apps.nomenclature_barcode_smoke import FakeBarcodeSource
        from apps.nomenclature_activation_intents_smoke import ordinary
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        from packages.application import operator_nomenclature as operator
        from packages.application.business_data_heavy_admission import heavy_admitted
        before = self.seed_config()
        item = before[0]
        provider = FakeBarcodeSource({item['nm_id']: ['4600000999998']})
        self.block.barcode_source = provider
        entry = RegistryUploadHttpEntrypoint.__new__(RegistryUploadHttpEntrypoint)
        entry.runtime, entry.supplier_shipments_block = self.runtime, self.block
        with patch('packages.application.nomenclature_activation_intents.drain_nomenclature_activation_intents',
                   side_effect=AssertionError('HTTP acceptance ran heavy activation')):
            result = entry.handle_nomenclature_item_barcode_sync_request(item['item_id'],
                actor='tester', request_id='opsku_' + '9' * 32)
        self.assertEqual('processing', result['acceptance']['state'])
        self.assertEqual([], provider.calls)
        self.assertEqual(len(before), len(self.runtime.list_nomenclature_items(read_only=True)))
        with heavy_admitted(self.runtime.runtime_dir, operation='fixture'):
            operator.drain_external(self.runtime, block=self.block)
        self.assertEqual([[item['nm_id']]], provider.calls)
        self.assertEqual('4600000999998', self.runtime.load_nomenclature_item(item['item_id'])['barcode'])
        ordinary(self.runtime)
        self.assertTrue(self.runtime.load_nomenclature_item(item['item_id'])['is_active'])

    def test_bootstrap_failure_does_not_leave_partial_catalog(self):
        before = self.seed_config()
        native = database._upsert_nomenclature_rows
        def fail(conn, rows):
            native(conn, rows[:1])
            raise RuntimeError('fixture interruption before commit')
        with patch.object(database, '_upsert_nomenclature_rows', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'fixture interruption'):
                self.block._seed_nomenclature_from_current_config_if_empty()
        self.assertEqual([], self.runtime.list_nomenclature_items(read_only=True))
        self.block._seed_nomenclature_from_current_config_if_empty()
        self.assertEqual({r['item_id'] for r in before},
                         {r['item_id'] for r in self.runtime.list_nomenclature_items(read_only=True)})


if __name__ == '__main__':
    unittest.main()
