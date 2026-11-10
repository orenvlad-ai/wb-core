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
        self.runtime = fixture(Path(self.temp.name), facilities=0)
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


if __name__ == '__main__':
    unittest.main()
