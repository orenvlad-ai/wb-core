"""Typed policy-owned dated publication; no accounting receipt authority.

The final command and native source binding remain the original operator truth.
This continuation uses native ready publication intents and the authenticated
History supervisor. It changes only derived cells computed from the same date's
saved inputs. Neither cost/quantity nor archived History objects are rewritten.
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
import hashlib,json,sqlite3,time

from packages.application import operator_policy as op, ready_publication as ready, historical_dated_inputs
from packages.application.historical_dated_inputs import selected, dated_slice, book_lineage, prepared_book_lineage

CONTRACT='operator_policy_dated_publication_v1'
MAX_DATES=366
MAX_BYTES=160*1024**2
MAX_COHORT=32

def code_authority():
    from packages.application import web_vitrina_management_history,calculation_parameters,calculation_parameters_v4,vitrina_economics,vitrina_incident_rematerialization,wb_incident_policy
    return {Path(m.__file__).name:hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in (web_vitrina_management_history,calculation_parameters,calculation_parameters_v4,vitrina_economics,vitrina_incident_rematerialization,wb_incident_policy,historical_dated_inputs)} | {Path(__file__).name:hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

def require(condition,code):
    if not condition:raise ValueError('policy_history_'+code)

def days_between(first,last):
    start,end=date.fromisoformat(first),date.fromisoformat(last)
    require(start<=end and (end-start).days<MAX_DATES,'date_scope_unavailable')
    return [(start+timedelta(days=n)).isoformat() for n in range((end-start).days+1)]

class ProjectionRead:
    """Native incident evaluator's exact read interface, no runtime bootstrap."""
    def __init__(self,conn):self.conn=conn
    def load_temporal_source_snapshot(self,*,source_key,snapshot_date):
        from packages.application.registry_upload_db_backed_runtime import _deserialize_temporal_source_payload
        row=self.conn.execute('SELECT captured_at,payload_json FROM temporal_source_snapshots WHERE source_key=? AND snapshot_date=?',(source_key,snapshot_date)).fetchone()
        return (_deserialize_temporal_source_payload(row['payload_json']),row['captured_at']) if row else (None,None)
    def load_wb_incident_policy_for_date(self,*,seller_id,snapshot_date):
        from packages.application.registry_upload_db_backed_runtime import _wb_incident_policy_row_to_dict
        row=self.conn.execute('SELECT * FROM '+op.NATIVE['wb_incident_policy']+' WHERE seller_id=? AND effective_from<=? ORDER BY revision DESC LIMIT 1',(seller_id,snapshot_date)).fetchone()
        return _wb_incident_policy_row_to_dict(row,seller_id=seller_id)
    load_wb_incident_policy_started_by_date=load_wb_incident_policy_for_date
    def load_latest_wb_incident_policy(self,*,seller_id):
        from packages.application.registry_upload_db_backed_runtime import _wb_incident_policy_row_to_dict
        row=self.conn.execute('SELECT * FROM '+op.NATIVE['wb_incident_policy']+' WHERE seller_id=? ORDER BY revision DESC LIMIT 1',(seller_id,)).fetchone()
        return _wb_incident_policy_row_to_dict(row,seller_id=seller_id)
    def list_sheet_vitrina_user_configs(self,*,config_key):
        return [] # Typed authority requires a real native policy; no legacy fallback.

def scope(conn,command,today):
    native=op.actual_bound(conn,command)
    first=command.source['effective_date']
    if first>today:return []
    dates=days_between(first,today)
    if command.kind=='wb_incident_policy':
        # Ended/disabled intervals still own projection: exact facts must be
        # restored after the removal date, with this same native revision.
        return dates
    return [day for day in dates if (row:=op.current_native(conn,command.kind,day)) and row['version_id']==native['identity']]


def parameters_for_date(conn,day):
    from packages.application.calculation_parameters import _parameters_from_row as parse3,PROXY_BLOCK_KEY
    from packages.application.calculation_parameters_v4 import _parameters_from_row as parse4,PROXY_V4_BLOCK_KEY
    result=[]
    for table,block,parse in ((op.NATIVE['legacy_proxy'],PROXY_BLOCK_KEY,parse3),(op.NATIVE['proxy_v4_tax'],PROXY_V4_BLOCK_KEY,parse4)):
        row=conn.execute('SELECT * FROM '+table+' WHERE block_key=? AND effective_date<=? ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1',(block,day)).fetchone()
        result.append(parse(row) if row else None)
    return tuple(result)

