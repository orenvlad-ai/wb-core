"""Supplier source commit/continuation crash, exact scope and restart contracts."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _connect
from packages.application import supplier_preparation_intents as intents
from packages.application.supplier_shipment_invoice_revision import SupplierInvoiceRevisionAdapter, AUDIT
from packages.application.wb_supplies import WbSuppliesBlock
from apps.supplier_invoice_revision_smoke import fixture as invoice_fixture

NOW = '2026-09-12T00:00:00Z'
HEADER = dict(shipment_id='source', created_at=NOW, updated_at=NOW, shipment_date='2026-09-20', invoice_date='2026-08-01', invoice_no='probe', currency='CNY', order_status='production', match_status='all_matched')
LINES = [dict(line_id='line', line_type='product', internal_nm_id=101, qty=10, unit_price=10, amount=100, match_status='matched')]
QUEUE = 'sheet_vitrina_v1_warehouse_targeted_recalc_queue'


def source(runtime, header=None, lines=None):
    runtime.save_supplier_shipment(header=header or HEADER, lines=lines or LINES)


def request(runtime):
    with _connect(runtime.db_path) as conn:
        return dict(conn.execute(f'SELECT * FROM {intents.TABLE} WHERE shipment_id=?', ('source',)).fetchone())


def queue(runtime):
    with _connect(runtime.db_path) as conn:
        return [dict(row) for row in conn.execute(f'SELECT * FROM {QUEUE} ORDER BY requested_at,queue_id')]


def finish(runtime):
    result = intents.drain_supplier_preparation_intents(runtime)
    assert result['status'] == 'queued', result
    return result['requests'][0]


def test_source_boundaries():
    with TemporaryDirectory() as raw:
        rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
        source(rt)
        before = request(rt)
        assert before['revision'] == 1 and before['status'] == 'pending'
        assert json.loads(before['affected_nm_ids_json']) == [101]
        first = finish(rt)
        assert first['source_revision'].startswith('supplier-preparation:1:')
        assert queue(rt)[0]['status'] == 'queued'
        # Identical input and repeated consumer do not rerun preparation.
        with patch.object(intents, '_prepare', side_effect=AssertionError('duplicate preparation')):
            source(rt)
            assert request(rt)['revision'] == 1
            assert intents.drain_supplier_preparation_intents(rt)['status'] == 'no_op'
        changed = deepcopy(LINES); changed[0]['internal_nm_id'] = 202
        later = {**HEADER, 'invoice_date': '2026-08-10'}
        source(rt, later, changed)
        second = request(rt)
        assert second['revision'] == 2
        assert json.loads(second['affected_nm_ids_json']) == [101, 202]
        assert second['effective_date'] == '2026-08-01'
        finish(rt)
        # Returning to A creates revision 3, not an old completed digest.
        source(rt)
        assert request(rt)['revision'] == 3
        finish(rt)
        # A failure within intent creation must roll back the primary fact.
        with _connect(rt.db_path) as conn:
            conn.execute(f"CREATE TRIGGER reject_intent BEFORE UPDATE ON {intents.TABLE} BEGIN SELECT RAISE(ABORT,'stop-before-source-commit'); END")
            conn.commit()
        try:
            source(rt, {**HEADER, 'invoice_no': 'never-saved'})
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError('source committed without intent')
        assert rt.load_supplier_shipment('source')['header']['invoice_no'] == 'probe'
        assert request(rt)['revision'] == 3


def test_retry_and_new_revision():
    for phase in ('before_preparation', 'after_preparation', 'before_ack'):
        with TemporaryDirectory() as raw:
            rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw)); source(rt)
            def fail(current, _):
                if current == phase:
                    raise RuntimeError('simulated-process-stop:' + phase)
            outcome = intents.drain_supplier_preparation_intents(rt, inject_failure=fail)
            assert outcome['status'] == 'pending', outcome
            assert request(rt)['status'] == 'error'
            # Fresh consumer instance/connection restarts just the saved revision.
            finish(rt)
            assert len(queue(rt)) == 1
            assert request(rt)['status'] == 'delivered'
    with TemporaryDirectory() as raw:
        rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw)); source(rt)
        def concurrent(phase, _):
            if phase == 'after_preparation':
                changed = deepcopy(LINES); changed[0]['internal_nm_id'] = 202
                source(rt, {**HEADER, 'invoice_date': '2026-08-10'}, changed)
        result = intents.drain_supplier_preparation_intents(rt, inject_failure=concurrent)
        assert result['status'] == 'pending'
        newest = request(rt)
        assert newest['revision'] == 2 and newest['status'] == 'pending'
        assert json.loads(newest['affected_nm_ids_json']) == [101, 202]
        delivered = finish(rt)
        assert delivered['preparation_revision'] == 2
        assert len(queue(rt)) == 1 and queue(rt)[0]['source_revision'].startswith('supplier-preparation:2:')


def test_financial_scope_and_archive():
    with TemporaryDirectory() as raw:
        rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw)); source(rt); finish(rt)
        doc = dict(document_id='expense',supplier_order_id='source',document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date='2026-07-30',parse_status='confirmed',total_amount_rub=200)
        line = dict(line_id='expense-line',amount_rub=200,amount=200,currency='RUB',category='logistics',status='confirmed')
        rt.save_supplier_financial_document(document=doc, expense_lines=[line])
        saved = request(rt)
        # Volatile timestamps / JSON key order cannot create a source revision.
        rt.save_supplier_financial_document(document={**doc,'updated_at':'2026-09-12T01:00:00Z'},expense_lines=[dict(reversed(list(line.items())))])
        assert request(rt)['revision']==saved['revision']
        assert saved['effective_date'] == '2026-07-30'
        assert json.loads(saved['document_ids_json']) == ['expense']
        finish(rt)
        rt.update_supplier_financial_document_status(supplier_order_id='source',document_id='expense',parse_status='excluded',updated_at=NOW)
        finish(rt)
        rt.delete_supplier_financial_document(supplier_order_id='source',document_id='expense')
        assert json.loads(request(rt)['document_ids_json']) == ['expense']
        finish(rt)
        rt.archive_supplier_shipment(shipment_id='source',archived_at=NOW)
        finish(rt)
        before = request(rt)['revision']
        rt.archive_supplier_shipment(shipment_id='source',archived_at=NOW)
        assert request(rt)['revision'] == before


def test_financial_rebind_and_revision():
    with TemporaryDirectory() as raw:
        rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw)); source(rt); finish(rt)
        source(rt, {**HEADER, 'shipment_id':'other', 'invoice_date':'2026-07-01'}, [{**LINES[0], 'line_id':'other-line', 'internal_nm_id':202}]); finish(rt)
        doc = dict(document_id='rebind',supplier_order_id='source',document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date='2026-08-05',parse_status='confirmed',total_amount_rub=200)
        expense = dict(line_id='rebind-line',amount=200,amount_rub=200,currency='RUB',category='logistics',status='confirmed')
        rt.save_supplier_financial_document(document=doc,expense_lines=[expense]); finish(rt)
        rt.save_supplier_financial_document(document={**doc,'document_id':'unrelated','supplier_order_id':'other','total_amount_rub':50},expense_lines=[{**expense,'line_id':'unrelated-expense','amount':50,'amount_rub':50}]); finish(rt)
        from packages.application.warehouse_business_projection import ensure_warehouse_projection_source_outbox, OUTBOX_TABLE
        with _connect(rt.db_path) as conn:
            event_id = conn.execute("SELECT event_id FROM sheet_vitrina_v1_own_capital_events WHERE shipment_id='source'").fetchone()[0]
            conn.execute(f"UPDATE {OUTBOX_TABLE} SET request_id=?,status='published_exact' WHERE stable_source_id=?", ('whbpo_event_'+event_id, 'own_capital_event:'+event_id))
            legacy_row = dict(conn.execute(f"SELECT * FROM {OUTBOX_TABLE} WHERE request_id=?", ('whbpo_event_'+event_id,)).fetchone())
            unrelated_event = dict(conn.execute("SELECT * FROM sheet_vitrina_v1_own_capital_events WHERE shipment_id='other'").fetchone())
            unrelated_outbox = [dict(row) for row in conn.execute(f"SELECT * FROM {OUTBOX_TABLE} WHERE stable_source_id=?", ('own_capital_event:'+unrelated_event['event_id'],))]
            current_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='warehouse_projection_own_capital_event'").fetchone()[0]
            modern = "CASE WHEN instr(NEW.event_id,'cost_payment:financial_expense:')=1\n                  THEN 'whbpo_event_' || NEW.event_id || '_' || NEW.evidence_hash\n                  ELSE 'whbpo_event_' || NEW.event_id END"
            assert modern in current_sql
            conn.execute('DROP TRIGGER warehouse_projection_own_capital_event')
            conn.execute(current_sql.replace(modern, "'whbpo_event_' || NEW.event_id"))
            ensure_warehouse_projection_source_outbox(conn)
            first_version = conn.execute('PRAGMA schema_version').fetchone()[0]
            ensure_warehouse_projection_source_outbox(conn)
            assert conn.execute('PRAGMA schema_version').fetchone()[0] == first_version
            assert dict(conn.execute(f"SELECT * FROM {OUTBOX_TABLE} WHERE request_id=?", (legacy_row['request_id'],)).fetchone()) == legacy_row
            conn.commit()
        rt.save_supplier_financial_document(document={**doc,'total_amount_rub':300},expense_lines=[{**expense,'amount':300,'amount_rub':300}]); finish(rt)
        rt.save_supplier_financial_document(document={**doc,'supplier_order_id':'other','total_amount_rub':300},expense_lines=[{**expense,'amount':300,'amount_rub':300}])
        with _connect(rt.db_path) as conn:
            pending = [dict(row) for row in conn.execute(f"SELECT * FROM {intents.TABLE} WHERE status='pending' ORDER BY shipment_id")]
        assert {row['shipment_id'] for row in pending} == {'source','other'}
        assert {row['shipment_id']:json.loads(row['affected_nm_ids_json']) for row in pending} == {'source':[101], 'other':[202]}
        assert {row['shipment_id']:row['effective_date'] for row in pending} == {'source':'2026-08-01', 'other':'2026-07-01'}
        # New owner first; old-owner recovery cannot delete the new allocation.
        assert intents.drain_supplier_preparation_intents(rt,shipment_ids=['other'])['status']=='queued'
        assert intents.drain_supplier_preparation_intents(rt,shipment_ids=['source'])['status']=='queued'
        with _connect(rt.db_path) as conn:
            events = [dict(row) for row in conn.execute("SELECT shipment_id,nm_id,capital_rub FROM sheet_vitrina_v1_own_capital_events WHERE event_id LIKE 'cost_payment:financial_expense:rebind:%'")]
        assert events == [{'shipment_id':'other','nm_id':202,'capital_rub':'300'}], events
        rt.save_supplier_financial_document(document={**doc,'supplier_order_id':'other','total_amount_rub':0},expense_lines=[])
        assert intents.drain_supplier_preparation_intents(rt,shipment_ids=['other'])['status']=='queued'
        with _connect(rt.db_path) as conn:
            assert conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_own_capital_events').fetchone()[0] == 1
            assert dict(conn.execute('SELECT * FROM sheet_vitrina_v1_own_capital_events').fetchone()) == unrelated_event
            assert [dict(row) for row in conn.execute(f"SELECT * FROM {OUTBOX_TABLE} WHERE stable_source_id=?", ('own_capital_event:'+unrelated_event['event_id'],))] == unrelated_outbox
            assert dict(conn.execute(f"SELECT * FROM {OUTBOX_TABLE} WHERE request_id=?", (legacy_row['request_id'],)).fetchone()) == legacy_row
            assert conn.execute(f"SELECT COUNT(*) FROM {OUTBOX_TABLE} WHERE stable_source_id=? AND source_revision NOT LIKE 'deleted:%'", (legacy_row['stable_source_id'],)).fetchone()[0] == 2




def test_trigger_upgrade_atomicity():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event
    from packages.application.warehouse_business_projection import _ensure_supplier_expense_event_trigger
    with TemporaryDirectory() as raw:
        rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw)); source(rt); finish(rt)
        with sqlite3.connect(rt.db_path) as conn:
            modern = conn.execute("SELECT sql FROM sqlite_master WHERE name='warehouse_projection_own_capital_event'").fetchone()[0]
            expression = "CASE WHEN instr(NEW.event_id,'cost_payment:financial_expense:')=1\n                  THEN 'whbpo_event_' || NEW.event_id || '_' || NEW.evidence_hash\n                  ELSE 'whbpo_event_' || NEW.event_id END"
            legacy = modern.replace(expression, "'whbpo_event_' || NEW.event_id")
            assert modern != legacy
            conn.execute('DROP TRIGGER warehouse_projection_own_capital_event'); conn.execute(legacy)
            conn.execute('CREATE TABLE upgrade_outer_probe(value INTEGER)'); conn.execute('INSERT INTO upgrade_outer_probe VALUES(0)'); conn.commit()
        class Wrapped:
            def __init__(self, conn, hook): self.conn, self.hook = conn, hook
            def __getattr__(self, key): return getattr(self.conn, key)
            def execute(self, sql, *args):
                result = self.conn.execute(sql, *args)
                self.hook(sql)
                return result
        def stopped(sql):
            if sql.startswith('DROP TRIGGER'):
                raise SystemExit('stop-after-drop')
        for outer in (False, True):
            with sqlite3.connect(rt.db_path) as conn:
                if outer:
                    conn.execute('BEGIN IMMEDIATE'); conn.execute('UPDATE upgrade_outer_probe SET value=1')
                try: _ensure_supplier_expense_event_trigger(Wrapped(conn, stopped), modern)
                except SystemExit: pass
                else: raise AssertionError('upgrade crash injection missing')
                assert conn.in_transaction == outer
                assert conn.execute("SELECT sql FROM sqlite_master WHERE name='warehouse_projection_own_capital_event'").fetchone()[0] == legacy
                if outer:
                    assert conn.execute('SELECT value FROM upgrade_outer_probe').fetchone()[0] == 1
                    conn.rollback()
        # A successful nested upgrade is still part of the caller's rollback.
        with sqlite3.connect(rt.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE'); conn.execute('UPDATE upgrade_outer_probe SET value=2')
            _ensure_supplier_expense_event_trigger(conn, modern)
            assert conn.in_transaction
            conn.rollback()
            assert conn.execute("SELECT sql FROM sqlite_master WHERE name='warehouse_projection_own_capital_event'").fetchone()[0] == legacy
            assert conn.execute('SELECT value FROM upgrade_outer_probe').fetchone()[0] == 0
        dropped, checked = Event(), Event()
        def hold(sql):
            if sql.startswith('DROP TRIGGER'):
                dropped.set(); assert checked.wait(5)
        def upgrade():
            with sqlite3.connect(rt.db_path, timeout=5) as conn:
                _ensure_supplier_expense_event_trigger(Wrapped(conn, hold), modern)
        with ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(upgrade); assert dropped.wait(5)
            with sqlite3.connect(rt.db_path, timeout=0.05) as conn:
                try:
                    conn.execute("INSERT INTO sheet_vitrina_v1_own_capital_events(event_id,event_type,effective_date,shipment_id,supply_id,nm_id,stage_from,stage_to,quantity,capital_rub,confirmed_quantity,cost_layer_id,warehouse,destination,payload_json,evidence_hash,created_at) VALUES('cost_payment:financial_expense:concurrent:1','cost_payment','2026-08-01','source','',101,'','production','0','10','0','','','','{}','race',?)", (NOW,))
                except sqlite3.OperationalError as exc:
                    assert 'locked' in str(exc)
                else:
                    raise AssertionError('writer entered the uncommitted trigger gap')
            checked.set(); work.result(timeout=5)
        # Two initializers that observed the old definition serialize and only
        # the first performs DROP; the second rechecks inside its transaction.
        with sqlite3.connect(rt.db_path) as conn:
            conn.execute('DROP TRIGGER warehouse_projection_own_capital_event'); conn.execute(legacy)
        barrier = Barrier(2); drops = []
        class ConcurrentInit(Wrapped):
            def execute(self, sql, *args):
                if sql == 'BEGIN IMMEDIATE': barrier.wait(timeout=5)
                return super().execute(sql, *args)
        def initialize(_):
            with sqlite3.connect(rt.db_path, timeout=5) as conn:
                _ensure_supplier_expense_event_trigger(ConcurrentInit(conn, lambda sql: drops.append(sql) if sql.startswith('DROP TRIGGER') else None), modern)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(initialize, range(2)))
        assert len(drops) == 1
        with sqlite3.connect(rt.db_path) as conn:
            assert conn.execute("SELECT sql FROM sqlite_master WHERE name='warehouse_projection_own_capital_event'").fetchone()[0] == modern
            assert conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_own_capital_events WHERE event_id='cost_payment:financial_expense:concurrent:1'").fetchone()[0] == 0


def test_existing_worker_and_broken_scope():
    with TemporaryDirectory() as raw:
        rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw)); source(rt)
        block = WbSuppliesBlock(runtime=rt)
        with patch.object(block, '_ensure_ff_stock_wb_auto_writeoff_checkpoint', return_value={}), patch.object(block.ff_stock_ledger, 'apply_confirmed_wb_supply_returns', return_value={}), patch.object(block.ff_stock_ledger, 'record_wb_supply_debits', return_value={}):
            result = block.reconcile_functional_ff_state()
        assert result['supplier_preparation']['requests'][0]['status'] == 'queued'
        assert request(rt)['status'] == 'delivered'
        broken = deepcopy(LINES); broken[0]['internal_nm_id'] = None
        # A newly saved unrelated source has no valid scope even though another queue exists.
        source(rt, {**HEADER, 'shipment_id': 'broken'}, [{**broken[0], 'line_id': 'broken-line'}])
        outcome = intents.drain_supplier_preparation_intents(rt, shipment_ids=['broken'])
        assert outcome['status'] == 'pending'
        assert 'no proven SKU/date scope' in outcome['requests'][0]['error']
        assert len(queue(rt)) == 1


def test_invoice_audit_recovery():
    with TemporaryDirectory() as raw:
        req, rt, _, _ = invoice_fixture(Path(raw))
        adapter = SupplierInvoiceRevisionAdapter(); preview = adapter.preview(req, 'source-crash')
        with patch.object(adapter, 'enqueue', side_effect=RuntimeError('stop-after-commit')):
            result = adapter.apply(req, 'source-crash', preview)
        assert result['queue']['status'] == 'pending'
        readback = adapter.readback(req, 'source-crash')
        assert readback['state'] == 'applied' and readback['source_verified']
        outcome = intents.drain_supplier_preparation_intents(rt)
        assert outcome['status'] == 'queued', outcome
        assert len(queue(rt)) == 1
        with _connect(rt.db_path) as conn:
            assert conn.execute(f'SELECT COUNT(*) FROM {AUDIT}').fetchone()[0] == 1
        assert intents.drain_supplier_preparation_intents(rt)['status'] == 'no_op'


def test_statement_composite_restart():
    from packages.application.supplier_financial_documents import SupplierFinancialDocumentsBlock, BANK_FEE_LOGICAL_GROUPING_VERSION
    from packages.application.cny_ledger import CnyLedgerBlock
    for atomic_cny in (False, True):
        with TemporaryDirectory() as raw:
            rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw)); source(rt); finish(rt)
            ledger = CnyLedgerBlock(runtime=rt, timestamp_factory=lambda: NOW)
            ledger.create_opening_balance({'operation_date':'2026-07-01','cny_amount':100,'rub_value':1000})
            block = SupplierFinancialDocumentsBlock(runtime=rt,timestamp_factory=lambda: NOW)
            fee = dict(row_id='fee-row',semantic_operation_id='fee-operation',logical_fee_id='fee-group',operation_date='2026-08-05',amount='5',currency='CNY',confidence='strong',fee_category='bank_transfer_fee',import_allowed=True,operation_status='new')
            revision = block._bank_fee_preview_revision('source',statement_file_sha256='a'*64,exclude_document_id='statement')
            doc = dict(document_id='statement',supplier_order_id='source',document_type='bank_fee_statement',file_sha256='a'*64,uploaded_at=NOW,updated_at=NOW,parse_status='parsed',document_date='2026-08-05',normalized_parse={'statement_import':{'logical_grouping_version':BANK_FEE_LOGICAL_GROUPING_VERSION,'target_revision':revision,'matched_fee_rows':[fee],'logical_fee_groups':[],'confirmed_operation_ids':[]}})
            rt.save_supplier_financial_document(document=doc,expense_lines=[])
            factory = (lambda row, document: ledger.build_bank_fee_document(source_order_id='source',linked_financial_document_id='statement',natural_key=row['cny_ledger_natural_key'],fee_row=row)) if atomic_cny else None
            with _connect(rt.db_path) as conn:
                conn.execute(f"CREATE TRIGGER reject_statement_intent BEFORE UPDATE ON {intents.TABLE} BEGIN SELECT RAISE(ABORT,'stop-before-composite-commit'); END")
                conn.commit()
            try:
                block.confirm_bank_fee_statement_import('source','statement',selected_operation_ids=['fee-operation'],expected_source_sha256='a'*64,expected_target_revision=revision,defer_downstream=True,cny_document_factory=factory)
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError('composite source committed without its intent')
            assert rt.load_supplier_financial_document(supplier_order_id='source',document_id='statement')['parse_status']=='parsed'
            assert not rt.list_supplier_bank_operation_assignments()
            assert not [row for row in rt.list_cny_documents() if row.get('linked_financial_document_id')=='statement']
            with _connect(rt.db_path) as conn:
                conn.execute('DROP TRIGGER reject_statement_intent'); conn.commit()
            confirmed = block.confirm_bank_fee_statement_import('source','statement',selected_operation_ids=['fee-operation'],expected_source_sha256='a'*64,expected_target_revision=revision,defer_downstream=True,cny_document_factory=factory)
            assert confirmed['parse_status'] == 'confirmed'
            assert len(rt.list_supplier_bank_operation_assignments()) == 1
            assert len([row for row in rt.list_cny_documents() if row.get('linked_financial_document_id')=='statement']) == int(atomic_cny)
            # Stop after the parent commit: fresh worker reconstructs precisely
            # the missing continuation without replaying primary confirmation.
            with patch.object(CnyLedgerBlock,'replay_ledger',side_effect=RuntimeError('stop-inside-composite-preparation')):
                failed = intents.drain_supplier_preparation_intents(rt)
            assert failed['status']=='pending'
            assert len(rt.list_supplier_bank_operation_assignments())==1
            assert len([row for row in rt.list_cny_documents() if row.get('linked_financial_document_id')=='statement'])==1
            outcome = intents.drain_supplier_preparation_intents(rt)
            assert outcome['status'] == 'queued', outcome
            linked = [row for row in rt.list_cny_documents() if row.get('linked_financial_document_id')=='statement']
            assert len(linked)==1 and linked[0]['status']=='posted', linked
            assert len(rt.list_supplier_bank_operation_assignments()) == 1
            with patch.object(intents,'_prepare',side_effect=AssertionError('duplicate composite preparation')):
                repeated = block.confirm_bank_fee_statement_import('source','statement',selected_operation_ids=['fee-operation'],expected_source_sha256='a'*64,expected_target_revision=revision)
            assert repeated['idempotent']
            if atomic_cny:
                from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
                entry = RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir, runtime=rt)
                entry.supplier_financial_documents_block = block
                with patch.object(block,'get_document',side_effect=OSError('late-readback-unavailable')), patch.object(block,'finalize_bank_fee_statement_import',side_effect=RuntimeError('late-continuation-unavailable')):
                    late = entry.handle_supplier_financial_document_confirm_import_request('source','statement',selected_operation_ids=['fee-operation'],expected_source_sha256='a'*64,expected_target_revision=revision)
                assert late['http_status']==202 and late['operation_applied'] and late['readback_pending']
                assert late['document_id']=='statement'
                assert len(rt.list_supplier_bank_operation_assignments())==1
                assert len([row for row in rt.list_cny_documents() if row.get('linked_financial_document_id')=='statement'])==1
            # The archive source commit must retain linked-status + replay work.
            rt.update_supplier_financial_document_status(supplier_order_id='source',document_id='statement',parse_status='excluded',updated_at=NOW)
            outcome = intents.drain_supplier_preparation_intents(rt)
            assert outcome['status'] == 'queued', outcome
            assert rt.list_cny_documents()[-1]['status']=='excluded' or any(row['status']=='excluded' and row.get('linked_financial_document_id')=='statement' for row in rt.list_cny_documents())
            assert len(rt.list_supplier_bank_operation_assignments()) == 1


def test_invoice_late_audit_receipt():
    from packages.application import supplier_shipment_invoice_revision as invoice
    with TemporaryDirectory() as raw:
        req, rt, _, _ = invoice_fixture(Path(raw))
        adapter = SupplierInvoiceRevisionAdapter(); preview = adapter.preview(req, 'receipt-crash')
        original = invoice.update
        def stop_receipt(conn, table, *args, **kwargs):
            if table == AUDIT:
                raise sqlite3.OperationalError('stop-after-enqueue-before-queue-json')
            return original(conn, table, *args, **kwargs)
        with patch.object(invoice, 'update', stop_receipt):
            applied = adapter.apply(req, 'receipt-crash', preview)
        assert applied['queue']['audit_receipt_pending']
        assert len(queue(rt)) == 1 and queue(rt)[0]['status'] == 'queued'
        readback = adapter.readback(req, 'receipt-crash')
        assert readback['state'] == 'applied' and readback['source_verified']
        assert readback['queue']['operation_id'] == 'receipt-crash'
        assert readback['queue']['status'] == 'pending'
        with patch.object(adapter, 'apply', side_effect=AssertionError('source resubmit')):
            assert adapter.readback(req, 'receipt-crash')['state'] == 'applied'
        assert intents.drain_supplier_preparation_intents(rt)['status'] == 'no_op'


def test_uncaught_process_exit():
    import subprocess
    from packages.application.own_product_capital import OwnProductCapitalBlock
    with TemporaryDirectory() as raw:
        rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw)); source(rt); finish(rt)
        rt.save_supplier_financial_document(document=dict(document_id='crash-doc', supplier_order_id='source', document_type='logistics_invoice', uploaded_at=NOW, updated_at=NOW, document_date='2026-08-05', parse_status='confirmed', total_amount_rub=200), expense_lines=[dict(line_id='crash-line', amount=200, amount_rub=200, currency='RUB', category='logistics', status='confirmed')])
        # A separate interpreter exits without executing the Exception handler.
        code = """import sys
