#!/usr/bin/env python3
"""Known archived valuation gaps reuse only proven native unavailability."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.shared_sku_cost_sources_smoke import fixture, add_version, DAY, P
from apps.ready_publication_smoke import seed, make_book
from apps.warehouse_functional_runner import _publish_fbs_snapshot_accounting
from packages.application import fbs_accounting_runtime as accounting
from packages.application import fbs_accounting_historical_sources as sources
from packages.application.fbs_accounting_historical_stages import HistoricalArchivedWbValuationUnavailable
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.shared_sku_cost_sources import capture_wb_component


class SourceClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'source.sqlite3'
        self.conn = fixture(self.db); self.addCleanup(self.conn.close)
        self.conn.executescript(f'''
            CREATE TABLE {P}warehouse_functional_read_models(version_id,payload_json);
            CREATE TABLE {P}warehouse_business_projection_current_rows(as_of_date,metrics_json,presentation_json,provenance_json);
        ''')
        self.book = {'state': {'periods': {'2026-09-06': {}}}}

    def capture(self):
        self.conn.commit()
        before = self.db.read_bytes()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA query_only=ON'); self.conn.execute('BEGIN')
        try:
            with patch.object(sources, 'archived_quantity_snapshot', return_value={
                    'rows': [{'nm_id': 1}, {'nm_id': 2}]}):
                return sources.capture_missing_dated_inputs(self.conn, db_path=self.db,
                    book=self.book, capture={'business_date': '2026-09-08', 'captured_at': '2026-09-08T14:00:00Z'})
        finally:
            self.conn.rollback()
            self.assertEqual(self.db.read_bytes(), before)

    def test_native_authoritative_cost_gap_is_typed_with_exact_source(self):
        self.conn.execute(f"UPDATE {P}warehouse_functional_balances SET cost_covered_quantity='0' WHERE warehouse_key='wb'")
        with self.assertRaises(HistoricalArchivedWbValuationUnavailable) as caught:
            self.capture()
        error = caught.exception
        self.assertEqual(error.book_version, fingerprint(self.book))
        self.assertEqual(error.blocker['business_date'], DAY)
        self.assertEqual(error.blocker['source']['version_id'], 'v1')
        self.assertEqual(error.blocker['missing_by_nm_id'], {'1': {'quantity': 12,
            'components': {'physical': 7, 'to_customer': 3, 'from_customer': 2},
            'reason': 'wb_cost_coverage_incomplete'}})
        self.assertTrue(error.blocker['source_digest'].startswith('sha256:'))

    def test_authority_corruption_is_not_typed_unavailability(self):
        self.conn.execute(f'UPDATE {P}warehouse_wb_snapshots SET pagination_complete=0')
        with self.assertRaises(ValueError) as caught: self.capture()
        self.assertNotIsInstance(caught.exception, HistoricalArchivedWbValuationUnavailable)

    def test_other_valuation_invariant_is_not_typed_unavailability(self):
        self.conn.execute(f"UPDATE {P}warehouse_functional_balances SET capital_rub='1201' WHERE warehouse_key='wb'")
        with self.assertRaises(ValueError) as caught: self.capture()
        self.assertNotIsInstance(caught.exception, HistoricalArchivedWbValuationUnavailable)

    def test_mixed_missing_reason_and_unpinned_source_fail_closed(self):
        wb = {'complete': False, 'authority_complete': True, 'reason': 'wb_sku_components_incomplete',
            'business_date': DAY, 'version_id': 'v1', 'source_digest': 'sha256:' + 'a' * 64,
            'source': {'version_id': 'v1', 'snapshot_date': DAY},
            'rows': [{'nm_id': 1, 'quantity': 12, 'components': {}, 'status': 'missing',
                      'reason': 'wb_cost_coverage_incomplete'},
                     {'nm_id': 2, 'status': 'missing', 'reason': 'sku_outside_exact_wb_snapshot'}]}
        for change in ({}, {'rows': wb['rows'][:1], 'source_digest': ''},
                       {'rows': wb['rows'][:1], 'source': {'version_id': 'foreign', 'snapshot_date': DAY}}):
            with self.subTest(change=change), self.assertRaises(ValueError) as caught:
                sources._require_archived_wb_valuation({**wb, **change}, day=DAY, book=self.book)
            self.assertNotIsInstance(caught.exception, HistoricalArchivedWbValuationUnavailable)

    def test_oversized_gap_cannot_authorize_truncated_diagnostic(self):
        wb = {'complete': False, 'authority_complete': True, 'reason': 'wb_sku_components_incomplete',
            'business_date': DAY, 'version_id': 'v1', 'source_digest': 'sha256:' + 'a' * 64,
            'source': {'version_id': 'v1', 'snapshot_date': DAY},
            'rows': [{'nm_id': n, 'quantity': 1, 'components': {'physical': 1, 'to_customer': 0,
                'from_customer': 0}, 'status': 'missing', 'reason': 'wb_cost_coverage_incomplete'}
                for n in range(1, 1001)]}
        with self.assertRaises(ValueError) as caught:
            sources._require_archived_wb_valuation(wb, day=DAY, book=self.book)
        self.assertNotIsInstance(caught.exception, HistoricalArchivedWbValuationUnavailable)


class ExistingInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.runtime = seed(self.root)
        book, expected = make_book(self.root, opening=True)
        self.day = max(book['state']['periods'])
        book['state']['periods'][self.day]['quality'] = 'incomplete'
        version = accounting.save(self.root, book, expected=expected, operation_id='opening')
        accounting._record_prepare_failure(self.root, expected=version,
            error=ValueError('incomplete_period_cannot_close'))
        self.book, self.version = accounting.load(self.root)
        # Use the native archived source reader and the actual saved book as
        # the classifier operands; never assign its captured fingerprint.
        source_db = self.root / 'archived-source.sqlite3'
        source = fixture(source_db)
        add_version(source, 'historical-v2', '2026-09-09')
        source.execute(f"UPDATE {P}warehouse_functional_balances SET cost_covered_quantity='0' WHERE warehouse_key='wb'")
        source.commit(); source.close()
        wb = capture_wb_component(source_db, day='2026-09-09', nm_ids=[1, 2])
        with self.assertRaises(HistoricalArchivedWbValuationUnavailable) as caught:
            sources._require_archived_wb_valuation(wb, day='2026-09-09', book=self.book)
        self.error = caught.exception; self.blocker = self.error.blocker
        self.assertEqual(self.error.book_version, self.version)

    def test_verified_existing_invalidation_exposes_exact_blocker_without_writes(self):
        before = accounting.path(self.root).read_bytes()
        with patch('packages.application.fbs_accounting_historical_cycle.refresh', side_effect=self.error), \
             patch.object(accounting, 'refresh', side_effect=AssertionError('must not advance ordinary book')):
            with self.assertRaises(accounting.FbsAccountingUnavailable) as caught:
                _publish_fbs_snapshot_accounting(self.runtime)
        diagnostic = caught.exception.diagnostic()
        self.assertEqual(diagnostic, {'status': 'unavailable', 'reason': 'incomplete_period_cannot_close',
            'version': self.version, 'blocked_date': self.day, 'ready_obligation': 'not_performed',
            'historical_valuation_blocker': self.blocker})
        self.assertIs(caught.exception.__cause__, self.error)
        self.assertEqual(accounting.path(self.root).read_bytes(), before)
        self.assertEqual(accounting.load(self.root), (self.book, self.version))
        diagnostic['historical_valuation_blocker']['missing_by_nm_id'].clear()
        self.assertEqual(caught.exception.diagnostic()['historical_valuation_blocker'], self.blocker)

    def test_missing_invalidation_book_drift_and_non_open_period_remain_fatal(self):
        changes = [({'publication_error': {}}, self.version), ({}, 'foreign-version'),
                   ({'active': False}, self.version)]
        closed = deepcopy(self.book); closed['state']['periods'][self.day]['status'] = 'closed'
        complete = deepcopy(self.book); complete['state']['periods'][self.day]['quality'] = 'complete'
        cases = [({**self.book, **change}, version) for change, version in changes]
        cases += [(closed, self.version), (complete, self.version)]
        for book, version in cases:
            with self.subTest(book=book.get('publication_error'), version=version), \
                 patch('packages.application.fbs_accounting_historical_cycle.refresh', side_effect=self.error), \
                 patch.object(accounting, 'load', return_value=(book, version)), \
                 self.assertRaises(HistoricalArchivedWbValuationUnavailable) as caught:
                _publish_fbs_snapshot_accounting(self.runtime)
            self.assertIs(caught.exception, self.error)

    def test_open_day_after_historical_gap_remains_fatal(self):
        error = HistoricalArchivedWbValuationUnavailable(book_version=self.version,
            blocker={**self.blocker, 'business_date': '2026-09-07'})
        with patch('packages.application.fbs_accounting_historical_cycle.refresh', side_effect=error), \
             self.assertRaises(HistoricalArchivedWbValuationUnavailable):
            _publish_fbs_snapshot_accounting(self.runtime)

    def test_unexpected_valueerror_is_not_downgraded(self):
        for error in (ValueError('historical_archived_wb_incomplete:2026-09-09'),
                      ValueError('historical_archive_source_corrupt'), TypeError('programming-error')):
            with self.subTest(error=str(error)), \
                 patch('packages.application.fbs_accounting_historical_cycle.refresh', side_effect=error), \
                 self.assertRaises(type(error)) as caught:
                _publish_fbs_snapshot_accounting(self.runtime)
            self.assertIs(caught.exception, error)


if __name__ == '__main__':
    unittest.main()
