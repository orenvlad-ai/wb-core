"""Synthetic partial Ads acceptance, scope counters and economics; no network."""
import json
import socket
import sys
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.ads_daily_report_contract_smoke import campaign
from apps.sheet_vitrina_v1_business_time_smoke import _build_live_plan
from packages.adapters.ads_compact_block import HttpBackedAdsCompactSource
from packages.application.ads_compact_block import AdsCompactBlock
from packages.application.metric_completeness import aggregate_counters
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_live_plan import SOURCE_TEMPORAL_POLICIES, TEMPORAL_ROLE_ACCEPTED_CLOSED
from packages.contracts.ads_compact_block import AdsCompactRequest, AdsCompactSuccess, AdsCompactEmpty
from packages.contracts.source_attempt_diagnostics import SourceAttemptError
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow
DAY='2026-09-11';OBS='2026-09-11T18:10:34Z'

def partial(pages=None, nms=(101,102), ids=(5,6), day=DAY):
    source=HttpBackedAdsCompactSource(complete_catalog=True, partial_from_date=DAY, batch_sleep_seconds=0)
    replies=[{'all':len(ids),'adverts':[{'status':9,'count':len(ids),'advert_list':[{'advertId':i} for i in ids]}]}]+deepcopy(pages or [[campaign(day=day)]])
    source._get_json=lambda **kw:replies.pop(0)
    with patch('packages.adapters.ads_compact_block.load_runtime_config', return_value=NS(base_url='fixture',token='fixture',timeout_seconds=1)),patch('packages.adapters.ads_compact_block.observed_now',return_value=OBS):
        return AdsCompactBlock(source).execute(AdsCompactRequest('ads_compact',day,list(nms))).result

def row(scope,key,value,cell=None,group=None):
    return WebVitrinaContractRow(row_id=scope+'|'+key,row_order=1,scope_kind=scope.split(':')[0],scope_key=scope,scope_label=scope,metric_key=key,metric_label=key,row_last_updated_at='',section='',group=group,nm_id=None,format='number',values_by_date={DAY:value},presentation_by_date={DAY:cell or {}})

