"""Executed proof is an immutable attachment, never another expense."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.fulfillment_recalc_intents_smoke import fixture
from apps.ff_pool_documents_smoke import identity
from apps.sheet_vitrina_v1_fulfillment_services_smoke import _build_workbook, _valid_row
from packages.application import fulfillment_payment_proof as proof
from packages.application import operator_fulfillment_services as receipts
from packages.application import ff_pool_documents as overhead
from packages.application.russian_payment_orders import parse_russian_payment_order_text

PDF = b'%PDF-1.4\nsynthetic bank text extraction fixture\n%%EOF'
TEXT = (ROOT/'apps/fixtures/russian_payment_orders/wb_bank_0401060.txt').read_text()


def parse(data, **kwargs):
    return parse_russian_payment_order_text(TEXT, file_sha256='sha256:'+hashlib.sha256(data).hexdigest())


def fails(fn, code):
    try:
        fn()
    except (ValueError, sqlite3.IntegrityError) as exc:
        assert code in str(exc) or code in str(getattr(exc, 'code', '')), str(exc)
    else:
        raise AssertionError('expected failure: '+code)


def upload(block, supply='1001'):
    row = _valid_row(supply)
    # Minimal exact invoice total including VAT; no spurious service decomposition.
    row[2:8] = [None]*6
    row[8:10] = [11757.78, 587.89]
    result = block.upload_xlsx(_build_workbook([row]))
    assert result['validation_status'] == 'ok', result
    return result['upload']['upload_id']


def preview(rt, uid, data=PDF):
    return proof.preview(rt, uid, data, filename='bank.pdf', expected_invoice_number='SYN-101', actor='previewer')


def confirm(rt, uid, item):
    return proof.confirm(rt, uid, operation_id=item['operation_id'], preview_fingerprint=item['preview_fingerprint'], actor='confirmer')


def business_rows(rt):
    with closing(receipts.readonly(rt.db_path)) as conn:
        tables = [proof.UPLOADS, proof.LINES, receipts.TABLE,
                  'sheet_vitrina_v1_fulfillment_recalc_intents',
                  'sheet_vitrina_v1_warehouse_recalculation_queue']
        return {table: [tuple(row) for row in conn.execute('SELECT * FROM '+table)]
                for table in tables if receipts._exists(conn, table)}


def accept_overhead(rt):
    service = overhead.FfPoolDocumentService(db_path=rt.db_path, runtime_dir=rt.runtime_dir, resume=False)
    return service._accept(identity=identity('proof-duplicate'), document_kind='pool_overhead',
        manifest={'category':'other','comment':'test','source_mode':'payment_order_pdf',
                  'amount_rub':'12345.67','payment_evidence':parse(PDF)},
        source_bytes=PDF,source_filename='bank.pdf',source_content_type='application/pdf',template_fingerprint='')


def run():
    with TemporaryDirectory() as raw, patch.object(proof, 'parse_russian_payment_order_pdf', side_effect=parse):
        rt, block = fixture(raw)
        uid = upload(block)
        baseline = business_rows(rt)
        p = preview(rt, uid)
        assert p['status'] == 'preview' and p['amount'] == '12345.67'
        assert p['supplies'][0]['wb_supply_id'] == '1001' and p['payment_date'] == '2026-08-19'
        assert proof.read(rt, uid)['operation_id'] == p['operation_id']
        fails(lambda: proof.download(rt, uid), 'not_attached')
        fails(lambda: proof.confirm(rt, uid, operation_id=p['operation_id'], preview_fingerprint='wrong'), 'preview_mismatch')
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: confirm(rt, uid, p), range(2)))
        assert results[0] == results[1] and results[0]['status'] == 'confirmed'
        assert proof.download(rt, uid)[0] == PDF
        assert preview(rt, uid)['operation_id'] == p['operation_id']
        fails(lambda: preview(rt, uid, PDF+b'\nchanged layout'), 'original_pdf_already_retained')
        assert proof.download(rt, uid)[0] == PDF  # first exact file remains authoritative
        assert business_rows(rt) == baseline
        assert block.get_upload(uid)['upload']['payment_proof_status'] == 'confirmed'
        assert block.list_uploads()['uploads'][0]['payment_proof']['operation_id'] == p['operation_id']
        # A committed confirmation response has no postcommit fallible read.
        with patch.object(proof, 'read', side_effect=OSError('read unavailable')):
            assert confirm(rt, uid, p) == results[0]
        assert proof.read(rt, uid, operation_id=p['operation_id']) == results[0]
        with closing(proof._connect(rt)) as conn, conn:
            assert conn.execute(f'SELECT count(*) FROM {proof.CONFIRMATIONS}').fetchone()[0] == 1
            assert conn.execute(f'SELECT actor FROM {proof.CONFIRMATIONS}').fetchone()[0] == 'confirmer'
            fails(lambda: conn.execute(f'DELETE FROM {proof.CONFIRMATIONS}'), 'immutable')
            fails(lambda: conn.execute(f'UPDATE {proof.PREVIEWS} SET filename="changed"'), 'immutable')
            fails(lambda: proof.assert_not_claimed_by_fulfillment(conn, p['payment_fingerprint']), 'already_attached')
        other = upload(block, '1002')
        fails(lambda: preview(rt, other), 'already_attached')
        # Both entry points of the existing overhead owner reject a claimed payment.
        fails(lambda: accept_overhead(rt), 'already_attached')
        with closing(proof._connect(rt)) as conn:
            fails(lambda: overhead._overhead_metadata(conn, request={'source_sha256':p['file_sha256']}, manifest={
                'category':'other','comment':'test','source_mode':'payment_order_pdf',
                'payment_evidence':parse(PDF)}, amount_cents=1234567), 'already_attached')
        block.delete_upload(uid)
        fails(lambda: confirm(rt, uid, p), 'upload_not_accepted')
        fails(lambda: proof.download(rt, uid), 'upload_not_accepted')
    print('proof attachment: exact source, immutable PDF, semantic replay/concurrency, no expense/cost/quantity writes')

    with TemporaryDirectory() as raw, patch.object(proof, 'parse_russian_payment_order_pdf', side_effect=parse):
        rt, block = fixture(raw)
        uid = upload(block)
        fails(lambda: proof.preview(rt, uid, PDF, filename='invoice.pdf', expected_invoice_number='WRONG'), 'invoice_mismatch')
        normal = block.upload_xlsx(_build_workbook([_valid_row('1002')]))['upload']['upload_id']
        fails(lambda: preview(rt, normal), 'amount_mismatch')
        for patch_data in ({'execution_status':'not_executed'}, {'execution_status':'unclear'},
                           {'posting_eligible':False}, {'currency':'USD'}, {'parse_status':'needs_review'}):
            with patch.object(proof, 'parse_russian_payment_order_pdf', return_value={**parse(PDF), **patch_data}):
                fails(lambda: preview(rt, uid), 'not_executed_or_unrecognized')
        fails(lambda: preview(rt, uid, b'not a PDF'), 'pdf_required')
        p = preview(rt, uid)
        with closing(proof._connect(rt)) as conn, conn:
            conn.execute(f'UPDATE {proof.LINES} SET route=? WHERE upload_id=?', ('changed',uid))
        fails(lambda: confirm(rt, uid, p), 'source_receipt_mismatch')
    print('fail-closed: wrong invoice/amount/currency, unexecuted/unclear/unrecognized, source drift, invalid file')

    with TemporaryDirectory() as raw, patch.object(proof, 'parse_russian_payment_order_pdf', side_effect=parse):
        rt, block = fixture(raw)
        uid = upload(block)
        p = preview(rt, uid)
        # Existing overhead immutable reservations also block the interval
        # between FF proof preview and confirmation, before any extra expense.
        accept_overhead(rt)
        fails(lambda: confirm(rt, uid, p), 'already_used_for_overhead')
        fails(lambda: preview(rt, uid), 'already_used_for_overhead')
        with closing(receipts.readonly(rt.db_path)) as conn:
            assert conn.execute(f'SELECT count(*) FROM {proof.CONFIRMATIONS}').fetchone()[0] == 0
    print('overhead duplicate reservation blocks preview and confirm; zero second expense')


if __name__ == '__main__':
    run()
