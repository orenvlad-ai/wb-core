"""Local command integration: guarded prepared inputs, resume, publication readback."""
from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import web_vitrina_history_group_repair as command
from apps.web_vitrina_history_group_repair_smoke import fixture, GROUP, DAYS
from packages.application.web_vitrina_history_compiler import digest
from packages.application.web_vitrina_history_store import HistoryStore, _atomic, _read
from packages.application.web_vitrina_group_repair_inputs import SCHEMA


def main():
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        source,base,old=fixture(root)
        migration=root/'candidate';(migration/'proofs').mkdir(parents=True)
        _atomic(migration/'proofs/source-proofs.json',{'source':['fixture',1,2,'old-formula']})
        candidate=HistoryStore(migration/'group-repair-history')
        interrupted=root/'interrupted-cache';interrupted.mkdir()
        with patch.object(command.os,'replace',side_effect=OSError('interrupted cache copy')):
            try: command.warm_cache(migration/'proofs',interrupted,candidate,time.monotonic()+20)
            except OSError: pass
            else: raise AssertionError('copy failure was not injected')
        assert not (interrupted/'source-proofs.json').exists()
        command.warm_cache(migration/'proofs',interrupted,candidate,time.monotonic()+20)
        assert _read(interrupted/'source-proofs.json')['source'][3]=='old-formula'
        contract={'candidate_root':str(root/'serving'), 'group_migration_root':str(migration)}
        # Serving path uses the real immutable source fixture.
        (root/'serving').mkdir();(root/'serving/history').symlink_to(source.root,target_is_directory=True)
        args=SimpleNamespace(expected_base=base,max_days=1,runtime_dir=root/'runtime',action='prepare',
                             expected_candidate='',preview_token='')
        code=digest('reviewed-code')
        vector={'epoch':'captured-context','dates':{d:digest(d) for d in old['days']}}
        class Adapter:
            def __init__(self,**kwargs):
                assert kwargs['formula_epoch']=='old-formula'
                self.stats={'bytes':123,'queries':2}
            def capture(self): return deepcopy(vector)
        def prepare(day,catalog,cells,**kwargs):
            result=deepcopy(cells[GROUP[0]]);result[:2]=[30,'30']
            return {'schema':SCHEMA,'day':day,'patch':{GROUP[0]:result},'unresolved':{},
                'auxiliary_digests':{'dated_catalog':digest(catalog),'stored_day_cells':digest(cells)}}
        def basis(runtime,day,catalog,cells,*,accepted_binding,authority,deadline):
            assert authority['old_token']==HistoryStore.day_proofs(old)[day]['token']
            assert authority['fresh_token']==vector['dates'][day]
            return {},{'status':'unresolved_source_authority'}
        with patch.object(command,'code_proof',return_value=code), \
             patch.object(command,'RegistryUploadDbBackedRuntime',return_value=SimpleNamespace(db_path=root/'db')), \
             patch.object(command,'StoreRegistry'),patch.object(command,'LiveNativeAdapter',Adapter), \
             patch.object(command,'window_read_context',return_value=nullcontext()), \
             patch.object(command,'captured_repair_cost_bindings',return_value={}), \
             patch.object(command,'load_repair_cost_basis',side_effect=basis), \
             patch.object(command,'prepare_group_repair_day',side_effect=prepare):
            for i in range(len(DAYS)):
                result=command.prepare_inputs(args,contract,source,candidate,time.monotonic()+20)
                assert result['completed']==i+1
            assert result['status']=='inputs_ready'
            assert source._current()['current']==base and candidate._current() is None
            args.action='build'
            for i in range(len(DAYS)):
                result=command.worker(args,contract,migration,time.monotonic()+20)
                assert result['completed']==i+1
            assert result['status']=='ready'
            args.expected_candidate=result['edition_id'];args.action='preview'
            preview=command.worker(args,contract,migration,time.monotonic()+20)
            assert preview['changed_group_cells']==len(DAYS)
            args.action='publish';args.preview_token=preview['preview_token']
            result=command.worker(args,contract,migration,time.monotonic()+20)
            assert result['status']=='published'
            with patch.object(command,'preview_group_repair',side_effect=AssertionError('readback must not preview/submit again')):
                assert command.worker(args,contract,migration,time.monotonic()+20)['status']=='already_published'
        argv=['repair','--runtime-dir',str(root/'absent-runtime'),'--runtime-contract',str(root/'absent-contract'),
              '--expected-base',base,'--action','prepare','--maintenance-window-id','not-held','--manual']
        with patch.object(sys,'argv',argv),patch.object(command,'worker',side_effect=AssertionError('admission must stop before writes')):
            output=StringIO()
            with redirect_stdout(output): assert command.main()==0
            assert json.loads(output.getvalue())['status']=='skipped_maintenance'
        print(json.dumps({'status':'PASS','bounded_preparation_resume':True,'published_base_unchanged_until_commit':True,
            'same_prepared_inputs_to_preview_CAS':True,'ambiguous_publish_readback_only':True,
            'no_held_window_no_writes':True,'native_cost_authority_explicit':True,'interrupted_cache_copy_recoverable':True}))


if __name__=='__main__': main()
