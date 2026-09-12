"""Dated Seller Portal source/publication recovery through the one-submit launcher."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager, ExitStack
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter, readonly, private_json
from apps.seller_portal_automation_guard import seller_portal_automation_lock
from apps.production_apply_contract import AmbiguousSubmit
from packages.adapters.seller_portal_web_source_collector import _dedupe, _funnel_items, _search_items
from packages.adapters.web_source_current_sync import ShellBackedWebSourceCurrentSync, _load_env_file, _closed_day_required_fetched_after
from packages.application.ready_publication import ExpectedReady, replace_ready
from packages.application.seller_funnel_snapshot_block import transform_legacy_payload as funnel_payload
from packages.application.web_source_snapshot_block import transform_legacy_payload as search_payload
from packages.application.warehouse_sync_lock import warehouse_sync_lock
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock

SOURCE_KEYS = ('seller_funnel_snapshot','web_source_snapshot')
SKU_METRICS = {'seller_funnel_snapshot':('view_count','open_card_count','ctr'), 'web_source_snapshot':('views_current','ctr_current')}
TOTAL_METRICS = {'seller_funnel_snapshot':('total_view_count','total_open_card_count'), 'web_source_snapshot':('total_views_current','avg_ctr_current')}
PG_TABLES = {
    False: [('sales_funnel_daily_raw','snapshot_date'),('search_analytics_raw','date_to')],
    True: [('web_source_sales_funnel_daily','snapshot_date'),('web_source_search_analytics_daily','date_to')],
}
PG_NUMBERS = {'view_count','open_card_count','ctr','views_current','ctr_current','orders_current','position_avg'}


def pg_rows(rows):
    """Canonical exact images: retain all columns, normalize PG numeric/time types."""
    value=json.loads(canonical(rows))
    for row in value:
        for key,v in row.items():
            if v is not None and key in PG_NUMBERS:row[key]=format(Decimal(str(v)).normalize(),'f')
            elif v is not None and key in {'fetched_at','source_fetched_at','handoff_synced_at'}:
                row[key]=datetime.fromisoformat(v.replace('Z','+00:00')).astimezone(timezone.utc).isoformat()
    return sorted(value,key=lambda r:(r.get('snapshot_date',r.get('date_to')),r.get('date_from',''),r['nm_id']))


def canonical(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',',':'),default=str)


def digest(v):
    return 'sha256:' + hashlib.sha256(canonical(v).encode()).hexdigest()


def durable_json(path,value):
    private_json(path,value)
    descriptor=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def validate_observations(request):
    observations = request['observations']
    days = sorted(set(request['dates']))
    if not days or len(days)>7 or any(date.fromisoformat(d).isoformat()!=d for d in days):
        raise ValueError('source-date-scope-invalid')
    if digest(observations)!=request['source_sha256']:
        raise ValueError('source-digest-mismatch')
    if {(x['source_key'],x['snapshot_date']) for x in observations} != {(s,d) for s in SOURCE_KEYS for d in days} or len(observations)!=2*len(days):
        raise ValueError('source-observation-scope-invalid')
    for o in observations:
        day=o['snapshot_date']
        stamp=datetime.fromisoformat(o['source_fetched_at'].replace('Z','+00:00'))
        if (o.get('contract')!='seller_portal_dated_observation_v1' or o.get('completeness')!='complete'
            or o['request_period']!={'start':day,'end':day} or stamp.tzinfo is None
            or stamp < _closed_day_required_fetched_after(day)
            or stamp > datetime.now(timezone.utc) or not o['items']
            or o['supplier_identity_sha256']!=request['supplier_identity_sha256']):
            raise ValueError('source-observation-not-closed-or-unbound')
        ids=[i['nm_id'] for i in o['items']]
        if len(ids)!=len(set(ids)) or any(type(n) is not int for n in ids):
            raise ValueError('source-sku-identity-invalid')
        report=o['raw_report']
        additional=report.get('additionalErrors')
        if report.get('error') or (additional.get('errors') if isinstance(additional,dict) and set(additional)=={'errors'} else additional):
            raise ValueError('source-error-envelope')
        groups=report['data']['groups']
        if o['source_key']=='seller_funnel_snapshot':
            pages=o.get('detail_pages',[])
            if (not pages or len(pages)+1!=o['pages'] or any(p['offset']!=i*50 or p['limit']!=50 for i,p in enumerate(pages))
                or len(pages[-1]['response']['data'])>=50 or any(len(p['response']['data'])!=50 for p in pages[:-1])):
                raise ValueError('funnel-pagination-evidence-incomplete')
            reconstructed=_dedupe(_funnel_items([r for g in groups for r in g['itemsGroup']]+[r for p in pages for r in p['response']['data']]))
            if reconstructed!=o['items'] or o['pages']<2 or any(sum(g[key]['current'] for g in groups)!=sum(i[field] for i in o['items'])
                for key,field in [('viewCount','view_count'),('openCard','open_card_count')]):
                raise ValueError('funnel-source-pages-or-totals-incomplete')
        elif report['data']['commonInfo']['totalProducts']!=len(ids) or _dedupe(_search_items([r for g in groups for r in g['items']]))!=o['items']:
            raise ValueError('search-source-product-count-incomplete')
    return observations


def source_result(o, nm_ids):
    if o['source_key']=='seller_funnel_snapshot':
        return funnel_payload({'date':o['snapshot_date'],'count':len(o['items']),'items':o['items'],
            'source_fetched_at':o['source_fetched_at']}, nm_ids=nm_ids).result
    return search_payload({'date_from':o['snapshot_date'],'date_to':o['snapshot_date'],
        'count':len(o['items']),'items':o['items'],'source_fetched_at':o['source_fetched_at']}).result


def non_target_digest(plan, observations):
    """Remove only this domain's exact cells/status/provenance before comparing."""
    value=deepcopy(plan)
    for o in observations:
        day,source=o['snapshot_date'],o['source_key']
        slots={s['slot_key'] for s in value['temporal_slots'] if s['column_date']==day}
        keys=set()
        for sheet in value['sheets']:
            if sheet['sheet_name']=='DATA_VITRINA' and day in sheet['header']:
                col=sheet['header'].index(day)
                for row in sheet['rows']:
                    scope,metric=row[1].split('|',1)
                    if (scope=='TOTAL' and metric in TOTAL_METRICS[source]) or (scope.startswith('SKU:') and metric in SKU_METRICS[source]):
                        row[col]=None;keys.add(row[1])
            elif sheet['sheet_name']=='STATUS':
                for i,row in enumerate(sheet['rows']):
                    if any(row[0]==f'{source}[{slot}]' for slot in slots):sheet['rows'][i]=[row[0]]
        metadata=value.get('metadata',{})
        for key in keys:
            cells=metadata.get('server_cell_presentation',{})
            if key in cells:
                cells[key].pop(day,None)
                if not cells[key]:cells.pop(key)
            metadata.get('row_last_updated_at_by_row_id',{}).pop(key,None)
        for key in ('server_cell_presentation','row_last_updated_at_by_row_id'):
            if not metadata.get(key):metadata.pop(key,None)
    return digest(value)


