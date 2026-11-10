"""Real bounded HTTP preview/confirm/read/download and mutation boundaries."""
from contextlib import closing
from http import HTTPStatus
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.fulfillment_payment_proof_smoke import fixture, upload, business_rows, TEXT
from apps.russian_payment_orders_smoke import _render_pdf
from apps.operator_fulfillment_services_integration_smoke import server_for, stop, request, UPLOADS
from packages.adapters import registry_upload_http_entrypoint as web

PDF = _render_pdf(TEXT, title='synthetic-ff-payment-proof', x_offset=0)
HEADERS = {'Content-Type':'application/pdf','X-WB-FF-Pool-CSRF':'1','X-Filename':'bank.pdf','X-Expected-Invoice-Number':'SYN-101'}


def run():
    with TemporaryDirectory(prefix='ff-payment-http-') as raw:
        rt, block = fixture(raw); uid = upload(block)
        entry, server, thread, base = server_for(rt)
        path = UPLOADS+'/'+uid+'/payment-proof'
        try:
            baseline = business_rows(rt)
            # Authorization and CSRF happen before PDF or confirm handlers.
            def deny(handler, path):
                web._write_json_response(handler, HTTPStatus.FORBIDDEN, {'error':'forbidden'})
                return False
            with patch.object(web, '_ensure_supply_operator_role', side_effect=deny), patch.object(entry, 'handle_fulfillment_payment_proof_preview_request', side_effect=AssertionError('before authorization')):
                assert request(base,path+'/preview',method='POST',data=PDF,headers=HEADERS)[0] == 403
                assert request(base,path)[0] == 403
                assert request(base,path+'.pdf')[0] == 403
            with patch.object(entry,'handle_fulfillment_payment_proof_preview_request',side_effect=AssertionError('before csrf')):
                assert request(base,path+'/preview',method='POST',data=PDF,headers={'Content-Type':'application/pdf'})[0] == 403
                assert request(base,path+'/preview',method='POST',data=PDF,headers={**HEADERS,'Origin':'https://foreign.example'})[0] == 403
            def maintenance(handler, target):
                web._write_json_response(handler, HTTPStatus.LOCKED, {'code':'business_data_maintenance'})
                return False
            with patch.object(web, '_ensure_business_data_write_allowed', side_effect=maintenance), patch.object(entry,'handle_fulfillment_payment_proof_preview_request',side_effect=AssertionError('before maintenance')):
                assert request(base,path+'/preview',method='POST',data=PDF,headers=HEADERS)[0] == 423
                assert request(base,path+'/confirm',method='POST',data=b'{}',headers={'Content-Type':'application/json','X-WB-FF-Pool-CSRF':'1'})[0] == 423
                assert request(base,path)[0] == 200  # GET remains read-only and available
            assert request(base,path+'/preview',method='POST',data=PDF,headers={**HEADERS,'Content-Length':str(16*1024**2+1)})[0] == 413
            assert request(base,path+'/preview',method='POST',data=PDF,headers={**HEADERS,'X-Expected-Invoice-Number':'WRONG'})[0] == 400
            status, preview = request(base,path+'/preview',method='POST',data=PDF,headers=HEADERS)
            assert status == 200 and preview['status'] == 'preview', preview
            assert preview['supplies'][0]['wb_supply_id'] == '1001'
            assert request(base,path)[1] == preview
            assert request(base,path+'.pdf')[0] == 404
            payload = {key:preview[key] for key in ('operation_id','preview_fingerprint')}
            status, confirmed = request(base,path+'/confirm',method='POST',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-WB-FF-Pool-CSRF':'1'})
            assert status == 200 and confirmed['status'] == 'confirmed', confirmed
            assert request(base,path+'?operation_id='+preview['operation_id'])[1] == confirmed
            with urlopen(base+path+'.pdf') as response:
                assert response.status == 200 and response.read() == PDF
                assert response.headers['Content-Type'] == 'application/pdf'
                assert response.headers['Cache-Control'] == 'private, no-store'
            assert business_rows(rt) == baseline
            assert block.list_uploads()['uploads'][0]['payment_proof']['operation_id'] == preview['operation_id']
            assert request(base,path+'/confirm',method='POST',data=json.dumps({**payload,'amount':'1'}).encode(),headers={'Content-Type':'application/json','X-WB-FF-Pool-CSRF':'1'})[0] == 400
            print('HTTP proof: real PDF parser, explicit confirmation, exact recovery/download, permissions+CSRF+maintenance+16MB boundary, no money writes: OK')
        finally:
            stop(server,thread)


if __name__ == '__main__': run()
