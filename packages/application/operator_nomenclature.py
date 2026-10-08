"""Immutable operator source receipts; native activation and Finance own execution.

This is a source-version adapter, not another catalog or recalculation queue.
Reads never bootstrap schemas. A request's actor, action and semantic digest are immutable.
"""
from contextlib import closing
from pathlib import Path
from datetime import datetime,timezone
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3

TABLE = 'sheet_vitrina_v1_operator_nomenclature'
CATALOG = 'sheet_vitrina_v1_nomenclature_items'
GROUPS = 'sheet_vitrina_v1_sku_groups'
DOMAIN = 'nomenclature'
REQUEST_PATH = '/v1/sheet-vitrina-v1/settings/nomenclature/operations/'
# Routing is read by wb_finance_weekly._nomenclature_identity_index. CNY price
# is a future supplier price reference; it does not replace historical cost.
COST_FIELDS = ('nm_id','vendor_code','barcode','barcodes_json','our_sku',
               'match_key','aliases_json','product_type','purchase_price_yuan')
TITLES = {'create':'Новый SKU','update':'Изменение SKU','delete':'Выключение SKU',
          'import':'Импорт справочника','group_create':'Новая группа SKU',
          'group_update':'Изменение группы SKU','group_delete':'Выключение группы SKU',
          'barcode':'Обновление ШК из WB','wb_sync':'Синхронизация SKU с WB'}


def digest(value):
    return 'sha256:'+hashlib.sha256(json.dumps(value,ensure_ascii=False,
        sort_keys=True,separators=(',',':')).encode()).hexdigest()


def exists(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone())


def readonly(db_path):
    conn=sqlite3.connect('file:'+str(db_path)+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def ensure_schema(conn):
    # No executescript: source, native activation demand and receipt share commit.
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE}(
        operation_id TEXT PRIMARY KEY,actor TEXT NOT NULL,action TEXT NOT NULL,
        request_digest TEXT NOT NULL,accepted_at TEXT NOT NULL,
        source_json TEXT NOT NULL,source_digest TEXT NOT NULL,
        result_json TEXT NOT NULL,cost_required INTEGER NOT NULL,
        cost_receipt_json TEXT NOT NULL DEFAULT '{{}}',
        activation_receipt_json TEXT NOT NULL DEFAULT '{{}}',
        activation_pending INTEGER NOT NULL DEFAULT 0,
        external_state TEXT NOT NULL DEFAULT '',external_json TEXT NOT NULL DEFAULT '{{}}',
        external_snapshot_json TEXT NOT NULL DEFAULT '{{}}',
        external_last_attempt_at TEXT NOT NULL DEFAULT ''
    )''')
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS operator_nomenclature_source_immutable
        BEFORE UPDATE OF operation_id,actor,action,request_digest,accepted_at,source_json,source_digest,result_json,cost_required ON {TABLE}
        BEGIN SELECT RAISE(ABORT,'operator nomenclature source is immutable'); END''')
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS operator_nomenclature_external_snapshot_immutable
        BEFORE UPDATE OF external_snapshot_json ON {TABLE}
        WHEN OLD.external_snapshot_json!='{{}}' AND OLD.external_snapshot_json!=NEW.external_snapshot_json
        BEGIN SELECT RAISE(ABORT,'operator WB read snapshot is immutable'); END''')
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS operator_nomenclature_no_delete
        BEFORE DELETE ON {TABLE}
        BEGIN SELECT RAISE(ABORT,'operator nomenclature receipts are append-only'); END''')
    conn.execute(f'CREATE INDEX IF NOT EXISTS operator_nomenclature_actor_time ON {TABLE}(actor,accepted_at,operation_id)')
    conn.execute(f'CREATE INDEX IF NOT EXISTS operator_nomenclature_activation_pending ON {TABLE}(activation_pending)')


@dataclass(frozen=True)
class Request:
    operation_id: str
    actor: str
    action: str
    request_digest: str
    expected: object = None