class Tests(unittest.TestCase):
    def test_observed_partial_and_unclassified_platform(self):
        a=campaign(day=DAY);a['days'][0]['apps'][0]['appType']=0
        p=partial([[a]])
        self.assertEqual(p.kind,'incomplete');self.assertEqual(len(p.items),1)
        self.assertEqual(p.missing_nm_ids,[102]);self.assertEqual(p.diagnostics['unresolved_campaign_ids'],[6])
        self.assertEqual(p.diagnostics['unclassified_platform_campaign_ids'],[5])
        self.assertFalse(p.diagnostics['zero_fill_applied']);self.assertIsNone(p.diagnostics['missing_sku_count'])
    def test_bad_data_stays_rejected(self):
        for bad in (True,float('nan'),-1,'0',None):
            a=campaign(day=DAY);a['views']=bad
            with self.subTest(bad=bad),self.assertRaises(SourceAttemptError):partial([[a]])
        for payload in (None,[],[{'advertId':5,'days':[]}], [campaign(day=DAY),campaign(day=DAY)]):
            with self.subTest(payload=payload),self.assertRaises(SourceAttemptError):partial([payload])
    def test_observed_zero_and_undefined_rate(self):
        p=partial([[campaign(day=DAY,zero=True)]])
        self.assertEqual(p.items[0].ads_sum,0);self.assertIsNone(p.items[0].ads_cpc)
        self.assertEqual(p.kind,'incomplete');self.assertEqual(p.count,1)
    def test_cutover_strict_before_11(self):
        with self.assertRaises(SourceAttemptError):partial(day='2026-09-10')
    def test_acceptance_closure_and_old_complete(self):
        p=partial()
        with TemporaryDirectory() as tmp:
            runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp));block=_build_live_plan(runtime)
            block.now_factory=lambda:datetime(2026,9,12,0,20,tzinfo=timezone.utc)
            kw=dict(source_key='ads_compact',temporal_slot='yesterday_closed',temporal_policy=SOURCE_TEMPORAL_POLICIES['ads_compact'],column_date=DAY,requested_nm_ids=[101,102],execution_mode='auto_daily',accepted_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,allow_persisted_retry=True,current_web_source_sync_note=None)
            status,result=block._capture_temporal_source_with_acceptance(**kw,loader=lambda:p)
            self.assertIsNotNone(result);self.assertEqual(result.kind,'incomplete')
            self.assertEqual(status.diagnostics['source_observed_at'],OBS)
            closure=runtime.load_temporal_source_closure_state(source_key='ads_compact',target_date=DAY,slot_kind='yesterday_closed')
            self.assertNotEqual(closure.state,'success');self.assertIsNone(closure.last_success_at)
            for prior in (AdsCompactSuccess('success',DAY,p.count,p.items),AdsCompactEmpty('empty',DAY,0,[],'confirmed fixture zero',{'completeness_state':'complete','no_activity_proven':True,'dated_roster_state':'caller_qualified_dated_roster'})):
                runtime.save_temporal_source_slot_snapshot(source_key='ads_compact',snapshot_date=DAY,snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,captured_at='2026-09-12T00:10:00Z',payload=prior)
                before=runtime.load_temporal_source_slot_snapshot(source_key='ads_compact',snapshot_date=DAY,snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED)
                status,result=block._capture_temporal_source_with_acceptance(**kw,loader=lambda:p)
                after=runtime.load_temporal_source_slot_snapshot(source_key='ads_compact',snapshot_date=DAY,snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED)
                self.assertEqual(before,after);self.assertEqual(result.kind,prior.kind)
    def test_general_scope_counter(self):
        sku=[row('SKU:A','m',10,{'quality_state':'partial'}),row('SKU:B','m',0),row('SKU:C','m',None),row('SKU:D','m',None)]
        ev={'operand_date':DAY,'applicable_scope':['SKU:A','SKU:B','SKU:C','SKU:D'],'inapplicable_scope':['SKU:D'],'sku_metric_keys':['m'],'missing_scope':['SKU:A','SKU:B'],'group_scopes':{'GROUP:g':['SKU:A','SKU:C']}}
        # Supplied evidence B is excluded from group; exact dedup A/C=2.
        group=row('GROUP:g','m_total',10,{'evidence':ev})
        got=aggregate_counters(sku+[group],dates=[DAY])[-1].presentation_by_date[DAY]
        self.assertEqual(got['missing_sku_count'],2)
        ev['missing_scope']=[];total=row('TOTAL','m_total',10,{'evidence':ev})
        self.assertEqual(aggregate_counters(sku+[total],dates=[DAY])[-1].presentation_by_date[DAY]['missing_sku_count'],2)
        ev['applicable_scope']=['SKU:B'];self.assertEqual(aggregate_counters(sku+[total],dates=[DAY])[-1].presentation_by_date[DAY]['missing_sku_count'],0)
        ev['applicable_scope']=['SKU:C'];got=aggregate_counters(sku+[replace(total,values_by_date={DAY:None})],dates=[DAY])[-1]
        self.assertEqual(got.presentation_by_date[DAY]['missing_sku_count'],1);self.assertIsNone(got.values_by_date[DAY])
    def test_unknown_impact_does_not_become_count_one(self):
        r=row('TOTAL','ads_sum',10,{'quality_state':'partial','completeness_state':'unknown_scope','missing_sku_count':None})
        self.assertIsNone(aggregate_counters([r],dates=[DAY])[0].presentation_by_date[DAY]['missing_sku_count'])
    def test_registry_formula_shared_scope_and_merge(self):
        from packages.application.sheet_vitrina_v1_live_plan import _MetricEvaluator, _merge_cell_presentations
        from packages.application.metric_completeness import ads_partial_presentation, evaluator_scope_presentation
        from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item, FormulaV2Item
        formulas={x['formula_id']:FormulaV2Item(**x) for x in json.loads((ROOT/'artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/input/formulas_v3_seed__fixture.json').read_text())['items']}
        def m(key,scope,kind,ref):return MetricV2Item(key,True,scope,key,kind,ref,True,'number',1,'ads')
        metrics={x.metric_key:x for x in [m('orderSum','SKU','metric','orderSum'),m('ads_sum','SKU','metric','ads_sum'),m('total_orderSum','TOTAL','metric','orderSum'),m('total_ads_sum','TOTAL','metric','ads_sum'),m('drr','TOTAL','formula','F_ADS_ROAS_TOTAL'),m('drr_group','GROUP','formula','F_ADS_ROAS')]}
        slot=NS(slot_key='today_current',column_date=DAY)
        status=NS(source_key='ads_compact',kind='incomplete',temporal_slot=slot.slot_key,column_date=DAY,diagnostics=partial().diagnostics)
        evaluator=_MetricEvaluator(enabled_config=[ConfigV2Item(101,True,'A','g',1),ConfigV2Item(102,True,'B','g',2)],metrics_by_key=metrics,formulas_by_id=formulas,live_sources=NS(statuses=[status]))
        inputs={(101,'orderSum'):100,(101,'ads_sum'):10,(102,'orderSum'):900,(102,'ads_sum'):None}
        evaluator._resolve_direct_sku=lambda key,nm,slot:inputs.get((nm,key))
        self.assertEqual(evaluator.resolve_total('drr',slot.slot_key),.1)
        self.assertEqual(evaluator.resolve_group('drr_group','g',slot.slot_key),.1)
        data=[['total','TOTAL|drr',.1]]
        merged=_merge_cell_presentations(evaluator_scope_presentation(rows=data,slots=[slot],evaluator=evaluator,current_date=DAY),ads_partial_presentation(rows=data,slots=[slot],statuses=[status],metrics=metrics,formulas=formulas),{'TOTAL|drr':{DAY:{'state':'unconfirmed','evidence':{'domain':'fixture'}}}})
        cell=merged['TOTAL|drr'][DAY]
        self.assertEqual(cell['completeness_state'],'unknown_scope');self.assertEqual(cell['quality_state'],'partial')
        self.assertEqual(cell['metric_scope_evidence']['applicable_scope'],['SKU:101','SKU:102'])
        inputs[(101,'ads_sum')]=None;evaluator.sku_cache.clear();evaluator.total_cache.clear()
        self.assertIsNone(evaluator.resolve_total('drr',slot.slot_key))
    def test_finance_full_only_coverage(self):
        import sqlite3
        from datetime import date
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        conn.execute('CREATE TABLE temporal_source_slot_snapshots(source_key,snapshot_date,snapshot_role,captured_at,payload_json)')
        p=partial(nms=(101,))
        conn.execute('INSERT INTO temporal_source_slot_snapshots VALUES(?,?,?,?,?)',('ads_compact',DAY,'accepted_closed_day_snapshot',OBS,json.dumps(asdict(p))))
        result=WbFinanceWeeklyBlock._ads_coverage_manifest(None,conn,nm_ids=['101'],date_from=date.fromisoformat(DAY),date_to=date.fromisoformat(DAY))
        self.assertFalse(result['complete']);self.assertEqual(result['missing_date_nm_id_count'],1)
        conn.close()
    def test_partial_economics_same_operands(self):
        from apps.web_vitrina_catalog_economics_smoke import fixture,P
        from packages.application.vitrina_economics import project_catalog_economics
        original=fixture();day='2026-09-08';cells=original.setdefault('metadata',{}).setdefault('server_cell_presentation',{})
        cells.setdefault('SKU:1|ads_sum',{})[day]={'quality_state':'partial','completeness_state':'unknown_scope','source_as_of_date':day,'quality_reason':'unknown contribution'}
        plan=project_catalog_economics(original,day=day,parameters=(P,P));c=plan['metadata']['server_cell_presentation']
        self.assertEqual(c['SKU:1|proxy_profit_4_rub'][day]['quality_state'],'partial')
        total=c['TOTAL|total_proxy_profit_4_rub'][day]
        self.assertIsNone(total['missing_sku_count']);self.assertEqual(total['evidence']['partial_scope'],['SKU:1'])
        self.assertEqual(total['evidence']['eligible_scope'],c['TOTAL|proxy_margin_4_pct_total'][day]['evidence']['eligible_scope'])

if __name__=='__main__':
    def forbidden(*a,**k):raise AssertionError('network forbidden')
    socket.create_connection=forbidden
    unittest.main(verbosity=2)
