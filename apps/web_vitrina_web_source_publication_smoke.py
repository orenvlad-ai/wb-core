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
    rows=[['price','TOTAL|weighted_price_seller_discounted',524.434744],['stock','SKU:1|stock_total',900],
          ['orders','TOTAL|total_orderCount',1234]]
    for n in [1,2,3]:
        rows.extend([[m,f'SKU:{n}|{m}',7] for m in ['view_count','open_card_count','ctr','views_current','ctr_current','orders_current','position_avg']])
    rows.extend([[m,'TOTAL|'+m,9] for m in ['total_view_count','total_open_card_count','total_views_current',
        'avg_ctr_current','total_orders_current','avg_position_avg']])
    return {'date_columns':['2026-09-11'],'temporal_slots':[{'slot_key':'yesterday_closed','column_date':'2026-09-11'}],
        'metadata':{'unrelated':'retained'},'sheets':[{'sheet_name':'DATA_VITRINA','header':['name','id','2026-09-11'],'rows':rows},
        {'sheet_name':'STATUS','rows':[[s+'[yesterday_closed]','success','','','','','',3,0,'','stale'] for s in ['seller_funnel_snapshot','web_source_snapshot']]}]}


def held_pause(runtime):
    from packages.application import business_data_maintenance_pause as pause
    from packages.application.business_data_procedure_admission import initialize_admission
    from packages.application.business_data_write_barrier import acquire_barrier, confirm_barrier_hold

    initialize_admission(runtime)
    baseline = {'fixture':True}
    state = {'schema_version':pause.SCHEMA,'phase':'held','window_id':'fixture-source-recovery',
             'baseline':baseline,'baseline_fingerprint':pause.fingerprint(baseline),'hold_readback':{'quiet':True}}
    acquire_barrier(runtime,window_id=state['window_id'],window_kind='maintenance_pause',
        plan_fingerprint=state['baseline_fingerprint'],approval_reference='fixture-approved',actor='fixture',reason='fixture')
    pause.save_state(runtime,state,'fixture-held')
    confirm_barrier_hold(runtime,window_id=state['window_id'],plan_fingerprint=state['baseline_fingerprint'],maintenance_state=state)
    return state['window_id']


def source_recovery_checks(request):
    """Exercise the real launcher/adapter with transactional stores and injected IO failures."""
    from contextlib import contextmanager
    from datetime import datetime,timezone
    from apps.web_vitrina_web_source_publication import PG_TABLES
    from apps.production_apply_launcher import ApplyError
    request={**request,'phase':'source'}
    for failure in ('raw-after-commit','handoff-before-commit','none','held'):
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
                    if locked:
                        from packages.application.business_data_heavy_admission import require_heavy_owner
                        require_heavy_owner(runtime)
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
            if failure=='held':request['maintenance_window_id']=held_pause(runtime)
            operation='operation-source-'+failure
            preview=execute(action='preview',adapter_name='fixture',operation_id=operation,request=request,adapters=adapters)
            receipt=execute(action='apply',adapter_name='fixture',operation_id=operation,request=request,
                expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters=adapters)
            if failure not in {'none','held'}:
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


