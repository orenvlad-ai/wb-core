"""Operator projection over native facility audits, dense intents and FBS bindings.

No new receipt table, queue or accounting authority. Native writers retain the
exact correlation/proof alongside their own source commit; GET never bootstraps.
"""
from contextlib import closing
from contextvars import ContextVar
import fcntl,json,re
from pathlib import Path
from uuid import uuid4
from packages.application import operator_supplier_shipments as common
from packages.application.ff_pool_foundation import FACILITIES_TABLE,FACILITY_PROFILES_TABLE,FACILITY_CHANGES_TABLE
from packages.application.ff_pool_fbs_applicability import DENSE_INTENTS_TABLE,DENSE_INTENT_EVENTS_TABLE

DOMAIN='facility_mapping'
ACTIONS=frozenset({'create','update','activate','binding'})
BINDINGS='sheet_vitrina_v1_wb_fbs_binding_confirmations'
META='_operator_acceptance'
_ACTIVE=ContextVar('operator_facility_mapping',default=None)


def source_only_context():
    return _ACTIVE.get() is not None


def facility_source(conn,facility_id):
    row=conn.execute(f'SELECT * FROM {FACILITIES_TABLE} WHERE facility_id=?',(facility_id,)).fetchone()
    profile=conn.execute(f'SELECT * FROM {FACILITY_PROFILES_TABLE} WHERE facility_id=?',(facility_id,)).fetchone()
    return {'facility':dict(row) if row else {},'profile':dict(profile) if profile else {}}


def guard_facility(conn,facility_id):
    ctx=_ACTIVE.get()
    if not ctx or ctx['action'] not in {'update','activate','binding'}:return
    current=facility_source(conn,facility_id)
    if facility_id!=ctx['entity_id'] or common.digest(current)!=ctx['before_digest']:
        raise ValueError('facility source changed before saving this exact request')


def _metadata(conn,*,entity_id,kind,evidence,accepted_at):
    ctx=_ACTIVE.get()
    if not ctx:return None
    proof={'kind':kind,'entity_id':entity_id,**evidence}
    return {**ctx,'entity_id':entity_id,'proof':proof,'proof_digest':common.digest(proof),'accepted_at':accepted_at}


def audit_current(conn,*,facility_id,current,changed_at,request_id,request_identity,action):
    ctx=_ACTIVE.get()
    if not ctx:return dict(current)
    return {**current,META:_metadata(conn,entity_id=facility_id,kind='facility_audit',
        evidence={'before':ctx['before'],'after':facility_source(conn,facility_id),
                  'native_ref':{'request_id':request_id,'request_identity':request_identity,'action':action}},accepted_at=changed_at)}


def dense_staged_receipt(conn,intent_id,receipt,recorded_at):
    ctx=_ACTIVE.get()
    if not ctx or ctx['action']!='activate':return dict(receipt)
    intent=dict(conn.execute(f'SELECT * FROM {DENSE_INTENTS_TABLE} WHERE intent_id=?',(intent_id,)).fetchone())
    guard_facility(conn,intent['subject_id'])
    return {**receipt,META:_metadata(conn,entity_id=intent['subject_id'],kind='facility_activation',
        evidence={'before':ctx['before'],'after':facility_source(conn,intent['subject_id']),'intent':intent},accepted_at=recorded_at)}


def binding_receipt(conn,request,mapping_id,confirmed_at):
    from packages.application.wb_fbs_orders import WAREHOUSE_MAPPINGS_TABLE
    ctx=_ACTIVE.get()
    if not ctx:return '{}'
    mapping=dict(conn.execute(f'SELECT * FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE mapping_id=?',(mapping_id,)).fetchone())
    if ctx['entity_id']:
        guard_facility(conn,mapping['facility_id'])
    return common._json(_metadata(conn,entity_id=mapping['facility_id'],kind='official_binding',
        evidence={'request':dict(request),'mapping':mapping},accepted_at=confirmed_at))


