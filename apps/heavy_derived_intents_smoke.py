"""Real-process exclusion of derived consumers; disposable saved source data only."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.business_data_heavy_admission import heavy_admitted, HeavyAdmissionBusy, require_heavy_owner
from packages.application.business_data_procedure_admission import admission_idle
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _connect
from packages.application import supplier_preparation_intents as supplier, cny_preparation_intents as cny
from packages.application import fulfillment_recalc_intents as fulfillment, nomenclature_activation_intents as nomenclature
from packages.application.ff_pool_dense_fbs import DenseFbsService
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from apps.supplier_preparation_intents_smoke import source, request as supplier_request, queue as supplier_queue, LINES
from apps.cny_preparation_intents_smoke import fixture as cny_fixture
from apps.nomenclature_activation_intents_smoke import fixture as sku_fixture, sources, ordinary
from apps.ff_pool_dense_fbs_smoke import _sku, NOW
from apps.fulfillment_recalc_intents_smoke import fixture as ff_fixture, queue as ff_queue, consume_preparation
from apps.sheet_vitrina_v1_fulfillment_services_smoke import _build_workbook, _valid_row


@contextmanager
def other_heavy(runtime):
    child = subprocess.Popen([sys.executable, '-c',
        'import sys; from pathlib import Path; from packages.application.business_data_heavy_admission import heavy_admitted; '
        '\nwith heavy_admitted(Path(sys.argv[1]),operation="offline-other"):\n print("held",flush=True); sys.stdin.readline()',
        str(runtime.runtime_dir)], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'held'
        yield child
    finally:
        child.stdin.write('\n'); child.stdin.flush()
        _, errors = child.communicate(timeout=20)
        assert child.returncode == 0, errors


def fresh_owned(raw, kind):
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'consume', raw, kind],
        cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def consume(raw, kind):
    rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
    with heavy_admitted(rt.runtime_dir, operation='cycle'):
        # Real existing automatic reconciliation entry, with unrelated physical
        # movements isolated as in its source-intent regression fixtures.
        result = consume_preparation(rt) if kind == 'fulfillment' else ordinary(rt)[kind]
        assert require_heavy_owner(rt.runtime_dir).operation == 'cycle'
        print(json.dumps(result))


class HeavyDerivedSmoke(unittest.TestCase):
    def test_admitted_bodies_require_actual_owner_without_provisioning(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as raw:
            runtime=SimpleNamespace(runtime_dir=Path(raw),db_path=Path(raw)/'absent.sqlite3')
            for drain in (supplier._drain_supplier_preparation_intents,cny._drain_cny_preparation_intents,
                          fulfillment._drain_fulfillment_recalc_intents,nomenclature._drain_nomenclature_activation_intents):
                with self.assertRaises(RuntimeError): drain(runtime)
            service=DenseFbsService(db_path=runtime.db_path,runtime_dir=runtime.runtime_dir)
            with self.assertRaises(RuntimeError):
                service._activate_staged_skus(staged_items=[],orchestration_key='untrusted',request_identity='untrusted',actor='offline')
            entrypoint=RegistryUploadHttpEntrypoint.__new__(RegistryUploadHttpEntrypoint); entrypoint.runtime=runtime
            with self.assertRaises(RuntimeError): entrypoint._replay_ff_document_queue_admitted(stable_source_ids=[])
            assert not list(Path(raw).iterdir())

    def test_supplier_saved_pending_identity_and_fresh_owned_ack(self):
        with tempfile.TemporaryDirectory() as raw:
            rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
            with other_heavy(rt):
                source(rt)
                first = supplier_request(rt)
                result = supplier.resume_supplier_preparation(rt, 'source')
                assert result['operation_applied'] and result['durable_saved'] and result['deferred'], result
                assert result['preparation_revision'] == first['revision'] and result['source_revision'].endswith(first['source_fingerprint'])
                assert supplier_request(rt) == first and not supplier_queue(rt)
                # A second committed source changes the existing coalesced demand,
                # never resends its source or acknowledges the previous revision.
                source(rt, lines=[{**LINES[0], 'unit_price': 20, 'amount': 200}])
                latest = supplier_request(rt)
                assert latest['revision'] > first['revision'] and latest['status'] == 'pending'
                assert supplier.resume_supplier_preparation(rt,'source')['preparation_revision'] == latest['revision']
            result = fresh_owned(raw, 'supplier_preparation')
            assert result['status']=='queued', result
            current = supplier_request(rt)
            assert current['status']=='delivered' and current['revision']==latest['revision']
            queued = supplier_queue(rt)
            assert len(queued)==1 and queued[0]['source_revision'].startswith('supplier-preparation:'+str(latest['revision'])+':')
            assert admission_idle(rt.runtime_dir)['idle']

    def test_cny_commit_not_failed_busy_and_automatic_fresh_consumer(self):
        with tempfile.TemporaryDirectory() as raw:
            rt, ledger = cny_fixture(raw, payments=False)
            before = len(rt.list_cny_documents())
            with other_heavy(rt):
                result = ledger.create_opening_balance({'operation_date':'2026-07-05','cny_amount':10,'rub_value':100})
                assert len(rt.list_cny_documents())==before+1
                intent = cny.read_account_request(rt)
                assert intent['status']=='pending'
                outcome = ledger.replay_ledger(reason='busy-proof')
                assert outcome['operation_applied'] and outcome['durable_saved'] and outcome['deferred'], outcome
                assert outcome['durable_retry_identity']['account_revision']==intent['revision']
                assert cny.read_account_request(rt)==intent
            result = fresh_owned(raw,'cny_preparation')
            assert result['status']=='ok',result
            assert cny.read_account_request(rt)['status']=='delivered'
            assert len(rt.list_cny_documents())==before+1
            assert admission_idle(rt.runtime_dir)['idle']

    def test_nomenclature_staged_save_and_direct_publisher_cannot_bypass(self):
        with tempfile.TemporaryDirectory() as raw:
            rt = sku_fixture(Path(raw))
            with other_heavy(rt):
                saved = rt.save_nomenclature_item(_sku(101, updated_at=NOW))
                assert not saved['is_active'] and saved['activation_status']=='pending',saved
                pending = sources(rt)
                result = nomenclature.drain_nomenclature_activation_intents(rt)
                assert result['deferred'] and sources(rt)==pending
                # Direct publisher receives real busy before domain, intent,
                # document creation, or publication.
                service = DenseFbsService(db_path=rt.db_path,runtime_dir=rt.runtime_dir)
                with self.assertRaises(HeavyAdmissionBusy):
                    service.activate_staged_skus(staged_items=[{'item_id':pending[0]['item_id'],'nm_id':101,
                        'updated_at':NOW,'source_revision':pending[0]['revision']}],
                        orchestration_key='offline-direct',request_identity='offline-direct',actor='offline')
                assert sources(rt)==pending
            result=fresh_owned(raw,'nomenclature_activation')
            assert result['status']=='ok',result
            assert sources(rt)[0]['status']=='active' and sources(rt)[0]['revision']==pending[0]['revision']
            assert rt.load_nomenclature_item('dense-sku-101')['is_active']
            assert admission_idle(rt.runtime_dir)['idle']

    def test_fulfillment_and_ff_queue_preserved_without_scan_or_running_claim(self):
        with tempfile.TemporaryDirectory() as raw:
            rt, block = ff_fixture(raw)
            with other_heavy(rt):
                upload = block.upload_xlsx(_build_workbook([_valid_row('1001')]))
                before=ff_queue(rt)
                assert len(before)==1 and before[0]['status']=='queued'
                result=fulfillment.drain_fulfillment_recalc_intents(rt)
                assert result['deferred'] and ff_queue(rt)==before
                entrypoint=RegistryUploadHttpEntrypoint.__new__(RegistryUploadHttpEntrypoint)
                entrypoint.runtime=rt  # No domain blocks: any read/plan before admission would fail.
                result=entrypoint._replay_ff_document_queue(stable_source_ids=[before[0]['stable_source_id']])
                assert result['status']=='queued' and result['deferred'] and ff_queue(rt)==before,result
            result=fresh_owned(raw,'fulfillment')
            assert result['status']=='ok',result
            assert ff_queue(rt)==before  # Delivery is exact; full warehouse ack is its separate phase.
            assert block.list_uploads()['uploads'][0]['upload_id']==upload['upload']['upload_id']
            assert admission_idle(rt.runtime_dir)['idle']

    def test_supplier_exception_keeps_exact_intent_and_releases_before_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            rt=RegistryUploadDbBackedRuntime(runtime_dir=Path(raw)); source(rt)
            before=supplier_request(rt)
            def fail(phase, request):
                if phase=='after_preparation': raise RuntimeError('offline-after-preparation')
            result=supplier.drain_supplier_preparation_intents(rt,inject_failure=fail)
            assert result['status']=='pending' and not supplier_queue(rt)
            after=supplier_request(rt)
            assert after['revision']==before['revision'] and after['source_fingerprint']==before['source_fingerprint']
            assert after['status']=='error' and admission_idle(rt.runtime_dir)['idle']
            result=fresh_owned(raw,'supplier_preparation')
            assert result['status']=='queued' and supplier_request(rt)['revision']==before['revision']
            assert len(supplier_queue(rt))==1 and admission_idle(rt.runtime_dir)['idle']

    def test_invoice_source_writer_closed_and_old_revision_receipt_not_replaced(self):
        from packages.application import warehouse_functional_lock as domain
        from packages.application.supplier_shipment_invoice_revision import SupplierInvoiceRevisionAdapter, AUDIT
        from apps.supplier_invoice_revision_smoke import fixture as invoice_fixture
        for concurrent in (False,True):
            with tempfile.TemporaryDirectory() as raw:
                request,rt,_,_=invoice_fixture(Path(raw))
                adapter=SupplierInvoiceRevisionAdapter()
                preview=adapter.preview(request,'offline-revision')
                original=adapter.enqueue
                captured=[]
                def after_commit(root,before,after,now):
                    assert not getattr(domain._LOCAL,'warehouse_functional_locks',{}), 'invoice source writer not closed'
                    with _connect(rt.db_path) as conn:
                        old=dict(conn.execute(f'SELECT * FROM {supplier.TABLE} WHERE shipment_id=?',('sup_test',)).fetchone())
                    captured.append(old)
                    if concurrent:
                        saved=rt.load_supplier_shipment('sup_test')
                        rt.save_supplier_shipment(header={**saved['header'],'updated_at':'2026-10-01T00:00:00Z'},
                            lines=[{**row,'unit_price':float(row['unit_price'] or 0)+1} for row in saved['lines']])
                    return original(root,before,after,now)
                with other_heavy(rt),patch.object(adapter,'enqueue',side_effect=after_commit):
                    result=adapter.apply(request,'offline-revision',preview)
                    assert result['disposition']=='submitted' and result['queue']['status']=='pending',result
                    assert result['queue']['preparation_revision']==captured[0]['revision']
                    if concurrent: assert result['queue']['reason']=='source_revision_superseded'
                    else: assert result['queue']['deferred']
                    with _connect(rt.db_path) as conn:
                        audit=json.loads(conn.execute(f'SELECT queue_json FROM {AUDIT} WHERE operation_id=?',('offline-revision',)).fetchone()[0])
                    assert audit==result['queue']
                assert admission_idle(rt.runtime_dir)['idle']

    def test_nomenclature_source_writer_closed_before_admission_and_nested_reentry(self):
        from packages.application import warehouse_functional_lock as domain, business_data_heavy_admission as heavy
        with tempfile.TemporaryDirectory() as raw:
            rt=sku_fixture(Path(raw))
            original=heavy.heavy_admitted
            entered=[]
            @contextmanager
            def ordered(runtime_dir, **kwargs):
                held=getattr(domain._LOCAL,'warehouse_functional_locks',{})
                assert not held, 'source writer still held before new heavy admission'
                with original(runtime_dir,**kwargs) as lease:
                    entered.append(lease.operation)
                    yield lease
            # Dense reentry happens while its drain has the domain lock: only
            # the root acquisition must occur after closing the source writer.
            def outer_only(runtime_dir,**kwargs):
                if heavy.current_heavy_owner(runtime_dir) is not None:
                    return original(runtime_dir,**kwargs)
                return ordered(runtime_dir,**kwargs)
            with patch.object(heavy,'heavy_admitted',side_effect=outer_only):
                saved=rt.save_nomenclature_item(_sku(101,updated_at=NOW))
            assert saved['is_active'] and entered==['nomenclature-activation']
            assert admission_idle(rt.runtime_dir)['idle']

    def test_nomenclature_admission_initialization_failure_then_exact_restart_ack(self):
        from packages.application import business_data_heavy_admission as heavy
        with tempfile.TemporaryDirectory() as raw:
            rt=sku_fixture(Path(raw))
            with patch.object(heavy,'heavy_admitted',side_effect=RuntimeError('offline initialization failed')):
                saved=rt.save_nomenclature_item(_sku(101,updated_at=NOW))
            current=sources(rt)[0]
            assert current['status']=='pending' and not saved['is_active']
            assert saved['activation_source_revision']==current['revision']
            assert saved['activation_continuation_diagnostic']['attempted_source_revision']==current['revision']
            result=fresh_owned(raw,'nomenclature_activation')
            assert result['status']=='ok' and sources(rt)[0]['status']=='active',result
            assert sources(rt)[0]['revision']==current['revision']
            assert admission_idle(rt.runtime_dir)['idle']

    def test_nomenclature_runtime_error_reads_actual_newer_cancelled_revision(self):
        from packages.application import business_data_heavy_admission as heavy
        with tempfile.TemporaryDirectory() as raw:
            rt=sku_fixture(Path(raw))
            attempted=[]
            def fail(*args,**kwargs):
                attempted.append(sources(rt)[0]['revision'])
                rt.delete_nomenclature_item('dense-sku-101',updated_at=NOW)
                raise RuntimeError('offline initialization race')
            with patch.object(heavy,'heavy_admitted',side_effect=fail):
                saved=rt.save_nomenclature_item(_sku(101,updated_at=NOW))
            current=sources(rt)[0]
            assert current['status']=='cancelled' and current['revision']>attempted[0]
            assert saved['activation_status']=='cancelled' and not saved['is_active']
            assert saved['activation_source_revision']==current['revision']
            assert saved['activation_continuation_diagnostic']['attempted_source_revision']==attempted[0]
            assert fresh_owned(raw,'nomenclature_activation')['status']=='no_op'

    def test_nomenclature_runtime_error_does_not_claim_pending_after_actual_ack(self):
        from packages.application import business_data_heavy_admission as heavy
        with tempfile.TemporaryDirectory() as raw:
            rt=sku_fixture(Path(raw)); original=heavy.heavy_admitted
            @contextmanager
            def after_terminal(*args,**kwargs):
                with original(*args,**kwargs) as lease:
                    yield lease
                raise RuntimeError('offline lost terminal response')
            with patch.object(heavy,'heavy_admitted',side_effect=after_terminal):
                saved=rt.save_nomenclature_item(_sku(101,updated_at=NOW))
            current=sources(rt)[0]
            assert saved['is_active'] and saved['activation_status']=='active'
            assert current['status']=='active' and saved['activation_source_revision']==current['revision']
            assert saved['activation_continuation_diagnostic']['attempted_source_revision']==current['revision']
            assert admission_idle(rt.runtime_dir)['idle']


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='consume': consume(*sys.argv[2:])
    else: unittest.main()
