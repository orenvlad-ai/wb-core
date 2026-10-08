"""Demand coverage recovery, stage ambiguity and unlimited-history regression."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.fbs_demand_history import load_daily_fbs_demand, estimate_fbs_demand, _certified_ranges
from packages.application.wb_fbs_orders import OBSERVATIONS_TABLE, STATUS_CURRENT_TABLE, STATUS_OBSERVATIONS_TABLE


def seed_observer(path, receipts, orders=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(f'''CREATE TABLE observer_runs(result_json TEXT);
            CREATE TABLE {OBSERVATIONS_TABLE}(observation_sequence INTEGER PRIMARY KEY,order_id INTEGER,source_created_at TEXT,warehouse_id INTEGER,nm_id INTEGER,is_zero_order INTEGER);
            CREATE INDEX wb_fbs_observations_by_order ON {OBSERVATIONS_TABLE}(order_id,observation_sequence DESC);
            CREATE TABLE {STATUS_CURRENT_TABLE}(order_id INTEGER PRIMARY KEY,supplier_status TEXT,wb_status TEXT);
            CREATE TABLE {STATUS_OBSERVATIONS_TABLE}(observation_sequence INTEGER PRIMARY KEY,order_id INTEGER,supplier_status TEXT,wb_status TEXT);''')
        conn.executemany('INSERT INTO observer_runs VALUES(?)', [(json.dumps(r),) for r in receipts])
        for sequence, (oid, day, warehouse, nm, supplier, wb, dispatched) in enumerate(orders, 1):
            conn.execute(f'INSERT INTO {OBSERVATIONS_TABLE} VALUES(?,?,?,?,?,0)', (sequence,oid,day+'T12:00:00Z',warehouse,nm))
            conn.execute(f'INSERT OR REPLACE INTO {STATUS_CURRENT_TABLE} VALUES(?,?,?)', (oid,supplier,wb))
            conn.execute(f'INSERT INTO {STATUS_OBSERVATIONS_TABLE} VALUES(?,?,?,?)', (sequence,oid,'complete' if dispatched else supplier,'sorted' if dispatched else wb))


def receipt(start, end, cursor=0, next_cursor=0, complete=True, **extra):
    return dict(window_date_from=int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp()),
        window_date_to=int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp()),
        start_cursor=cursor,next_cursor=next_cursor,listing_complete=complete,schema_drift_count=0,**extra)


class DemandTests(unittest.TestCase):
    def test_cursor_chain_recovery_and_unproven_gap(self):
        first = receipt('2026-04-01', '2026-04-05', next_cursor=12, complete=False)
        recovered = receipt('2026-04-01', '2026-04-05', cursor=12)
        self.assertEqual(_certified_ranges([first]), [])
        self.assertEqual(_certified_ranges([recovered]), [])
        self.assertEqual(len(_certified_ranges([first,recovered])), 1)
        self.assertEqual(_certified_ranges([first,{**recovered,'start_cursor':13}]), [])
        self.assertEqual(len(_certified_ranges([{**first,'schema_drift_count':1},recovered])), 1)

    def test_full_business_day_zero_missing_and_cancel_stages(self):
        with TemporaryDirectory() as raw:
            path = Path(raw)/'observer.sqlite3'
            runs=[{**receipt('2026-04-01','2026-04-04'), 'schema_drift_count': 1}, receipt('2026-04-05','2026-04-07')]
            orders=[(1,'2026-04-02',1,10,'cancel','canceled',False),
                (2,'2026-04-02',1,10,'cancel','canceled',True),
                (3,'2026-04-02',1,20,'new','canceled_by_client',False),
                (4,'2026-04-02',2,10,'complete','canceled_by_client',True),
                (5,'2026-04-02',1,30,'unknown_new_status','waiting',False),
                (6,'2026-04-03',1,30,'cancel','canceled',False)]
            seed_observer(path,runs,orders)
            # A later revision of the same order never doubles demand.
            with sqlite3.connect(path) as conn:
                conn.execute(f'INSERT INTO {OBSERVATIONS_TABLE} VALUES(99,2,\'2026-04-02T12:00:00Z\',1,10,0)')
                conn.execute(f"INSERT INTO {STATUS_OBSERVATIONS_TABLE} VALUES(99,6,'complete','waiting')")
            history=load_daily_fbs_demand(path,report_date=date(2026,4,7),nm_ids=[10,20,30],warehouse_facility_map={1:'moscow',2:'orenburg'})
            self.assertEqual(history.samples('moscow',10)['2026-04-02'],1)
            self.assertEqual(history.samples('orenburg',10)['2026-04-02'],1)
            self.assertIsNone(history.samples('moscow',20)['2026-04-02'])
            self.assertIsNone(history.samples('moscow',30)['2026-04-02'])
            self.assertEqual(history.samples('moscow',20)['2026-04-03'],0)
            self.assertIsNone(history.samples('moscow',30)['2026-04-03'])
            self.assertIsNone(history.samples('moscow',10)['2026-04-04'])
            self.assertNotIn('2026-04-07',history.complete_dates)
            self.assertTrue(history.coverage['fingerprint'])

    def test_unlimited_backfill_frozen_threshold_and_unknown_isolation(self):
        report=date(2026,10,8)
        samples={(report-timedelta(days=i)).isoformat():0.0 for i in range(1,201)}
        for i in [1,3,150,160]:
            samples[(report-timedelta(days=i)).isoformat()]=10.0
        samples[(report-timedelta(days=2)).isoformat()]=None
        samples[(report-timedelta(days=4)).isoformat()]=1
        # Older, much stronger demand must not move the threshold established
        # from the latest requested positive days.
        for i in range(161,201):
            samples[(report-timedelta(days=i)).isoformat()]=1000
        samples[report.isoformat()]=99999
        result=estimate_fbs_demand(samples,report_date=report,sales_avg_period_days=4)
        self.assertEqual(result.daily_demand,10)
        self.assertEqual(len(result.used_dates),4)
        self.assertEqual(result.valid_day_threshold,4.5)
        self.assertIn((report-timedelta(days=160)).isoformat(),result.used_dates)
        self.assertEqual(len(result.incomplete_dates),1)
        partial=estimate_fbs_demand({'2026-10-07':5},report_date=report,sales_avg_period_days=14)
        self.assertEqual(partial.daily_demand,5)
        self.assertTrue(partial.warning)
        unknown=estimate_fbs_demand({'2026-10-07':None,'2026-10-06':0},report_date=report,sales_avg_period_days=14)
        self.assertIsNone(unknown.daily_demand)
        custom=estimate_fbs_demand(samples,report_date=report,sales_avg_period_days=4,date_from=date(2026,10,5),date_to=date(2026,10,7))
        self.assertEqual(len(custom.used_dates),2)


if __name__ == '__main__':
    unittest.main()
