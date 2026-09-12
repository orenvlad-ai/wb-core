"""Fulfillment atomic source demand, exact retries and real cost/warehouse consumer."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.sheet_vitrina_v1_fulfillment_services_smoke import _wb_supply_row, _seed_wb_supplies, _build_workbook, _valid_row, _storage_row, NOW
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _connect
from packages.application.fulfillment_services import FulfillmentServicesBlock, UPLOADS_TABLE, LINES_TABLE
from packages.application import fulfillment_recalc_intents as intents
from packages.application.our_wb_costs import OurWbCostBlock
from packages.application.wb_supplies import WbSuppliesBlock


def fixture(raw):
    rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
    _seed_wb_supplies(rt)
    return rt, FulfillmentServicesBlock(runtime=rt, timestamp_factory=lambda: NOW)


def queue(rt):
    with _connect(rt.db_path) as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {intents.QUEUE} WHERE stable_source_id LIKE 'fulfillment_upload:%' ORDER BY queue_id")]


def count(rt, table):
    with _connect(rt.db_path) as conn:
        return conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]


def consume_preparation(rt):
    worker = WbSuppliesBlock(runtime=rt)
    # Unrelated physical inventory/external flows are outside these documents.
    with patch.object(worker, '_ensure_ff_stock_wb_auto_writeoff_checkpoint', return_value={}), patch.object(worker.ff_stock_ledger, 'apply_confirmed_wb_supply_returns', return_value={}), patch.object(worker.ff_stock_ledger, 'record_wb_supply_debits', return_value={}):
        return worker.reconcile_functional_ff_state()['fulfillment_preparation']


def child(raw, action, phase):
    rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
    block = FulfillmentServicesBlock(runtime=rt, timestamp_factory=lambda: NOW)
    if phase in ('before_intent', 'after_intent'):
        original = intents.capture_request
        def stop(*args, **kw):
            if phase == 'after_intent': original(*args, **kw)
            os._exit(73)
        intents.capture_request = stop
    elif action == 'upload':
        block.get_upload = lambda *args: os._exit(73)
    else:
        block._runtime_path = lambda *args: os._exit(73)
    if action == 'upload':
        block.upload_xlsx((Path(raw)/'fixture.xlsx').read_bytes())
    else:
        block.delete_upload((Path(raw)/'upload-id').read_text())
    raise AssertionError('checkpoint not reached')


def test_process_crashes():
    for action in ('upload', 'delete'):
        for phase in ('before_intent', 'after_intent', 'after_commit'):
            with TemporaryDirectory() as raw:
                rt, block = fixture(raw)
                payload = _build_workbook([_valid_row('1001'), _storage_row()])
                (Path(raw)/'fixture.xlsx').write_bytes(payload)
                if action == 'delete':
                    upload = block.upload_xlsx(payload)
                    (Path(raw)/'upload-id').write_text(upload['upload']['upload_id'])
                before = queue(rt) if action == 'delete' else []
                result = subprocess.run([sys.executable, __file__, 'child', raw, action, phase], capture_output=True, text=True)
                assert result.returncode == 73, (action, phase, result.stderr)
                with _connect(rt.db_path) as conn:
                    rows = list(conn.execute(f'SELECT deleted_at FROM {UPLOADS_TABLE}'))
                if phase == 'after_commit':
                    assert len(rows) == 1 and bool(rows[0]['deleted_at']) == (action == 'delete')
                    assert len(queue(rt)) == len(before) + 1
                    consume_preparation(rt)
                    assert len(queue(rt)) == len(before) + 1
                else:
                    assert len(rows) == (1 if action == 'delete' else 0)
                    assert not rows or not rows[0]['deleted_at']
                    assert queue(rt) == before
    print('hard_process_checkpoints=6; primary+intent atomic before/after commit')


def test_identity_and_before_scope():
    with TemporaryDirectory() as raw:
        rt, block = fixture(raw)
        payload = _build_workbook([_valid_row('1001'), _storage_row()])
        first = block.upload_xlsx(payload)
        upload_id = first['upload']['upload_id']
        for _ in range(10):
            repeat = block.upload_xlsx(payload, uploaded_filename='renamed.xlsx')
            assert repeat['duplicate'] and repeat['upload']['upload_id'] == upload_id
        assert count(rt, UPLOADS_TABLE) == 1 and count(rt, LINES_TABLE) == 2 and len(queue(rt)) == 1
        assert len(list((Path(raw)/'fulfillment_services').rglob('*.pdf'))) == 1
        assert block.approved_overlay_by_supply()['1001']['amount_with_vat_total'] == 2100
        with _connect(rt.db_path) as conn:
            conn.execute("DELETE FROM sheet_vitrina_v1_wb_supplies WHERE supply_id='1001'")
            conn.commit()
        deleted = block.delete_upload(upload_id)
        assert deleted['operation_applied'] and len(queue(rt)) == 2
        delete_queue = deleted['warehouse_targeted_recalculation']
        assert json.loads(delete_queue['affected_nm_ids_json']) == [1] and delete_queue['effective_date'] == '2026-07-06'
        before = queue(rt)
        assert block.delete_upload(upload_id)['already_deleted']
        assert queue(rt) == before
        old = first['warehouse_targeted_recalculation']
        with _connect(rt.db_path) as conn:
            conn.execute(f"UPDATE {intents.QUEUE} SET status='complete' WHERE queue_id=? AND stable_source_id=? AND source_revision=?", (old['queue_id'], old['stable_source_id'], old['source_revision']))
            conn.commit()
        assert next(row for row in queue(rt) if row['queue_id'] == delete_queue['queue_id'])['status'] == 'queued'
        _seed_wb_supplies(rt)
        restored = block.upload_xlsx(payload)
        assert restored['upload']['upload_id'] != upload_id and not restored['duplicate']
        assert len(queue(rt)) == 3 and count(rt, UPLOADS_TABLE) == 2
        different = block.upload_xlsx(_build_workbook([_valid_row('1001')]))
        assert different['upload']['upload_id'] != restored['upload']['upload_id']
        assert block.approved_overlay_by_supply()['1001']['amount_with_vat_total'] == 3675
    print('exact_file_retry=10; duplicate primary effects=0; delete/recreate and stale ack distinct')


def test_concurrent_duplicate():
    with TemporaryDirectory() as raw:
        rt, block = fixture(raw)
        payload = _build_workbook([_valid_row('1001')])
        gate = threading.Barrier(2)
        original = block._save_upload
        def save(**kw):
            gate.wait(10)
            return original(**kw)
        block._save_upload = save
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(block.upload_xlsx, payload) for _ in range(2)]
            results = [future.result(20) for future in futures]
        assert results[0]['upload']['upload_id'] == results[1]['upload']['upload_id']
        assert count(rt, UPLOADS_TABLE) == len(queue(rt)) == 1
        assert len(list((Path(raw)/'fulfillment_services').rglob('*.pdf'))) == 1
    print('concurrent_upload_retry=one document/one queue/one referenced PDF')


def test_missing_scope_and_truthful_readback():
    with TemporaryDirectory() as raw:
        rt, block = fixture(raw)
        with _connect(rt.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json='[]' WHERE supply_id='1001'")
            conn.commit()
        upload = block.upload_xlsx(_build_workbook([_valid_row('1001')]))
        assert upload['warehouse_targeted_recalculation']['status'] == 'pending'
        assert not queue(rt) and consume_preparation(rt)['status'] == 'pending'
        deleted = block.delete_upload(upload['upload']['upload_id'])
        assert deleted['warehouse_targeted_recalculation']['status'] == 'pending'
        _seed_wb_supplies(rt)
        outcome = consume_preparation(rt)
        assert outcome['status'] == 'ok' and len(queue(rt)) == 2
        assert all(json.loads(row['affected_nm_ids_json']) == [1] for row in queue(rt))
        block.get_upload = lambda *args: (_ for _ in ()).throw(RuntimeError('readback interruption'))
        result = block.upload_xlsx(_build_workbook([_valid_row('1002')]))
        assert result['operation_applied'] and result['durable_saved'] and result['status'] == 'pending'
        assert result['upload']['validation_status'] == 'ok'
    print('missing_goods=durable_pending; existing worker resumes upload+delete; postcommit readback truthful')


def test_legacy_startup_does_not_schedule_history():
    with TemporaryDirectory() as raw:
        rt, block = fixture(raw)
        for index in range(6):
            row = _valid_row('1001'); row[1] = 'legacy-' + str(index)
            block.upload_xlsx(_build_workbook([row]))
        with _connect(rt.db_path) as conn:
            conn.execute('DELETE FROM ' + intents.TABLE)
            conn.execute("UPDATE " + intents.QUEUE + " SET status='complete'")
            conn.commit()
        before = queue(rt)
        reopened = FulfillmentServicesBlock(runtime=rt)
        assert len(reopened.list_uploads()['uploads']) == 6
        assert consume_preparation(rt) == {'status':'ok', 'requests':[]}
        assert queue(rt) == before and count(rt, intents.TABLE) == 0
        assert all(reopened.get_upload(upload['upload_id'])['warehouse_targeted_recalculation']['status'] == 'not_tracked' for upload in reopened.list_uploads()['uploads'])
    print('legacy_startup=6 documents/6 complete queues unchanged, no scheduled history')


def test_preorder_identity_and_cost_window():
    for promotion in (False, True):
        with TemporaryDirectory() as raw:
            rt, block = fixture(raw)
            row = _wb_supply_row('preorder:9001', accepted_quantity=10, quantity_added=10, cost_total=0)
            row.update(cache_key='preorder:9001', wb_supply_id='', preorder_id='9001', status_id=1,
                raw_goods=[] if promotion else [{'nmID':1,'quantity':10,'acceptedQuantity':10}])
            rt.save_wb_supply_rows(rows=[row], warehouses=[], synced_at=NOW)
            upload = block.upload_xlsx(_build_workbook([_valid_row('9001')]))
            if promotion:
                assert upload['warehouse_targeted_recalculation']['status'] == 'pending'
                rt.delete_wb_supply_records(['preorder:9001'])
                promoted = _wb_supply_row('4001', accepted_quantity=10, quantity_added=10, cost_total=0)
                promoted.update(preorder_id='9001')
                rt.save_wb_supply_rows(rows=[promoted], warehouses=[], synced_at=NOW)
                assert consume_preparation(rt)['status'] == 'ok'
                expected_id = '4001'
            else:
                expected_id = 'preorder:9001'
            assert rt.load_wb_supply_record('preorder:9001')['supply_id'] == expected_id
            costs = OurWbCostBlock(runtime=rt)
            costs.materialize_wb_supply_cost_layers()
            with _connect(rt.db_path) as conn:
                intents.require_current_cost_layers(conn)
                layer = conn.execute("SELECT ff_upload_id,ff_services_per_unit_rub FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE wb_supply_id=? AND is_current=1", (expected_id,)).fetchone()
                assert layer['ff_upload_id'] == upload['upload']['upload_id'] and layer['ff_services_per_unit_rub'] == 157.5, dict(layer)
            assert json.loads(queue(rt)[0]['affected_nm_ids_json']) == [1]
            # Another document on the actual supply sums once with the promoted
            # preorder identity, rather than overwriting the earlier overlay.
            if promotion:
                second = block.upload_xlsx(_build_workbook([_valid_row('4001')]))
                assert block.approved_overlay_by_supply()['4001']['amount_with_vat_total'] == 3150
                block.delete_upload(second['upload']['upload_id'])
                assert block.approved_overlay_by_supply()['4001']['amount_with_vat_total'] == 1575
    with TemporaryDirectory() as raw:
        rt, block = fixture(raw)
        row = _wb_supply_row('4001', accepted_quantity=10, quantity_added=10, cost_total=0)
        row.update(fact_date='2026-06-30', supply_date='2026-06-30')
        rt.save_wb_supply_rows(rows=[row], warehouses=[], synced_at=NOW)
        upload = block.upload_xlsx(_build_workbook([_valid_row('4001')]))
        assert upload['warehouse_targeted_recalculation']['terminal_no_op'] and not queue(rt)
        no_op = upload['warehouse_targeted_recalculation']
        assert no_op['reason'] == 'fulfillment_supply_outside_current_cost_window'
        assert no_op['source_revision'] == upload['upload']['file_sha256']
        assert no_op['stable_source_id'] == 'fulfillment_upload:' + upload['upload']['upload_id']
        assert no_op['durable_saved'] and no_op['warehouse_mutation_count'] == 0
        assert block.approved_overlay_by_supply()['4001']['amount_with_vat_total'] == 1575
        deleted = block.delete_upload(upload['upload']['upload_id'])
        assert deleted['warehouse_targeted_recalculation']['terminal_no_op']
        assert deleted['warehouse_targeted_recalculation']['source_revision'].startswith('deleted:')
        assert deleted['warehouse_targeted_recalculation']['source_revision'] != no_op['source_revision']
        assert consume_preparation(rt)['status'] == 'ok'
        OurWbCostBlock(runtime=rt).materialize_wb_supply_cost_layers()
        with _connect(rt.db_path) as conn:
            intents.require_current_cost_layers(conn)
            assert conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE wb_supply_id='4001'").fetchone()[0] == 0
        assert not queue(rt)
    print('identity=preorder+promotion resolved; mixed aliases sum once; preopening=explicit no-op, no historical layers')


def test_actual_cost_and_capture_races():
    from apps.warehouse_targeted_replay_smoke import _seed_functional
    from packages.application.warehouse_functional import WarehouseFunctionalBlock
    with TemporaryDirectory() as raw:
        rt, block = fixture(raw)
        costs = OurWbCostBlock(runtime=rt, timestamp_factory=lambda: NOW)
        # Existing actual warehouse capture and apply, no mocked queue acknowledger.
        _seed_functional(rt)
        from packages.application.canonical_cost_engine import CanonicalCostEngine
        CanonicalCostEngine(runtime=rt)
        with _connect(rt.db_path) as conn:
            conn.execute('DELETE FROM sheet_vitrina_v1_warehouse_functional_balances')
            conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id<>'base'")
            conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id<>'base'")
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES('base','ff',1,'30','10','300','30','opening',0,'0','0','0','{}')")
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_versions SET version_kind='functional_cutover',effective_at=?,business_effective_date='2026-07-01' WHERE version_id='base'", (NOW,))
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_cutovers SET cutover_at='2026-07-01T00:00:00Z'")
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_snapshots SET snapshot_date='2026-07-06',fetched_at='2026-07-06T08:00:00Z',requested_nm_ids_json='[1]',raw_rows_json='[]',items_json='[{\"nm_id\":1,\"quantity\":\"30\"}]',raw_row_count=0")
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_opening_cost_map SELECT cutover_id,1,'10','10','direct_24_06','{}','fixture',? FROM sheet_vitrina_v1_warehouse_functional_cutovers", (NOW,))
            conn.execute("INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions VALUES('fixture',1,'2026-07-01','fixture','2026-07-01','0',0,'0',0,0,'fixture','{}',1,?,NULL)", (NOW,))
            conn.commit()
        # Physical source fixture: the cutover had 30 units at FF with WAC10.
        # Preserve the original ledger opening and exact supply-linked debits.
        for operation_id, source_type, supply_id, delta, stamp in (
            ('opening', 'manual_excel', '', 30, '2026-07-01T00:00:00Z'),
            ('debit-1001', 'wb_supply', '1001', -10, '2026-07-05T00:00:00Z'),
            ('debit-1002', 'wb_supply', '1002', -20, '2026-07-05T01:00:00Z'),
        ):
            rt.create_ff_stock_operation(operation_id=operation_id, operation_type='manual_receipt' if delta > 0 else 'auto_writeoff',
                source_type=source_type, source_key=operation_id, source_object_id=supply_id,
                created_at=stamp, lines=[{'nm_id':1, 'quantity_delta':delta}])
        def primary_snapshot():
            with _connect(rt.db_path) as conn:
                return {table: sorted(json.dumps(dict(row), sort_keys=True) for row in conn.execute('SELECT * FROM ' + table)) for table in (
                    'sheet_vitrina_v1_ff_stock_operations', 'sheet_vitrina_v1_ff_stock_operation_lines',
                    'sheet_vitrina_v1_cny_documents', 'sheet_vitrina_v1_supplier_financial_documents',
                    'sheet_vitrina_v1_wb_supplies')}
        primary_before = primary_snapshot()
        functional = WarehouseFunctionalBlock(runtime=rt, timestamp_factory=lambda: NOW)
        costs.materialize_wb_supply_cost_layers()
        # Real cost materialization happened before a concurrent source commit.
        upload = block.upload_xlsx(_build_workbook([_valid_row('1001'), _storage_row()]))
        with _connect(rt.db_path) as conn:
            try: intents.require_current_cost_layers(conn)
            except ValueError as exc: assert 'materialization_pending' in str(exc)
            else: raise AssertionError('stale cost layer acknowledged')
        assert costs.materialize_wb_supply_cost_layers(supply_ids=['1001']) == 1
        with _connect(rt.db_path) as conn:
            intents.require_current_cost_layers(conn)
            row = conn.execute("SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE wb_supply_id='1001' AND is_current=1").fetchone()
            assert row['ff_services_per_unit_rub'] == 157.5 and row['ff_storage_per_unit_rub'] == 52.5
            assert row['ff_upload_id'] == upload['upload']['upload_id']
        pending = queue(rt)
        plan = functional.build_targeted_recovery_plan(affected_nm_ids=[1], stable_source_ids=[pending[0]['stable_source_id']], targeted_recalc_requests=pending)
        published = functional.apply_plan(plan, confirm_fingerprint=plan['plan_fingerprint'])
        assert published['status'] == 'ready', published
        balance = next(row for row in published['balances'] if row['warehouse_key'] == 'wb' and row['nm_id'] == 1)
        assert (balance['quantity'], balance['wac_rub'], balance['capital_rub']) == ('30', '100', '3000'), balance
        print('actual_upload_balance=' + json.dumps({key:balance[key] for key in ('version_id','quantity','wac_rub','capital_rub')}))
        assert all(row['status'] == 'complete' for row in queue(rt))
        # Deletion races after cost materialization; retain its own queue until refreshed.
        block.delete_upload(upload['upload']['upload_id'])
        with _connect(rt.db_path) as conn:
            try: intents.require_current_cost_layers(conn)
            except ValueError: pass
            else: raise AssertionError('delete acknowledged against previous services')
        try:
            functional.build_targeted_recovery_plan(affected_nm_ids=[1], stable_source_ids=[pending[0]['stable_source_id']], targeted_recalc_requests=queue(rt))
        except ValueError as exc:
            assert 'materialization_pending' in str(exc)
        else:
            raise AssertionError('actual warehouse capture bypassed causal guard')
        costs.materialize_wb_supply_cost_layers(supply_ids=['1001'])
        pending = [row for row in queue(rt) if row['status'] == 'queued']
        plan = functional.build_targeted_recovery_plan(affected_nm_ids=[1], stable_source_ids=[pending[0]['stable_source_id']], targeted_recalc_requests=pending)
        published = functional.apply_plan(plan, confirm_fingerprint=plan['plan_fingerprint'])
        assert published['status'] == 'ready' and all(row['status'] == 'complete' for row in queue(rt))
        balance = next(row for row in published['balances'] if row['warehouse_key'] == 'wb' and row['nm_id'] == 1)
        assert (balance['quantity'], balance['wac_rub'], balance['capital_rub']) == ('30', '30', '900'), balance
        print('actual_delete_balance=' + json.dumps({key:balance[key] for key in ('version_id','quantity','wac_rub','capital_rub')}))
        with _connect(rt.db_path) as conn:
            row = conn.execute("SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE wb_supply_id='1001' AND is_current=1").fetchone()
            assert row['ff_upload_id'] is None and row['ff_services_per_unit_rub'] == row['ff_storage_per_unit_rub'] == 0
        # A new source between capture and actual warehouse commit cannot be
        # acknowledged by the old plan. The new revision has its own demand.
        second = block.upload_xlsx(_build_workbook([_valid_row('1001')]))
        costs.materialize_wb_supply_cost_layers(supply_ids=['1001'])
        pending = [row for row in queue(rt) if row['status'] == 'queued']
        stale = functional.build_targeted_recovery_plan(affected_nm_ids=[1], stable_source_ids=[pending[0]['stable_source_id']], targeted_recalc_requests=pending)
        block.delete_upload(second['upload']['upload_id'])
        try:
            functional.apply_plan(stale, confirm_fingerprint=stale['plan_fingerprint'])
        except ValueError:
            pass
        else:
            raise AssertionError('stale source capture published')
        assert len([row for row in queue(rt) if row['status'] == 'queued']) == 2
        assert count(rt, 'sheet_vitrina_v1_cny_documents') == 0
        assert count(rt, 'sheet_vitrina_v1_supplier_financial_documents') == 0
        assert primary_snapshot() == primary_before

    print('actual_consumer=upload 157.5 services+52.5 storage per unit -> delete 0/0; two ready warehouse commits/exact acknowledgements')


def main():
    test_process_crashes()
    test_identity_and_before_scope()
    test_concurrent_duplicate()
    test_missing_scope_and_truthful_readback()
    test_legacy_startup_does_not_schedule_history()
    test_preorder_identity_and_cost_window()
    test_actual_cost_and_capture_races()
    print('fulfillment_recalc_intents_smoke: OK')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'child': child(*sys.argv[2:])
    else: main()
