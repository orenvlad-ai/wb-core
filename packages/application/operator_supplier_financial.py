"""Source-owned one-shot financial confirmations and exact batch child receipts.

No financial algorithm, processing queue, network submit, or GET bootstrap lives
here. Native source writers call before_write/record_saved in their own txn.
"""
from contextlib import closing, contextmanager, ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import hashlib
import json
import re
import time
from uuid import uuid4

from packages.application import operator_supplier_shipments as source

DOMAIN = 'supplier_financial_document'
REQUESTS = 'sheet_vitrina_v1_operator_supplier_financial_requests'
CHILDREN = 'sheet_vitrina_v1_operator_supplier_financial_children'
OUTCOMES = 'sheet_vitrina_v1_operator_supplier_financial_outcomes'
SCOPES = 'sheet_vitrina_v1_operator_supplier_financial_cost_scopes'
CORE = 'sheet_vitrina_v1_operator_supplier_financial_core_proofs'
ACCOUNT_CORE_PREFIX = 'cny_account:authority:'
_ACTION = ContextVar('supplier_financial_source_action', default=None)
FINANCIAL_ACTIONS = frozenset({'confirm_upload', 'confirm_import', 'exclude', 'status', 'zero_fee'})
TITLES = {'confirm_upload': 'Добавление финансовых документов', 'confirm_import': 'Добавление комиссий',
          'exclude': 'Исключение финансового документа', 'status': 'Изменение финансового документа',
          'zero_fee': 'Подтверждение отсутствия комиссий'}


