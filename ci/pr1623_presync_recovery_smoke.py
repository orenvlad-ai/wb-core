"""Offline proof of exact pre-sync admission and single canonical continuation."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ci import pr1623_presync_recovery as c
from ci import post_merge_release_recovery as r
from ci.post_merge_release_recovery_smoke import CommentsClient
from packages.application import business_data_deploy_protection as deploy
from packages.application import business_data_held_formula as formula


def failure_log(release_run_id=c.RUN):
    case=c._profile(release_run_id);profile=case['PROFILE']
    command = 'python3 ci/github_release_runner.py run --workflow-run-id "$GATE_RUN_ID" --output "$RUNNER_TEMP/release-receipt.json"'
    return ('##[group]Run ' + command + '\n' + command + '\nshell: /usr/bin/bash -e {0}\nenv:\n'
        f'  GATE_RUN_ID: {profile["gate_run_id"]}\n##[endgroup]\n'
        'line 1208, in deploy_current_checkout\n'
        'run_stage("root-storage-preflight", root_storage_commands["preflight"],\n'
        'allow_transport_reconciliation=False)\n'
        + json.dumps(dict(stage='root-storage-preflight',ok=False,alerts=[dict(available_bytes=case['AVAILABLE'],
            required_reserve_bytes=case['RESERVE'],code='storage_reserve_breach',role=case['ROLE'])])) + '\n'
        + json.dumps(dict(phase='prepared', fingerprint=case['OWNER'], baseline=case['BASELINE'], operation=profile['operation_id']))
        + "\nsubprocess.CalledProcessError: Command ['ssh'] returned non-zero exit status 3.\n").encode()


class RecoveryTests(unittest.TestCase):
    def test_exact_failure_and_native_stage_order(self):
        log = failure_log()
        self.assertEqual(c.prove_failure(log, c.PROFILE['gate_run_id'], 'job')['exit_status'], 3)
        # Candidate Gate checkouts are shallow. Exercise the actual checked-out
        # adapter's ordering with a bounded historical Git fixture; the recovery
        # workflow itself fetches full history and proves the real fixed refs.
        adapter = (ROOT / c.CODE_PATHS[0]).read_text()
        def historical_git(args):
            if args[0] == 'diff':
                self.assertEqual(args[2:4], [c.PREVIOUS, c.PROFILE['merge_sha']])
                return SimpleNamespace(stdout='')
            self.assertEqual(args, ['show', c.PROFILE['merge_sha'] + ':' + c.CODE_PATHS[0]])
            return SimpleNamespace(stdout=adapter)
        def historical_blob(args, **kwargs):
            self.assertEqual(args[:2], ['git', 'show'])
            ref, path = args[2].split(':', 1)
            self.assertEqual(ref, c.PREVIOUS); self.assertIn(path, c.CODE_PATHS)
            return SimpleNamespace(stdout=(ROOT / path).read_bytes())
        with patch.object(r, '_git', side_effect=historical_git), patch.object(subprocess, 'run', side_effect=historical_blob):
            self.assertEqual(set(c.source_proof()), set(c.CODE_PATHS))
        with patch.object(r, '_git', return_value=SimpleNamespace(stdout='changed guard')):
            with self.assertRaises(r.RecoveryError): c.source_proof()
        adapter = adapter.replace('run_stage("sync", rsync_plan)', 'run_stage("sync", changed_plan)')
        with patch.object(r, '_git', side_effect=historical_git):
            with self.assertRaises(ValueError): c.source_proof()
        self.assertEqual(r._prove_failed_stage(log, c.PROFILE['gate_run_id'], 'job', release_run_id=c.RUN)['stage'],
                         'root-storage-preflight-before-owner-start')
        for before, after in [(b'1208', b'1268'), (b'exit status 3', b'exit status 255'),
                (c.OWNER.encode(), b'wrong'), (b'26843545600', b'1'), (b'prepared', b'mutation_started'),
                (str(c.PROFILE['gate_run_id']).encode(), b'77')]:
            with self.subTest(after=after), self.assertRaises(r.RecoveryError):
                c.prove_failure(log.replace(before, after), c.PROFILE['gate_run_id'], 'job')
        with self.assertRaises(r.RecoveryError):
            c.prove_failure(log + b"subprocess.CalledProcessError: returned non-zero exit status 3.", c.PROFILE['gate_run_id'], 'job')

    def test_owner_proof_executes_only_reads_and_preserves_formula_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'guard'; path.write_bytes(b'exact')
            owner = dict(operation_id=c.PROFILE['operation_id'], phase='prepared', expected_sha='', fingerprint=c.OWNER)
            state = dict(window_id=c.WINDOW, baseline_fingerprint=c.BASELINE)
            current = dict(quiet=True, admission={'idle': True})
            expected = dict(app_dir=directory, runtime_dir=directory, env_file='env', operation=c.PROFILE['operation_id'],
                phases=['prepared'], expected_sha='', window=c.WINDOW, baseline=c.BASELINE,
                owner_fingerprint=c.OWNER, code_hashes={'guard': r.digest(b'exact')})
            def execute():
                output = io.StringIO()
                with redirect_stdout(output): exec(c.owner_script(expected), {})
                return json.loads(output.getvalue())
            with patch.object(deploy, 'load', return_value=owner), patch.object(deploy, '_binding', return_value=state), \
                 patch.object(deploy, '_clients', return_value=(None, lambda: current, lambda: {})), \
                 patch.object(formula, 'unchanged') as unchanged, patch.object(subprocess, 'run', side_effect=AssertionError('unexpected command')):
                self.assertTrue(execute()['quiet']); unchanged.assert_called_once()
                for obj, key, value in [(owner, 'operation_id', 'other'), (owner, 'phase', 'mutation_started'),
                        (owner, 'expected_sha', c.PROFILE['merge_sha']), (owner, 'fingerprint', 'drift'),
                        (state, 'window_id', 'other'), (state, 'baseline_fingerprint', 'drift'),
                        (current, 'quiet', False), (current['admission'], 'idle', False)]:
                    old = obj[key]; obj[key] = value
                    with self.subTest(key=key), self.assertRaises(SystemExit): execute()
                    obj[key] = old
                unchanged.side_effect = RuntimeError('native formula proof refused')
                with self.assertRaises(RuntimeError): execute()
                unchanged.side_effect = None
                path.write_bytes(b'changed')
                with self.assertRaises(SystemExit): execute()

    def fixture(self, stack, *, release_run_id=c.RUN):
        case=c._profile(release_run_id);profile=case['PROFILE']
        self.client = CommentsClient()
        original = {**profile, 'schema': r.release.RECEIPT_SCHEMA, 'state': 'blocked'}
        self.proof = dict(original_receipt=original, failure={'stage': 'exact'})
        self.runner = dict(trusted_main_sha='f' * 40, target_merge_sha=profile['merge_sha'])
        self.prestate = dict(metadata={'commit': case['PREVIOUS'], 'deployment_complete': True},
            metadata_sha256='old', main_pid=123, finance_pilot={'write_mode': 'readonly'})
        self.final = {**self.prestate, 'metadata': {'commit': profile['merge_sha'], 'deployment_complete': True}}
        self.target = SimpleNamespace(target_id='wb_core_eu_hosted_runtime_active')
        def mock(obj, name, **kwargs): return stack.enter_context(patch.object(obj, name, **kwargs))
        mock(c, 'evidence', return_value=self.proof)
        mock(c, 'source_proof', return_value={'code': 'exact'})
        self.owner = mock(c, 'collect_owner', return_value={'owner': 'exact'})
        self.storage = mock(c, 'storage_preflight', return_value={'ok': True})
        self.main = mock(r, 'prove_repo_only_descendant', return_value=self.runner)
        self.readback = mock(r, 'collect_prestate', side_effect=lambda t, sha, **kw: deepcopy(self.prestate if sha == case['PREVIOUS'] else self.final))
        self.finish = mock(r, '_finish_deploy_protection', return_value={})
        self.deploy = mock(c, 'canonical_deploy')
        return c.build_preview(self.client, self.target, release_run_id=release_run_id)

    def test_one_deploy_exact_completion_and_repeat_readback(self):
        with ExitStack() as stack:
            preview = self.fixture(stack)
            result = c.run(self.client, self.target, 'apply', preview['preview_fingerprint'])
            self.assertEqual(result['state'], 'complete')
            self.deploy.assert_called_once(); self.finish.assert_called_once()
            self.assertEqual(c.run(self.client, self.target, 'apply', preview['preview_fingerprint'])['state'], 'complete')
            self.deploy.assert_called_once(); self.assertEqual(self.finish.call_count, 2)

    def test_stale_preview_and_failed_capacity_never_claim(self):
        with ExitStack() as stack:
            preview = self.fixture(stack)
            self.prestate['main_pid'] = 124
            with self.assertRaises(r.RecoveryError): c.run(self.client, self.target, 'apply', preview['preview_fingerprint'])
            self.assertFalse(self.client.values); self.deploy.assert_not_called()
            self.storage.side_effect = r.RecoveryError('root reserve')
            with self.assertRaises(r.RecoveryError): c.build_preview(self.client, self.target)
            self.assertFalse(self.client.values)

    def test_claim_survives_deploy_failure_no_replay_or_finish(self):
        with ExitStack() as stack:
            preview = self.fixture(stack)
            self.deploy.side_effect = subprocess.CalledProcessError(255, ['canonical'])
            result = c.run(self.client, self.target, 'apply', preview['preview_fingerprint'])
            self.assertEqual(result['state'], 'ambiguous')
            self.readback.side_effect = r.RecoveryError('metadata incomplete')
            for _ in range(2):
                self.assertEqual(c.run(self.client, self.target, 'apply', preview['preview_fingerprint'])['state'], 'ambiguous')
            self.deploy.assert_called_once(); self.finish.assert_not_called()

    def test_terminal_gap_only_finishes_and_receipts_without_deploy_replay(self):
        for gap in ('before-finish', 'before-receipt'):
            with self.subTest(gap=gap), ExitStack() as stack:
                preview = self.fixture(stack)
                if gap == 'before-finish':
                    self.finish.side_effect = RuntimeError('lost before finish')
                else:
                    publish = r._publish_once
                    def lose_receipt(client, pr, marker, payload):
                        if payload['state'] == 'complete': raise RuntimeError('lost before receipt')
                        return publish(client, pr, marker, payload)
                    lost = stack.enter_context(patch.object(r, '_publish_once', side_effect=lose_receipt))
                self.assertEqual(c.run(self.client, self.target, 'apply', preview['preview_fingerprint'])['state'], 'ambiguous')
                self.finish.side_effect = None
                if gap == 'before-receipt': lost.side_effect = publish
                before_finish = self.finish.call_count
                # A preview with an existing claim never finishes the owner.
                self.assertEqual(c.run(self.client, self.target, 'preview', '')['state'], 'reviewable')
                self.assertEqual(self.finish.call_count, before_finish)
                self.assertEqual(c.run(self.client, self.target, 'apply', preview['preview_fingerprint'])['state'], 'complete')
                self.deploy.assert_called_once()

    def test_completed_markers_with_changed_finance_cannot_finish(self):
        with ExitStack() as stack:
            preview = self.fixture(stack)
            self.finish.side_effect = RuntimeError('gap')
            self.assertEqual(c.run(self.client, self.target, 'apply', preview['preview_fingerprint'])['state'], 'ambiguous')
            self.finish.reset_mock(); self.finish.side_effect = None
            self.final['finance_pilot'] = {'write_mode': 'changed'}
            self.assertEqual(c.run(self.client, self.target, 'apply', preview['preview_fingerprint'])['state'], 'ambiguous')
            self.finish.assert_not_called(); self.deploy.assert_called_once()

    def test_drift_after_claim_refuses_deploy_and_final_main_refuses_finish(self):
        with ExitStack() as stack:
            preview = self.fixture(stack)
            real_publish = r._publish_once
            def publish(*args):
                real_publish(*args); self.prestate['main_pid'] = 124
            stack.enter_context(patch.object(r, '_publish_once', side_effect=publish))
            self.assertEqual(c.run(self.client, self.target, 'apply', preview['preview_fingerprint'])['state'], 'ambiguous')
            self.deploy.assert_not_called(); self.finish.assert_not_called()
        with ExitStack() as stack:
            preview = self.fixture(stack)
            def drift(**kwargs): self.main.return_value = {**self.runner, 'trusted_main_sha': 'e' * 40}
            self.deploy.side_effect = drift
            self.assertEqual(c.run(self.client, self.target, 'apply', preview['preview_fingerprint'])['state'], 'ambiguous')
            self.deploy.assert_called_once(); self.finish.assert_not_called()

    def test_canonical_adapter_exact_checkout_identity_and_cleanup(self):
        calls = []
        def local_git(args):
            if args[0] == 'clone': (Path(args[-1]) / '.git').mkdir(parents=True)
            return SimpleNamespace(stdout=c.PROFILE['merge_sha'])
        with patch.object(r, '_git', side_effect=local_git) as git, patch.object(subprocess, 'run', side_effect=lambda args, **kw: calls.append((args, kw))):
            c.canonical_deploy()
        self.assertEqual(git.call_args_list[0].args[0][:3], ['clone', '--shared', '--no-checkout'])
        self.assertEqual(git.call_args_list[1].args[0][-3:], ['checkout', '--detach', c.PROFILE['merge_sha']])
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[1:], ['apps/registry_upload_http_entrypoint_hosted_runtime.py', 'deploy'])
        for key, value in [('WB_CORE_RELEASE_PR', '1623'), ('WB_CORE_RELEASE_HEAD', c.PROFILE['head_sha']),
                ('WB_CORE_RELEASE_OPERATION_ID', c.PROFILE['operation_id']), ('WB_CORE_RELEASE_DEFER_DEPLOY_OWNER_FINISH', 'true')]:
            self.assertEqual(kwargs['env'][key], value)
        self.assertNotEqual(kwargs['cwd'], r.ROOT)
        self.assertEqual(kwargs['env']['WB_CORE_HOSTED_RUNTIME_TARGET_FILE'], str(kwargs['cwd'] / c.TARGET_RELATIVE))
        self.assertTrue(Path(kwargs['env']['WB_CORE_HOSTED_RUNTIME_TARGET_FILE']).is_absolute())
        self.assertFalse(kwargs['cwd'].exists())

    def test_new_lane_is_repo_only_and_registered(self):
        from ci.select_checks import build_plan_from_paths
        paths = ['ci/pr1623_presync_recovery.py', 'ci/pr1623_presync_recovery_smoke.py',
            'ci/post_merge_release_recovery.py', 'ci/checks.json', 'docs/runbooks/release_recovery.md']
        plan = build_plan_from_paths(pull_request=1, base=c.PROFILE['merge_sha'], head='f' * 40,
            paths=paths, file_exists=lambda sha, p: (ROOT / p).exists())
        self.assertEqual(plan['release_kind'], 'repo_only')
        self.assertIn(['python3', 'ci/pr1623_presync_recovery_smoke.py'], plan['commands'])

    def test_real_local_clone_uses_old_exact_head_and_never_copies_git_file(self):
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            def git(*args):
                return real_run(['git', *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
            git('init', '-q'); git('config', 'user.email', 'fixture@example.invalid'); git('config', 'user.name', 'Fixture')
            (repo / 'payload').write_text('original')
            target_file = repo / c.TARGET_RELATIVE
            target_file.parent.mkdir(parents=True)
            target_file.write_text('{"target":"original-exact"}')
            git('add', '.'); git('commit', '-qm', 'original'); exact = git('rev-parse', 'HEAD')
            (repo / 'payload').write_text('later repo-only runner')
            target_file.write_text('{"target":"later"}')
            git('commit', '-qam', 'runner')
            calls = []
            def run(args, **kwargs):
                if args[0] == 'git': return real_run(args, **kwargs)
                checkout = kwargs['cwd']
                self.assertTrue((checkout / '.git').is_dir())
                self.assertEqual((checkout / 'payload').read_text(), 'original')
                selected_target = Path(kwargs['env']['WB_CORE_HOSTED_RUNTIME_TARGET_FILE'])
                self.assertEqual(selected_target, checkout / c.TARGET_RELATIVE)
                self.assertEqual(json.loads(selected_target.read_text()), {'target': 'original-exact'})
                self.assertEqual(real_run(['git', 'rev-parse', 'HEAD'], cwd=checkout,
                    capture_output=True, check=True, text=True).stdout.strip(), exact)
                calls.append(checkout)
                raise subprocess.CalledProcessError(1, args)
            with patch.object(r, 'ROOT', repo), patch.dict(c.PROFILE, merge_sha=exact), patch.object(subprocess, 'run', side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError): c.canonical_deploy()
            self.assertEqual(len(calls), 1); self.assertFalse(calls[0].exists())
            self.assertEqual((repo / 'payload').read_text(), 'later repo-only runner')


class Pr1626StorageRefusalTests(unittest.TestCase):
    RUN=45514713724
    def fixture(self,stack):
        return RecoveryTests.fixture(self,stack,release_run_id=self.RUN)

    def test_exact_backup_failure_cannot_admit_other_run_role_or_owner(self):
        case=c._profile(self.RUN);profile=case['PROFILE'];raw=failure_log(self.RUN)
        self.assertEqual(c.prove_failure(raw,profile['gate_run_id'],'job',release_run_id=self.RUN)['exit_status'],3)
        self.assertEqual(r._prove_failed_stage(raw,profile['gate_run_id'],'job',release_run_id=self.RUN)['stage'],
            'root-storage-preflight-before-owner-start')
        for before,after in [(b'"backup"',b'"root"'),(str(case['RESERVE']).encode(),b'1'),
                (case['OWNER'].encode(),c.OWNER.encode()),(case['BASELINE'].encode(),c.BASELINE.encode()),
                (b'prepared',b'mutation_started'),(b'exit status 3',b'exit status 255'),
                (b'allow_transport_reconciliation=False',b'allow_transport_reconciliation=True'),
                (str(profile['gate_run_id']).encode(),str(c.PROFILE['gate_run_id']).encode())]:
            with self.subTest(after=after),self.assertRaises(r.RecoveryError):
                c.prove_failure(raw.replace(before,after),profile['gate_run_id'],'job',release_run_id=self.RUN)
        for raw,gate,run in [(raw,profile['gate_run_id'],c.RUN),
                (failure_log(),c.PROFILE['gate_run_id'],self.RUN),(raw,profile['gate_run_id'],1)]:
            with self.subTest(run=run),self.assertRaises(r.RecoveryError):c.prove_failure(raw,gate,'job',release_run_id=run)

    def test_one_canonical_deploy_uses_original_identity_and_correct_issue(self):
        with ExitStack() as stack:
            preview=self.fixture(stack)
            self.assertEqual(preview['release_run_id'],self.RUN)
            self.assertEqual(preview['recovery_case'],'pr1626-proven-unentered-presync')
            with patch.object(r,'_publish_once',wraps=r._publish_once) as publish:
                result=c.run(self.client,self.target,'apply',preview['preview_fingerprint'],release_run_id=self.RUN)
                self.assertEqual(result['state'],'complete')
                self.assertEqual([call.args[1] for call in publish.call_args_list],[1626,1626])
            self.deploy.assert_called_once_with(release_run_id=self.RUN)
            self.finish.assert_called_once_with(self.target,self.proof['original_receipt'])
            self.assertEqual(c.run(self.client,self.target,'apply',preview['preview_fingerprint'],release_run_id=self.RUN)['state'],'complete')
            self.deploy.assert_called_once()

    def test_uncertain_existing_claim_has_no_deploy_replay(self):
        with ExitStack() as stack:
            preview=self.fixture(stack);self.deploy.side_effect=subprocess.CalledProcessError(255,['canonical'])
            self.assertEqual(c.run(self.client,self.target,'apply',preview['preview_fingerprint'],release_run_id=self.RUN)['state'],'ambiguous')
            self.readback.side_effect=r.RecoveryError('incomplete runtime')
            self.assertEqual(c.run(self.client,self.target,'apply',preview['preview_fingerprint'],release_run_id=self.RUN)['state'],'ambiguous')
            self.deploy.assert_called_once_with(release_run_id=self.RUN);self.finish.assert_not_called()

    def test_stale_prestate_and_storage_refusal_cannot_claim(self):
        with ExitStack() as stack:
            preview=self.fixture(stack);self.prestate['main_pid']=124
            with self.assertRaises(r.RecoveryError):c.run(self.client,self.target,'apply',preview['preview_fingerprint'],release_run_id=self.RUN)
            self.assertFalse(self.client.values);self.deploy.assert_not_called()
            self.storage.side_effect=r.RecoveryError('backup reserve still breached')
            with self.assertRaises(r.RecoveryError):c.build_preview(self.client,self.target,release_run_id=self.RUN)
            self.assertFalse(self.client.values)

    def test_canonical_checkout_and_environment_bind_original_merge(self):
        case=c._profile(self.RUN);profile=case['PROFILE'];calls=[]
        def git(args):
            if args[0]=='clone':(Path(args[-1])/'.git').mkdir(parents=True)
            return SimpleNamespace(stdout=profile['merge_sha'])
        with patch.object(r,'_git',side_effect=git),patch.object(subprocess,'run',side_effect=lambda args,**kw:calls.append((args,kw))):
            c.canonical_deploy(release_run_id=self.RUN)
        self.assertEqual(len(calls),1);args,kwargs=calls[0]
        self.assertEqual(args[1:],['apps/registry_upload_http_entrypoint_hosted_runtime.py','deploy'])
        self.assertEqual(kwargs['env']['WB_CORE_RELEASE_PR'],'1626')
        self.assertEqual(kwargs['env']['WB_CORE_RELEASE_OPERATION_ID'],profile['operation_id'])
        self.assertEqual(kwargs['env']['WB_CORE_RELEASE_HEAD'],profile['head_sha'])
        self.assertEqual(kwargs['env']['WB_CORE_RELEASE_DEFER_DEPLOY_OWNER_FINISH'],'true')
        self.assertEqual(kwargs['env']['WB_CORE_HOSTED_RUNTIME_TARGET_FILE'],str(kwargs['cwd']/c.TARGET_RELATIVE))
        self.assertTrue(Path(kwargs['env']['WB_CORE_HOSTED_RUNTIME_TARGET_FILE']).is_absolute())



if __name__ == '__main__':
    unittest.main()
