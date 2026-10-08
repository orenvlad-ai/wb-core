"""Actual legacy POST refusal preserves source, previews and original grants."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.operator_manual_ff_stock_smoke import cutover, counts, source
from apps.operator_facility_mappings_http_smoke import setup, server_for, stop, request
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes


def upload(base, body):
    boundary='ManualBoundary'
    data=(f'--{boundary}\r\nContent-Disposition: form-data; name="operation_type"\r\n\r\nmanual_receipt\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="manual.xlsx"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode()+body+f'\r\n--{boundary}--\r\n'.encode()
    req=Request(base+http.DEFAULT_FF_STOCKS_PREVIEW_PATH,data=data,headers={'Content-Type':'multipart/form-data; boundary='+boundary},method='POST')
    try: reply=urlopen(req)
    except HTTPError as error: reply=error
    with reply: return reply.status,json.loads(reply.read())


def main():
    with TemporaryDirectory(prefix='manual-ff-http-') as raw:
        rt,entry=setup(raw);server,thread,base=server_for(entry)
        try:
            data=build_single_sheet_workbook_bytes('Manual',[['barcode','nmId','quantity'],['',101,5]])
            status,preview=upload(base,data);assert status==200 and preview['apply_allowed'],preview
            saved=source(rt,'http-history');cutover(rt);before=counts(rt)
            status,result=upload(base,data)
            assert status==409 and result['code']=='modern_workflow_required' and result['source_not_saved'] and 'acceptance' not in result,result
            status,result=request(base,http.DEFAULT_FF_STOCKS_CONFIRM_PATH,'POST',{'preview_id':preview['preview']['preview_id']})
            assert status==409 and result['code']=='modern_workflow_required',result
            assert counts(rt)==before
            assert request(base,http.DEFAULT_FF_STOCKS_PATH)[0]==200
            assert rt.load_ff_stock_operation(saved['operation_id'])['operation_id']==saved['operation_id']
            for user in ({'username':'cash','role':'operator','allowed_sections':['cash']}, {'username':'reports','role':'operator','allowed_sections':['reports']}, {'username':'supplier','role':'supplier','allowed_sections':[]}):
                with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value=user):
                    assert request(base,http.DEFAULT_FF_STOCKS_CONFIRM_PATH,'POST',{'preview_id':preview['preview']['preview_id']})[0]==403,user
            from packages.application.business_data_write_barrier import acquire_barrier
            acquire_barrier(rt.runtime_dir,window_id='manual-maintenance',window_kind='snapshot',plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic',actor='fixture',reason='synthetic maintenance')
            assert request(base,http.DEFAULT_FF_STOCKS_CONFIRM_PATH,'POST',{'preview_id':preview['preview']['preview_id']})[0]==423
            assert counts(rt)==before
        finally: stop(server,thread)
    print('manual FF actual HTTP: native 409 before preview/source, original history and grants, maintenance 423 retained: OK')

if __name__=='__main__':main()
