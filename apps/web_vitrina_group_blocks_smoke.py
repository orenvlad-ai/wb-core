"""Owned GROUP aggregation, stable registry and explicit candidate cutover."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import json
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from apps.web_vitrina_history_rolling14_smoke import fixture, unit
from apps.web_vitrina_history_store_smoke import expect_error
from packages.application import web_vitrina_group_blocks as groups_module
from packages.application.web_vitrina_group_blocks import AcceptedGroupEvaluator, include_group_rows, accepted_source_statuses
from packages.application.web_vitrina_history_compiler import NativeDatedCompiler, digest
from packages.application.web_vitrina_history_store import HistoryStore, HistoryUnavailable
from packages.application.web_vitrina_history_http_read import read_history_page
from packages.application.web_vitrina_window_read_context import window_read_context
from packages.application.sheet_vitrina_v1_web_vitrina import _effective_web_vitrina_metrics, SheetVitrinaV1WebVitrinaBlock
from packages.application.sheet_vitrina_v1_live_plan import _MetricEvaluator, TemporalLiveSources
from packages.application.sheet_vitrina_v1_own_product_capital import own_stage_metric_key, own_stage_total_metric_key
from packages.application.vitrina_catalog import reporting_config, reporting_groups
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow


def row(kind, key, nm=None, values=None, presentation=None):
    return WebVitrinaContractRow(f'{kind}:{nm}|{key}' if kind == 'SKU' else f'TOTAL|{key}',
        1, kind, f'SKU:{nm}' if kind == 'SKU' else 'TOTAL', str(nm or 'Итого'), key, key,
        '', 'Экономика', None, nm, 'number', values or {}, presentation or {})


def semantic_checks(runtime):
    days = ['2026-04-20', '2026-09-14', '2026-09-20']
    state = runtime.load_current_state()
    metrics = {m.metric_key: m for m in _effective_web_vitrina_metrics(state.metrics_v2)}
    # Exercise generated TOTAL-specific branches in the real registry, not a
    # clone with scope=GROUP. Each outcome is numeric or honestly unavailable.
    totals = [row('TOTAL', m.metric_key, values={d: 90000 for d in days}) for m in metrics.values() if m.scope == 'TOTAL' and m.enabled and m.show_in_data]
    actual = SheetVitrinaV1WebVitrinaBlock(runtime=runtime,now_factory=lambda:datetime(2026,4,20,12,tzinfo=timezone.utc)).build(page_route='/',read_route='/read',date_from='2026-04-20',date_to='2026-04-20')
    for observed in actual.rows:
        if observed.scope_kind == 'TOTAL' and not any(r.metric_key == observed.metric_key for r in totals):
            totals.append(replace(observed,values_by_date={d:90000 for d in days}))
    for key in (own_stage_total_metric_key('WB','confirmed_share_pct'), own_stage_total_metric_key('WB','cost_coverage_pct'), 'total_inventory_wb_total_qty_v1'):
        if key not in metrics:
            metrics[key] = MetricV2Item(key,True,'TOTAL',key,'metric',key,True,'percent',1,'Товарный капитал')
        if not any(r.metric_key == key for r in totals):
            totals.append(row('TOTAL',key,values={d:90000 for d in days}))
    config = [ConfigV2Item(11, True, 'First', 'a', 1), ConfigV2Item(12, True, 'Hidden', 'a', 2), ConfigV2Item(13, True, 'Other', 'b', 3)]
    groups = [{'group_key': k, 'label': label, 'display_order': n, 'is_active': True} for n, (k,label) in enumerate([('b','B'),('a','A'),('empty','Empty')])]
    values = {
        11: {'orderCount': 10, 'orderSum': 1000, 'view_count': 100, 'proxy_profit_4_rub': 200,
             'ads_sum': 10, 'our_wb_unit_cost_rub': 10, 'inventory_wb_total_qty_v1': 7,
             'inventory_fbs_total_qty_v1': 3, 'stock_total': 10},
        12: {'orderCount': 30, 'orderSum': 9000, 'view_count': 300, 'proxy_profit_4_rub': 300,
             'ads_sum': 20, 'our_wb_unit_cost_rub': 30, 'inventory_wb_total_qty_v1': 17,
             'inventory_fbs_total_qty_v1': 2, 'stock_total': 19},
        13: {'orderCount': 2, 'orderSum': 1600, 'view_count': 50, 'proxy_profit_4_rub': 40,
             'ads_sum': 3, 'our_wb_unit_cost_rub': 90, 'inventory_wb_total_qty_v1': 70,
             'inventory_fbs_total_qty_v1': 8, 'stock_total': 78},
    }
    for nm, qty, capital, confirmed in [(11, 10, 100, .5), (12, 30, 900, 1), (13, 2, 180, 0)]:
        values[nm].update({own_stage_metric_key('WB','qty'): qty,
            own_stage_metric_key('WB','capital_rub'): capital,
            own_stage_metric_key('WB','confirmed_share_pct'): confirmed,
            own_stage_metric_key('WB','cost_coverage_pct'): 1})
    from packages.application.sheet_vitrina_v1_card_rating import SKU_METRIC_KEY, TOTAL_METRIC_KEY
    from packages.application.sheet_vitrina_v1_authenticated_buyer import EFFECTIVE_DISCOUNT_METRIC_KEY, AVG_EFFECTIVE_DISCOUNT_METRIC_KEY
    for nm, rating, discount in [(11,4,.1),(12,None,.3),(13,2,.5)]:
        values[nm][SKU_METRIC_KEY] = rating
        values[nm][EFFECTIVE_DISCOUNT_METRIC_KEY] = discount
    sku = [row('SKU', key, nm, {d: value for d in days}) for nm, data in values.items() for key,value in data.items()]
    canonical = {nm: {'our_wb_unit_cost_rub': data['our_wb_unit_cost_rub'], 'stock_qty': data['stock_total'],
        'cost_covered_qty': data['stock_total'], 'confirmed_qty': data['stock_total']} for nm,data in values.items()}
    params = SimpleNamespace(buyout_rate=.8)
    mature = {nm: {'2026-09-14': SimpleNamespace(buyout_percent=percent,order_count=count)} for nm,percent,count in [(11,.5,10),(12,.9,30),(13,.2,2)]}
    # These pure native readers have their own parity tests; owned accepted
    # observations isolate the group aggregation/late-injection boundary.
    with patch.object(groups_module, 'load_canonical_wb_cost_lookup', return_value=canonical), \
         patch.object(groups_module.OwnProductCapitalBlock, '_load_functional_daily_metric_lookup', return_value=None), \
         patch.object(groups_module.OwnProductCapitalBlock, '_load_canonical_daily_metric_lookup', return_value={}), \
         patch.object(groups_module, 'load_buyout_percent_snapshot_metrics', return_value=mature):
        result = include_group_rows(totals+sku, groups=groups, config=config, metrics=metrics,
            formulas={f.formula_id:f for f in state.formulas_v2}, dates=days, runtime=runtime,
            today='2026-09-20', parameters3=lambda d: params, parameters4=lambda d: params)
    projected = {r.row_id:r for r in result if r.scope_kind == 'GROUP'}
    assert len(projected) == len(totals)*2 and not any(k.startswith('GROUP:empty') for k in projected)
    for key in projected:
        assert set(projected[key].values_by_date) == set(days)
        assert all(projected[key].presentation_by_date[d]['source'] == 'native_reporting_group_v1' for d in days)
    def val(g,k,d='2026-09-20'): return projected[f'GROUP:{g}|{k}'].values_by_date[d]
    assert val('a','total_orderCount') == 40 and val('b','total_orderCount') == 2
    assert val('a','weighted_price_seller_discounted') == 250 and val('b','weighted_price_seller_discounted') == 800
    assert val('a', own_stage_total_metric_key('WB','unit_cost_rub')) == 25
    assert val('a', own_stage_total_metric_key('WB','confirmed_share_pct')) == .875, val('a', own_stage_total_metric_key('WB','confirmed_share_pct'))
    assert val('a', own_stage_total_metric_key('WB','cost_coverage_pct')) == 1
    assert val('a','fin_storage_fee_total') == '' and projected['GROUP:a|fin_storage_fee_total'].presentation_by_date[days[0]]['quality_state'] == 'unallocated'
    assert val('a',TOTAL_METRIC_KEY) == 4 and val('b',TOTAL_METRIC_KEY) == 2
    assert abs(val('a',AVG_EFFECTIVE_DISCOUNT_METRIC_KEY)-.2) < 1e-12
    assert val('a','total_inventory_wb_total_qty_v1') == 24
    # Mature D-6 uses native order weights; current immature day stays absent.
    from packages.application.sheet_vitrina_v1_buyout_percent import BUYOUT_PERCENT_METRIC_KEY
    assert abs(val('a',BUYOUT_PERCENT_METRIC_KEY,'2026-09-14')-.8) < 1e-12
    assert val('a',BUYOUT_PERCENT_METRIC_KEY) == ''
    # Native WB+FF evidence includes the real FF facility operands, not a
    # WB-only average or copied global cost. Proxy4 uses final group profits
    # and the same expected-revenue/quantity denominators as native TOTAL.
    from apps.inventory_cost_blend_smoke import _product, _location
    physical = {11:_product(wb=('10','100'),ff=('2','40',[_location('ff_a','FBS','2','40')])),
                12:_product(wb=('30','900')),13:_product(wb=('2','180'))}
    with patch.object(groups_module, 'load_canonical_wb_cost_lookup', return_value=canonical), \
         patch.object(groups_module.OwnProductCapitalBlock, '_load_functional_daily_metric_lookup', return_value=physical), \
         patch.object(groups_module.OwnProductCapitalBlock, 'load_daily_metric_lookup', return_value=physical), \
         patch.object(groups_module, 'load_buyout_percent_snapshot_metrics', return_value=mature):
        native = include_group_rows(totals+sku, groups=groups, config=config, metrics=metrics,
            formulas={f.formula_id:f for f in state.formulas_v2}, dates=days, runtime=runtime,
            today='2026-09-20', parameters3=lambda d: params, parameters4=lambda d: params)
    native = {r.row_id:r for r in native if r.scope_kind=='GROUP'}
    def native_value(group,key):return native[f'GROUP:{group}|{key}'].values_by_date['2026-09-20']
    assert abs(native_value('a','total_our_wb_unit_cost_rub')-1040/42)<1e-12
    assert native_value('b','total_our_wb_unit_cost_rub')==90
    assert native_value('a','total_proxy_profit_4_rub')==500
    assert abs(native_value('a','proxy_margin_4_pct_total')-500/8000)<1e-12
    assert abs(native_value('a','proxy_margin_per_unit_rub_total')-500/32)<1e-12
    # Positive uncovered FF invalidates WAC instead of reusing WB-only cost.
    physical[11]['own_capital_FF_cost_covered_qty']=0
    from packages.application.inventory_cost_blend import build_inventory_cost_blend_lookup
    proof = build_inventory_cost_blend_lookup(as_of_date='2026-09-20',wb_compat_lookup=canonical,product_capital_lookup=physical)
    assert proof[11]['our_wb_unit_cost_rub'] is None
    # Accepted advertising incompleteness is dated, not inferred from a blank
    # operand: native TOTAL aligns both ratio/formula member sets only then.
    ratio_key = 'group_ads_ratio_test'
    ads_metrics = {**metrics, ratio_key: MetricV2Item(ratio_key, True, 'TOTAL', ratio_key,
        'ratio', 'ads_sum/orderSum', True, 'percent', 1, 'Экономика')}
    asymmetric = [replace(r, values_by_date={d: (10 if r.nm_id == 11 else None)
        if r.metric_key == 'ads_sum' else (100 if r.nm_id == 11 else 900)
        for d in days}) if r.nm_id in (11,12) and r.metric_key in {'ads_sum','orderSum'} else r
        for r in sku]
    source_plan = SimpleNamespace(temporal_slots=[SimpleNamespace(slot_key='accepted', column_date=days[-1])],
        sheets=[SimpleNamespace(sheet_name='STATUS',header=['source_key','kind'],
            rows=[['ads_compact[accepted]','incomplete'], ['ads_compact[unrelated]','incomplete']])])
    statuses = accepted_source_statuses(source_plan, column_date=days[-1])
    assert statuses == [{'source_key':'ads_compact','kind':'incomplete','temporal_slot':days[-1]}]
    assert accepted_source_statuses(source_plan,column_date=days[0]) == []
    assert accepted_source_statuses(source_plan,column_date=days[-1],requested_date='2026-09-21')[0]['temporal_slot']=='2026-09-21'
    with patch.object(groups_module, 'load_canonical_wb_cost_lookup', return_value=canonical), \
         patch.object(groups_module.OwnProductCapitalBlock, '_load_functional_daily_metric_lookup', return_value=None), \
         patch.object(groups_module.OwnProductCapitalBlock, '_load_canonical_daily_metric_lookup', return_value={}), \
         patch.object(groups_module, 'load_buyout_percent_snapshot_metrics', return_value=mature):
        aligned = include_group_rows(totals+[row('TOTAL',ratio_key)]+asymmetric,
            groups=groups,config=config,metrics=ads_metrics,
            formulas={f.formula_id:f for f in state.formulas_v2},dates=days,runtime=runtime,
            today=days[-1],parameters3=lambda d:params,parameters4=lambda d:params,
            source_statuses=statuses)
    aligned = {r.row_id:r for r in aligned if r.scope_kind=='GROUP'}
    for key in (ratio_key,'ads_drr_total'):
        assert abs(aligned[f'GROUP:a|{key}'].values_by_date[days[-1]]-.1)<1e-12
        assert abs(aligned[f'GROUP:a|{key}'].values_by_date[days[0]]-.01)<1e-12
        assert aligned[f'GROUP:a|{key}'].presentation_by_date[days[-1]]['quality_state']=='partial'
    # The native capital-return fail-closed branch must receive the same status;
    # cached confirmed numerator/capital isolate this source-completeness rule.
    from packages.application.sheet_vitrina_v1_live_plan import (
        OWN_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY, OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
        OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY)
    ads_metrics[OWN_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY]=MetricV2Item(
        OWN_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY, True, 'TOTAL', 'Capital return', 'metric',
        OWN_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY, True, 'percent', 1, 'Товарный капитал')
    for day, expected in [(days[0],.5),(days[-1],None)]:
        sources=TemporalLiveSources([], [SimpleNamespace(**x) for x in statuses], {}, {})
        evaluator=AcceptedGroupEvaluator(rows=asymmetric,enabled_config=config[:2],
            metrics_by_key=ads_metrics,formulas_by_id={},live_sources=sources)
        native=_MetricEvaluator(enabled_config=config[:2],metrics_by_key=ads_metrics,
            formulas_by_id={},live_sources=sources)
        for e in (evaluator,native):
            e.total_cache[(day,OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY)]=100
            e.total_cache[(day,OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY)]=200
            assert e.resolve_total(OWN_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY,day)==expected
    # Missing positive inventory is never silently counted as zero.
    broken = [replace(r, values_by_date={**r.values_by_date,'2026-09-20':None}) if r.nm_id == 12 and r.metric_key == own_stage_metric_key('WB','confirmed_share_pct') else r for r in sku]
    evaluator = AcceptedGroupEvaluator(rows=broken,enabled_config=config[:2],metrics_by_key=metrics,
        formulas_by_id={}, live_sources=TemporalLiveSources([],[],{'2026-09-20':SimpleNamespace(column_date='2026-09-20',own_product_capital_lookup={11:{own_stage_metric_key('WB','qty'):10,own_stage_metric_key('WB','confirmed_qty'):5},12:{own_stage_metric_key('WB','qty'):30,own_stage_metric_key('WB','confirmed_qty'):None}})},{}))
    assert evaluator.resolve_total(own_stage_total_metric_key('WB','confirmed_share_pct'),'2026-09-20') is None
    return {'total_metric_outcomes':len(totals),'group_metric_outcomes':len(projected),'weighted_cost_share_price':True,'account_storage_unallocated':True,'D6_buyout':True,'missing_not_zero':True,'native_WB_FF_Proxy4':True,'partial_ads_dated_native_alignment':True}


def migration_checks():
    with TemporaryDirectory(prefix='group-candidate-') as tmp:
        old, new = HistoryStore(Path(tmp)/'served'), HistoryStore(Path(tmp)/'candidate')
        catalog = fixture()
        days = ['2026-04-18','2026-04-19','2026-04-20']
        vector = {'coverage':'complete_frozen_native_v1','epoch':'old','dates':{d:digest(d) for d in days}}
        before = old.update(vector=vector,catalog=catalog,compile_day=lambda d:unit(catalog,d),revalidate=lambda:vector)
        fresh = deepcopy(catalog);fresh['context_epoch']='group-native'
        fresh['group_identity_contract']='current_nomenclature_group_key_v1'
        fresh['reporting_groups']=[{'group_key':'a','label':'A','display_order':1,'is_active':True}]
        for kind,rid in [('group','GROUP:a|stock')]:
            r=deepcopy(fresh['rows']['TOTAL|stock']);r.update(row_id=rid,row_kind=kind,group_id='group:a');fresh['rows'][rid]=r;fresh['order'].append(rid)
        fresh['rows']['SKU:7|stock']['group_id']='group:a'
        vector={**vector,'epoch':'group-reviewed'}
        first=new.update(vector=vector,catalog=fresh,compile_day=lambda d:unit(fresh,d),revalidate=lambda:vector,max_recomputes=1)
        assert first['status']=='pending' and old._current()['current']==before['edition_id']
        new.update(vector=vector,catalog=fresh,compile_day=lambda d:unit(fresh,d),revalidate=lambda:vector,max_recomputes=1)
        after=new.update(vector=vector,catalog=fresh,compile_day=lambda d:unit(fresh,d),revalidate=lambda:vector)
        assert after['status']=='published'
        args={'expected_current':before['edition_id'],'expected_candidate':after['edition_id']}
        preview=old.group_candidate_preview(new,**args)
        assert old._current()['current']==before['edition_id']
        expect_error(lambda:old.publish_group_candidate(new,**args,preview_token='wrong',revalidate=lambda:vector),HistoryUnavailable,'preview_mismatch')
        result=old.publish_group_candidate(new,**args,preview_token=preview['preview_token'],revalidate=lambda:{**vector,'epoch':'superseded'})
        assert result['status']=='superseded' and old._current()['current']==before['edition_id']
        clock=[0.0]; revalidations=[]
        def final_check():
            revalidations.append(True)
            if len(revalidations)==2:clock[0]=10.0
            return vector
        with patch('packages.application.web_vitrina_history_store.time.monotonic',side_effect=lambda:clock[0]):
            late=old.publish_group_candidate(new,**args,preview_token=preview['preview_token'],
                revalidate=final_check,deadline_monotonic=5.0)
        assert late['status']=='pending' and len(revalidations)==2
        assert old._current()['current']==before['edition_id']
        assert all((old.root/'objects'/(key+'.sqlite3')).exists() for key in new.edition()['days'].values())
        result=old.publish_group_candidate(new,**args,preview_token=preview['preview_token'],revalidate=lambda:vector)
        assert result['status']=='published' and old._current()['previous']==before['edition_id']
        for d in days: assert old.edition()['days'][d]==new.edition()['days'][d]
        assert old.read(date_from=days[0],date_to=days[-1],edition_id=before['edition_id'],scope='total')['rows'][0]['cells'][days[0]]==unit(catalog,days[0])['cells']['TOTAL|stock']
        meta=read_history_page(old,date_from=days[0],date_to=days[-1],scope='catalog')
        assert meta['table_surface']['rows']==[] and meta['history_snapshot']['reporting_groups'][0]['total_available']
        group=read_history_page(old,date_from=days[0],date_to=days[-1],scope='group',group_id='group:a',edition_id=result['edition_id'])
        sku=read_history_page(old,date_from=days[0],date_to=days[-1],scope='sku',group_id='group:a',edition_id=result['edition_id'],limit=1)
        assert {r['row_kind'] for r in group['table_surface']['rows']}=={'group'}
        assert {r['row_kind'] for r in sku['table_surface']['rows']}=={'sku'}
        assert group['history_snapshot']['edition_id']==sku['history_snapshot']['edition_id']==result['edition_id']
        # After the one-off cutover, new dates use the same ordinary rolling
        # engine; full-history migration is never the default update mode.
        newer={**vector,'dates':{**vector['dates'],'2026-04-21':digest('new day')}}
        calls=[]
        def compile_day(d):calls.append(d);return unit(fresh,d)
        ordinary=old.update(vector=newer,catalog=fresh,compile_day=compile_day,
            revalidate=lambda:newer,business_date='2026-04-21')
        assert ordinary['status']=='published' and calls==['2026-04-21']
        assert old.edition()['days']['2026-04-18']==new.edition()['days']['2026-04-18']
        expect_error(lambda:old.group_candidate_preview(new,**args),HistoryUnavailable,'superseded')
        return {'isolated_resumable_candidate':True,'preview_CAS_fence':True,'post_revalidation_deadline_retains_CURRENT':True,'old_all16_pinned':True,'catalog_zero_cells':True,'group_no_SKU':True,'sameedition_SKU':True,'ordinary_rolling14_after_cutover':True}



def membership_checks():
    # Synthetic shape of the accepted 94-card contract: retained hidden are
    # reporting members; No Frame keys and Other do not use stale Balance labels.
    with TemporaryDirectory(prefix='group-membership-') as tmp:
        db=Path(tmp)/'source.sqlite3'
        counts=[('clean',13),('anti_spy',20),('matte',20),('no_frame_clean',10),('no_frame_anti_spy',12),('no_frame_matte',12),('other',7)]
        configured=[]
        with closing(sqlite3.connect(db)) as conn,conn:
            conn.execute('CREATE TABLE sheet_vitrina_v1_nomenclature_items(item_id TEXT,nm_id INTEGER,is_active INTEGER,is_hidden INTEGER,updated_at TEXT,product_type TEXT,nomenclature_name TEXT)')
            index=0
            for key,count in counts:
                for _ in range(count):
                    index+=1
                    conn.execute('INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,?,?,?,?,?)',(str(index),index,index<=73,index>73,'2026-04-20',key,str(index)))
                    configured.append(ConfigV2Item(index,True,str(index),'Stale label',index))
        fixed,scope=reporting_config(db,configured,canonical_groups=True)
        assert len(fixed)==94 and scope['main_count']==73 and scope['retained_hidden_count']==21
        assert {key:sum(x.group==key for x in fixed) for key,_ in counts}==dict(counts)
        with closing(sqlite3.connect(db)) as conn,conn:
            conn.execute('INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,?,?,?,?,?)',('duplicate',1,1,0,'2026-04-20','clean','Duplicate'))
        expect_error(lambda:reporting_config(db,configured,canonical_groups=True),ValueError)
    return {'unique_members':94,'visible':73,'retained_hidden':21,'NoFrame':34,'Other':7,'duplicate_refused':True}


def cli_checks():
    from contextlib import redirect_stdout
    from io import StringIO
    from apps import web_vitrina_history_candidate_build as command
    from packages.application.business_data_procedure_admission import initialize_admission
    from packages.application.business_data_write_barrier import STATE_FILENAME,SCHEMA_VERSION
    repo=Path(__file__).resolve().parents[1]
    contract_path=repo/'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json'
    contract=json.loads(contract_path.read_text())
    with patch.object(command.os.path,'ismount',return_value=True),patch.object(command.subprocess,'check_output',return_value=contract['mount']),patch.object(command.os,'statvfs',return_value=SimpleNamespace(f_bavail=contract['reserve_bytes'],f_frsize=1)):
        command.runtime_storage_admission(Path(contract['group_migration_root']),contract_path,contract['formula_epoch'],group_migration=True)
        expect_error(lambda:command.runtime_storage_admission(Path(contract['candidate_root']),contract_path,contract['formula_epoch'],group_migration=True),ValueError,'storage_path')
    with TemporaryDirectory(prefix='group-CLI-') as tmp:
        runtime=Path(tmp)/'runtime';runtime.mkdir();initialize_admission(runtime)
        (runtime/'.web-vitrina-finished-builder.lock').write_bytes(b'owned')
        argv=['candidate','--runtime-dir',str(runtime),'--candidate-root',str(Path(tmp)/'candidate'),'--formula-epoch','owned','--date-from','2026-04-18','--date-to','2026-04-20','--manual','--full-history-group-migration','--runtime-contract',str(contract_path)]
        with patch.object(sys,'argv',argv),patch.object(command,'bounded_worker') as worker,redirect_stdout(StringIO()):
            expect_error(command.main,ValueError,'explicit held')
            assert not worker.called
        window='owned-group-migration-window'
        (runtime/STATE_FILENAME).write_text(json.dumps({'schema_version':SCHEMA_VERSION,'phase':'held','window_id':window,'window_kind':'maintenance_pause','hold_confirmed':True}))
        (runtime/STATE_FILENAME).chmod(0o600)
        argv+=['--maintenance-window-id',window]
        def worker(child,seconds):
            assert '--full-history-group-migration' in child and child[child.index('--maintenance-window-id')+1]==window
            assert seconds==180 and child[-1]=='--worker'
            return {'status':'owned_fixture_only'}
        with patch.object(sys,'argv',argv),patch.object(command,'admission',return_value='idle'),patch.object(command,'runtime_storage_admission'),patch.object(command,'bounded_worker',side_effect=worker) as spawned,redirect_stdout(StringIO()):
            assert command.main()==0 and spawned.called
        with patch.object(sys,'argv',[*argv,'--worker']),patch.object(command,'runtime_storage_admission'),patch.object(command,'StoreRegistry'),patch.object(command,'RegistryUploadDbBackedRuntime'),patch.object(command,'LiveNativeAdapter'),patch.object(command,'HistoryStore'),patch.object(command,'update_live_history',return_value={'status':'owned_fixture_only'}) as update,redirect_stdout(StringIO()):
            assert command.main()==0
            assert update.call_args.kwargs['rolling14'] is False and update.call_args.kwargs['group_blocks'] is True
        with patch.object(sys,'argv',[a for a in [*argv,'--worker'] if a!='--full-history-group-migration']),patch.object(command,'runtime_storage_admission'),patch.object(command,'StoreRegistry'),patch.object(command,'RegistryUploadDbBackedRuntime'),patch.object(command,'LiveNativeAdapter'),patch.object(command,'HistoryStore'),patch.object(command,'update_live_history',return_value={'status':'owned_fixture_only'}) as update,redirect_stdout(StringIO()):
            assert command.main()==0 and update.call_args.kwargs['rolling14'] is True
    return {'migration_only_exact_held_manual':True,'separate_root_guard':True,'parent180_worker31_defaults':True,'ordinary_rolling14':True}

def main():
    started=time.monotonic()
    server=LocalWebVitrinaFixtureServer(with_ready_snapshot=True,ready_days=1)
    with server:
        runtime=server.entrypoint.runtime
        block=server.entrypoint.supplier_shipments_block
        items=block.list_nomenclature()['items']
        block.update_sku_group('clean',{'display_order':7,'label':'Clean renamed'})
        block.update_sku_group('matte',{'display_order':2})
        before=hashlib.sha256(runtime.db_path.read_bytes()).hexdigest()
        with window_read_context(runtime.db_path,runtime_dir=runtime.runtime_dir):
            registry=reporting_groups(__import__('packages.application.web_vitrina_window_read_context',fromlist=['borrowed_operational_connection']).borrowed_operational_connection(runtime.db_path))
            assert next(g for g in registry if g['group_key']=='clean')['display_order']==7
            assert next(g for g in registry if g['group_key']=='clean')['label']=='Clean renamed'
            # The period composer must preserve the covering snapshot's actual
            # STATUS, remapping its slot to the requested day without unrelated slots.
            import packages.application.sheet_vitrina_v1_web_vitrina as web_module
            ready=runtime.load_sheet_vitrina_ready_snapshot(as_of_date='2026-04-20')
            slots=[SimpleNamespace(slot_key='accepted',column_date='2026-04-20'),
                   SimpleNamespace(slot_key='other',column_date='2026-04-19')]
            status=SimpleNamespace(sheet_name='STATUS',header=['source_key','kind'],
                rows=[['ads_compact[accepted]','incomplete'],['ads_compact[other]','success']])
            accepted=replace(ready,temporal_slots=slots,
                sheets=[x for x in ready.sheets if x.sheet_name!='STATUS']+[status])
            binding=web_module._PeriodDateBinding('2026-04-21','2026-04-20','2026-04-20',covering_snapshot=accepted)
            with patch.object(web_module,'_resolve_period_date_bindings',return_value=[binding]):
                period,_=web_module._build_period_snapshot(runtime=runtime,date_from='2026-04-21',
                    date_to='2026-04-21',default_visible_snapshot=ready)
            assert period.metadata['group_source_statuses']==[
                {'source_key':'ads_compact','kind':'incomplete','temporal_slot':'2026-04-21'}]
            semantic=semantic_checks(runtime)
            normal=NativeDatedCompiler(runtime,datetime(2026,4,20,12,tzinfo=timezone.utc),'2026-04-20','2026-04-20')
            grouped=NativeDatedCompiler(runtime,datetime(2026,4,20,12,tzinfo=timezone.utc),'2026-04-20','2026-04-20',group_blocks=True)
            old,new=normal.compile('2026-04-20'),grouped.compile('2026-04-20')
            for rid,cell in old['cells'].items(): assert new['cells'][rid]==cell,(rid,new['cells'][rid],cell)
            assert any(rid.startswith('GROUP:') for rid in new['cells'])
            for rid,r in grouped.catalog['rows'].items():
                if r['row_kind']=='group' and rid.startswith('GROUP:clean|'):
                    assert r['group_id']=='group:clean' and r['values']['group'][0]=='Clean renamed'
        assert before==hashlib.sha256(runtime.db_path.read_bytes()).hexdigest()
    print(json.dumps({'status':'PASS','semantics':semantic,'native_compiler_all16_unchanged':True,'stable_label_identity_order':True,'source_RO':True,'migration':migration_checks(),'membership':membership_checks(),'CLI':cli_checks(),'seconds':round(time.monotonic()-started,3)}))

if __name__=='__main__':main()
