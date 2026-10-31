"""Sparse financial days become zero only after complete dated source proof."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import copy
from datetime import date
import json
import sqlite3
import unittest
from packages.application.canonical_wb_cost_resolver import proven_finance_no_operation_skus
from apps.our_wb_costs_smoke import _slot_lookup, _metric
from packages.application.sheet_vitrina_v1_live_plan import _MetricEvaluator, TemporalLiveSources
from packages.application.sheet_vitrina_v1_our_wb_costs import extend_metrics_with_our_wb_cost_metrics
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item

FIELDS = ('covered_sales_revenue_rub','uncovered_sales_revenue_rub','covered_returns_revenue_rub',
          'uncovered_returns_revenue_rub','covered_sales_units','uncovered_sales_units')
DAY = date(2026,10,20)

def coverage(daily):
    return {**{k: sum(r[k] for r in daily) for k in FIELDS}, 'daily_rows':daily,
        'global_week_content_hash':'hash', 'identity_blockers':[],
        'quality':{'operation_date_fallback_rows':0}}

def fixture():
    c=sqlite3.connect(':memory:')
    c.executescript('''
    CREATE TABLE wb_finance_weekly_sync(seller_id,week_start,week_end,status,unchanged_sync_count,last_error,content_hash,raw_row_count,report_count);
    CREATE TABLE wb_finance_weekly_reports(seller_id,week_start,week_end,row_count,content_hash);
    CREATE TABLE wb_finance_weekly_sku_aggregates(seller_id,week_start,week_end,nm_id,coverage_json,week_content_hash,raw_row_count,formula_version);
    INSERT INTO wb_finance_weekly_sync VALUES('s','2026-10-19','2026-10-25','completed',2,NULL,'hash',2,1);
    INSERT INTO wb_finance_weekly_reports VALUES('s','2026-10-19','2026-10-25',2,'reporthash');
    ''')
    daily={**dict.fromkeys(FIELDS,0),'operation_date':'2026-10-19','uncovered_sales_revenue_rub':500,'uncovered_sales_units':1}
    for nm,rows,count in [('497413000',[daily],1),('497413001',[],0),('__account__',[],1)]:
        c.execute('INSERT INTO wb_finance_weekly_sku_aggregates VALUES(?,?,?,?,?,?,?,?)',
            ('s','2026-10-19','2026-10-25',nm,json.dumps(coverage(rows)),'hash',count,'wb_finance_weekly_sku_aggregate_v5'))
    return c

class AbsenceProofTest(unittest.TestCase):
    def test_positive_and_future_unknown(self):
        with fixture() as c:
            self.assertEqual(proven_finance_no_operation_skus(c,as_of_date=DAY),{497413000,497413001})
            self.assertEqual(proven_finance_no_operation_skus(c,as_of_date=date(2026,10,19)),{497413001})
            self.assertEqual(proven_finance_no_operation_skus(c,as_of_date=date(2026,10,26)),set())

    def test_partial_stale_ambiguous_and_unknown_proofs_refused(self):
        mutations=[
            "UPDATE wb_finance_weekly_sync SET status='loaded_preliminary'",
            "UPDATE wb_finance_weekly_sync SET unchanged_sync_count=0",
            "UPDATE wb_finance_weekly_sync SET last_error='failed'",
            "UPDATE wb_finance_weekly_sync SET content_hash='new'",
            "UPDATE wb_finance_weekly_reports SET row_count=1",
            "UPDATE wb_finance_weekly_sku_aggregates SET raw_row_count=0 WHERE nm_id='497413000'",
            "UPDATE wb_finance_weekly_sku_aggregates SET formula_version='old'",
            "DELETE FROM wb_finance_weekly_sku_aggregates WHERE nm_id='__account__'",
            "INSERT INTO wb_finance_weekly_sync SELECT 'other',week_start,week_end,status,unchanged_sync_count,last_error,content_hash,raw_row_count,report_count FROM wb_finance_weekly_sync",
        ]
        for sql in mutations:
            with self.subTest(sql=sql), fixture() as c:
                c.execute(sql)
                self.assertEqual(proven_finance_no_operation_skus(c,as_of_date=DAY),set())
        for payload in [[], {'quality':None}, coverage([]),
                        {**coverage([]),'identity_blockers':['unknown']},
                        {**coverage([]),'quality':{'operation_date_fallback_rows':1}}]:
            with self.subTest(payload=payload), fixture() as c:
                # Empty daily decomposition with existing positive weekly total fails.
                if payload == coverage([]): payload['uncovered_sales_revenue_rub']=500
                c.execute("UPDATE wb_finance_weekly_sku_aggregates SET coverage_json=? WHERE nm_id='497413000'",(json.dumps(payload),))
                self.assertEqual(proven_finance_no_operation_skus(c,as_of_date=DAY),set())

    def test_bad_dates_duplicates_and_unattributed_sales_refused(self):
        for mode in ('bad_date','duplicate','account_sale','missing_daily_field','nonfinite'):
            with self.subTest(mode=mode), fixture() as c:
                nm='__account__' if mode=='account_sale' else '497413000'
                payload=json.loads(c.execute('SELECT coverage_json FROM wb_finance_weekly_sku_aggregates WHERE nm_id=?',(nm,)).fetchone()[0])
                if mode=='bad_date': payload['daily_rows'][0]['operation_date']='2026-10-19x'
                elif mode=='duplicate': payload['daily_rows']*=2
                elif mode=='account_sale':
                    row={**dict.fromkeys(FIELDS,0),'operation_date':'2026-10-19','uncovered_sales_revenue_rub':50}
                    payload=coverage([row])
                elif mode=='missing_daily_field': payload['daily_rows'][0].pop('uncovered_sales_units')
                else: payload['uncovered_sales_revenue_rub']='NaN'
                c.execute('UPDATE wb_finance_weekly_sku_aggregates SET coverage_json=? WHERE nm_id=?',(json.dumps(payload),nm))
                self.assertEqual(proven_finance_no_operation_skus(c,as_of_date=DAY),set())

    def test_diagnostic_only_preserves_cost_profit_and_strict_total(self):
        lookup=_slot_lookup(column_date=DAY.isoformat(),onec_cost=100,our_cost=80)
        lookup.our_wb_cost_lookup={497413000:{'sales_without_cost_rub':'500'}}
        before=copy.deepcopy(lookup.our_wb_cost_lookup)
        metrics={m.metric_key:m for m in extend_metrics_with_our_wb_cost_metrics([_metric('orderSum'),_metric('orderCount'),_metric('ads_sum')])}
        config=[ConfigV2Item(nm_id=n,enabled=True,display_name=str(n),group='A',display_order=i) for i,n in enumerate((497413000,497413001))]
        def evaluator():
            return _MetricEvaluator(enabled_config=config,metrics_by_key=metrics,formulas_by_id={},
                live_sources=TemporalLiveSources(temporal_slots=[],statuses=[],slot_lookups={'d':lookup},source_temporal_policies={}))
        old=evaluator()
        self.assertIsNone(old.resolve_total('total_sales_without_cost_rub','d'))
        others={k:old.resolve_sku(k,497413001,'d') for k,m in metrics.items() if m.scope == 'SKU' and k != 'sales_without_cost_rub'}
        lookup.finance_no_operation_skus={497413000,497413001}
        new=evaluator()
        self.assertEqual(new.resolve_total('total_sales_without_cost_rub','d'),500)
        self.assertEqual(new.resolve_sku('sales_without_cost_rub',497413001,'d'),0)
        self.assertEqual(lookup.our_wb_cost_lookup,before)
        self.assertEqual({k:new.resolve_sku(k,497413001,'d') for k in others},others)
        config.append(ConfigV2Item(nm_id=999,enabled=True,display_name='unknown',group='A',display_order=3))
        self.assertIsNone(evaluator().resolve_total('total_sales_without_cost_rub','d'))

if __name__=='__main__': unittest.main()
