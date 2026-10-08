"""Immutable library source receipts; native document/link transactions own writes.

No work queue, financial authority or cost completion is inferred by this adapter.
"""
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from packages.application.operator_supplier_shipments import digest, readonly, source_payload, verified_wire_digest

DOMAIN = 'trade_document_library'
TABLE = 'sheet_vitrina_v1_operator_trade_receipts'
REJECTIONS = 'sheet_vitrina_v1_operator_trade_rejections'
DOCS = 'sheet_vitrina_v1_trade_documents'
LINKS = 'sheet_vitrina_v1_invoice_contract_links'
TITLES = {'upload': 'Сохранение документа в библиотеке', 'edit': 'Изменение реквизитов документа',
          'archive': 'Архивирование документа', 'link': 'Связь invoice с договором', 'unlink': 'Удаление связи invoice с договором'}
ACTIONS = frozenset({'upload', 'edit', 'archive', 'link', 'unlink'})
_ACTIVE = ContextVar('operator_trade_action', default=None)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def ensure_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE}(
        operation_id TEXT PRIMARY KEY,request_scope TEXT NOT NULL,request_id TEXT NOT NULL,
        actor TEXT NOT NULL,action TEXT NOT NULL,payload_digest TEXT NOT NULL,wire_digest TEXT NOT NULL,
        document_id TEXT NOT NULL,revision INTEGER NOT NULL,source_digest TEXT NOT NULL,
        before_json TEXT NOT NULL,source_json TEXT NOT NULL,accepted_at TEXT NOT NULL,
        UNIQUE(request_scope,request_id),UNIQUE(document_id,revision))''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {REJECTIONS}(
        request_scope TEXT NOT NULL,request_id TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,
        payload_digest TEXT NOT NULL,wire_digest TEXT NOT NULL,reason TEXT NOT NULL,
        PRIMARY KEY(request_scope,request_id))''')
    for table in (TABLE, REJECTIONS):
        for event in ('UPDATE', 'DELETE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{event.lower()} BEFORE {event} ON {table}
                BEGIN SELECT RAISE(ABORT,'library acknowledgement is immutable'); END''')


def capture(conn, document_id):
    doc = conn.execute(f'SELECT * FROM {DOCS} WHERE document_id=?', (document_id,)).fetchone() if exists(conn, DOCS) else None
    links = conn.execute(f'''SELECT * FROM {LINKS} WHERE invoice_document_id=? OR contract_document_id=?
        ORDER BY invoice_document_id''', (document_id, document_id)).fetchall() if exists(conn, LINKS) else []
    return {'document': dict(doc) if doc else {}, 'links': [dict(row) for row in links]}


def _link_intents(conn, document_id):
    from packages.application.supplier_preparation_intents import TABLE as intents
    shipments = 'sheet_vitrina_v1_supplier_shipments'
    if not exists(conn, intents) or not exists(conn, shipments):
        return []
    return [dict(row) for row in conn.execute(f"SELECT i.* FROM {intents} i JOIN {shipments} s ON s.shipment_id=i.shipment_id WHERE s.invoice_document_id=? ORDER BY i.shipment_id", (document_id,))]


def _lookup(conn, context):
    row = conn.execute(f'SELECT * FROM {TABLE} WHERE request_scope=? AND request_id=?',
        (context['request_scope'], context['request_id'])).fetchone() if exists(conn, TABLE) else None
    reject = conn.execute(f'SELECT * FROM {REJECTIONS} WHERE request_scope=? AND request_id=?',
        (context['request_scope'], context['request_id'])).fetchone() if exists(conn, REJECTIONS) else None
    for saved in (row, reject):
        if saved and any(saved[key] != context[key] for key in ('action', 'payload_digest', 'wire_digest')):
            raise ValueError('library request identity already belongs to another action')
    return row or reject


class AlreadySaved(Exception):
    pass


def active():
    return _ACTIVE.get()


