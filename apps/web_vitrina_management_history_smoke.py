"""Missing-only estimate, dated formulas, period identity and persistence checks."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
from types import SimpleNamespace
import json
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.calculation_parameters import DEFAULT_PROXY_PARAMETERS
from packages.application.calculation_parameters_v4 import _parameters_from_values, PROXY_V4_FORMULA_VERSION
from packages.application.web_vitrina_management_history import project, carry_forward, restore_rows, SOURCE
from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
from packages.application.sheet_vitrina_v1_web_vitrina import _resolve_period_date_bindings, _build_period_snapshot
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaWriteTarget, SheetVitrinaV1TemporalSlot


def main():
    p3 = DEFAULT_PROXY_PARAMETERS
    p4 = SimpleNamespace(buyout_rate=Decimal('.8'), included_expense_rate=Decimal('.3'),
                         retained_share=Decimal('.7'), version_id='dated-v4')
    from packages.application.web_vitrina_management_history import TARGET_METRICS, data_sheet
    day='2026-09-01'
    source={'snapshot_id':'frozen5','column_date':'2026-09-05','plan_version':'v1','bundle_version':'b', 'costs':{}}
    rows=[]
    for scope, cost in [('SKU:1','10'),('SKU:2','20'),('TOTAL','15')]:
        source['costs'][scope+'|'+('total_' if scope=='TOTAL' else '')+'our_wb_unit_cost_rub']={
            'management_value':cost, 'source':'official_fbs_management_inventory_v1', 'source_as_of_date':'2026-09-05'}
        for metric in TARGET_METRICS:
            is_total=metric.startswith('total_') or metric.endswith('_total')
            if is_total != (scope=='TOTAL'): continue
            rows.append(['test',scope+'|'+metric,''])
        if scope!='TOTAL':
            for metric,value in [('orderSum',1000),('orderCount',4 if scope=='SKU:1' else 0),('ads_sum',10),('stock_total',987)]:
                rows.append(['test',scope+'|'+metric,value])
    plan={'date_columns':[day], 'metadata':{}, 'sheets':[{'sheet_name':'DATA_VITRINA','header':['label','key',day],'rows':rows}]}
    # The second SKU already has a canonical cost: do not replace it by 20.
    next(r for r in rows if r[1]=='SKU:2|our_wb_unit_cost_rub')[2]=19
    result=project(plan,dates=[day],source=source,parameters={day:(p3,p4)},operation_id='test-operation')
    values={r[1]:r[2] for r in data_sheet(result['plan'])['rows']}
    assert values['SKU:1|our_wb_unit_cost_rub']==10 and values['SKU:2|our_wb_unit_cost_rub']==19
    assert values['TOTAL|total_our_wb_unit_cost_rub']==15
    assert values['SKU:1|proxy_profit_4_rub']==518
    assert values['TOTAL|proxy_margin_per_unit_rub_total']==518/3.2
    assert values['SKU:2|proxy_margin_per_unit_rub']==''
    assert values['SKU:2|stock_total']==987
    assert project(result['plan'],dates=[day],source=source,parameters={day:(p3,p4)},operation_id='test-operation')['changes']==[]
    missing=deepcopy(plan)
    next(r for r in data_sheet(missing)['rows'] if r[1]=='SKU:1|orderSum')[2]=''
    m=project(missing,dates=[day],source=source,parameters={day:(p3,p4)},operation_id='test-missing')
    mv={r[1]:r[2] for r in data_sheet(m['plan'])['rows']}
    assert mv['TOTAL|total_proxy_profit_4_rub']=='' and mv['SKU:1|proxy_profit_3_rub']==''
    from packages.application.web_vitrina_management_history import project_complete_day,digest,FACT_GROUPS
    fact_sources={k:{'payload':{'kind':'success','items':[{'nm_id':1},{'nm_id':2}]},
        'business_date':day,'complete':True,'fetched_at':'2026-09-05T17:00:00Z'} for k in FACT_GROUPS}
    fact_sources['sales_funnel_history']['payload']['items']=[{'nm_id':n,'date':day,'metric':metric,'value':value}
        for n in (1,2) for metric,value in [('orderSum',2000),('orderCount',8)]]
    fact_sources['ads_compact']['payload']['items']=[{'nm_id':n,'ads_sum':20} for n in (1,2)]
    for source_item in fact_sources.values():source_item['payload_sha256']=digest(source_item['payload'])
    registry=[SimpleNamespace(metric_key=k,calc_type='metric',calc_ref=k) for k in ['orderSum','orderCount','ads_sum']]
    with patch('packages.application.registry_upload_db_backed_runtime._load_config_items',return_value=[SimpleNamespace(nm_id=n,enabled=True,group='g',display_order=n) for n in (1,2)]), \
         patch('packages.application.registry_upload_db_backed_runtime._load_metric_items',return_value=registry), \
         patch('packages.application.registry_upload_db_backed_runtime._load_formula_items',return_value=[]):
        facts=project_complete_day(plan,sources=fact_sources,conn=None,bundle_version='b',operation_id='facts')
        assert next(r[2] for r in data_sheet(facts['plan'])['rows'] if r[1]=='SKU:1|orderSum')==2000
        assert next(r[2] for r in data_sheet(facts['plan'])['rows'] if r[1]=='SKU:1|stock_total')==987
        assert all(c['row_id'].split('|')[1] in {'orderSum','orderCount','ads_sum'} for c in facts['changes'])
    sheet=SheetVitrinaWriteTarget('DATA_VITRINA','A1','A1:C99','A:C','replace',False,['label','key',day],rows,len(rows),3)
    older=SheetVitrinaV1Envelope('v1','older-bundle','2026-08-31',[day],[],{},[sheet])
    from packages.application.web_vitrina_management_history import FACT_SOURCE,FACT_GROUPS
    older=replace(older,metadata={'server_cell_presentation':{'SKU:1|orderSum':{day:{'source':FACT_SOURCE,
        'source_as_of_date':day,'complete_source_groups':list(FACT_GROUPS)}}}})
    latest_same_outer=replace(older,snapshot_id='new-bundle',date_columns=['2026-08-31'],sheets=[replace(sheet,header=['label','key','2026-08-31'])])
    class Runtime:
        exact=[]
        covering=older
        def list_sheet_vitrina_ready_snapshot_dates_any_bundle(self,**kwargs): return self.exact
        def load_sheet_vitrina_ready_snapshot_covering_date_any_bundle(self,**kwargs):
            if self.covering is None: raise ValueError('no exact column')
            return self.covering
        def load_sheet_vitrina_ready_snapshot_any_bundle(self,**kwargs): return latest_same_outer
    runtime=Runtime()
    runtime.covering=replace(older,metadata={})
    assert _resolve_period_date_bindings(runtime=runtime,date_from=day,date_to=day,default_visible_snapshot=None)[0].missing
    runtime.covering=older
    combined,bindings=_build_period_snapshot(runtime=runtime,date_from=day,date_to=day,default_visible_snapshot=None)
    assert bindings[0].covering_snapshot is older and combined.date_columns==[day]
    assert combined.sheets[0].rows==rows
    runtime.exact=[day]
    bindings=_resolve_period_date_bindings(runtime=runtime,date_from=day,date_to=day,default_visible_snapshot=None)
    assert bindings[0].covering_snapshot is None and bindings[0].snapshot_as_of_date==day
    runtime.exact=[];runtime.covering=None
    assert _resolve_period_date_bindings(runtime=runtime,date_from=day,date_to=day,default_visible_snapshot=None)[0].missing
    frozen=result['plan']['metadata']['server_cell_presentation']
    persisted=carry_forward(replace(older,as_of_date=day),presentation=frozen)
    assert persisted.metadata['server_cell_presentation']['SKU:1|our_wb_unit_cost_rub'][day]['source']==SOURCE
    assert next(r for r in persisted.sheets[0].rows if r[1]=='SKU:1|our_wb_unit_cost_rub')[2]==10
    preserved=deepcopy(plan)
    next(r for r in data_sheet(preserved)['rows'] if r[1]=='SKU:1|proxy_profit_4_rub')[2]=400
    mixed=project(preserved,dates=[day],source=source,parameters={day:(p3,p4)},operation_id='preserved')
    assert next(r[2] for r in data_sheet(mixed['plan'])['rows'] if r[1]=='SKU:1|proxy_margin_per_unit_rub')==125
    from packages.application.web_vitrina_management_history import recalculate_current
    empty_markers=deepcopy(plan)
    empty_markers['metadata']={}
    recalculate_current(empty_markers,business_date=day,parameters=None)
    live=deepcopy(plan)
    live['metadata']={'server_cell_presentation':{k:{day:{**c,'source_as_of_date':day}} for k,c in source['costs'].items()}}
    for row in data_sheet(live)['rows']:
        if row[1] in source['costs']: row[2]=float(source['costs'][row[1]]['management_value'])
    live=recalculate_current(live,business_date=day,parameters=(p3,p4))
    assert next(r[2] for r in data_sheet(live)['rows'] if r[1]=='SKU:1|proxy_profit_4_rub')==518
    live['metadata']['server_cell_presentation']['SKU:1|our_wb_unit_cost_rub'][day]['management_value']='11'
    for row in data_sheet(live)['rows']:
        if row[1]=='SKU:1|orderSum': row[2]=2000
        if row[1]=='SKU:1|orderCount': row[2]=5
    changed=recalculate_current(live,business_date=day,parameters=(p3,p4))
    assert next(r[2] for r in data_sheet(changed)['rows'] if r[1]=='SKU:1|proxy_profit_4_rub')==1066
    changed['metadata']['server_cell_presentation']['SKU:2|our_wb_unit_cost_rub'][day]['management_value']=''
    partial=recalculate_current(changed,business_date=day,parameters=(p3,p4))
    pv={r[1]:r[2] for r in data_sheet(partial)['rows']}
    assert pv['SKU:1|proxy_profit_4_rub']==1066 and pv['SKU:2|proxy_profit_4_rub']=='' and pv['TOTAL|total_proxy_profit_4_rub']==''
    assert next(r[2] for r in data_sheet(recalculate_current(changed,business_date=day,parameters=None))['rows'] if r[1]=='SKU:1|proxy_profit_4_rub')==''
    from packages.application.warehouse_functional_economics_backfill import _transform_snapshot
    for previously_closed in (False,True):
        owned=deepcopy(result['plan']);data_sheet(owned)['write_start_cell']='A1'
        if previously_closed:
            owned['metadata']['warehouse_history_coverage']={day:{'status':'closed','functional_version_id':'old','covered_nm_id_count':2,'uncovered_scope_nm_ids':[]}}
        replay=_transform_snapshot({'plan_json':json.dumps(owned),'refreshed_at':'2026-09-05T10:00:00Z'},
            costs={day:{}},warehouse_metrics={day:{}},warehouse_exact_dates={day},warehouse_covered_nm_ids={day:{1,2}},warehouse_version_ids={day:'new'},
            parameters={day:p3},proxy_v4_parameters={day:p4},source_fingerprint='test',cutover_business_date='2026-07-18',operation_business_date='2026-09-05')
        transformed=json.loads(replay['after_plan_json'])
        for change in result['changes']:
            key=change['row_id']
            assert transformed['metadata']['server_cell_presentation'][key][day]==owned['metadata']['server_cell_presentation'][key][day]
            assert next(r[2] for r in data_sheet(transformed)['rows'] if r[1]==key)==change['after']
    _writer_rollover(rows, p3, p4)
    print('management_history: mixed exact/estimate, no missing-to-zero, dated formulas, eligible TOTAL, idempotency, rollover, cross-bundle exact column: ok')


def _writer_rollover(base_rows,p3,p4):
    from apps.web_vitrina_official_fbs_smoke import fixture
    from packages.application.web_vitrina_official_fbs import build_current_official_fbs_estimate
    from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
    from apps.sheet_vitrina_v1_web_vitrina_contract_smoke import BUNDLE_FIXTURE
    class Clock(datetime):
        current=datetime(2026,9,5,10,10,tzinfo=timezone.utc)
        @classmethod
        def now(cls,tz=None): return cls.current
    with TemporaryDirectory() as tmp:
        path=Path(tmp)/'source.sqlite3';conn=fixture(path);conn.close()
        model=build_current_official_fbs_estimate(path,nm_ids=[1,2],now=Clock.current)
        runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp)/'runtime')
        runtime.ingest_bundle(json.loads(BUNDLE_FIXTURE.read_text()),activated_at='2026-09-05T10:00:00Z')
        state=runtime.load_current_state()
        rows=deepcopy(base_rows)
        sheet=SheetVitrinaWriteTarget('DATA_VITRINA','A1','A1:C99','A:C','replace',False,['label','key','2026-09-05'],rows,len(rows),3)
        status=SheetVitrinaWriteTarget('STATUS','A1','A1:A1','A:A','replace',False,['status'],[],0,1)
        plan=SheetVitrinaV1Envelope('v1','save5-first','2026-09-04',['2026-09-05'],[SheetVitrinaV1TemporalSlot('today_current','today','2026-09-05')],{},[sheet,status])
        with patch('packages.application.registry_upload_db_backed_runtime.datetime',Clock), patch('packages.application.web_vitrina_management_history.dated_parameters',return_value=(p3,p4)), patch('packages.application.web_vitrina_official_fbs.build_current_official_fbs_estimate',return_value=model):
            runtime.save_sheet_vitrina_ready_snapshot(current_state=state,refreshed_at='2026-09-05T10:10:00Z',plan=plan)
        first=runtime.load_sheet_vitrina_ready_snapshot(as_of_date='2026-09-04')
        firstprofit=next(r[2] for r in first.sheets[0].rows if r[1]=='SKU:1|proxy_profit_4_rub')
        revised=deepcopy(first.sheets[0].rows)
        next(r for r in revised if r[1]=='SKU:1|orderSum')[2]=2000
        model=deepcopy(model);model['skus'][1]['cost']+=Decimal('1')
        plan=replace(first,snapshot_id='save5-second',sheets=[replace(first.sheets[0],rows=revised),status])
        with patch('packages.application.registry_upload_db_backed_runtime.datetime',Clock), patch('packages.application.web_vitrina_management_history.dated_parameters',return_value=(p3,p4)), patch('packages.application.web_vitrina_official_fbs.build_current_official_fbs_estimate',return_value=model):
            runtime.save_sheet_vitrina_ready_snapshot(current_state=state,refreshed_at='2026-09-05T10:20:00Z',plan=plan)
        latest=runtime.load_sheet_vitrina_ready_snapshot(as_of_date='2026-09-04')
        lastprofit=next(r[2] for r in latest.sheets[0].rows if r[1]=='SKU:1|proxy_profit_4_rub')
        lastcost=next(r[2] for r in latest.sheets[0].rows if r[1]=='SKU:1|our_wb_unit_cost_rub')
        assert lastprofit!=firstprofit
        from packages.application.warehouse_functional_economics_backfill import _transform_snapshot
        hourly=_transform_snapshot({'plan_json':json.dumps(asdict(latest)),'refreshed_at':'2026-09-05T10:20:00Z'},
            costs={'2026-09-05':{}},warehouse_metrics={'2026-09-05':{}},warehouse_exact_dates={'2026-09-05'},
            warehouse_covered_nm_ids={'2026-09-05':{1,2}},warehouse_version_ids={'2026-09-05':'different'},
            parameters={'2026-09-05':p3},proxy_v4_parameters={'2026-09-05':p4},source_fingerprint='hourly',
            cutover_business_date='2026-07-18',operation_business_date='2026-09-06')
        with sqlite3.connect(runtime.db_path) as db:
            db.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?',(hourly['after_plan_json'],'2026-09-04'))
        Clock.current=datetime(2026,9,6,10,10,tzinfo=timezone.utc)
        cleared=[r[:2]+['',''] for r in latest.sheets[0].rows]
        tomorrow=replace(latest,snapshot_id='save6',as_of_date='2026-09-05',date_columns=['2026-09-05','2026-09-06'],metadata={},
            temporal_slots=[SheetVitrinaV1TemporalSlot('yesterday_closed','closed','2026-09-05'),SheetVitrinaV1TemporalSlot('today_current','current','2026-09-06')],
            sheets=[replace(latest.sheets[0],header=['label','key','2026-09-05','2026-09-06'],rows=cleared,column_count=4),status])
        with patch('packages.application.registry_upload_db_backed_runtime.datetime',Clock), patch('packages.application.web_vitrina_official_fbs.build_current_official_fbs_estimate',return_value={'available':False}):
            runtime.save_sheet_vitrina_ready_snapshot(current_state=state,refreshed_at='2026-09-06T10:10:00Z',plan=tomorrow)
        closed=runtime.load_sheet_vitrina_ready_snapshot(as_of_date='2026-09-05')
        assert next(r[2] for r in closed.sheets[0].rows if r[1]=='SKU:1|proxy_profit_4_rub')==lastprofit
        assert next(r[2] for r in closed.sheets[0].rows if r[1]=='SKU:1|our_wb_unit_cost_rub')==lastcost
        assert closed.metadata['server_cell_presentation']['SKU:1|our_wb_unit_cost_rub']['2026-09-05']['source']=='official_fbs_management_inventory_v1'


if __name__ == '__main__':
    main()