def _records(conn,*,request_scope,request_id='',operation_id='',all_for_scope=False):
    selected=[]
    for table,column,path in ((FACILITY_CHANGES_TABLE,'current_json','$.'+META),
                              (DENSE_INTENT_EVENTS_TABLE,'receipt_json','$.'+META),
                              (BINDINGS,'operator_receipt_json','$')):
        if not common._exists(conn,table) or column not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:continue
        key='operation_id' if operation_id else 'request_id';identity=operation_id or request_id
        safe_json=f"CASE WHEN json_valid({column}) THEN {column} ELSE '{{}}' END"
        if all_for_scope:
            rows=conn.execute(f"SELECT * FROM {table} WHERE json_extract({safe_json},?)=? AND json_extract({safe_json},?)='accepted' AND json_extract({safe_json},?) IN ({','.join('?' for _ in ACTIONS)})",
                (path+'.request_scope',request_scope,path+'.status',path+'.action',*sorted(ACTIONS))).fetchall()
        else:
            rows=conn.execute(f"SELECT * FROM {table} WHERE json_extract({safe_json},?)=? AND json_extract({safe_json},?)=?",
                (path+'.request_scope',request_scope,path+'.'+key,identity)).fetchall()
        for row in rows:
            value=json.loads(row[column]);meta=value if table==BINDINGS else value.get(META)
            if not isinstance(meta,dict) or not isinstance(meta.get('proof'),dict) or meta.get('action') not in ACTIONS or common.digest(meta['proof'])!=meta.get('proof_digest'):continue
            proof=meta['proof']
            if table==FACILITY_CHANGES_TABLE:
                ref=proof.get('native_ref') or {}
                if (proof['kind']!='facility_audit' or proof['entity_id']!=row['facility_id']
                    or any(ref.get(key)!=row[key] for key in ('request_id','request_identity','action'))):continue
            elif table==DENSE_INTENT_EVENTS_TABLE:
                intent=proof.get('intent') or {}
                stored=conn.execute(f'SELECT * FROM {DENSE_INTENTS_TABLE} WHERE intent_id=?',(row['intent_id'],)).fetchone()
                if proof['kind']!='facility_activation' or row['state']!='staged' or not _valid_event(row) or not stored or dict(stored)!=intent:continue
            else:
                request=proof.get('request') or {};mapping=proof.get('mapping') or {}
                if proof['kind']!='official_binding' or request.get('request_id')!=row['request_id'] or mapping.get('mapping_id')!=row['mapping_id']:continue
            selected.append(meta)
    unique={m['operation_id']:m for m in sorted(selected,key=lambda value:(value['operation_id'],value['proof_digest']))}
    return list(unique.values())


def _valid_event(row):
    if not row:return None
    try:value=json.loads(row['receipt_json'])
    except (ValueError,TypeError):return None
    return value if isinstance(value,dict) and common.digest(value)==row['receipt_fingerprint'] else None


def _public(conn,meta):
    result={'domain':DOMAIN,'request_id':meta['request_id'],'action':meta['action'],'wire_digest':meta['wire_digest'],
            'status':meta['status'],'settled':True,'acceptance':None}
    if meta['status']=='rejected':result['error']=meta['error'];return result
    proof=meta['proof'];kind=proof['kind'];state='completed';reason='Изменение справочника сохранено.'
    processing={'kind':'source_only','complete':True,'cost_applicable':False}
    if kind=='facility_activation':
        intent=proof['intent'];plan=json.loads(intent['plan_json'])
        if common.digest(plan)!=intent['plan_fingerprint']:return {'domain':DOMAIN,'status':'unknown','settled':False,'acceptance':None}
        active=conn.execute(f"SELECT * FROM {DENSE_INTENT_EVENTS_TABLE} WHERE intent_id=? AND state='active' ORDER BY event_sequence DESC LIMIT 1",(intent['intent_id'],)).fetchone()
        materialized=conn.execute(f"SELECT * FROM {DENSE_INTENT_EVENTS_TABLE} WHERE intent_id=? AND state='materialized' ORDER BY event_sequence DESC LIMIT 1",(intent['intent_id'],)).fetchone()
        completed=_valid_event(active);coverage=_valid_event(materialized)
        audit=conn.execute(f"SELECT * FROM {FACILITY_CHANGES_TABLE} WHERE request_id=? AND action='activated' AND facility_id=? AND request_identity=?",
            (intent['orchestration_key'][len('facility:'):-len(':dense-fbs')],meta['entity_id'],intent['request_identity'])).fetchone()
        native_active=bool(audit and completed and audit['changed_at']==completed.get('activated_at') and json.loads(audit['current_json']).get('active') is True)
        complete=bool(native_active and coverage and completed.get('facility_id')==meta['entity_id'] and completed.get('coverage_fingerprint')==coverage.get('fingerprint')
                      and common.digest({k:v for k,v in coverage.items() if k!='fingerprint'})==coverage.get('fingerprint') and coverage.get('complete') is True)
        if complete:
            from packages.application.ff_pool_documents import REQUESTS_TABLE
            for specification in plan.get('documents') or []:
                request=conn.execute(f'SELECT state,source_revision,posted_document_id FROM {REQUESTS_TABLE} WHERE client_request_id=? OR request_id=? ORDER BY request_id LIMIT 1',
                    (specification['request_id'],specification['request_id'])).fetchone()
                if not request or request['state']!='complete' or request['source_revision']!=intent['plan_fingerprint'] or not request['posted_document_id']:
                    complete=False;break
        processing={'kind':'native_dense_facility_activation','complete':complete,'cost_applicable':False,
            'intent_id':intent['intent_id'],'plan_fingerprint':intent['plan_fingerprint'],'published_active':complete}
        state='completed' if complete else 'processing';reason='Склад активирован.' if complete else 'Запрос сохранён. Склад ожидает активации.'
        if not complete:
            blocked=conn.execute(f"SELECT * FROM {DENSE_INTENT_EVENTS_TABLE} WHERE intent_id=? AND state='blocked' ORDER BY event_sequence DESC LIMIT 1",(intent['intent_id'],)).fetchone()
            if _valid_event(blocked):state='needs_attention';reason='Запрос сохранён. Активация склада требует внимания.';processing['terminal']=True
    elif kind=='official_binding':reason='Точная связь со складом WB сохранена. Остатки не изменены.'
    elif proof['before']==proof['after']:reason='Проверка сохранена. Значения не изменились.';processing['effect']='unchanged'
    result['acceptance']={'domain':DOMAIN,'operation_id':meta['operation_id'],'durable_saved':True,'accepted_at':meta['accepted_at'],
        'state':state,'actor':meta['actor'],'primary_effect':'source_saved','calculation_completed':False,'title_ru':'Склад и связь FBS',
        'source_ref':{'domain':DOMAIN,'entity_id':meta['entity_id'],'action':meta['action'],'source_digest':meta['proof_digest']},
        'processing':processing,'reason_ru':reason,'fields':[{'label':'Склад','value':meta['entity_id']}],
        'detail_path':'/v1/sheet-vitrina-v1/web-vitrina?operation_id='+meta['operation_id']}
    return result


