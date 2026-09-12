"""Synthetic publication CAS, preservation, missing/zero and exact rollback."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.web_source_publication import WebSourcePublicationAdapter, canonical, digest, project, validate_observations, non_target_digest
from apps.production_apply_launcher import execute


def observations():
    funnel_raw=[{'nmId':1,'name':'one','vendorCode':'one','viewCount':{'current':100},'openCard':{'current':10},'viewToOpen':{'current':10}},
        {'nmId':2,'name':'two','vendorCode':'two','viewCount':{'current':0},'openCard':{'current':0},'viewToOpen':{'current':0}}]
    search_raw=[{'nmId':1,'views':{'current':100},'ctr':{'current':10},'orders':{'current':1},'avgPosition':{'current':2}},
        {'nmId':2,'views':{'current':300},'ctr':{'current':30},'orders':{'current':2},'avgPosition':{'current':3}}]
    from packages.adapters.seller_portal_web_source_collector import _funnel_items,_search_items
    common={'contract':'seller_portal_dated_observation_v1','snapshot_date':'2026-09-11','source_fetched_at':'2026-09-12T22:00:00Z',
        'supplier_identity_sha256':'fixture','completeness':'complete','request_period':{'start':'2026-09-11','end':'2026-09-11'}}
    return [{**common,'source_key':'seller_funnel_snapshot','pages':2,'items':_funnel_items(funnel_raw),
        'detail_pages':[{'offset':0,'limit':50,'response':{'data':funnel_raw}}],
        'raw_report':{'error':False,'additionalErrors':{'errors':None},'data':{'groups':[{'itemsGroup':funnel_raw,'viewCount':{'current':100},'openCard':{'current':10}}]}}},
        {**common,'source_key':'web_source_snapshot','pages':1,'items':_search_items(search_raw),'detail_pages':[],
        'raw_report':{'error':False,'additionalErrors':{'errors':None},'data':{'groups':[{'items':search_raw}],'commonInfo':{'totalProducts':2}}}}]


def plan():
    rows=[['price','TOTAL|weighted_price_seller_discounted',524.434744],['stock','SKU:1|stock_total',900]]
    for n in [1,2,3]:
        rows.extend([[m,f'SKU:{n}|{m}',7] for m in ['view_count','open_card_count','ctr','views_current','ctr_current']])
    rows.extend([[m,'TOTAL|'+m,9] for m in ['total_view_count','total_open_card_count','total_views_current','avg_ctr_current']])
    return {'date_columns':['2026-09-11'],'temporal_slots':[{'slot_key':'yesterday_closed','column_date':'2026-09-11'}],
        'metadata':{'unrelated':'retained'},'sheets':[{'sheet_name':'DATA_VITRINA','header':['name','id','2026-09-11'],'rows':rows},
        {'sheet_name':'STATUS','rows':[[s+'[yesterday_closed]','success','','','','','',3,0,'','stale'] for s in ['seller_funnel_snapshot','web_source_snapshot']]}]}


def main():
    obs=observations();request={'dates':['2026-09-11'],'nm_ids':[1,2,3],'observations':obs,'source_sha256':digest(obs),'supplier_identity_sha256':'fixture','phase':'publication'}
    validate_observations(request)
    old=plan();new,changes=project(old,obs,[1,2,3],'op-fixture')
    assert non_target_digest(old,obs)==non_target_digest(new,obs)
    rows={r[1]:r for r in new['sheets'][0]['rows']}
    assert rows['SKU:2|view_count'][2]==0 and rows['SKU:3|view_count'][2]==''
    assert rows['TOTAL|avg_ctr_current'][2]==0.25
    assert rows['TOTAL|weighted_price_seller_discounted'][2]==524.434744 and rows['SKU:1|stock_total'][2]==900
    invalid=deepcopy(request);invalid['observations'][0]['items'][0]['view_count']=999;invalid['source_sha256']=digest(invalid['observations'])
    try:validate_observations(invalid)
    except ValueError:pass
    else:raise AssertionError('changed source counter passed provenance check')
    with TemporaryDirectory() as tmp:
        runtime=Path(tmp);db=runtime/'operational.sqlite3'
        with sqlite3.connect(db) as conn:
            conn.executescript('''CREATE TABLE registry_upload_current_state(slot INTEGER,bundle_version TEXT);
                CREATE TABLE registry_upload_config_v2(bundle_version TEXT,nm_id INTEGER);
                CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version TEXT,as_of_date TEXT,plan_json TEXT,PRIMARY KEY(bundle_version,as_of_date));
                CREATE TABLE temporal_source_slot_snapshots(source_key TEXT,snapshot_date TEXT,snapshot_role TEXT,captured_at TEXT,payload_json TEXT,PRIMARY KEY(source_key,snapshot_date,snapshot_role));
                CREATE TABLE temporal_source_closure_state(source_key TEXT,target_date TEXT,slot_kind TEXT,state TEXT,attempt_count INTEGER,next_retry_at TEXT,last_reason TEXT,last_attempt_at TEXT,last_success_at TEXT,accepted_at TEXT,PRIMARY KEY(source_key,target_date,slot_kind));''')
            conn.execute("INSERT INTO registry_upload_current_state VALUES(1,'bundle')")
            conn.executemany("INSERT INTO registry_upload_config_v2 VALUES('bundle',?)",[(1,),(2,),(3,)])
            conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES('bundle','2026-09-11',?)",(canonical(old),))
        class LocalAdapter(WebSourcePublicationAdapter):
            def target(self,request):return runtime,db
            def _materialized(self,request):validate_observations(request)
        adapter=LocalAdapter();adapters={'fixture':adapter};request['runtime_dir']=str(runtime)
        preview=execute(action='preview',adapter_name='fixture',operation_id='operation-fixture-1',request=request,adapters=adapters)
        receipt=execute(action='apply',adapter_name='fixture',operation_id='operation-fixture-1',request=request,
            expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters=adapters)
        assert receipt['state']=='applied'
        repeated=execute(action='apply',adapter_name='fixture',operation_id='operation-fixture-1',request=request,
            expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters=adapters)
        assert repeated['state']=='applied' and repeated.get('submit') is None
        assert adapter.rollback(request,'operation-fixture-1')['state']=='restored'
        with sqlite3.connect(db) as conn:
            assert json.loads(conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0])==old
            assert conn.execute('SELECT count(*) FROM temporal_source_slot_snapshots').fetchone()[0]==0
        preview=adapter.preview(request,'operation-fixture-2')
        with sqlite3.connect(db) as conn:
            drift=plan();drift['metadata']['unrelated']='concurrent-change'
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(canonical(drift),))
        try:adapter.apply(request,'operation-fixture-2',preview)
        except ValueError as exc:assert str(exc)=='candidate-cas-drift'
        else:raise AssertionError('concurrent ready change accepted')
        assert not (runtime/'evidence'/'operation-fixture-2.before.json').exists()
    print('web source publication smoke: source binding, target cells, missing/zero, price/non-target preservation, CAS, one-submit readback and rollback passed')


if __name__=='__main__':main()
