"""Durable exact native factual job admission/restart and real targeted apply."""
from contextlib import closing
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.supplier_confirmation_flows_smoke import _seed,_seed_empty_functional,NOW,SHIPMENT_ID
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime,_connect
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application import operator_supplier_factual_dates as facts,operator_supplier_shipments as source


def fixture(raw):
    runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
    _seed(runtime);_seed_empty_functional(runtime,NOW)
    entry=RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir,runtime=runtime,activated_at_factory=lambda:NOW)
    return runtime,entry


def main():
    with TemporaryDirectory(prefix='operator-factual-') as raw:
        runtime,entry=fixture(raw)
        preview=entry.handle_supplier_factual_dates_preview_request(SHIPMENT_ID,{'actual_shipment_date':'2026-06-26'})
        body={'request_id':'factual-confirm-fixture','confirmation_token':preview['confirmation_token']}
        original=runtime.load_supplier_shipment(SHIPMENT_ID)
        # HTTP admits native intent only; no thread, candidate or source apply.
        with patch.object(entry.operator_jobs,'start',side_effect=AssertionError('heavy HTTP thread')), \
             patch.object(entry.supplier_shipment_factual_correction_block,'dry_run',side_effect=AssertionError('heavy HTTP candidate')), \
             patch.object(runtime,'complete_supplier_confirmation_preview',side_effect=RuntimeError('lost auxiliary consumption')):
            accepted=entry.handle_supplier_factual_dates_confirm_request(SHIPMENT_ID,body,actor='alice',request_scope='alice-key')
        native_id=accepted['acceptance']['operation_id']
        assert accepted['status']=='accepted' and not accepted['acceptance']['physical_applied']
        assert accepted['acceptance']['primary_effect']=='request_saved'
        assert runtime.load_supplier_shipment(SHIPMENT_ID)==original
        restart=RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir,runtime=runtime,activated_at_factory=lambda:NOW)
        with patch.object(restart.supplier_shipment_factual_correction_block,'create_job',side_effect=AssertionError('duplicate job')):
            assert restart.handle_supplier_factual_dates_confirm_request(SHIPMENT_ID,body,actor='alice',request_scope='alice-key')['acceptance']['operation_id']==native_id
        before=runtime.db_path.read_bytes()
        with patch.object(facts,'ensure_schema',side_effect=AssertionError('GET bootstrap')):
            assert facts.read(runtime.db_path,body['request_id'],shipment_id=SHIPMENT_ID,request_scope='alice-key')['acceptance']['operation_id']==native_id
            assert facts.read(runtime.db_path,body['request_id'],shipment_id=SHIPMENT_ID,request_scope='foreign')['status']=='unknown'
            assert facts.read(runtime.db_path,body['request_id'],shipment_id='foreign-shipment',request_scope='alice-key')['status']=='unknown'
        assert runtime.db_path.read_bytes()==before
        consumed=facts.consume(runtime,block=restart.supplier_shipment_factual_correction_block)
        assert consumed['requests'][0]['status']=='success',consumed
        after=facts.read(runtime.db_path,body['request_id'],shipment_id=SHIPMENT_ID,request_scope='alice-key')
        assert after['acceptance']['physical_applied'] and after['acceptance']['state']=='processing'
        assert not after['acceptance']['processing']['complete']
        assert after['shipment']['actual_shipment_date']=='2026-06-26'
        assert runtime.load_supplier_shipment(SHIPMENT_ID)['header']['actual_shipment_date']=='2026-06-26'
        with closing(source.readonly(runtime.db_path)) as conn:
            intent=dict(conn.execute(f'SELECT * FROM {source.intents.TABLE} WHERE shipment_id=?',(SHIPMENT_ID,)).fetchone())
            assert source.intents._request_source_matches(conn,intent)
            proof=facts.applied_proof(conn,conn.execute(f'SELECT * FROM {facts.TABLE}').fetchone())
            assert proof['preparation_intent']['revision']==intent['revision']
        # Model process death after the atomic apply, before job status update.
        with _connect(runtime.db_path) as conn:
            conn.execute(f"UPDATE {facts.JOB} SET status='running' WHERE correction_id=?",(native_id,));conn.commit()
        with patch.object(restart.supplier_shipment_factual_correction_block,'run_job',side_effect=AssertionError('duplicate native apply')):
            assert facts.consume(runtime,block=restart.supplier_shipment_factual_correction_block)['requests'][0]['status']=='applied'
        assert facts.read(runtime.db_path,body['request_id'],shipment_id=SHIPMENT_ID,request_scope='alice-key')['acceptance']['processing']['native_state']=='success'
        assert facts.consume(runtime)['status']=='no_op'
    print('native factual job+alias atomic; same-ID lost preview/restart; query-only scoped GET; actual targeted apply+intent; crash retained proof no reapply; cost remains separate: OK')


if __name__=='__main__':main()