def transform(conn,command,encoded,dates):
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan,_serialize_sheet_vitrina_plan
    from packages.application.web_vitrina_management_history import dated_parameters,recalculate_dated_proxy
    from packages.application.vitrina_incident_rematerialization import _rematerialize_snapshot,_non_target_digest
    payload=json.loads(encoded);before=json.loads(encoded);evidence={}
    for day in dates:
        if command.kind=='wb_incident_policy':
            native=op.binding(conn,command.operation_id,attempt=command.attempt)
            plan,proof=_rematerialize_snapshot(ProjectionRead(conn),snapshot=_deserialize_sheet_vitrina_plan(op.canonical(payload)),target_dates=[day],generated_at=command.source['accepted_history_at'],seller_id=command.source['seller_id'])
            require(proof.get(day,{}).get('status')=='ready' and proof[day].get('policy_revision')==int(native['identity']),'dated_incident_inputs_missing')
            require(_non_target_digest(_deserialize_sheet_vitrina_plan(op.canonical(payload)),target_dates=[day])==_non_target_digest(plan,target_dates=[day]),'non_target_changed')
            payload=json.loads(_serialize_sheet_vitrina_plan(plan));evidence[day]=proof[day]
        else:
            params=parameters_for_date(conn,day);require(params[0] and (command.kind=='legacy_proxy' or params[1]),'dated_parameters_missing')
            result=recalculate_dated_proxy(payload,day=day,parameters=params,operation_id='policy-history:'+command.operation_id)
            sheet=next(s for s in payload['sheets'] if s['sheet_name']=='DATA_VITRINA');index=sheet['header'].index(day);values={r[1]:r[index] for r in sheet['rows'] if len(r)>index}
            metric='proxy_profit_3_rub' if command.kind=='legacy_proxy' else 'proxy_profit_4_rub'
            require(any(key.startswith('SKU:') and key.endswith('|'+metric) for key in values) and 'TOTAL|total_'+metric in values,'dated_proxy_roster_missing')
            def terminal(item):
                scope,metric=item['row_id'].split('|',1)
                if 'margin' not in metric:return False
                if scope=='TOTAL':
                    scopes={key.split('|',1)[0] for key in values if key.endswith('|orderSum') and key.startswith('SKU:')}
                    return bool(scopes) and all(values.get(s+'|orderSum')==0 and values.get(s+'|orderCount')==0 for s in scopes)
                return values.get(scope+'|orderSum')==0 and values.get(scope+'|orderCount')==0
            if params[1] is None:
                # Before the native V4 epoch, a legacy Proxy change owns only
                # Proxy3. Preserve absent V4 policy semantics and old cells.
                original={r[1]:r for r in sheet['rows']}
                cells=payload.get('metadata',{}).get('server_cell_presentation',{})
                revised=result['plan'].get('metadata',{}).get('server_cell_presentation',{})
                for row in next(s for s in result['plan']['sheets'] if s['sheet_name']=='DATA_VITRINA')['rows']:
                    if 'proxy_' in row[1] and ('_4' in row[1] or 'per_unit' in row[1]):
                        row[index]=original[row[1]][index]
                        if day in cells.get(row[1],{}):revised.setdefault(row[1],{})[day]=deepcopy(cells[row[1]][day])
                        else:revised.get(row[1],{}).pop(day,None)
                result['remaining']=[item for item in result['remaining'] if '_4' not in item['row_id'] and 'per_unit' not in item['row_id']]
            require(all(terminal(item) for item in result['remaining']),'dated_proxy_inputs_missing')
            require(result['changes'] or any(day in s['header'] for s in payload['sheets'] if s['sheet_name']=='DATA_VITRINA'),'dated_proxy_roster_missing')
            payload=result['plan'];evidence[day]={'proxy3_version':params[0].version_id,'proxy3_fingerprint':params[0].fingerprint,'proxy4_version':params[1].version_id if params[1] else None,'proxy4_fingerprint':params[1].fingerprint if params[1] else None}
    if command.kind!='wb_incident_policy':
        # Reject broad evaluator changes, including any current cost/quantity
        # substitution. Only its documented Proxy/dated economics outputs may
        # differ; all original operands and other dates must remain exact.
        from packages.application.web_vitrina_management_history import TARGET_METRICS
        allowed=set(TARGET_METRICS)-{'our_wb_unit_cost_rub','total_our_wb_unit_cost_rub'}
        from packages.application.vitrina_economics import METRICS, TOTALS
        allowed|=set(METRICS)|set(TOTALS)
        def fence(value):
            value=json.loads(op.canonical(value));meta=value.setdefault('metadata',{});cells=meta.get('server_cell_presentation',{})
            for sheet in value['sheets']:
                if sheet['sheet_name']!='DATA_VITRINA':continue
                for row in sheet['rows']:
                    if row[1].split('|',1)[-1] in allowed:
                        for day in dates:
                            if day in sheet['header'] and len(row)>sheet['header'].index(day):row[sheet['header'].index(day)]='<policy>'
                        for day in dates:cells.get(row[1],{}).pop(day,None)
            for key in list(cells):
                if not cells[key]:cells.pop(key)
            coverage=meta.get('catalog_economics_coverage',{})
            for day in dates:coverage.pop(day,None)
            if not coverage:meta.pop('catalog_economics_coverage',None)
            return value
        require(fence(before)==fence(payload),'non_target_changed')
    return op.canonical(payload),evidence

