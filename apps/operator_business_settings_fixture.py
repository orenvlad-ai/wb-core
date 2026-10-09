"""UI-only fixtures use actual native config CAS/proof; no provider operations.

The browser smokes keep unrelated prices/calculations mocked. Settings acceptance
comes from the real source transaction, not a fabricated green receipt. Actual
HTTP principal binding is tested separately by operator_business_settings_smoke.
"""
from tempfile import TemporaryDirectory
from pathlib import Path
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application import operator_business_settings as source,operator_operations as journal


class BusinessSettingsFixture:
    def __enter__(self):
        self.temp=TemporaryDirectory(prefix='business-settings-browser-')
        self.runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(self.temp.name))
        return self
    def __exit__(self,*args):self.temp.cleanup()
    def save(self,config,body,current):
        field=source.FIELDS[config];user='fixture-config-user'
        # Align prior mocked settings revisions (including the explicit simulated
        # concurrent conflict) through ordinary native source saves, not SQL edits.
        stored=self.runtime.load_sheet_vitrina_user_config(user_key=user,config_key=config)
        revision=int(stored.get('revision') or 0)
        while revision<int(current['revision']):
            saved=self.runtime.save_sheet_vitrina_user_config(user_key=user,config_key=config,schema_version=1,
                payload={field:current[field],'table':current['table']},expected_revision=revision,
                updated_at='2026-07-20T12:00:00Z')
            revision=saved['revision']
        command={'operation_id':body['operation_id'],'actor':'fixture-operator','seller_id':'fixture'} if body.get('operation_id') else None
        saved=self.runtime.save_sheet_vitrina_user_config(user_key=user,config_key=config,schema_version=1,
            payload={field:body[field],'table':body['table']},expected_revision=body['base_revision'],
            updated_at='2026-07-20T12:01:00Z',operator_command=command)
        if saved['status']!='ok':raise AssertionError(saved)
        result=dict(status='ok',revision=saved['revision'],operator_scope=user,**saved['config'])
        if saved.get('acceptance'):result['acceptance']=saved['acceptance']
        return result
    def read(self,identity):
        return journal.read_acceptance(self.runtime.db_path,identity,allowed_domains={source.DOMAIN},
            settings_scope=source.SettingsScope('fixture-operator','fixture-config-user','fixture',frozenset(source.FIELDS)))


from contextlib import contextmanager

@contextmanager
def native_server():
    """Only the native local JSON source and HTTP owner; no feedback/AI fixtures."""
    import threading
    from unittest.mock import patch
    from packages.adapters import registry_upload_http_entrypoint as web
    from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
    from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
    with TemporaryDirectory(prefix='business-settings-native-http-') as tmp, patch.dict('os.environ',{'WB_CORE_WEB_AUTH_USERNAME':'local_operator'}):
        runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        app=RegistryUploadHttpEntrypoint(runtime_dir=Path(tmp),runtime=runtime)
        config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=0,upload_path=web.DEFAULT_UPLOAD_PATH,
            sheet_plan_path=web.DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path=web.DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=web.DEFAULT_SHEET_STATUS_PATH,sheet_operator_ui_path=web.DEFAULT_SHEET_OPERATOR_UI_PATH,runtime_dir=Path(tmp))
        server=web.build_registry_upload_http_server(config,entrypoint=app)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:yield 'http://127.0.0.1:'+str(server.server_address[1]),app,None,None
        finally:server.shutdown();server.server_close();thread.join(timeout=5)
