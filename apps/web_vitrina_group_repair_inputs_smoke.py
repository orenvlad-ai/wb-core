"""Bounded pure repair, per-SKU parameter parity and source-authority guards."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import sys
import time
import sqlite3
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application.web_vitrina_group_repair_inputs import (
    prepare_group_repair_day, GroupRepairTransform, load_repair_cost_basis, captured_repair_cost_bindings,
)
from apps.web_vitrina_history_store_smoke import expect_error
from packages.application.web_vitrina_compact_table import CELL_DEFAULTS
from packages.application.web_vitrina_history_compiler import digest


def fixture():
    day='2026-09-22';catalog={'rows':{},'order':[]};cells={}
    def put(rid,value,kind='number',formatter='number_default',group='a',quality='exact'):
        scope,key=rid.split('|',1);nm=int(scope[4:]) if scope.startswith('SKU:') else None
        rowkind='sku' if nm else 'group' if scope.startswith('GROUP:') else 'total'
        catalog['rows'][rid]={'row_id':rid,'row_kind':rowkind,'group_id':'group:'+group if rowkind!='total' else None,
            'values':{k:[v] for k,v in dict(scope_kind=rowkind.upper(),scope_key=scope,
                scope_label=str(nm or scope),metric_key=key,metric_label=key,nm_id=nm,section='Test').items()}}
        catalog['order'].append(rid)
        cell=list(CELL_DEFAULTS);cell[:5]=[value,str(value),kind,formatter,'renderer:'+kind+':'+formatter]
        cell[5:13]=['available','neutral','accepted',quality,'','','complete',0]
        cell[13:16]=['observed_count','2026-09-22T12:00:00Z','accepted-finalization']
        cells[rid]=cell
    for nm,q,revenue,profit,opens,views in [(11,10,1000,100,44,4),(12,30,9000,-200,10,100)]:
        for k,v in {'orderCount':q,'orderSum':revenue,'ads_sum':10,'open_card_count':opens,'view_count':views,
            'our_wb_unit_cost_rub':12 if nm==11 else 30,'stock_total':7 if nm==11 else None,
            'proxy_profit_3_rub':profit,'proxy_profit_4_rub':profit,
            'proxy_margin_3_pct':profit/(revenue*.8),'proxy_margin_4_pct':profit/(revenue*.8),
            'proxy_margin_per_unit_rub':profit/(q*.8)}.items():
            put(f'SKU:{nm}|'+k,v)
    put('TOTAL|ctr',.5,'percent','percent_default')
    for k in ('ctr','proxy_margin_3_pct_total','proxy_margin_4_pct_total'):
        put('GROUP:a|'+k,999,'percent','percent_default')
    for k in ('total_proxy_profit_3_rub','total_proxy_profit_4_rub','proxy_margin_per_unit_rub_total',
              'total_our_wb_unit_cost_rub','total_stock_total'):
        put('GROUP:a|'+k,999,'money' if 'rub' in k else 'number','money_rub' if 'rub' in k else 'number_default')
    put('GROUP:a|buyoutPercent',.7,'percent','percent_default')
    basis={11:{'quantity':12,'capital':144,'source_digest':'s11','presentation_digest':'blob','presentation_version':'v'},
           12:{'quantity':30,'capital':900,'source_digest':'s12','presentation_digest':'blob','presentation_version':'v'}}
    return day,catalog,cells,basis


def main():
    started=time.monotonic();day,catalog,cells,basis=fixture()
    result=prepare_group_repair_day(day,catalog,cells,cost_basis=basis)
    assert not result['unresolved'],result['unresolved']
    patched=result['patch'];ctr=patched['GROUP:a|ctr']
    assert ctr[0]==54/104 and ctr[1]=='51,92%'
    assert patched['GROUP:a|total_proxy_profit_4_rub'][0]==-100
    assert abs(patched['GROUP:a|proxy_margin_4_pct_total'][0]-(-100/8000))<1e-12
    assert patched['GROUP:a|proxy_margin_per_unit_rub_total'][0]==-100/32
    assert patched['GROUP:a|total_our_wb_unit_cost_rub'][0]==1044/42
    assert patched['GROUP:a|total_our_wb_unit_cost_rub'][1]=='25 ₽'
    assert patched['GROUP:a|total_stock_total'][0]==7 and patched['GROUP:a|total_stock_total'][12]==1
    assert 'GROUP:a|buyoutPercent' not in patched
    assert all(rid.startswith('GROUP:') for rid in patched)
    assert all(patched[rid][2:5]==cells[rid][2:5] and patched[rid][13:]==cells[rid][13:] for rid in patched)
    applied={**cells,**patched}
    assert all(applied[rid]==old for rid,old in cells.items() if not rid.startswith('GROUP:'))
    callback=GroupRepairTransform(json.loads(json.dumps({day:result}))); assert callback(day,catalog,cells)['patch']==patched
    bad=deepcopy(cells);bad['SKU:11|orderSum'][0]+=1
    expect_error(lambda:callback(day,catalog,bad),ValueError,'input_changed')
    # Checking one scalar or one GROUP aggregate is insufficient: both margins
    # and per-unit rates of EACH eligible SKU must corroborate the dated rate.
    for key in ('proxy_margin_4_pct','proxy_margin_per_unit_rub'):
        bad=deepcopy(cells);bad['SKU:12|'+key][0]*=2
        res=prepare_group_repair_day(day,catalog,bad,cost_basis=basis)
        assert res['unresolved']['GROUP:a|total_proxy_profit_4_rub']
        assert res['patch']['GROUP:a|total_proxy_profit_4_rub']==cells['GROUP:a|total_proxy_profit_4_rub']
    no_basis=prepare_group_repair_day(day,catalog,cells)
    assert no_basis['patch']['GROUP:a|total_our_wb_unit_cost_rub']==cells['GROUP:a|total_our_wb_unit_cost_rub']
    assert no_basis['unresolved']['GROUP:a|total_our_wb_unit_cost_rub']=='accepted_cost_basis_unproven'
    zero=deepcopy(cells)
    for nm in (11,12): zero[f'SKU:{nm}|view_count'][0]=0
    z=prepare_group_repair_day(day,catalog,zero,cost_basis=basis)['patch']['GROUP:a|ctr']
    assert z[0] is None and z[8]=='undefined' and z[12]==0
    partial=deepcopy(cells);partial['SKU:11|view_count'][11:13]=['unknown_scope',None]
    p=prepare_group_repair_day(day,catalog,partial,cost_basis=basis)['patch']['GROUP:a|ctr']
    assert p[0]==54/104 and p[11]=='unknown_scope' and p[12] is None
    authority={'old_epoch':'old','fresh_epoch':'old','old_token':'day','fresh_token':'day'}
    fake=SimpleNamespace(runtime_dir='not-used')
    with patch('packages.application.web_vitrina_group_blocks.accepted_cost_basis',return_value={day:basis}) as load:
        b,proof=load_repair_cost_basis(fake,day,catalog,cells,accepted_binding={'book_version':'exact'},authority=authority,deadline=time.monotonic()+1)
        assert b==basis and proof['checked_saved_costs']==2
        bad=deepcopy(cells);bad['SKU:12|our_wb_unit_cost_rub'][0]+=1
        b,proof=load_repair_cost_basis(fake,day,catalog,bad,accepted_binding={'book_version':'exact'},authority=authority,deadline=time.monotonic()+1)
        assert not b and proof['nm_ids']==[12]
        load.reset_mock()
        b,proof=load_repair_cost_basis(fake,day,catalog,cells,accepted_binding={},authority={**authority,'fresh_token':'changed'},deadline=time.monotonic()+1)
        assert not b and proof['status']=='unresolved_source_authority' and not load.called
        expect_error(lambda:load_repair_cost_basis(fake,day,catalog,cells,accepted_binding={},authority=authority,deadline=time.monotonic()-1),ValueError,'deadline')
    # Exact native header selection reuses compact, freshly captured bindings;
    # neither latest book nor a full READY plan is fetched for the repair.
    conn=sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE registry_upload_current_state(slot INTEGER,bundle_version TEXT)')
    conn.execute('INSERT INTO registry_upload_current_state VALUES(1,?)',('accepted',))
    conn.execute('CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version TEXT,as_of_date TEXT,snapshot_id TEXT,activated_at TEXT,refreshed_at TEXT)')
    conn.execute('CREATE TABLE sheet_vitrina_v1_ready_revisions(bundle_version TEXT,as_of_date TEXT,revision INTEGER)')
    identity=['accepted',day,'snapshot','activated','refreshed',1]
    conn.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?)',identity[:5])
    conn.execute('INSERT INTO sheet_vitrina_v1_ready_revisions VALUES(?,?,?)',(identity[0],day,1));conn.commit()
    conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN');queries=[];conn.set_trace_callback(queries.append)
    binding={'book_version':'exact','date':day,'ready_target':{'bundle_version':'accepted','as_of_date':day}}
    adapter=SimpleNamespace(db_path='pinned',now=datetime(2026,9,23,12,tzinfo=timezone.utc),days=[day],
        context={'fresh':True},_quality_cache={'headers':{digest(identity):{'dates':[day],'book':{day:binding}}}})
    with patch('packages.application.web_vitrina_window_read_context.borrowed_operational_connection',return_value=conn):
        assert captured_repair_cost_bindings(adapter)=={day:binding}
        adapter._quality_cache={'headers':{'wrong':{}}}
        expect_error(lambda:captured_repair_cost_bindings(adapter),ValueError,'header_changed')
    assert not any('plan_json' in q or 'accounting_current' in q for q in queries);conn.close()
    print(json.dumps({'status':'PASS','seconds':round(time.monotonic()-started,3),
        'per_SKU_margin_and_perunit_parity':True,'negative_profit_preserved':True,
        'all_saved_cost_parity':True,'source_epoch_token_before_reads':True,'unproven_preserved':True,
        'GROUP_only_all16_others_unchanged':True,'formatted_percent_money':True,'undefined_not_source_missing':True,
        'partial_unknown_propagated':True,'immutable_prepared_callback':True,'buyout_untouched':True,'exact_native_compact_header_selection':True}))

if __name__=='__main__':main()
