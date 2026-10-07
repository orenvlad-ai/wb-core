"""Synthetic publication CAS, preservation, missing/zero and exact rollback."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.web_vitrina_web_source_publication import WebSourcePublicationAdapter, canonical, digest, project, validate_observations, non_target_digest, source_date_pairs
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
        'raw_report':{'error':False,'additionalErrors':{'errors':None},'data':{'groups':[{'items':search_raw,
            'metrics':{'views':{'current':400},'orders':{'current':3}}}],'commonInfo':{'totalProducts':2}}}}]


def plan():
    rows=[['price','TOTAL|weighted_price_seller_discounted',524.434744],['stock','SKU:1|stock_total',900]]
    for n in [1,2,3]:
        rows.extend([[m,f'SKU:{n}|{m}',7] for m in ['view_count','open_card_count','ctr','views_current','ctr_current']])
    rows.extend([[m,'TOTAL|'+m,9] for m in ['total_view_count','total_open_card_count','total_views_current','avg_ctr_current']])
    return {'date_columns':['2026-09-11'],'temporal_slots':[{'slot_key':'yesterday_closed','column_date':'2026-09-11'}],
        'metadata':{'unrelated':'retained'},'sheets':[{'sheet_name':'DATA_VITRINA','header':['name','id','2026-09-11'],'rows':rows},
        {'sheet_name':'STATUS','rows':[[s+'[yesterday_closed]','success','','','','','',3,0,'','stale'] for s in ['seller_funnel_snapshot','web_source_snapshot']]}]}


def source_recovery_checks(request):
    """Exercise the real launcher/adapter with transactional stores and injected IO failures."""
    from contextlib import contextmanager
    from datetime import datetime,timezone
    from apps.web_vitrina_web_source_publication import PG_TABLES
    from apps.production_apply_launcher import ApplyError
    request={**request,'phase':'source'}
    for failure in ('raw-after-commit','handoff-before-commit','none'):
        with TemporaryDirectory() as tmp:
            runtime=Path(tmp);db=runtime/'operational.sqlite3'
            sqlite3.connect(db).close()
            class Cursor:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def execute(self,sql):assert sql=='SELECT transaction_timestamp()'
                def fetchone(self):return (datetime.now(timezone.utc),)
            class Connection:
                def __init__(self,owner,target):
                    self.owner,self.target=owner,target;self.staged=deepcopy(owner.images[target])
                def cursor(self):return Cursor()
                def commit(self):
                    self.owner.commits.append(self.target)
                    if self.target and self.owner.failure=='handoff-before-commit':
                        self.owner.failure='none';raise OSError('connection lost before commit')
                    self.owner.images[self.target]=deepcopy(self.staged)
                    if not self.target and self.owner.failure=='raw-after-commit':
                        self.owner.failure='none';raise OSError('connection lost after commit')
                def rollback(self):pass
                def close(self):pass
            class Adapter(WebSourcePublicationAdapter):
                def __init__(self):
                    self.images={t:{name:[] for name,_ in tables} for t,tables in PG_TABLES.items()}
                    self.failure=failure;self.commits=[];self.locked=False
                def target(self,r):return runtime,db
                @contextmanager
                def _pg_connections(self,*,locked=False):
                    self.locked=locked
                    try:yield {t:Connection(self,t) for t in PG_TABLES}
                    finally:self.locked=False
                def _pg_read(self,conn,tables,r):return deepcopy(conn.staged)
                def _pg_capture(self,r,connections=None):
                    states={}
                    for t in PG_TABLES:states.update(deepcopy(connections[t].staged if connections else self.images[t]))
                    return states,{'False':['fixture-raw'],'True':['fixture-serving']}
                def _pg_replace(self,conn,r,tables,images):
                    assert self.locked,'PG CAS must hold both stores locked'
                    conn.staged=deepcopy(images)
                def _materialized(self,r):pass
            adapter=Adapter();adapters={'fixture':adapter};request['runtime_dir']=str(runtime)
            operation='operation-source-'+failure
            preview=execute(action='preview',adapter_name='fixture',operation_id=operation,request=request,adapters=adapters)
            receipt=execute(action='apply',adapter_name='fixture',operation_id=operation,request=request,
                expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters=adapters)
            if failure!='none':
                assert receipt['state']=='ambiguous' and receipt['readback']['parts']=={'raw':'after','handoff':'before'},receipt
                commits=list(adapter.commits)
                try:execute(action='apply',adapter_name='fixture',operation_id=operation,request=request,
                    expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters=adapters)
                except ApplyError as exc:assert str(exc)=='operation-not-ready'
                else:raise AssertionError('partial submit repeated')
                assert adapter.commits==commits
            else:
                assert receipt['state']=='applied'
                # The API is still candidate, but any raw-only update is drift.
                saved=deepcopy(adapter.images[False]);adapter.images[False]['sales_funnel_daily_raw'][0]['view_count']='999'
                assert adapter.readback(request,operation)['parts']['raw']=='drift'
                before=deepcopy(adapter.images)
                try:adapter.rollback(request,operation)
                except ValueError as exc:assert str(exc)=='rollback-after-state-drift'
                else:raise AssertionError('raw drift overwritten')
                assert adapter.images==before
                adapter.images[False]=deepcopy(saved)
                # A new raw SKU or a changed JSON report is equally observable.
                added=deepcopy(saved['sales_funnel_daily_raw'][0]);added['nm_id']=999
                adapter.images[False]['sales_funnel_daily_raw'].append(added)
                assert adapter.readback(request,operation)['parts']['raw']=='drift'
                adapter.images[False]=deepcopy(saved)
                adapter.images[False]['search_analytics_raw'][0]['raw_json']={'new':'report'}
                assert adapter.readback(request,operation)['parts']['raw']=='drift'
                adapter.images[False]=deepcopy(saved)
            assert adapter.rollback(request,operation)['state']=='restored'
            assert all(not rows for store in adapter.images.values() for rows in store.values())


def sparse_scope_checks():
    """A successful neighboring funnel day is captured but never rewritten."""
    from types import ModuleType
    from unittest.mock import patch
    source=observations()
    search_next=deepcopy(source[1])
    search_next['snapshot_date']='2026-09-12'
    search_next['request_period']={'start':'2026-09-12','end':'2026-09-12'}
    search_next['source_fetched_at']='2026-09-13T22:00:00Z'
    sparse=[*source,search_next]
    request={'dates':['2026-09-11','2026-09-12'],'observations':sparse,'source_sha256':digest(sparse),
        'supplier_identity_sha256':'fixture','source_date_pairs':[
            {'source_key':'seller_funnel_snapshot','snapshot_date':'2026-09-11'},
            {'source_key':'web_source_snapshot','snapshot_date':'2026-09-11'},
            {'source_key':'web_source_snapshot','snapshot_date':'2026-09-12'}]}
    assert source_date_pairs(request)=={('seller_funnel_snapshot','2026-09-11'),
        ('web_source_snapshot','2026-09-11'),('web_source_snapshot','2026-09-12')}
    bad=deepcopy(request)
    bad['source_date_pairs'].append({'source_key':'seller_funnel_snapshot','snapshot_date':'2026-09-12'})
    try:validate_observations(bad)
    except ValueError as exc:assert str(exc)=='source-observation-scope-invalid'
    else:raise AssertionError('unobserved successful funnel day entered write scope')

    statements=[];batches=[]
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,params):statements.append((sql,params))
    class Connection:
        def cursor(self):return Cursor()
    images={'sales_funnel_daily_raw':[
            {'snapshot_date':'2026-09-11','nm_id':1},
            {'snapshot_date':'2026-09-12','nm_id':2}],
        'search_analytics_raw':[
            {'date_from':'2026-09-11','date_to':'2026-09-11','nm_id':1},
            {'date_from':'2026-09-12','date_to':'2026-09-12','nm_id':2}]}
    # This transaction fixture must not require an installed PostgreSQL driver.
    pg=ModuleType('psycopg2');extras=ModuleType('psycopg2.extras')
    extras.Json=lambda value:value
    extras.execute_batch=lambda cursor,sql,rows:batches.append((sql,rows))
    pg.extras=extras
    with patch.dict(sys.modules,{'psycopg2':pg,'psycopg2.extras':extras}):
        WebSourcePublicationAdapter()._pg_replace(Connection(),request,
            [('sales_funnel_daily_raw','snapshot_date'),('search_analytics_raw','date_to')],images)
    assert [entry[1][0] for entry in statements]==[['2026-09-11'],['2026-09-11','2026-09-12']]
    assert len(batches[0][1])==1 and len(batches[1][1])==2
    source_recovery_checks(request)
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
            conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES('bundle','2026-09-11',?)",(canonical(plan()),))
            conn.execute("INSERT INTO temporal_source_slot_snapshots VALUES('seller_funnel_snapshot','2026-09-12','accepted_closed_day_snapshot','2026-09-13T21:00:00Z','preserved')")
            conn.execute("INSERT INTO temporal_source_closure_state VALUES('seller_funnel_snapshot','2026-09-12','yesterday_closed','success',1,NULL,'preserved','2026-09-13T21:00:00Z','2026-09-13T21:00:00Z','2026-09-13T21:00:00Z')")
        class LocalAdapter(WebSourcePublicationAdapter):
            def target(self,_):return runtime,db
            def _materialized(self,r):validate_observations(r)
        adapter=LocalAdapter();adapters={'fixture':adapter}
        publication={**request,'phase':'publication','nm_ids':[1,2,3],'runtime_dir':str(runtime)}
        preview=execute(action='preview',adapter_name='fixture',operation_id='sparse-publication',request=publication,adapters=adapters)
        receipt=execute(action='apply',adapter_name='fixture',operation_id='sparse-publication',request=publication,
            expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters=adapters)
        assert receipt['state']=='applied'
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key='seller_funnel_snapshot' AND snapshot_date='2026-09-12'").fetchone()[0]=='preserved'
            assert conn.execute("SELECT last_reason FROM temporal_source_closure_state WHERE source_key='seller_funnel_snapshot' AND target_date='2026-09-12'").fetchone()[0]=='preserved'
        assert adapter.rollback(publication,'sparse-publication')['state']=='restored'
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key='seller_funnel_snapshot' AND snapshot_date='2026-09-12'").fetchone()[0]=='preserved'
            assert conn.execute("SELECT last_reason FROM temporal_source_closure_state WHERE source_key='seller_funnel_snapshot' AND target_date='2026-09-12'").fetchone()[0]=='preserved'


def search_batch_checks():
    from packages.adapters.seller_portal_web_source_collector import _search_complete_items, CollectorError
    raw=[{'nmId':nm,'views':{'current':nm},'ctr':{'current':10},'orders':{'current':1},
          'avgPosition':{'current':2}} for nm in range(1,66)]
    def response(rows,total):
        return {'error':False,'additionalErrors':{'errors':None},'data':{
            'groups':[{'items':rows,'metrics':{'views':{'current':sum(r['views']['current'] for r in rows)},
                'orders':{'current':len(rows)}}}] if rows else [],'commonInfo':{'totalProducts':total}}}
    global_report=response(raw,65)
    global_report['data']['groups'][0]['items']=raw[:50]
    class Client:
        def post(self,url,**kwargs):
            wanted=set(json.loads(kwargs['data'])['nmIds'])
            rows=[r for r in raw if r['nmId'] in wanted]
            return type('Result',(),{'status':200,'json':lambda self:response(rows,len(rows))})()
    items,batches=_search_complete_items(Client(),'https://seller-content.wildberries.ru/search-report/report',
        {},{'currentPeriod':{'start':'2026-09-11','end':'2026-09-11'}},global_report,list(range(1,100)))
    assert len(items)==65 and len(batches)==3
    assert sum(r['views']['current'] for r in raw[:50]) < global_report['data']['groups'][0]['metrics']['views']['current']
    o=deepcopy(observations()[1]);o.update(pages=4,reported_count=65,raw_report=global_report,
        items=items,search_batches=batches)
    request={'dates':['2026-09-11'],'observations':[o],'source_sha256':digest([o]),
        'supplier_identity_sha256':'fixture','source_date_pairs':[{'source_key':'web_source_snapshot','snapshot_date':'2026-09-11'}]}
    validate_observations(request)
    incomplete=deepcopy(request);incomplete['observations'][0]['search_batches'].pop(1)
    incomplete['observations'][0]['pages']-=1;incomplete['source_sha256']=digest(incomplete['observations'])
    try:validate_observations(incomplete)
    except ValueError as exc:assert str(exc)=='search-source-product-count-incomplete'
    else:raise AssertionError('missing last search batch accepted')
    try:_search_complete_items(Client(),'https://seller-content.wildberries.ru/search-report/report',
        {},{'currentPeriod':{'start':'2026-09-11','end':'2026-09-11'}},global_report,list(range(1,65)))
    except CollectorError as exc:assert str(exc)=='search_report_product_count_incomplete'
    else:raise AssertionError('incomplete search universe accepted')

    def reject_publication(observation,reason):
        invalid={**request,'observations':[observation],'source_sha256':digest([observation])}
        try:validate_observations(invalid)
        except ValueError as exc:assert str(exc)==reason,(str(exc),reason)
        else:raise AssertionError('invalid search observation accepted:'+reason)

    def reject_collection(report,reason,client=None):
        try:_search_complete_items(client or Client(),
            'https://seller-content.wildberries.ru/search-report/report',{},
            {'currentPeriod':{'start':'2026-09-11','end':'2026-09-11'}},report,list(range(1,100)))
        except CollectorError as exc:assert str(exc)==reason,(str(exc),reason)
        else:raise AssertionError('invalid search report accepted:'+reason)

    # Full count and individually consistent batches cannot excuse a bad
    # unfiltered total. Cover both additive fields, including missing metrics.
    for metric in ('views','orders'):
        bad_report=deepcopy(global_report)
        bad_report['data']['groups'][0]['metrics'][metric]['current']+=1
        reject_collection(bad_report,'search_report_group_totals_incomplete')
        bad=deepcopy(o);bad['raw_report']=bad_report
        reject_publication(bad,'search-source-group-totals-incomplete')
    bad=deepcopy(o);bad['raw_report']['data']['groups'][0]['metrics'].pop('views')
    reject_collection(bad['raw_report'],'search_report_group_totals_incomplete')
    reject_publication(bad,'search-source-group-totals-incomplete')

    # Equal duplicate preview rows must not disappear through _dedupe, on
    # either the partial preview route or the already complete report route.
    for base in (o,observations()[1]):
        bad=deepcopy(base)
        rows=bad['raw_report']['data']['groups'][0]['items']
        rows.append(deepcopy(rows[0]))
        reject_collection(bad['raw_report'],'search_report_product_count_incomplete')
        reject_publication(bad,'search-source-pages-incomplete')
    complete=deepcopy(observations()[1])
    complete['raw_report']['data']['groups'][0]['metrics']['views']['current']+=1
    reject_collection(complete['raw_report'],'search_report_group_totals_incomplete')
    reject_publication(complete,'search-source-group-totals-incomplete')

    # Preserve all filtered-report guards while deferring only global totals.
    mutations=[
        ('search_report_batch_group_totals_incomplete','search-source-batch-group-totals-incomplete',
         lambda answer:answer['data']['groups'][0]['metrics']['views'].update(current=-1)),
        ('search_report_batch_incomplete_or_unbound','search-source-batch-count-or-filter-invalid',
         lambda answer:answer['data']['commonInfo'].update(totalProducts=999)),
        ('search_report_batch_incomplete_or_unbound','search-source-batch-count-or-filter-invalid',
         lambda answer:answer['data']['groups'][0]['items'][0].update(nmId=999)),
        ('search_report_batch_overlap_or_value_drift','search-source-batch-overlap-or-value-drift',
         lambda answer:answer['data']['groups'][0]['items'][0]['ctr'].update(current=99)),
    ]
    for collector_reason,publication_reason,mutate in mutations:
        class MutatedClient(Client):
            def post(self,url,**kwargs):
                answer=deepcopy(super().post(url,**kwargs).json())
                if 1 in json.loads(kwargs['data'])['nmIds']:mutate(answer)
                return type('Result',(),{'status':200,'json':lambda self:answer})()
        reject_collection(global_report,collector_reason,MutatedClient())
        bad=deepcopy(o)
        bad['search_batches'][0]['response']=deepcopy(bad['search_batches'][0]['response'])
        mutate(bad['search_batches'][0]['response'])
        reject_publication(bad,publication_reason)
    bad=deepcopy(o)
    bad['search_batches'][1]['requested_nm_ids'].insert(0,1)
    reject_publication(bad,'search-source-batch-scope-invalid')


def main():
    obs=observations();request={'dates':['2026-09-11'],'nm_ids':[1,2,3],'observations':obs,'source_sha256':digest(obs),'supplier_identity_sha256':'fixture','phase':'publication'}
    validate_observations(request)
    source_recovery_checks(request)
    sparse_scope_checks()
    search_batch_checks()
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
        # Full slot/closure images, not only payload bytes, belong to CAS.
        for table,column,drift in [('temporal_source_closure_state','last_reason','independent-new-attempt'),
            ('temporal_source_closure_state','state','closure_retrying'),('temporal_source_slot_snapshots','captured_at','2099-01-01T00:00:00Z')]:
            with sqlite3.connect(db) as conn:
                original=conn.execute(f'SELECT {column} FROM {table} ORDER BY rowid LIMIT 1').fetchone()[0]
                conn.execute(f'UPDATE {table} SET {column}=? WHERE rowid=(SELECT min(rowid) FROM {table})',(drift,))
            assert adapter.readback(request,'operation-fixture-1')['parts']['publication']=='drift'
            try:adapter.rollback(request,'operation-fixture-1')
            except ValueError as exc:assert str(exc)=='rollback-after-state-drift'
            else:raise AssertionError('independent closure/slot change overwritten')
            with sqlite3.connect(db) as conn:
                assert conn.execute(f'SELECT {column} FROM {table} ORDER BY rowid LIMIT 1').fetchone()[0]==drift
                conn.execute(f'UPDATE {table} SET {column}=? WHERE rowid=(SELECT min(rowid) FROM {table})',(original,))
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
