"""Disposable native source acceptance, identities and strict read-only projection."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.fulfillment_recalc_intents_smoke import fixture, queue
from apps.sheet_vitrina_v1_fulfillment_services_smoke import _build_workbook, _valid_row, _storage_row, _wb_supply_row, NOW
from packages.application import operator_fulfillment_services as receipt
from packages.application.registry_upload_db_backed_runtime import _connect, RegistryUploadDbBackedRuntime
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock


def expect_failure(function, reason):
    try:
        function()
    except (ValueError, RuntimeError) as exc:
        assert reason in str(exc), str(exc)
    else:
        raise AssertionError('expected rejection: ' + reason)


def test_sources_and_readonly():
    with TemporaryDirectory() as raw:
        empty = Path(raw) / 'empty.sqlite3'
        sqlite3.connect(empty).close()
        before = empty.read_bytes()
        assert receipt.journal(empty)['items'] == [] and receipt.read_acceptance(empty, 'missing') is None
        with receipt.readonly(empty) as conn:
            assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
            try: conn.execute('CREATE TABLE forbidden(x)')
            except sqlite3.OperationalError: pass
            else: raise AssertionError('reader was writable')
        assert empty.read_bytes() == before
        rt, block = fixture(raw)
        data = _build_workbook([_valid_row('1001'), _storage_row()])
        result = block.upload_xlsx(data)
        accepted = result['acceptance']
        assert accepted['durable_saved'] and accepted['state'] == 'accepted'
        assert accepted['source_ref']['source_sha256'] == result['upload']['file_sha256']
        assert accepted['publication']['status'] == 'pending'
        with heavy_admitted(rt.runtime_dir, operation='fixture-busy'):
            from packages.application.fulfillment_recalc_intents import drain_fulfillment_recalc_intents
            # A different thread has no right to reuse this live owner's lease.
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(drain_fulfillment_recalc_intents, rt).result()['status'] == 'pending'
        reopened = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
        assert receipt.read_acceptance(reopened.db_path, accepted['operation_id'])['source_ref'] == accepted['source_ref']
        repeated = block.upload_xlsx(data, uploaded_filename='renamed.xlsx')
        assert repeated['duplicate'] and repeated['acceptance']['operation_id'] == accepted['operation_id']
        overlay = block.approved_overlay_by_supply()['1001']
        assert overlay['amount_with_vat_total'] == 2100
        assert overlay['service_amount_with_vat_without_storage_total'] == 1575
        assert overlay['storage_allocated_amount_with_vat_total'] == 525
        other_row = _valid_row('1001'); other_row[1] = 'Другая услуга'
        other = block.upload_xlsx(_build_workbook([other_row]))
        assert block.approved_overlay_by_supply()['1001']['amount_with_vat_total'] == 3675
        deleted = block.delete_upload(result['upload']['upload_id'], deleted_by='fixture-actor')
        assert deleted['acceptance']['source_ref']['action'] == 'delete'
        assert deleted['acceptance']['operation_id'] != accepted['operation_id']
        assert deleted['acceptance']['actor'] == 'fixture-actor'
        assert block.approved_overlay_by_supply()['1001']['amount_with_vat_total'] == 1575
        assert block.approved_overlay_by_supply()['1001']['upload_ids'] == [other['upload']['upload_id']]
        assert receipt.read_acceptance(rt.db_path, accepted['operation_id'])['native_state'] == 'superseded'
        assert block.delete_upload(result['upload']['upload_id'])['acceptance']['operation_id'] == deleted['acceptance']['operation_id']
        new = block.upload_xlsx(data)
        assert new['upload']['upload_id'] != result['upload']['upload_id']
        assert new['acceptance']['operation_id'] != accepted['operation_id']
        assert len(receipt.journal(rt.db_path)['items']) == 4
        with _connect(rt.db_path) as conn:
            for sql in ('UPDATE ' + receipt.TABLE + " SET actor='other'", 'DELETE FROM ' + receipt.TABLE):
                try: conn.execute(sql)
                except sqlite3.IntegrityError: pass
                else: raise AssertionError('receipt was mutable')
        invalid = block.upload_xlsx(_build_workbook([_valid_row('missing')]))
        assert invalid['validation_status'] == 'failed' and not invalid['durable_saved'] and invalid['acceptance'] is None
        with patch.object(block, 'get_upload', side_effect=RuntimeError('lost read')):
            with patch.object(receipt, 'read_source_acceptance', side_effect=sqlite3.OperationalError('read unavailable')):
                lost = block.upload_xlsx(data)
                assert lost['durable_saved'] and lost['acceptance_readback_pending'] and lost['acceptance'] is None
    print('source receipts: atomic final upload/delete; restart/busy/duplicate/reupload; conservation; immutable; readonly GET')


def test_not_tracked_noop_and_drift():
    with TemporaryDirectory() as raw:
        rt, block = fixture(raw)
        row = _wb_supply_row('4001', accepted_quantity=10, quantity_added=10, cost_total=0)
        row.update(fact_date='2026-06-30', supply_date='2026-06-30')
        rt.save_wb_supply_rows(rows=[row], warehouses=[], synced_at=NOW)
        result = block.upload_xlsx(_build_workbook([_valid_row('4001')]))
        accepted = result['acceptance']
        assert accepted['native_state'] == 'no_op' and accepted['publication']['terminal_no_op']
        assert accepted['state'] != 'completed'
        with heavy_admitted(rt.runtime_dir,operation='no-applicable-derived-work'), warehouse_functional_job_lock(rt.runtime_dir):
            source_complete = receipt.record_completion(rt,accepted['operation_id'])
        assert source_complete['source_complete'] and source_complete['processing_not_applicable']
        assert source_complete['state']=='accepted' and source_complete['publication']['warehouse_mutation_count']==0
        with _connect(rt.db_path) as conn:
            conn.execute('DELETE FROM sheet_vitrina_v1_fulfillment_recalc_intents')
            conn.commit()
        assert receipt.read_acceptance(rt.db_path, accepted['operation_id'])['native_state'] == 'not_tracked'
        with _connect(rt.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_fulfillment_service_lines SET amount_with_vat=999 WHERE upload_id=?", (result['upload']['upload_id'],))
            conn.commit()
        assert receipt.read_acceptance(rt.db_path, accepted['operation_id'])['reason_code'] == 'source_changed'
    with TemporaryDirectory() as raw:
        rt, block = fixture(raw)
        data = _build_workbook([_valid_row('1001')])
        result = block.upload_xlsx(data)
        with _connect(rt.db_path) as conn:
            conn.execute('DROP TABLE ' + receipt.COMPLETIONS)
            conn.execute('DROP TABLE ' + receipt.TABLE)
            conn.commit()
        assert block.get_upload(result['upload']['upload_id'])['acceptance'] is None
        assert block.upload_xlsx(data)['acceptance'] is None
        with receipt.readonly(rt.db_path) as conn:
            assert not receipt._exists(conn, receipt.TABLE)
    print('no-op/not-tracked/source drift remain truthful; no legacy receipt scan/backfill')


if __name__ == '__main__':
    test_sources_and_readonly()
    test_not_tracked_noop_and_drift()
    print('operator_fulfillment_services_smoke: OK')