def _inputs(conn,command,dates):
    queries=[('SELECT operation_id,kind,actor,request_digest,accepted_at,effective_date,source_json,source_digest FROM '+op.TABLE+' WHERE operation_id=?',(command.operation_id,)),('SELECT * FROM '+op.BINDINGS+' WHERE operation_id=? AND role IN (\'source\',\'recovery\') ORDER BY attempt,role',(command.operation_id,))]
    for day in dates:
        if command.kind=='wb_incident_policy':
            queries.append(('SELECT * FROM '+op.NATIVE[command.kind]+' WHERE seller_id=? AND effective_from<=? ORDER BY revision DESC LIMIT 1',(command.source['seller_id'],day)))
            queries.append(("SELECT * FROM temporal_source_snapshots WHERE source_key='stocks' AND snapshot_date=?",(day,)))
        else:
            from packages.application.calculation_parameters import PROXY_BLOCK_KEY
            from packages.application.calculation_parameters_v4 import PROXY_V4_BLOCK_KEY
            for kind,block in (('legacy_proxy',PROXY_BLOCK_KEY),('proxy_v4_tax',PROXY_V4_BLOCK_KEY)):
                queries.append(('SELECT * FROM '+op.NATIVE[kind]+' WHERE block_key=? AND effective_date<=? ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1',(block,day)))
    return ready.pin_queries(conn,queries)



def prepare(runtime,command,*,today,created_at):
    from dataclasses import replace
    # The deterministic publisher clock is volatile continuation data, never
    # a mutation of the original authorized source/date.
    working=replace(command,source={**command.source,'accepted_history_at':created_at})
    with ready.readonly(runtime.db_path) as conn:
        native=op.actual_bound(conn,command);dates=scope(conn,command,today)
        require(dates,'no_effective_dates')
        targets=[]
        for row,days in selected(conn,dates,today):
            after,evidence=transform(conn,working,row['plan_json'],days)
            targets.append({'expected':asdict(ready.ExpectedReady(row['bundle_version'],row['as_of_date'],row['plan_json'])),'after_json':after,'dates':days,'evidence':evidence,'dated_outputs':{day:op.digest(dated_slice(after,day)) for day in days}})
        with prepared_book_lineage(runtime,[(t['after_json'],t['dates']) for t in targets]) as books:
            for target in targets:target['dated_book_lineage']={day:book_lineage(runtime,target['after_json'],day,prepared=books) for day in target['dates']}
            books.guard()
        inputs=_inputs(conn,command,dates)
    result={'contract':CONTRACT,'operation_id':'policy-history:'+command.operation_id,'attempt_id':str(command.attempt),'command_id':command.operation_id,'command_attempt':command.attempt,'kind':command.kind,'native':native,'dates':dates,'today':today,'created_at':created_at,'targets':targets,'source_inputs':inputs,'code_authority':code_authority()}
    require(len(op.canonical(result).encode())<=MAX_BYTES,'publication_size_limit')
    result['manifest_digest']=op.digest(result)
    return result

