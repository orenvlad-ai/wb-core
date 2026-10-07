#!/usr/bin/env python3
"""Offline deploy-owner races, real admission drain and protected shell checks."""
from __future__ import annotations
from contextlib import ExitStack
import ast
from copy import deepcopy
import json
import io
import multiprocessing
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.business_data_maintenance_pause_smoke import fixture
from packages.application import business_data_deploy_protection as owner
from packages.application import business_data_maintenance_pause as pause
from packages.application import business_data_write_barrier as barrier
from packages.application.business_data_heavy_admission import HeavyAdmissionLease
from packages.application.business_data_procedure_admission import AdmissionLease, MaintenanceAdmissionBlocked

SHA = 'a' * 40
OP = 'release-test-001'


def hold_cycle(runtime, started, release):
    lease = HeavyAdmissionLease(Path(runtime), operation='cycle')
    try:
        with lease.entered():
            started.set()
            release.wait(10)
    finally:
        lease.close()


def competing_owner_action(runtime, action, fingerprint, ready, go, results):
    ready.set()
    go.wait(5)
    try:
        value = (owner.start(Path(runtime), OP, SHA) if action == 'start'
                 else owner.cancel_prepared(Path(runtime), OP, fingerprint))
        results.put(('ok', value['phase']))
    except RuntimeError:
        results.put(('blocked', action))


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Path(self.directory.name)
        self.systemd, self.activity = fixture(self.runtime)
        self.proc = self.runtime / 'proc'
        self.proc.mkdir()
        self.options = dict(systemd=self.systemd, activity_reader=self.activity, proc_root=self.proc,
            window_id='explicit-pause-test', actor='test', reason='offline deployment protection')
        self.addCleanup(patch.stopall)
        patch.object(pause, '_cron_entries', return_value=[]).start()
        patch.object(owner, '_selected', return_value=True).start()

    def quiet(self):
        return pause.readback(self.runtime, systemd=self.systemd,
                              activity_reader=self.activity, proc_root=self.proc)

    def held(self):
        self.assertEqual(pause.pause(self.runtime, **self.options)['status'], 'held')
        return pause.load_state(self.runtime)

    def claim(self):
        return owner.claim(self.runtime, OP, quiet_reader=self.quiet)

    def test_real_cycle_lease_drains_without_stop_and_timeout_is_explicit(self):
        context = multiprocessing.get_context('fork')
        started, release = context.Event(), context.Event()
        process = context.Process(target=hold_cycle, args=(str(self.runtime), started, release))
        process.start()
        try:
            self.assertTrue(started.wait(5))
            with self.assertRaises(TimeoutError):
                pause.pause(self.runtime, **self.options, wait_timeout_seconds=0)
            self.assertTrue(process.is_alive())
            self.assertEqual(pause.load_state(self.runtime)['phase'], 'draining')
            self.assertTrue(barrier.barrier_status(self.runtime)['active'])
            with self.assertRaisesRegex(RuntimeError, 'already held'):
                self.claim()
            self.assertFalse((self.runtime / owner.FILENAME).exists())
            self.assertTrue(all(unit.endswith('.timer') for _, unit in self.systemd.calls))
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(process.exitcode, 0)
        self.held()
        self.assertEqual(self.claim()['phase'], 'prepared')

    def test_no_pause_or_nonquiet_cannot_claim(self):
        with self.assertRaisesRegex(RuntimeError, 'already held'):
            self.claim()
        self.held()
        with self.assertRaisesRegex(RuntimeError, 'fresh held/quiet'):
            owner.claim(self.runtime, OP, quiet_reader=lambda: {'quiet': False})
        self.assertFalse((self.runtime / owner.FILENAME).exists())

    def test_owner_blocks_resume_and_central_release_before_timer_changes(self):
        state = self.held()
        before = list(self.systemd.calls)
        self.claim()
        owner.start(self.runtime, OP, SHA)
        with self.assertRaisesRegex(RuntimeError, 'unfinished deploy'):
            pause.resume(self.runtime, **self.options)
        self.assertEqual(self.systemd.calls, before)
        with self.assertRaisesRegex(RuntimeError, 'unfinished deploy'):
            barrier.release_barrier(self.runtime, window_id=state['window_id'],
                plan_fingerprint=state['baseline_fingerprint'], actor='test', reason='race',
                restore_readback={'status': 'restored', 'exact_prior_state_restored': True})
        with self.assertRaisesRegex(RuntimeError, 'unfinished deploy'):
            barrier.abort_barrier_acquire(self.runtime, window_id=state['window_id'],
                plan_fingerprint=state['baseline_fingerprint'], actor='test', reason='race',
                restore_readback={'status': 'restored', 'exact_prior_state_restored': True})
        with self.assertRaises(MaintenanceAdmissionBlocked):
            AdmissionLease(self.runtime)

    def test_prepared_cancel_wins_and_then_sync_start_is_refused(self):
        self.held()
        value = self.claim()
        owner.cancel_prepared(self.runtime, OP, value['fingerprint'])
        with self.assertRaisesRegex(RuntimeError, 'prepared deploy'):
            owner.start(self.runtime, OP, SHA)
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])
        self.assertEqual(pause.load_state(self.runtime)['phase'], 'held')

    def test_mutation_start_wins_and_then_cancel_cannot_open_window(self):
        self.held()
        value = self.claim()
        owner.start(self.runtime, OP, SHA)
        with self.assertRaisesRegex(RuntimeError, 'cancellation proof'):
            owner.cancel_prepared(self.runtime, OP, value['fingerprint'])
        with self.assertRaisesRegex(RuntimeError, 'already started'):
            owner.start(self.runtime, OP, SHA)
        self.assertEqual(owner.start(self.runtime, OP, SHA, recovery=True)['phase'], 'mutation_started')
        with self.assertRaisesRegex(RuntimeError, 'another deploy'):
            owner.claim(self.runtime, 'unrelated-release', quiet_reader=self.quiet)
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])

    def test_process_race_cancel_vs_first_mutation_has_one_winner(self):
        self.held()
        value = self.claim()
        context = multiprocessing.get_context('fork')
        go, results = context.Event(), context.Queue()
        ready = [context.Event(), context.Event()]
        processes = [context.Process(target=competing_owner_action,
            args=(str(self.runtime), action, value['fingerprint'], ready[i], go, results))
            for i, action in enumerate(('start', 'cancel'))]
        for process in processes:
            process.start()
        try:
            self.assertTrue(all(event.wait(5) for event in ready))
            go.set()
            outcomes = [results.get(timeout=5) for _ in processes]
            self.assertEqual(sum(item[0] == 'ok' for item in outcomes), 1, outcomes)
            self.assertIn(owner.load(self.runtime)['phase'], {'mutation_started', 'cancelled'})
            self.assertTrue(barrier.barrier_status(self.runtime)['active'])
        finally:
            go.set()
            for process in processes:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join()
            results.close()
            results.join_thread()

    def test_finish_requires_exact_readback_and_leaves_manual_pause_held(self):
        before = self.held()
        self.claim()
        owner.start(self.runtime, OP, SHA)
        app = self.runtime / 'app'
        app.mkdir()
        (app / '.wb-core-runtime-sha').write_text(SHA + '\n')
        (app / '.wb-core-deploy.json').write_text(json.dumps({'commit': SHA, 'deployment_complete': False}))
        registry = lambda: {'active_state': 'active', 'main_pid': 123}
        with self.assertRaisesRegex(RuntimeError, 'readback is unproven'):
            owner.finish(self.runtime, OP, SHA, app_dir=app, registry_reader=registry)
        self.assertEqual(owner.load(self.runtime)['phase'], 'mutation_started')
        (app / '.wb-core-deploy.json').write_text(json.dumps({'commit': SHA, 'deployment_complete': True}))
        self.assertEqual(owner.finish(self.runtime, OP, SHA, app_dir=app, registry_reader=registry)['phase'], 'complete')
        terminal_bytes = (self.runtime / owner.FILENAME).read_bytes()
        owner.finish(self.runtime, OP, SHA, app_dir=app, registry_reader=registry)
        self.assertEqual((self.runtime / owner.FILENAME).read_bytes(), terminal_bytes)
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])
        self.assertEqual(pause.load_state(self.runtime)['baseline'], before['baseline'])
        self.assertEqual(pause.resume(self.runtime, **self.options)['status'], 'restored')

    def test_guarded_shell_failure_blocks_all_semicolon_commands(self):
        self.held()
        self.claim()
        owner.start(self.runtime, OP, SHA)
        identity = dict(app_dir=ROOT, runtime_dir=self.runtime, env_file=self.runtime / 'unused.env',
                        operation='wrong-owner', expected_sha=SHA)
        marker = self.runtime / 'effect'
        shell = owner.guarded_shell(f'touch {marker}; touch {marker}.second', **identity)
        result = subprocess.run(['sh', '-c', shell], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertFalse(Path(str(marker) + '.second').exists())
        identity['operation'] = OP
        result = subprocess.run(['sh', '-c', owner.guarded_shell('printf safe-output', **identity)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'safe-output')

    def test_guarded_rsync_receiver_keeps_stdout_clean_and_appends_arguments(self):
        self.held()
        self.claim()
        owner.start(self.runtime, OP, SHA)
        identity = dict(app_dir=ROOT, runtime_dir=self.runtime, env_file=self.runtime / 'unused.env',
                        operation=OP, expected_sha=SHA, bootstrap=True)
        # An executable receiver emits only its own protocol, never owner JSON.
        shell = owner.guarded_shell('printf', receiver=True, **identity) + ' receiver-output'
        result = subprocess.run(['sh', '-c', shell], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'receiver-output')

    def test_first_sync_inline_check_needs_no_installed_protection_module(self):
        self.held()
        self.claim()
        owner.start(self.runtime, OP, SHA)
        shell = owner.remote_shell('check', app_dir=ROOT, runtime_dir=self.runtime,
            env_file=self.runtime / 'unused.env', operation=OP, expected_sha=SHA, bootstrap=True)
        words = shlex.split(shell)
        self.assertEqual(words[-2], '-c')
        # Block importing the new module. Only sender-provided code can pass;
        # the existing installed pause/admission APIs remain real.
        program = ("import sys; sys.modules['packages.application.business_data_deploy_protection']=None; "
                   + words[-1])
        result = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['phase'], 'mutation_started')

    def test_standalone_prepare_and_managed_restart_refuse_missing_owner(self):
        self.held()
        from apps.wb_autoanswers_activation import _deployment_quiesce
        from apps.hosted_runtime_deploy_barrier import reconcile
        with patch.dict('os.environ', {'WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE': 'true'}), \
                patch('apps.wb_autoanswers_activation.subprocess.run') as stop:
            with self.assertRaisesRegex(RuntimeError, 'ownership is required'):
                with _deployment_quiesce(self.runtime):
                    self.fail('unsafe mutation body')
            stop.assert_not_called()
        with patch('apps.hosted_runtime_deploy_barrier.subprocess.run') as restart:
            with self.assertRaisesRegex(RuntimeError, 'ownership is required'):
                reconcile(runtime_dir=self.runtime, enable=[], restart=['wb-core-registry-http.service'])
            restart.assert_not_called()

    def test_prepare_deploy_guard_does_not_depend_on_optional_quiesce_flag(self):
        self.held()
        from apps import wb_autoanswers_activation as activation
        for flag in (None, 'false'):
            with patch.dict('os.environ', {'WB_AUTOANSWERS_FORCE_OFF': 'true'}, clear=True), \
                    patch.object(activation, 'AutoanswersRepository') as repository:
                if flag is not None:
                    import os
                    os.environ['WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE'] = flag
                with self.assertRaisesRegex(RuntimeError, 'ownership is required'):
                    activation.run(action='prepare-deploy', runtime_dir=self.runtime)
                repository.assert_not_called()

    def test_all_central_phase_mutations_are_blocked_while_deploy_is_unfinished(self):
        state = self.held()
        self.claim()
        for phase in ('prepared', 'mutation_started'):
            if phase == 'mutation_started':
                owner.start(self.runtime, OP, SHA)
            before = (self.runtime / barrier.STATE_FILENAME).read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'unfinished deploy'):
                barrier.mark_barrier_restoring(self.runtime, window_id=state['window_id'],
                    plan_fingerprint=state['baseline_fingerprint'])
            with self.assertRaisesRegex(RuntimeError, 'unfinished deploy'):
                barrier.confirm_barrier_hold(self.runtime, window_id=state['window_id'],
                    plan_fingerprint=state['baseline_fingerprint'], maintenance_state=state)
            with self.assertRaisesRegex(RuntimeError, 'unfinished deploy'):
                barrier.acquire_barrier(self.runtime, window_id=state['window_id'],
                    window_kind='maintenance_pause', plan_fingerprint=state['baseline_fingerprint'],
                    approval_reference='test-approval', actor='test', reason='race')
            self.assertEqual((self.runtime / barrier.STATE_FILENAME).read_bytes(), before)

    def test_premerge_refusal_emits_blocked_receipt_without_merge(self):
        from apps import github_release_runner as runner
        plan = {'release_kind': 'live_runtime', 'plan_sha256': 'c' * 64}
        output = self.runtime / 'receipt.json'
        with patch.object(runner, 'admit', return_value=({}, plan, 7, 'b' * 40, SHA)), \
                patch.object(runner, 'prepare_deploy_ownership', side_effect=RuntimeError('busy')), \
                patch.object(runner, 'merge_exact') as merge, patch.object(runner, 'publish'):
            result = runner.run(object(), 1, output)
        self.assertEqual(result['state'], 'blocked')
        self.assertIsNone(result['merge_sha'])
        merge.assert_not_called()

    def test_runner_claim_precedes_merge_and_full_readback_precedes_finish(self):
        from apps import github_release_runner as runner
        calls = []
        plan = {'release_kind': 'live_runtime', 'plan_sha256': 'c' * 64}
        output = self.runtime / 'receipt.json'
        with patch.object(runner, 'admit', return_value=({}, plan, 7, 'b' * 40, SHA)), \
                patch.object(runner, 'prepare_deploy_ownership', side_effect=lambda *_: calls.append('claim')), \
                patch.object(runner, 'merge_exact', side_effect=lambda *_: calls.append('merge') or SHA), \
                patch.object(runner, 'checkout_merge', side_effect=lambda *_: calls.append('checkout')), \
                patch.object(runner, 'deploy_exact', side_effect=lambda *_, **__: calls.append('deploy') or SHA), \
                patch.object(runner, 'publish'):
            self.assertEqual(runner.run(object(), 1, output)['state'], 'done')
        self.assertEqual(calls, ['claim', 'merge', 'checkout', 'deploy'])
        calls.clear()
        def command(command, **options):
            if command[:2] == [sys.executable, 'apps/registry_upload_http_entrypoint_hosted_runtime.py']:
                self.assertEqual(options['env']['WB_CORE_RELEASE_DEFER_DEPLOY_OWNER_FINISH'], 'true')
                self.assertEqual(options['env']['WB_CORE_RELEASE_OPERATION_ID'], OP)
                calls.append('deploy')
            else:
                self.assertIn('business_data_deploy_protection finish', command[-1])
                calls.append('finish')
            return subprocess.CompletedProcess(command, 0)
        with patch.object(runner, 'configure_ssh'), patch.object(runner, 'trusted_main_sha', return_value=SHA), \
                patch.object(runner, 'runtime_readback', side_effect=lambda *_: calls.append('full-readback')), \
                patch.object(runner.subprocess, 'run', side_effect=command):
            self.assertEqual(runner.deploy_exact(7, SHA, SHA, operation=OP), SHA)
        self.assertEqual(calls, ['deploy', 'full-readback', 'finish'])
        calls.clear()
        with patch.object(runner, 'configure_ssh'), \
                patch.object(runner, 'runtime_readback', side_effect=RuntimeError('unknown readback')), \
                patch.object(runner.subprocess, 'run', side_effect=command):
            with self.assertRaises(RuntimeError):
                runner.deploy_exact(7, SHA, SHA, operation=OP)
        self.assertEqual(calls, ['deploy'])

    def recovery_fixture(self, case, *, finish_fails=False, operation=OP):
        from ci import post_merge_release_recovery as recovery
        from ci.post_merge_release_recovery_smoke import preview, CommentsClient, FINGERPRINT
        self.held()
        value = preview()
        value['source']['original_operation_id'] = operation
        value['recovery_case'] = case.value
        original = {**value['source'], 'operation_id': operation}
        value['operation_id'] = recovery.recovery_operation_id(9, original)
        value['predependency_diff'] = value['selective_b9_diff'] = {'exact': True}
        value['activation_failure_proof'] = {'job_rows': 0}
        if case is recovery.RecoveryCase.PREDEPENDENCY_STORAGE:
            value['prestate']['predependency_live_contract'] = {'dependency_versions': {
                'npm_modules': {'installed': True, 'versions': '8.17.1 3.0.1'}}}
        app = self.runtime / 'installed-markers'
        app.mkdir()
        (app / '.wb-core-runtime-sha').write_text(SHA + '\n')
        meta = app / '.wb-core-deploy.json'
        meta.write_text(json.dumps({'commit': SHA, 'deployment_complete': False}))
        effects = self.runtime / 'effects'
        calls = []
        state_flags = {'restart': False, 'finish_fails': finish_fails}
        target = SimpleNamespace(target_dir=str(ROOT), runtime_env={'REGISTRY_UPLOAD_RUNTIME_DIR': str(self.runtime)},
                                 environment_file=str(self.runtime / 'unused.env'), ssh_destination='offline-test')
        def stage(name, *, stdin=False):
            code = f"from pathlib import Path; p=Path({str(effects)!r}); p.open('a').write({name!r}+'\\n')"
            if stdin:
                code += f"; import sys,json; assert sys.stdin.read()=='exact-cas'; Path({str(meta)!r}).write_text(json.dumps({{'commit':{SHA!r},'deployment_complete':True}}))"
                # Actual stdin Python CAS exercises grouping and preservation of
                # its incoming stream after the separate owner check process.
                return ['ssh', 'offline-test', 'python3 -c ' + shlex.quote(code)]
            return ['ssh', 'offline-test', 'python3 -c ' + shlex.quote(code)]
        commands = {name: [name] for name in ('root_storage_readback', 'status', 'auth', 'activation_readback')}
        commands.update(activation=stage('activation'), completion=stage('completion', stdin=True),
            completion_input='exact-cas', cleaner_precomplete_probe=['cleaner-probe'],
            root_storage_status=stage('storage-refresh'))
        commands['normal_activation_tail'] = {name: [name] for name in (
            'auth', 'status', 'storage_readback', 'barrier')}
        commands['normal_activation_tail'].update({name: stage(name) for name in (
            'prepare', 'install', 'daemon_reload', 'nginx', 'restart', 'reconcile', 'storage')})
        def current(_target, _sha, *, require_incomplete, **_kwargs):
            result = deepcopy(value['prestate'])
            result['main_pid'] = 43 if state_flags['restart'] else 42
            result['metadata']['deployment_complete'] = json.loads(meta.read_text())['deployment_complete']
            self.assertEqual(result['metadata']['deployment_complete'], not require_incomplete)
            return result
        def run_stage(command, *, input_text=None, **_kwargs):
            if command[0] != 'ssh':
                calls.append(command[0])
                return subprocess.CompletedProcess(command, 0, stdout='read', stderr='')
            shell = command[-1]
            words = shlex.split(shell)
            if '-c' in words and "namespace['main'](" in words[-1]:
                arguments = ast.literal_eval(words[-1].rsplit("namespace['main'](", 1)[1][:-2])
                self.assertEqual(arguments[arguments.index('--operation') + 1], operation)
                calls.append(arguments[0])
                proof = owner.claim(self.runtime, operation, quiet_reader=self.quiet)
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(proof), stderr='')
            if ('packages.application.business_data_deploy_protection' in words
                    and words[words.index('packages.application.business_data_deploy_protection') + 1] in {'recovery-start', 'finish'}):
                action = words[words.index('packages.application.business_data_deploy_protection') + 1]
                calls.append(action)
                try:
                    if action == 'recovery-start':
                        proof = owner.start(self.runtime, operation, SHA, recovery=True)
                    else:
                        proof = owner.finish(self.runtime, operation, SHA, app_dir=app, registry_reader=lambda: {
                            'active_state': 'inactive' if state_flags['finish_fails'] else 'active', 'main_pid': 123})
                except RuntimeError:
                    return subprocess.CompletedProcess(command, 1, stdout='blocked', stderr='')
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(proof), stderr='')
            # Execute the actual generated owner guard and the isolated effect,
            # including activation/completion. These helpers are not mocked.
            owner.require_owner(self.runtime, operation, SHA)
            result = subprocess.run(['sh', '-c', shell], input=input_text, capture_output=True, text=True)
            if effects.exists() and 'restart' in effects.read_text().splitlines():
                state_flags['restart'] = True
            return result
        client = CommentsClient()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(recovery, 'build_stage_commands', return_value=commands))
        stack.enter_context(patch.object(recovery, '_run_stage', side_effect=run_stage))
        stack.enter_context(patch.object(recovery, 'collect_prestate', side_effect=current))
        stack.enter_context(patch.object(recovery, 'prove_repo_only_descendant', return_value=value['runner']))
        stack.enter_context(patch.object(recovery, '_predependency_diff_proof', return_value=value['predependency_diff']))
        stack.enter_context(patch.object(recovery, '_selective_b9_diff_proof', return_value=value['selective_b9_diff']))
        stack.enter_context(patch.object(recovery, 'collect_sqlite_activation_failure', return_value=value['activation_failure_proof']))
        return recovery, value, client, target, effects, calls, state_flags, original, FINGERPRINT, stack

    def test_all_canonical_recovery_branches_execute_actual_owner_and_effect_guards(self):
        from ci.post_merge_release_recovery import RecoveryCase
        for case in RecoveryCase:
            # Fresh private state for every independent supported tail.
            with self.subTest(case=case.value):
                self.runtime = Path(self.directory.name) / case.value
                self.runtime.mkdir()
                self.systemd, self.activity = fixture(self.runtime)
                self.proc = self.runtime / 'proc'
                self.proc.mkdir()
                self.options = dict(systemd=self.systemd, activity_reader=self.activity, proc_root=self.proc,
                    window_id='explicit-pause-test', actor='test', reason='offline recovery protection')
                recovery, value, client, target, effects, calls, flags, original, fp, stack = self.recovery_fixture(case)
                result = recovery.apply_recovery(client, value, fp, target)
                self.assertEqual(result['state'], 'complete', result)
                self.assertEqual(calls[:2], ['claim', 'recovery-start'])
                self.assertEqual(calls[-1], 'finish')
                self.assertEqual(owner.load(self.runtime)['phase'], 'complete')
                self.assertTrue(barrier.barrier_status(self.runtime)['active'])
                applied = effects.read_text().splitlines()
                self.assertEqual(applied.count('activation'), 1)
                self.assertEqual(applied.count('completion'), 1)
                if recovery.normal_activation_tail_case(case):
                    self.assertLess(applied.index('restart'), applied.index('activation'))
                stack.close()

    def test_recovery_finish_ambiguity_is_not_complete_and_same_claim_only_terminalizes_owner(self):
        from ci.post_merge_release_recovery import RecoveryCase
        recovery, value, client, target, effects, calls, flags, original, fp, stack = self.recovery_fixture(
            RecoveryCase.STORAGE_TAIL, finish_fails=True)
        result = recovery.apply_recovery(client, value, fp, target)
        self.assertEqual(result['state'], 'ambiguous')
        self.assertEqual(owner.load(self.runtime)['phase'], 'mutation_started')
        self.assertFalse(any(recovery.RECEIPT_MARKER in body for body in client.values))
        before = effects.read_bytes()
        flags['finish_fails'] = False
        with patch.object(recovery, 'collect_evidence', return_value={
                'original_receipt': original, 'recovery_case': RecoveryCase.STORAGE_TAIL.value}):
            terminal = recovery.existing_recovery_readback(client, 9, fp, target)
            self.assertEqual(terminal['state'], 'complete')
            terminal_bytes = (self.runtime / owner.FILENAME).read_bytes()
            self.assertEqual(recovery.existing_recovery_readback(client, 9, fp, target)['state'], 'complete')
            self.assertEqual((self.runtime / owner.FILENAME).read_bytes(), terminal_bytes)
        self.assertEqual(effects.read_bytes(), before)
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])

    def test_owner_guard_preserves_here_document_and_blocks_wrong_safe_finalize_owner(self):
        self.held()
        self.claim()
        owner.start(self.runtime, OP, SHA)
        identity = dict(app_dir=ROOT, runtime_dir=self.runtime, env_file=self.runtime / 'unused.env',
                        operation=OP, expected_sha=SHA)
        script = "python3 - <<'PY'\nprint('heredoc-proof')\nPY"
        result = subprocess.run(['sh', '-c', owner.guarded_shell(script, **identity)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'heredoc-proof\n')
        from apps.hosted_runtime_transport_reconcile import _remote_command
        target = SimpleNamespace(target_dir=str(ROOT), service_name='registry.service', target_id='offline-test',
            environment_file=str(self.runtime / 'unused.env'), health_paths=(), public_health_paths=(), route_paths={},
            runtime_env={'REGISTRY_UPLOAD_RUNTIME_DIR': str(self.runtime)}, ssh_destination='offline-test')
        shell = _remote_command(target, 'safe-finalize', expected_sha=SHA,
            expected_metadata_sha256='a'*64, expected_runtime_sha256='b'*64,
            expected_main_pid=123, expected_post_metadata_sha256='c'*64,
            deploy_operation='wrong-owner')[-1]
        self.assertIn('--operation wrong-owner', shell)
        self.assertIn('--expected-sha ' + SHA, shell)
        result = subprocess.run(['sh', '-c', shell], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(owner.load(self.runtime)['phase'], 'mutation_started')

    def test_hosted_first_sync_uses_bootstrap_and_unknown_start_never_syncs(self):
        from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
        target = SimpleNamespace(target_dir=str(ROOT), ssh_destination='offline-test',
            environment_file=str(self.runtime / 'unused.env'), restart_command='restart-test',
            status_command='', runtime_env={'REGISTRY_UPLOAD_RUNTIME_DIR': str(self.runtime)})
        calls = []
        def execute(command, **kwargs):
            calls.append(command)
            if kwargs and fail_start[0] and "'start'" in command[-1]:
                raise subprocess.TimeoutExpired(command, 120)
        fail_start = [False]
        with ExitStack() as stack:
            for name in ('_ensure_target_allows_mutation', '_validate_managed_systemd_units'):
                stack.enter_context(patch.object(hosted, name))
            stack.enter_context(patch.object(hosted, '_missing_for_deploy', return_value=[]))
            stack.enter_context(patch.object(hosted, '_git_output', return_value=SHA))
            stack.enter_context(patch.object(hosted, '_remote_shell_command', side_effect=lambda target, shell: ['ssh', 'offline-test', shell]))
            stack.enter_context(patch.object(hosted, '_ssh_command', return_value=['ssh']))
            for name in dir(hosted):
                if name.startswith('_build_') and name.endswith('_command'):
                    stack.enter_context(patch.object(hosted, name, return_value=['ssh', 'offline-test', name]))
            stack.enter_context(patch.object(hosted, '_build_root_storage_policy_commands', return_value={'status': [], 'status_artifact_readback': []}))
            stack.enter_context(patch.object(hosted, '_build_managed_systemd_commands', return_value={
                key: [] for key in ('install', 'retire', 'daemon_reload', 'enable', 'restart', 'reconcile', 'preflight')}))
            stack.enter_context(patch.object(hosted, '_run_command', side_effect=execute))
            stack.enter_context(patch.object(hosted.subprocess, 'run', side_effect=execute))
            stack.enter_context(patch.dict('os.environ', {owner.ENV_OPERATION: OP,
                'WB_CORE_RELEASE_DEFER_DEPLOY_OWNER_FINISH': 'false'}))
            hosted.deploy_current_checkout(target, target_file=None, dry_run=False, allow_dirty=True)
            self.assertEqual(calls[0][-1], '_build_auth_env_preflight_command')
            self.assertIn("'claim'", calls[1][-1])
            self.assertIn("'start'", calls[2][-1])
            self.assertIn('deploy_owner_bootstrap', calls[3][-1])  # first mkdir
            rsync = next(command for command in calls if command[0] == 'rsync')
            self.assertIn('deploy_owner_bootstrap', rsync[rsync.index('--rsync-path') + 1])
            self.assertIn('python3 -m packages.application.business_data_deploy_protection check', calls[5][-1])
            self.assertIn('business_data_deploy_protection finish', calls[-1][-1])
            calls.clear()
            fail_start[0] = True
            with self.assertRaises(subprocess.TimeoutExpired):
                hosted.deploy_current_checkout(target, target_file=None, dry_run=False, allow_dirty=True)
            self.assertEqual(len(calls), 3)  # readonly auth, claim, ambiguous start

    def old_runner_evidence(self):
        from apps import github_release_runner as runner
        from apps.github_release_runner_smoke import plan
        checked = plan()
        checked['release_kind'] = 'live_runtime'
        checked.pop('plan_sha256')
        checked['plan_sha256'] = runner.sha256(runner.canonical_bytes(checked))
        run = dict(id=10, name=runner.WORKFLOW_NAME, path=runner.WORKFLOW_PATH,
            event='pull_request', run_attempt=1, status='completed', conclusion='success',
            repository={'full_name': runner.REPOSITORY}, head_sha=checked['head_sha'],
            pull_requests=[{'number': 7}])
        event = {'repository': {'full_name': runner.REPOSITORY}, 'workflow_run': deepcopy(run)}
        run['pull_requests'] = []  # actual GitHub post-merge API behavior
        path = self.runtime / 'workflow-run-event.json'
        path.write_text(json.dumps(event))
        env = {'GITHUB_EVENT_PATH': str(path), 'GITHUB_TOKEN': 'offline-fixture-token',
            'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_REPOSITORY': runner.REPOSITORY,
            'GITHUB_WORKFLOW': 'Release Runner', 'WB_CORE_RELEASE_PR': '7',
            'WB_CORE_RELEASE_HEAD': checked['head_sha']}
        payloads = {'run': run, 'plan': checked, 'event': event,
            'jobs': {'jobs': [dict(name=name, status='completed', conclusion='success')
                for name in ('Core', 'Plan', 'Checks', 'pr-gate')]},
            'pr': dict(number=7, merged=True, merge_commit_sha=SHA,
                head={'sha': checked['head_sha'], 'repo': {'full_name': runner.REPOSITORY}},
                base={'ref': 'main', 'repo': {'full_name': runner.REPOSITORY}}),
            'commit': {'sha': SHA, 'parents': [{'sha': checked['base_sha']}]}}
        requests = []
        def request(client, method, endpoint, body=None, *, raw=False):
            self.assertEqual(method, 'GET')
            self.assertIsNone(body)
            self.assertEqual(client.repository, runner.REPOSITORY)
            requests.append(endpoint)
            if endpoint == '/actions/runs/10':
                return payloads['run']
            if endpoint == '/actions/runs/10/artifacts?per_page=100':
                return {'artifacts': [{'id': 12, 'name': 'check-plan-exact', 'expired': False}]}
            if endpoint == '/actions/artifacts/12/zip':
                self.assertTrue(raw)
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, 'w') as archive:
                    archive.writestr('check-plan.json', json.dumps(payloads['plan']))
                return stream.getvalue()
            if endpoint == '/actions/runs/10/jobs?filter=latest&per_page=100':
                return payloads['jobs']
            if endpoint == '/pulls/7':
                return payloads['pr']
            if endpoint == '/git/commits/' + SHA:
                return payloads['commit']
            raise AssertionError('unexpected bootstrap request: ' + endpoint)
        canonical = runner.operation_id(10, 7, checked['base_sha'], checked['head_sha'], checked['plan_sha256'])
        return runner, env, payloads, requests, request, canonical

    def test_old_runner_canonical_identity_retains_same_owner_through_supported_recovery(self):
        from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
        from ci.post_merge_release_recovery import RecoveryCase
        runner, env, payloads, requests, request, canonical = self.old_runner_evidence()
        recovery, value, client, target, effects, calls, flags, original, fp, recovery_stack = self.recovery_fixture(
            RecoveryCase.STORAGE_TAIL, operation=canonical)
        old_receipt = runner.receipt(state='blocked', run_id=10, pr=7,
            base=payloads['plan']['base_sha'], head=payloads['plan']['head_sha'],
            plan=payloads['plan'], merge=SHA, reason='fixture-interruption')
        self.assertEqual(old_receipt['operation_id'], canonical)
        self.assertEqual(value['source']['original_operation_id'], old_receipt['operation_id'])
        target.status_command = ''
        target.restart_command = 'offline-restart'
        remote_calls, stage_calls = [], []
        def remote_owner(command, **kwargs):
            remote_calls.append(command)
            self.assertEqual(kwargs, {'timeout': 120, 'check': True})
            words = shlex.split(command[-1])
            arguments = ast.literal_eval(words[-1].rsplit("namespace['main'](", 1)[1][:-2])
            self.assertEqual(arguments[arguments.index('--operation') + 1], canonical)
            if arguments[0] == 'claim':
                owner.claim(self.runtime, canonical, quiet_reader=self.quiet)
            else:
                self.assertEqual(arguments[0], 'start')
                owner.start(self.runtime, canonical, SHA)
            return subprocess.CompletedProcess(command, 0)
        def stage(command):
            stage_calls.append(command)
            if command[0] == 'ssh' and '_build_deploy_metadata_command' in command[-1]:
                raise RuntimeError('fixture interruption after protected sync')
        with ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ', env, clear=True))  # OLD Runner: neither new env variable exists
            stack.enter_context(patch.object(runner.GitHub, 'request', request))
            stack.enter_context(patch.object(runner, 'trusted_main_sha', return_value=SHA))
            stack.enter_context(patch.object(hosted, '_git_output', return_value=SHA))
            for name in ('_ensure_target_allows_mutation', '_validate_managed_systemd_units'):
                stack.enter_context(patch.object(hosted, name))
            stack.enter_context(patch.object(hosted, '_missing_for_deploy', return_value=[]))
            stack.enter_context(patch.object(hosted, '_ssh_command', return_value=['ssh']))
            stack.enter_context(patch.object(hosted, '_remote_shell_command', side_effect=lambda target, shell: ['ssh', 'offline-test', shell]))
            for name in dir(hosted):
                if name.startswith('_build_') and name.endswith('_command'):
                    stack.enter_context(patch.object(hosted, name, return_value=['ssh', 'offline-test', name]))
            stack.enter_context(patch.object(hosted, '_build_root_storage_policy_commands', return_value={'status': [], 'status_artifact_readback': []}))
            stack.enter_context(patch.object(hosted, '_build_managed_systemd_commands', return_value={
                key: [] for key in ('install', 'retire', 'daemon_reload', 'enable', 'restart', 'reconcile', 'preflight')}))
            stack.enter_context(patch.object(hosted, '_run_command', side_effect=stage))
            stack.enter_context(patch.object(hosted.subprocess, 'run', side_effect=remote_owner))
            with patch.dict('os.environ', {**env, 'GITHUB_EVENT_PATH': ''}, clear=True):
                with self.assertRaises(runner.RunnerError):
                    hosted.deploy_current_checkout(target, target_file=None, dry_run=False, allow_dirty=True)
            self.assertEqual(remote_calls, [])
            self.assertEqual(stage_calls, [])
            self.assertIsNone(owner.load(self.runtime))
            with self.assertRaisesRegex(RuntimeError, 'fixture interruption'):
                hosted.deploy_current_checkout(target, target_file=None, dry_run=False, allow_dirty=True)
        self.assertEqual(len(requests), 6)
        prepared = owner.load(self.runtime)
        self.assertEqual(prepared['operation_id'], canonical)
        self.assertEqual(prepared['phase'], 'mutation_started')
        self.assertEqual(recovery.apply_recovery(client, value, fp, target)['state'], 'complete')
        self.assertEqual(owner.load(self.runtime)['operation_id'], canonical)
        self.assertEqual(owner.load(self.runtime)['phase'], 'complete')
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])
        recovery_stack.close()

    def test_old_runner_missing_or_mismatched_evidence_never_falls_back_to_manual_owner(self):
        from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
        runner, env, payloads, requests, request, canonical = self.old_runner_evidence()
        original_payloads, original_env = deepcopy(payloads), dict(env)
        def checked_plan_change(payloads, field, value):
            checked = payloads['plan']
            checked.pop('plan_sha256')
            checked[field] = value
            checked['plan_sha256'] = runner.sha256(runner.canonical_bytes(checked))
        cases = [
            ('missing-event', lambda p, e: e.pop('GITHUB_EVENT_PATH')),
            ('missing-token', lambda p, e: e.pop('GITHUB_TOKEN')),
            ('partial-release-context', lambda p, e: e.pop('WB_CORE_RELEASE_HEAD')),
            ('wrong-trigger', lambda p, e: e.update(GITHUB_EVENT_NAME='workflow_dispatch')),
            ('wrong-repository', lambda p, e: e.update(GITHUB_REPOSITORY='other/repository')),
            ('wrong-runner', lambda p, e: e.update(GITHUB_WORKFLOW='Other Workflow')),
            ('event-repository', lambda p, e: p['event']['repository'].update(full_name='other/repository')),
            ('event-run-head', lambda p, e: p['event']['workflow_run'].update(head_sha='f' * 40)),
            ('event-run-pr', lambda p, e: p['event']['workflow_run'].update(pull_requests=[{'number': 8}])),
            ('run-workflow', lambda p, e: p['run'].update(path='wrong.yml')),
            ('run-not-successful', lambda p, e: p['run'].update(conclusion='failure')),
            ('run-retried', lambda p, e: p['run'].update(run_attempt=2)),
            ('run-head', lambda p, e: p['run'].update(head_sha='f' * 40)),
            ('run-pr-links', lambda p, e: p['run'].update(pull_requests=[{'number': 8}])),
            ('plan-head', lambda p, e: p['plan'].update(head_sha='f' * 40)),  # also invalidates checked digest
            ('plan-hash', lambda p, e: p['plan'].update(plan_sha256='f' * 64)),
            ('checked-plan-head', lambda p, e: checked_plan_change(p, 'head_sha', 'f' * 40)),
            ('checked-plan-base', lambda p, e: checked_plan_change(p, 'base_sha', 'f' * 40)),
            ('checked-plan-pr', lambda p, e: checked_plan_change(p, 'pull_request', 8)),
            ('checked-plan-kind', lambda p, e: checked_plan_change(p, 'release_kind', 'repo_only')),
            ('jobs-not-successful', lambda p, e: p['jobs']['jobs'][0].update(conclusion='failure')),
            ('unmerged-pr', lambda p, e: p['pr'].update(merged=False)),
            ('pr-merge', lambda p, e: p['pr'].update(merge_commit_sha='f' * 40)),
            ('pr-head', lambda p, e: p['pr']['head'].update(sha='f' * 40)),
            ('merge-parent', lambda p, e: p['commit'].update(parents=[{'sha': 'f' * 40}])),
            ('merge-sha', lambda p, e: p['commit'].update(sha='f' * 40)),
        ]
        for label, mutate in cases:
            with self.subTest(label=label):
                payloads.clear()
                payloads.update(deepcopy(original_payloads))
                evidence_env = dict(original_env)
                mutate(payloads, evidence_env)
                Path(original_env['GITHUB_EVENT_PATH']).write_text(json.dumps(payloads['event']))
                with patch.dict('os.environ', evidence_env, clear=True), \
                        patch.object(runner.GitHub, 'request', request), \
                        patch.object(runner, 'trusted_main_sha', return_value=SHA), \
                        patch.object(owner, 'claim') as claim:
                    with self.assertRaises(runner.RunnerError):
                        hosted.resolve_deploy_operation(SHA)
                    claim.assert_not_called()
                self.assertFalse((self.runtime / owner.FILENAME).exists())
        payloads.clear()
        payloads.update(deepcopy(original_payloads))
        payloads['event']['workflow_run']['pull_requests'] = []
        Path(original_env['GITHUB_EVENT_PATH']).write_text(json.dumps(payloads['event']))
        with patch.dict('os.environ', original_env, clear=True), \
                patch.object(runner.GitHub, 'request', request), \
                patch.object(runner, 'trusted_main_sha', return_value=SHA):
            self.assertEqual(hosted.resolve_deploy_operation(SHA), canonical)
        with patch.dict('os.environ', original_env, clear=True), \
                patch.object(runner.GitHub, 'request', request), \
                patch.object(runner, 'trusted_main_sha', return_value='f' * 40):
            with self.assertRaises(runner.RunnerError):
                hosted.resolve_deploy_operation(SHA)
        with patch.dict('os.environ', original_env, clear=True), \
                patch.object(runner.GitHub, 'request', side_effect=TimeoutError('offline timeout')):
            with self.assertRaisesRegex(runner.RunnerError, 'bootstrap-github-evidence-unavailable'):
                hosted.resolve_deploy_operation(SHA)
        self.assertFalse((self.runtime / owner.FILENAME).exists())
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(hosted.resolve_deploy_operation(SHA), 'manual-deploy-' + SHA)
        with patch.dict('os.environ', {owner.ENV_OPERATION: OP}, clear=True):
            self.assertEqual(hosted.resolve_deploy_operation(SHA), OP)


def main():
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(ProtectionTests))
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == '__main__':
    main()
