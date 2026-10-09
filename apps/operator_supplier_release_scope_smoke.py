"""Fresh import/render closure for the bounded FF, supplier, CNY, library/contracts facilities SKU, current/historical policy, external and feedback projections and JSON business settings and native AI settings and analysis prompt and complaint schedule/manual and cleaner receipt projections and Balance and SPP jobs release."""
from pathlib import Path
import ast,importlib,importlib.abc,inspect,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
FUTURE=frozenset()
ASSETS=frozenset()


class DenyFuture(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.rsplit('.',1)[-1] in FUTURE:raise AssertionError('uninstalled future import: '+fullname)
        return None


class Tests(unittest.TestCase):
    def test_fresh_imports_and_all_exposed_pages(self):
        guard=DenyFuture();sys.meta_path.insert(0,guard);self.addCleanup(sys.meta_path.remove,guard)
        modules=['packages.adapters.registry_upload_http_entrypoint','packages.application.registry_upload_http_entrypoint','packages.application.operator_operations','packages.application.operator_supplier_journal','packages.application.operator_cny_documents','packages.application.operator_nomenclature','packages.application.operator_external_operations','packages.application.operator_balance_jobs','packages.application.operator_spp_jobs','packages.application.operator_business_settings','packages.application.operator_feedback_operations','packages.application.operator_autoanswers_settings','packages.application.operator_feedback_analysis_settings','packages.application.operator_cleaner_operations','packages.application.operator_feedback_complaint_schedules','packages.application.operator_complaint_runs','packages.application.operator_complaint_source_projection','packages.application.operator_compat_uploads','packages.application.operator_facility_mappings','packages.application.operator_manual_ff_stock','packages.application.operator_trade_documents','packages.application.operator_supplier_contracts','packages.application.operator_supplier_history','packages.application.operator_supplier_history_candidate','packages.application.operator_policy','packages.application.operator_policy_history','packages.application.owned_history_worker','apps.web_vitrina_history_candidate_build','apps.web_vitrina_owned_history_worker']
        for name in modules:importlib.import_module(name)
        from packages.adapters import registry_upload_http_entrypoint as adapter
        page_names=['_render_sheet_vitrina_supplier_ui','_render_sheet_vitrina_supplier_safe_ui','_render_sheet_vitrina_web_vitrina_ui','_render_sheet_vitrina_settings_ui','_render_sheet_vitrina_operator_ui']
        for name in page_names:
            function=getattr(adapter,name)
            required={key:'/synthetic/'+key for key,param in inspect.signature(function).parameters.items() if param.default is inspect.Parameter.empty}
            html=function(**required)
            self.assertIn('<',html,name)
            for asset in ASSETS:self.assertNotIn(asset,html,name)
        self.assertFalse(FUTURE.intersection(name.rsplit('.',1)[-1] for name in sys.modules))

    def test_static_installed_source_imports_and_assets(self):
        paths=['packages/adapters/registry_upload_http_entrypoint.py','packages/application/registry_upload_http_entrypoint.py','packages/application/operator_operations.py','apps/web_vitrina_owned_history_worker.py','packages/application/owned_history_worker.py']
        paths += ['packages/application/operator_business_settings.py','packages/application/operator_external_operations.py']
        paths += [str(p.relative_to(ROOT)) for p in (ROOT/'packages/application').glob('operator_supplier*.py')]
        paths += [str(p.relative_to(ROOT)) for p in (ROOT/'packages/application').glob('operator_policy*.py')]
        for relative in paths:
            text=(ROOT/relative).read_text();tree=ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom):
                    names={alias.name for alias in node.names}|{(node.module or '').rsplit('.',1)[-1]}
                    self.assertFalse(names.intersection(FUTURE),(relative,names.intersection(FUTURE)))
                elif isinstance(node,ast.Import):
                    self.assertFalse({alias.name.rsplit('.',1)[-1] for alias in node.names}.intersection(FUTURE),relative)
            for asset in ASSETS:self.assertNotIn(asset,text,relative)

    def test_actual_governed_hashes_epoch_routes_and_neutral_authority(self):
        import hashlib,json
        from packages.application import operator_policy_history as policy, historical_dated_inputs as neutral
        from apps.registry_upload_http_entrypoint_hosted_runtime import _validated_public_routes
        contract=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json').read_text())
        hashes=contract['formula_code_hashes']
        for relative, expected in hashes.items():
            self.assertEqual(hashlib.sha256((ROOT/relative).read_bytes()).hexdigest(),expected,relative)
        epoch='wbc0069k16-reviewed-native-v1:'+hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(epoch,contract['formula_epoch'])
        self.assertIn(epoch,(ROOT/'artifacts/registry_upload_http_entrypoint/systemd/wb-core-web-vitrina-finished-snapshot.service').read_text())
        for name in ('selected','dated_slice','book_lineage'):self.assertIs(getattr(policy,name),getattr(neutral,name))
        self.assertEqual(policy.code_authority()['historical_dated_inputs.py'],hashlib.sha256(Path(neutral.__file__).read_bytes()).hexdigest())
        routes=_validated_public_routes(json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json').read_text()))
        policy_routes=[r for r in routes if '/policy-operations/' in r['path']]
        self.assertEqual({r['path'] for r in policy_routes},{'/v1/sheet-vitrina-v1/settings/policy-operations/'+k+'/' for k in ('legacy_proxy','proxy_v4_tax','wb_incident_policy')})
        self.assertTrue(all(r['methods']==['GET'] for r in policy_routes))


if __name__=='__main__':unittest.main()