def request(identity, *, actor, action, payload, expected=None):
    if not re.fullmatch(r'opsku_[a-f0-9]{32}',str(identity or '')):
        raise ValueError('operator_nomenclature_request_id_invalid')
    if not actor or action not in TITLES:
        raise ValueError('operator_nomenclature_actor_or_action_invalid')
    return Request(identity,str(actor),action,digest({"payload":payload,"expected":expected}),expected)


def lookup(conn, req):
    row=conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=?',(req.operation_id,)).fetchone() if exists(conn) else None
    if row and (row['actor']!=req.actor or row['action']!=req.action or row['request_digest']!=req.request_digest):
        raise ValueError('operator_nomenclature_request_identity_conflict')
    return row


def before_write(conn, req, before):
    ensure_schema(conn)
    if lookup(conn,req):
        raise ValueError('operator_nomenclature_request_already_saved')
    if req.action in {'create','group_create'} and any(row is not None for row in before.values()):
        raise ValueError('operator_nomenclature_source_already_exists')
    if req.expected is not None:
        expected=req.expected
        if isinstance(expected,str):
            matches=len(before)==1 and expected==digest(next(iter(before.values())))
        elif isinstance(expected,dict):
            matches=all(expected.get(identity)==(digest(row) if row else None) for identity,row in before.items())
        else:
            matches=False
        if not matches:
            raise ValueError('operator_nomenclature_source_version_changed')