def read(db_path,*,request_scope,request_id='',operation_id=''):
    unknown={'domain':DOMAIN,'request_id':request_id,'status':'unknown','settled':False,'acceptance':None}
    if not Path(db_path).is_file():return unknown
    with closing(common.readonly(db_path)) as conn:
        found=_records(conn,request_scope=request_scope,request_id=request_id,operation_id=operation_id)
        return _public(conn,found[0]) if len(found)==1 else unknown


def journal_entries(db_path, *, request_scope):
    """Root journal calls only after native Supply grant, before count/search/page.

    Principal and this explicit action family are SQL predicates. Multiple native
    field audit rows represent one source operation; legacy rows are not backfilled.
    """
    if not Path(db_path).is_file():return []
    with closing(common.readonly(db_path)) as conn:
        projected=[_public(conn,meta) for meta in _records(conn,request_scope=request_scope,all_for_scope=True)]
        return [value['acceptance'] for value in projected if value.get('acceptance') and value['acceptance'].get('durable_saved') is True]


def execute(owner,*,action,payload,actor,request_scope,entity_id='',native_write):
    identity=str(payload.get('request_id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,120}',identity) or action not in ACTIONS:raise ValueError('exact facility request identity required')
    operands=common.source_payload(payload);wire=common.verified_wire_digest(payload);fingerprint=common.digest({'action':action,'entity_id':entity_id,'operands':operands})
    owner.runtime_dir.mkdir(parents=True,exist_ok=True)
    with (owner.runtime_dir/'.operator-facility-source.lock').open('a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        with closing(common.readonly(owner.db_path)) as conn:
            prior=_records(conn,request_scope=request_scope,request_id=identity)
            if prior:
                if len(prior)!=1 or prior[0]['payload_digest']!=fingerprint or prior[0]['wire_digest']!=wire:raise ValueError('facility request identity already belongs to another source')
                return _public(conn,prior[0])
            before=facility_source(conn,entity_id) if entity_id else {}
        if payload.get('expected_source_digest') and payload['expected_source_digest']!=common.digest(before):
            error=ValueError('facility source changed after the form was opened')
        else:error=None
        ctx={'request_id':identity,'operation_id':'ff_directory_'+uuid4().hex,'request_scope':request_scope,'actor':actor,'action':action,
            'payload_digest':fingerprint,'wire_digest':wire,'entity_id':entity_id,'before':before,'before_digest':common.digest(before),'status':'accepted'}
        token=_ACTIVE.set(ctx)
        try:
            try:
                if error:raise error
                native_write()
            except ValueError as exc:
                current=read(owner.db_path,request_scope=request_scope,request_id=identity)
                if current['status']=='accepted':return current
                ctx.update(status='rejected',error=str(exc).replace('\n',' ')[:500])
                # Only a known native facility can retain a no-effect refusal audit.
                # No surrogate source is created for absent facilities/previews.
                if before and before.get('facility'):
                    from packages.application.ff_pool_surfaces import _connect_write
                    with _connect_write(owner.db_path) as conn:
                        conn.execute('BEGIN IMMEDIATE');current_source=facility_source(conn,entity_id)
                        ctx.update(before=current_source,before_digest=common.digest(current_source))
                        facility=current_source['facility']
                        owner._append_facility_change(conn,request_id=identity,request_identity=fingerprint,facility_id=entity_id,
                            action='unchanged',actor=actor,previous=facility,current=facility,changed_at=owner._now());conn.commit()
                    return read(owner.db_path,request_scope=request_scope,request_id=identity)
                return {'domain':DOMAIN,'request_id':identity,'action':action,'wire_digest':wire,'status':'rejected','settled':True,'acceptance':None,'error':ctx['error']}
            return read(owner.db_path,request_scope=request_scope,request_id=identity)
        finally:_ACTIVE.reset(token)
