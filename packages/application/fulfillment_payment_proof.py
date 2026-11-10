"""Immutable executed-bank-PDF evidence for an existing FF service expense.

This module never changes expense amounts, quantities, cost dates, or capital.
Preview retains the exact PDF; confirmation only attaches it to an existing
accepted XLSX. GET is the recovery path after an uncertain confirmation.
"""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from packages.application import operator_fulfillment_services as receipts
from packages.application.russian_payment_orders import parse_russian_payment_order_pdf
from packages.application.sqlite_contention import connect_sqlite

PREVIEWS = 'sheet_vitrina_v1_fulfillment_payment_previews'
CONFIRMATIONS = 'sheet_vitrina_v1_fulfillment_payment_confirmations'
UPLOADS = 'sheet_vitrina_v1_fulfillment_service_uploads'
LINES = 'sheet_vitrina_v1_fulfillment_service_lines'
OVERHEAD = 'sheet_vitrina_v1_ff_pool_overhead_payment_evidence'
BASE = '/v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/'
MAX_BYTES = 16 * 1024**2


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _money(value):
    try:
        amount = Decimal(str(value))
        _require(amount.is_finite() and amount > 0 and amount == amount.quantize(Decimal('.01')),
                 'fulfillment_payment_invalid_amount')
        return format(amount, '.2f')
    except (InvalidOperation, TypeError):
        raise ValueError('fulfillment_payment_invalid_amount') from None


def ensure_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {PREVIEWS}(
        operation_id TEXT PRIMARY KEY, upload_id TEXT NOT NULL,
        payment_fingerprint TEXT NOT NULL, file_sha256 TEXT NOT NULL,
        preview_fingerprint TEXT NOT NULL, source_json TEXT NOT NULL,
        parser_json TEXT NOT NULL, filename TEXT NOT NULL,
        pdf_bytes BLOB NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL,
        UNIQUE(upload_id,payment_fingerprint))''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {CONFIRMATIONS}(
        operation_id TEXT PRIMARY KEY REFERENCES {PREVIEWS}(operation_id),
        upload_id TEXT NOT NULL UNIQUE, payment_fingerprint TEXT NOT NULL UNIQUE,
        confirmed_at TEXT NOT NULL, actor TEXT NOT NULL)''')
    for table in (PREVIEWS, CONFIRMATIONS):
        for operation in ('UPDATE', 'DELETE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'immutable fulfillment payment evidence'); END''')


