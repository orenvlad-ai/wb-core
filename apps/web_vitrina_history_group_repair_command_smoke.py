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
        code=digest('reviewed-code')
        candidate=HistoryStore(migration/('group-repair-history-'+code[:16]))
        # Old-code prepared artifacts are not rewritten/reused after this fix.
        previous=migration/'group-repair-history';previous.mkdir()
        (previous/'old-prepared.json').write_text('{"old":true}')
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
        vector={'epoch':'captured-context','dates':{d:digest(d) for d in old['days']}}
        class Adapter:
            def __init__(self,**kwargs):
                assert kwargs['formula_epoch']=='old-formula'
                self.stats={'bytes':123,'queries':2}
                self.max_capture_seconds=20
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
        assert (previous/'old-prepared.json').read_text()=='{"old":true}'
        # Root pin is exact bytes at the fixed original migration path and
        # an existing immutable original candidate edition, not an input path.
        (migration/'history').symlink_to(source.root,target_is_directory=True)
        cache_sha=command.hashlib.sha256((migration/'proofs/source-proofs.json').read_bytes()).hexdigest()
        args.retained_cost_cache_sha256=cache_sha;args.original_candidate_edition=base
        retained_cache,original=command.read_retained_cache(args,contract,time.monotonic()+20)
        assert original==old and retained_cache['source'][3]=='old-formula'
        args.retained_cost_cache_sha256='a'*64
        try: command.read_retained_cache(args,contract,time.monotonic()+20)
        except ValueError as exc: assert 'cache_changed' in str(exc)
        else: raise AssertionError('unpinned original cache accepted')
        args.retained_cost_cache_sha256=cache_sha
        # A new codeproof isolates inputs; no old prepared files are overwritten.
        # Only retained binding is passed, even if fresh selector offers latest.
        args.expected_base=source._current()['current'];args.action='prepare'
        new_code=digest('retained-authority-code')
        new_candidate=HistoryStore(migration/('group-repair-history-'+new_code[:16]))
        day=DAYS[0];marker={'authority_mode':'retained_original_cost_binding'}
        def retained_basis(runtime,day,catalog,cells,*,accepted_binding,authority,deadline):
            assert accepted_binding=={'book_version':'original-covered'}
            assert authority['retained_cost_binding']==marker
            return {}, {'status':'fixture_retained'}
        with patch.object(command,'code_proof',return_value=new_code), \
             patch.object(command,'RegistryUploadDbBackedRuntime',return_value=SimpleNamespace(db_path=root/'db')), \
             patch.object(command,'StoreRegistry'),patch.object(command,'LiveNativeAdapter',Adapter), \
             patch.object(command,'window_read_context',return_value=nullcontext()), \
             patch.object(command,'captured_repair_cost_bindings',return_value={day:{'book_version':'latest'}}), \
             patch.object(command,'retained_repair_cost_bindings',return_value={day:({'book_version':'original-covered'},marker)}), \
             patch.object(command,'load_repair_cost_basis',side_effect=retained_basis), \
             patch.object(command,'prepare_group_repair_day',side_effect=prepare):
            result=command.prepare_inputs(args,contract,source,new_candidate,time.monotonic()+20)
            assert result['processed']==1
            pinned_index=_read(new_candidate.root/'repair-inputs/index.json')
            assert pinned_index['retained_cost_origin']==command.retained_claim(args)
            args.original_candidate_edition=source._current()['current']
            try: command.prepare_inputs(args,contract,source,new_candidate,time.monotonic()+20)
            except Exception as exc: assert 'resume_conflict' in str(exc)
            else: raise AssertionError('retained origin changed during resume')
        argv=['repair','--runtime-dir',str(root/'absent-runtime'),'--runtime-contract',str(root/'absent-contract'),
              '--expected-base',base,'--action','prepare','--maintenance-window-id','not-held','--manual']
        with patch.object(sys,'argv',argv),patch.object(command,'worker',side_effect=AssertionError('admission must stop before writes')):
            output=StringIO()
            with redirect_stdout(output): assert command.main()==0
            assert json.loads(output.getvalue())['status']=='skipped_maintenance'
        print(json.dumps({'status':'PASS','bounded_preparation_resume':True,'published_base_unchanged_until_commit':True,
            'same_prepared_inputs_to_preview_CAS':True,'ambiguous_publish_readback_only':True,
            'no_held_window_no_writes':True,'native_cost_authority_explicit':True,'interrupted_cache_copy_recoverable':True,
            'retained_fixed_root_byte_pin':True,'retained_origin_resume_binding':True,
            'retained_covered_not_latest':True,'new_code_isolated_candidate_old_inputs_preserved':True}))


if __name__=='__main__': main()
