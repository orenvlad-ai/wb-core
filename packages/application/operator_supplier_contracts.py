"""Supplier contract intent receipts. Native supplier preparation owns linking.

Upload + the exact native post-action are admitted in the same source transaction.
This is an audit projection, not a queue, invoice library or financial ledger.
"""
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from packages.application import operator_supplier_shipments as source
from packages.application import operator_trade_documents as library
from packages.application import supplier_preparation_intents as intents

DOMAIN = 'supplier_contract'
REQUESTS = 'sheet_vitrina_v1_operator_contract_requests'
STAGES = 'sheet_vitrina_v1_operator_contract_stages'
REFUSALS = 'sheet_vitrina_v1_operator_contract_refusals'
ACTIONS = frozenset({'upload_link', 'link', 'unlink'})
CORRELATION = 'operator_contract_operation_id'
_ACTIVE = ContextVar('operator_supplier_contract', default=None)


def now():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {REQUESTS}(
        operation_id TEXT PRIMARY KEY,request_scope TEXT NOT NULL,request_id TEXT NOT NULL,
        actor TEXT NOT NULL,action TEXT NOT NULL,shipment_id TEXT NOT NULL,payload_digest TEXT NOT NULL,
        wire_digest TEXT NOT NULL,operands_json TEXT NOT NULL,order_json TEXT NOT NULL,requested_at TEXT NOT NULL,
        UNIQUE(request_scope,request_id))''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {STAGES}(
        operation_id TEXT NOT NULL,stage TEXT NOT NULL,source_digest TEXT NOT NULL,source_json TEXT NOT NULL,
        saved_at TEXT NOT NULL,PRIMARY KEY(operation_id,stage))''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {REFUSALS}(
        operation_id TEXT PRIMARY KEY,reason TEXT NOT NULL,refused_at TEXT NOT NULL)''')
    for table in (REQUESTS, STAGES, REFUSALS):
        for event in ('UPDATE', 'DELETE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{event.lower()} BEFORE {event} ON {table}
                BEGIN SELECT RAISE(ABORT,'contract source receipt is immutable'); END''')


def _save_stage(conn, operation_id, stage, proof):
    conn.execute(f'INSERT OR IGNORE INTO {STAGES} VALUES(?,?,?,?,?)',
        (operation_id, stage, source.digest(proof), source._json(proof), now()))


def _stage(conn, operation_id, stage):
    row=conn.execute(f'SELECT * FROM {STAGES} WHERE operation_id=? AND stage=?', (operation_id, stage)).fetchone()
    proof=json.loads(row['source_json']) if row else None
    return proof if row and source.digest(proof)==row['source_digest'] else None


def _file(runtime_dir, document):
    path=Path(document.get('file_path') or '')
    if not path.is_absolute():path=Path(runtime_dir)/path
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=document.get('file_sha256'):
        raise ValueError('contract/invoice file does not match its native source SHA')


def _order_guard(conn, ctx):
    current=source.capture(conn,ctx['shipment_id'])
    if not current['header'] or current['header'].get('archived_at'):
        raise ValueError('supplier order is missing or archived')
    if source.digest(current)!=source.digest(ctx['order']):
        raise ValueError('supplier order changed before saving the contract intent; reload required')
    return current


def _pair(conn, invoice_id, target):
    invoice=library.capture(conn,invoice_id)['document']
    if invoice.get('document_type')!='invoice' or invoice.get('status')!='active':
        raise ValueError('supplier invoice source is not active')
    contract=library.capture(conn,target)['document'] if target else {}
    if target and (contract.get('document_type')!='contract' or contract.get('status')!='active'):
        raise ValueError('contract document is not active')
    return invoice,contract


