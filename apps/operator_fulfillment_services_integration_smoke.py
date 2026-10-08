"""Disposable actual HTTP source identity, RO recovery and native completion."""
from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.operator_fulfillment_services_native_smoke import seed, publish_functional, publish_accounting, MOMENT, NOW
from apps.sheet_vitrina_v1_fulfillment_services_smoke import _build_workbook, _valid_row, _reserve_free_port
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.application import operator_fulfillment_services as receipts, operator_operations as operations
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.fulfillment_recalc_intents import drain_fulfillment_recalc_intents
from packages.application.warehouse_functional import WarehouseFunctionalBlock
from types import SimpleNamespace
from unittest.mock import Mock
from datetime import timedelta

UPLOADS = http.DEFAULT_FULFILLMENT_SERVICES_UPLOADS_PATH
JOURNAL = '/v1/sheet-vitrina-v1/operations'


def server_for(rt):
    entry = RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir, runtime=rt, activated_at_factory=lambda:NOW, now_factory=lambda:MOMENT)
    cfg = RegistryUploadHttpEntrypointConfig(host='127.0.0.1', port=_reserve_free_port(), runtime_dir=rt.runtime_dir,
        upload_path=http.DEFAULT_UPLOAD_PATH, sheet_plan_path=http.DEFAULT_SHEET_PLAN_PATH,
        sheet_refresh_path='/v1/sheet-vitrina-v1/refresh', sheet_status_path=http.DEFAULT_SHEET_STATUS_PATH,
        sheet_operator_ui_path=http.DEFAULT_SHEET_OPERATOR_UI_PATH)
    server = http.build_registry_upload_http_server(cfg,entrypoint=entry)
    thread = Thread(target=server.serve_forever,daemon=True);thread.start()
    return entry,server,thread,'http://127.0.0.1:'+str(cfg.port)


def stop(server,thread):
    server.shutdown();server.server_close();thread.join(timeout=5)


def request(base,path,*,method='GET',data=None,headers=None):
    try:
        response=urlopen(Request(base+path,method=method,data=data,headers=headers or {}),timeout=10)
    except HTTPError as exc: response=exc
    with closing(response):
        body=response.read()
        return response.status,json.loads(body) if body else None


