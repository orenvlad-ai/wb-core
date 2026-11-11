"""Incident recovery refusal, durability and canonical-tail contract checks."""
from __future__ import annotations

import ast
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ci import pr1615_openrsync_recovery as r


class RecoverySmoke(unittest.TestCase):
    def test_manifest_no_symlink_traversal_and_rsync_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            (root / '.git' / 'secret').write_text('excluded')
            (root / 'tracked').write_text('old')
            (root / 'link').symlink_to('/nonexistent/never-traverse')
            result = r.manifest(root)
            self.assertEqual(set(result), {'tracked', 'link'})
            self.assertEqual(result['link']['kind'], 'symlink')
            expected = {'tracked': {'kind': 'file', 'size': 3, 'sha256': r.digest(b'old'), 'executable': False},
                        'link': {'kind': 'symlink', 'target': '/nonexistent/never-traverse'}}
            r.verify_old(result, expected)
            (root / 'tracked').write_text('new')
            with self.assertRaisesRegex(RuntimeError, 'old-tree-drift'):
                r.verify_old(r.manifest(root), expected)
            (root / 'tracked').write_text('old')
            (root / 'unknown-production-file').write_text('never-delete-unreviewed')
            with self.assertRaisesRegex(RuntimeError, 'unexpected-deletable-files'):
                r.verify_old(r.manifest(root), expected)

    def test_claim_is_private_exclusive_and_persistent_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claim'
            r.durable_create(path, {'fingerprint': 'first'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                r.durable_create(path, {'fingerprint': 'second'})
            self.assertEqual(json.loads(r.read_private(path)), {'fingerprint': 'first'})
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                r.read_private(link)

    def test_owner_prestate_claim_revalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / 'app'
            state = Path(directory) / 'state'
            app.mkdir(); state.mkdir()
            helper = app / 'packages/application/business_data_deploy_protection.py'
            helper.parent.mkdir(parents=True)
            helper.write_text('old helper')
            (app / '.wb-core-runtime-sha').write_text(r.BASE)
            (app / '.wb-core-deploy.json').write_text(json.dumps({'commit': r.BASE, 'deployment_complete': True}))
            owner = {'fingerprint': r.OWNER_FP, 'phase': 'mutation_started', 'window_id': r.WINDOW,
                     'baseline_fingerprint': r.BASELINE}
            deploy = types.SimpleNamespace(_locked=lambda _: nullcontext(), require_owner=lambda *_: owner,
                _clients=lambda *_: (None, lambda: {'quiet': True}), load=lambda _: owner)
            pause = types.SimpleNamespace(load_state=lambda _: {}, _unchanged=lambda *_: None)
            modules = {'packages.application.business_data_deploy_protection': deploy,
                       'packages.application.business_data_maintenance_pause': pause}
            # Package attributes must also be replaced because Python caches them.
            import packages.application
            with patch.dict(sys.modules, modules), patch.object(packages.application, 'business_data_deploy_protection', deploy, create=True), \
                 patch.object(packages.application, 'business_data_maintenance_pause', pause, create=True), \
                 patch.object(r, 'APP', str(app)), patch.object(r, 'STATE', str(state)):
                expected = {'packages/application/business_data_deploy_protection.py':
                    {'kind': 'file', 'size': 10, 'sha256': r.digest(b'old helper'), 'executable': False}}
                evidence = {'action': 'preview', 'old_files': expected}
                proof = r.remote_action(evidence)
                self.assertFalse((state / r.CLAIM).exists())
                bad = {**evidence, 'action': 'claim', 'prestate': {}, 'fingerprint': 'reviewed'}
                with self.assertRaisesRegex(RuntimeError, 'prestate-changed'):
                    r.remote_action(bad)
                self.assertFalse((state / r.CLAIM).exists())
                r.remote_action({**bad, 'prestate': proof})
                self.assertTrue((state / r.CLAIM).exists())
                with self.assertRaisesRegex(RuntimeError, 'existing-claim-readback-only'):
                    r.remote_action({**bad, 'prestate': proof})
                self.assertEqual(r.remote_action({'action': 'readback'})['runtime_sha']['value'], r.BASE)
                (app / '.wb-core-runtime-sha').unlink()
                (app / '.wb-core-deploy.json').write_text('{broken')
                observed = r.remote_action({'action': 'readback'})
                self.assertEqual(observed['runtime_sha']['state'], 'absent')
                self.assertEqual(observed['metadata']['state'], 'unreadable-or-invalid')
                self.assertIsNotNone(observed['claim'])
                with self.assertRaisesRegex(RuntimeError, 'claim-mismatch'):
                    r.remote_action({'action': 'event', 'fingerprint': 'other', 'event': {}})

    def test_remote_source_is_standalone_parseable(self):
        compile(r.remote_script({'action': 'readback'}), '<remote>', 'exec')

    def test_command_fingerprint_only_normalizes_metadata_clock(self):
        a = ['ssh', 'host', 'printf \'{"deployed_at":"2026-11-11T12:00:00Z","commit":"abc"}\'']
        b = ['ssh', 'host', a[-1].replace('12:00:00', '13:00:00')]
        self.assertEqual(r.command_fingerprint('metadata', a), r.command_fingerprint('metadata', b))
        self.assertNotEqual(r.command_fingerprint('metadata', a), r.command_fingerprint('metadata',
                            [*b[:-1], b[-1].replace('abc', 'def')]))
        self.assertNotEqual(r.command_fingerprint('restart', a), r.command_fingerprint('restart', b))

    def test_existing_claim_bypasses_all_mutation_planning(self):
        args = types.SimpleNamespace(checkout=ROOT, target_file=ROOT / 'target', action='apply')
        readback = {'claim': '{}', 'journal': None}
        with patch.object(r, 'bind_checkout', return_value=(None, None, None, None)), \
             patch.object(r, 'remote', return_value=readback) as remote, \
             patch.object(r, 'local_evidence', side_effect=AssertionError('must not re-plan')):
            self.assertEqual(r.run(args)['state'], 'readback-only')
            self.assertEqual(remote.call_count, 1)
            self.assertEqual(remote.call_args.args[-1], {'action': 'readback'})

    def test_standalone_source_required_before_import_or_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.git').write_text('gitdir: elsewhere')
            with self.assertRaisesRegex(RuntimeError, 'standalone-source-checkout-required'):
                r.bind_checkout(root, root / r.TARGET_REL)

    def test_real_metadata_builder_fingerprint_is_stable(self):
        from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
        target = types.SimpleNamespace(target_dir=r.APP, ssh_destination='test')
        first = hosted._build_deploy_metadata_command(target, deployment_complete=False)
        second = hosted._build_deploy_metadata_command(target, deployment_complete=False)
        self.assertNotEqual(first, second)
        self.assertEqual(r.command_fingerprint('metadata', first), r.command_fingerprint('metadata', second))

    def test_stage_failure_burns_claim_and_never_repeats_sync(self):
        self._claim_or_stage_failure(False)

    def test_claim_transport_timeout_is_ambiguous_and_never_syncs(self):
        self._claim_or_stage_failure(True)

    def _claim_or_stage_failure(self, claim_timeout):
        with tempfile.TemporaryDirectory() as directory:
            target_file = Path(directory) / 'target'
            target_file.write_text('{}')
            args = types.SimpleNamespace(checkout=ROOT, target_file=target_file, action='preview', fingerprint='')
            claim = []
            def remote(_hosted, _target, payload):
                if payload['action'] == 'readback':
                    return {'claim': 'partial-or-complete-claim' if claim else None}
                if payload['action'] == 'claim':
                    claim.append(payload)
                    if claim_timeout:
                        raise subprocess.TimeoutExpired(['ssh', 'claim'], 180)
                return {'manifest': {}}
            hosted = types.SimpleNamespace(_ssh_command=lambda: ['ssh'],
                _build_auth_env_preflight_command=lambda _: None,
                _build_root_storage_policy_commands=lambda _: {},
                _run_command=unittest.mock.Mock(side_effect=subprocess.CalledProcessError(2, ['sync'])))
            deploy = types.SimpleNamespace(guarded_shell=lambda cmd, **_: cmd)
            with patch.object(r, 'bind_checkout', return_value=(hosted, None, deploy, None)), \
                 patch.object(r, 'remote', side_effect=remote), \
                 patch.object(r, 'local_evidence', return_value={}), \
                 patch.object(r, 'canonical_stages', side_effect=lambda *_: [('sync', ['rsync', 'SOURCE', 'TARGET'])]), \
                 patch.object(r, 'manifest', return_value={}), \
                 patch.object(r, 'old_files', return_value={}), \
                 patch.object(r.subprocess, 'run', return_value=types.SimpleNamespace(stdout='.wb-core-runtime-sha')):
                preview = r.run(args)
                args.action = 'apply'
                args.fingerprint = preview['fingerprint']
                receipt = r.run(args)
                self.assertEqual(receipt['state'], 'ambiguous')
                self.assertEqual(len(claim), 1)
                self.assertEqual(hosted._run_command.call_count, 0 if claim_timeout else 1)
                self.assertEqual(r.run(args)['state'], 'readback-only')
                self.assertEqual(hosted._run_command.call_count, 0 if claim_timeout else 1)

    def test_canonical_order_matches_real_deploy_tail(self):
        # Execute only canonical command construction and capture stage ordering
        # from an AST-derived read-only trace, never the production function.
        from apps import registry_upload_http_entrypoint_hosted_runtime as hosted
        source = ast.parse(__import__('inspect').getsource(hosted.deploy_current_checkout))
        function = source.body[0]
        tail = function.body
        sync_index = next(i for i, node in enumerate(tail) if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
            and node.value.func.id == 'run_stage' and isinstance(node.value.args[0], ast.Constant)
            and node.value.args[0].value == 'sync')
        expected = []
        for node in tail[sync_index:]:
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    if call.func.id == 'run_stage':
                        expected.append(call.args[0].value)
                    elif call.func.id == '_run_deploy_status_readback':
                        expected.append('status-readback')
        # Fake all optional commands present so the builder traverses all branches.
        keys = ('rsync chown_target_dir systemd_barrier_preflight systemd_retire systemd_daemon_reload '
            'deploy_metadata root_storage_status seller_portal_recovery_os_dependencies '
            'seller_portal_owner_runtime_os_dependencies runtime_pip_install seller_portal_recovery_venv '
            'seller_portal_owner_runtime_venv seller_portal_owner_runtime_contract '
            'seller_portal_recovery_playwright_browser buyer_chrome_install autoanswers_os_dependencies '
            'autoanswers_node_dependencies autoanswers_prepare_deploy systemd_install nginx_public_routes_update '
            'restart systemd_reconcile root_storage_status_artifact_readback status auth_env_preflight '
            'change_registry_activation cleaner_precomplete_probe deploy_completion_metadata').split()
        fake = types.SimpleNamespace(deploy_current_checkout=lambda *_a, **_k: {'commands': {k: [k] for k in keys}},
                                     _build_root_storage_policy_commands=lambda _: {'release_bridge': True})
        actual = [stage for stage, _ in r.canonical_stages(fake, None, None)]
        self.assertEqual(actual, expected)
        self.assertNotIn('mkdir', actual)
        self.assertEqual(actual.count('sync'), 1)
        self.assertEqual(actual[-1], 'metadata-complete')


if __name__ == '__main__':
    unittest.main()