def project(plan, observations, nm_ids, operation_id):
    result=deepcopy(plan)
    sheet=next(s for s in result['sheets'] if s['sheet_name']=='DATA_VITRINA')
    changes=[]
    for o in observations:
        day,source=o['snapshot_date'],o['source_key']
        if day not in sheet['header']:continue
        col=sheet['header'].index(day)
        items={i['nm_id']:i for i in o['items'] if i['nm_id'] in nm_ids}
        missing=sorted(set(nm_ids)-set(items))
        if not items:raise ValueError('source-has-no-applicable-sku')
        if source=='seller_funnel_snapshot':
            totals={'total_view_count':sum(i['view_count'] for i in items.values()),
                    'total_open_card_count':sum(i['open_card_count'] for i in items.values())}
        else:
            views=sum(i['views_current'] for i in items.values())
            totals={'total_views_current':views,'avg_ctr_current':round(sum(i['ctr_current']*i['views_current'] for i in items.values())/views/100,6) if views else ''}
        for row in sheet['rows']:
            scope,metric=row[1].split('|',1)
            if scope=='TOTAL' and metric in TOTAL_METRICS[source]:
                value=totals[metric]
                scope_missing=missing
            elif scope.startswith('SKU:') and metric in SKU_METRICS[source]:
                nm=int(scope.split(':')[1])
                if nm not in nm_ids:continue
                item=items.get(nm)
                value='' if item is None or item.get(metric) is None else item[metric]/100 if metric in {'ctr','ctr_current'} else item[metric]
                scope_missing=[nm] if value=='' else []
            else:continue
            before=row[col];row[col]=value
            metadata=result.setdefault('metadata',{})
            cell={'source_key':source,'source_date':day,'source_fetched_at':o['source_fetched_at'],
                'operation_id':operation_id,'source_digest':digest(o),'source_completeness':'complete',
                'completeness_state':'partial' if scope_missing else 'complete','missing_sku_count':len(scope_missing),
                'zero_fill_applied':False}
            if scope=='TOTAL':
                cell['metric_scope_evidence']={'operand_date':day,'applicable_scope':['SKU:'+str(n) for n in nm_ids],
                    'missing_scope':['SKU:'+str(n) for n in missing],'sku_metric_keys':list(SKU_METRICS[source]),'group_scopes':{}}
            metadata.setdefault('server_cell_presentation',{}).setdefault(row[1],{})[day]=cell
            metadata.setdefault('row_last_updated_at_by_row_id',{})[row[1]]=o['source_fetched_at']
            changes.append({'row_id':row[1],'date':day,'before':before,'after':value})
        for status_sheet in result['sheets']:
            if status_sheet['sheet_name']!='STATUS':continue
            slots={s['slot_key'] for s in result['temporal_slots'] if s['column_date']==day}
            for row in status_sheet['rows']:
                if any(row[0]==f'{source}[{slot}]' for slot in slots):
                    row[1]='success';row[2]=day;row[7]=len(nm_ids);row[8]=len(items);row[9]=','.join(map(str,missing))
                    row[10]=f'source_fetched_at={o["source_fetched_at"]}; source_completeness=complete; catalog_covered={len(items)}/{len(nm_ids)}; missing_not_zero=true; resolution_rule=accepted_closed_dated_source_recovery; operation_id={operation_id}'
    return result,changes


