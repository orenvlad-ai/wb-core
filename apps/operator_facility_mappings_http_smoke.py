"""Actual directory HTTP: exact accepted native source and source grants."""
import json,sqlite3,sys
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_facility_mappings_smoke import fixture,envelope,NOW
from apps.operator_supplier_shipments_http_smoke import server_for,stop
from apps.wb_fbs_warehouse_registry_smoke import FakeSource,CatalogSource
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.wb_fbs_warehouse_registry import WbFbsWarehouseRegistry
from packages.application.ff_pool_dense_fbs import DenseFbsService
from packages.application.ff_pool_foundation import FACILITY_CHANGES_TABLE,BALANCES_TABLE
from packages.application.wb_fbs_orders import WAREHOUSE_MAPPINGS_TABLE
from packages.application import operator_facility_mappings as op
from packages.adapters import registry_upload_http_entrypoint as http
PATH=http.DEFAULT_FF_POOL_PATH


def request(base,path,method='GET',payload=None):
    from urllib.request import Request,urlopen
    from urllib.error import HTTPError
    from contextlib import closing
    headers={'Content-Type':'application/json','Accept':'application/json','X-WB-FF-Pool-CSRF':'1'}
    if payload and payload.get('request_id'):headers['X-Request-ID']=payload['request_id']
    try:response=urlopen(Request(base+path,method=method,data=json.dumps(payload).encode() if payload is not None else None,headers=headers),timeout=15)
    except HTTPError as exc:response=exc
    with closing(response):return response.status,json.loads(response.read())


def setup(raw):
    rt,surface=fixture(Path(raw)/'runtime')
    entry=RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir,runtime=rt)
    entry.ff_pool_surface=surface
    ticks=iter(f'2026-08-26T08:00:{n:02d}Z' for n in range(60))
    registry=WbFbsWarehouseRegistry(db_path=rt.db_path,runtime_dir=rt.runtime_dir,source=FakeSource(),catalog_source=CatalogSource(),writer_enabled=True,timestamp_factory=lambda:next(ticks))
    registry.collect();entry.wb_fbs_warehouse_registry=registry
    return rt,entry