def verify_manifest(manifest):
    require(manifest.get('contract')==CONTRACT and manifest.get('manifest_digest')==op.digest({k:v for k,v in manifest.items() if k!='manifest_digest'}),'manifest_corrupt')
    require(manifest.get('operation_id')=='policy-history:'+manifest['command_id'] and manifest.get('attempt_id')==str(manifest['command_attempt']) and manifest.get('kind') in op.KINDS,'native_identity_changed')
    require(manifest['dates']==sorted(set(manifest['dates'])) and 0<len(manifest['dates'])<=MAX_DATES,'date_scope_invalid')
    require(sorted(day for target in manifest['targets'] for day in target['dates'])==manifest['dates'],'date_coverage_invalid')

def publish(runtime,manifest):
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
    require(require_heavy_owner(runtime.runtime_dir).operation=='cycle','cycle_owner_required')
    verify_manifest(manifest)
    require(manifest['code_authority']==code_authority(),'formula_changed')
    from packages.application.fbs_accounting_runtime import writer_lock
    with prepared_book_lineage(runtime,[(t['after_json'],t['dates']) for t in manifest['targets']]) as books,warehouse_functional_write_lock(runtime.runtime_dir),writer_lock(runtime.runtime_dir),closing(sqlite3.connect(runtime.db_path)) as conn:
        conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE');books.guard()
        require(op.digest(ready.pin_queries(conn,[(sql,params) for sql,params,_ in manifest['source_inputs']]))==op.digest(manifest['source_inputs']),'source_inputs_changed')
        row=conn.execute('SELECT * FROM '+op.TABLE+' WHERE operation_id=?',(manifest['command_id'],)).fetchone();require(row,'command_missing')
        command=op.command_from_row(row,manifest['command_attempt'])
        require(op.actual_bound(conn,command)==manifest['native'],'native_source_changed')
        require(scope(conn,command,manifest['today'])==manifest['dates'],'affected_date_scope_changed')
        from dataclasses import replace
        working=replace(command,source={**command.source,'accepted_history_at':manifest['created_at']})
        for target in manifest['targets']:
            expected=ready.ExpectedReady(**target['expected']);ready.check_expected(conn,expected)
            rebuilt,evidence=transform(conn,working,expected.plan_json,target['dates'])
            require(rebuilt==target['after_json'] and evidence==target['evidence'] and target['dated_outputs']=={day:op.digest(dated_slice(rebuilt,day)) for day in target['dates']} and target['dated_book_lineage']=={day:book_lineage(runtime,rebuilt,day,prepared=books) for day in target['dates']},'independent_rebuild_mismatch')
        # Schema belongs to normal source/bootstrap, never read GET or apply.
        require(op.exists(conn,'sheet_vitrina_v1_ready_publications'),'native_publication_schema_missing')
        ready.record_intent(conn,operation_id=manifest['operation_id'],attempt_id=manifest['attempt_id'],kind=CONTRACT,expected=None,inputs=manifest,expected_book=None,book_required=False,ready_required=True,created_at=manifest['created_at'])
        for target in manifest['targets']:ready.replace_ready(conn,expected=ready.ExpectedReady(**target['expected']),plan_json=target['after_json'])
        ready.complete_publication(conn,operation_id=manifest['operation_id'],attempt_id=manifest['attempt_id'],book_version=None,after_digest=op.digest([op.digest(t['after_json']) for t in manifest['targets']]),finished_at=manifest['created_at'])
        books.guard();conn.commit()
    return PolicyHistory(runtime,manifest['operation_id'],manifest['attempt_id'])