from pathlib import Path
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.supplier_preparation_intents import drain_supplier_preparation_intents
def stop(phase, request):
    if phase == 'after_preparation':
        raise SystemExit(73)
drain_supplier_preparation_intents(RegistryUploadDbBackedRuntime(runtime_dir=Path(sys.argv[1])), inject_failure=stop)
"""
        child = subprocess.run([sys.executable, '-c', code, raw], cwd=ROOT, check=False)
        assert child.returncode == 73
        assert request(rt)['status'] == 'pending' and request(rt)['prepared_at'] is None
        with _connect(rt.db_path) as conn:
            before = conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_own_capital_events').fetchone()[0]
        with patch.object(intents, '_prepare', wraps=intents._prepare) as prepare, patch.object(OwnProductCapitalBlock, 'recalculate', side_effect=AssertionError('unchanged expense recalculated after process exit')):
            finish(rt)
        with _connect(rt.db_path) as conn:
            assert conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_own_capital_events').fetchone()[0] == before == 1
        assert prepare.call_count == 1  # One repeated preparation after hard exit; no repeated calculation/event.
        assert len(queue(rt)) == 2  # Initial shipment plus financial revision.


def test_consumer_to_functional_result():
    from apps.warehouse_targeted_replay_smoke import _seed_functional
    from apps.cny_ledger_smoke import _save_payment
    from packages.application.cny_ledger import CnyLedgerBlock
    from packages.application.fulfillment_services import _ensure_schema as ensure_fulfillment
    from packages.application.warehouse_functional import WarehouseFunctionalBlock
    now = '2026-07-01T10:00:00Z'
    with TemporaryDirectory() as raw:
        rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
        source(rt, {**HEADER, 'invoice_date': '2026-07-01', 'shipment_date': '2026-07-01', 'actual_shipment_date':'2026-07-01', 'order_status':'in_transit'})
        ledger = CnyLedgerBlock(runtime=rt, timestamp_factory=lambda: now)
        ledger.create_opening_balance({'operation_date': '2026-07-01', 'cny_amount': 100, 'rub_value': 1000})
        _save_payment(rt, 'fixture-payment', 'source', now, '100')
        ledger.replay_ledger(reason='temporary-fixture')
        _seed_functional(rt)
        functional = WarehouseFunctionalBlock(runtime=rt, timestamp_factory=lambda: now)
        # A minimal July-1 opening: zero WB quantity, frozen WAC 10, one paid
        # supplier source. All fixture writes precede the tested continuation.
        with _connect(rt.db_path) as conn:
            ensure_fulfillment(conn)
            conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue")
            conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_balances")
            conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id<>'base'")
            conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id<>'base'")
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_versions SET version_kind='functional_cutover',effective_at=?,business_effective_date='2026-07-01' WHERE version_id='base'", (now,))
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_cutovers SET cutover_at=?", (now,))
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_snapshots SET snapshot_date='2026-07-01',raw_rows_json='[]',items_json=?,raw_row_count=0", (json.dumps([{'nm_id':101,'quantity':'0'}]),))
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_opening_cost_map SELECT cutover_id,101,'10','10','direct_24_06','{}','fixture',? FROM sheet_vitrina_v1_warehouse_functional_cutovers", (now,))
            conn.execute("INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions VALUES('fixture',1,'2026-07-01','fixture','2026-07-01','0',0,'0',0,0,'fixture','{}',1,?,NULL)", (now,))
            conn.commit()
        import time
        start = time.perf_counter_ns()
        rt.save_supplier_financial_document(document=dict(document_id='fixture-logistics',supplier_order_id='source',document_type='logistics_invoice',uploaded_at=now,updated_at=now,document_date='2026-07-01',parse_status='confirmed',total_amount_rub=200),expense_lines=[dict(line_id='fixture-logistics-line',amount=200,amount_rub=200,currency='RUB',category='domestic_transport',status='confirmed')])
        source_committed = time.perf_counter_ns()
        from datetime import datetime, timezone
        pending_age_ms = (datetime.now(timezone.utc)-datetime.fromisoformat(request(rt)['requested_at'].replace('Z','+00:00'))).total_seconds()*1000
        worker = WbSuppliesBlock(runtime=rt)
        with patch.object(worker, '_ensure_ff_stock_wb_auto_writeoff_checkpoint', return_value={}), patch.object(worker.ff_stock_ledger, 'apply_confirmed_wb_supply_returns', return_value={}), patch.object(worker.ff_stock_ledger, 'record_wb_supply_debits', return_value={}):
            outcome = worker.reconcile_functional_ff_state()
        assert outcome['supplier_preparation']['status'] == 'queued'
        queue_persisted = time.perf_counter_ns()
        accepted = queue(rt)[0]
        plan = functional.build_targeted_recovery_plan(affected_nm_ids=[101], stable_source_ids=['supplier_shipment:source'], targeted_recalc_requests=[accepted])
        assert plan['target_scope']['non_target_changed_line_count'] == 0
        published = functional.apply_plan(plan, confirm_fingerprint=plan['plan_fingerprint'])
        result_published = time.perf_counter_ns()
        assert published['status'] == 'ready' and queue(rt)[0]['status'] == 'complete'
        print('consumer_pipeline_once: ' + json.dumps({'n':1,'pending_age_before_consumer_ms':pending_age_ms,'source_method_ms':(source_committed-start)/1e6,'source_return_to_queue_ms':(queue_persisted-source_committed)/1e6,'source_return_to_functional_result_ms':(result_published-source_committed)/1e6,'note':'single temporary paid shipment +logistics; upper-bound completion measurements; not production latency'}))
        assert queue(rt)[0]['source_revision'] == accepted['source_revision']
        with _connect(rt.db_path) as conn:
            row = conn.execute("SELECT quantity,capital_rub FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=(SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1) AND warehouse_key='china_to_ff' AND nm_id=101").fetchone()
            assert tuple(row) == ('10', '1200'), dict(row)
        assert intents.resume_supplier_preparation(rt, 'source')['status'] == 'complete'
        from packages.application.warehouse_business_projection import OUTBOX_TABLE
        revisions = [accepted['source_revision']]
        # Model the documented legacy terminal A row after exact publication.
        # A→B→A must be carried by monotonic supplier revisions even when the
        # generic event outbox deliberately retains the old published A status.
        for amount in (300, 200):
            with _connect(rt.db_path) as conn:
                conn.execute(f"UPDATE {OUTBOX_TABLE} SET status='published_exact' WHERE stable_source_id LIKE 'own_capital_event:cost_payment:financial_expense:fixture-logistics:%'")
                conn.commit()
            document = rt.load_supplier_financial_document(supplier_order_id='source',document_id='fixture-logistics')
            rt.save_supplier_financial_document(document={**document,'total_amount_rub':amount},expense_lines=[{**document['expense_lines'][0],'amount':amount,'amount_rub':amount}])
            assert intents.drain_supplier_preparation_intents(rt)['status']=='queued'
            pending = [row for row in queue(rt) if row['status']=='queued']
            assert len(pending)==1 and pending[0]['source_revision'] not in revisions
            revisions.append(pending[0]['source_revision'])
            next_plan = functional.build_targeted_recovery_plan(affected_nm_ids=[101],stable_source_ids=['supplier_shipment:source'],targeted_recalc_requests=pending)
            result = functional.apply_plan(next_plan,confirm_fingerprint=next_plan['plan_fingerprint'])
            assert result['status']=='ready' and all(row['status']=='complete' for row in queue(rt))
            with _connect(rt.db_path) as conn:
                row = conn.execute("SELECT quantity,capital_rub FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=(SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1) AND warehouse_key='china_to_ff' AND nm_id=101").fetchone()
                assert tuple(row)==('10',str(1000+amount)),dict(row)
        assert len(revisions)==len(set(revisions))==3


def test_two_consumers_one_preparation():
    from contextlib import contextmanager
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from packages.application import warehouse_functional_lock as locks
    with TemporaryDirectory() as raw:
        rt=RegistryUploadDbBackedRuntime(runtime_dir=Path(raw));source(rt)
        barrier=Barrier(2);original_lock=locks.warehouse_functional_write_lock
        @contextmanager
        def synchronized_lock(*args,**kwargs):
            barrier.wait(timeout=20)
            with original_lock(*args,**kwargs) as value:
                yield value
        with patch.object(locks,'warehouse_functional_write_lock',synchronized_lock), patch.object(intents,'_prepare',wraps=intents._prepare) as prepare:
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes=list(pool.map(lambda _:intents.drain_supplier_preparation_intents(rt),range(2)))
        assert prepare.call_count==1, outcomes
        assert sorted(outcome['status'] for outcome in outcomes)==['no_op','queued'],outcomes
        assert len(queue(rt))==1


def main():
    for test in (test_source_boundaries,test_retry_and_new_revision,test_financial_scope_and_archive,test_financial_rebind_and_revision,test_trigger_upgrade_atomicity,test_existing_worker_and_broken_scope,test_invoice_audit_recovery,test_statement_composite_restart,test_invoice_late_audit_receipt,test_uncaught_process_exit,test_consumer_to_functional_result,test_two_consumers_one_preparation):
        test(); print(test.__name__ + ': OK')
    print('supplier_preparation_intents_smoke: OK')

if __name__ == '__main__':
    main()