def _accept_link_intent(conn, ctx, target):
    current=_order_guard(conn,ctx)
    invoice_id=current['header'].get('invoice_document_id') or ''
    invoice,contract=_pair(conn,invoice_id,target)
    _file(ctx['runtime_dir'],invoice)
    if contract:_file(ctx['runtime_dir'],contract)
    previous=intents.begin_source_change(conn,[ctx['shipment_id']])
    before=dict(conn.execute(f'SELECT * FROM {intents.TABLE} WHERE shipment_id=?',(ctx['shipment_id'],)).fetchone() or {})
    action={'invoice_document_id':invoice_id,'contract_document_id':target,'linked_by':ctx['actor'],
            'source':'operator',CORRELATION:ctx['operation_id']}
    intents.finish_source_change(conn,previous,post_actions={'invoice_contract':action})
    native=dict(conn.execute(f'SELECT * FROM {intents.TABLE} WHERE shipment_id=?',(ctx['shipment_id'],)).fetchone())
    if json.loads(native['post_actions_json']).get('invoice_contract')!=action:
        raise ValueError('supplier invoice archive superseded the contract link intent')
    _save_stage(conn,ctx['operation_id'],'link_intent',{'invoice':invoice,'contract':contract,
        'native_before':before,'native_intent':native,'order':current,
        'link_before':library.capture(conn,invoice_id)['links']})


def before_document_write(conn, document_id):
    ctx=_ACTIVE.get()
    if not ctx:return None
    if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
    _order_guard(conn,ctx)
    before=library.capture(conn,document_id)
    if before['document']!=ctx.get('expected_duplicate',{}):
        raise ValueError('contract duplicate source changed before saving; reload required')
    return {'context':ctx,'before':before}


def record_document_saved(conn, document_id, guard):
    if guard is None:return
    ctx=guard['context'];after=library.capture(conn,document_id)
    doc=after['document']
    if doc.get('document_type')!='contract' or doc.get('status')!='active' or doc.get('file_sha256')!=ctx['operands'].get('file_sha256'):
        raise ValueError('uploaded contract source identity does not match the admitted intent')
    _file(ctx['runtime_dir'],doc)
    _save_stage(conn,ctx['operation_id'],'contract_file',{'before':guard['before'],'after':after})
    _accept_link_intent(conn,ctx,document_id)


def before_link_write(conn, invoice_id, target, request, *, runtime_dir):
    if request is None:return None
    action=json.loads(request['post_actions_json']).get('invoice_contract',{})
    operation_id=action.get(CORRELATION)
    if not operation_id:return None
    from packages.application.business_data_heavy_admission import require_heavy_owner
    require_heavy_owner(runtime_dir)
    row=conn.execute(f'SELECT * FROM {REQUESTS} WHERE operation_id=?',(operation_id,)).fetchone()
    accepted=_stage(conn,operation_id,'link_intent') if row else None
    if not accepted or request['shipment_id']!=row['shipment_id'] or accepted['invoice']['document_id']!=invoice_id or accepted['contract'].get('document_id','')!=target:
        raise ValueError('native contract continuation does not match its exact accepted intent')
    invoice,contract=_pair(conn,invoice_id,target)
    _file(runtime_dir,invoice)
    if contract:_file(runtime_dir,contract)
    if invoice!=accepted['invoice'] or contract!=accepted['contract']:
        raise ValueError('native contract source changed before link continuation')
    return {'operation_id':operation_id,'native_request':request,'before':library.capture(conn,invoice_id),
            'contract':contract,'invoice':invoice}


def record_link_applied(conn, invoice_id, target, guard):
    if guard is None:return
    after=library.capture(conn,invoice_id)
    actual=next((r['contract_document_id'] for r in after['links'] if r['invoice_document_id']==invoice_id),'')
    if actual!=target:raise ValueError('native contract link/unlink readback did not match accepted intent')
    _save_stage(conn,guard['operation_id'],'link_applied',{**guard,'after':after})