class PolicyHistory:
    """Only this exact source-owned native publication enrolls old History dates."""
    def __init__(self,runtime,operation_id,attempt_id):
        self.runtime=runtime;self.operation_id=operation_id;self.attempt_id=attempt_id
        self.manifest=self._read()[0];verify_manifest(self.manifest)
        self.dates=tuple(self.manifest['dates'])
    def _read(self):
        with ready.readonly(self.runtime.db_path) as conn:
            row=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(self.operation_id,self.attempt_id)).fetchone()
            require(row and row['kind']==CONTRACT and row['state']=='complete' and len(row['inputs_json'].encode())<=MAX_BYTES,'publication_not_complete')
            manifest=json.loads(row['inputs_json']);verify_manifest(manifest)
            require(manifest['operation_id']==self.operation_id and manifest['attempt_id']==self.attempt_id,'receipt_identity_changed')
            require(row['after_digest']==op.digest([op.digest(t['after_json']) for t in manifest['targets']]),'native_publication_digest_changed')
            return manifest,json.loads(row['diagnostics_json'])
    def binding(self):
        require(self._read()[0]==self.manifest,'receipt_changed')
        return {'operation_id':self.operation_id,'attempt_id':self.attempt_id,'digest':self.manifest['manifest_digest'],'dates':list(self.dates)}
    def publication_dates(self):return self.dates
    def _validate_sources_conn(self,conn,*,books=None):
        if books is None:
            require(conn.execute('PRAGMA query_only').fetchone()[0]==1,'dated_book_cache_required')
            pairs=selected(conn,list(self.dates),self.manifest['today'])
            with prepared_book_lineage(self.runtime,[(row['plan_json'],days) for row,days in pairs]) as prepared:
                return self._validate_sources_conn(conn,books=prepared)
        books.guard()
        manifest=self.manifest
        require(manifest['code_authority']==code_authority(),'formula_changed')
        row=conn.execute('SELECT * FROM '+op.TABLE+' WHERE operation_id=?',(manifest['command_id'],)).fetchone()
        require(row,'command_missing');command=op.command_from_row(row,manifest['command_attempt'])
        require(op.actual_bound(conn,command)==manifest['native'],'native_source_changed')
        require(scope(conn,command,manifest['today'])==manifest['dates'],'affected_date_scope_changed')
        require(op.digest(ready.pin_queries(conn,[(sql,params) for sql,params,_ in manifest['source_inputs']]))==op.digest(manifest['source_inputs']),'source_inputs_changed')
        pairs=selected(conn,list(self.dates),manifest['today']);proof={}
        for row,days in pairs:
            for day in days:
                target=next((t for t in manifest['targets'] if day in t['dates']),None)
                require(target and op.digest(dated_slice(row['plan_json'],day))==target['dated_outputs'][day],'dated_ready_changed')
                require(book_lineage(self.runtime,row['plan_json'],day,prepared=books)==target['dated_book_lineage'][day],'dated_book_lineage_changed')
                proof[day]={'ready_key':[row['bundle_version'],row['as_of_date']],'ready_digest':ready.digest(row['plan_json']),'dated_digest':target['dated_outputs'][day],'input_evidence':target['evidence'][day]}
        books.guard()
        return {'binding':{'operation_id':self.operation_id,'attempt_id':self.attempt_id,'digest':manifest['manifest_digest'],'dates':list(self.dates)},'dated':proof}
    def validate_sources_readonly(self,*,now):
        with ready.readonly(self.runtime.db_path) as conn:proof=self._validate_sources_conn(conn)
        require(self._read()[0]==self.manifest,'receipt_changed')
        return proof
    def validate_native_readonly(self,*,adapter,store,now):
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
        from packages.application.web_vitrina_history_store import HistoryStore
        from packages.application.owned_history_native_ack import _object
        require(type(adapter) is LiveNativeAdapter and type(store) is HistoryStore and adapter.db_path==Path(self.runtime.db_path).resolve() and adapter.runtime_dir==Path(self.runtime.runtime_dir).resolve(),'native_context_changed')
        before=self.validate_sources_readonly(now=now);vector=adapter.capture();fence=adapter.fence;pointer=store._current();require(pointer,'history_current_missing');edition=store.edition();proofs=store.day_proofs(edition);native={}
        for day in self.dates:
            require(day in edition['days'] and proofs.get(day)=={'epoch':vector['epoch'],'token':vector['dates'].get(day)},'native_date_pending')
            _object(store,day,edition['days'][day],store.day_catalogs(edition)[day],time.monotonic()+30)
            native[day]={'edition_id':pointer['current'],'object_digest':edition['days'][day],**proofs[day],**before['dated'][day]}
        require(self.validate_sources_readonly(now=now)==before and adapter.capture()==vector and adapter.fence==fence and store._current()==pointer,'changed_during_ack')
        return {'receipt_digest':self.manifest['manifest_digest'],'current':pointer,'vector':vector,'fence':fence,'native':native}
    def _acknowledge_verified_native(self,proof):
        from packages.application.owned_history_native_ack import consume_policy_history_ack
        native=consume_policy_history_ack(proof,self.runtime.runtime_dir,self.manifest['manifest_digest'],self.dates)
        manifest=self.manifest
        from packages.application.fbs_accounting_runtime import writer_lock
        from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
        with ready.readonly(self.runtime.db_path) as read:
            pairs=selected(read,list(self.dates),manifest['today'])
        with prepared_book_lineage(self.runtime,[(row['plan_json'],days) for row,days in pairs]) as books,warehouse_functional_write_lock(self.runtime.runtime_dir),writer_lock(self.runtime.runtime_dir),closing(sqlite3.connect(Path(self.runtime.db_path).as_uri()+'?mode=rw',uri=True,timeout=0)) as conn:
            conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE');books.guard()
            row=conn.execute('SELECT * FROM '+op.TABLE+' WHERE operation_id=?',(manifest['command_id'],)).fetchone();require(row,'command_missing')
            command=op.command_from_row(row,manifest['command_attempt']);require(op.actual_bound(conn,command)==manifest['native'],'native_source_changed')
            publication=conn.execute('SELECT inputs_json,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=? AND state=\'complete\'',(self.operation_id,self.attempt_id)).fetchone()
            require(publication and json.loads(publication['inputs_json'])==manifest,'receipt_changed')
            # Repeat relevant source and selected-ready pins under the final
            # writer CAS. Pre-transaction global stamps are insufficient.
            current=self._validate_sources_conn(conn,books=books)['dated']
            require(set(current)==set(native) and all(all(native[day].get(key)==value for key,value in current[day].items()) for day in current),'ack_source_changed')
            diagnostics=json.loads(publication['diagnostics_json'])
            ack={'manifest_digest':manifest['manifest_digest'],'native':{d:dict(v) for d,v in native.items()}}
            diagnostics['policy_history_ack']=ack
            proof_value={'consumer':CONTRACT,'exact_version':manifest['native']['identity'],'readback_verified':True,'native_operation_id':self.operation_id,'native_attempt_id':self.attempt_id,'manifest_digest':manifest['manifest_digest'],'dates':list(self.dates),'native_history':ack['native']}
            old=op.binding(conn,manifest['command_id'],'publication',manifest['command_attempt']);require(not old or old==proof_value,'operator_proof_conflict')
            require(conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=? AND diagnostics_json=?',(op.canonical(diagnostics),self.operation_id,self.attempt_id,publication['diagnostics_json'])).rowcount==1,'ack_cas_failed')
            if not old:op.add_binding(conn,command,'publication',proof_value)
            bound=manifest['native']
            if bound.get('queue_id'):
                require(conn.execute("UPDATE sheet_vitrina_v1_proxy_targeted_recalc_queue SET status='complete',completed_at=?,error=NULL WHERE request_id=? AND settings_version_id=?",(manifest['created_at'],bound['queue_id'],bound['identity'])).rowcount==1,'exact_queue_missing')
            conn.execute('UPDATE '+op.TABLE+" SET state='completed',reason_code='',progress_json='{}' WHERE operation_id=?",(manifest['command_id'],));books.guard();conn.commit()

def publication_proof_valid(conn,native,proof,*,operation_id=None):
    if not native or proof.get('consumer')!=CONTRACT or str(proof.get('exact_version'))!=str(native['identity']) or proof.get('readback_verified') is not True:return False
    row=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(proof.get('native_operation_id'),proof.get('native_attempt_id'))).fetchone()
    if not row or row['kind']!=CONTRACT or row['state']!='complete':return False
    manifest=json.loads(row['inputs_json']);verify_manifest(manifest)
    if operation_id is None or manifest['command_id']!=operation_id:return False
    kind=manifest['kind'];table=op.NATIVE[kind]
    if kind=='wb_incident_policy':source=conn.execute('SELECT * FROM '+table+' WHERE seller_id=? AND revision=?',(native['row']['seller_id'],native['identity'])).fetchone()
    else:source=conn.execute('SELECT * FROM '+table+' WHERE version_id=?',(native['identity'],)).fetchone()
    if not source or op.digest(dict(source))!=op.digest(native['row']):return False
    ack=json.loads(row['diagnostics_json']).get('policy_history_ack',{})
    dates=manifest['dates'];actual=ack.get('native',{})
    return bool(manifest['native']==native and proof.get('manifest_digest')==manifest['manifest_digest']==ack.get('manifest_digest') and proof.get('dates')==dates and proof.get('native_history')==actual and set(actual)==set(dates) and all(actual[day].get('object_digest') and actual[day].get('edition_id') and actual[day].get('input_evidence')==next(t['evidence'][day] for t in manifest['targets'] if day in t['dates']) and actual[day].get('dated_digest')==next(t['dated_outputs'][day] for t in manifest['targets'] if day in t['dates']) for day in dates))

