"""Exact Chrome-guard continuation, drift refusal and consumed-claim tests."""
from contextlib import ExitStack
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ci import pr1623_dependency_continuation as c
from ci import post_merge_release_recovery as r
from ci.post_merge_release_recovery_smoke import CommentsClient


def failure_log():
    return ('line 1242, in deploy_current_checkout\nrun_stage("dependencies", buyer_chrome_install_command)\n'
        'line 151, in install\nraise RuntimeError("insufficient RAM for Chrome staging")\n'
        'RuntimeError: insufficient RAM for Chrome staging\npython3 apps/wb_buyer_chrome_runtime.py\n'
        +c.prior.PROFILE['operation_id']+' --expected-sha '+c.MERGE+
        "\nsubprocess.CalledProcessError: Command ['ssh'] returned non-zero exit status 1.").encode()


class Tests(unittest.TestCase):
    def test_exact_failed_stage(self):
        raw=failure_log(); self.assertEqual(len(c.prove_failure(raw)),64)
        for old,new in [(b'1242',b'1243'),(b'151',b'169'),(b'staging',b'runtime'),
                        (b'exit status 1',b'exit status 255'),(c.MERGE.encode(),b'other')]:
            with self.subTest(new=new),self.assertRaises(r.RecoveryError): c.prove_failure(raw.replace(old,new))
        with self.assertRaises(r.RecoveryError):
            c.prove_failure(raw+b'\nsubprocess.CalledProcessError: returned non-zero exit status 1.')

    def test_parent_immutable_chain_and_claim(self):
        original=dict(c.prior.PROFILE)
        receipt=dict(schema=r.RECOVERY_SCHEMA,state='ambiguous',operation_id=c.PARENT,
            preview_fingerprint=c.FINGERPRINT,reason='CalledProcessError',
            recovery='readback-only; never repeat canonical deploy',source=original)
        run=dict(name='Post-merge Release Recovery',path='.github/workflows/release-recovery.yml',
            event='workflow_dispatch',run_attempt=1,status='completed',conclusion='failure',head_sha=c.RUNNER)
        claim=dict(state='claimed',operation_id=c.PARENT,source=original,preview_fingerprint=c.FINGERPRINT,
            finance_acceptance={'write_mode':'readonly'})
        class Client:
            repository=r.REPOSITORY
            def get(self,path):
                if 'artifacts?' in path: return {'artifacts':[dict(id=1,name=f'release-recovery-{c.prior.RUN}-{c.FAILED_RUN}',expired=False)]}
                if 'jobs?' in path: return {'jobs':[dict(id=2,name='Proven post-merge deploy-tail recovery',status='completed',conclusion='failure',steps=[dict(name='Validate evidence and preview or continue only the proven tail',conclusion='failure')])]}
                return run
            def request(self,method,path,raw=False):
                if path.endswith('/logs'): return failure_log()
                stream=io.BytesIO()
                with zipfile.ZipFile(stream,'w') as z:z.writestr('recovery-receipt.json',json.dumps(receipt))
                return stream.getvalue()
        with patch.object(c.prior,'evidence',return_value={'original_receipt':original}),patch.object(r,'_matching_comments',return_value=[claim]):
            self.assertEqual(c.parent_evidence(Client())['source'],original)
            for obj,key,value in [(run,'run_attempt',2),(run,'head_sha','other'),(run,'conclusion','success'),
                    (receipt,'reason','unknown'),(receipt,'preview_fingerprint','other'),(claim,'source',{}),
                    (claim,'finance_acceptance',None)]:
                old=obj[key];obj[key]=value
                with self.subTest(key=key),self.assertRaises(r.RecoveryError):c.parent_evidence(Client())
                obj[key]=old

    def fixture(self,stack):
        self.client=CommentsClient();self.target=SimpleNamespace(target_id='wb_core_eu_hosted_runtime_active')
        self.parent=dict(source=dict(c.prior.PROFILE),finance={'write_mode':'readonly'},receipt_sha256='x',job_log_sha256='y')
        self.runner={'trusted_main_sha':'f'*40}
        self.state=dict(metadata_sha256='old',main_pid=123,pilot_pid=456,finance_pilot=self.parent['finance'],
            owner={'owner':{'phase':'mutation_started'},'window':c.prior.WINDOW,'baseline':c.prior.BASELINE},
            started_owner={'proof':'exact'},dependency_closure={'dependency_versions':{'npm_modules':{'installed':True,'versions':'8.17.1 3.0.1'}}})
        self.complete=False;self.invocations=[];self.fail=''
        def mock(obj,name,**kw):return stack.enter_context(patch.object(obj,name,**kw))
        mock(c,'parent_evidence',return_value=self.parent)
        self.main=mock(r,'prove_repo_only_descendant',return_value=self.runner)
        def collect(t,p,complete=False):
            if complete and not self.complete:raise r.RecoveryError('not complete')
            return deepcopy(self.state)
        self.collect=mock(c,'collect',side_effect=collect)
        mock(c,'source_proof',return_value={'guard':'exact'})
        mock(c.prior,'collect_owner',return_value={})
        mock(c.prior,'storage_preflight',return_value={})
        self.finish=mock(r,'_finish_deploy_protection',return_value={})
        def readback(*args,require_incomplete,**kw):
            if not require_incomplete and not self.complete:raise r.RecoveryError('not complete')
            return dict(finance_pilot=self.parent['finance'])
        mock(r,'collect_prestate',side_effect=readback)
        names=('auth','storage','barrier','prepare','install','daemon_reload','nginx','restart','reconcile','storage_readback','status')
        commands=dict(normal_activation_tail={n:['ssh',n] for n in names},activation=['ssh','activation'],
            cleaner=['ssh','cleaner'],npm=['ssh','npm'],completion=['ssh','completion'],completion_input='CAS')
        mock(c,'commands',return_value=commands)
        mock(r,'_protect_deploy_command',side_effect=lambda t,s,command:command)
        def stage(name,command,**kwargs):
            self.invocations.append(name)
            if name==self.fail:raise r.RecoveryError('injected-'+name)
            if name=='restart':self.state['main_pid']=124
            if name=='metadata-complete':self.complete=True
            if name=='locked-npm-dependencies':self.state['dependency_closure']['dependency_versions']['npm_modules']={'installed':True,'versions':'8.17.1 3.0.1'}
            return {'stage':name}
        mock(r,'_must_succeed',side_effect=stage)
        mock(r,'_bounded_status_readback',side_effect=lambda command:stage('managed-service-status',command))
        return c.build_preview(self.client,self.target)

    def test_full_tail_and_existing_claim_only_readback(self):
        with ExitStack() as stack:
            p=self.fixture(stack)
            result=c.run(self.client,self.target,'apply',p['preview_fingerprint'])
            self.assertEqual(result['state'],'complete')
            self.assertLess(self.invocations.index('cleaner-before-complete'),self.invocations.index('metadata-complete'))
            self.assertNotIn('sync',self.invocations)
            before=list(self.invocations)
            self.assertEqual(c.run(self.client,self.target,'apply',p['preview_fingerprint'])['state'],'complete')
            self.assertEqual(self.invocations,before)

    def test_failed_cleaner_or_restart_cannot_replay_or_complete(self):
        for phase in ('restart','cleaner-before-complete','autoanswers-prepare-deploy'):
            with self.subTest(phase=phase),ExitStack() as stack:
                p=self.fixture(stack);self.fail=phase
                self.assertEqual(c.run(self.client,self.target,'apply',p['preview_fingerprint'])['state'],'ambiguous')
                self.assertFalse(self.complete);self.finish.assert_not_called()
                before=list(self.invocations);self.fail=''
                self.assertEqual(c.run(self.client,self.target,'apply',p['preview_fingerprint'])['state'],'ambiguous')
                self.assertEqual(self.invocations,before)

    def test_stale_preview_and_main_or_dependency_drift_refuse(self):
        with ExitStack() as stack:
            p=self.fixture(stack);self.state['dependency_closure']['changed']=True
            with self.assertRaises(r.RecoveryError):c.run(self.client,self.target,'apply',p['preview_fingerprint'])
            self.assertFalse(self.client.values);self.assertFalse(self.invocations)
        with ExitStack() as stack:
            p=self.fixture(stack)
            real=r._publish_once
            def drift(*args):
                real(*args)
                if args[-1].get('state')=='claimed':self.main.return_value={'trusted_main_sha':'e'*40}
            stack.enter_context(patch.object(r,'_publish_once',side_effect=drift))
            self.assertEqual(c.run(self.client,self.target,'apply',p['preview_fingerprint'])['state'],'ambiguous')
            self.assertFalse(self.invocations)

    def test_only_absent_locked_node_closure_installs_once(self):
        with ExitStack() as stack:
            self.fixture(stack)
            self.state['dependency_closure']['dependency_versions']['npm_modules']={'installed':False,'versions':None}
            p=c.build_preview(self.client,self.target)
            self.assertIn('absent-locked-npm-dependencies-once-and-readback',p['stages'])
            self.assertEqual(c.run(self.client,self.target,'apply',p['preview_fingerprint'])['state'],'complete')
            self.assertEqual(self.invocations.count('locked-npm-dependencies'),1)
            self.assertLess(self.invocations.index('locked-npm-dependencies'),self.invocations.index('autoanswers-prepare-deploy'))

    def test_terminal_gap_recovery_never_restarts(self):
        with ExitStack() as stack:
            p=self.fixture(stack);self.finish.side_effect=RuntimeError('lost response')
            self.assertEqual(c.run(self.client,self.target,'apply',p['preview_fingerprint'])['state'],'ambiguous')
            before=list(self.invocations);self.finish.side_effect=None
            self.assertEqual(c.run(self.client,self.target,'preview','')['state'],'reviewable')
            self.assertEqual(c.run(self.client,self.target,'apply',p['preview_fingerprint'])['state'],'complete')
            self.assertEqual(before,self.invocations)

    def test_main_drift_after_completion_cannot_finish_owner(self):
        with ExitStack() as stack:
            p=self.fixture(stack);real=r._publish_once
            def drift(*args):
                real(*args)
                if args[-1].get('phase')=='metadata-complete' and args[-1].get('state')=='after':
                    self.main.return_value={'trusted_main_sha':'e'*40}
            stack.enter_context(patch.object(r,'_publish_once',side_effect=drift))
            self.assertEqual(c.run(self.client,self.target,'apply',p['preview_fingerprint'])['state'],'ambiguous')
            self.assertTrue(self.complete);self.finish.assert_not_called()

    def test_dependency_probe_uses_full_native_verifier_without_installer(self):
        source='header\nprint(json.dumps({"dependency_versions":versions}))'
        with patch.object(r,'_selective_live_contract_script',return_value=source):
            script=c.dependency_script(SimpleNamespace())
        self.assertIn(r._buyer_chrome_native_validator_source(),script)
        self.assertIn("versions['installed_buyer_chrome']=verify_installed_buyer_chrome",script)
        for forbidden in ('apt-get','npm ci','ensure_runtime()','install()','systemctl restart'):
            self.assertNotIn(forbidden,script)

    def test_native_commands_bind_merge_and_keep_phase_budgets(self):
        from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
        target=hosted.load_hosted_runtime_target(r.TARGET_FILE)
        with patch.object(hosted,'_git_output',return_value='e'*40):
            command=c.commands(target,dict(metadata_sha256='a'*64,main_pid=123))
        self.assertIn('--expected-sha '+c.MERGE,command['cleaner'][-1])
        self.assertNotIn('e'*40,command['cleaner'][-1])
        for phase,expected in [('autoanswers-prepare-deploy',720),('systemd-reconcile',180)]:
            with patch.object(r,'_run_stage',return_value=SimpleNamespace(returncode=0,stdout='ok')) as run:
                r._must_succeed(phase,['fixture'])
                self.assertEqual(run.call_args.kwargs['timeout'],expected)
        value=dict(main_pid=1,pilot_pid=2,nested={'pid':3},started_owner={'controls':'pinned'})
        normalized=c.deepcopy_without_processes(value)
        self.assertEqual(normalized,{'nested':{'pid':3},'started_owner':{'controls':'pinned'}})

    def test_plan_is_repo_only_with_registered_smoke(self):
        from ci.select_checks import build_plan_from_paths
        paths=['ci/pr1623_dependency_continuation.py','ci/pr1623_dependency_continuation_smoke.py',
            'ci/post_merge_release_recovery.py','ci/checks.json','docs/runbooks/release_recovery.md']
        plan=build_plan_from_paths(pull_request=1,base=c.RUNNER,head='f'*40,paths=paths,
            file_exists=lambda sha,path:(ROOT/path).exists())
        self.assertEqual(plan['release_kind'],'repo_only')
        self.assertIn(['python3','ci/pr1623_dependency_continuation_smoke.py'],plan['commands'])


if __name__=='__main__':unittest.main()