def _connect(runtime):
    conn = connect_sqlite(runtime.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _source(conn, upload_id, expected_invoice_number):
    _require(re.fullmatch(r'[A-Za-z0-9_-]{1,120}', str(upload_id)), 'fulfillment_payment_invalid_upload_id')
    invoice = str(expected_invoice_number or '').strip()
    _require(invoice and len(invoice) <= 120, 'fulfillment_payment_invoice_required')
    upload = conn.execute(f'SELECT * FROM {UPLOADS} WHERE upload_id=?', (upload_id,)).fetchone()
    _require(upload and upload['validation_status'] == 'ok' and not upload['deleted_at'],
             'fulfillment_payment_upload_not_accepted')
    source = receipts._source(conn, upload_id)
    accepted = conn.execute(f'SELECT * FROM {receipts.TABLE} WHERE upload_id=? AND action=? AND source_revision=?',
                           (upload_id, 'upload', upload['file_sha256'])).fetchone()
    full_source = {**source, 'action': 'upload', 'source_revision': upload['file_sha256']}
    _require(accepted and accepted['source_digest'] == receipts.digest(full_source),
             'fulfillment_payment_source_receipt_mismatch')
    rows = conn.execute(f'SELECT * FROM {LINES} WHERE upload_id=? ORDER BY row_index,id', (upload_id,)).fetchall()
    ordinary = [r for r in rows if not r['is_storage_line']]
    _require(ordinary and all(r['match_status'] == 'ok' and r['matched_wb_supply_id'] and
             r['matched_wb_cache_key'] == 'supply:' + str(r['matched_wb_supply_id']) for r in ordinary),
             'fulfillment_payment_supply_not_exact')
    return dict(upload_id=upload_id, source_sha256=upload['file_sha256'],
                source_digest=receipts.digest(full_source), amount=_money(upload['amount_with_vat_total']),
                expected_invoice_number=invoice,
                supplies=[dict(wb_supply_id=str(r['matched_wb_supply_id']), route=r['route'],
                               boxes_qty=r['boxes_qty']) for r in ordinary])


def _validate_parser(parsed, source, data):
    _require(parsed.get('parse_status') == 'parsed' and parsed.get('posting_eligible') is True
             and parsed.get('execution_status') == 'executed' and parsed.get('execution_date')
             and parsed.get('currency') == 'RUB', 'fulfillment_payment_not_executed_or_unrecognized')
    _require(_money(parsed.get('amount')) == source['amount'], 'fulfillment_payment_amount_mismatch')
    _require(str((parsed.get('invoice_reference') or {}).get('number') or '').casefold() ==
             source['expected_invoice_number'].casefold(), 'fulfillment_payment_invoice_mismatch')
    _require(parsed.get('file_sha256') == 'sha256:' + hashlib.sha256(data).hexdigest()
             and re.fullmatch(r'sha256:[0-9a-f]{64}', str(parsed.get('payment_fingerprint'))),
             'fulfillment_payment_evidence_invalid')


def assert_not_claimed_by_fulfillment(conn, payment_fingerprint):
    """Shared reverse guard, used at overhead acceptance and posting."""
    if receipts._exists(conn, CONFIRMATIONS):
        found = conn.execute(f'SELECT operation_id FROM {CONFIRMATIONS} WHERE payment_fingerprint=?',
                             (payment_fingerprint,)).fetchone()
        _require(not found, 'payment_already_attached_to_fulfillment')


def _assert_available(conn, fingerprint, upload_id):
    if receipts._exists(conn, OVERHEAD):
        found = conn.execute(f'SELECT request_id FROM {OVERHEAD} WHERE payment_fingerprint=?', (fingerprint,)).fetchone()
        _require(not found, 'fulfillment_payment_already_used_for_overhead')
    if receipts._exists(conn, CONFIRMATIONS):
        rows = conn.execute(f'SELECT upload_id,payment_fingerprint FROM {CONFIRMATIONS} '
                            'WHERE upload_id=? OR payment_fingerprint=?', (upload_id, fingerprint)).fetchall()
        _require(all(r['upload_id'] == upload_id and r['payment_fingerprint'] == fingerprint for r in rows),
                 'fulfillment_payment_already_attached')


def _payload(row, confirmation=None):
    source, parsed = json.loads(row['source_json']), json.loads(row['parser_json'])
    return dict(operation_id=row['operation_id'], upload_id=row['upload_id'],
                preview_fingerprint=row['preview_fingerprint'],
                status='confirmed' if confirmation else 'preview',
                amount=source['amount'], supplies=source['supplies'],
                payment_date=parsed['execution_date'], payment_order_number=parsed['payment_order_number'],
                invoice_reference=parsed['invoice_reference'], payer=parsed['payer'], beneficiary=parsed['beneficiary'],
                payment_purpose=parsed['payment_purpose'], file_sha256=row['file_sha256'],
                payment_fingerprint=row['payment_fingerprint'],
                confirmed_at=confirmation['confirmed_at'] if confirmation else None,
                download_path=BASE+quote(row['upload_id'], safe='')+'/payment-proof.pdf' if confirmation else '',
                paid_projection_status='not_projected')


def _verify_saved(row):
    data = bytes(row['pdf_bytes'])
    source, parsed = json.loads(row['source_json']), json.loads(row['parser_json'])
    _require(0 < len(data) <= MAX_BYTES and data.startswith(b'%PDF-'), 'fulfillment_payment_file_invalid')
    _validate_parser(parsed, source, data)
    _require(row['file_sha256'] == parsed['file_sha256'] and row['payment_fingerprint'] == parsed['payment_fingerprint']
             and row['preview_fingerprint'] == receipts.digest([source, parsed]), 'fulfillment_payment_evidence_changed')
    return data


def preview(runtime, upload_id, pdf_bytes, *, filename, expected_invoice_number, actor=''):
    data = bytes(pdf_bytes)
    _require(0 < len(data) <= MAX_BYTES and data.startswith(b'%PDF-') and str(filename).lower().endswith('.pdf'),
             'fulfillment_payment_pdf_required')
    parsed = parse_russian_payment_order_pdf(data, filename=str(filename))
    with closing(_connect(runtime)) as conn, conn:
        ensure_schema(conn)
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        source = _source(conn, upload_id, expected_invoice_number)
        _validate_parser(parsed, source, data)
        fingerprint = parsed['payment_fingerprint']
        _assert_available(conn, fingerprint, upload_id)
        operation_id = 'ffpay_' + receipts.digest([upload_id, source['source_sha256'], fingerprint]).removeprefix('sha256:')[:32]
        existing = conn.execute(f'SELECT * FROM {PREVIEWS} WHERE operation_id=?', (operation_id,)).fetchone()
        if existing:
            _require(json.loads(existing['source_json']) == source, 'fulfillment_payment_preview_stale')
            _verify_saved(existing)
            _require(existing['file_sha256'] == parsed['file_sha256'],
                     'fulfillment_payment_original_pdf_already_retained')
        else:
            conn.execute(f'INSERT INTO {PREVIEWS} VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         (operation_id, upload_id, fingerprint, parsed['file_sha256'], receipts.digest([source, parsed]),
                          receipts.canonical(source), receipts.canonical(parsed), Path(str(filename)).name[:240],
                          data, _now(), actor))
        saved = conn.execute(f'SELECT * FROM {PREVIEWS} WHERE operation_id=?', (operation_id,)).fetchone()
        confirmation = conn.execute(f'SELECT * FROM {CONFIRMATIONS} WHERE operation_id=?', (operation_id,)).fetchone()
        result = _payload(saved, confirmation)
        conn.commit()
    return result


