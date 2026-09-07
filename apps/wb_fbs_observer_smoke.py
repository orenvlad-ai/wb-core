"""Observer isolation, bounded resumption and status coverage checks."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.adapters.wb_fbs_orders import WbFbsOrdersPage, WbFbsOrderStatus
from packages.application.wb_fbs_orders import WbFbsOrdersCollector, OBSERVATIONS_TABLE, STATUS_CURRENT_TABLE
from packages.application.wb_fbs_observer import WbFbsObserver, observer_status
from packages.application.wb_fbs_shadow_polling import fbs_shadow_poll_lock
from packages.application.canonical_wb_cost_resolver import CanonicalChannelCostSnapshot, classify_finance_channel


class Source:
    def __init__(self):
        self.ids = [1]
        self.cursors = []
        self.status_ids = []
        self.cursor = 0
        self.missing = set()

    def list_orders(self, **kwargs):
        self.cursors.append(kwargs['next_cursor'])
        return WbFbsOrdersPage([
            {'id':i,'nmId':100,'deliveryType':'fbs','createdAt':'2026-07-01T00:00:00Z','rid':f'rid-{i}'}
            for i in self.ids], self.cursor, kwargs['limit'],kwargs['date_from'],kwargs['date_to'])

    def list_statuses(self, ids):
        self.status_ids.extend(ids)
        return [WbFbsOrderStatus(i,'complete','waiting') for i in ids if i not in self.missing]


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.canonical = self.root/'canonical.sqlite3'
        self.epoch = 1788775200
        self.source = Source()
        WbFbsOrdersCollector(db_path=self.canonical,source=self.source,enabled=True,
            timestamp_factory=self.timestamp).collect(date_from=100,date_to=200)
        self.source.ids = [2]
        self.source.status_ids.clear()
        self.observer = WbFbsObserver(runtime_dir=self.root,canonical_db_path=self.canonical,
            source=self.source,timestamp_factory=self.timestamp,unix_time_factory=lambda:self.epoch,max_pages=1)

    def timestamp(self):
        return datetime.fromtimestamp(self.epoch,timezone.utc).isoformat().replace('+00:00','Z')

    def dump(self):
        with closing(sqlite3.connect(self.canonical)) as conn:
            return '\n'.join(conn.iterdump())

    def channel(self, path):
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory=sqlite3.Row
            return classify_finance_channel(CanonicalChannelCostSnapshot.from_connection(conn),operation={'rid':'rid-2'})

    def test_isolation_and_old_unfinished_statuses(self):
        before=self.dump()
        self.assertEqual(self.channel(self.canonical),'wb_non_fbs')
        with patch('packages.application.wb_fbs_shadow_polling.process_post_t_fbs_lifecycle',side_effect=AssertionError('accounting call')):
            first=self.observer.poll_once()
            self.assertEqual(first['status'],'success')
            self.assertEqual(first['bootstrap_order_count'],1)
            self.assertIn(1,self.source.status_ids)  # Initial unfinished order older than listing window.
            self.assertEqual(self.channel(self.observer.db_path),'fbs_exact_identity')
            self.assertEqual(self.channel(self.canonical),'wb_non_fbs')
            self.epoch+=3600
            repeat=self.observer.poll_once()
            self.assertEqual(repeat['new_order_observation_count'],0)
            self.assertEqual(repeat['new_status_observation_count'],0)
            self.assertEqual(repeat['transition_count'],0)
        self.assertEqual(self.dump(),before)
        with closing(sqlite3.connect(self.observer.db_path)) as conn:
            tables=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            self.assertFalse(any('lifecycle' in t or 'finance' in t or 'queue' in t for t in tables))
        self.assertTrue(observer_status(self.root)['query_only'])

    def test_single_flight_and_bounded_cursor(self):
        calls=len(self.source.cursors)
        with fbs_shadow_poll_lock(self.observer.root/'lease'):
            self.assertEqual(self.observer.poll_once()['status'],'single_flight_skipped')
        self.assertEqual(calls,len(self.source.cursors))
        self.source.cursor=123
        first=self.observer.poll_once()
        self.assertEqual(first['status'],'bounded_partial')
        self.source.cursor=0
        self.epoch+=3600
        second=self.observer.poll_once()
        self.assertEqual(second['start_cursor'],123)
        self.assertEqual(second['status'],'success')

    def test_missing_status_is_partial_and_preserves_old_status(self):
        self.observer.poll_once()
        self.epoch+=3600
        self.source.missing={1}
        result=self.observer.poll_once()
        self.assertEqual(result['status'],'bounded_partial')
        self.assertGreater(result['missing_status_count'],0)
        with closing(sqlite3.connect(self.observer.db_path)) as conn:
            self.assertEqual(conn.execute(f'SELECT wb_status FROM {STATUS_CURRENT_TABLE} WHERE order_id=1').fetchone()[0],'waiting')

    def test_storage_alias_rejected_before_accounting_write(self):
        before=self.dump()
        self.observer.root.mkdir()
        self.observer.db_path.hardlink_to(self.canonical)
        with self.assertRaisesRegex(RuntimeError,'separate'):
            self.observer.poll_once()
        self.assertEqual(self.dump(),before)


if __name__=='__main__':
    unittest.main()
