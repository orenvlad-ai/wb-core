"""Dated Seller Portal source/publication recovery through the one-submit launcher."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter, readonly, private_json
from apps.seller_portal_web_source_collect import write_source
from apps.seller_portal_automation_guard import seller_portal_automation_lock
from apps.production_apply_contract import AmbiguousSubmit
from packages.adapters.seller_portal_web_source_collector import _dedupe, _funnel_items, _search_items
from packages.adapters.web_source_current_sync import ShellBackedWebSourceCurrentSync, _build_env, _load_env_file, _closed_day_required_fetched_after
from packages.application.ready_publication import ExpectedReady, replace_ready
from packages.application.seller_funnel_snapshot_block import transform_legacy_payload as funnel_payload
from packages.application.web_source_snapshot_block import transform_legacy_payload as search_payload
from packages.application.warehouse_sync_lock import warehouse_sync_lock
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock

SOURCE_KEYS = ('seller_funnel_snapshot','web_source_snapshot')
SKU_METRICS = {'seller_funnel_snapshot':('view_count','open_card_count','ctr'), 'web_source_snapshot':('views_current','ctr_current')}
TOTAL_METRICS = {'seller_funnel_snapshot':('total_view_count','total_open_card_count'), 'web_source_snapshot':('total_views_current','avg_ctr_current')}


def canonical(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',',':'),default=str)


def digest(v):
    return 'sha256:' + hashlib.sha256(canonical(v).encode()).hexdigest()


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

    def _pg_state(self,request):
        out={}
        for target,tables in [(False,[('sales_funnel_daily_raw','snapshot_date'),('search_analytics_raw','date_to')]),
            (True,[('web_source_sales_funnel_daily','snapshot_date'),('web_source_search_analytics_daily','date_to')])]:
            conn=self._pg_connect(target);conn.set_session(readonly=True)
            try:
                with conn.cursor() as cur:
                    for table,col in tables:
                        exact=' AND date_from=date_to' if col=='date_to' else ''
                        cur.execute(f'SELECT * FROM public.{table} WHERE {col}=ANY(%s::date[]){exact} ORDER BY {col},nm_id',(request['dates'],))
                        out[table]=[dict(zip([c.name for c in cur.description],row)) for row in cur.fetchall()]
                conn.rollback()
            finally:conn.close()
        return json.loads(canonical(out))

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

    def build(self,request,operation_id,conn):
        observations=validate_observations(request)
        if request['phase']=='source':
            before=self._pg_state(request)
            return {'operation_id':operation_id,'phase':'source','prestate_sha256':digest(before),
                'before_images':before,'source_sha256':request['source_sha256']}
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
        return {'operation_id':operation_id,'phase':'publication','prestate_sha256':digest(before),
            'before_images':before,'updates':updates,'changes':changes,'source_sha256':request['source_sha256']}

    def preview(self,request,operation_id):
        runtime,db=self.target(request);backup=self._backup(runtime,operation_id)
        if backup.exists():
            retained=json.loads(backup.read_text())
            if digest(retained['request'])!=digest(request):raise ValueError('operation-request-mismatch')
            candidate=retained['candidate']
        else:
            with readonly(db) as conn:candidate=self.build(request,operation_id,conn)
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
            with sqlite3.connect(db,timeout=30) as conn:
                conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
                self.target(request);candidate=self.build(request,operation_id,conn)
                if digest(candidate)!=preview['candidate_sha256']:raise ValueError('candidate-cas-drift')
                backup=self._backup(runtime,operation_id)
                backup.parent.mkdir(exist_ok=True)
                if backup.exists():raise ValueError('operation-already-submitted-readback-only')
                if request['phase']=='publication':self._materialized(request)
                private_json(backup,{'operation_id':operation_id,'candidate':candidate,'request':request})
                if digest(json.loads(backup.read_text())['candidate'])!=preview['candidate_sha256']:raise ValueError('backup-verification-failed')
                if request['phase']=='source':
                    env=_build_env(Path('/opt/wb-web-bot/.env'))
                    for observation in request['observations']:write_source(observation,env=env)
                    sync=ShellBackedWebSourceCurrentSync()
                    for o in request['observations']:
                        source=o['source_key'];day=o['snapshot_date']
                        sync._run(['/opt/wb-ai/venv/bin/python','run_web_source_handoff.py','--only',
                            'sales-funnel' if source=='seller_funnel_snapshot' else 'search-analytics',
                            '--sales-funnel-date' if source=='seller_funnel_snapshot' else '--search-analytics-date-to',day],
                            cwd=Path('/opt/wb-ai'),env=_build_env(Path('/opt/wb-ai/.env')),label='dated source handoff')
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
                conn.commit()
        return {'operation_id':operation_id,'disposition':'submitted'}

    def readback(self,request,operation_id):
        runtime,db=self.target(request);backup=self._backup(runtime,operation_id)
        if not backup.exists():return {'operation_id':operation_id,'state':'not_submitted'}
        retained=json.loads(backup.read_text())
        if digest(retained['request'])!=digest(request):raise ValueError('operation-request-mismatch')
        candidate=retained['candidate'];bad=[]
        try:self._materialized(request)
        except Exception as exc:bad.append(str(exc))
        if request['phase']=='publication':
            with readonly(db) as conn:
                for u in candidate['updates']:
                    row=conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',(u['bundle_version'],u['as_of_date'])).fetchone()
                    if row is None or row[0]!=u['after_plan_json']:bad.append('ready-mismatch:'+u['as_of_date'])
                for o in request['observations']:
                    row=conn.execute("SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key=? AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'",(o['source_key'],o['snapshot_date'])).fetchone()
                    if row is None or row[0]!=canonical(asdict(source_result(o,request['nm_ids']))):bad.append('accepted-source-mismatch')
        return {'operation_id':operation_id,'state':'applied' if not bad else 'ambiguous','mismatches':bad,'recovery_reference':str(backup)}

    def rollback(self,request,operation_id):
        """Restore retained before images only while exact after images still match."""
        runtime,db=self.target(request)
        if self.readback(request,operation_id)['state']!='applied':raise ValueError('rollback-after-state-drift')
        candidate=json.loads(self._backup(runtime,operation_id).read_text())['candidate']
        with warehouse_functional_job_lock(runtime,blocking=False),warehouse_sync_lock(runtime,blocking=False), seller_portal_automation_lock(runtime_dir=runtime,owner='web_source_publication',purpose='rollback',run_id=operation_id+'-rollback',expected_max_seconds=600):
            if self.readback(request,operation_id)['state']!='applied':raise ValueError('rollback-after-state-drift')
            if request['phase']=='source':
                from psycopg2.extras import Json, execute_batch
                # These two stores contain derived, reproducible observations.
                # Restore each exact source date plus its previous handoff image.
                for target,tables in [(False,[('sales_funnel_daily_raw','snapshot_date'),('search_analytics_raw','date_to')]),
                    (True,[('web_source_sales_funnel_daily','snapshot_date'),('web_source_search_analytics_daily','date_to')])]:
                    conn=self._pg_connect(target)
                    try:
                        with conn:
                            with conn.cursor() as cur:
                                for table,col in tables:
                                    exact=' AND date_from=date_to' if col=='date_to' else ''
                                    cur.execute(f'DELETE FROM public.{table} WHERE {col}=ANY(%s::date[]){exact}',(request['dates'],))
                                    rows=candidate['before_images'][table]
                                    if rows:
                                        columns=list(rows[0])
                                        execute_batch(cur,f'INSERT INTO public.{table} ('+','.join(columns)+') VALUES ('+','.join('%s' for _ in columns)+')',
                                            [tuple(Json(r[k]) if k=='raw_json' else r[k] for k in columns) for r in rows])
                    finally:conn.close()
            else:
                with sqlite3.connect(db,timeout=30) as conn:
                    conn.execute('BEGIN IMMEDIATE')
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
                    conn.commit()
        return {'operation_id':operation_id,'state':'restored'}
