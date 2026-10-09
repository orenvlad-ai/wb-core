#!/usr/bin/env python3
"""Offline evidence for planning stock admission, dated inbound and cached monitor."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_official_fbs_smoke import fixture
from packages.application.official_fbs_stock_read import current_official_fbs_facilities
from packages.application.wb_fbs_warehouse_registry import FACILITIES_TABLE, WAREHOUSE_MAPPINGS_TABLE
from packages.application.ff_pool_foundation import FACILITY_PROFILES_TABLE
from packages.application.stock_monitor import (
    StockMonitorService, forecast_cells, first_deficit_date, read_expected_shipments,
    read_national_samples, STOCK_MAX_AGE_SECONDS,
)
from packages.application.fbs_demand_history import FbsDemandHistory

NOW = datetime(2026, 9, 5, 10, 10, tzinfo=timezone.utc)
TODAY = date(2026, 9, 5)


def event(day, quantity=10, target='A'):
    return {'nm_id': 1, 'arrival_date': day, 'quantity': quantity, 'target_facility_id': target,
        'shipment_id': 's', 'label': 's', 'status': 'in_transit'}


class ForecastTests(unittest.TestCase):
    def test_arrival_start_of_zero_day_rescues_it(self):
        events = [event('2026-09-06', 20)]
        cells = forecast_cells(today=TODAY, horizon_days=5, quantity=10, daily_demand=10, inbounds=events)
        self.assertEqual([c['quantity'] for c in cells], [10, 20, 10, 0, 0])
        self.assertEqual(first_deficit_date(today=TODAY, quantity=10, daily_demand=10, inbounds=events), '2026-09-08')

    def test_lost_demand_is_not_carried_as_debt(self):
        events = [event('2026-09-08', 20)]
        cells = forecast_cells(today=TODAY, horizon_days=5, quantity=5, daily_demand=10, inbounds=events)
        self.assertEqual([c['quantity'] for c in cells], [5, 0, 0, 20, 10])
        self.assertEqual(first_deficit_date(today=TODAY, quantity=5, daily_demand=10, inbounds=events), '2026-09-06')

    def test_deficit_beyond_visible_and_no_arbitrary_horizon_cap(self):
        self.assertEqual(first_deficit_date(today=TODAY, quantity=100000, daily_demand=1, inbounds=[]),
            (TODAY + timedelta(days=100000)).isoformat())
        self.assertEqual(len(forecast_cells(today=TODAY, horizon_days=60, quantity=100000,
            daily_demand=1, inbounds=[])), 60)

    def test_unknown_future_never_becomes_zero(self):
        cells = forecast_cells(today=TODAY, horizon_days=2, quantity=100, daily_demand=None, inbounds=[])
        self.assertEqual([cell['quantity'] for cell in cells], [100, None])
        self.assertEqual(cells[1]['color'], 'unknown')

    def test_bounded_window_matches_full_calendar_without_lost_debt(self):
        events = [event('2026-09-05', 2), event('2026-09-08', 20), event('2026-10-07', 400)]
        full = forecast_cells(today=TODAY, horizon_days=90, quantity=5, daily_demand=10, inbounds=events)
        for offset in (1, 3, 31, 32, 50):
            window = forecast_cells(today=TODAY, horizon_days=20, quantity=5, daily_demand=10, inbounds=events, date_offset=offset)
            self.assertEqual(window, full[offset:offset+20])

    def test_arrival_today_and_color_cover(self):
        cells = forecast_cells(today=TODAY, horizon_days=1, quantity=0, daily_demand=1,
            inbounds=[event('2026-09-05', 31)])
        self.assertEqual(cells[0]['quantity'], 31)
        self.assertEqual(cells[0]['color'], 'green')
        self.assertEqual(cells[0]['bar_ratio'], 1)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'stock.sqlite3'
        self.conn = fixture(self.path); self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        for column in ['code', 'name']:
            self.conn.execute(f'ALTER TABLE {FACILITIES_TABLE} ADD COLUMN {column} TEXT')
        self.conn.execute(f"UPDATE {FACILITIES_TABLE} SET name='FF Москва',code=facility_id")
        self.conn.execute(f'CREATE TABLE {FACILITY_PROFILES_TABLE}(facility_id,city)')
        self.conn.commit()

    def read(self, at, planning=False):
        kwargs = {'planning_max_age_seconds': STOCK_MAX_AGE_SECONDS} if planning else {}
        return current_official_fbs_facilities(self.path, requested_nm_ids=[1, 2], now=at, **kwargs)

    def test_cross_midnight_planning_72h_and_strict_unchanged(self):
        at = NOW + timedelta(days=2)
        before = self.path.read_bytes()
        result = self.read(at, True)
        self.assertEqual(result['facilities'][0]['available'], 3)
        self.assertEqual(result['snapshot_date'], '2026-09-05')
        self.assertTrue(result['warning'])
        self.assertIsNone(self.read(at)['facilities'][0]['available'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(self.read(NOW + timedelta(days=3, minutes=1), True)['facilities'][0]['available'])

    def test_partial_attempt_does_not_replace_last_complete_and_mapping_protected(self):
        self.conn.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_warehouse_registry_runs SELECT 'partial',2,'partial',0,policy_version,catalog_scope_json,warehouse_scope_json,generation_digest,started_at,completed_at FROM sheet_vitrina_v1_wb_fbs_warehouse_registry_runs WHERE run_id='g1'")
        self.conn.commit()
        self.assertEqual(self.read(NOW + timedelta(days=1), True)['facilities'][0]['available'], 3)
        self.conn.execute(f"UPDATE {WAREHOUSE_MAPPINGS_TABLE} SET active=0 WHERE facility_id='A'"); self.conn.commit()
        self.assertIsNone(self.read(NOW + timedelta(days=1), True)['facilities'][0]['available'])


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:'); self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE sheet_vitrina_v1_supplier_shipments(shipment_id,order_status,shipment_date,
                actual_shipment_date,actual_ff_acceptance_date,target_facility_id,invoice_no,archived_at,
                historical_status_exception);
            CREATE TABLE sheet_vitrina_v1_supplier_shipment_lines(line_id,shipment_id,line_type,
                sort_order,match_status,internal_nm_id,qty);
            CREATE TABLE sheet_vitrina_v1_ff_stock_operations(operation_id,source_type,source_object_id);
            CREATE TABLE sheet_vitrina_v1_ff_stock_operation_lines(operation_id,nm_id,quantity_delta);
            CREATE TABLE temporal_source_snapshots(source_key,snapshot_date,captured_at,payload_json);
        ''')

    def shipment(self, sid, planned, actual='', acceptance='', target='', qty=100):
        self.conn.execute('INSERT INTO sheet_vitrina_v1_supplier_shipments VALUES(?,?,?,?,?,?,?,?,?)',
            (sid, 'production', planned, actual, acceptance, target, sid, '', ''))
        self.conn.execute('INSERT INTO sheet_vitrina_v1_supplier_shipment_lines VALUES(?,?,?,?,?,?,?)',
            (sid+'line', sid, 'product', 1, 'matched', 1, qty))

    def test_planned_and_actual_anchor_default_moscow_partial_and_received(self):
        self.shipment('planned', '2026-09-10')
        self.shipment('departed', '2026-08-01', actual='2026-09-02', target='B')
        self.shipment('received', '2026-08-01', actual='2026-08-02', acceptance='2026-09-01')
        self.shipment('past', '2026-07-01')
        self.shipment('overdue', '2026-09-01')
        self.conn.execute("INSERT INTO sheet_vitrina_v1_ff_stock_operations VALUES('receipt','supplier_shipment_acceptance','planned')")
        self.conn.execute("INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines VALUES('receipt',1,30)")
        events, warnings = read_expected_shipments(self.conn, today=TODAY, moscow_facility_id='A', delivery_days=10)
        by_id = {e['shipment_id']: e for e in events}
        self.assertEqual(set(by_id), {'planned', 'departed', 'overdue'})
        self.assertEqual(by_id['planned']['quantity'], 70)
        self.assertEqual(by_id['planned']['target_facility_id'], 'A')
        self.assertEqual(by_id['planned']['arrival_date'], '2026-09-20')
        self.assertEqual(by_id['departed']['arrival_date'], '2026-09-12')
        self.assertEqual(by_id['departed']['target_facility_id'], 'B')
        self.assertTrue(any('просрочена' in w for w in warnings))
        self.assertTrue(any('прошла' in w for w in warnings))

    def test_receipt_recovery_reopens_full_inbound_quantity(self):
        self.shipment('restored', '2026-09-10')
        self.conn.executemany('INSERT INTO sheet_vitrina_v1_ff_stock_operations VALUES(?,?,?)',
            [('receipt','supplier_shipment_acceptance','restored'), ('recovery','supplier_shipment_acceptance_recovery','restored')])
        self.conn.executemany('INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines VALUES(?,?,?)',
            [('receipt',1,100), ('recovery',1,-100)])
        events, _ = read_expected_shipments(self.conn, today=TODAY, moscow_facility_id='A')
        self.assertEqual(events[0]['quantity'], 100)

    def test_national_missing_not_zero_duplicate_and_open_capture_excluded(self):
        def save(day, items, captured='2026-09-05T10:00:00Z'):
            self.conn.execute('INSERT INTO temporal_source_snapshots VALUES(?,?,?,?)',
                ('sales_funnel_history', day, captured, json.dumps({'kind':'success',
                    'date_from':day,'date_to':day,'items':items})))
        def item(day,nm,value): return {'date':day,'nm_id':nm,'metric':'orderCount','value':value}
        save('2026-09-01', [item('2026-09-01',1,10)])
        save('2026-09-02', [item('2026-09-02',1,10),item('2026-09-02',1,20)])
        save('2026-09-03', [item('2026-09-03',1,10)], captured='2026-09-03T10:00:00Z')
        samples = read_national_samples(self.conn, nm_ids=[1,2], report_date=TODAY)
        self.assertEqual(samples[1], {'2026-09-01':10, '2026-09-02':None})
        self.assertEqual(samples[2], {})


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.now = NOW
        self.service = StockMonitorService(runtime=SimpleNamespace(runtime_dir=root, db_path=root/'never-open.sqlite'),
            now_factory=lambda: self.now, ads_loader=lambda: {'index': {}})
        source = {'captured_at': NOW.isoformat(), 'date': '2026-09-05'}
        child = {'facility_id':'A','name':'FBS Москва','current_stock':100,'avg_daily_sales':10,
            'warnings':[],'stock_source':source}
        self.base = {'schema_version':1,'contract_name':'stock_monitor','contract_version':1,
            'status':'ready','period_days':14,'generated_at':NOW.isoformat(),'report_date':'2026-09-05',
            'source_fingerprint':'abc','warnings':[],'inbounds':[],
            'rows':[{'nm_id':1,'name':'SKU','category':'cat','current_stock':100,'avg_daily_sales':10,
                'warnings':[],'warehouses':[child]}]}

    def test_get_does_not_scan_or_build_and_rechecks_stock_age(self):
        with patch.object(self.service, '_build_base', return_value=self.base):
            self.service.refresh_snapshot()
        with (patch.object(self.service, '_build_base', side_effect=AssertionError('must not rebuild')),
              patch.object(self.service, '_load_market_ads', side_effect=AssertionError('GET must not read bids')),
              patch('packages.application.stock_monitor_market.build_market', side_effect=AssertionError('GET must not read references')),
              patch('packages.application.stock_monitor._read', side_effect=AssertionError('must not open sources'))):
            result = self.service.get_snapshot()
            self.assertEqual(result['rows'][0]['cells'][0]['quantity'], 100)
            self.assertTrue(result['cache']['hit'])
            self.now += timedelta(hours=73)
            result = self.service.get_snapshot()
            self.assertIsNone(result['rows'][0]['current_stock'])
            self.assertIsNone(result['rows'][0]['cells'][0]['quantity'])
        self.assertFalse(self.service.get_snapshot(period_days=30)['cache']['hit'])

    def test_legacy_saved_snapshot_without_market_remains_readable(self):
        path = self.service._path(14)
        path.parent.mkdir()
        path.write_text(json.dumps(self.base))
        with patch('packages.application.stock_monitor_market.build_market', side_effect=AssertionError('GET reference enrichment')):
            result = self.service.get_snapshot()
        self.assertEqual(result['rows'][0]['current_stock'],100)
        self.assertNotIn('market',result['rows'][0])

    def test_reference_failure_does_not_prevent_new_stock_publication(self):
        from unittest.mock import Mock
        self.service._ads_loader = Mock(return_value={'index':{1:[{'status':9,'payment_type':'cpm',
            'advert_id':1,'placement':'search','current_bid_rub':250,'campaign_fetched_at':NOW.isoformat()}]}})
        with (patch.object(self.service,'_build_base',return_value=self.base),
              patch('packages.application.stock_monitor_market._temporal',return_value=({1:{'price_seller_discounted':500,'promo_participation':0}},NOW.isoformat(),False))):
            self.service.refresh_snapshot()
        self.base['rows'][0]['warehouses'][0]['current_stock'] = 20
        self.base['rows'][0]['current_stock'] = 20
        self.service._ads_loader.side_effect = RuntimeError('unavailable')
        self.service._market_ads_read_at = None
        with (patch.object(self.service,'_build_base',return_value=self.base),
              patch('packages.application.stock_monitor_market._temporal',side_effect=OSError('unavailable'))):
            self.service.refresh_snapshot()
        result = self.service.get_snapshot()['rows'][0]
        self.assertEqual(result['current_stock'],20)
        self.assertEqual(result['market']['seller_price']['value'],500)
        self.assertEqual(result['market']['seller_price']['captured_at'],NOW.isoformat())
        self.assertEqual(result['market']['seller_price']['status'],'stale')
        self.assertEqual(result['market']['cpm_bid']['value'],250)
        self.assertEqual(result['market']['cpm_bid']['status'],'stale')

    def test_large_horizon_transports_at_most_ninety_cells_and_validates_period(self):
        with patch.object(self.service, '_build_base', return_value=self.base):
            self.service.refresh_snapshot()
        result = self.service.get_snapshot(horizon_days=3650, date_offset=3000)
        self.assertEqual(result['horizon_days'], 3650)
        self.assertEqual(result['date_offset'], 3000)
        self.assertEqual(result['visible_days'], 90)
        self.assertEqual(len(result['dates']), 90)
        self.assertEqual(len(result['rows'][0]['cells']), 90)
        self.assertEqual(result['rows'][0]['deficit_days'], 10)
        with self.assertRaises(ValueError):
            self.service.get_snapshot(period_days=0)

    def test_failed_refresh_keeps_previous_snapshot(self):
        with patch.object(self.service, '_build_base', return_value=self.base):
            self.service.refresh_snapshot()
        before = self.service._path(14).read_bytes()
        with patch.object(self.service, '_build_base', side_effect=ValueError('source_failure')):
            with self.assertRaisesRegex(ValueError,'source_failure'):
                self.service.refresh_snapshot()
        self.assertEqual(before, self.service._path(14).read_bytes())
        result = self.service.get_snapshot()
        self.assertEqual(result['refresh']['status'], 'failed')
        self.assertTrue(any('предыдущий снимок' in warning for warning in result['warnings']))


class IntegrationTests(unittest.TestCase):
    def test_all_sku_snapshot_separates_national_and_fbs_and_keeps_unknown_local(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / 'stock.sqlite3'
            conn = fixture(path)
            for column in ['code', 'name']:
                conn.execute(f'ALTER TABLE {FACILITIES_TABLE} ADD COLUMN {column} TEXT')
            conn.execute(f"UPDATE {FACILITIES_TABLE} SET name=CASE facility_id WHEN 'A' THEN 'FF Москва' ELSE 'FF Оренбург' END,code=facility_id")
            conn.execute(f'CREATE TABLE {FACILITY_PROFILES_TABLE}(facility_id,city)')
            conn.execute(f'ALTER TABLE {WAREHOUSE_MAPPINGS_TABLE} ADD COLUMN seller_warehouse_id INTEGER')
            conn.execute(f"UPDATE {WAREHOUSE_MAPPINGS_TABLE} SET seller_warehouse_id=CASE facility_id WHEN 'A' THEN 1 ELSE 2 END")
            conn.executescript("""
                CREATE TABLE sheet_vitrina_v1_nomenclature_items(item_id,nm_id,is_active,is_hidden,updated_at,our_sku,nomenclature_name,product_type);
                INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES('one',1,1,0,'','','(Anti-Spy) iPhone 14','anti_spy');
                INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES('two',2,1,0,'','','(Matte) iPhone 15','matte');
                CREATE TABLE sheet_vitrina_v1_sku_groups(group_key,label);
                INSERT INTO sheet_vitrina_v1_sku_groups VALUES('anti_spy','Anti-spy'),('matte','Matte');
                CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(slot,version_id);
                INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,'v1');
                CREATE TABLE temporal_source_snapshots(source_key,snapshot_date,captured_at,payload_json);
            """)
            conn.execute('INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('wb','v1',NOW.isoformat(),TODAY.isoformat(),'[1,2]',1,1,'[0]',2,'wb-digest','[]',
                json.dumps([{'nm_id':1,'quantity':20},{'nm_id':2,'quantity':0}]),NOW.isoformat()))
            for day in ('2026-09-03','2026-09-04'):
                conn.execute('INSERT INTO temporal_source_snapshots VALUES(?,?,?,?)',
                    ('sales_funnel_history',day,NOW.isoformat(),json.dumps({'kind':'success','date_from':day,'date_to':day,
                    'items':[{'date':day,'nm_id':1,'metric':'orderCount','value':10},
                             {'date':day,'nm_id':2,'metric':'orderCount','value':0}]})))
            conn.commit(); conn.close()
            before = path.read_bytes()
            history = FbsDemandHistory({'A':{1:{'2026-09-03':100,'2026-09-04':100},2:{}},'B':{1:{},2:{}}},
                ('2026-09-03','2026-09-04'),(),{'status':'available','fingerprint':'history'})
            service = StockMonitorService(runtime=SimpleNamespace(runtime_dir=root,db_path=path),now_factory=lambda:NOW,
                ads_loader=lambda: {'index': {}})
            with patch('packages.application.stock_monitor.load_daily_fbs_demand',return_value=history):
                refreshed = service.refresh_snapshot(period_days=2)
            self.assertEqual(refreshed['sku_count'],2)
            result = service.get_snapshot(period_days=2)
            rows = {r['nm_id']:r for r in result['rows']}
            self.assertEqual(rows[1]['name'],'iPhone 14')
            self.assertEqual(rows[1]['category'],'Anti-spy')
            self.assertEqual(rows[1]['current_stock'],28)
            self.assertEqual(rows[1]['avg_daily_sales'],10)  # never sum with FBS 100/day
            self.assertEqual(rows[1]['forecast_state'],'ready')
            self.assertEqual(rows[1]['warehouses'][0]['avg_daily_sales'],100)
            self.assertIsNone(rows[1]['warehouses'][-1]['avg_daily_sales'])
            self.assertEqual(rows[2]['current_stock'],0)
            self.assertEqual(rows[2]['forecast_state'],'unavailable')
            self.assertIsNone(rows[2]['cells'][1]['quantity'])
            self.assertEqual(before,path.read_bytes())


if __name__ == '__main__':
    unittest.main()