def _materialize_invoice(block, ctx):
    """Source-only, one-order canonical invoice materialization; no scan/parse."""
    from packages.application.registry_upload_db_backed_runtime import _connect,_ensure_schema,_save_trade_document_in_connection
    header=ctx['order']['header']
    if header.get('invoice_document_id'):
        with closing(source.readonly(block.runtime.db_path)) as conn:
            invoice,_=_pair(conn,header['invoice_document_id'],'')
        _file(ctx['runtime_dir'],invoice);return
    path=Path(ctx['runtime_dir'])/str(header.get('source_file_path') or '')
    if not path.is_file():raise ValueError('supplier invoice source file is missing')
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    if header.get('source_file_sha256') and sha!=header['source_file_sha256']:
        raise ValueError('supplier invoice source SHA changed before materialization')
    # Same native metadata and source type as the existing migration primitive.
    from packages.application.supplier_shipments import legacy_invoice_document_source
    with _connect(block.runtime.db_path) as conn:
        _ensure_schema(conn);conn.execute('BEGIN IMMEDIATE');ensure_schema(conn)
        _order_guard(conn,ctx)
        existing=conn.execute(f"SELECT * FROM {library.DOCS} WHERE document_type='invoice' AND file_sha256=? AND source_shipment_id=? ORDER BY created_at ASC LIMIT 1",(sha,ctx['shipment_id'])).fetchone()
        document=dict(existing) if existing else legacy_invoice_document_source(header,ctx['shipment_id'],sha,block.timestamp_factory())
        if existing and existing['status']!='active':raise ValueError('supplier invoice source is archived')
        if not existing:_save_trade_document_in_connection(conn,document)
        conn.execute('UPDATE sheet_vitrina_v1_supplier_shipments SET invoice_document_id=?,updated_at=? WHERE shipment_id=?',
            (document['document_id'],block.timestamp_factory(),ctx['shipment_id']))
        after=source.capture(conn,ctx['shipment_id'])
        invoice=library.capture(conn,document['document_id'])['document'];_file(ctx['runtime_dir'],invoice)
        _save_stage(conn,ctx['operation_id'],'invoice_source',{'before':ctx['order'],'after':after,'invoice':invoice})
        conn.commit();ctx['order']=after


def _lookup(conn, scope, identity):
    return conn.execute(f'SELECT * FROM {REQUESTS} WHERE request_scope=? AND request_id=?',(scope,identity)).fetchone() if source._exists(conn,REQUESTS) else None