class WebSourcePublicationAdapter(WebVitrinaManagementHistoryAdapter):
    def target(self, request):
        value=super().target(request)
        configured=_load_env_file(Path('/opt/wb-ai/.env')).get('SELLER_PORTAL_CANONICAL_SUPPLIER_ID','')
        if not configured or hashlib.sha256(configured.encode()).hexdigest()!=request['supplier_identity_sha256']:
            raise ValueError('source-target-supplier-mismatch')
        return value

    def _backup(self,runtime,operation_id):
        return runtime/'evidence'/(operation_id+'.before.json')

    def _pg_connect(self, target=False):
        import psycopg2
        env=_load_env_file(Path('/opt/wb-ai/.env'))
        prefix='' if target else 'WEB_SOURCE_SRC_'
        return psycopg2.connect(**{name:env[prefix+key] for name,key in
            [('host','PGHOST'),('port','PGPORT'),('dbname','PGDATABASE'),('user','PGUSER'),('password','PGPASSWORD')]})

    @contextmanager
    def _pg_connections(self, *, locked=False):
        connections={}
        try:
            for target,tables in PG_TABLES.items():
                conn=self._pg_connect(target);connections[target]=conn
                if locked:
                    with conn.cursor() as cur:
                        cur.execute('SET LOCAL lock_timeout = \'5s\'')
                        for table,_ in tables:cur.execute(f'LOCK TABLE public.{table} IN SHARE ROW EXCLUSIVE MODE')
                else:conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
            yield connections
        finally:
            for conn in connections.values():
                conn.rollback();conn.close()

    def _pg_read(self,conn,tables,request):
        out={}
        with conn.cursor() as cur:
            for table,col in tables:
                # The handoff API reads every date_from for date_to. Bind these
                # neighboring rows too, while writes replace only exact-day rows.
                cur.execute(f'SELECT * FROM public.{table} WHERE {col}=ANY(%s::date[]) ORDER BY {col},nm_id',(request['dates'],))
                out[table]=pg_rows([dict(zip([c.name for c in cur.description],row)) for row in cur.fetchall()])
        return out

    def _pg_capture(self,request,connections=None):
        if connections is None:
            with self._pg_connections() as opened:return self._pg_capture(request,opened)
        rows={};identities={}
        for target,conn in connections.items():
            rows.update(self._pg_read(conn,PG_TABLES[target],request))
            with conn.cursor() as cur:
                cur.execute('SELECT current_database(),current_user,inet_server_addr()::text,inet_server_port()')
                identities[str(target)]=list(cur.fetchone())
        return rows,identities

    def _source_rows(self,request,target,stamp,before):
        after=deepcopy(before)
        for o in request['observations']:
            funnel=o['source_key']=='seller_funnel_snapshot';day=o['snapshot_date']
            table=PG_TABLES[target][0 if funnel else 1][0]
            # Explicit multi-day reports are not this recovery's write target.
            keep=[r for r in after[table] if (r.get('snapshot_date',r.get('date_to'))!=day or
                (not funnel and r['date_from']!=day))]
            for item in o['items']:
                row=deepcopy(item)
                if funnel:
                    row.update(snapshot_date=day)
                    row['source_fetched_at' if target else 'fetched_at']=o['source_fetched_at']
                else:
                    row.update(date_from=day,date_to=day,raw_json=o['raw_report'])
                    if not target:row['fetched_at']=o['source_fetched_at']
                if target:row['handoff_synced_at']=stamp
                keep.append(row)
            after[table]=pg_rows(keep)
        return after

    def _pg_replace(self,conn,request,tables,images):
        from psycopg2.extras import Json, execute_batch
        with conn.cursor() as cur:
            for table,col in tables:
                exact=' AND date_from=date_to' if col=='date_to' else ''
                cur.execute(f'DELETE FROM public.{table} WHERE {col}=ANY(%s::date[]){exact}',(request['dates'],))
                rows=[r for r in images[table] if col!='date_to' or r['date_from']==r['date_to']]
                if rows:
                    columns=list(rows[0])
                    execute_batch(cur,f'INSERT INTO public.{table} ('+','.join(columns)+') VALUES ('+','.join('%s' for _ in columns)+')',
                        [tuple(Json(r[k]) if k=='raw_json' else r[k] for k in columns) for r in rows])

    def _part_path(self,runtime,operation_id,target):
        return runtime/'evidence'/(operation_id+('.handoff' if target else '.raw')+'.write-ahead.json')

    def _source_apply(self,request,operation_id,candidate,runtime,connections):
        for target,conn in connections.items():
            tables=PG_TABLES[target]
            before={t:candidate['before_images'][t] for t,_ in tables}
            if digest(self._pg_read(conn,tables,request))!=digest(before):raise ValueError('source-part-before-drift')
            with conn.cursor() as cur:
                cur.execute('SELECT transaction_timestamp()');stamp=cur.fetchone()[0].isoformat()
            after=self._source_rows(request,target,stamp,before)
            self._pg_replace(conn,request,tables,after)
            if digest(self._pg_read(conn,tables,request))!=digest(after):raise ValueError('source-staged-after-mismatch')
            # Durable exact after image precedes commit, closing the crash window.
            part={'operation_id':operation_id,'request_sha256':digest(request),'candidate_sha256':digest(candidate),
                  'target':target,'stamp':stamp,'before':before,'after':after}
            path=self._part_path(runtime,operation_id,target);durable_json(path,part)
            if digest(json.loads(path.read_text()))!=digest(part):raise ValueError('source-journal-verification-failed')
            conn.commit()

    def _source_states(self,request,operation_id,candidate,runtime,connections=None):
        actual,identities=self._pg_capture(request,connections)
        if identities!=candidate['pg_targets']:return {'target':'drift'}
        states={}
        for target,tables in PG_TABLES.items():
            name='handoff' if target else 'raw'
            before={t:candidate['before_images'][t] for t,_ in tables}
            current={t:actual[t] for t,_ in tables}
            path=self._part_path(runtime,operation_id,target)
            after=None
            if path.exists():
                part=json.loads(path.read_text())
                if (part['operation_id']!=operation_id or part['request_sha256']!=digest(request)
                    or part['candidate_sha256']!=digest(candidate) or part['target']!=target or part['before']!=before):
                    raise ValueError('source-journal-identity-mismatch')
                after=part['after']
                if digest(after)!=digest(self._source_rows(request,target,part['stamp'],before)):
                    raise ValueError('source-journal-after-mismatch')
            states[name]='after' if after is not None and digest(current)==digest(after) else 'before' if digest(current)==digest(before) else 'drift'
        return states

    def _materialized(self,request):
        from urllib.request import urlopen
        from urllib.parse import urlencode
        for o in request['observations']:
            day=o['snapshot_date'];source=o['source_key']
            path='/v1/sales-funnel/daily?'+urlencode({'date':day}) if source=='seller_funnel_snapshot' else '/v1/search-analytics/snapshot?'+urlencode({'date_from':day,'date_to':day})
            with urlopen('http://127.0.0.1:8000'+path,timeout=10) as r:payload=json.load(r)
            observed={int(i['nm_id']):i for i in payload['items']}
            wanted={int(i['nm_id']):i for i in o['items']}
            if set(observed)!=set(wanted):raise ValueError('materialized-source-sku-mismatch')
            for nm,item in wanted.items():
                if any(observed[nm].get(k)!=v for k,v in item.items()):raise ValueError('materialized-source-value-mismatch')
            state=ShellBackedWebSourceCurrentSync()._load_closed_day_source_state(source,day)
            if state is None or state.row_count!=len(wanted) or datetime.fromisoformat(state.fetched_at.replace('Z','+00:00'))!=datetime.fromisoformat(o['source_fetched_at'].replace('Z','+00:00')):
                raise ValueError('materialized-source-clock-mismatch')

    def build(self,request,operation_id,conn,pg_connections=None):
        observations=validate_observations(request)
        if request['phase']=='source':
            before,identities=self._pg_capture(request,pg_connections)
            if any(r['date_from']!=r['date_to'] for table in ('search_analytics_raw','web_source_search_analytics_daily') for r in before[table]):
                raise ValueError('source-handoff-period-overlap')
            return {'operation_id':operation_id,'phase':'source','prestate_sha256':digest(before),
                'before_images':before,'pg_targets':identities,'source_sha256':request['source_sha256']}
        if request['phase']!='publication':raise ValueError('phase-invalid')
        current=conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()[0]
        config=[dict(r) for r in conn.execute('SELECT * FROM registry_upload_config_v2 WHERE bundle_version=? ORDER BY nm_id',(current,))]
        records=[dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? ORDER BY as_of_date',(current,))
                 if set(json.loads(r['plan_json']).get('date_columns',[])).intersection(request['dates'])]
        if not records or len(records)>16:raise ValueError('publication-ready-scope-invalid')
        expected_ids=sorted(set(request['nm_ids']))
        before_slots=[dict(r) for r in conn.execute("SELECT * FROM temporal_source_slot_snapshots WHERE source_key IN (?,?) AND snapshot_date IN ("+','.join('?' for _ in request['dates'])+") ORDER BY source_key,snapshot_date,snapshot_role",(*SOURCE_KEYS,*request['dates']))]
        before_closure=[dict(r) for r in conn.execute("SELECT * FROM temporal_source_closure_state WHERE source_key IN (?,?) AND target_date IN ("+','.join('?' for _ in request['dates'])+") ORDER BY source_key,target_date,slot_kind",(*SOURCE_KEYS,*request['dates']))]
        updates=[];changes=[]
        for record in records:
            old=json.loads(record['plan_json'])
            actual_ids=sorted({int(r[1].split('|')[0][4:]) for s in old['sheets'] if s['sheet_name']=='DATA_VITRINA' for r in s['rows'] if r[1].startswith('SKU:')})
            if actual_ids!=expected_ids:raise ValueError('publication-roster-drift')
            new,delta=project(old,observations,expected_ids,operation_id)
            if non_target_digest(old,observations)!=non_target_digest(new,observations):
                raise ValueError('non-target-publication-content-changed')
            updates.append({'bundle_version':current,'as_of_date':record['as_of_date'],'after_plan_json':canonical(new)})
            changes.extend({**c,'outer_date':record['as_of_date']} for c in delta)
        before={'ready':records,'slots':before_slots,'closure':before_closure,'config':config}
        after=deepcopy(before)
        for row,update in zip(after['ready'],updates):row['plan_json']=update['after_plan_json']
        slots={(r['source_key'],r['snapshot_date'],r['snapshot_role']):r for r in after['slots']}
        closure={(r['source_key'],r['target_date'],r['slot_kind']):r for r in after['closure']}
        for o in observations:
            source,day=o['source_key'],o['snapshot_date'];stamp=o['source_fetched_at']
            key=(source,day,'accepted_closed_day_snapshot')
            slots[key]={'source_key':source,'snapshot_date':day,'snapshot_role':key[2],
                'captured_at':stamp,'payload_json':canonical(asdict(source_result(o,expected_ids)))}
            key=(source,day,'yesterday_closed')
            closure[key]={**closure.get(key,{'source_key':source,'target_date':day,'slot_kind':key[2],'attempt_count':1}),
                'state':'success','next_retry_at':None,'last_reason':'dated_source_recovery:'+operation_id,
                'last_attempt_at':stamp,'last_success_at':stamp,'accepted_at':stamp}
        after['slots']=[slots[k] for k in sorted(slots)]
        after['closure']=[closure[k] for k in sorted(closure)]
        return {'operation_id':operation_id,'phase':'publication','prestate_sha256':digest(before),
            'before_images':before,'after_images':after,'updates':updates,'changes':changes,'source_sha256':request['source_sha256']}

    def preview(self,request,operation_id):
        runtime,db=self.target(request);backup=self._backup(runtime,operation_id)
        if backup.exists():
            retained=json.loads(backup.read_text())
            if digest(retained['request'])!=digest(request):raise ValueError('operation-request-mismatch')
            candidate=retained['candidate']
        else:
            with readonly(db) as conn:
                conn.execute('BEGIN');candidate=self.build(request,operation_id,conn)
        return {'operation_id':operation_id,'target':str(db),'scope':{'phase':request['phase'],'dates':request['dates'],
                'source_keys':list(SOURCE_KEYS),'changed_cells':len(candidate.get('changes',[]))},
            'prestate_sha256':candidate['prestate_sha256'],'candidate_sha256':digest(candidate),'candidate':candidate,
            'recovery':{'kind':'exact-source-and-ready-before-images','path':str(backup)}}

    def apply(self,request,operation_id,preview):
        runtime,_=self.target(request)
        try:
            return self._apply(request,operation_id,preview)
        except Exception:
            if self._backup(runtime,operation_id).exists():
                raise AmbiguousSubmit('source-publication-operation-requires-readback') from None
            raise

    def _apply(self,request,operation_id,preview):
        runtime,db=self.target(request)
        with warehouse_functional_job_lock(runtime,blocking=False),warehouse_sync_lock(runtime,blocking=False), seller_portal_automation_lock(runtime_dir=runtime,owner='web_source_publication',purpose=request['phase'],run_id=operation_id,expected_max_seconds=600):
            with ExitStack() as stack:
                pg=stack.enter_context(self._pg_connections(locked=True)) if request['phase']=='source' else None
                conn=stack.enter_context(sqlite3.connect(db,timeout=30))
                conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
                self.target(request);candidate=self.build(request,operation_id,conn,pg_connections=pg)
                if digest(candidate)!=preview['candidate_sha256']:raise ValueError('candidate-cas-drift')
                backup=self._backup(runtime,operation_id)
                backup.parent.mkdir(exist_ok=True)
                if backup.exists():raise ValueError('operation-already-submitted-readback-only')
                if request['phase']=='publication':self._materialized(request)
                durable_json(backup,{'operation_id':operation_id,'candidate':candidate,'request':request})
                if digest(json.loads(backup.read_text())['candidate'])!=preview['candidate_sha256']:raise ValueError('backup-verification-failed')
                if request['phase']=='source':
                    self._source_apply(request,operation_id,candidate,runtime,pg)
                else:
                    for update,before in zip(candidate['updates'],candidate['before_images']['ready']):
                        replace_ready(conn,expected=ExpectedReady(update['bundle_version'],update['as_of_date'],before['plan_json']),plan_json=update['after_plan_json'])
                    for o in request['observations']:
                        source,day=o['source_key'],o['snapshot_date'];stamp=o['source_fetched_at']
                        payload=canonical(asdict(source_result(o,request['nm_ids'])))
                        conn.execute('INSERT INTO temporal_source_slot_snapshots(source_key,snapshot_date,snapshot_role,captured_at,payload_json) VALUES(?,?,?,?,?) ON CONFLICT(source_key,snapshot_date,snapshot_role) DO UPDATE SET captured_at=excluded.captured_at,payload_json=excluded.payload_json',
                            (source,day,'accepted_closed_day_snapshot',stamp,payload))
                        conn.execute("INSERT INTO temporal_source_closure_state(source_key,target_date,slot_kind,state,attempt_count,next_retry_at,last_reason,last_attempt_at,last_success_at,accepted_at) VALUES(?,?,?,'success',1,NULL,?,?,?,?) ON CONFLICT(source_key,target_date,slot_kind) DO UPDATE SET state='success',next_retry_at=NULL,last_reason=excluded.last_reason,last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,accepted_at=excluded.accepted_at",
                            (source,day,'yesterday_closed','dated_source_recovery:'+operation_id,stamp,stamp,stamp))
                    if self._publication_state(request,operation_id,candidate,conn)!='after':raise ValueError('publication-staged-state-mismatch')
                conn.commit()
        return {'operation_id':operation_id,'disposition':'submitted'}

    def _retained(self,request,operation_id,runtime):
        retained=json.loads(self._backup(runtime,operation_id).read_text())
        if retained['operation_id']!=operation_id or digest(retained['request'])!=digest(request):
            raise ValueError('operation-request-mismatch')
        return retained['candidate']

    def _publication_state(self,request,operation_id,candidate,conn):
        actual=self.build(request,operation_id,conn)['before_images']
        return 'after' if digest(actual)==digest(candidate['after_images']) else 'before' if digest(actual)==digest(candidate['before_images']) else 'drift'

    def readback(self,request,operation_id):
        runtime,db=self.target(request);backup=self._backup(runtime,operation_id)
        if not backup.exists():return {'operation_id':operation_id,'state':'not_submitted'}
        candidate=self._retained(request,operation_id,runtime)
        if request['phase']=='source':states=self._source_states(request,operation_id,candidate,runtime)
        else:
            with readonly(db) as conn:
                conn.execute('BEGIN');states={'publication':self._publication_state(request,operation_id,candidate,conn)}
        bad=[name+':'+state for name,state in states.items() if state!='after']
        if not bad:
            try:self._materialized(request)
            except Exception as exc:bad.append('materialized:'+type(exc).__name__)
        return {'operation_id':operation_id,'state':'applied' if not bad else 'ambiguous',
            'parts':states,'mismatches':bad,'recovery_reference':str(backup)}

    def rollback(self,request,operation_id):
        """Restore only an exact before/our write-ahead after mixture under CAS."""
        runtime,db=self.target(request)
        candidate=self._retained(request,operation_id,runtime)
        with warehouse_functional_job_lock(runtime,blocking=False),warehouse_sync_lock(runtime,blocking=False), seller_portal_automation_lock(runtime_dir=runtime,owner='web_source_publication',purpose='rollback',run_id=operation_id+'-rollback',expected_max_seconds=600):
            self.target(request)
            if request['phase']=='source':
                with self._pg_connections(locked=True) as connections:
                    states=self._source_states(request,operation_id,candidate,runtime,connections)
                    if any(s not in {'before','after'} for s in states.values()):raise ValueError('rollback-after-state-drift')
                    # Both stores are locked and all parts checked before any write.
                    # A partial recovery remains an exact before/after mixture.
                    for target,conn in connections.items():
                        if states['handoff' if target else 'raw']=='before':continue
                        tables=PG_TABLES[target];before={t:candidate['before_images'][t] for t,_ in tables}
                        self._pg_replace(conn,request,tables,before)
                        if digest(self._pg_read(conn,tables,request))!=digest(before):raise ValueError('rollback-staged-state-mismatch')
                        conn.commit()
                if any(s!='before' for s in self._source_states(request,operation_id,candidate,runtime).values()):
                    raise ValueError('rollback-readback-drift')
            else:
                with sqlite3.connect(db,timeout=30) as conn:
                    conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
                    state=self._publication_state(request,operation_id,candidate,conn)
                    if state=='drift':raise ValueError('rollback-after-state-drift')
                    if state=='after':
                        for after,before in zip(candidate['updates'],candidate['before_images']['ready']):
                            replace_ready(conn,expected=ExpectedReady(after['bundle_version'],after['as_of_date'],after['after_plan_json']),plan_json=before['plan_json'])
                        for source in SOURCE_KEYS:
                            for day in request['dates']:
                                conn.execute("DELETE FROM temporal_source_slot_snapshots WHERE source_key=? AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'",(source,day))
                                conn.execute("DELETE FROM temporal_source_closure_state WHERE source_key=? AND target_date=? AND slot_kind='yesterday_closed'",(source,day))
                        for table,rows in [('temporal_source_slot_snapshots',candidate['before_images']['slots']),('temporal_source_closure_state',candidate['before_images']['closure'])]:
                            for row in rows:
                                if table=='temporal_source_slot_snapshots' and row['snapshot_role']!='accepted_closed_day_snapshot':continue
                                if table=='temporal_source_closure_state' and row['slot_kind']!='yesterday_closed':continue
                                columns=list(row)
                                conn.execute(f'INSERT INTO {table} ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(row[k] for k in columns))
                    if self._publication_state(request,operation_id,candidate,conn)!='before':raise ValueError('rollback-staged-state-mismatch')
                    conn.commit()
        return {'operation_id':operation_id,'state':'restored'}
