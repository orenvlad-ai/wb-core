"""Saved reference observations: truth, auth resets, ambiguity and source failures."""
from pathlib import Path
import json
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.stock_monitor_market import build_market
from packages.application.stock_monitor import StockMonitorService

DAY = '2026-10-09'
STAMP = '2026-10-09T10:00:00Z'


class MarketTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.runtime = SimpleNamespace(db_path=Path(temp.name)/'fixture.sqlite3')
        self.conn = sqlite3.connect(self.runtime.db_path); self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE temporal_source_snapshots(source_key,snapshot_date,captured_at,payload_json, PRIMARY KEY(source_key,snapshot_date));
            CREATE TABLE temporal_source_slot_snapshots(source_key,snapshot_date,snapshot_role,captured_at,payload_json, PRIMARY KEY(source_key,snapshot_date,snapshot_role));
            CREATE TABLE temporal_source_closure_state(source_key,target_date,slot_kind,state,last_reason,last_attempt_at, PRIMARY KEY(source_key,target_date,slot_kind));
            CREATE TABLE wb_buyer_authenticated_runs(run_id,business_date,started_at,status);
            CREATE TABLE wb_buyer_authenticated_observations(run_id,nm_id,business_date,measured_at,status,payload_json);
        ''')
        for source, field, value in [('prices_snapshot','price_seller_discounted',500),('promo_by_price','promo_participation',0)]:
            self.save(source, DAY, STAMP, {'kind':'success','items':[{'nm_id':1,field:value}]})
        self.ads = Mock(return_value={'captured_at':STAMP, 'index':{1:[self.bid('cpm',100,250), self.bid('cpc',200,3.75)]}})

    def bid(self, payment, advert, value, placement='search', status=9):
        return dict(payment_type=payment,advert_id=advert,current_bid_rub=value,placement=placement,status=status,campaign_fetched_at=STAMP)

    def save(self, source, day, stamp, payload):
        if source == 'prices_snapshot':
            self.conn.execute('INSERT OR REPLACE INTO temporal_source_slot_snapshots VALUES(?,?,?,?,?)',(source,day,'accepted_current_snapshot',stamp,json.dumps(payload)))
        else:
            self.conn.execute('INSERT OR REPLACE INTO temporal_source_snapshots VALUES(?,?,?,?)',(source,day,stamp,json.dumps(payload)))
        self.conn.commit()

    def observe(self, run, context, *, stamp=STAMP, status='observed', wallet=400):
        self.conn.execute('INSERT INTO wb_buyer_authenticated_runs VALUES(?,?,?,?)',(run,DAY,stamp,'completed'))
        self.conn.execute('INSERT INTO wb_buyer_authenticated_observations VALUES(?,?,?,?,?,?)',(run,1,DAY,stamp,status,json.dumps(dict(nm_id=1,auth_run_reference=context,profile_reference='profile',buyer_nonwallet_price_rub=450,buyer_wallet_price_rub=wallet,measured_at=stamp))))
        self.conn.commit()

    def build(self, previous=None):
        return build_market(self.runtime,nm_ids=[1,2],today=DAY,previous=previous or {},ads_loader=self.ads)

    def test_exact_metrics_and_one_ads_batch_not_per_sku(self):
        self.observe('good','current')
        before = self.runtime.db_path.read_bytes()
        result = self.build()
        self.ads.assert_called_once_with()
        self.assertEqual(result[1]['seller_price']['value'],500)
        self.assertEqual(result[1]['buyer_wallet_price']['value'],400)
        self.assertEqual(result[1]['buyer_wallet_price']['captured_at'],STAMP)
        self.assertEqual(result[1]['promo_participation']['value'],0)
        self.assertEqual(result[1]['cpm_bid']['value'],250)
        self.assertEqual(result[1]['cpc_bid']['value'],3.75)
        self.assertTrue(all(cell['value'] is None for cell in result[2].values()))
        self.assertEqual(before,self.runtime.db_path.read_bytes())

    def test_failures_keep_prices_and_bids_timestamp_but_never_old_buyer(self):
        previous = self.build()
        previous[1]['buyer_wallet_price'] = {'value':999,'captured_at':STAMP,'status':'ready','source':'wb_buyer_authenticated'}
        self.ads.side_effect = RuntimeError('private provider response')
        self.save('prices_snapshot',DAY,STAMP,{'kind':'empty','items':[{'nm_id':1,'price_seller_discounted':0}]})
        self.save('promo_by_price',DAY,STAMP,{'kind':'empty','items':[{'nm_id':1,'promo_participation':0}]})
        result = self.build(previous)
        for field in ('seller_price','promo_participation','cpm_bid','cpc_bid'):
            self.assertEqual(result[1][field]['value'],previous[1][field]['value'])
            self.assertEqual(result[1][field]['captured_at'],STAMP)
            self.assertEqual(result[1][field]['status'],'stale')
        self.assertIsNone(result[1]['buyer_wallet_price']['value'])
        self.assertNotIn('private provider response', json.dumps(result))

    def test_prices_use_accepted_current_slots_not_daily_or_other_roles(self):
        for role in ('provisional_current_snapshot','accepted_closed_day_snapshot'):
            self.conn.execute('INSERT INTO temporal_source_slot_snapshots VALUES(?,?,?,?,?)',('prices_snapshot',DAY,role,'2026-10-09T11:00:00Z',json.dumps({'kind':'success','items':[{'nm_id':1,'price_seller_discounted':999}]})))
        self.conn.execute('INSERT INTO temporal_source_snapshots VALUES(?,?,?,?)',('prices_snapshot',DAY,'2026-10-09T12:00:00Z',json.dumps({'kind':'success','items':[{'nm_id':1,'price_seller_discounted':888}]})))
        self.conn.commit()
        cell = self.build()[1]['seller_price']
        self.assertEqual(cell['value'],500)
        self.assertEqual(cell['captured_at'],STAMP)
        self.assertEqual(cell['status'],'ready')

    def test_preserved_acceptance_after_failure_retains_real_source_clock(self):
        self.conn.execute('INSERT INTO temporal_source_closure_state VALUES(?,?,?,?,?,?)',('prices_snapshot',DAY,'today_current','success','accepted_snapshot_preserved_after_invalid_attempt','2026-10-09T11:00:00Z'))
        self.conn.commit()
        cell = self.build()[1]['seller_price']
        self.assertEqual(cell['value'],500)
        self.assertEqual(cell['captured_at'],STAMP)
        self.assertEqual(cell['status'],'stale')

    def test_previous_day_retains_source_clock_and_is_stale_without_cutoff(self):
        self.conn.execute("DELETE FROM temporal_source_slot_snapshots WHERE source_key='prices_snapshot'")
        self.conn.commit()
        self.save('prices_snapshot','2026-09-01','2026-09-01T12:00:00Z',{'kind':'success','items':[{'nm_id':1,'price_seller_discounted':321}]})
        cell = self.build()[1]['seller_price']
        self.assertEqual(cell['value'],321)
        self.assertEqual(cell['captured_at'],'2026-09-01T12:00:00Z')
        self.assertEqual(cell['status'],'stale')

    def test_new_failed_daily_date_with_same_stamp_does_not_fake_ready(self):
        self.conn.execute("DELETE FROM temporal_source_snapshots WHERE source_key='promo_by_price'")
        self.conn.commit()
        self.save('promo_by_price','2026-10-08',STAMP,{'kind':'success','items':[{'nm_id':1,'promo_participation':1}]})
        self.save('promo_by_price',DAY,STAMP,{'kind':'empty','items':[]})
        self.assertEqual(self.build()[1]['promo_participation']['status'],'stale')

    def test_account_reset_does_not_resurrect_prior_session(self):
        self.observe('old','old-context')
        previous = self.build()
        self.observe('new','new-context',stamp='2026-10-09T11:00:00Z',status='failed',wallet=None)
        self.assertIsNone(self.build(previous)[1]['buyer_wallet_price']['value'])

    def test_unavailable_auth_context_clears_previous_buyer(self):
        self.observe('good','current')
        previous = self.build()
        with patch('packages.application.stock_monitor_market.load_daily_projection', side_effect=OSError('unavailable')):
            self.assertIsNone(self.build(previous)[1]['buyer_wallet_price']['value'])

    def test_multiple_campaigns_or_different_placements_are_ambiguous(self):
        for rows in ([self.bid('cpm',1,100),self.bid('cpm',2,100)], [self.bid('cpm',1,100),self.bid('cpm',1,200,'recommendations')]):
            self.ads.return_value = {'index':{1:rows}}
            cell = self.build()[1]['cpm_bid']
            self.assertIsNone(cell['value']); self.assertEqual(cell['status'],'ambiguous')
            self.assertEqual(len(cell['details']),2)

    def test_paused_campaign_or_zero_price_is_not_zero_reference(self):
        self.ads.return_value = {'index':{1:[self.bid('cpm',1,300,status=11)]}}
        self.save('prices_snapshot',DAY,STAMP,{'kind':'success','items':[{'nm_id':1,'price_seller_discounted':0}]})
        result = self.build()
        self.assertIsNone(result[1]['cpm_bid']['value'])
        self.assertIsNone(result[1]['seller_price']['value'])
        self.assertEqual(result[1]['promo_participation']['value'],0)

    def test_actual_ads_normalizer_missing_campaign_or_placement_stays_unknown(self):
        from packages.application.sheet_vitrina_v1_ads import SheetVitrinaV1AdsBlock, _parse_campaign
        for payment in ('cpm','cpc'):
            valid = {'id':11,'status':9,'settings':{'payment_type':payment,'placements':{'search':True}},'nm_settings':[{'nm_id':1,'bids_kopecks':{'search':10000}}]}
            unknown = {'id':22,'status':9,'settings':{'payment_type':payment,'placements':{'search':True}},'nm_settings':[{'nm_id':1,'bids_kopecks':{}}]}
            for adverts in ([valid, unknown], [{**valid,'settings':{'payment_type':payment,'placements':{'search':True,'recommendations':True}}}]):
                campaigns = [_parse_campaign(a) for a in adverts]
                payload = {'fetched_at':STAMP,'campaigns':campaigns}
                block = SimpleNamespace(_load_campaigns=Mock(return_value=payload),_campaign_cache={'payload':payload})
                block.build_placement_index_read = lambda **kwargs: SheetVitrinaV1AdsBlock.build_placement_index_read(block,**kwargs)
                self.runtime.runtime_dir = self.runtime.db_path.parent
                service = StockMonitorService(runtime=self.runtime)
                service._market_ads = block
                read = service._load_market_ads()
                # Actual owning index has just one known bid; same-batch
                # membership must not certify it as the complete active set.
                self.assertEqual(len(read['index'][1]),1)
                result = build_market(self.runtime,nm_ids=[1],today=DAY,previous={},ads_loader=lambda:read)
                cell = result[1][payment+'_bid']
                self.assertIsNone(cell['value'])
                self.assertEqual(cell['status'],'ambiguous')
                self.assertEqual(len(cell['details']),2)
                block._load_campaigns.assert_called_once_with(bypass_cache=False)

    def test_batch_success_and_failure_are_reused_between_periods(self):
        self.runtime.runtime_dir = self.runtime.db_path.parent
        service = StockMonitorService(runtime=self.runtime,ads_loader=self.ads)
        service._load_market_ads(); service._load_market_ads()
        self.ads.assert_called_once_with()
        failed = Mock(side_effect=RuntimeError('unavailable'))
        service = StockMonitorService(runtime=self.runtime,ads_loader=failed)
        for _ in range(2):
            with self.assertRaises(RuntimeError): service._load_market_ads()
        failed.assert_called_once_with()


if __name__ == '__main__': unittest.main()