def _public(conn,row):
    row=dict(row);op=row['operation_id'];accepted=_stage(conn,op,'link_intent');file=_stage(conn,op,'contract_file');applied=_stage(conn,op,'link_applied')
    refusal=conn.execute(f'SELECT * FROM {REFUSALS} WHERE operation_id=?',(op,)).fetchone()
    result={'domain':DOMAIN,'request_id':row['request_id'],'action':row['action'],'wire_digest':row['wire_digest'],
            'status':'unknown','settled':False,'acceptance':None,'stages':[]}
    if file:
        doc=file['after']['document'];result['stages'].append({'stage':'contract_file','durable_saved':True,'document_id':doc['document_id'],'file_sha256':doc['file_sha256']})
    if not accepted:
        if refusal:result.update(status='partial' if file else 'rejected',settled=True,error=refusal['reason'])
        return result
    native=accepted['native_intent'];current=dict(conn.execute(f'SELECT * FROM {intents.TABLE} WHERE shipment_id=?',(row['shipment_id'],)).fetchone() or {})
    action=json.loads(current.get('post_actions_json') or '{}').get('invoice_contract',{})
    processing={'kind':'native_contract_link','complete':bool(applied),'physical_link_applied':bool(applied and accepted['contract']),
                'physical_unlink_applied':bool(applied and not accepted['contract']),'cost_applicable':False,
                'native_intent_revision':native['revision'],'applied_revision':applied['native_request']['revision'] if applied else None}
    state='completed' if applied else 'processing'
    reason=('Связь с договором сохранена.' if accepted['contract'] else 'Связь с договором удалена.') if applied else 'Документ сохранён. Связь ожидает обработки.'
    if applied:
        before=[r['contract_document_id'] for r in applied['before']['links']]
        after=[r['contract_document_id'] for r in applied['after']['links']]
        processing['effect']='unchanged' if before==after else 'linked' if accepted['contract'] else 'unlinked'
    if not applied and current.get('revision',0)>native['revision'] and action.get(CORRELATION)!=op:
        successor=conn.execute(f'SELECT operation_id FROM {REQUESTS} WHERE operation_id=? AND request_scope=?',
            (action.get(CORRELATION),row['request_scope'])).fetchone()
        state='needs_attention';processing.update(terminal=True,reason_code='superseded',superseded_by=successor[0] if successor else None);reason='Эту связь заменили последующими изменениями.'
    elif not applied and current.get('error'):
        state='needs_attention';processing['reason_code']='native_link_attention';reason='Договор сохранён. Связь требует внимания.'
    result['stages'].append({'stage':'link_intent','durable_saved':True,'native_revision':native['revision'],
        'invoice_document_id':accepted['invoice']['document_id'],'contract_document_id':accepted['contract'].get('document_id','')})
    result.update(status='accepted',settled=True,acceptance={'domain':DOMAIN,'operation_id':op,'durable_saved':True,
        'accepted_at':conn.execute(f'SELECT saved_at FROM {STAGES} WHERE operation_id=? AND stage=?',(op,'link_intent')).fetchone()[0],
        'actor':row['actor'],'state':state,'title_ru':'Договор заказа поставщику',
        'source_ref':{'domain':DOMAIN,'entity_id':row['shipment_id'],'action':row['action'],
            'invoice_document_id':accepted['invoice']['document_id'],'contract_document_id':accepted['contract'].get('document_id',''),
            'native_intent_revision':native['revision'],'source_digest':source.digest(accepted),
            'invoice_source_digest':source.digest(accepted['invoice']),'contract_source_digest':source.digest(accepted['contract'])},
        'processing':processing,'reason_ru':reason,'fields':[{'label':'Заказ','value':row['shipment_id']},
            {'label':'Договор','value':accepted['contract'].get('number') or accepted['contract'].get('file_original_name') or 'Связь удалена'}],
        'detail_path':'/sheet-vitrina-v1/supplier?embedded=operator&operation_id='+op})
    if processing.get('superseded_by'):
        result['acceptance']['native_path']='/sheet-vitrina-v1/supplier?embedded=operator&operation_id='+processing['superseded_by']
    return result


def read(db_path,identity,*,shipment_id,request_scope,operation=False):
    unknown={'domain':DOMAIN,'status':'unknown','settled':False,'acceptance':None,'request_id':identity}
    if not Path(db_path).is_file():return unknown
    with closing(source.readonly(db_path)) as conn:
        row=conn.execute(f'SELECT * FROM {REQUESTS} WHERE operation_id=? AND request_scope=? AND shipment_id=?',(identity,request_scope,shipment_id)).fetchone() if operation and source._exists(conn,REQUESTS) else _lookup(conn,request_scope,identity) if not operation else None
        if not row or row['shipment_id']!=shipment_id or row['action'] not in ACTIONS:return unknown
        return _public(conn,row)


def read_operation(db_path,operation_id,*,request_scope):
    if not Path(db_path).is_file():return None
    with closing(source.readonly(db_path)) as conn:
        row=conn.execute(f'SELECT * FROM {REQUESTS} WHERE operation_id=? AND request_scope=?',(operation_id,request_scope)).fetchone() if source._exists(conn,REQUESTS) else None
        return _public(conn,row) if row and row['action'] in ACTIONS else None


