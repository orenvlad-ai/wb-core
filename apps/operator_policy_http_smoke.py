"""Real local policy HTTP acceptance/read/error and permission-before-read contracts."""
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime,timezone
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import json,sys,threading,socket
from http import HTTPStatus
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.warehouse_fbs_material_rematerialization_smoke import _seed,DAY,NOW
from apps.business_data_heavy_producers_smoke import busy
from packages.adapters import registry_upload_http_entrypoint as web
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.application import operator_policy as op

def _free_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]

def main():
    with TemporaryDirectory() as directory:
        runtime=_seed(Path(directory),mixed=False)
        entry=RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir,runtime=runtime,activated_at_factory=lambda:NOW,now_factory=lambda:datetime(2026,8,26,12,tzinfo=timezone.utc))
        config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=_free_port(),upload_path=web.DEFAULT_UPLOAD_PATH,sheet_plan_path=web.DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',sheet_status_path=web.DEFAULT_SHEET_STATUS_PATH,sheet_operator_ui_path=web.DEFAULT_SHEET_OPERATOR_UI_PATH,runtime_dir=runtime.runtime_dir)
        server=web.build_registry_upload_http_server(config,entrypoint=entry);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(config.port)
        def request(path,body=None,identity=None):
            headers={'Accept':'application/json'}
            if identity:headers['X-Operator-Request-ID']=identity
            req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
            try:
                with urlopen(req) as response:return response.status,json.load(response)
            except HTTPError as error:return error.code,json.load(error)
        try:
            payload={'effective_date':DAY,'buyout_rate':'1','tax_rate':'0.1'}
            code,preview=request(web.DEFAULT_CALCULATION_PARAMETERS_PREVIEW_PATH,payload);assert code==200
            payload['preview_fingerprint']=preview['preview_fingerprint'];identity='oppolicy_'+'b'*32;payload['_operator_request_id']=identity
            with busy(runtime.runtime_dir),patch.object(entry.calculation_parameters_block,'create_version',side_effect=AssertionError('HTTP native apply')):
                code,result=request(web.DEFAULT_CALCULATION_PARAMETERS_PATH,payload,identity);assert code==200,result
            receipt=result['acceptance'];assert receipt['actor']=='local_operator' and receipt['state']=='processing' and receipt['source_ref']['native_identity'] is None
            code,result=request(op.PATH+'legacy_proxy/'+identity);assert code==200 and result['operation']['operation_id']==identity
            with patch.object(web,'_current_web_user_actor',return_value='foreign'):
                code,result=request(op.PATH+'legacy_proxy/'+identity);assert code==404
            code,result=request(op.PATH+'wb_incident_policy/'+identity);assert code==404
            supply={'username':'supply','role':web.WEB_AUTH_ROLE_SUPPLY_OPERATOR,'allowed_sections':[web.WEB_AUTH_SECTION_SUPPLY]}
            settings={'username':'settings','role':web.WEB_AUTH_ROLE_OPERATOR,'allowed_sections':[web.WEB_AUTH_SECTION_SETTINGS]}
            assert web._user_can_access_path(supply,op.PATH+'wb_incident_policy/'+identity)
            assert not web._user_can_access_path(supply,op.PATH+'legacy_proxy/'+identity)
            assert web._user_can_access_path(settings,op.PATH+'legacy_proxy/'+identity)
            assert not web._user_can_access_path(settings,op.PATH+'wb_incident_policy/'+identity)
            with patch.object(entry,'handle_policy_operation_request',side_effect=AssertionError('read before permission')):
                def deny(handler,path):web._write_json_response(handler,HTTPStatus.FORBIDDEN,{'error':'forbidden'});return False
                with patch.object(web,'_ensure_operator_role',side_effect=deny):assert request(op.PATH+'legacy_proxy/'+identity)[0]==403
                with patch.object(web,'_ensure_supply_operator_role',side_effect=deny):assert request(op.PATH+'wb_incident_policy/'+identity)[0]==403
            code,result=request(web.DEFAULT_CALCULATION_PARAMETERS_PATH,{**payload,'tax_rate':'0.5'},identity);assert code==422 and 'source_not_saved' not in result,result
            code,result=request(web.DEFAULT_CALCULATION_PARAMETERS_PATH,payload,'oppolicy_'+'c'*32);assert code==422 and result['error']=='operator_policy_identity_alias_conflict'
            bad='oppolicy_'+'d'*32
            code,result=request(web.DEFAULT_WB_WAREHOUSE_EXCLUSION_SETTINGS_PATH,{'_operator_request_id':bad,'base_revision':0,'active':True,'excluded_wb_warehouse_ids':[-1],'reason':'bad','effective_from':DAY},bad)
            assert code==422 and result['source_not_saved'] is True and result['operation_id']==bad,result
            # Read-only GET constructs no services and does not initialize receipt schema.
            with patch('packages.application.calculation_parameters.CalculationParametersBlock.__init__',side_effect=AssertionError('GET constructor')):assert request(op.PATH+'legacy_proxy/'+'oppolicy_'+'e'*32)[0]==404
            # Timeout after durable accept reconciles only this exact same actor/body.
            saved=op.accept
            def lose(*args,**kwargs):saved(*args,**kwargs);raise RuntimeError('lost response after accepted')
            # Separate kind avoids an intentional pending-command business conflict.
            v4={'tax_rate':'0.1'};v4['preview_fingerprint']=entry.proxy_v4_parameters_block.preview_tax_version(v4)['preview_fingerprint'];late='oppolicy_'+'f'*32
            with patch.object(op,'accept',side_effect=lose):code,result=request(web.DEFAULT_PROXY_V4_PARAMETERS_PATH,{**v4,'_operator_request_id':late},late)
            assert code==200 and result['acceptance']['operation_id']==late,result
        finally:server.shutdown();server.server_close();thread.join(timeout=5)
    print('operator_policy_http_smoke: PASS busy/no HTTP apply/actor/permissions/exact GET/known-unsaved/alias/after-save readback')
if __name__=='__main__':main()