def before_write(conn, document_id, *, kind):
    ctx = active()
    if not ctx:
        return
    expected_kind = 'link' if ctx['action'] in ('link', 'unlink') else 'document'
    if kind != expected_kind:
        raise ValueError('library action does not match native writer')
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    ensure_schema(conn)
    if _lookup(conn, ctx):
        raise AlreadySaved()
    if ctx['document_id'] and document_id != ctx['document_id']:
        raise ValueError('library source identity changed before save')
    ctx['document_id'] = document_id
    current = capture(conn, document_id)
    if ctx['action'] != 'upload' and digest(current) != ctx['expected_source']:
        raise ValueError('library source changed before save; reload required')
    if ctx['action'] == 'upload' and current['document'] and ctx.get('expected_source') is not None and digest(current) != ctx['expected_source']:
        raise ValueError('library duplicate source changed before save')
    ctx['before'] = current
    ctx['before']['native_link_intents'] = _link_intents(conn, document_id)
    if ctx['action'] in ('link', 'unlink'):
        invoice = current['document']
        if invoice.get('document_type') != 'invoice' or invoice.get('status') != 'active':
            raise ValueError('invoice document is not active')
        target = ctx['operands'].get('contract_document_id', '') if ctx['action'] == 'link' else ''
        if target:
            contract = capture(conn, target)['document']
            if contract.get('document_type') != 'contract' or contract.get('status') != 'active':
                raise ValueError('contract document is not active')
            ctx['before']['target_contract'] = contract


def record_saved(conn, document_id):
    ctx = active()
    if not ctx:
        return
    if not conn.in_transaction or ctx.get('document_id') != document_id or 'before' not in ctx:
        raise ValueError('library acknowledgement requires exact native source transaction')
    source = capture(conn, document_id)
    source['native_link_intents'] = _link_intents(conn, document_id)
    document = source['document']
    if not document:
        raise ValueError('library acknowledgement has no saved source')
    if ctx['action'] == 'link':
        target = ctx['operands'].get('contract_document_id')
        if not any(row['invoice_document_id'] == document_id and row['contract_document_id'] == target for row in source['links']):
            raise ValueError('library link was not saved')
        source['target_contract'] = capture(conn, target)['document']
    if ctx['action'] == 'unlink' and any(row['invoice_document_id'] == document_id for row in source['links']):
        raise ValueError('library unlink was not saved')
    if ctx['action'] == 'archive' and document['status'] != 'archived':
        raise ValueError('library archive was not saved')
    if ctx['action'] == 'upload' and document['file_sha256'] != ctx['operands'].get('file_sha256'):
        raise ValueError('library file source does not match uploaded bytes')
    path = Path(document['file_path'])
    if not path.is_absolute():
        path = ctx['runtime_dir'] / path
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != document['file_sha256']:
        raise ValueError('library saved file readback does not match native SHA')
    revision = conn.execute(f'SELECT COALESCE(MAX(revision),0)+1 FROM {TABLE} WHERE document_id=?', (document_id,)).fetchone()[0]
    conn.execute(f'INSERT INTO {TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('trade_library_' + uuid4().hex, ctx['request_scope'], ctx['request_id'], ctx['actor'], ctx['action'],
         ctx['payload_digest'], ctx['wire_digest'], document_id, revision, digest(source),
         _json(ctx['before']), _json(source), datetime.now(timezone.utc).isoformat()))


def _public(row):
    source = json.loads(row['source_json']); document = source['document']
    receipt = {'domain': DOMAIN, 'operation_id': row['operation_id'], 'durable_saved': True,
        'accepted_at': row['accepted_at'], 'actor': row['actor'], 'state': 'completed', 'title_ru': TITLES[row['action']],
        'source_ref': {'domain': DOMAIN, 'document_id': row['document_id'], 'revision': row['revision'],
                       'action': row['action'], 'source_digest': row['source_digest'], 'file_sha256': document['file_sha256'],
                       'document_type': document['document_type']},
        'fields': [{'label': 'Документ', 'value': document.get('number') or document['file_original_name']},
                   {'label': 'Версия изменения', 'value': str(row['revision'])}],
        'processing': {'complete': True, 'cost_applicable': False, 'kind': 'library_source_only',
                       'reason_ru': 'Изменение библиотеки сохранено.'},
        'detail_path': '/sheet-vitrina-v1/settings?embedded=1&operation_id=' + row['operation_id']}
    return {'domain': DOMAIN, 'status': 'accepted', 'settled': True, 'request_id': row['request_id'],
            'action': row['action'], 'wire_digest': row['wire_digest'], 'acceptance': receipt}


def read_request(db_path, request_id, *, request_scope):
    unknown = {'domain': DOMAIN, 'status': 'unknown', 'settled': False, 'request_id': request_id, 'acceptance': None}
    if not Path(db_path).is_file():
        return unknown
    with closing(readonly(db_path)) as conn:
        row = conn.execute(f'SELECT * FROM {TABLE} WHERE request_scope=? AND request_id=?',
            (request_scope, request_id)).fetchone() if exists(conn, TABLE) else None
        if row and row['action'] in ACTIONS:
            return _public(row)
        rejected = conn.execute(f'SELECT * FROM {REJECTIONS} WHERE request_scope=? AND request_id=?',
            (request_scope, request_id)).fetchone() if exists(conn, REJECTIONS) else None
        if rejected and rejected['action'] in ACTIONS:
            return {**unknown, 'status': 'rejected', 'settled': True, 'action': rejected['action'],
                    'wire_digest': rejected['wire_digest'], 'error': rejected['reason']}
    return unknown


