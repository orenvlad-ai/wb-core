"""Settings host rendered asset and existing source-specific HTTP grants/recovery."""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_facility_mappings_http_smoke import setup,request,server_for,stop,PATH
from apps.operator_facility_mappings_smoke import envelope
from packages.adapters import registry_upload_http_entrypoint as http


def main():
    with TemporaryDirectory(prefix='settings-facility-http-') as raw:
        rt,entry=setup(raw);server,thread,base=server_for(entry)
        try:
            with urlopen(base+http.DEFAULT_SETTINGS_UI_PATH+'?embedded=1') as response:page=response.read().decode()
            assert 'window.FacilitySourceAcceptance' in page and '<!-- FACILITY_ACCEPTANCE_ASSET -->' not in page
            assert 'warehouseSourceReceipt' in page and 'warehouseActiveInput' not in page
            _,preview=request(base,PATH+'/facilities/preview','POST',{'request_id':'settings-http-preview','name':'Settings HTTP','active':False})
            final=envelope(request_id='settings-http-final',confirm=True,preview_request_id=preview['request_id'],preview_fingerprint=preview['preview_fingerprint'])
            status,saved=request(base,PATH+'/facilities/onboarding/'+preview['request_id']+'/confirm','POST',final)
            assert status==200 and saved['status']=='accepted'
            alias=PATH+'/facility-operations?request_id='+final['request_id'];before=rt.db_path.read_bytes()
            with patch.object(entry.ff_pool_surface,'facility_detail',side_effect=AssertionError('GET source aggregate')),patch('packages.application.registry_upload_db_backed_runtime._connect',side_effect=AssertionError('GET writer')):
                assert request(base,alias)[1]==saved
            assert before==rt.db_path.read_bytes()
            for user in ({'username':'settings-only','role':'operator','allowed_sections':['settings']},{'username':'supplier','role':'supplier','allowed_sections':[]}):
                with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value=user),patch.object(entry,'handle_ff_facility_operation_read',side_effect=AssertionError('grant before source read')):
                    assert request(base,alias)[0]==403
                    assert request(base,PATH+'/facilities/onboarding/'+preview['request_id']+'/confirm','POST',final)[0]==403
            with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value={'username':'other','role':'operator','allowed_sections':['settings','supply']}):
                assert request(base,alias)[1]['status']=='unknown'
        finally:stop(server,thread)
    print('Settings HTTP rendered same06casset/query-only/source Supply grants/same principal: PASS')
if __name__=='__main__':main()