def pending(runtime,*,now):
    """Fair bounded scan, at most one own publication per existing cycle.

    Existing operator progress carries the last check; missing dated operands
    never monopolize the consumer. A foreign native revision gets no authority.
    """
    from packages.business_time import current_business_date_iso
    today=current_business_date_iso(now)
    with ready.readonly(runtime.db_path) as conn:
        if not op.exists(conn):return None
        rows=conn.execute('SELECT * FROM '+op.TABLE+" WHERE state IN ('native_applied','needs_attention') AND reason_code!='native_source_drift' AND effective_date<? AND NOT EXISTS (SELECT 1 FROM "+op.BINDINGS+" b WHERE b.operation_id="+op.TABLE+".operation_id AND b.role IN ('publication','recovery') AND b.attempt=(SELECT max(s.attempt) FROM "+op.BINDINGS+" s WHERE s.operation_id=b.operation_id AND s.role='source')) ORDER BY coalesce(json_extract(progress_json,'$.history_last_checked_at'),''),accepted_at,operation_id LIMIT ?",(today,MAX_COHORT)).fetchall()
        candidates=[]
        for row in rows:
            attempt=conn.execute('SELECT max(attempt) FROM '+op.BINDINGS+" WHERE operation_id=? AND role='source'",(row['operation_id'],)).fetchone()[0]
            if not attempt:continue
            identity='policy-history:'+row['operation_id']
            prior=conn.execute('SELECT state FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=? AND kind=?',(identity,str(attempt),CONTRACT)).fetchone()
            candidates.append((dict(row),op.command_from_row(row,attempt),identity,prior is not None))
    for row,command,identity,prior in candidates:
        progress={**json.loads(row['progress_json']),'history_last_checked_at':now.isoformat(),'original_effective_date':command.source['effective_date'],'date_to':today}
        op.progress(runtime,command.operation_id,state=row['state'],reason=row['reason_code'],data=progress)
        try:
            if prior:
                receipt=PolicyHistory(runtime,identity,str(command.attempt));receipt.validate_sources_readonly(now=now);return receipt
            manifest=prepare(runtime,command,today=today,created_at=now.isoformat())
        except ValueError as exc:
            reason=str(exc)
            # The native source owner checks immutable accepted -> actual bound
            # equality. Supersession cannot enroll an old receipt in History.
            if reason=='policy_history_formula_changed':
                op.progress(runtime,command.operation_id,state='needs_attention',reason=reason,data=progress);continue
            if reason=='operator_policy_source_version_changed':
                op.progress(runtime,command.operation_id,state='needs_attention',reason='native_source_drift',data={**progress,'error':reason});continue
            if any(code in reason for code in ('dated_ready_inputs_missing','dated_proxy_inputs_missing','dated_proxy_roster_missing','dated_incident_inputs_missing','date_scope_unavailable','dated_parameters_missing','source_inputs_changed','dated_ready_changed','affected_date_scope_changed','no_effective_dates')):
                op.progress(runtime,command.operation_id,state='needs_attention',reason=reason,data=progress);continue
            raise
        return publish(runtime,manifest)
    return None