def read_operation(db_path, operation_id, *, request_scope):
    if not Path(db_path).is_file():
        return None
    with closing(readonly(db_path)) as conn:
        row = conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=? AND request_scope=?',
            (operation_id, request_scope)).fetchone() if exists(conn, TABLE) else None
        return _public(row) if row and row['action'] in ACTIONS else None


def read_documents(block, *, document_id=None):
    """The native library SELECT/serializer on a read-only snapshot, no bootstrap."""
    from packages.application.registry_upload_db_backed_runtime import _trade_document_to_dict
    if not block.runtime.db_path.is_file():
        return []
    with closing(readonly(block.runtime.db_path)) as conn:
        if not exists(conn, DOCS):
            return []
        where = 'WHERE d.document_id=?' if document_id is not None else "WHERE d.status='active'"
        rows = conn.execute(f"""SELECT d.*,link.contract_document_id AS linked_contract_document_id,
            contract.number AS linked_contract_number,contract.document_date AS linked_contract_date,
            COALESCE(invoice_counts.invoice_count,0) AS linked_invoice_count
            FROM {DOCS} d LEFT JOIN {LINKS} link ON link.invoice_document_id=d.document_id
            LEFT JOIN {DOCS} contract ON contract.document_id=link.contract_document_id
            LEFT JOIN (SELECT contract_document_id,COUNT(*) AS invoice_count FROM {LINKS}
                GROUP BY contract_document_id) invoice_counts ON invoice_counts.contract_document_id=d.document_id
            {where} ORDER BY d.updated_at DESC,d.created_at DESC,d.document_id ASC""",
            (document_id,) if document_id is not None else ()).fetchall()
        return [block._with_document_download_path(_trade_document_to_dict(row)) for row in rows]


def execute(block, *, action, payload, native_write, request_scope, actor, document_id=''):
    runtime = block.runtime
    identity = str(payload.get('request_id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', identity) or action not in ACTIONS:
        raise ValueError('library action requires an exact request identity')
    operands = source_payload(payload)
    ctx = {'request_id': identity, 'request_scope': str(request_scope), 'actor': str(actor), 'action': action,
        'payload_digest': digest({'document_id': document_id, 'operands': operands}),
        'wire_digest': verified_wire_digest(payload), 'operands': operands, 'document_id': str(document_id),
        'runtime_dir': runtime.runtime_dir}
    runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
    with (runtime.runtime_dir / '.operator-trade-source.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if runtime.db_path.is_file():
            with closing(readonly(runtime.db_path)) as conn:
                prior = _lookup(conn, ctx)
                if prior:
                    return read_request(runtime.db_path, identity, request_scope=request_scope)
                ctx['expected_source'] = digest(capture(conn, document_id)) if document_id else None
        else:
            ctx['expected_source'] = None
        token = _ACTIVE.set(ctx)
        try:
            try:
                result = native_write(operands)
                # An unchanged native SHA duplicate can be acknowledged in a short
                # source CAS transaction; it is never uploaded again.
                if read_request(runtime.db_path, identity, request_scope=request_scope)['status'] == 'unknown':
                    doc = (result or {}).get('document', {})
                    if not doc.get('document_id'):
                        raise ValueError('library native action did not retain a source')
                    from packages.application.registry_upload_db_backed_runtime import _connect
                    with _connect(runtime.db_path) as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        before_write(conn, doc['document_id'], kind='document')
                        record_saved(conn, doc['document_id']); conn.commit()
            except AlreadySaved:
                pass
            except ValueError as error:
                if read_request(runtime.db_path, identity, request_scope=request_scope)['status'] != 'accepted':
                    from packages.application.registry_upload_db_backed_runtime import _connect
                    with _connect(runtime.db_path) as conn:
                        conn.execute('BEGIN IMMEDIATE'); ensure_schema(conn)
                        if not _lookup(conn, ctx):
                            conn.execute(f'INSERT INTO {REJECTIONS} VALUES(?,?,?,?,?,?,?)',
                                (request_scope, identity, actor, action, ctx['payload_digest'], ctx['wire_digest'], str(error).replace('\n', ' ')[:500]))
                        conn.commit()
            return read_request(runtime.db_path, identity, request_scope=request_scope)
        finally:
            _ACTIVE.reset(token)
