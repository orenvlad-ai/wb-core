#!/usr/bin/env python3
"""Exact held formula receipt is read-only and bound to one prepared owner."""
from pathlib import Path
from types import SimpleNamespace
import json
import io
from contextlib import redirect_stdout
import shlex
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.business_data_formula_resume_smoke import FormulaResumeTests, OP, SHA
from packages.application import business_data_maintenance_pause as pause
from packages.application import business_data_write_barrier as barrier
from packages.application import business_data_deploy_protection as deploy
from packages.application import business_data_formula_resume as formula
from packages.application import business_data_held_formula as held
from ci import held_formula_deploy_claim as bootstrap


class HeldFormulaTests(unittest.TestCase):
    setUp = FormulaResumeTests.setUp

    def context(self):
        return dict(app_dir=self.app, unit_directory=self.units, **self.options)

    def check(self):
        return held.unchanged(self.runtime, pause.load_state(self.runtime),
            pause.readback(self.runtime, **self.options), **self.context())

    def first_claim(self):
        return bootstrap.claim(self.runtime, 'next-deploy', **self.context())

    def native_claim(self, operation='next-deploy'):
        return deploy.claim(self.runtime, operation,
            quiet_reader=lambda: pause.readback(self.runtime, **self.options), formula_context=self.context())

    def assert_held(self, before):
        for name, data in before.items():
            self.assertEqual((self.runtime / name).read_bytes(), data)
        self.assertTrue(pause.readback(self.runtime, **self.options)['quiet'])
        self.assertFalse((self.runtime / formula.DIRECTORY).exists())

    def held_bytes(self):
        return {name: (self.runtime / name).read_bytes()
            for name in (pause.STATE_FILENAME, barrier.STATE_FILENAME)}

    def test_completed_then_prepared_are_read_only(self):
        before = self.held_bytes()
        self.assertEqual(self.check()['authority']['owner']['operation_id'], OP)
        owner = self.first_claim()
        self.assertEqual(self.native_claim(), owner)
        self.assertEqual(self.check()['authority']['owner']['operation_id'], OP)
        self.assert_held(before)

    def test_native_new_claim_retains_same_receipt(self):
        self.native_claim()
        saved = (self.runtime / held.FILENAME).read_bytes()
        self.native_claim()
        self.assertEqual((self.runtime / held.FILENAME).read_bytes(), saved)

    def test_different_prepared_owner_cannot_reuse_receipt(self):
        self.first_claim()
        deploy._save(self.runtime, dict(deploy.load(self.runtime), operation_id='foreign-deploy'))
        with self.assertRaisesRegex(RuntimeError, 'binding differs'): self.check()

    def test_mutation_started_cannot_reuse_premerge_receipt(self):
        self.first_claim()
        deploy.start(self.runtime, 'next-deploy', 'b' * 40)
        with self.assertRaisesRegex(RuntimeError, 'binding differs'): self.check()

    def test_receipt_modification_and_installed_drift_fail_closed(self):
        self.first_claim()
        path = self.runtime / held.FILENAME
        saved = path.read_bytes()
        proof = json.loads(saved); proof['plan']['expected_sha'] = 'c' * 40
        barrier._atomic_write_private_json(path, proof)
        with self.assertRaises(RuntimeError): self.check()
        path.write_bytes(saved)
        source = self.app / 'packages/application/fbs_snapshot_cost.py'
        source.write_bytes(source.read_bytes() + b'\n# drift\n')
        with self.assertRaises(RuntimeError): self.check()

    def test_next_complete_formula_deploy_reproves_original_baseline(self):
        before = self.held_bytes()
        self.first_claim(); self.native_claim()
        next_sha = 'b' * 40
        deploy.start(self.runtime, 'next-deploy', next_sha)
        contract_path = self.app / formula.CONTRACT_RELATIVE
        contract = json.loads(contract_path.read_text())
        source = self.app / 'packages/application/fbs_snapshot_cost.py'
        source.write_bytes(source.read_bytes() + b'\n# second completed deploy\n')
        contract['formula_code_hashes']['packages/application/fbs_snapshot_cost.py'] = formula._hash(source.read_bytes())
        epoch = 'wbc0069k16-reviewed-native-v1:' + formula._hash(json.dumps(
            contract['formula_code_hashes'], sort_keys=True, separators=(',', ':')).encode())
        contract['formula_epoch'] = epoch; contract_path.write_text(json.dumps(contract))
        for path in (self.app / formula.UNIT_RELATIVE, self.units / formula.UNIT):
            path.write_bytes(path.read_bytes().replace(self.new_epoch.encode(), epoch.encode()))
        props = self.systemd.states[formula.UNIT]['properties']
        props['ExecStart'] = props['ExecStart'].replace(self.new_epoch, epoch)
        (self.app / '.wb-core-runtime-sha').write_text(next_sha + '\n')
        (self.app / '.wb-core-deploy.json').write_text(json.dumps(dict(
            schema_version='wb_core_deploy_metadata_v2', commit=next_sha, deployment_complete=True)))
        deploy.finish(self.runtime, 'next-deploy', next_sha, app_dir=self.app,
            registry_reader=lambda: dict(active_state='active',main_pid=42))
        plan = self.check()
        self.assertEqual(plan['delta']['old_epoch'], self.old_epoch)
        self.assertEqual(plan['delta']['new_epoch'], epoch)
        self.assertEqual(plan['authority']['owner']['operation_id'], 'next-deploy')
        self.native_claim('third-deploy')
        self.assertEqual(self.check()['authority']['owner']['operation_id'], 'next-deploy')
        self.assert_held(before)

    def test_presync_claim_executes_with_proof_modules_not_installed(self):
        self.first_claim()
        before = self.held_bytes()
        owner = deploy.load(self.runtime)
        # Fixture app has no new runtime proof module. Execute the actual
        # transported source, changing only the synthetic systemd path.
        self.assertFalse((self.app / 'packages/application/business_data_held_formula.py').exists())
        command = deploy.remote_shell('claim', app_dir=self.app, runtime_dir=self.runtime,
            env_file=self.root / 'private.env', operation='next-deploy')
        source = shlex.split(command)[-1].replace('/etc/systemd/system', str(self.units))
        import apps.business_data_maintenance as maintenance
        import packages.application as application
        with patch.dict(sys.modules), \
                patch.object(application, 'business_data_formula_resume', formula), \
                patch.object(application, 'business_data_held_formula', held), \
                patch.object(maintenance, 'SystemdClient', return_value=self.systemd), \
                patch.object(maintenance, 'RuntimeScheduleClient', return_value=SimpleNamespace(_request=lambda path:self.activity())), \
                patch.object(maintenance, '_read_env_file', return_value={}), \
                patch.object(maintenance, '_build_web_auth_cookie', return_value=''), \
                patch.object(pause, '_writer_processes', return_value=[]), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                exec(source, {})
            self.assertEqual(result.exception.code, 0)
        self.assertEqual(deploy.load(self.runtime), owner)
        self.assert_held(before)

    def test_claim_transport_contains_native_proof_before_sync(self):
        command = deploy.remote_shell('claim', app_dir=self.app, runtime_dir=self.runtime,
            env_file=self.root / 'private.env', operation='next-deploy')
        source = shlex.split(command)[-1]
        compile(source, '<owner-bootstrap>', 'exec')
        self.assertIn('business_data_held_formula', source)
        self.assertIn('_completed_owner', source)
        self.assertLess(len(command.encode()), 128 * 1024)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(HeldFormulaTests)
    result = unittest.TextTestRunner().run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