def confirm(runtime, upload_id, *, operation_id, preview_fingerprint, actor=''):
    with closing(_connect(runtime)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        _require(receipts._exists(conn, PREVIEWS), 'fulfillment_payment_preview_missing')
        row = conn.execute(f'SELECT * FROM {PREVIEWS} WHERE operation_id=? AND upload_id=?',
                           (operation_id, upload_id)).fetchone()
        _require(row and row['preview_fingerprint'] == preview_fingerprint, 'fulfillment_payment_preview_mismatch')
        _verify_saved(row)
        source = json.loads(row['source_json'])
        _require(_source(conn, upload_id, source['expected_invoice_number']) == source, 'fulfillment_payment_preview_stale')
        _assert_available(conn, row['payment_fingerprint'], upload_id)
        conn.execute(f'INSERT OR IGNORE INTO {CONFIRMATIONS} VALUES(?,?,?,?,?)',
                     (operation_id, upload_id, row['payment_fingerprint'], _now(), actor))
        confirmation = conn.execute(f'SELECT * FROM {CONFIRMATIONS} WHERE operation_id=?', (operation_id,)).fetchone()
        result = _payload(row, confirmation)
        conn.commit()
    # No fallible read or ValueError after commit: a domain rejection is proven
    # precommit. Transport/SQLite uncertainty still resolves with GET only.
    return result


def read(runtime, upload_id, *, operation_id=''):
    with closing(receipts.readonly(runtime.db_path)) as conn:
        if not receipts._exists(conn, PREVIEWS):
            return dict(status='not_found', upload_id=upload_id, operation_id=operation_id)
        if operation_id:
            row = conn.execute(f'SELECT * FROM {PREVIEWS} WHERE operation_id=? AND upload_id=?',
                               (operation_id, upload_id)).fetchone()
        else:
            row = conn.execute(f'SELECT p.* FROM {PREVIEWS} p JOIN {CONFIRMATIONS} c USING(operation_id) '
                               'WHERE c.upload_id=?', (upload_id,)).fetchone()
            if row is None:
                row = conn.execute(f'SELECT * FROM {PREVIEWS} WHERE upload_id=? ORDER BY created_at DESC,operation_id DESC LIMIT 1',
                                   (upload_id,)).fetchone()
        if row is None:
            return dict(status='not_found', upload_id=upload_id, operation_id=operation_id)
        _verify_saved(row)
        source = json.loads(row['source_json'])
        current = _source(conn, upload_id, source['expected_invoice_number'])
        _require(source == current, 'fulfillment_payment_source_changed')
        confirmation = conn.execute(f'SELECT * FROM {CONFIRMATIONS} WHERE operation_id=?', (row['operation_id'],)).fetchone()
        return _payload(row, confirmation)


def public_status(conn, upload_id):
    if not receipts._exists(conn, CONFIRMATIONS):
        return dict(payment_proof=None, payment_proof_status='not_attached')
    row = conn.execute(f'SELECT p.* FROM {PREVIEWS} p JOIN {CONFIRMATIONS} c USING(operation_id) WHERE c.upload_id=?',
                       (upload_id,)).fetchone()
    if row is None:
        return dict(payment_proof=None, payment_proof_status='not_attached')
    try:
        _verify_saved(row)
        source = json.loads(row['source_json'])
        _require(_source(conn, upload_id, source['expected_invoice_number']) == source, 'fulfillment_payment_source_changed')
        confirmation = conn.execute(f'SELECT * FROM {CONFIRMATIONS} WHERE operation_id=?', (row['operation_id'],)).fetchone()
        return dict(payment_proof=_payload(row, confirmation), payment_proof_status='confirmed')
    except (ValueError, KeyError, TypeError):
        return dict(payment_proof=None, payment_proof_status='unavailable')


def download(runtime, upload_id):
    with closing(receipts.readonly(runtime.db_path)) as conn:
        _require(receipts._exists(conn, CONFIRMATIONS), 'fulfillment_payment_not_attached')
        row = conn.execute(f'SELECT p.* FROM {PREVIEWS} p JOIN {CONFIRMATIONS} c USING(operation_id) WHERE c.upload_id=?',
                           (upload_id,)).fetchone()
        _require(row, 'fulfillment_payment_not_attached')
        source = json.loads(row['source_json'])
        _require(_source(conn, upload_id, source['expected_invoice_number']) == source, 'fulfillment_payment_source_changed')
        return _verify_saved(row), 'fulfillment-payment-proof-'+upload_id+'.pdf', 'application/pdf'
