#!/usr/bin/env python3
"""Native synthetic pause/owner/formula proof; no production connections."""
from pathlib import Path
import json
import shlex
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.business_data_formula_resume_smoke import FormulaResumeTests, OP
from packages.application import business_data_maintenance_pause as pause
from packages.application import business_data_write_barrier as barrier
from packages.application import business_data_deploy_protection as deploy
from ci import held_formula_deploy_claim as claim


class HeldClaimTests(unittest.TestCase):
    setUp = FormulaResumeTests.setUp

    def claim(self, operation='next-deploy'):
        return claim.claim(self.runtime, operation, app_dir=self.app,
            unit_directory=self.units, **self.options)

    def test_claim_retains_exact_proof_without_resume(self):
        before = {name: (self.runtime / name).read_bytes()
            for name in (pause.STATE_FILENAME, barrier.STATE_FILENAME)}
        result = self.claim()
        self.assertEqual(result['phase'], 'prepared')
        self.assertEqual(result['operation_id'], 'next-deploy')
        proof = json.loads((self.runtime / claim.FILENAME).read_text())
        self.assertEqual(proof['plan']['authority']['owner']['operation_id'], OP)
        self.assertEqual(proof['plan']['baseline'], self.baseline)
        self.assertFalse((self.runtime / '.business-data-formula-resume').exists())
        self.assertTrue(pause.readback(self.runtime, **self.options)['quiet'])
        for name, data in before.items():
            self.assertEqual((self.runtime / name).read_bytes(), data)

    def test_foreign_configuration_drift_cannot_claim(self):
        unit = pause.TIMERS[0]
        self.systemd.states[unit]['properties']['AccuracyUSec'] = '19s'
        owner = deploy.load(self.runtime)
        with self.assertRaises(RuntimeError): self.claim()
        self.assertEqual(deploy.load(self.runtime), owner)
        self.assertFalse((self.runtime / claim.FILENAME).exists())

    def test_modified_formula_source_cannot_claim(self):
        path = self.app / 'packages/application/fbs_snapshot_cost.py'
        path.write_bytes(path.read_bytes() + b'\n# unauthorized drift\n')
        with self.assertRaises(RuntimeError): self.claim()
        self.assertEqual(deploy.load(self.runtime)['phase'], 'complete')

    def test_terminal_same_operation_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'terminal'): self.claim(OP)

    def test_orphan_proof_is_not_overwritten(self):
        path = self.runtime / claim.FILENAME
        barrier._atomic_write_private_json(path, {'operation_id': 'other-deploy'})
        before = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'unconsumed'): self.claim()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(deploy.load(self.runtime)['phase'], 'complete')

    def test_transport_is_one_quoted_read_proof_and_claim(self):
        command = claim.remote_shell(app_dir="/app 'quoted", runtime_dir='/state',
            env_file='/private/env', operation='next-deploy')
        words = shlex.split(command)
        self.assertEqual(words[:2], ['python3', '-c'])
        compile(words[2], '<bootstrap>', 'exec')
        self.assertNotIn('formula.apply', words[2])


def run():
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(HeldClaimTests))
    if not result.wasSuccessful(): raise AssertionError('held formula deploy claim smoke failed')


if __name__ == '__main__': run()
