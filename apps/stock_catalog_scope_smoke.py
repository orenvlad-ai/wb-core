#!/usr/bin/env python3
"""Systemic catalog coverage, identity failures and snapshot consistency."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.adapters.wb_content import WbContentCard, WbContentCatalogSnapshot
from packages.application.stock_catalog_scope import (
    StockCatalogScopeError, read_stock_catalog_scope, require_stock_catalog_scope,
)
from packages.application.stocks_block import StocksBlock
from packages.application.warehouse_stocks import WarehouseStocksBlock, WarehouseOpeningSnapshotError
from packages.application.wb_fbs_warehouse_registry import WbFbsWarehouseRegistry


NOW = "2026-09-07T15:00:00Z"


class StockCatalogScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "operational.sqlite3"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("""CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                item_id TEXT PRIMARY KEY,nm_id INTEGER,is_active INTEGER,is_hidden INTEGER,
                updated_at TEXT,nomenclature_name TEXT)""")
            conn.executemany("INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,?,?,?,?)", [
                ("main", 101, 1, 0, NOW, "Main"),
                ("hidden", 202, 1, 1, NOW, "Hidden"),
                ("inactive-hidden", 303, 0, 1, NOW, "Inactive hidden"),
                ("staged", 404, 0, 0, NOW, "Not activated"),
            ])

    def block(self, source=None):
        # Test the real request boundary without constructing unrelated ledgers.
        block = WarehouseStocksBlock.__new__(WarehouseStocksBlock)
        block.runtime = SimpleNamespace(db_path=self.db)
        block.wb_nomenclature_provider = None
        block.now_factory = lambda: datetime(2026, 9, 7, 15, tzinfo=timezone.utc)
        block.stocks_block = StocksBlock(source or Source())
        return block

    def catalog(self, ids):
        return WbContentCatalogSnapshot.from_cards([
            WbContentCard(nm_id=nm, vendor_code=str(nm), title=str(nm), subject_name="test",
                updated_at=NOW, barcodes=[str(nm)], chrt_ids=[nm * 10, nm * 10 + 1])
            for nm in ids
        ], pages_fetched=1, terminal_short_page=True, cursor_chain_digest="sha256:test")

    def test_all_main_and_hidden_read_without_accounting_or_bundle(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        conn = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            scope = read_stock_catalog_scope(conn)
        finally:
            conn.close()
        self.assertEqual(scope["nm_ids"], [101, 202, 303])
        self.assertEqual((scope["main_count"], scope["retained_hidden_count"]), (1, 2))
        self.assertTrue(scope["complete"])
        block = self.block()
        rows = block._opening_nomenclature_request()
        payload = block._fetch_wb_stock_snapshot(rows)
        self.assertEqual(payload["requested_nm_ids"], [101, 202, 303])
        self.assertEqual(payload["query_catalog_scope"]["scope_digest"], scope["scope_digest"])
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_new_sku_reaches_both_requests_without_configuration_edit(self):
        block = self.block()
        prior = block._opening_nomenclature_request()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,?,?,?,?)",
                ("new", 505, 1, 0, NOW, "No photo, sales or stock required"))
        self.assertEqual([r["nm_id"] for r in block._opening_nomenclature_request()], [101, 202, 303, 505])
        registry = WbFbsWarehouseRegistry(db_path=self.db)
        mapping, scope = registry._exact_catalog_scope(self.catalog([101, 202, 303, 505]))
        self.assertTrue(scope["complete"])
        self.assertEqual(set(mapping.values()), {101, 202, 303, 505})
        self.assertEqual(len(mapping), 8)
        with self.assertRaisesRegex(WarehouseOpeningSnapshotError, "changed before"):
            block._fetch_wb_stock_snapshot(prior)

    def test_missing_hidden_card_is_incomplete_not_zero(self):
        mapping, scope = WbFbsWarehouseRegistry(db_path=self.db)._exact_catalog_scope(self.catalog([101, 202]))
        self.assertFalse(scope["complete"])
        self.assertEqual(scope["missing_active_nm_ids"], [303])
        self.assertNotIn(303, mapping.values())

    def test_visibility_change_is_bound_to_scope_digest(self):
        registry = WbFbsWarehouseRegistry(db_path=self.db)
        catalog = self.catalog([101, 202, 303])
        first = registry._exact_catalog_scope(catalog)[1]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET is_hidden=1 WHERE nm_id=101")
        second = registry._exact_catalog_scope(catalog)[1]
        self.assertTrue(second["complete"])
        self.assertNotEqual(first["scope_digest"], second["scope_digest"])
        self.assertEqual(first["active_nm_id_count"], second["active_nm_id_count"])

    def test_identity_failure_blocks_instead_of_using_old_bundle(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET nm_id=NULL WHERE item_id='main'")
        with self.assertRaisesRegex(StockCatalogScopeError, "invalid_items=.*main"):
            self.block()._opening_nomenclature_request()
        _, scope = WbFbsWarehouseRegistry(db_path=self.db)._exact_catalog_scope(self.catalog([202, 303]))
        self.assertFalse(scope["complete"])
        self.assertEqual(scope["invalid_identity_item_ids"], ["main"])

    def test_duplicate_identity_not_silently_deduplicated(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET nm_id=101 WHERE item_id='hidden'")
        with self.assertRaisesRegex(StockCatalogScopeError, "duplicate_nm_ids=.*101"):
            require_stock_catalog_scope(self.db)

    def test_missing_catalog_not_reported_as_empty(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE sheet_vitrina_v1_nomenclature_items")
        with self.assertRaisesRegex(StockCatalogScopeError, "unavailable"):
            self.block()._opening_nomenclature_request()

    def test_catalog_change_during_fetch_rejects_snapshot(self):
        def change():
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET updated_at='2026-09-07T15:00:01Z' WHERE nm_id=101")
        block = self.block(Source(change))
        with self.assertRaisesRegex(WarehouseOpeningSnapshotError, "changed during"):
            block._fetch_wb_stock_snapshot(block._opening_nomenclature_request())


class Source:
    def __init__(self, callback=lambda: None):
        self.callback = callback

    def fetch(self, request):
        self.callback()
        return {"snapshot_date": request.snapshot_date, "requested_nm_ids": list(request.nm_ids),
            "data": {"fetched_at": NOW, "pagination_complete": True, "missing_nm_ids_are_zero": True,
                "rows": []}}


if __name__ == "__main__":
    unittest.main()
