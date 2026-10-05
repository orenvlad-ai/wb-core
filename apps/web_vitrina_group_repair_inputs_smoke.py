"""Bounded pure repair, per-SKU parameter parity and source-authority guards."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import math
import sys
import time
import sqlite3
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application.web_vitrina_group_repair_inputs import (
    prepare_group_repair_day, GroupRepairTransform, load_repair_cost_basis, captured_repair_cost_bindings,
    retained_repair_cost_bindings,
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
    assert not any('plan_json' in q or 'accounting_current' in q for q in queries)
    # The original builder cache is operational provenance, not a claim that
    # a changed whole-day token matched. A newly exact READY must not replace
    # the original covered day's older book (production Oct5 <- Oct4 shape).
    covered, today = '2026-09-23', '2026-09-24'
    old_binding={**binding,'date':covered}
    index={'shared_days':{},'wb_days':{},'retained_days':{},
           'presentations':{day:'blob',covered:'covered-blob'}}
    retained_cache={'source':['old-source'], 'headers':{digest(identity):{
        'dates':[day,covered],'book':{day:binding,covered:old_binding}}},
        'book':{'exact':{'index':index,'verified':['blob','covered-blob']}}}
    conn.rollback();conn.execute('PRAGMA query_only=OFF')
    newer=['accepted',covered,'new-exact','new-activated','new-refreshed',1]
    conn.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?)',newer[:5])
    conn.execute('INSERT INTO sheet_vitrina_v1_ready_revisions VALUES(?,?,?)',(newer[0],covered,1))
    conn.commit();conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
    adapter.days=[day,covered,today]
    adapter._quality_cache={'source':['old-source'],'headers':{
        **retained_cache['headers'],digest(newer):{'dates':[covered],
            'book':{covered:{**old_binding,'book_version':'latest'}}}}}
    original={'catalog':digest(catalog),'days':{d:digest(['old-object',d]) for d in adapter.days},
              'consumed':{'epoch':'old','dates':{d:'day' for d in adapter.days}}}
    origin_id=digest(original)
    def retained(served=original):
        return retained_repair_cost_bindings(adapter,retained_cache,cache_sha256='a'*64,
            original_edition_id=origin_id,original=original,served=served)
    with patch('packages.application.web_vitrina_window_read_context.borrowed_operational_connection',return_value=conn):
        selected=retained()
        assert selected[covered][0]['book_version']=='exact'
        assert selected[covered][0]['ready_target']['as_of_date']==day
        assert today not in selected  # accounting_current authority was not retained
        changed=deepcopy(original);changed['days'][day]=digest('new-object')
        assert day not in retained(changed)
        changed=deepcopy(original);changed['catalog']=digest('other-context')
        assert not retained(changed)
        changed=deepcopy(original);changed['consumed']['dates'][day]='different-proof'
        assert day not in retained(changed)
        retained_cache['book']['exact']['verified'].remove('blob')
        expect_error(retained,ValueError,'book_proof_missing')
        retained_cache['book']['exact']['verified'].append('blob')
        conn.rollback();conn.execute('PRAGMA query_only=OFF')
        conn.execute('UPDATE sheet_vitrina_v1_ready_revisions SET revision=2 WHERE as_of_date=?',(day,))
        conn.commit();conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
        expect_error(retained,ValueError,'ready_changed')
    conn.close()
    book=sqlite3.connect(':memory:')
    book.execute('CREATE TABLE accounting_revisions(version TEXT,payload TEXT)')
    book.execute('INSERT INTO accounting_revisions VALUES(?,?)',('exact',json.dumps(index)))
    book.commit();book.execute('PRAGMA query_only=ON');book.execute('BEGIN')
    claim=selected[day][1]
    retained_authority={**authority,'fresh_token':'other-component-changed','retained_cost_binding':claim}
    pinned=SimpleNamespace(borrow_book=lambda path:book)
    archived_basis={nm:{**b,'presentation_digest':'blob'} for nm,b in basis.items()}
    with patch('packages.application.web_vitrina_window_read_context.active_window_read_context',return_value=pinned), \
         patch('packages.application.web_vitrina_group_blocks.accepted_cost_basis',return_value={day:archived_basis}) as load:
        loaded,receipt=load_repair_cost_basis(fake,day,catalog,cells,accepted_binding=binding,
            authority=retained_authority,deadline=time.monotonic()+1)
        assert loaded==archived_basis and receipt['authority_mode']=='retained_original_cost_binding'
        assert receipt['proof_claim']['old_day_proof']!=receipt['proof_claim']['captured_day_proof']
        assert receipt['checked_saved_costs']==2
        bad=deepcopy(cells);bad['SKU:12|our_wb_unit_cost_rub'][0]+=1
        loaded,receipt=load_repair_cost_basis(fake,day,catalog,bad,accepted_binding=binding,
            authority=retained_authority,deadline=time.monotonic()+1)
        assert not loaded and receipt['nm_ids']==[12]  # one matching WAC is insufficient
        load.reset_mock()
        bad=deepcopy(retained_authority);bad['retained_cost_binding']['book_index_digest']=digest('wrong')
        expect_error(lambda:load_repair_cost_basis(fake,day,catalog,cells,accepted_binding=binding,
            authority=bad,deadline=time.monotonic()+1),ValueError,'book_changed')
        assert not load.called
        wrong_blob={nm:{**b,'presentation_digest':'other'} for nm,b in basis.items()}
        load.return_value={day:wrong_blob}
        expect_error(lambda:load_repair_cost_basis(fake,day,catalog,cells,accepted_binding=binding,
            authority=retained_authority,deadline=time.monotonic()+1),ValueError,'blob_changed')
        load.reset_mock()
        loaded,receipt=load_repair_cost_basis(fake,day,catalog,cells,accepted_binding=binding,
            authority={**retained_authority,'fresh_epoch':'drift'},deadline=time.monotonic()+1)
        assert not loaded and not load.called
    book.close()
    # Exercise the actual immutable accounting decoder, not just a mocked
    # scalar. Doubling capital AND quantity preserves WAC but changes weights:
    # the original blob fingerprint must reject that body under its old ref.
    import zlib
    from packages.application.fbs_snapshot_cost import fingerprint
    from packages.application.fbs_accounting_runtime import SOURCE
    payload={'date':day,'version_id':'v','quality':'preliminary',
             'rows':{str(nm):{'shared_cost':{**{k:v for k,v in b.items() if k!='capital'},
                         'capital_rub':str(b['capital']),'quantity':str(b['quantity'])}}
                     for nm,b in basis.items()}}
    ref=fingerprint(payload)
    native_index={**index,'presentations':{day:ref},'effective_date':'2026-09-08'}
    native_projection={k:native_index[k] for k in index}
    native_binding={**binding,'source':SOURCE,'presentation_version':'v',
                    'quality':'preliminary','effective_date':'2026-09-08'}
    native_claim={**claim,'presentation_blob':ref,'book_index_digest':digest(native_projection),
                  'binding_digest':digest(native_binding)}
    native_authority={**retained_authority,'retained_cost_binding':native_claim}
    book=sqlite3.connect(':memory:')
    book.execute('CREATE TABLE accounting_revisions(version TEXT,payload TEXT)')
    book.execute('CREATE TABLE accounting_blobs(digest TEXT,payload BLOB)')
    book.execute('INSERT INTO accounting_revisions VALUES(?,?)',('exact',json.dumps(native_index)))
    book.execute('INSERT INTO accounting_blobs VALUES(?,?)',(ref,zlib.compress(json.dumps(payload).encode())))
    book.commit();book.execute('PRAGMA query_only=ON');book.execute('BEGIN')
    pinned=SimpleNamespace(borrow_book=lambda path:book)
    with patch('packages.application.web_vitrina_window_read_context.active_window_read_context',return_value=pinned):
        loaded,receipt=load_repair_cost_basis(fake,day,catalog,cells,accepted_binding=native_binding,
            authority=native_authority,deadline=time.monotonic()+1)
        assert receipt['checked_saved_costs']==2 and loaded[11]['presentation_digest']==ref
        assert loaded[11]['capital']==loaded[11]['capital_rub']=='144'
        assert loaded[12]['capital']==loaded[12]['capital_rub']=='900'
        repaired=prepare_group_repair_day(day,catalog,cells,cost_basis=loaded)
        assert not repaired['unresolved']
        assert math.isclose(repaired['patch']['GROUP:a|total_our_wb_unit_cost_rub'][0],1044/42)
        changed=deepcopy(payload)
        changed['rows']['11']['shared_cost']['quantity']=str(float(changed['rows']['11']['shared_cost']['quantity'])*2)
        changed['rows']['11']['shared_cost']['capital_rub']=str(float(changed['rows']['11']['shared_cost']['capital_rub'])*2)
        assert float(changed['rows']['11']['shared_cost']['capital_rub'])/float(changed['rows']['11']['shared_cost']['quantity'])==cells['SKU:11|our_wb_unit_cost_rub'][0]
        book.rollback();book.execute('PRAGMA query_only=OFF')
        book.execute('UPDATE accounting_blobs SET payload=? WHERE digest=?',
                     (zlib.compress(json.dumps(changed).encode()),ref))
        book.commit();book.execute('PRAGMA query_only=ON');book.execute('BEGIN')
        expect_error(lambda:load_repair_cost_basis(fake,day,catalog,cells,accepted_binding=native_binding,
            authority=native_authority,deadline=time.monotonic()+1),ValueError,'presentation_mismatch')
    book.close()
    print(json.dumps({'status':'PASS','seconds':round(time.monotonic()-started,3),
        'per_SKU_margin_and_perunit_parity':True,'negative_profit_preserved':True,
        'all_saved_cost_parity':True,'source_epoch_token_before_reads':True,'unproven_preserved':True,
        'GROUP_only_all16_others_unchanged':True,'formatted_percent_money':True,'undefined_not_source_missing':True,
        'partial_unknown_propagated':True,'immutable_prepared_callback':True,'buyout_untouched':True,'exact_native_compact_header_selection':True,
        'retained_original_not_latest_covered_binding':True,'retained_day_object_catalog_proof_links':True,
        'retained_READY_revision_index_blob_drift_refused':True,'retained_ALL_saved_cost_parity':True,
        'native_blob_changed_weights_same_WAC_refused':True,'native_capital_rub_normalized_GROUP_cost':True}))

if __name__=='__main__':main()