def ensure_schema(conn):
    from packages.application.operator_supplier_processing import ensure_schema as ensure_processing
    ensure_processing(conn)
    conn.execute(f'CREATE TABLE IF NOT EXISTS {REQUESTS}(request_scope TEXT NOT NULL,request_id TEXT NOT NULL,action TEXT NOT NULL,shipment_id TEXT NOT NULL,actor TEXT NOT NULL,payload_digest TEXT NOT NULL,wire_digest TEXT NOT NULL,payload_json TEXT NOT NULL,manifest_json TEXT NOT NULL,accepted_at TEXT NOT NULL,operation_id TEXT NOT NULL UNIQUE,PRIMARY KEY(request_scope,request_id))')
    conn.execute(f'CREATE TABLE IF NOT EXISTS {CHILDREN}(operation_id TEXT PRIMARY KEY,request_scope TEXT NOT NULL,request_id TEXT NOT NULL,child_key TEXT NOT NULL,kind TEXT NOT NULL,subject_id TEXT NOT NULL,revision INTEGER NOT NULL,source_digest TEXT NOT NULL,source_json TEXT NOT NULL,scope_json TEXT NOT NULL,intents_json TEXT NOT NULL,cny_intent_json TEXT NOT NULL,accepted_at TEXT NOT NULL,UNIQUE(request_scope,request_id,child_key))')
    conn.execute(f'CREATE TABLE IF NOT EXISTS {OUTCOMES}(request_scope TEXT NOT NULL,request_id TEXT NOT NULL,child_key TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT NOT NULL,PRIMARY KEY(request_scope,request_id,child_key))')
    conn.execute(f'CREATE TABLE IF NOT EXISTS {SCOPES}(operation_id TEXT PRIMARY KEY,parent_operation_id TEXT NOT NULL,shipment_id TEXT NOT NULL,intent_kind TEXT NOT NULL,source_json TEXT NOT NULL,intent_json TEXT NOT NULL)')
    conn.execute(f'CREATE TABLE IF NOT EXISTS {CORE}(operation_id TEXT NOT NULL,account_revision INTEGER NOT NULL,proof_json TEXT NOT NULL,proof_digest TEXT NOT NULL,PRIMARY KEY(operation_id,account_revision))')
    for table in (REQUESTS, CHILDREN, OUTCOMES, SCOPES, CORE):
        for event in ('UPDATE', 'DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{event.lower()}_immutable BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT,'financial source acknowledgement is immutable'); END")


def active():
    return _ACTION.get()


def _lock_path(runtime_dir, scope, identity):
    return Path(runtime_dir)/'operator_supplier_financial_locks'/(hashlib.sha256((scope+'|'+identity).encode()).hexdigest()+'.lock')


def _inflight(runtime_dir, scope, identity):
    path = _lock_path(runtime_dir, scope, identity)
    if not path.is_file():
        return False
    with path.open('r') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def capture_owners(conn, owners):
    result = {}
    for owner in sorted(set(owners)-{''}):
        result[owner] = {**source.capture(conn, owner),
            'financial_documents': [dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_supplier_financial_documents WHERE supplier_order_id=? ORDER BY document_id', (owner,))],
            'fee_confirmations': [dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_supplier_payment_fee_confirmations WHERE supplier_order_id=? ORDER BY confirmation_id', (owner,))]}
    return result


def snapshot(conn, kind, subject):
    if kind == 'financial':
        row = conn.execute('SELECT * FROM sheet_vitrina_v1_supplier_financial_documents WHERE document_id=?', (subject,)).fetchone()
        return {'document': dict(row) if row else None,
            'expenses': [dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_supplier_financial_expense_lines WHERE financial_document_id=? ORDER BY sort_order,line_id', (subject,))],
            'assignments': [dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_supplier_bank_operation_assignments WHERE financial_document_id=? ORDER BY semantic_operation_id', (subject,))],
            'cny_companions': [dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_cny_documents WHERE linked_financial_document_id=? ORDER BY document_id', (subject,))]}
    table = 'cny_documents' if kind == 'cny' else 'supplier_payment_fee_confirmations'
    field = 'document_id' if kind == 'cny' else 'confirmation_id'
    row = conn.execute(f'SELECT * FROM sheet_vitrina_v1_{table} WHERE {field}=?', (subject,)).fetchone()
    return {'document': dict(row) if row else None}


def account_guard(conn):
    from packages.application import cny_preparation_intents as cny
    row = dict(conn.execute(f"SELECT * FROM {cny.TABLE} WHERE account_id='account'").fetchone() or {}) if source._exists(conn,cny.TABLE) else {}
    owners = json.loads(row.get('affected_shipment_ids_json') or '[]')
    return {'revision':row.get('revision'), 'versions':cny._versions(cny.capture_source(conn)),
            'dependencies':cny._dependencies(conn,owners)}


def before_write(conn, *, kind, subject_id, owners):
    context = active()
    if context is None or context.get('saved') or context['kind'] != kind:
        return None
    if not conn.in_transaction:
        raise ValueError('financial receipt requires native source transaction')
    ensure_schema(conn)
    if context.get('subject_id') and context['subject_id'] != subject_id:
        raise ValueError('financial native subject differs from admitted action')
    prior = conn.execute(f'SELECT operation_id FROM {CHILDREN} WHERE request_scope=? AND request_id=? AND child_key=?', (context['request_scope'], context['request_id'], context['child_key'])).fetchone()
    if prior:
        raise source.AlreadySaved()
    guarded = capture_owners(conn, context['expected_owners'])
    if source.digest(guarded) != source.digest(context['expected_owners']):
        raise ValueError('financial source changed before native save; request new previews')
    if context.get('expected_account') is not None and account_guard(conn) != context['expected_account']:
        raise ValueError('CNY account source changed before native save; request new previews')
    all_owners = sorted(set(owners) | set(context['expected_owners']))
    before = capture_owners(conn, all_owners)
    old_intents = {owner: dict(conn.execute(f'SELECT * FROM {source.intents.TABLE} WHERE shipment_id=?', (owner,)).fetchone() or {}) if source._exists(conn, source.intents.TABLE) else {} for owner in all_owners}
    from packages.application import cny_preparation_intents as cny
    old_cny = dict(conn.execute(f"SELECT * FROM {cny.TABLE} WHERE account_id='account'").fetchone() or {}) if source._exists(conn,cny.TABLE) else {}
    return {'before': before, 'before_document': snapshot(conn, kind, subject_id), 'old_intents': old_intents, 'old_cny': old_cny, 'account_before':account_guard(conn)}


def record_saved(conn, *, kind, subject_id, guard):
    if guard is None:
        return
    context = active()
    from packages.application import cny_preparation_intents as cny
    after = capture_owners(conn, guard['before'])
    document = snapshot(conn, kind, subject_id)
    if not document['document']:
        raise ValueError('financial final source missing inside native transaction')
    intents = {owner: dict(conn.execute(f'SELECT * FROM {source.intents.TABLE} WHERE shipment_id=?', (owner,)).fetchone() or {}) if source._exists(conn, source.intents.TABLE) else {} for owner in after}
    intents = {owner: row for owner, row in intents.items() if row.get('revision') != guard['old_intents'][owner].get('revision')}
    cny_intent = dict(conn.execute(f'SELECT * FROM {cny.TABLE} WHERE account_id=\'account\'').fetchone() or {}) if source._exists(conn, cny.TABLE) else {}
    cny_changed = cny_intent.get('revision') != guard['old_cny'].get('revision')
    identity = 'supplier_financial_'+hashlib.sha256((context['request_scope']+'|'+context['request_id']+'|'+context['child_key']).encode()).hexdigest()[:32]
    revision = conn.execute(f'SELECT COALESCE(MAX(revision),0)+1 FROM {CHILDREN} WHERE kind=? AND subject_id=?', (kind, subject_id)).fetchone()[0]
    scope = {'before': guard['before'], 'after': after, 'before_document': guard['before_document'], 'account_before':guard['account_before'],'account_after':account_guard(conn)}
    conn.execute(f'INSERT INTO {CHILDREN} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (identity, context['request_scope'], context['request_id'], context['child_key'], kind, subject_id, revision, source.digest(document), source._json(document), source._json(scope), source._json(intents), source._json(cny_intent), context['accepted_at']))
    for owner, intent in intents.items():
        cost_id = identity+'_'+hashlib.sha256(owner.encode()).hexdigest()[:12]
        conn.execute(f'INSERT INTO {SCOPES} VALUES(?,?,?,?,?,?)', (cost_id, identity, owner, 'supplier', source._json(after[owner]), source._json(intent)))
    if cny_changed and json.loads(cny_intent['affected_nm_ids_json']):
        for owner in json.loads(cny_intent['affected_shipment_ids_json']):
            saved = source.capture(conn,owner)
            if saved['header']:
                cost_id=identity+'_cny_'+hashlib.sha256(owner.encode()).hexdigest()[:12]
                conn.execute(f'INSERT INTO {SCOPES} VALUES(?,?,?,?,?,?)',(cost_id,identity,owner,'cny',source._json(saved),source._json(cny_intent)))
    # The native source transaction owns this known child outcome too. Later
    # projection/preview-consumption failures cannot hide or reject this save.
    conn.execute(f'INSERT INTO {OUTCOMES} VALUES(?,?,?,?,?)', (context['request_scope'], context['request_id'], context['child_key'], 'accepted', source._json({'operation_id': identity, 'document_id': subject_id, 'supplier_order_id': context['shipment_id'], 'readback_confirmed': True, 'outcome': context.get('outcome', 'saved')})))
    context['saved'] = identity  # Hint only; RO readback always verifies the row.
    context['core_needed'] = cny_intent.get('revision') != guard['old_cny'].get('revision')


@contextmanager
def child_context(context):
    token = _ACTION.set(context)
    try:
        yield context
    finally:
        _ACTION.reset(token)


def retain_outcome(runtime, context, status, result):
    from packages.application.registry_upload_db_backed_runtime import _connect
    with _connect(runtime.db_path) as conn:
        conn.execute('BEGIN IMMEDIATE');ensure_schema(conn)
        conn.execute(f'INSERT OR IGNORE INTO {OUTCOMES} VALUES(?,?,?,?,?)', (context['request_scope'], context['request_id'], context['child_key'], status, source._json(result)))
        conn.commit()


def _interval(scope, intents, cny_intent):
    days, ids = set(), set()
    for phase in ('before', 'after'):
        for owner, saved in scope[phase].items():
            header = saved['header']
            days.update(str(header.get(key) or '')[:10] for key in ('invoice_date','shipment_date','actual_shipment_date','actual_ff_acceptance_date'))
            ids.update(int(r['internal_nm_id']) for r in saved['lines'] if r.get('line_type') == 'product' and int(r.get('internal_nm_id') or 0)>0)
            days.update(str(doc.get('document_date') or '')[:10] for doc in saved['financial_documents'])
    for intent in list(intents.values())+[cny_intent]:
        if intent:
            days.add(intent.get('effective_date',''));ids.update(json.loads(intent['affected_nm_ids_json']))
    days.discard('')
    return {'shipment_ids': sorted(set(scope['before'])|set(json.loads(cny_intent.get('affected_shipment_ids_json') or '[]'))), 'affected_nm_ids': sorted(ids), 'effective_from': min(days) if days else None, 'source_dates': sorted(days), 'effective_to': None, 'continuation': 'native_current_and_dated_history'}


def read_account_core(conn, *, request=None, operations=None, state=None):
    from packages.application import cny_preparation_intents as cny
    from packages.application.cny_ledger import _cny_operation_revision_payload
    from packages.application.registry_upload_db_backed_runtime import _cny_ledger_operation_to_dict
    if not source._exists(conn, CORE) or not source._exists(conn, cny.TABLE):
        return None
    request = request if request is not None else dict(conn.execute(f"SELECT * FROM {cny.TABLE} WHERE account_id='account'").fetchone() or {})
    if not request or not cny._matches(conn, request):
        return None
    operations = operations if operations is not None else [_cny_ledger_operation_to_dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_cny_ledger_operations ORDER BY sequence_key,operation_id')]
    state = state if state is not None else dict(conn.execute('SELECT * FROM sheet_vitrina_v1_cny_ledger_replay_state WHERE slot=1').fetchone() or {})
    actual = {r['operation_id']:_cny_operation_revision_payload(r) for r in operations}
    for row in conn.execute(f'SELECT * FROM {CORE} WHERE account_revision=? AND substr(operation_id,1,?)=? ORDER BY operation_id', (request['revision'],len(ACCOUNT_CORE_PREFIX),ACCOUNT_CORE_PREFIX)):
        proof=json.loads(row['proof_json'])
        if (source.digest(proof)==row['proof_digest'] and proof.get('account_source_fingerprint')==request['source_fingerprint']
            and proof.get('dependency_fingerprint')==request['dependency_fingerprint'] and proof.get('source_versions')==json.loads(request['source_versions_json'])
            and proof.get('ledger_digest')==source.digest(actual) and all(str(proof.get(k))==str(state.get(k)) for k in ('balance_cny','balance_rub_value','average_rate','replayed_at','status'))):
            return proof
    return None


def public_child(conn, row, *, account_proof=None):
    row = dict(row);doc = json.loads(row['source_json'])['document'];scope = json.loads(row['scope_json']);intents = json.loads(row['intents_json']);cny = json.loads(row['cny_intent_json'])
    costs = conn.execute(f'SELECT operation_id FROM {SCOPES} WHERE parent_operation_id=? ORDER BY operation_id',(row['operation_id'],)).fetchall()
    needs_cost = bool(costs)
    receipt = {'domain': DOMAIN, 'operation_id': row['operation_id'], 'durable_saved': True, 'accepted_at': row['accepted_at'], 'state': 'accepted' if needs_cost else 'completed',
        'title_ru': 'Финансовый документ сохранён', 'fields': [{'label':'Документ','value':row['subject_id']},{'label':'Версия','value':str(row['revision'])}],
        'source_ref': {'domain':DOMAIN,'entity_id':conn.execute(f'SELECT shipment_id FROM {REQUESTS} WHERE request_scope=? AND request_id=?',(row['request_scope'],row['request_id'])).fetchone()[0],'action':'financial','kind':row['kind'],'document_id':row['subject_id'],'revision':row['revision'],'digest':row['source_digest'],'native_sha256':doc.get('file_sha256'),'native_updated_at':doc.get('updated_at')},
        'processing': {'kind':'cost_publication' if needs_cost else 'source_only','complete':not needs_cost},
        'reason_ru': 'Документ сохранён. Ожидает обработки.' if needs_cost else 'Документ сохранён. Расчёт не требуется.',
        'detail_path':'/sheet-vitrina-v1/supplier?operation_id='+row['operation_id'],
        'journal_path':'/sheet-vitrina-v1/operations?operation_id='+row['operation_id']}
    if not costs and row['kind']=='cny':
        receipt['processing']['existing_source_obligation']={'revision':cny.get('revision'),'state':cny.get('status'),'complete':None}
        receipt['reason_ru']='Сохранение подтверждено. Состояние расчёта существующего документа проверяется отдельно.'
    if costs:
        from packages.application import operator_supplier_processing as processing
        stages=[]
        for r in costs:
            stage=processing.public_completion(conn,r['operation_id'])
            if not stage and processing.superseded(conn,processing.operation(conn,r['operation_id'])):
                stage={'state':'needs_attention','complete':False,'terminal':True,'reason_code':processing.SUPERSEDED}
            stages.append(stage or {'state':'processing','complete':False,'reason_code':'supplier_exact_cost_pending'})
        receipt['processing']['scopes']=[{'operation_id':r['operation_id'],**state} for r,state in zip(costs,stages)]
        receipt['processing']['complete']=all(state['complete'] for state in stages)
        receipt['processing']['terminal']=all(state.get('terminal') or state['complete'] for state in stages)
        if receipt['processing']['complete']:
            receipt['state']='completed';receipt['reason_ru']='Обработка этой версии завершена.'
        elif any(state.get('terminal') for state in stages):
            receipt['state']='delayed';receipt['reason_ru']='Эту версию заменили последующими изменениями.'
            retired_kinds={processing.operation(conn,r['operation_id']).get('intent_kind','supplier') for r,state in zip(costs,stages) if state.get('terminal')}
            for newer in conn.execute(f'SELECT s.operation_id,c.operation_id child_id FROM {SCOPES} s JOIN {CHILDREN} c ON c.operation_id=s.parent_operation_id WHERE c.request_scope=? AND c.operation_id<>? ORDER BY c.accepted_at DESC,c.operation_id DESC LIMIT 32',(row['request_scope'],row['operation_id'])):
                candidate=processing.operation(conn,newer['operation_id'])
                if candidate.get('intent_kind','supplier') in retired_kinds and candidate['shipment_id'] in scope['after'] and processing.current(conn,candidate):
                    receipt['processing']['superseded_by']={'domain':DOMAIN,'operation_id':newer['child_id'],'detail_path':'/sheet-vitrina-v1/supplier?operation_id='+newer['child_id']}
                    break
        elif any(state['state']=='needs_attention' for state in stages):
            receipt['state']='needs_attention';receipt['reason_ru']='Обработка документа требует внимания.'
    core = conn.execute(f'SELECT * FROM {CORE} WHERE operation_id=? ORDER BY account_revision DESC LIMIT 1',(row['operation_id'],)).fetchone()
    # An exact current account proof includes all native documents/operations,
    # so aliases beyond the bounded per-child cohort do not wait indefinitely.
    if account_proof and source.digest(snapshot(conn,row['kind'],row['subject_id']))==row['source_digest'] and (row['kind']=='cny' or json.loads(row['source_json']).get('cny_companions')):
        ids={row['subject_id']} if row['kind']=='cny' else {r['document_id'] for r in json.loads(row['source_json'])['cny_companions']}
        proof={**account_proof,'source_ref':{'operation_id':row['operation_id'],'revision':row['revision'],'digest':row['source_digest']},
            'operation_revisions':[r for r in account_proof['operation_revisions'] if r['source_document_id'] in ids]}
        core={'proof_json':source._json(proof),'proof_digest':source.digest(proof)}
    if core and source.digest(json.loads(core['proof_json'])) == core['proof_digest']:
        proof=json.loads(core['proof_json'])
        receipt['financial_applied'] = all(r['status'] != 'blocked' for r in proof['operation_revisions'])
        receipt['financial_receipt'] = {key:proof[key] for key in ('source_ref','account_revision','account_source_fingerprint','replayed_at')}
        receipt['financial_receipt']['state'] = 'applied' if receipt['financial_applied'] else 'needs_attention'
        if not receipt['financial_applied']:
            receipt['state']='needs_attention';receipt['reason_ru']='Документ сохранён. Финансовая обработка требует внимания.'
    else:
        receipt['financial_applied'] = None if cny and (row['kind']=='cny' or json.loads(row['source_json']).get('cny_companions')) else False
    receipt['source_scope'] = _interval(scope,intents,cny)
    return receipt


def read_request(runtime_dir, db_path, request_id, *, request_scope, shipment_id='', allowed_actions=FINANCIAL_ACTIONS):
    unknown = {'domain':DOMAIN,'request_id':request_id,'status':'unknown','acceptance':None}
    if not Path(db_path).is_file():
        return unknown
    with ExitStack() as stack:
        conn=stack.enter_context(closing(source.readonly(db_path)))
        row = conn.execute(f'SELECT * FROM {REQUESTS} WHERE request_scope=? AND request_id=?', (request_scope,request_id)).fetchone() if source._exists(conn,REQUESTS) else None
        if row is None or row['action'] not in allowed_actions or (shipment_id and row['shipment_id']!=shipment_id):
            return unknown
        lock=stack.enter_context(_lock_path(runtime_dir,request_scope,request_id).open('r'))
        try:
            fcntl.flock(lock,fcntl.LOCK_SH | fcntl.LOCK_NB);inflight=False
            # Writer has released its one-shot lock. Refresh the pinned RO
            # snapshot under this observer lock before proving missing children.
            conn.rollback();conn.execute('BEGIN')
            row=conn.execute(f'SELECT * FROM {REQUESTS} WHERE request_scope=? AND request_id=?',(request_scope,request_id)).fetchone()
        except BlockingIOError:
            inflight=True
        manifest = json.loads(row['manifest_json']);results=[];accepted=[];pending=False
        account_proof = read_account_core(conn)
        for item in manifest:
            outcome = conn.execute(f'SELECT * FROM {OUTCOMES} WHERE request_scope=? AND request_id=? AND child_key=?', (request_scope,request_id,item['child_key'])).fetchone()
            if outcome is None:
                status = 'processing' if inflight else 'not_saved'
                result = {'child_key':item['child_key'],'status':status,'readback_confirmed':False,'error':'Сохранение ещё выполняется.' if inflight else 'Этот документ не был сохранён этой операцией.'}
                pending |= inflight
            else:
                status = outcome['status'];result=json.loads(outcome['result_json']);result.update(child_key=item['child_key'],status=status)
            if status == 'accepted':
                child = conn.execute(f'SELECT * FROM {CHILDREN} WHERE request_scope=? AND request_id=? AND child_key=?', (request_scope,request_id,item['child_key'])).fetchone()
                if child is None or result['operation_id']!=child['operation_id'] or source.digest(json.loads(child['source_json']))!=child['source_digest']:
                    return unknown
                result['acceptance']=public_child(conn,child,account_proof=account_proof);accepted.append(result['acceptance'])
            results.append(result)
        status = 'processing' if pending else 'accepted' if accepted else 'preview' if any(r['status']=='preview' for r in results) else 'rejected'
        result = {'domain':DOMAIN,'request_id':request_id,'action':'financial_'+row['action'],'wire_digest':row['wire_digest'],'payload_digest':row['payload_digest'],'status':status,'settled':not pending,'results':results,'acceptance':None,'supplier_order_id':row['shipment_id'],'shipment':{'shipment_id':row['shipment_id']},'readback_confirmed':bool(accepted)}
        if accepted:
            identity=row['operation_id']
            partial=len(accepted)!=len(results)
            complete=not pending and not partial and all(r['processing']['complete'] for r in accepted)
            result['acceptance']={'domain':DOMAIN,'operation_id':identity,'durable_saved':True,'accepted_at':row['accepted_at'],'state':'processing' if pending else 'completed' if complete else 'accepted','title_ru':TITLES[row['action']],
                'fields':[{'label':'Документы сохранены','value':str(len(accepted))},{'label':'Всего','value':str(len(results))}],
                'source_ref':{'domain':DOMAIN,'entity_id':row['shipment_id'],'action':'financial_'+row['action'],'digest':row['payload_digest']},
                'children':accepted,'partial':partial,'processing':{'kind':'batch','complete':complete},
                'reason_ru':'Сохранена часть документов. Результат каждого указан ниже.' if partial else 'Документы сохранены. Обработка проверяется отдельно.',
                'detail_path':'/sheet-vitrina-v1/supplier?operation_id='+identity,
                'journal_path':'/sheet-vitrina-v1/operations?operation_id='+identity}
            if partial:
                # A saved subset retains its child proofs, but cannot certify
                # this manifest complete or be hidden by a superseded subset.
                if not pending:result['acceptance']['state']='needs_attention'
            elif not pending and any(r['state']=='needs_attention' for r in accepted):
                result['acceptance']['state']='needs_attention'
                result['acceptance']['reason_ru']='Документы сохранены. Обработка требует внимания. Результат каждого указан ниже.'
            elif accepted and all(r['processing'].get('terminal') and not r['processing']['complete'] for r in accepted):
                result['acceptance']['state']='delayed';result['acceptance']['processing']['terminal']=True
                result['acceptance']['reason_ru']='Эту версию заменили последующими изменениями.'
                replacement=next((r['processing'].get('superseded_by') for r in accepted if r['processing'].get('superseded_by')),None)
                if replacement:result['acceptance']['processing']['superseded_by']=replacement
        elif status=='rejected':
            result['error']='; '.join(r.get('error','Документ не сохранён.') for r in results)
        return result


def execute(runtime, *, action, payload, shipment_id, actor, request_scope, manifest, validate, write_child, after_source=None):
    """One-shot request. A known manifest can only be read, never resumed here."""
    from packages.application.registry_upload_db_backed_runtime import _connect
    identity=str(payload.get('request_id') or 'supplier_financial_'+uuid4().hex)
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',identity) or action not in TITLES:
        raise ValueError('financial request identity/action is invalid')
    wire=source.verified_wire_digest(payload);operands=source.source_payload(payload)
    context={'request_scope':str(request_scope),'request_id':identity,'shipment_id':shipment_id,'actor':str(actor),'action':action,'payload_digest':source.digest(operands),'wire_digest':wire,'accepted_at':datetime.now(timezone.utc).isoformat()}
    path=_lock_path(runtime.runtime_dir,context['request_scope'],identity);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as lock:
        deadline=time.monotonic()+5
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise ValueError('financial action is still saving; read the same identity')
                time.sleep(0.01)
        with _connect(runtime.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE');ensure_schema(conn)
            prior=conn.execute(f'SELECT * FROM {REQUESTS} WHERE request_scope=? AND request_id=?',(context['request_scope'],identity)).fetchone()
            if prior:
                if prior['action']!=action or prior['shipment_id']!=shipment_id or prior['payload_digest']!=context['payload_digest'] or prior['wire_digest']!=wire or prior['manifest_json']!=source._json(manifest):
                    raise ValueError('financial identity already belongs to another source action')
                conn.rollback()
            else:
                conn.execute(f'INSERT INTO {REQUESTS} VALUES(?,?,?,?,?,?,?,?,?,?,?)',(context['request_scope'],identity,action,shipment_id,str(actor),context['payload_digest'],wire,source._json(operands),source._json(manifest),context['accepted_at'],'supplier_financial_batch_'+hashlib.sha256((context['request_scope']+'|'+identity).encode()).hexdigest()[:32]));conn.commit()
        if not prior:
            try:
                owners=validate()
            except ValueError as exc:
                for item in manifest:
                    retain_outcome(runtime,{**context,**item},'rejected',{'error':str(exc),'readback_confirmed':False})
            else:
                with closing(source.readonly(runtime.db_path)) as conn:
                    expected=capture_owners(conn,owners);expected_account=account_guard(conn)
                for item in manifest:
                    child={**context,**item,'expected_owners':expected,'expected_account':expected_account}
                    try:
                        with child_context(child):
                            result=write_child(child)
                            if after_source is not None and child.get('saved'):
                                after_source(child,result)
                        with closing(source.readonly(runtime.db_path)) as conn:
                            saved=conn.execute(f'SELECT * FROM {CHILDREN} WHERE request_scope=? AND request_id=? AND child_key=?',(context['request_scope'],identity,item['child_key'])).fetchone()
                            if saved is not None:
                                scope=json.loads(saved['scope_json']);expected=scope['after'];expected_account=scope['account_after']
                        if saved is None:
                            if result.get('preview_required') and result.get('active_saved') is False:
                                retain_outcome(runtime,child,'preview',result)
                            else:
                                raise ValueError('native source did not retain an exact financial acknowledgement')
                    except source.AlreadySaved:
                        pass
                    except ValueError as exc:
                        retain_outcome(runtime,child,'rejected',{'error':str(exc),'readback_confirmed':False})
                    except Exception:
                        # Unknown errors cannot prove a refusal. Atomic alias
                        # readback below wins; missing child is not_saved only
                        # after this request lock is released, never resubmitted.
                        break
        fcntl.flock(lock,fcntl.LOCK_UN)
        return read_request(runtime.runtime_dir,runtime.db_path,identity,request_scope=context['request_scope'],shipment_id=shipment_id,allowed_actions={action})


def adopt_existing(runtime, *, kind, subject_id, owners):
    """A new action can acknowledge an exact final source without re-submitting."""
    from packages.application.registry_upload_db_backed_runtime import _connect
    with _connect(runtime.db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        context=active()
        if context:context['adopting']=True
        guard=before_write(conn,kind=kind,subject_id=subject_id,owners=owners)
        if kind=='cny' or snapshot(conn,kind,subject_id).get('cny_companions'):
            from packages.application import cny_preparation_intents as cny
            row=conn.execute(f"SELECT * FROM {cny.TABLE} WHERE account_id='account'").fetchone()
            if row is None or not cny._matches(conn,dict(row)):
                cny.finish_source_change(conn,cny.capture_source(conn),force=True)
        record_saved(conn,kind=kind,subject_id=subject_id,guard=guard)
        conn.commit()
    context=active()
    if context and context.get('saved') and (kind=='cny' or snapshot_required_cny(runtime,kind,subject_id)):
        context['core_needed']=True


def snapshot_required_cny(runtime,kind,subject_id):
    with closing(source.readonly(runtime.db_path)) as conn:
        return bool(snapshot(conn,kind,subject_id).get('cny_companions'))


def _during_core_proof():
    """Inert seam for actual commits under the pinned RO snapshot."""


def _after_core_proof():
    """Inert seam for deterministic real inter-connection CAS tests."""


def retain_native_core(runtime, *, request, operations, replay_state):
    """Actual native core readback; full numerical proof outside the writer.

    The SAME live RO observer fences the pinned snapshot and source receipt CAS.
    Native source+account watermark remain authoritative; no submitted proof.
    """
    if not request:return False
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.registry_upload_db_backed_runtime import _connect, _cny_ledger_operation_to_dict
    from packages.application.cny_ledger import _cny_operation_revision_payload
    from packages.application import cny_preparation_intents as cny
    context=active()
    require_heavy_owner(runtime.runtime_dir)
    # Native owner may confirm a legacy-only account, with no admitted child.
    # This retains readback evidence only; it never backfills source receipts.
    with _connect(runtime.db_path) as bootstrap:
        ensure_schema(bootstrap)
        bootstrap.commit()
    token=lambda conn:int(conn.execute('PRAGMA main.data_version').fetchone()[0])
    with closing(source.readonly(runtime.db_path)) as observer:
        before=token(observer)
        if not source._exists(observer,CHILDREN):return False
        require_heavy_owner(runtime.runtime_dir)
        if context and context.get('saved'):
            children=observer.execute(f'SELECT * FROM {CHILDREN} WHERE operation_id=?',(context['saved'],)).fetchall()
        else:
            # Native latest per-document aliases only. Replaced versions cannot
            # occupy every slot and starve an unchanged admitted final source.
            children=observer.execute(f"SELECT c.* FROM {CHILDREN} c WHERE (kind='cny' OR json_array_length(source_json,'$.cny_companions')>0) AND NOT EXISTS(SELECT 1 FROM {CORE} p WHERE p.operation_id=c.operation_id AND p.account_revision=?) AND NOT EXISTS(SELECT 1 FROM {CHILDREN} newer WHERE newer.kind=c.kind AND newer.subject_id=c.subject_id AND newer.revision>c.revision) ORDER BY accepted_at,operation_id LIMIT 32", (request['revision'],)).fetchall()
        row=observer.execute(f"SELECT * FROM {cny.TABLE} WHERE account_id='account'").fetchone()
        if not row or row['revision']!=request['revision'] or row['source_fingerprint']!=request['source_fingerprint'] or not cny._matches(observer,dict(row)):return False
        native_ops=[_cny_ledger_operation_to_dict(r) for r in observer.execute('SELECT * FROM sheet_vitrina_v1_cny_ledger_operations ORDER BY sequence_key,operation_id')]
        _during_core_proof()
        expected={r['operation_id']:_cny_operation_revision_payload(r) for r in operations}
        actual={r['operation_id']:_cny_operation_revision_payload(r) for r in native_ops}
        state=observer.execute('SELECT * FROM sheet_vitrina_v1_cny_ledger_replay_state WHERE slot=1').fetchone()
        fields=('status','reason','replayed_at','operation_count','document_count','balance_cny','balance_rub_value','average_rate')
        if actual!=expected or not state or any(str(state[k])!=str(replay_state[k]) for k in fields):return False
        account_proof={'source_ref':{'native_source_id':cny.SOURCE_ID,'revision':row['revision'],'digest':row['source_fingerprint']},
            'account_revision':row['revision'],'account_source_fingerprint':row['source_fingerprint'],
            'dependency_fingerprint':row['dependency_fingerprint'],'ledger_digest':source.digest(actual),
            'source_versions':json.loads(row['source_versions_json']),'operation_revisions':list(actual.values()),
            **{k:state[k] for k in ('balance_cny','balance_rub_value','average_rate','replayed_at','status')}}
        account_identity=ACCOUNT_CORE_PREFIX+source.digest(account_proof).removeprefix('sha256:')
        proofs=[]
        for child in children:
            if source.digest(snapshot(observer,child['kind'],child['subject_id']))!=child['source_digest']:continue
            saved=json.loads(child['source_json']);ids={child['subject_id']} if child['kind']=='cny' else {r['document_id'] for r in saved.get('cny_companions',[])}
            own=[actual[r['operation_id']] for r in native_ops if r['source_document_id'] in ids]
            proof={'source_ref':{'operation_id':child['operation_id'],'revision':child['revision'],'digest':child['source_digest']},
                   'account_revision':row['revision'],'account_source_fingerprint':row['source_fingerprint'],
                   'dependency_fingerprint':row['dependency_fingerprint'],'ledger_digest':source.digest(actual),
                   'operation_revisions':own,'balance_cny':state['balance_cny'],'balance_rub_value':state['balance_rub_value'],
                   'average_rate':state['average_rate'],'replayed_at':state['replayed_at'],'status':state['status']}
            proofs.append((dict(child),proof))
        observer.commit()
        if token(observer)!=before:return False
        _after_core_proof()
        with _connect(runtime.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            live=conn.execute(f"SELECT * FROM {cny.TABLE} WHERE account_id='account'").fetchone()
            if token(observer)!=before or not live or live['revision']!=row['revision'] or live['source_fingerprint']!=row['source_fingerprint']:return False
            conn.execute(f'INSERT OR IGNORE INTO {CORE} VALUES(?,?,?,?)',(account_identity,row['revision'],source._json(account_proof),source.digest(account_proof)))
            for child,proof in proofs:
                current=conn.execute(f'SELECT source_digest FROM {CHILDREN} WHERE operation_id=?',(child['operation_id'],)).fetchone()
                if not current or current[0]!=child['source_digest']:return False
                conn.execute(f'INSERT OR IGNORE INTO {CORE} VALUES(?,?,?,?)',(child['operation_id'],row['revision'],source._json(proof),source.digest(proof)))
            if token(observer)!=before:return False
            conn.commit()
    return True


def exact_child(conn,op):
    scope=conn.execute(f'SELECT parent_operation_id FROM {SCOPES} WHERE operation_id=?',(op['operation_id'],)).fetchone()
    return conn.execute(f'SELECT * FROM {CHILDREN} WHERE operation_id=?',(scope[0],)).fetchone() if scope else None


def child_current(conn,op):
    child=exact_child(conn,op)
    return bool(child and source.digest(snapshot(conn,child['kind'],child['subject_id']))==child['source_digest'])


def child_superseded(conn,op):
    child=exact_child(conn,op)
    if not child:return False
    latest=conn.execute(f'SELECT * FROM {CHILDREN} WHERE kind=? AND subject_id=? AND revision>? ORDER BY revision DESC LIMIT 1',(child['kind'],child['subject_id'],child['revision'])).fetchone()
    return bool(latest and source.digest(snapshot(conn,latest['kind'],latest['subject_id']))==latest['source_digest'])


def read_operation(runtime_dir, db_path, operation_id, *, request_scope, allowed_actions=FINANCIAL_ACTIONS):
    unknown={'domain':DOMAIN,'operation_id':operation_id,'status':'unknown','acceptance':None}
    if not Path(db_path).is_file():
        return unknown
    with closing(source.readonly(db_path)) as conn:
        row=conn.execute(f'SELECT request_id FROM {REQUESTS} WHERE operation_id=? AND request_scope=?',(operation_id,request_scope)).fetchone() if source._exists(conn,REQUESTS) else None
        child=None
        if row is None and source._exists(conn,CHILDREN):
            child=conn.execute(f'SELECT request_id,operation_id FROM {CHILDREN} WHERE operation_id=? AND request_scope=?',(operation_id,request_scope)).fetchone()
            row=child
    if row is None:
        return unknown
    result=read_request(runtime_dir,db_path,row['request_id'],request_scope=request_scope,allowed_actions=allowed_actions)
    if child is not None:
        receipt=next((r.get('acceptance') for r in result.get('results',[]) if r.get('operation_id')==operation_id),None)
        result['acceptance']=receipt
    return result if result.get('acceptance') and result['acceptance']['operation_id']==operation_id else unknown