def record_saved(conn, req, *, before, after, accepted_at, result=None):
    from packages.application.nomenclature_activation_intents import TABLE as ACTIVATION
    group=req.action.startswith('group_')
    sources=[]
    cost_required=False
    for identity,row in sorted(after.items()):
        activation=conn.execute(f'SELECT revision,status,desired_fingerprint,dense_intent_id FROM {ACTIVATION} WHERE item_id=?',(identity,)).fetchone() if not group else None
        expected=dict(row)
        if activation and activation['status']=='pending':
            expected['is_active']=1
        old=before.get(identity) or {}
        if not group and any(old.get(k)!=expected.get(k) for k in COST_FIELDS):
            cost_required=True
        sources.append({'entity_id':identity,'before':before.get(identity),
                        'row':expected,'revision':digest(expected),
                        'activation_revision':activation['revision'] if activation and activation['status']=='pending' else None})
    # Preserve unresolved cost demand through a later metadata-only revision.
    if not group and sources and exists(conn):
        ids={s['entity_id'] for s in sources}
        for pending in conn.execute(f"SELECT source_json FROM {TABLE} WHERE cost_required=1 AND cost_receipt_json='{{}}'"):
            if ids & {s['entity_id'] for s in json.loads(pending['source_json'])['sources']}:
                cost_required=True
    source={'domain':DOMAIN,'action':req.action,'sources':sources,'group':group}
    if req.action in {'create','update'} and len(sources)==1:
        saved=sources[0]
        if saved['row'].get('nm_id') and not str(saved['row'].get('barcode') or '').strip():
            source['auto_barcode_task']={'item_id':saved['entity_id']}
    conn.execute(f'''INSERT INTO {TABLE}(operation_id,actor,action,request_digest,
        accepted_at,source_json,source_digest,result_json,cost_required,activation_pending,external_state)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(req.operation_id,req.actor,req.action,req.request_digest,
            accepted_at,json.dumps(source,ensure_ascii=False),digest(source),
            json.dumps(result or {'items':list(after.values())},ensure_ascii=False),int(cost_required),
            sum(s['activation_revision'] is not None for s in sources),
            'auto_pending' if source.get('auto_barcode_task') else ''))


def _source(row):
    source=json.loads(row['source_json'])
    if digest(source)!=row['source_digest']:
        raise ValueError('operator_nomenclature_source_corrupt')
    return source


def _actual_source(conn, source):
    table=GROUPS if source['group'] else CATALOG
    key='group_key' if source['group'] else 'item_id'
    result=[]
    for saved in source['sources']:
        actual=conn.execute(f'SELECT * FROM {table} WHERE {key}=?',(saved['entity_id'],)).fetchone()
        if not actual or digest(dict(actual))!=saved['revision']:
            return None
        if saved['activation_revision'] is not None:
            from packages.application.nomenclature_activation_intents import TABLE as ACTIVATION
            activation=conn.execute(f'SELECT revision,status,dense_intent_id FROM {ACTIVATION} WHERE item_id=?',(saved['entity_id'],)).fetchone()
            if not activation or activation['revision']!=saved['activation_revision'] or activation['status']!='active' or not activation['dense_intent_id']:
                return None
        result.append({'entity_id':saved['entity_id'],'revision':saved['revision']})
    return result


def finance_sources(conn):
    """Capture exact saved versions evaluated by the existing native cost model."""
    if not exists(conn):
        return []
    selected=[]
    for row in conn.execute(f"SELECT * FROM {TABLE} WHERE cost_required=1 AND cost_receipt_json='{{}}' ORDER BY accepted_at,operation_id"):
        source=_source(row)
        actual=_actual_source(conn,source)
        if actual is not None:
            selected.append({'operation_id':row['operation_id'],'source_digest':row['source_digest'],'sources':actual})
    return selected


def valid_finance_proof(proof):
    if not isinstance(proof,dict) or not isinstance(proof.get('source_dependency',{}),dict) or not isinstance(proof.get('post_source_dependency',{}),dict):
        return False
    valid_pair=(proof.get('status'),proof.get('outcome')) in {
        ('already_current','derived_no_change'),('applied','native_cost_evaluated')}
    return not (not valid_pair or proof.get('consumer')!='wb_finance_stale_cost_recalculation_v1'
            or type(proof.get('checked_week_count')) is not int or proof['checked_week_count']<0
            or proof.get('non_target_preserved') is not True
            or type(proof.get('post_verify_stale_week_count')) is not int
            or proof['post_verify_stale_week_count']!=0
            or not str(proof.get('fingerprint','')).startswith('sha256:')
            or not str(proof.get('source_dependency',{}).get('digest','')).startswith('sha256:')
            or (proof['status']=='applied' and (
                not proof.get('target_image_digest') or proof.get('post_source_dependency',{}).get('digest')!=proof['source_dependency']['digest'])))


def acknowledge_finance(conn, *, selected, proof):
    """Only native plan/apply invokes this within its exact writer transaction."""
    if not valid_finance_proof(proof):
        raise ValueError('operator_nomenclature_finance_proof_incomplete')
    for captured in selected:
        row=conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=?',(captured['operation_id'],)).fetchone()
        if not row or row['source_digest']!=captured['source_digest'] or _actual_source(conn,_source(row))!=captured['sources']:
            raise ValueError('operator_nomenclature_finance_source_changed')
        receipt={**proof,**captured,'exact_revision_acked':True}
        previous=json.loads(row['cost_receipt_json'])
        if previous and previous!=receipt:
            raise ValueError('operator_nomenclature_finance_ack_conflict')
        conn.execute(f'UPDATE {TABLE} SET cost_receipt_json=? WHERE operation_id=?',
                     (json.dumps(receipt,ensure_ascii=False),row['operation_id']))


def acknowledge_activation(conn, *, staged_items, intent_id):
    """Native dense consumer persists an exact witness in its own transaction."""
    if not exists(conn):
        return
    from packages.application.ff_pool_fbs_applicability import DENSE_INTENTS_TABLE,dense_intent_state
    native_intent=conn.execute(f'SELECT plan_json FROM {DENSE_INTENTS_TABLE} WHERE intent_id=?',(intent_id,)).fetchone()
    native_state=dense_intent_state(conn,intent_id)
    if not native_intent or native_state.get('state')!='active' or not native_state.get('receipt',{}).get('coverage_fingerprint'):
        raise ValueError('operator_nomenclature_activation_proof_missing')
    native_items=json.loads(native_intent['plan_json'])['expected_subject']['staged_items']
    if list(native_items)!=list(staged_items):
        raise ValueError('operator_nomenclature_activation_proof_mismatch')
    revisions={str(item['item_id']):item['source_revision'] for item in staged_items if 'source_revision' in item}
    for row in conn.execute(f'SELECT * FROM {TABLE} WHERE activation_pending>0').fetchall():
        source=_source(row)
        expected=[s for s in source['sources'] if s['activation_revision'] is not None]
        receipt=json.loads(row['activation_receipt_json'])
        matched=[s for s in expected if revisions.get(s['entity_id'])==s['activation_revision'] and s['entity_id'] not in receipt]
        if not matched:
            continue
        for saved in matched:
            actual=conn.execute(f'SELECT * FROM {CATALOG} WHERE item_id=?',(saved['entity_id'],)).fetchone()
            if not actual or digest(dict(actual))!=saved['revision']:
                raise ValueError('operator_nomenclature_activation_source_changed')
            receipt[saved['entity_id']]={'source_revision':saved['activation_revision'],
                'dense_intent_id':intent_id,'source_row_revision':saved['revision'],
                'event_id':native_state['event_id'],'receipt_fingerprint':native_state['receipt_fingerprint'],
                'coverage_fingerprint':native_state['receipt']['coverage_fingerprint']}
        conn.execute(f'UPDATE {TABLE} SET activation_receipt_json=?,activation_pending=? WHERE operation_id=?',
                     (json.dumps(receipt,ensure_ascii=False),len(expected)-len(receipt),row['operation_id']))


def public(conn,row):
    source=_source(row)
    if source.get("external_task"):
        return public_external(conn,row,source)
    cost=json.loads(row['cost_receipt_json'])
    activation_proof=json.loads(row['activation_receipt_json'])
    actual=_actual_source(conn,source)
    pending_activation=[]
    for saved in source['sources']:
        if saved['activation_revision'] is not None:
            proof=activation_proof.get(saved['entity_id'],{})
            if proof.get('source_revision')==saved['activation_revision'] and proof.get('source_row_revision')==saved['revision'] and valid_activation_proof(conn,proof):
                continue
            from packages.application.nomenclature_activation_intents import TABLE as ACTIVATION
            current=conn.execute(f'SELECT revision,status,dense_intent_id FROM {ACTIVATION} WHERE item_id=?',(saved['entity_id'],)).fetchone()
            if not current or current['revision']!=saved['activation_revision']:
                pending_activation.append('source_version_superseded')
            elif current['status']!='active' or not current['dense_intent_id']:
                pending_activation.append('native_activation_pending')
            else:
                pending_activation.append('native_activation_proof_missing')
    cost_valid=valid_finance_proof(cost) and cost.get('exact_revision_acked') is True and cost.get('operation_id')==row['operation_id'] and cost.get('source_digest')==row['source_digest'] and cost.get('sources')==[{'entity_id':saved['entity_id'],'revision':saved['revision']} for saved in source['sources']]
    completed=not pending_activation and (not row['cost_required'] or cost_valid)
    reason=pending_activation[0] if pending_activation else ('native_finance_pending' if not completed else '')
    if row['cost_required'] and not cost and actual is None and not pending_activation:
        reason='source_version_superseded'
    if row['cost_required'] and cost and not cost_valid and not pending_activation:
        reason='native_finance_proof_missing'
    auto_receipt={}
    if source.get('auto_barcode_task'):
        auto_receipt=json.loads(row['external_json'])
        children=[]
        for identity in auto_receipt.get('children',[]):
            child=conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=? AND actor=?',(identity,row['actor'])).fetchone()
            children.append(public(conn,child) if child else None)
        auto_done=row['external_state']=='finished' and all(child and child['state']=='completed' for child in children)
        if completed and not auto_done:
            completed=False
            child_attention=any(child and child['state']=='needs_attention' for child in children)
            reason='source_version_superseded' if row['external_state']=='needs_attention' or child_attention else 'native_barcode_pending' if row['external_state']=='auto_pending' else 'native_child_processing'
        auto_receipt={**auto_receipt,'state':row['external_state'],'children':children}
    return {'contract_name':'operator_operations_v1','domain':DOMAIN,
        'operation_id':row['operation_id'],'request_id':row['operation_id'],
        'actor':row['actor'],'accepted_at':row['accepted_at'],'durable_saved':True,
        'primary_effect':'source_saved','physical_applied':False,
        'kind':row['action'],'document_kind':'nomenclature_'+row['action'],
        'title_ru':TITLES[row['action']],'state':'completed' if completed else 'needs_attention' if reason in {'source_version_superseded','native_activation_proof_missing','native_finance_proof_missing'} else 'processing',
        'reason_code':reason,'reason_ru':reason_ru(reason),'source_ref':{'domain':DOMAIN,'revision':row['source_digest']},
        'processing_receipt':{'cost':cost,'activation':activation_proof,'barcode':auto_receipt},
        'summary':{'entity_ids':[s['entity_id'] for s in source['sources']]},
        'fields':[{'label':'Записей','value':str(len(source['sources']))}],
        'detail_path':REQUEST_PATH+row['operation_id'],
        'journal_path':'/sheet-vitrina-v1/operations?operation_id='+row['operation_id']}


def read(db_path, identity, *, actor):
    if not Path(db_path).is_file():
        return None
    with closing(readonly(db_path)) as conn:
        row=conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=? AND actor=?',(identity,actor)).fetchone() if exists(conn) else None
        return public(conn,row) if row else None


def read_result(db_path, req):
    if not Path(db_path).is_file():
        return None
    with closing(readonly(db_path)) as conn:
        row=lookup(conn,req)
        if row:
            return {**json.loads(row['result_json']),'status':'ok','acceptance':public(conn,row)}
    return None


def perform(runtime, req, write):
    saved=read_result(runtime.db_path,req)
    if saved:
        return saved
    try:
        result=write()
    except Exception:
        saved=read_result(runtime.db_path,req)
        if saved:
            return saved
        raise
    if result.get('status')!='ok':
        return result
    saved=read_result(runtime.db_path,req)
    if not saved:
        raise ValueError('operator_nomenclature_saved_readback_missing')
    return {**result,'acceptance':saved['acceptance']}


def journal_source():
    """Root-owned common journal may use this only after operator permission."""
    return {'table':TABLE,'identity_column':'operation_id','domain':DOMAIN,
            'permission':'settings','permission_guard':'_ensure_operator_role','actor_column':'actor',
            'accepted_at_column':'accepted_at','reader':public,'detail_path':REQUEST_PATH}


def external_child_request(parent, index, card):
    identity='opsku_'+hashlib.sha256((parent['operation_id']+':'+str(index)+':'+digest(card)).encode()).hexdigest()[:32]
    return request(identity,actor=parent['actor'],action='wb_sync',
                   payload={'parent':parent['operation_id'],'index':index,'card':card})


def child_before(db_path,req):
    with closing(readonly(db_path)) as conn:
        row=lookup(conn,req)
        before=_source(row)['sources'][0]['before']
        if before is None:
            return None
        from packages.application.registry_upload_db_backed_runtime import _nomenclature_item_to_dict
        return _nomenclature_item_to_dict(before)


def accept_external(runtime,req,body,*,accepted_at):
    """Accept a WB read task. No provider call, Dense plan or weekly calculation."""
    saved=read_result(runtime.db_path,req)
    if saved:
        return saved
    from packages.application.supplier_shipments import _bounded_int
    task={'item_id':str(body.get('item_id') or '')} if req.action=='barcode' else {
        'limit':_bounded_int(body.get('limit'),default=100,minimum=1,maximum=100),
        'max_pages':_bounded_int(body.get('max_pages'),default=500,minimum=1,maximum=5000)}
    with closing(sqlite3.connect(runtime.db_path)) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        before={}
        if req.action=='barcode':
            row=conn.execute(f'SELECT * FROM {CATALOG} WHERE item_id=?',(task['item_id'],)).fetchone()
            if row is None:
                raise ValueError('nomenclature item not found')
            before={task['item_id']:dict(row)}
        before_write(conn,req,before)
        source={'domain':DOMAIN,'action':req.action,'sources':[], 'group':False,
                'external_task':task,'before':before}
        conn.execute(f'''INSERT INTO {TABLE}(operation_id,actor,action,request_digest,
            accepted_at,source_json,source_digest,result_json,cost_required,external_state)
            VALUES(?,?,?,?,?,?,?,'{{}}',0,'pending')''',
            (req.operation_id,req.actor,req.action,req.request_digest,accepted_at,
             json.dumps(source,ensure_ascii=False),digest(source)))
        conn.commit()
    return read_result(runtime.db_path,req)


def _external_update(db_path,identity,*,snapshot=None,state=None,result=None):
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=?',(identity,)).fetchone()
        if snapshot is not None:
            previous=json.loads(row['external_snapshot_json'])
            if previous and previous!=snapshot:
                raise ValueError('operator_nomenclature_external_snapshot_conflict')
            conn.execute(f'UPDATE {TABLE} SET external_snapshot_json=? WHERE operation_id=?',
                         (json.dumps(snapshot,ensure_ascii=False),identity))
        if state is not None:
            conn.execute(f'UPDATE {TABLE} SET external_state=?,external_json=? WHERE operation_id=?',
                         (state,json.dumps(result or {},ensure_ascii=False),identity))
        conn.commit()


def drain_external(runtime,*,block=None,batch_limit=10):
    """Existing owned SKU consumer performs native WB reads and native source writes."""
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.supplier_shipments import (SupplierShipmentsBlock,
        _normalize_wb_card_for_sync,_wb_card_has_identity,_barcode_resolution_barcodes,
        _barcode_resolution_evidence,_safe_barcode_error)
    require_heavy_owner(runtime.runtime_dir)
    with closing(readonly(runtime.db_path)) as conn:
        rows=conn.execute(f"SELECT * FROM {TABLE} WHERE external_state IN ('pending','auto_pending') ORDER BY external_last_attempt_at,accepted_at,operation_id LIMIT ?",(batch_limit,)).fetchall() if exists(conn) else []
    native=block or SupplierShipmentsBlock(runtime=runtime)
    results=[]
    for row in rows:
        source=_source(row); auto=bool(source.get('auto_barcode_task')); task=source.get('external_task') or source['auto_barcode_task']; snapshot=json.loads(row['external_snapshot_json'])
        if auto and not (snapshot and read_result(runtime.db_path,external_child_request(row,0,task))):
            with closing(readonly(runtime.db_path)) as conn:
                actual=_actual_source(conn,source)
                initial_ready=(row['activation_pending']==0 and (not row['cost_required'] or json.loads(row['cost_receipt_json']).get('exact_revision_acked') is True))
                if actual is None:
                    # Pending Dense has not applied yet; drift is distinct from
                    # technical wait. Compare its exact staged native fingerprint.
                    saved=source['sources'][0]
                    current=conn.execute(f'SELECT * FROM {CATALOG} WHERE item_id=?',(saved['entity_id'],)).fetchone()
                    staged={**saved['row'],'is_active':0} if saved['activation_revision'] is not None else saved['row']
                    if current and digest(dict(current))==digest(staged) and not initial_ready:
                        continue
                    _external_update(runtime.db_path,row['operation_id'],state='needs_attention',result={'reason_code':'operator_nomenclature_source_version_changed'})
                    continue
                if not initial_ready:
                    continue
        with closing(sqlite3.connect(runtime.db_path)) as attempt_writer:
            attempt_writer.execute(f'UPDATE {TABLE} SET external_last_attempt_at=? WHERE operation_id=?',
                (datetime.now(timezone.utc).isoformat(),row['operation_id']))
            attempt_writer.commit()
        try:
            if not snapshot:
                if row['action']=='wb_sync' and not auto:
                    cards=native.barcode_source.fetch_cards(**task)
                    snapshot={'cards':[_normalize_wb_card_for_sync(card) for card in cards],
                              'captured_at':native.timestamp_factory()}
                else:
                    item=native.runtime.load_nomenclature_item(task['item_id'])
                    with closing(readonly(runtime.db_path)) as conn:
                        current=conn.execute(f'SELECT * FROM {CATALOG} WHERE item_id=?',(task['item_id'],)).fetchone()
                        before_row=source['sources'][0]['row'] if auto else source['before'][task['item_id']]
                    if not current or digest(dict(current))!=digest(before_row):
                        raise ValueError('operator_nomenclature_source_version_changed')
                    nm_id=item.get('nm_id')
                    resolution=None
                    # Native manual barcodes and absent nmID retain their own behavior.
                    if nm_id and not (item.get('barcode_source')=='manual' and item.get('barcode')):
                        resolution=native.barcode_source.fetch_barcodes_by_nm_ids([int(nm_id)]).get(int(nm_id))
                    snapshot={'resolution':{'barcodes':_barcode_resolution_barcodes(resolution),
                        **_barcode_resolution_evidence(resolution,nm_id=int(nm_id or 0),sync_reason='manual_row_sync')},
                        'captured_at':native.timestamp_factory()}
                _external_update(runtime.db_path,row['operation_id'],snapshot=snapshot)
            class CachedRead:
                def fetch_cards(self,**_kwargs): return snapshot['cards']
                def fetch_barcodes_by_nm_ids(self,ids): return {nm:snapshot['resolution'] for nm in ids}
            consumer=SupplierShipmentsBlock(runtime=runtime,barcode_source=CachedRead(),
                timestamp_factory=lambda:snapshot['captured_at'])
            if row['action']=='wb_sync' and not auto:
                result=consumer.sync_nomenclature_with_wb(task,operator_parent=row)
                children=[external_child_request(row,index,card).operation_id for index,card in enumerate(snapshot['cards']) if _wb_card_has_identity(card)]
            else:
                req=external_child_request(row,0,task)
                from dataclasses import replace
                before_rows={task['item_id']:source['sources'][0]['row']} if auto else source['before']
                req=replace(req,expected={key:digest(value) for key,value in before_rows.items()})
                saved=read_result(runtime.db_path,req)
                result=saved or consumer.sync_nomenclature_item_barcode(task['item_id'],operator_request=req)
                children=[req.operation_id] if read_result(runtime.db_path,req) else []
                if not children:
                    # A manual barcode is a native evaluated no-change result. The
                    # exact accepted before-image must still hold inside its proof txn.
                    with closing(sqlite3.connect(runtime.db_path)) as conn:
                        conn.row_factory=sqlite3.Row; conn.execute('BEGIN IMMEDIATE')
                        current=conn.execute(f'SELECT * FROM {CATALOG} WHERE item_id=?',(task['item_id'],)).fetchone()
                        if not current or digest(dict(current))!=digest(before_rows[task['item_id']]):
                            raise ValueError('operator_nomenclature_source_version_changed')
                        conn.execute(f'UPDATE {TABLE} SET external_state=\'finished\',external_json=? WHERE operation_id=?',
                            (json.dumps({'children':[], 'native_result':result,'exact_source_revision':digest(dict(current)), 'outcome':'derived_no_change'},ensure_ascii=False),row['operation_id']))
                        conn.commit()
                    results.append({'operation_id':row['operation_id'],'state':'finished'}); continue
            if result.get('status')!='ok':
                raise RuntimeError('native_wb_read_result_failed')
            _external_update(runtime.db_path,row['operation_id'],state='finished',result={
                'children':children,'native_result':result,'snapshot_digest':digest(snapshot)})
            results.append({'operation_id':row['operation_id'],'state':'finished'})
        except Exception as exc:
            message=str(exc)
            drift='source_version_changed' in message or 'identity_conflict' in message
            code='operator_nomenclature_source_version_changed' if drift else str(getattr(exc,'code',type(exc).__name__))
            state='needs_attention' if drift else 'auto_pending' if auto else 'pending'
            _external_update(runtime.db_path,row['operation_id'],state=state,result={'reason_code':code,'error':_safe_barcode_error(exc)})
            results.append({'operation_id':row['operation_id'],'state':state,'reason_code':code})
    return results


def public_external(conn,row,source):
    result=json.loads(row['external_json']); children=[]
    for identity in result.get('children',[]):
        child=conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=? AND actor=?',(identity,row['actor'])).fetchone()
        children.append(public(conn,child) if child else None)
    completed=row['external_state']=='finished' and all(child and child['state']=='completed' for child in children)
    attention=row['external_state']=='needs_attention' or any(child and child['state']=='needs_attention' for child in children)
    return {'contract_name':'operator_operations_v1','domain':DOMAIN,
        'operation_id':row['operation_id'],'request_id':row['operation_id'],
        'actor':row['actor'],'accepted_at':row['accepted_at'],'durable_saved':True,
        'primary_effect':'source_saved','physical_applied':False,
        'kind':row['action'],'document_kind':'nomenclature_'+row['action'],
        'title_ru':TITLES[row['action']],'state':'completed' if completed else 'needs_attention' if attention else 'processing',
        'reason_code':result.get('reason_code','') if attention else '' if completed else 'native_wb_read_pending' if row['external_state']!='finished' else 'native_child_processing',
        'source_ref':{'domain':DOMAIN,'revision':row['source_digest']},
        'summary':{'task':source['external_task'],'child_operation_ids':result.get('children',[])},
        'fields':[{'label':'Задание','value':'Чтение карточек WB'}],
        'processing_receipt':{'external':result,'children':children},
        'detail_path':REQUEST_PATH+row['operation_id'],
        'journal_path':'/sheet-vitrina-v1/operations?operation_id='+row['operation_id']}


def validate_catalog_write(conn,prepared,before):
    """Repeat native group/unique guards under the source writer lock."""
    from packages.application.nomenclature_activation_intents import TABLE as ACTIVATION
    changed={str(item['item_id']) for item in prepared}
    has_activation=conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",(ACTIVATION,)).fetchone()
    pending=set(row[0] for row in conn.execute(f"SELECT item_id FROM {ACTIVATION} WHERE status='pending'")) if has_activation else set()
    keys={row['match_key'] for row in conn.execute(f'SELECT item_id,is_active,match_key FROM {CATALOG}')
          if row['item_id'] not in changed and (row['is_active'] or row['item_id'] in pending) and row['match_key']}
    for item in prepared:
        key=str(item.get('product_type') or '')
        group=conn.execute(f'SELECT is_active FROM {GROUPS} WHERE group_key=?',(key,)).fetchone()
        old=before.get(str(item['item_id'])) or {}
        if group is None and old.get('product_type')!=key:
            raise ValueError('nomenclature group is absent from server-owned SKU groups: '+key)
        if item['is_active'] and group is not None and not group['is_active']:
            raise ValueError('nomenclature group is inactive: '+key)
        match=item.get('match_key')
        if item['is_active'] and match:
            if match in keys:
                raise ValueError('duplicate active nomenclature match_key: '+match)
            keys.add(match)


def require_group_unused(conn,group_key):
    from packages.application.nomenclature_activation_intents import TABLE as ACTIVATION
    pending=""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",(ACTIVATION,)).fetchone():
        pending=f" OR EXISTS(SELECT 1 FROM {ACTIVATION} a WHERE a.item_id=n.item_id AND a.status='pending')"
    if conn.execute(f"SELECT 1 FROM {CATALOG} n WHERE product_type=? AND (is_active=1{pending}) LIMIT 1",(group_key,)).fetchone():
        raise ValueError('sku group is used by active or accepted pending nomenclature rows')


def reason_ru(code):
    return {'native_activation_pending':'SKU сохранён. Активация ожидает складской обработки.',
        'native_finance_pending':'Справочник сохранён. Проверка затрат ожидает плановой обработки.',
        'native_barcode_pending':'SKU сохранён. Чтение штрихкода WB ожидает плановой обработки.',
        'native_child_processing':'Источник сохранён. Результаты синхронизации ещё обрабатываются.',
        'native_wb_read_pending':'Задание сохранено. Чтение карточек WB ещё не завершено.',
        'source_version_superseded':'Сохранённая версия заменена. Автоматическое продолжение этой версии остановлено.'}.get(code,'')


def valid_activation_proof(conn,proof):
    if not all(proof.get(key) for key in ('dense_intent_id','event_id','receipt_fingerprint','coverage_fingerprint')):
        return False
    from packages.application.ff_pool_fbs_applicability import DENSE_INTENT_EVENTS_TABLE
    event=conn.execute(f'SELECT * FROM {DENSE_INTENT_EVENTS_TABLE} WHERE event_id=? AND intent_id=?',
                       (proof['event_id'],proof['dense_intent_id'])).fetchone()
    return bool(event and event['state']=='active' and event['receipt_fingerprint']==proof['receipt_fingerprint']
                and json.loads(event['receipt_json']).get('coverage_fingerprint')==proof['coverage_fingerprint'])
