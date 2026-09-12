"""CNY source atomicity, account dependencies, restart and real queue consumption."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _connect
from packages.application.cny_ledger import CnyLedgerBlock
from packages.application import cny_preparation_intents as intents
from packages.application.wb_supplies import WbSuppliesBlock
from apps.cny_ledger_smoke import _save_payment, _fixture_text_extractor
from apps.supplier_preparation_intents_smoke import source, HEADER, LINES

NOW = '2026-07-01T10:00:00Z'
QUEUE = 'sheet_vitrina_v1_warehouse_targeted_recalc_queue'


def fixture(raw: str, *, payments: bool = True):
    rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
    for shipment_id, nm_id in [('a', 101), ('b', 202), ('c', 303), ('unrelated', 404)]:
        source(rt, {**HEADER, 'shipment_id': shipment_id, 'invoice_amount_total': 100, 'invoice_date': '2026-07-01', 'shipment_date': '2026-07-01', 'actual_shipment_date': '2026-07-01', 'order_status': 'in_transit'}, [{**LINES[0], 'line_id': shipment_id+'-line', 'internal_nm_id': nm_id}])
    ledger = CnyLedgerBlock(runtime=rt, timestamp_factory=lambda: NOW, pdf_text_extractor=_fixture_text_extractor)
    ledger.create_opening_balance({'operation_date': '2026-07-01', 'cny_amount': 300, 'rub_value': 3000})
    if payments:
        _save_payment(rt, 'pay-a', 'a', '2026-07-02T10:00:00Z', '100')
        _save_payment(rt, 'pay-b', 'b', '2026-07-03T10:00:00Z', '100')
        result = ledger.replay_ledger(reason='fixture')
        assert result['status'] == 'ok', result
    return rt, ledger


def queue(rt):
    with _connect(rt.db_path) as conn:
        return [dict(row) for row in conn.execute(f'SELECT * FROM {QUEUE} WHERE stable_source_id=? ORDER BY requested_at,queue_id', (intents.SOURCE_ID,))]


def perform(rt, ledger, action):
    if action == 'opening':
        return ledger.create_opening_balance({'operation_date': '2026-07-01', 'cny_amount': 300, 'rub_value': 3600})
    if action == 'upload':
        return ledger.upload_document(file_bytes=b'conversion-fixture', uploaded_filename='conversion.pdf')
    if action == 'fee':
        return ledger.save_bank_fee_document(source_order_id='a', linked_financial_document_id='fixture-statement', natural_key='fixture-fee', fee_row={'operation_date':'2026-07-02','operation_datetime':'2026-07-02T11:00:00Z','amount':'1'}, replay=False)
    if action == 'relink':
        return ledger.relink_document('pay-a', target_shipment_id='c')
    if action == 'archive':
        return ledger.delete_document('pay-a')
    if action == 'restore':
        return ledger.restore_document('pay-a', target_shipment_id='c')
    if action == 'direct':
        document = rt.load_cny_document('pay-a')
        return rt.save_cny_document({**document, 'cny_amount':'50', 'parsed_payload':{**document['parsed_payload'], 'cny_amount':'50', 'transfer_amount':'50'}})
    raise AssertionError(action)


def child(raw, action, phase):
    rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
    ledger = CnyLedgerBlock(runtime=rt, timestamp_factory=lambda: NOW, pdf_text_extractor=_fixture_text_extractor)
    if phase == 'before':
        original = intents.finish_source_change
        def stop(conn, before, **kw):
            original(conn, before, **kw)
            os._exit(71)
        intents.finish_source_change = stop
    else:
        ledger.replay_ledger = lambda **kw: os._exit(72)
    perform(rt, ledger, action)
    os._exit(72)


def test_process_source_boundaries():
    counts = {'before': 0, 'after': 0}
    for action in ('opening', 'upload', 'fee', 'relink', 'archive', 'restore', 'direct'):
        for phase in ('before', 'after'):
            with TemporaryDirectory() as raw:
                rt, ledger = fixture(raw)
                if action == 'restore':
                    ledger.delete_document('pay-a')
                before_documents = rt.list_cny_documents()
                before = intents.read_account_request(rt)
                result = subprocess.run([sys.executable, __file__, 'child', raw, action, phase], capture_output=True, text=True)
                assert result.returncode == (71 if phase == 'before' else 72), (action, phase, result.stderr)
                current = intents.read_account_request(rt)
                if phase == 'before':
                    assert rt.list_cny_documents() == before_documents
                    assert current == before
                else:
                    assert current['revision'] == before['revision'] + 1, (action, current)
                    assert current['status'] == 'pending'
                    with _connect(rt.db_path) as conn:
                        assert json.loads(current['source_versions_json']) == intents._versions(intents.capture_source(conn))
                    resumed = intents.drain_cny_preparation_intents(rt)
                    assert resumed['status'] == 'ok', (action, resumed)
                    delivered = intents.read_account_request(rt)
                    assert delivered['status'] == 'delivered'
                    before_retry = rt.list_cny_documents()
                    assert intents.drain_cny_preparation_intents(rt)['status'] == 'ok'
                    assert rt.list_cny_documents() == before_retry
                    if action in ('relink', 'restore'):
                        assert set(json.loads(delivered['affected_shipment_ids_json'])) == {'a', 'b', 'c'}
                counts[phase] += 1
    print('hard_process_exits=' + json.dumps(counts))


def test_account_scope_revisions_and_duplicates():
    with TemporaryDirectory() as raw:
        rt, ledger = fixture(raw)
        source_before = rt.list_cny_documents()
        before = intents.read_account_request(rt)
        with patch.object(CnyLedgerBlock, '_replay_ledger', side_effect=AssertionError('duplicate replay')):
            assert ledger.reconcile_document('pay-a', reason='duplicate')['idempotent']
            assert intents.read_account_request(rt)['revision'] == before['revision']
        perform(rt, ledger, 'opening')
        newer = intents.read_account_request(rt)
        assert newer['revision'] > before['revision']
        assert json.loads(newer['affected_shipment_ids_json']) == ['a', 'b']
        assert json.loads(newer['affected_nm_ids_json']) == [101, 202]
        assert newer['status'] == 'delivered', newer
        with _connect(rt.db_path) as conn:
            rows = conn.execute('SELECT shipment_id,paid_rub FROM sheet_vitrina_v1_own_capital_payment_layers ORDER BY shipment_id').fetchall()
        assert [tuple(row) for row in rows] == [('a', '1200'), ('b', '1200')]
        ledger.create_opening_balance({'operation_date':'2026-07-01','cny_amount':300,'rub_value':3000})
        last = intents.read_account_request(rt)
        assert last['revision'] > newer['revision'] and last['source_fingerprint'] == before['source_fingerprint']
        assert len(rt.list_cny_documents()) == len(source_before)
        doc = rt.load_cny_document('pay-a')
        rt.save_cny_document({**doc, 'parsed_payload':{**doc['parsed_payload'], 'payment_date_provenance':{'source':'corrected_document','basis':'bank_execution_date'}}})
        assert intents.drain_cny_preparation_intents(rt)['status'] == 'ok'
        with _connect(rt.db_path) as conn:
            assert json.loads(conn.execute("SELECT provenance_json FROM sheet_vitrina_v1_own_capital_payment_layers WHERE payment_id='pay-a'").fetchone()[0])['payment_date_provenance']['source'] == 'corrected_document'
        assert len({row['source_revision'] for row in queue(rt)}) == len(queue(rt))
        # Delivered account work is still pending until its warehouse ack.
        ledger.relink_document('pay-a', target_shipment_id='c')
        rt.update_cny_document_context(document_id='pay-b', source_order_id='b', context_order_id='later-context', updated_at=NOW)
        pending = intents.read_account_request(rt)
        assert json.loads(pending['affected_shipment_ids_json']) == ['a','b','c']
        assert json.loads(pending['affected_nm_ids_json']) == [101,202,303]


def test_new_revision_and_consumer_race():
    with TemporaryDirectory() as raw:
        rt, ledger = fixture(raw)
        doc = rt.load_cny_document('pay-a')
        rt.update_cny_document_context(document_id='pay-a', source_order_id='c', context_order_id='c', updated_at=NOW)
        old = intents.read_account_request(rt)
        reached, changed = threading.Event(), threading.Event()
        def mutate():
            assert reached.wait(20)
            rt.save_cny_document(doc)
            changed.set()
        writer = threading.Thread(target=mutate); writer.start()
        def barrier(phase, request):
            if phase == 'after_preparation':
                reached.set(); assert changed.wait(20)
        outcome = intents.drain_cny_preparation_intents(rt, inject_failure=barrier)
        writer.join(20); assert not writer.is_alive()
        latest = intents.read_account_request(rt)
        assert latest['revision'] > old['revision'] and latest['status'] == 'pending', latest
        assert outcome['status'] == 'pending'
        assert json.loads(latest['affected_shipment_ids_json']) == ['a', 'b', 'c']
        assert not any(row['source_revision'].startswith('cny-preparation:'+str(old['revision'])+':') for row in queue(rt))
        assert intents.drain_cny_preparation_intents(rt)['status'] == 'ok'
        assert rt.load_cny_document('pay-a')['source_order_id'] == 'a'
        # Two simultaneous consumers both observe pending, only one prepares.
        rt.update_cny_document_context(document_id='pay-a', source_order_id='c', context_order_id='c', updated_at=NOW)
        gate = threading.Barrier(2); original_read = intents.read_account_request
        local = threading.local(); calls = []
        def read(runtime):
            row = original_read(runtime)
            if not getattr(local, 'seen', False):
                local.seen = True; gate.wait(timeout=20)
            return row
        original = CnyLedgerBlock._replay_ledger
        def replay(self, **kwargs):
            calls.append(1); return original(self, **kwargs)
        errors = []
        def consume():
            try:
                assert intents.drain_cny_preparation_intents(rt)['status'] == 'ok'
            except Exception as exc: errors.append(repr(exc))
        with patch.object(intents, 'read_account_request', read), patch.object(CnyLedgerBlock, '_replay_ledger', replay):
            threads = [threading.Thread(target=consume) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(30)
        assert not errors and len(calls) == 1, (errors, calls)


def test_source_and_late_failures():
    with TemporaryDirectory() as raw:
        rt, ledger = fixture(raw)
        source_before = rt.list_cny_documents()
        with patch.object(intents, 'finish_source_change', side_effect=RuntimeError('intent unavailable')):
            try: perform(rt, ledger, 'opening')
            except RuntimeError: pass
            else: raise AssertionError('source committed without intent')
        assert rt.list_cny_documents() == source_before
        with patch.object(CnyLedgerBlock, '_replay_ledger', side_effect=RuntimeError('late ledger failure')):
            outcome = perform(rt, ledger, 'opening')
        assert outcome['status'] == 'pending' and outcome['operation_applied'] and not outcome['readback_confirmed'], outcome
        docs = rt.list_cny_documents()
        assert intents.read_account_request(rt)['status'] == 'error'
        result = intents.drain_cny_preparation_intents(rt)
        assert result['status'] == 'ok', result
        assert rt.list_cny_documents() == docs
        # Retry a failed enqueue without repeating the prepared ledger work.
        rt.update_cny_document_context(document_id='pay-a', source_order_id='c', context_order_id='c', updated_at=NOW)
        def fail(phase, request):
            if phase == 'before_ack': raise RuntimeError('enqueue rollback')
        result = intents.drain_cny_preparation_intents(rt, inject_failure=fail)
        assert result['status'] == 'pending'
        assert intents.read_account_request(rt)['prepared_at']
        with patch.object(CnyLedgerBlock, '_replay_ledger', side_effect=AssertionError('prepared twice')):
            assert intents.drain_cny_preparation_intents(rt)['status'] == 'ok'


def test_noop_and_missing_link():
    with TemporaryDirectory() as raw:
        rt, ledger = fixture(raw, payments=False)
        no_op = ledger.replay_account()
        assert no_op['warehouse_targeted_recalculation']['terminal_no_op']
        assert not queue(rt)
        _save_payment(rt, 'missing', '', '2026-07-02T10:00:00Z', '10')
        result = ledger.replay_account()
        assert result['status'] == 'pending' and not result['retryable'], result
        assert result['warehouse_targeted_recalculation']['diagnostic_code'] == 'cny_replay_shipment_scope_missing'
        assert not queue(rt)
        _save_payment(rt, 'missing', 'absent', '2026-07-02T10:00:00Z', '10')
        result = ledger.replay_account()
        assert result['status'] == 'pending' and not result['retryable'], result
        assert not queue(rt)


def change_opening_source(rt, rub_value):
    document = next(row for row in rt.list_cny_documents() if row['document_type'] == 'opening_balance')
    return rt.save_cny_document({**document, 'rub_amount': str(rub_value),
        'parsed_payload': {**document['parsed_payload'], 'rub_value': str(rub_value)}})


def consumer_child(raw, phase):
    rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
    ledger = CnyLedgerBlock(runtime=rt)
    if phase == 'before_ledger_replace':
        rt.replace_cny_ledger_operations = lambda *args, **kw: os._exit(73)
    elif phase == 'after_ledger_replace':
        rt.update_supplier_shipments_cny_calculations = lambda *args, **kw: os._exit(73)
    def stop(point, request):
        if point == phase: os._exit(73)
    intents.drain_cny_preparation_intents(rt, block=ledger, inject_failure=stop)
    raise AssertionError('consumer did not stop')


def test_consumer_process_checkpoints():
    for phase in ('before_ledger_replace', 'after_ledger_replace', 'after_preparation', 'before_ack'):
        with TemporaryDirectory() as raw:
            rt, ledger = fixture(raw)
            change_opening_source(rt, 3600)
            pending = intents.read_account_request(rt)
            documents = rt.list_cny_documents()
            result = subprocess.run([sys.executable, __file__, 'consume-child', raw, phase], capture_output=True, text=True)
            assert result.returncode == 73, (phase, result.stderr)
            assert intents.read_account_request(rt)['status'] == 'pending'
            assert not any(row['source_revision'].startswith('cny-preparation:'+str(pending['revision'])+':') for row in queue(rt))
            assert intents.drain_cny_preparation_intents(rt)['status'] == 'ok'
            assert rt.list_cny_documents() == documents
            assert len(rt.list_cny_ledger_operations()) == 3
            with _connect(rt.db_path) as conn:
                rows = conn.execute('SELECT shipment_id,paid_rub FROM sheet_vitrina_v1_own_capital_payment_layers ORDER BY shipment_id').fetchall()
            assert [tuple(row) for row in rows] == [('a','1200'), ('b','1200')]
    print('hard_consumer_exits=4; exact revision recovered, 3 money docs unchanged')


def test_real_functional_consumption():
    from apps.warehouse_targeted_replay_smoke import _seed_functional
    from packages.application.supplier_preparation_intents import drain_supplier_preparation_intents
    from packages.application.fulfillment_services import _ensure_schema as ensure_fulfillment
    from packages.application.warehouse_functional import WarehouseFunctionalBlock
    import time

    with TemporaryDirectory() as raw:
        rt, ledger = fixture(raw)
        drain_supplier_preparation_intents(rt)
        _seed_functional(rt)
        functional = WarehouseFunctionalBlock(runtime=rt, timestamp_factory=lambda: '2026-07-04T10:00:00Z')
        with _connect(rt.db_path) as conn:
            ensure_fulfillment(conn)
            conn.execute('DELETE FROM '+QUEUE)
            conn.execute('DELETE FROM sheet_vitrina_v1_warehouse_functional_balances')
            conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id<>'base'")
            conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id<>'base'")
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_versions SET version_kind='functional_cutover',effective_at=?,business_effective_date='2026-07-01' WHERE version_id='base'", (NOW,))
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_cutovers SET cutover_at=?", (NOW,))
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_snapshots SET snapshot_date='2026-07-04',raw_rows_json='[]',items_json=?,raw_row_count=0", (json.dumps([{'nm_id':101,'quantity':'0'},{'nm_id':202,'quantity':'0'}]),))
            for nm_id in (101, 202):
                conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_opening_cost_map SELECT cutover_id,?,'10','10','direct_24_06','{}','fixture',? FROM sheet_vitrina_v1_warehouse_functional_cutovers", (nm_id,NOW))
            conn.execute("INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions VALUES('fixture',1,'2026-07-01','fixture','2026-07-01','0',0,'0',0,0,'fixture','{}',1,?,NULL)", (NOW,))
            conn.commit()
        documents_before = len(rt.list_cny_documents())
        results = []
        for rub_value in (3600, 3000):
            started = time.perf_counter_ns()
            change_opening_source(rt, rub_value)
            source_committed = time.perf_counter_ns()
            worker = WbSuppliesBlock(runtime=rt)
            with patch.object(worker, '_ensure_ff_stock_wb_auto_writeoff_checkpoint', return_value={}), patch.object(worker.ff_stock_ledger, 'apply_confirmed_wb_supply_returns', return_value={}), patch.object(worker.ff_stock_ledger, 'record_wb_supply_debits', return_value={}):
                outcome = worker.reconcile_functional_ff_state()
            assert outcome['cny_preparation']['status'] == 'ok', outcome
            pending = [row for row in queue(rt) if row['status'] == 'queued']
            assert len(pending) == 1 and json.loads(pending[0]['affected_nm_ids_json']) == [101,202], pending
            queued = time.perf_counter_ns()
            plan = functional.build_targeted_recovery_plan(affected_nm_ids=[101,202], stable_source_ids=[intents.SOURCE_ID], targeted_recalc_requests=pending)
            assert plan['target_scope']['non_target_changed_line_count'] == 0
            published = functional.apply_plan(plan, confirm_fingerprint=plan['plan_fingerprint'])
            completed = time.perf_counter_ns()
            assert published['status'] == 'ready', published
            assert all(row['status'] == 'complete' for row in queue(rt))
            with _connect(rt.db_path) as conn:
                rows = conn.execute("SELECT nm_id,quantity,capital_rub FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=(SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1) AND warehouse_key='china_to_ff' ORDER BY nm_id").fetchall()
            actual = [tuple(row) for row in rows]
            assert actual == [(101,'10',str(rub_value//3)),(202,'10',str(rub_value//3))], actual
            assert len(rt.list_cny_documents()) == documents_before == 3
            assert intents.read_account_request(rt)['status'] == 'delivered'
            assert ledger.get_status()['account_preparation']['warehouse_targeted_recalculation']['status'] == 'complete'
            results.append({'rub_value':rub_value, 'actual':actual, 'revision':pending[0]['source_revision'],
                'source_ms':(source_committed-started)/1e6, 'queue_ms':(queued-source_committed)/1e6,
                'source_to_result_ms':(completed-source_committed)/1e6})
        print('actual_consumer=' + json.dumps(results))


def test_existing_database_trigger_upgrade():
    import re
    from packages.application.warehouse_business_projection import ensure_warehouse_projection_source_outbox, OUTBOX_TABLE
    with TemporaryDirectory() as raw:
        rt, ledger = fixture(raw)
        with _connect(rt.db_path) as conn:
            definition = conn.execute("SELECT sql FROM sqlite_master WHERE name='warehouse_projection_own_capital_event'").fetchone()[0]
            # Exact PR1269 CASE expression, which already contains the old
            # generic evidence_hash marker. CNY is the additional upgrade.
            old = re.sub(r"CASE WHEN \(NEW.event_type='supplier_payment'.*?ELSE CASE WHEN", 'CASE WHEN', definition, count=1, flags=re.S).replace('END END,','END,')
            assert old != definition and "NEW.event_id || '_' || NEW.evidence_hash" in old
            assert "NEW.event_type='supplier_payment'" not in old
            conn.execute('DROP TRIGGER warehouse_projection_own_capital_event')
            conn.execute(old)
            conn.execute(f"UPDATE {OUTBOX_TABLE} SET request_id='whbpo_event_'||substr(stable_source_id,length('own_capital_event:')+1) WHERE source_kind='supplier_payment'")
            before = [dict(row) for row in conn.execute(f'SELECT * FROM {OUTBOX_TABLE} ORDER BY request_id')]
            ensure_warehouse_projection_source_outbox(conn)
            installed = conn.execute("SELECT sql FROM sqlite_master WHERE name='warehouse_projection_own_capital_event'").fetchone()[0]
            assert installed == definition
            version = conn.execute('PRAGMA schema_version').fetchone()[0]
            ensure_warehouse_projection_source_outbox(conn)
            assert conn.execute('PRAGMA schema_version').fetchone()[0] == version
            assert [dict(row) for row in conn.execute(f'SELECT * FROM {OUTBOX_TABLE} ORDER BY request_id')] == before
            conn.commit()
        assert perform(rt, ledger, 'opening')['readback_confirmed']
        assert intents.read_account_request(rt)['status'] == 'delivered'
        assert len(rt.list_cny_documents()) == 3


def test_fee_revisions():
    with TemporaryDirectory() as raw:
        rt, ledger = fixture(raw)
        fee = perform(rt, ledger, 'fee')
        fee_id = fee['document_id']
        def total():
            with _connect(rt.db_path) as conn:
                return [(row['shipment_id'],row['capital_rub']) for row in conn.execute("SELECT shipment_id,capital_rub FROM sheet_vitrina_v1_own_capital_events WHERE event_type='cost_payment' AND instr(event_id,?)=1", ('cost_payment:'+fee_id+':',))]
        assert ledger.replay_ledger(reason='fee')['status'] == 'ok'
        assert total() == [('a','10')], total()
        document = rt.load_cny_document(fee_id)
        rt.save_cny_document({**document, 'cny_amount':'2', 'parsed_payload':{**document['parsed_payload'],'fee_cny':'2'}})
        assert ledger.replay_ledger(reason='fee_revision')['status'] == 'ok'
        assert total() == [('a','20')], total()
        document = rt.load_cny_document(fee_id)
        rt.save_cny_document({**document,'parsed_payload':{**document['parsed_payload'],'payment_date_provenance':{'source':'statement_correction'}}})
        assert ledger.replay_ledger(reason='fee_provenance')['status'] == 'ok'
        rt.update_cny_document_context(document_id=fee_id,source_order_id='c',context_order_id='c',updated_at=NOW)
        assert ledger.replay_ledger(reason='fee_relink')['status'] == 'ok'
        assert total() == [('c','20')], total()
        document = rt.load_cny_document(fee_id)
        rt.save_cny_document({**document,'status':'excluded'})
        assert ledger.replay_ledger(reason='fee_archive')['status'] == 'ok'
        assert total() == []
        rt.save_cny_document(document)
        assert ledger.replay_ledger(reason='fee_restore')['status'] == 'ok'
        assert total() == [('c','20')]
        assert len(rt.list_cny_documents()) == 4


def test_validation_stays_closed():
    with TemporaryDirectory() as raw:
        rt, ledger = fixture(raw)
        document = rt.load_cny_document('pay-a')
        rt.save_cny_document({**document, 'cny_amount':'200',
            'parsed_payload':{**document['parsed_payload'],'cny_amount':'200','transfer_amount':'200'}})
        outcome = ledger.replay_ledger(reason='invalid_overpayment')
        assert outcome['status'] == 'pending' and outcome['readback_confirmed']
        assert intents.read_account_request(rt)['status'] == 'error'
        assert len(rt.list_cny_documents()) == 3
        with _connect(rt.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_own_capital_payment_layers WHERE payment_id='pay-a'").fetchone()[0] == 0
            assert conn.execute("SELECT paid_rub FROM sheet_vitrina_v1_own_capital_payment_layers WHERE payment_id='pay-b'").fetchone()[0] == '1000'
        # Correcting the saved evidence resumes the same account; no new money
        # document is required, even after compensation has already happened.
        rt.save_cny_document(document)
        assert ledger.replay_ledger(reason='corrected_overpayment')['status'] == 'ok'
        with _connect(rt.db_path) as conn:
            before = dict(conn.execute("SELECT * FROM sheet_vitrina_v1_own_capital_payment_layers WHERE payment_id='pay-a'").fetchone())
        shipment = rt.load_supplier_shipment('a')
        rt.save_supplier_shipment(header=shipment['header'],lines=[{**row,'match_status':'unmatched'} for row in shipment['lines']])
        outcome = ledger.replay_ledger(reason='invalid_matching')
        assert outcome['status'] == 'pending'
        with _connect(rt.db_path) as conn:
            assert dict(conn.execute("SELECT * FROM sheet_vitrina_v1_own_capital_payment_layers WHERE payment_id='pay-a'").fetchone()) == before
        assert len(rt.list_cny_documents()) == 3


def main():
    for test in (test_account_scope_revisions_and_duplicates, test_new_revision_and_consumer_race,
                 test_source_and_late_failures, test_noop_and_missing_link, test_process_source_boundaries,
                 test_consumer_process_checkpoints, test_real_functional_consumption, test_existing_database_trigger_upgrade, test_fee_revisions, test_validation_stays_closed):
        test(); print(test.__name__ + ': ok')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'child': child(*sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == 'consume-child': consumer_child(*sys.argv[2:])
    else: main()
