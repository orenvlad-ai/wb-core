"""Direct CNY source actions using native financial acceptance and consumers.

No ledger, queue or source retry lives here. Retained action separates this
projection from supplier attachments, including before journal enumeration.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path

from packages.application import operator_supplier_financial as financial
from packages.application import operator_supplier_shipments as source
from packages.application import cny_preparation_intents as intents

DOMAIN = 'cny_account_document'
ACTIONS = frozenset({'cny_upload', 'cny_opening', 'cny_exclude', 'cny_restore', 'cny_relink'})


def _project(result, *, db_path=None, request_scope=''):
    result['domain'] = DOMAIN
    receipt = result.get('acceptance')
    if not receipt:
        if result.get('status') == 'preview' and len(result.get('results', [])) == 1:
            result.update({k: v for k, v in result['results'][0].items() if k not in {'status', 'child_key'}})
        return result
    receipts = [receipt, *receipt.get('children', [])]
    for item in result.get('results', []):
        if item.get('acceptance'):
            receipts.append(item['acceptance'])
    seen = set()
    for child in receipts:
        if id(child) in seen:
            continue
        seen.add(id(child))
        child['domain'] = DOMAIN
        child['source_ref']['domain'] = DOMAIN
        child['detail_path'] = '/sheet-vitrina-v1/operator?embedded_tab=factory-order&operation_id=' + child['operation_id']
        replacement = child.get('processing', {}).get('superseded_by')
        if replacement and db_path and operation_action(db_path, replacement['operation_id'], request_scope=request_scope) in ACTIONS:
            replacement['domain'] = DOMAIN
            replacement['detail_path'] = '/sheet-vitrina-v1/operator?embedded_tab=factory-order&operation_id=' + replacement['operation_id']
        # Source-only is a cost fact. It does not prove monetary application.
        if child.get('processing', {}).get('kind') != 'batch':
            child['processing']['cost_complete'] = child['processing']['complete']
            child['processing']['complete'] = bool(child['processing']['cost_complete'] and child.get('financial_applied') is True)
            if child.get('financial_applied') is None and child['state'] == 'completed':
                child['state'] = 'processing'
                child['reason_ru'] = 'Документ сохранён. Финансовая обработка ожидает подтверждения.'
    if receipt.get('children'):
        receipt['processing']['complete'] = all(c['processing']['complete'] for c in receipt['children'])
        if receipt['state'] == 'completed' and not receipt['processing']['complete']:
            receipt['state'] = 'processing'
            receipt['reason_ru'] = 'Документ сохранён. Финансовая обработка ожидает подтверждения.'
    return result


def read_request(runtime_dir, db_path, request_id, *, request_scope):
    return _project(financial.read_request(runtime_dir, db_path, request_id,
        request_scope=request_scope, allowed_actions=ACTIONS), db_path=db_path, request_scope=request_scope)


def read_operation(runtime_dir, db_path, operation_id, *, request_scope):
    return _project(financial.read_operation(runtime_dir, db_path, operation_id,
        request_scope=request_scope, allowed_actions=ACTIONS), db_path=db_path, request_scope=request_scope)


def operation_action(db_path, operation_id, *, request_scope):
    """Resolve retained action before selecting a public domain; foreign is unknown."""
    if not Path(db_path).is_file():
        return None
    with closing(source.readonly(db_path)) as conn:
        if not source._exists(conn, financial.REQUESTS):
            return None
        row = conn.execute(f'SELECT action FROM {financial.REQUESTS} WHERE request_scope=? AND operation_id=?',
            (request_scope, operation_id)).fetchone()
        if row is None and source._exists(conn, financial.CHILDREN):
            row = conn.execute(f'SELECT r.action FROM {financial.CHILDREN} c JOIN {financial.REQUESTS} r ON r.request_scope=c.request_scope AND r.request_id=c.request_id WHERE c.request_scope=? AND c.operation_id=?',
                (request_scope, operation_id)).fetchone()
        return row[0] if row else None


def execute(block, *, action, payload, request_scope, actor, native_write, document_id='', target_shipment_id=''):
    if action not in ACTIONS:
        raise ValueError('CNY source action is invalid')
    runtime = block.runtime
    manifest = [{'child_key': action + ':' + (document_id or 'document'), 'kind': 'cny', 'subject_id': document_id}]

    def validate():
        owners = set()
        if document_id:
            document = runtime.load_cny_document(document_id)
            if document is None:
                raise ValueError('CNY document not found')
            if document.get('linked_financial_document_id'):
                raise ValueError('Измените исходный финансовый документ в карточке заказа.')
            owners.add(document.get('source_order_id') or '')
        if target_shipment_id:
            target = runtime.load_supplier_shipment(target_shipment_id)
            if target is None or target['header'].get('archived_at'):
                raise ValueError('Целевой заказ отсутствует или архивирован.')
            owners.add(target_shipment_id)
        request = intents.read_account_request(runtime)
        owners.update(json.loads((request or {}).get('affected_shipment_ids_json') or '[]'))
        return sorted(owners - {''})

    def write(child):
        result = native_write()
        if result.get('preview_required') and result.get('durable_saved') is False:
            return {**result, 'active_saved': False}
        # Native same-natural-key upload/relink has already-final source and
        # must acknowledge it without writing that source a second time.
        if not child.get('saved') and result.get('document_id'):
            existing = runtime.load_cny_document(result['document_id'])
            financial.adopt_existing(runtime, kind='cny', subject_id=result['document_id'],
                owners=[existing.get('source_order_id') or ''])
        return result

    def after_source(child, result):
        if not child.get('core_done'):
            block.replay_ledger(reason=action)

    result = financial.execute(runtime, action=action, payload=payload, shipment_id=target_shipment_id,
        actor=actor, request_scope=request_scope, manifest=manifest, validate=validate,
        write_child=write, after_source=after_source)
    return _project(result, db_path=runtime.db_path, request_scope=request_scope)


def upload(block, file_bytes, *, fields, filename, content_type, request_scope, actor):
    # The file hash is computed from received bytes, never trusted from fields.
    payload = {**dict(fields or {}), 'file_sha256': hashlib.sha256(file_bytes).hexdigest(),
        'filename': str(filename or 'cny-document.pdf')}
    payload.setdefault('payment_date', '')
    return execute(block, action='cny_upload', payload=payload, request_scope=request_scope, actor=actor,
        native_write=lambda: block.upload_document(file_bytes=file_bytes, uploaded_filename=filename,
            uploaded_content_type=content_type, manual_payment_date=payload['payment_date'] or None,
            manual_payment_date_actor=actor))


def read_status(block):
    """Consistent RO native records, with exact financial watermark separately."""
    from packages.application.cny_ledger import (
        CNY_LEDGER_CONTRACT_NAME, _ledger_summary, _ledger_diagnostics, _conversion_row,
        CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE, _cny_operation_revision_payload,
    )
    from packages.application.registry_upload_db_backed_runtime import _cny_document_to_dict, _cny_ledger_operation_to_dict
    documents, operations, state, request, proof = [], [], {}, {}, None
    db_path = block.runtime.db_path
    if Path(db_path).is_file():
        with closing(source.readonly(db_path)) as conn:
            if source._exists(conn, 'sheet_vitrina_v1_cny_documents'):
                documents = [block._with_download_path(_cny_document_to_dict(r)) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_cny_documents ORDER BY operation_date,document_id')]
            if source._exists(conn, 'sheet_vitrina_v1_cny_ledger_operations'):
                operations = [_cny_ledger_operation_to_dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_cny_ledger_operations ORDER BY sequence_key,operation_id')]
            if source._exists(conn, 'sheet_vitrina_v1_cny_ledger_replay_state'):
                row = conn.execute('SELECT * FROM sheet_vitrina_v1_cny_ledger_replay_state WHERE slot=1').fetchone()
                if row:
                    state = {k: row[k] for k in ('status','reason','replayed_at','operation_count','document_count','balance_cny','balance_rub_value','average_rate')}
                    state['diagnostics'] = json.loads(row['diagnostics_json'] or '[]')
            if source._exists(conn, intents.TABLE):
                request = dict(conn.execute(f"SELECT * FROM {intents.TABLE} WHERE account_id='account'").fetchone() or {})
            proof = financial.read_account_core(conn, request=request, operations=operations, state=state)
    continuation = intents._outcome(request) if request else {}
    return {'contract_name': CNY_LEDGER_CONTRACT_NAME, 'status': 'ok',
        'summary': _ledger_summary(documents, operations, state), 'documents': documents,
        'conversions': [_conversion_row(d) for d in documents if d['document_type'] == CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE and d['status'] != 'excluded'],
        'ledger_operations': operations, 'replay': state, 'diagnostics': _ledger_diagnostics(operations, state),
        'account_preparation': continuation,
        'financial_authority': {'current': bool(proof), 'account_revision': request.get('revision'),
            'source_fingerprint': request.get('source_fingerprint'), 'replayed_at': proof['replayed_at'] if proof else None},
        'empty_state': 'Загрузите документы конвертации или задайте начальный остаток' if not documents else ''}