def mixed_roster_checks(source_observations=None):
    """Overlapping historical plans retain distinct rosters and source coverage."""
    from contextlib import contextmanager
    import fcntl
    import os
    from unittest.mock import patch
    from packages.application.business_data_heavy_admission import (
        heavy_admitted, require_heavy_owner, HeavyAdmissionBusy, LOCK_FILENAME,
    )
    from packages.application.business_data_procedure_admission import MaintenanceAdmissionBlocked
    from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
    with TemporaryDirectory() as tmp:
        runtime=Path(tmp);db=runtime/'operational.sqlite3'
        first=plan()
        first['sheets'][0]['rows']=[r for r in first['sheets'][0]['rows'] if not r[1].startswith('SKU:2|')]
        second=plan()
        # The second outer snapshot also contains the requested date. Its next
        # column is outside recovery and must survive byte-for-byte in the plan.
        second['date_columns'].append('2026-09-12')
        second['sheets'][0]['header'].append('2026-09-12')
        for row in second['sheets'][0]['rows']:row.append(123)
        originals={'2026-09-11':first,'2026-09-12':second}
        with sqlite3.connect(db) as conn:
            conn.executescript('''CREATE TABLE registry_upload_current_state(slot INTEGER,bundle_version TEXT);
                CREATE TABLE registry_upload_config_v2(bundle_version TEXT,nm_id INTEGER);
                CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version TEXT,as_of_date TEXT,plan_json TEXT,PRIMARY KEY(bundle_version,as_of_date));
                CREATE TABLE temporal_source_slot_snapshots(source_key TEXT,snapshot_date TEXT,snapshot_role TEXT,captured_at TEXT,payload_json TEXT,PRIMARY KEY(source_key,snapshot_date,snapshot_role));
                CREATE TABLE temporal_source_closure_state(source_key TEXT,target_date TEXT,slot_kind TEXT,state TEXT,attempt_count INTEGER,next_retry_at TEXT,last_reason TEXT,last_attempt_at TEXT,last_success_at TEXT,accepted_at TEXT,PRIMARY KEY(source_key,target_date,slot_kind));''')
            conn.execute("INSERT INTO registry_upload_current_state VALUES(1,'bundle')")
            conn.executemany("INSERT INTO registry_upload_config_v2 VALUES('bundle',?)",[(1,),(2,),(3,)])
            conn.executemany("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES('bundle',?,?)",
                [(day,canonical(p)) for day,p in originals.items()])
        class LocalAdapter(WebSourcePublicationAdapter):
            def target(self,_):return runtime,db
            def _materialized(self,r):validate_observations(r)
        adapter=LocalAdapter();adapters={'fixture':adapter}
        obs=source_observations or observations()
        request={'dates':['2026-09-11'],'phase':'publication','nm_ids':[1,2,3],
            'expected_nm_ids_by_as_of_date':{'2026-09-11':[1,3],'2026-09-12':[1,2,3]},
            'observations':obs,'source_sha256':digest(obs),'supplier_identity_sha256':'fixture','runtime_dir':str(runtime)}
        for modify,reason in [
            (lambda r:r.pop('expected_nm_ids_by_as_of_date'),'publication-roster-drift'),
            (lambda r:r['expected_nm_ids_by_as_of_date'].pop('2026-09-12'),'publication-roster-scope-invalid'),
            (lambda r:r['expected_nm_ids_by_as_of_date'].update({'2026-09-13':[1]}),'publication-roster-scope-invalid'),
            (lambda r:r['expected_nm_ids_by_as_of_date'].update({'2026-09-11':[1,2]}),'publication-roster-drift'),
            (lambda r:r['expected_nm_ids_by_as_of_date'].update({'2026-09-11':[3,1]}),'publication-roster-scope-invalid'),
            (lambda r:r['expected_nm_ids_by_as_of_date'].update({'2026-09-11':[1,1,3]}),'publication-roster-scope-invalid'),
            (lambda r:r['expected_nm_ids_by_as_of_date'].update({'2026-09-11':[True,3]}),'publication-roster-scope-invalid'),
            (lambda r:r.update(nm_ids=[1,3]),'publication-roster-union-mismatch'),
        ]:
            invalid=deepcopy(request);modify(invalid)
            try:adapter.preview(invalid,'mixed-invalid')
            except ValueError as exc:assert str(exc)==reason,(str(exc),reason)
            else:raise AssertionError('unbound historical roster accepted')
        preview=adapter.preview(request,'mixed-publication')
        assert len(preview['candidate']['updates'])==2
        if obs[1]['completeness']=='scoped_complete':
            assert preview['scope']['search_scopes']==[{'snapshot_date':'2026-09-11',**obs[1]['search_scope']}]
        # A competing heavy producer must be rejected before domain locks or
        # evidence are created, even if caller already holds a valid preview.
        lock=runtime/LOCK_FILENAME
        fd=os.open(lock,os.O_CREAT|os.O_RDWR,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:adapter.apply(request,'mixed-publication',preview)
            except HeavyAdmissionBusy:pass
            else:raise AssertionError('competing heavy producer admitted')
        finally:os.close(fd)
        with patch('packages.application.business_data_procedure_admission.barrier_status',return_value={'active':True}):
            try:adapter.apply(request,'mixed-publication',preview)
            except MaintenanceAdmissionBlocked:pass
            else:raise AssertionError('maintenance bypassed')
        assert not (runtime/'.warehouse-functional-job.lock').exists()
        assert not (runtime/'evidence').exists()
        @contextmanager
        def checked_job_lock(root,**kwargs):
            require_heavy_owner(root)
            with warehouse_functional_job_lock(root,**kwargs) as owner:yield owner
        with patch('apps.web_vitrina_web_source_publication.warehouse_functional_job_lock',checked_job_lock):
            # Same-process heavy ownership is borrowed safely, never reacquired.
            with heavy_admitted(runtime,operation='fixture_parent') as owner:
                receipt=execute(action='apply',adapter_name='fixture',operation_id='mixed-publication',request=request,
                    expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters=adapters)
                assert require_heavy_owner(runtime) is owner
            assert receipt['state']=='applied'
            with sqlite3.connect(db) as conn:
                saved={day:json.loads(value) for day,value in conn.execute('SELECT as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots')}
                payload=json.loads(conn.execute("SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key='seller_funnel_snapshot'").fetchone()[0])
            assert [i['nm_id'] for i in payload['items']]==[1,2]
            for day,expected_views,expected_orders,expected_position in [
                ('2026-09-11',100,1,2),('2026-09-12',400,3,2.5),
            ]:
                new=saved[day];rows={r[1]:r for r in new['sheets'][0]['rows']}
                assert non_target_digest(originals[day],obs)==non_target_digest(new,obs)
                assert rows['TOTAL|total_views_current'][2]==expected_views
                assert rows['TOTAL|total_orders_current'][2]==expected_orders
                assert rows['TOTAL|avg_position_avg'][2]==expected_position
                assert rows['TOTAL|total_orderCount'][2]==1234
                assert rows['SKU:1|orders_current'][2]==1 and rows['SKU:1|position_avg'][2]==2
                assert rows['SKU:3|orders_current'][2]=='' and rows['SKU:3|position_avg'][2]==''
                assert rows['SKU:3|view_count'][2]==''
                meta=new['metadata']['server_cell_presentation']
                assert meta['SKU:1|orders_current']['2026-09-11']['source_completeness']==obs[1]['completeness']
                if obs[1]['completeness']=='scoped_complete':
                    assert meta['SKU:1|orders_current']['2026-09-11']['search_scope']==obs[1]['search_scope']
                assert meta['SKU:3|view_count']['2026-09-11']['zero_fill_applied'] is False
                assert meta['TOTAL|total_views_current']['2026-09-11']['missing_sku_count']==1
                assert len(meta['TOTAL|total_views_current']['2026-09-11']['metric_scope_evidence']['applicable_scope'])==len(request['expected_nm_ids_by_as_of_date'][day])
                for total,operand in [('total_orders_current','orders_current'),('avg_position_avg','position_avg')]:
                    cell=meta['TOTAL|'+total]['2026-09-11']
                    assert cell['source_key']=='web_source_snapshot' and cell['source_digest']==digest(obs[1])
                    assert cell['completeness_state']=='partial' and cell['missing_sku_count']==1
                    assert cell['zero_fill_applied'] is False
                    assert cell['metric_scope_evidence']=={
                        'operand_date':'2026-09-11',
                        'applicable_scope':[f'SKU:{nm}' for nm in request['expected_nm_ids_by_as_of_date'][day]],
                        'missing_scope':['SKU:3'], 'sku_metric_keys':[operand], 'group_scopes':{},
                    }
                    if day=='2026-09-12':
                        assert rows['TOTAL|'+total][3]==123
                        drift=deepcopy(new)
                        next(r for r in drift['sheets'][0]['rows'] if r[1]=='TOTAL|'+total)[3]+=1
                        assert non_target_digest(drift,obs)!=non_target_digest(new,obs)
            assert saved['2026-09-12']['sheets'][0]['rows'][0][3]==123
            with patch('packages.application.business_data_procedure_admission.barrier_status',return_value={'active':True}):
                try:adapter.rollback(request,'mixed-publication')
                except MaintenanceAdmissionBlocked:pass
                else:raise AssertionError('rollback maintenance bypassed')
            assert adapter.readback(request,'mixed-publication')['state']=='applied'
            assert adapter.rollback(request,'mixed-publication')['state']=='restored'
        # Even an unrelated change after review invalidates the whole candidate.
        preview=adapter.preview(request,'mixed-cas-drift')
        with sqlite3.connect(db) as conn:
            drift=deepcopy(first);drift['metadata']['unrelated']='concurrent'
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date='2026-09-11'",(canonical(drift),))
        try:adapter.apply(request,'mixed-cas-drift',preview)
        except ValueError as exc:assert str(exc)=='candidate-cas-drift'
        else:raise AssertionError('mixed candidate CAS drift ignored')
        assert not (runtime/'evidence'/'mixed-cas-drift.before.json').exists()


def scoped_observations():
    """A certificate proves queried SKU scope even when WB reports fewer products."""
    from packages.adapters.seller_portal_web_source_collector import validate_scoped_search_evidence
    obs=observations();o=obs[1]
    report=o['raw_report'];data=report['data'];group=data['groups'][0]
    data['commonInfo']['totalProducts']=1
    for row in group['items']:
        row.update(openCard={'current':1},addToCart={'current':1})
    group['metrics'].update(openCard={'current':2},addToCart={'current':2})
    batches=[{'requested_nm_ids':[1,2,3],'response':deepcopy(report)}]
    items,certificate=validate_scoped_search_evidence(report=report,batches=batches,scope_nm_ids=[1,2,3])
    o.update(completeness='scoped_complete',search_scope=certificate,items=items,
        search_batches=batches,pages=2,reported_count=1)
    return obs


def scoped_publication_checks():
    obs=scoped_observations()
    request={'dates':['2026-09-11'],'nm_ids':[1,2,3],'observations':obs,
        'source_sha256':digest(obs),'supplier_identity_sha256':'fixture','phase':'source'}
    validate_observations(request)
    for mutate,reason in [
        (lambda r:r.update(nm_ids=[1,2,3,4]),'search-scoped-source-roster-unbound'),
        (lambda r:r['observations'][1]['search_scope'].update(missing_nm_ids=[]),'search-scoped-source-evidence-mismatch'),
        (lambda r:r['observations'][1]['items'][0].update(orders_current=999),'search-scoped-source-evidence-mismatch'),
        (lambda r:r['observations'][1].update(pages=3),'search-scoped-source-evidence-mismatch'),
    ]:
        invalid=deepcopy(request);mutate(invalid);invalid['source_sha256']=digest(invalid['observations'])
        try:validate_observations(invalid)
        except ValueError as exc:assert str(exc)==reason,(str(exc),reason)
        else:raise AssertionError('invalid scoped publication accepted')
    from packages.adapters.seller_portal_web_source_collector import CollectorError
    for metric in ('orders','avgPosition'):
        invalid=deepcopy(request)
        invalid['observations'][1]['raw_report']['data']['groups'][0]['items'][0][metric]['current']=None
        invalid['source_sha256']=digest(invalid['observations'])
        try:validate_observations(invalid)
        except CollectorError as exc:assert str(exc)=='search_scope_metric_invalid'
        else:raise AssertionError('null search metric accepted as an observation')
    from apps.web_vitrina_web_source_publication import PG_TABLES
    class ExistingRowsAdapter(WebSourcePublicationAdapter):
        def _pg_capture(self,r,connections=None):return self.before,{}
    for table in ('search_analytics_raw','web_source_search_analytics_daily'):
        adapter=ExistingRowsAdapter()
        adapter.before={name:[] for tables in PG_TABLES.values() for name,_ in tables}
        adapter.before[table]=[{'date_from':'2026-09-11','date_to':'2026-09-11','nm_id':999}]
        try:adapter.build(request,'scope-before-drift',None)
        except ValueError as exc:assert str(exc)=='source-search-scope-excludes-existing-sku'
        else:raise AssertionError('source replacement could discard unqueried existing SKU')
        adapter.before[table][0]['nm_id']=3
        assert adapter.build(request,'scope-before-valid',None)['phase']=='source'
    source_recovery_checks(request)
    mixed_roster_checks(obs)


def explicit_search_zero_checks():
    from packages.adapters.seller_portal_web_source_collector import _search_items
    obs=observations()
    group=obs[1]['raw_report']['data']['groups'][0]
    group['items'][0]['orders']['current']=0
    group['items'][1]['orders']['current']=0
    group['items'][1]['avgPosition']['current']=0
    group['metrics']['orders']['current']=0
    obs[1]['items']=_search_items(group['items'])
    request={'dates':['2026-09-11'],'observations':obs,'source_sha256':digest(obs),'supplier_identity_sha256':'fixture'}
    validate_observations(request)
    new,_=project(plan(),obs,[1,2,3],'explicit-zero')
    values={row[1]:row[2] for row in new['sheets'][0]['rows']}
    assert values['TOTAL|total_orders_current']==0
    assert values['TOTAL|avg_position_avg']==1
    assert values['SKU:2|orders_current']==values['SKU:2|position_avg']==0
    assert values['SKU:3|orders_current']==values['SKU:3|position_avg']==''
    for item in obs[1]['items']:
        item.update(views_current=0,ctr_current=0)
    for expected, ctr in [(0,0),('',1)]:
        obs[1]['items'][0]['ctr_current']=ctr
        new,_=project(plan(),obs,[1,2,3],'zero-search-views')
        values={row[1]:row[2] for row in new['sheets'][0]['rows']}
        assert values['TOTAL|total_views_current']==0 and values['TOTAL|avg_ctr_current']==expected
        assert values['SKU:3|ctr_current']==''


def historical_row_expansion_checks():
    import fcntl
    from apps.web_vitrina_web_source_publication import SKU_METRICS, historical_publication_roster
    from packages.adapters.seller_portal_web_source_collector import _funnel_items, _search_items
    from packages.application.business_data_procedure_admission import AdmissionLease, LOCK_FILENAME, MaintenanceAdmissionBlocked
    from packages.application.web_vitrina_history_compiler import CONTRACT, digest as history_digest
    from packages.application.web_vitrina_history_store import HistoryStore

    first, second, untouched = '2026-09-11', '2026-09-12', '2026-09-13'
    all_metrics = tuple(metric for keys in SKU_METRICS.values() for metric in keys)
    funnel, search_first = observations()
    search_group = search_first['raw_report']['data']['groups'][0]
    for nm in (94, 96):
        search_group['items'].append({'nmId':nm,'views':{'current':nm},'ctr':{'current':10},
            'orders':{'current':nm},'avgPosition':{'current':nm}})
        funnel['raw_report']['data']['groups'][0]['itemsGroup'].append({'nmId':nm,'name':str(nm),
            'vendorCode':str(nm),'viewCount':{'current':nm},'openCard':{'current':1},'viewToOpen':{'current':1}})
    search_group['metrics']['views']['current'] += 190
    search_group['metrics']['orders']['current'] += 190
    search_first['raw_report']['data']['commonInfo']['totalProducts'] = 4
    search_first['items'] = _search_items(search_group['items'])
    search_second = deepcopy(search_first)
    funnel_group = funnel['raw_report']['data']['groups'][0]
    funnel_group['viewCount']['current'] += 190
    funnel_group['openCard']['current'] += 2
    funnel['items'] = _funnel_items(funnel_group['itemsGroup'])
    for observation in (funnel, search_second):
        observation.update(snapshot_date=second,request_period={'start':second,'end':second},
                           source_fetched_at='2026-09-13T22:00:00Z')
    obs = [search_first, funnel, search_second]

    def ready(ids):
        value = plan()
        sheet = value['sheets'][0]
        sheet['rows'].extend([[metric, f'SKU:{nm}|{metric}', 7] for nm in ids if nm > 3 for metric in all_metrics])
        value['date_columns'] = [first, second, untouched]
        sheet['header'] = ['label', 'key', *value['date_columns']]
        for row in sheet['rows']:
            row.extend([234, 345])
        sheet.update(write_start_cell='A1', row_count=len(sheet['rows']), write_rect=f"A1:E{len(sheet['rows']) + 1}")
        return value

    with TemporaryDirectory() as tmp:
        runtime = Path(tmp)
        db = runtime / 'operational.sqlite3'
        store = HistoryStore(runtime / 'history')
        rows = {f'SKU:{nm}|{metric}': {'row_id':f'SKU:{nm}|{metric}','row_kind':'sku',
            'values':{'scope_label':[f'Catalog SKU {nm}'],'metric_label':[f'Catalog {metric}']}}
            for nm in range(1,97) for metric in all_metrics}
        epoch = history_digest('catalog fixture')
        catalog = {'rows':rows,'order':list(rows),'context_epoch':epoch}
        units = {day:{'contract':CONTRACT,'date':day,'context_epoch':epoch,'accepted_ready_available':True,
            'members':[f'SKU:{nm}|{metric}' for nm in range(1,upper+1) for metric in all_metrics],
            'cells':{key:[None]*16 for key in rows}} for day,upper in [(first,94),(second,96),(untouched,96)]}
        def publish_fixture():
            vector={'coverage':'complete_frozen_native_v1','epoch':epoch,
                    'dates':{day:history_digest(unit) for day,unit in units.items()}}
            return store.update(vector=vector,catalog=catalog,compile_day=units.__getitem__,revalidate=lambda:vector)['edition_id']
        base = publish_fixture()
        dated = historical_publication_roster(store, base, obs)
        assert dated[first]['nm_ids']==list(range(1,95)) and dated[second]['nm_ids']==list(range(1,97))
        assert 'SKU:94|view_count' not in dated[first]['row_labels']
        originals = {first:ready(range(1,34)),untouched:ready(range(1,97))}
        with sqlite3.connect(db) as conn:
            conn.executescript('''CREATE TABLE registry_upload_current_state(slot INTEGER,bundle_version TEXT);
                CREATE TABLE registry_upload_config_v2(bundle_version TEXT,nm_id INTEGER);
                CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version TEXT,as_of_date TEXT,plan_json TEXT,PRIMARY KEY(bundle_version,as_of_date));
                CREATE TABLE temporal_source_slot_snapshots(source_key TEXT,snapshot_date TEXT,snapshot_role TEXT,captured_at TEXT,payload_json TEXT,PRIMARY KEY(source_key,snapshot_date,snapshot_role));
                CREATE TABLE temporal_source_closure_state(source_key TEXT,target_date TEXT,slot_kind TEXT,state TEXT,attempt_count INTEGER,next_retry_at TEXT,last_reason TEXT,last_attempt_at TEXT,last_success_at TEXT,accepted_at TEXT,PRIMARY KEY(source_key,target_date,slot_kind));''')
            conn.execute("INSERT INTO registry_upload_current_state VALUES(1,'bundle')")
            conn.executemany("INSERT INTO registry_upload_config_v2 VALUES('bundle',?)",[(nm,) for nm in range(1,97)])
            conn.executemany("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES('bundle',?,?)",
                [(day,canonical(value)) for day,value in originals.items()])
        class LocalAdapter(WebSourcePublicationAdapter):
            fail_materialized=False
            def target(self,_):return runtime,db
            def _materialized(self,r):
                if self.fail_materialized:raise ValueError('materialized-source-value-mismatch')
                validate_observations(r)
            def _history_store(self):return store
        adapter = LocalAdapter()
        request = {'dates':[first,second],'observations':obs,'source_sha256':digest(obs),'supplier_identity_sha256':'fixture',
            'source_date_pairs':[{'source_key':o['source_key'],'snapshot_date':o['snapshot_date']} for o in obs],
            'phase':'publication','runtime_dir':str(runtime),'nm_ids':list(range(1,97)),
            'expected_nm_ids_by_as_of_date':{first:list(range(1,34)),untouched:list(range(1,97))},
            'history_roster':{'edition_id':base,'dates':dated},'maintenance_window_id':held_pause(runtime)}
        for mutate, error in [
            (lambda r:r['history_roster']['dates'][first]['row_labels'].update({'SKU:94|position_avg':'invented'}),
             'publication-history-roster-drift'),
            (lambda r:r['history_roster']['dates'][first]['row_labels'].update({'SKU:94|stock_total':'invented'}),
             'publication-history-roster-drift'),
            (lambda r:r['history_roster']['dates'][first]['nm_ids'].remove(94),'publication-history-roster-drift'),
            (lambda r:r['history_roster'].update(edition_id='f'*64),'publication-history-edition-drift'),
            (lambda r:r['expected_nm_ids_by_as_of_date'][first].append(34),'publication-roster-drift'),
            (lambda r:r['nm_ids'].remove(96),'publication-history-roster-union-mismatch'),
            (lambda r:r.update(maintenance_window_id='incorrect-window'),'source-recovery-maintenance-window-mismatch'),
        ]:
            bad=deepcopy(request);mutate(bad)
            try:adapter.preview(bad,'history-invalid')
            except ValueError as exc:assert str(exc)==error,(str(exc),error)
            else:raise AssertionError('unbound history scope accepted')
        operation = 'history-expansion'
        adapters = {'fixture':adapter}
        preview = execute(action='preview',adapter_name='fixture',operation_id=operation,request=request,adapters=adapters)
        assert preview['scope']['added_rows']==63*7
        try:AdmissionLease.held_web_source_recovery(runtime)
        except ValueError as exc:assert str(exc)=='source-recovery-held-owner-required'
        else:raise AssertionError('procedure lease issued to an unbound caller')
        ordinary={key:value for key,value in request.items() if key!='maintenance_window_id'}
        try:adapter.apply(ordinary,operation,preview)
        except MaintenanceAdmissionBlocked:pass
        else:raise AssertionError('ordinary recovery bypassed held pause')
        with (runtime/LOCK_FILENAME).open('rb') as exclusive:
            fcntl.flock(exclusive,fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:adapter.apply(request,operation,preview)
            except ValueError as exc:assert str(exc)=='source-recovery-admission-not-idle'
            else:raise AssertionError('recovery bypassed a real procedure writer')
        with store._writer():
            try:adapter.apply(request,operation,preview)
            except ValueError as exc:assert str(exc)=='publication-history-writer-busy'
            else:raise AssertionError('native recovery admitted with a moving history base')
        receipt = execute(action='apply',adapter_name='fixture',operation_id=operation,request=request,
            expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters=adapters)
        assert receipt['state']=='applied'
        with sqlite3.connect(db) as conn:
            saved={day:json.loads(value) for day,value in conn.execute('SELECT as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots')}
        assert set(saved)=={first,untouched}  # A covered day without an outer READY is not synthesized.
        early = saved[first]['sheets'][0]
        projected = {row[1]:row for row in early['rows']}
        assert projected['SKU:94|position_avg']==['Catalog SKU 94: Catalog position_avg','SKU:94|position_avg',94,94,'']
        assert projected['SKU:96|orders_current'][2:]==['',96,'']
        assert projected['SKU:94|view_count'][2:]==['',94,'']
        assert projected['TOTAL|total_orders_current'][2:]==[97,193,345]
        assert projected['TOTAL|avg_position_avg'][2:]==[33,48.75,345]
        assert projected['TOTAL|total_view_count'][2:]==[9,290,345]
        assert projected['TOTAL|total_orderCount'][2:]==[1234,234,345]
        assert 'SKU:94|stock_total' not in projected
        assert early['row_count']==len(early['rows']) and early['write_rect']==f"A1:E{early['row_count'] + 1}"
        proof = saved[first]['metadata']['server_cell_presentation']
        assert proof['SKU:96|orders_current'].keys()=={second}
        assert proof['TOTAL|total_orders_current'][first]['history_edition_id']==base
        assert proof['TOTAL|total_orders_current'][first]['missing_sku_count']==91
        assert next(row for row in saved[untouched]['sheets'][0]['rows'] if row[1]=='SKU:96|orders_current')[2:]==[7,96,345]
        additions = adapter._retained(request,operation,runtime)['added_rows'][first]
        baseline = non_target_digest(originals[first],obs,dated_rosters=dated)
        assert non_target_digest(saved[first],obs,dated_rosters=dated,added_rows=additions)==baseline
        drift=deepcopy(saved[first])
        drift['metadata']['server_cell_presentation']['SKU:96|orders_current'][untouched]={'invented':True}
        assert non_target_digest(drift,obs,dated_rosters=dated,added_rows=additions)!=baseline
        drift=deepcopy(saved[first])
        next(row for row in drift['sheets'][0]['rows'] if row[1]=='SKU:96|orders_current')[2]=123
        try:non_target_digest(drift,obs,dated_rosters=dated,added_rows=additions)
        except ValueError as exc:assert str(exc)=='publication-history-added-row-outside-scope'
        else:raise AssertionError('new row could alter a day outside its roster')
        units[first]['source_note']='later verified edition'
        next_edition=publish_fixture()
        assert next_edition!=base
        assert adapter.readback(request,operation)['state']=='applied'
        neighbor=deepcopy(search_second)
        neighbor.update(snapshot_date=untouched,request_period={'start':untouched,'end':untouched},
                        source_fetched_at='2026-09-14T22:00:00Z')
        second_request={**request,'dates':[untouched],'observations':[neighbor],'source_sha256':digest([neighbor]),
            'source_date_pairs':[{'source_key':'web_source_snapshot','snapshot_date':untouched}],
            'expected_nm_ids_by_as_of_date':{first:list(range(1,97)),untouched:list(range(1,97))},
            'history_roster':{'edition_id':next_edition,'dates':historical_publication_roster(store,next_edition,[neighbor])}}
        second_operation='history-adjacent'
        second_preview=execute(action='preview',adapter_name='fixture',operation_id=second_operation,request=second_request,adapters=adapters)
        second_receipt=execute(action='apply',adapter_name='fixture',operation_id=second_operation,request=second_request,
            expected_prestate=second_preview['prestate_sha256'],expected_candidate=second_preview['candidate_sha256'],adapters=adapters)
        assert second_receipt['state']=='applied'
        assert adapter.readback(request,operation)['state']=='ambiguous'
        assert adapter.readback(request,operation,target_only=True)['state']=='applied'
        try:adapter.rollback(request,operation)
        except ValueError as exc:assert str(exc)=='rollback-after-state-drift'
        else:raise AssertionError('strict rollback overwrote the adjacent operation')
        with sqlite3.connect(db) as conn:
            current=conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?',(first,)).fetchone()[0]
        for kind in ('value','label','provenance','status'):
            altered=json.loads(current)
            row=next(row for row in altered['sheets'][0]['rows'] if row[1]=='SKU:94|position_avg')
            if kind=='value':row[2]+=1
            elif kind=='label':row[0]='changed label'
            elif kind=='provenance':altered['metadata']['server_cell_presentation'][row[1]][first]['source_digest']='changed'
            else:altered['sheets'][1]['rows'][1][10]='changed status'
            with sqlite3.connect(db) as conn:
                conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?',(canonical(altered),first))
            assert adapter.readback(request,operation,target_only=True)['parts']['publication']=='drift'
        with sqlite3.connect(db) as conn:
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?',(current,first))
        for table, key, column in [('temporal_source_slot_snapshots','snapshot_date','captured_at'),
                                   ('temporal_source_closure_state','target_date','last_reason')]:
            with sqlite3.connect(db) as conn:
                sql=f" WHERE {key}=? AND source_key='web_source_snapshot'"
                before=conn.execute(f'SELECT {column} FROM {table}'+sql,(first,)).fetchone()[0]
                conn.execute(f'UPDATE {table} SET {column}=?'+sql,('changed',first))
            assert adapter.readback(request,operation,target_only=True)['parts']['publication']=='drift'
            with sqlite3.connect(db) as conn:
                conn.execute(f'UPDATE {table} SET {column}=?'+sql,(before,first))
        adapter.fail_materialized=True
        assert adapter.readback(request,operation,target_only=True)['mismatches']==['materialized:ValueError']
        adapter.fail_materialized=False
        assert adapter.rollback(second_request,second_operation)['state']=='restored'
        assert adapter.rollback(request,operation)['state']=='restored'
        with sqlite3.connect(db) as conn:
            assert {day:json.loads(value) for day,value in conn.execute(
                'SELECT as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots')}==originals


def main():
    obs=observations();request={'dates':['2026-09-11'],'nm_ids':[1,2,3],'observations':obs,'source_sha256':digest(obs),'supplier_identity_sha256':'fixture','phase':'publication'}
    validate_observations(request)
    source_recovery_checks(request)
    sparse_scope_checks()
    search_batch_checks()
    mixed_roster_checks()
    scoped_publication_checks()
    explicit_search_zero_checks()
    historical_row_expansion_checks()
    old=plan();new,changes=project(old,obs,[1,2,3],'op-fixture')
    assert non_target_digest(old,obs)==non_target_digest(new,obs)
    rows={r[1]:r for r in new['sheets'][0]['rows']}
    assert rows['SKU:2|view_count'][2]==0 and rows['SKU:3|view_count'][2]==''
    assert rows['TOTAL|avg_ctr_current'][2]==0.25
    assert rows['SKU:1|orders_current'][2]==1 and rows['SKU:2|position_avg'][2]==3
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
