"""Fresh import/render closure for the bounded FF, supplier, CNY and library/contracts release."""
from pathlib import Path
import ast,importlib,importlib.abc,inspect,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
FUTURE=frozenset('operator_policy operator_policy_history operator_nomenclature operator_compat_uploads operator_external_operations operator_feedback_operations operator_business_settings operator_autoanswers_settings operator_feedback_analysis_settings operator_feedback_complaint_schedules operator_cleaner_operations operator_facility_mappings operator_manual_ff_stock'.split())
ASSETS=frozenset('sheet_vitrina_v1_operator_policy.js sheet_vitrina_v1_facility_acceptance.js sheet_vitrina_v1_operator_nomenclature.js'.split())


class DenyFuture(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.rsplit('.',1)[-1] in FUTURE:raise AssertionError('uninstalled future import: '+fullname)
        return None


class Tests(unittest.TestCase):
    def test_fresh_imports_and_all_exposed_pages(self):
        guard=DenyFuture();sys.meta_path.insert(0,guard);self.addCleanup(sys.meta_path.remove,guard)
        modules=['packages.adapters.registry_upload_http_entrypoint','packages.application.registry_upload_http_entrypoint','packages.application.operator_operations','packages.application.operator_supplier_journal','packages.application.operator_cny_documents','packages.application.operator_trade_documents','packages.application.operator_supplier_contracts','packages.application.operator_supplier_history','packages.application.operator_supplier_history_candidate','packages.application.owned_history_worker','apps.web_vitrina_history_candidate_build','apps.web_vitrina_owned_history_worker']
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
        paths += [str(p.relative_to(ROOT)) for p in (ROOT/'packages/application').glob('operator_supplier*.py')]
        for relative in paths:
            text=(ROOT/relative).read_text();tree=ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom):
                    names={alias.name for alias in node.names}|{(node.module or '').rsplit('.',1)[-1]}
                    self.assertFalse(names.intersection(FUTURE),(relative,names.intersection(FUTURE)))
                elif isinstance(node,ast.Import):
                    self.assertFalse({alias.name.rsplit('.',1)[-1] for alias in node.names}.intersection(FUTURE),relative)
            for asset in ASSETS:self.assertNotIn(asset,text,relative)


if __name__=='__main__':unittest.main()