def workbook_request(data,request_id):
    boundary='----fulfillment-identity-fixture'
    body=(f'--{boundary}\r\nContent-Disposition: form-data; name="request_id"\r\n\r\n{request_id}\r\n'
          f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fixture.xlsx"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode()+data+f'\r\n--{boundary}--\r\n'.encode()
    return body,{'Content-Type':'multipart/form-data; boundary='+boundary}


def test_owned_handler():
    """Bind the actual owned handler to saved upstream ports; native publishers
    and the completion verifier remain real, with no submitted evidence."""
    with TemporaryDirectory(prefix='ffsvc-owned-') as raw:
        rt,block=seed(raw)
        entry=RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir,runtime=rt,activated_at_factory=lambda:NOW,now_factory=lambda:MOMENT)
        data=_build_workbook([_valid_row('1001')])
        accepted=entry.handle_fulfillment_services_upload_request(data,request_id='owned-upload-source')['acceptance']
        finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT);finance.ensure_schema()
        entry.wb_finance_weekly_block=finance
        stamp=(MOMENT+timedelta(seconds=1)).isoformat()
        native=WarehouseFunctionalBlock(runtime=rt,timestamp_factory=lambda:stamp)
        def build():
            with receipts.readonly(rt.db_path) as conn:
                pending=[dict(row) for row in conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE status='queued'")]
            return native.build_targeted_recovery_plan(affected_nm_ids=[1],stable_source_ids=[row['stable_source_id'] for row in pending],targeted_recalc_requests=pending)
        entry.warehouse_functional_block=native
        # No live upstream collection in a disposable fixture. All local cost,
        # warehouse CAS, book/ready, Finance publication and proof are actual.
        entry.wb_supplies_block=SimpleNamespace(sync_functional_sources=Mock(return_value={'sync':{}}),
            collect_all_due_transit_costs=Mock(return_value={}),reconcile_functional_ff_state=lambda:drain_fulfillment_recalc_intents(rt))
        entry.calculation_parameters_block=SimpleNamespace(prepare_functional_economics_backup=lambda:{},
            process_pending_targeted_recalculations=lambda **kwargs:{'request_count':0},
            publish_current_functional_economics=lambda **kwargs:{'plan_fingerprint':'fixture-only-proxy-port'})
        entry.inventory_planning=SimpleNamespace(current=lambda:{})
        def refresh(*args,**kwargs):
            return {'status':'published','ready_obligation':'complete',**publish_accounting(rt,opening=True)}
        from packages.application.fbs_accounting_runtime import current_publication_receipt as native_publication_reader
        with patch.object(native,'build_sync_plan',side_effect=build), \
             patch('packages.application.fbs_accounting_runtime.current_publication_receipt',side_effect=lambda runtime,**kwargs:native_publication_reader(runtime,now=MOMENT)), \
             patch('packages.application.fbs_accounting_runtime.refresh',side_effect=refresh), \
             heavy_admitted(rt.runtime_dir,operation='actual-owned-handler'),warehouse_functional_job_lock(rt.runtime_dir) as owner:
            result=entry._handle_owned_warehouse_manual_sync_request(owner_token=owner['owner_token'])
        assert result['status']=='success',result
        assert result['fulfillment_operations']['status']=='ok',result['fulfillment_operations']
        assert receipts.read_acceptance(rt.db_path,accepted['operation_id'])['state']=='completed'
        print('actual owned handler: native intent consumer + local native functional/book/ready/Finance + exact completion: OK')


def main():
    with TemporaryDirectory(prefix='ffsvc-integration-') as raw:
        rt,_=seed(raw)
        entry,server,thread,base=server_for(rt)
        try:
            data=_build_workbook([_valid_row('1001')]);body,headers=workbook_request(data,'http-upload-identity')
            with patch.object(entry.our_wb_cost_block,'materialize_wb_supply_cost_layers',side_effect=AssertionError('heavy HTTP')), \
                 patch.object(receipts,'record_completion',side_effect=AssertionError('proof in HTTP')):
                status,value=request(base,UPLOADS,method='POST',data=body,headers=headers)
            assert status==200 and value['acceptance']['durable_saved'],value
            op=value['acceptance']['operation_id'];upload=value['upload']['upload_id']
            assert op!='http-upload-identity'
            # Simulate loss of all POST response fields: browser only knows its request.
            with patch.object(entry.fulfillment_services_block,'_ensure_service_schema',side_effect=AssertionError('GET bootstrap')):
                before=rt.db_path.read_bytes()
                status,recovered=request(base,UPLOADS+'?request_id=http-upload-identity')
                assert status==200 and recovered['request_id']=='http-upload-identity' and recovered['acceptance']['operation_id']==op
                assert recovered['payload_digest']==hashlib.sha256(data).hexdigest()
                assert request(base,UPLOADS)[0]==200
                assert request(base,UPLOADS+'/'+upload)[0]==200
                assert before==rt.db_path.read_bytes()
            assert request(base,UPLOADS,method='POST',data=body,headers=headers)[1]['acceptance']['operation_id']==op
            with receipts.readonly(rt.db_path) as conn:
                assert conn.execute('SELECT count(*) FROM '+receipts.TABLE).fetchone()[0]==1
            changed=_valid_row('1001');changed[1]='different'
            other_body,other_headers=workbook_request(_build_workbook([changed]),'http-upload-identity')
            assert request(base,UPLOADS,method='POST',data=other_body,headers=other_headers)[0]==400
            assert operations.journal(rt.db_path,allowed_sections=('reports',))['total']==0
            assert operations.read_acceptance(rt.db_path,op,allowed_sections=('reports',)) is None
            assert operations.journal(rt.db_path,domain=receipts.DOMAIN,search='fixture.xlsx')['total']==1
            user={'username':'reports-only','role':'operator','allowed_sections':['reports']}
            with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}), \
                 patch.object(http,'_authenticated_web_user',return_value=user):
                for path in (UPLOADS,UPLOADS+'?request_id=http-upload-identity'):
                    assert request(base,path)[0]==403,path
                assert request(base,JOURNAL+'?domain=fulfillment_services&search=fixture.xlsx')[1]['total']==0
                assert request(base,JOURNAL+'/'+op)[0]==404
                assert request(base,UPLOADS,method='POST',data=body,headers=headers)[0]==403
                assert request(base,UPLOADS+'/'+upload,method='DELETE')[0]==403
            stop(server,thread)
            entry,server,thread,base=server_for(rt)
            assert request(base,UPLOADS+'?request_id=http-upload-identity')[1]['acceptance']['operation_id']==op
            with heavy_admitted(rt.runtime_dir,operation='native-completion'),warehouse_functional_job_lock(rt.runtime_dir):
                assert receipts.reconcile(rt,now=MOMENT)['status']=='pending'
            publish_functional(rt);publish_accounting(rt,opening=True)
            finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT);finance.ensure_schema()
            with heavy_admitted(rt.runtime_dir,operation='native-completion'),warehouse_functional_job_lock(rt.runtime_dir):
                result=receipts.reconcile(rt,now=MOMENT)
                assert result['status']=='ok',result
            assert request(base,JOURNAL+'/'+op)[1]['operation']['state']=='completed'
            delete_path=UPLOADS+'/'+upload+'?request_id=http-delete-identity'
            status,deleted=request(base,delete_path,method='DELETE');assert status==200,deleted
            deletion=deleted['acceptance']['operation_id'];assert deletion!=op
            recovered=request(base,UPLOADS+'?request_id=http-delete-identity')[1]
            assert recovered['action']=='delete' and recovered['acceptance']['operation_id']==deletion
            assert request(base,delete_path,method='DELETE')[1]['acceptance']['operation_id']==deletion
            assert request(base,UPLOADS,method='POST',data=body,headers=headers)[1]['acceptance']['operation_id']==op # old alias cannot resurrect source
            new_body,new_headers=workbook_request(data,'http-upload-after-delete')
            recreated=request(base,UPLOADS,method='POST',data=new_body,headers=new_headers)[1]
            assert recreated['upload']['upload_id']!=upload and recreated['acceptance']['operation_id']!=op
            publish_functional(rt);publish_accounting(rt)
            with heavy_admitted(rt.runtime_dir,operation='native-coalesced'),warehouse_functional_job_lock(rt.runtime_dir):
                result=receipts.reconcile(rt,now=MOMENT)
            assert request(base,JOURNAL+'/'+recreated['acceptance']['operation_id'])[1]['operation']['state']=='completed',result
            from packages.application.business_data_write_barrier import acquire_barrier
            acquire_barrier(rt.runtime_dir,window_id='ffsvc-maintenance',window_kind='maintenance_pause',
                plan_fingerprint='sha256:'+'a'*64,approval_reference='offline-approval',actor='fixture',reason='HTTP guard')
            assert request(base,UPLOADS,method='POST',data=new_body,headers=new_headers)[0]==423
            assert request(base,UPLOADS+'/'+recreated['upload']['upload_id'],method='DELETE')[0]==423
            assert request(base,UPLOADS+'?request_id=http-upload-after-delete')[1]['acceptance']['state']=='completed'
            print('HTTP one-shot aliases, exact GET/restart, native completion, delete/reupload/coalesced, permissions before totals/details, maintenance: OK')
        finally: stop(server,thread)


if __name__=='__main__':
    main()
    test_owned_handler()