def accept(block,shipment_id,payload,*,action,request_scope,actor,native_upload=None):
    from packages.application.registry_upload_db_backed_runtime import _connect,_ensure_schema
    identity=str(payload.get('request_id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',identity) or action not in ACTIONS:raise ValueError('contract intent requires exact request identity')
    operands=source.source_payload(payload);wire=source.verified_wire_digest(payload)
    fingerprint=source.digest({'shipment_id':shipment_id,'action':action,'operands':operands})
    rt=block.runtime;rt.runtime_dir.mkdir(parents=True,exist_ok=True)
    with (rt.runtime_dir/'.operator-supplier-contract.lock').open('a') as lock:
        deadline=time.monotonic()+5
        while True:
            try:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic()>=deadline:
                    raise ValueError('contract intent is still saving; read the same request identity')
                time.sleep(0.01)
        if rt.db_path.is_file():
            with closing(source.readonly(rt.db_path)) as conn:
                prior=_lookup(conn,request_scope,identity)
                if prior:
                    if prior['action']!=action or prior['shipment_id']!=shipment_id or prior['payload_digest']!=fingerprint or prior['wire_digest']!=wire:
                        raise ValueError('contract request identity already belongs to another source intent')
                    return _public(conn,prior)
        with _connect(rt.db_path) as conn:
            _ensure_schema(conn);conn.execute('BEGIN IMMEDIATE');ensure_schema(conn)
            prior=_lookup(conn,request_scope,identity)
            if prior:
                if prior['action']!=action or prior['shipment_id']!=shipment_id or prior['payload_digest']!=fingerprint or prior['wire_digest']!=wire:
                    raise ValueError('contract request identity already belongs to another source intent')
                return _public(conn,prior)
            captured=source.capture(conn,shipment_id);operation_id='supplier_contract_'+uuid4().hex
            conn.execute(f'INSERT INTO {REQUESTS} VALUES(?,?,?,?,?,?,?,?,?,?,?)',(operation_id,request_scope,identity,actor,action,shipment_id,fingerprint,wire,source._json(operands),source._json(captured),now()));conn.commit()
        ctx={'operation_id':operation_id,'shipment_id':shipment_id,'order':captured,'actor':actor,'runtime_dir':rt.runtime_dir,'operands':operands}
        try:
            _materialize_invoice(block,ctx)
            if action=='upload_link':
                with (rt.runtime_dir/'.operator-trade-source.lock').open('a') as shared:
                    fcntl.flock(shared.fileno(),fcntl.LOCK_EX)
                    with closing(source.readonly(rt.db_path)) as conn:
                        duplicate=conn.execute(f"SELECT * FROM {library.DOCS} WHERE document_type='contract' AND file_sha256=? AND source='settings_upload' AND status='active' ORDER BY created_at ASC LIMIT 1",(operands['file_sha256'],)).fetchone()
                        ctx['expected_duplicate']=dict(duplicate) if duplicate else {}
                    token=_ACTIVE.set(ctx)
                    try:
                        result=native_upload(ctx['order']['header'])
                        # Native unchanged SHA duplicate: adopt exact final source + link intent in one source CAS.
                        with _connect(rt.db_path) as conn:
                            conn.execute('BEGIN IMMEDIATE')
                            if not _stage(conn,operation_id,'link_intent'):
                                doc=result.get('document') or {};guard=before_document_write(conn,doc.get('document_id',''))
                                record_document_saved(conn,doc.get('document_id',''),guard)
                            conn.commit()
                    finally:_ACTIVE.reset(token)
            else:
                with _connect(rt.db_path) as conn:
                    conn.execute('BEGIN IMMEDIATE');_accept_link_intent(conn,ctx,str(operands.get('contract_document_id') or '') if action=='link' else '');conn.commit()
        except ValueError as error:
            with _connect(rt.db_path) as conn:
                conn.execute('BEGIN IMMEDIATE')
                if not _stage(conn,operation_id,'link_intent'):
                    conn.execute(f'INSERT OR IGNORE INTO {REFUSALS} VALUES(?,?,?)',(operation_id,str(error).replace('\n',' ')[:500],now()))
                conn.commit()
        return read(rt.db_path,identity,shipment_id=shipment_id,request_scope=request_scope)