def main():
    with TemporaryDirectory(prefix='facility-http-') as raw:
        rt,entry=setup(raw);server,thread,base=server_for(entry)
        try:
            status,preview=request(base,PATH+'/facilities/preview','POST',{'request_id':'facility-http-preview','name':'Facility HTTP','city':'City','active':False})
            assert status==200 and 'acceptance' not in preview,(status,preview)
            body=envelope(request_id='facility-http-create',confirm=True,preview_request_id=preview['request_id'],preview_fingerprint=preview['preview_fingerprint'])
            route=PATH+'/facilities/onboarding/'+preview['request_id']+'/confirm'
            with patch.object(entry.ff_pool_surface,'facility_detail',side_effect=AssertionError('post-save source aggregate')):
                with ThreadPoolExecutor(max_workers=3) as pool:responses=list(pool.map(lambda _:request(base,route,'POST',body),range(3)))
            assert all(s==200 and v['status']=='accepted' for s,v in responses),responses
            saved=responses[0][1];fid=saved['acceptance']['source_ref']['entity_id'];alias=PATH+'/facility-operations?request_id='+body['request_id']
            assert len({v['acceptance']['operation_id'] for _,v in responses})==1
            before=rt.db_path.read_bytes()
            with patch('packages.application.registry_upload_db_backed_runtime._connect',side_effect=AssertionError('GET writer')),patch('packages.application.ff_pool_foundation.ensure_ff_pool_foundation_schema',side_effect=AssertionError('GET bootstrap')):
                assert request(base,alias)[1]==saved
                assert request(base,PATH+'/facility-operations?operation_id='+saved['acceptance']['operation_id'])[1]==saved
            assert before==rt.db_path.read_bytes()
            detail=entry.ff_pool_surface.facility_detail(fid)['facility']
            active=envelope(request_id='facility-http-active',active=True,expected_updated_at=detail['updated_at'],expected_source_digest=detail['operator_source_digest'])
            with patch.object(DenseFbsService,'_materialize',side_effect=AssertionError('heavy HTTP')),patch.object(entry.ff_pool_surface,'facility_detail',side_effect=AssertionError('post-save source aggregate')):
                status,pending=request(base,PATH+'/facilities/'+fid,'POST',active)
            assert status==200 and pending['acceptance']['state']=='processing',pending
            assert not entry.ff_pool_surface.facility_detail(fid)['facility']['active']
            assert request(base,PATH+'/facilities/'+fid,'POST',active)[1]==pending
            service=DenseFbsService(db_path=rt.db_path,runtime_dir=rt.runtime_dir,timestamp_factory=lambda:NOW)
            assert service.drain_facility_activations()['active']==1
            assert request(base,PATH+'/facility-operations?request_id='+active['request_id'])[1]['acceptance']['processing']['published_active']
            # Binding confirmation retains exact official preview, mapping and receipt in one source transaction.
            current=entry.ff_pool_surface.facility_detail(fid)['facility']
            status,binding=request(base,PATH+'/wb-warehouses/binding/preview','POST',{'request_id':'binding-http-preview','seller_warehouse_id':7001,'facility_id':fid})
            assert status==200 and 'acceptance' not in binding
            physical_before=None
            with sqlite3.connect(rt.db_path) as conn:physical_before=conn.execute(f'SELECT * FROM {BALANCES_TABLE}').fetchall()
            bound=envelope(request_id='binding-http-final',confirm=True,preview_request_id=binding['request_id'],preview_fingerprint=binding['preview_fingerprint'],facility_id=fid)
            status,result=request(base,PATH+'/wb-warehouses/binding/'+binding['request_id']+'/confirm','POST',bound)
            assert status==200 and result['status']=='accepted' and result['acceptance']['processing']['kind']=='source_only',result
            with sqlite3.connect(rt.db_path) as conn:
                assert conn.execute(f'SELECT * FROM {BALANCES_TABLE}').fetchall()==physical_before
                assert conn.execute(f'SELECT COUNT(*) FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE active=1').fetchone()[0]==1
            assert request(base,PATH+'/facility-operations?request_id='+bound['request_id'])[1]==result
            assert request(base,PATH+'/wb-warehouses/binding/'+binding['request_id']+'/confirm','POST',bound)[1]==result
            # Same timestamp metadata drift is a retained exact refusal, not a new link.
            p=request(base,PATH+'/wb-warehouses/binding/preview','POST',{'request_id':'binding-http-second','seller_warehouse_id':7002,'facility_id':fid})
            assert p[0]==409 # native one-to-one remains authoritative
            stale=envelope(request_id='facility-http-stale',name='Not saved',expected_updated_at=detail['updated_at'],expected_source_digest=detail['operator_source_digest'])
            status,rejected=request(base,PATH+'/facilities/'+fid,'POST',stale)
            assert status==200 and rejected['status']=='rejected' and not rejected['acceptance']
            assert request(base,PATH+'/facility-operations?request_id='+stale['request_id'])[1]==rejected
            for user in ({'username':'reports','role':'operator','allowed_sections':['reports']},{'username':'cash','role':'operator','allowed_sections':['cash']},{'username':'supplier','role':'supplier','allowed_sections':[]}):
                with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value=user),patch.object(entry,'handle_ff_facility_operation_read',side_effect=AssertionError('grant after read')):
                    assert request(base,alias)[0]==403,user
                    assert request(base,PATH+'/facilities/'+fid,'POST',active)[0]==403,user
            with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value={'username':'foreign','role':'operator','allowed_sections':['supply']}):
                assert request(base,alias)[1]['status']=='unknown'
            from packages.application.business_data_write_barrier import acquire_barrier
            acquire_barrier(rt.runtime_dir,window_id='facility-maintenance',window_kind='snapshot',plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic',actor='fixture',reason='synthetic maintenance')
            before=rt.db_path.read_bytes()
            assert request(base,PATH+'/facilities/'+fid,'POST',envelope(request_id='facility-http-blocked',name='Blocked',expected_updated_at=current['updated_at']))[0]==423
            assert request(base,PATH+'/facility-operations?request_id=facility-http-blocked')[1]['status']=='unknown'
            assert before==rt.db_path.read_bytes()
        finally:stop(server,thread)
    print('Actual native facility/binding HTTP: concurrent one source, exact query-only same ID, staged/active native proof, original one-to-one + source grants, retained refusal and maintenance: OK')
if __name__=='__main__':main()
