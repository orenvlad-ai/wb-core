#!/usr/bin/env python3
"""Activation CAS, no physical writes and atomic policy failure contracts."""
from contextlib import nullcontext
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import fbo_supply_lifecycle_smoke as fixtures
from apps.production_apply_contract import AdapterError
from apps.production_apply_launcher import execute
from packages.application import fbo_lifecycle_activation as module
from packages.application import fbo_supply_lifecycle as core
from packages.application.fulfillment_services import _ensure_schema


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.runtime.runtime_dir.resolve()
        self.db = self.fixture.runtime.db_path.resolve()
        with self.fixture.conn() as conn:
            _ensure_schema(conn)
        self.request = dict(actor='fixture', approval_reference='test authorization',
                            supply_ids=['1', '2'], cutoff_at=fixtures.NOW)
        self.adapter = module.FboLifecycleActivationAdapter()
        for target, replacement in (
            ('target', lambda request: (self.root, self.db)),
            ('_held_binding', lambda *a: {'window': 'same'}),
        ):
            patcher = patch.object(module.shared, target, replacement)
            patcher.start(); self.addCleanup(patcher.stop)
        for target, replacement in (('admitted', lambda *a: nullcontext()),
                                    ('require_held_activation', lambda *a: {'window': 'same'})):
            patcher = patch.object(module, target, replacement)
            patcher.start(); self.addCleanup(patcher.stop)

    def run_apply(self, operation, preview):
        return execute(action='apply', adapter_name='fixture', operation_id=operation,
                       request=self.request, expected_prestate=preview['prestate_sha256'],
                       expected_candidate=preview['candidate_sha256'], adapters={'fixture': self.adapter})

    def test_activate_once_with_two_policies_and_untouched_stock(self):
        operation = 'fixture-cutover-0001'
        preview = self.adapter.preview(self.request, operation)
        with self.fixture.conn() as conn:
            before = module.protected(conn)
        result = self.run_apply(operation, preview)
        self.assertEqual(result['state'], 'applied')
        self.assertTrue(result['readback']['forward_cost_policy_active'])
        self.assertTrue(result['readback']['protected_scope_unchanged_now'])
        with self.fixture.conn() as conn:
            self.assertEqual(module.protected(conn), before)
        self.assertEqual(self.fixture.service.detail('1')['status'], 'missing_source')
        with patch.object(self.adapter, 'apply', side_effect=AssertionError('second submit')):
            # Launcher resolves already-completed identity without a new submit.
            self.assertEqual(self.run_apply(operation, preview)['state'], 'applied')

    def test_source_drift_refuses_before_submit(self):
        operation = 'fixture-cutover-drift'
        preview = self.adapter.preview(self.request, operation)
        self.fixture.seed('1', [(101, 7)])
        with self.assertRaises(Exception):
            self.run_apply(operation, preview)
        self.assertEqual(self.adapter.readback(self.request, operation)['state'], 'not_submitted')

    def test_policy_failure_rolls_back_cutover_and_never_resubmits(self):
        from packages.application import fbo_forward_costs
        operation = 'fixture-cutover-failed'
        preview = self.adapter.preview(self.request, operation)
        original = fbo_forward_costs.activate
        def fail_after_policy(conn, activated_at, **kwargs):
            original(conn, activated_at, **kwargs)
            raise RuntimeError('failure after baseline insertion')
        with patch.object(fbo_forward_costs, 'activate', fail_after_policy):
            result = self.run_apply(operation, preview)
        self.assertEqual(result['state'], 'ambiguous')
        with self.fixture.conn() as conn:
            self.assertFalse(core.CONFIG in core._tables(conn) and conn.execute(f'SELECT 1 FROM {core.CONFIG}').fetchone())
            self.assertFalse(fbo_forward_costs.active(conn))
        with patch.object(self.adapter, 'apply', side_effect=AssertionError('second submit')):
            with self.assertRaises(Exception):
                self.run_apply(operation, preview)

    def failed_original(self):
        """The original production runtime already has empty lifecycle schema."""
        operation = 'fixture-cutover-recovery'
        with self.fixture.conn() as conn:
            core.ensure_schema(conn)
        preview = self.adapter.preview(self.request, operation)
        with patch('packages.application.fbo_forward_costs.activate', side_effect=RuntimeError('private failure text')):
            self.assertEqual(self.run_apply(operation, preview)['state'], 'ambiguous')
        folder, claim = module.paths(self.root, operation)
        raw = claim.read_bytes()
        profile = dict(claim_sha256='sha256:' + hashlib.sha256(raw).hexdigest(),
                       prestate_sha256=preview['prestate_sha256'],
                       atomic_sources={name: preview['prepared']['code'][name] for name in (
                           'fbo_lifecycle_activation.py', 'fbo_supply_lifecycle.py', 'fbo_forward_costs.py')})
        patcher = patch.dict(module.RECOVERABLE, {operation: profile})
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(module, '_completed_deploy', return_value={'owner': 'completed-fixture-deploy'})
        patcher.start(); self.addCleanup(patcher.stop)
        self.request = {**self.request, 'window_id': 'fresh-held-window', 'recovery': {
            'contract': module.RECOVERY_CONTRACT, 'original_claim_sha256': profile['claim_sha256'],
            'approval_reference': 'separate reviewed recovery authorization'}}
        return operation, folder, claim, raw

    def test_recovery_after_business_progress_preserves_original_claim(self):
        operation, folder, claim, original = self.failed_original()
        # Normal activity between the dated rollback and the new held window.
        self.fixture.seed('2', [(101, 8), (102, 1)])
        with self.fixture.conn() as conn:
            conn.execute(f'UPDATE {core.BALANCES_TABLE} SET quantity=quantity+1')
            before = module.protected(conn)
        preview = self.adapter.preview(self.request, operation)
        self.assertNotEqual(preview['prepared']['protected'], json.loads(original)['preview']['prepared']['protected'])
        ready = self.adapter.readback(self.request, operation)
        self.assertEqual(ready['state'], 'not_submitted')
        self.assertEqual(ready['original_submission_state'], 'proven_rolled_back')
        from packages.application import fbo_forward_costs as costs
        real = costs.activate
        def prove_same_plan_after_lifecycle_admission(conn, at, *, expected_plan):
            self.assertEqual(costs.baseline_preview(conn), preview['prepared']['forward_cost_baseline'])
            self.assertEqual(expected_plan, preview['prepared']['forward_cost_baseline'])
            return real(conn, at, expected_plan=expected_plan)
        with patch.object(costs, 'activate', prove_same_plan_after_lifecycle_admission):
            result = self.run_apply(operation, preview)
        self.assertEqual(result['state'], 'applied')
        self.assertEqual(claim.read_bytes(), original)
        self.assertTrue((folder / 'recovery-submitted.json').is_file())
        self.assertTrue((folder / 'recovery-receipt.json').is_file())
        with self.fixture.conn() as conn:
            self.assertEqual(module.protected(conn), before)
        with patch.object(self.adapter, 'apply', side_effect=AssertionError('recovery repeated')):
            self.assertEqual(self.run_apply(operation, preview)['state'], 'applied')

    def test_recovery_scope_claim_and_registration_are_exact(self):
        operation, folder, claim, original = self.failed_original()
        valid = copy.deepcopy(self.request)
        for key, value in (('supply_ids', ['1']), ('cutoff_at', '2026-11-09T01:00:00Z')):
            self.request = {**valid, key: value}
            with self.assertRaisesRegex(AdapterError, 'recovery_scope_changed'):
                self.adapter.preview(self.request, operation)
        self.request = valid
        with self.assertRaisesRegex(AdapterError, 'recovery_not_registered'):
            self.adapter.preview(self.request, 'unregistered-operation')
        claim.write_bytes(original + b' ')
        with self.assertRaisesRegex(AdapterError, 'recovery_original_claim_changed'):
            self.adapter.preview(self.request, operation)
        self.assertFalse((folder / 'recovery-submitted.json').exists())

    def test_recovery_requires_empty_canonical_immutable_policy(self):
        operation, folder, claim, original = self.failed_original()
        changes = (
            f'DROP TRIGGER fbo_lifecycle_config_no_delete',
            f'INSERT INTO {core.CONFIG} VALUES(1,\'date\',\'digest\',\'actor\',\'now\')',
            f'INSERT INTO {core.CURRENT} VALUES(\'foreign\',\'foreign\',\'now\',\'foreign\',\'{{}}\')',
            f'INSERT INTO {core.EVENTS}(cache_key,state_revision,state_json,occurred_at) VALUES(\'foreign\',\'rev\',\'{{}}\',\'now\')',
            'CREATE TABLE sheet_vitrina_v1_fbo_forward_cost_config(slot INTEGER)',
        )
        for sql in changes:
            with self.subTest(sql=sql):
                conn = self.fixture.conn()
                try:
                    conn.execute('BEGIN'); conn.execute(sql)
                    with self.assertRaises(AdapterError):
                        module._rollback_proof(conn)
                finally:
                    conn.rollback(); conn.close()
        self.assertEqual(claim.read_bytes(), original)
        self.assertFalse((folder / 'recovery-submitted.json').exists())

    def test_second_uncertain_recovery_is_readback_only_with_sanitized_cause(self):
        operation, folder, claim, original = self.failed_original()
        preview = self.adapter.preview(self.request, operation)
        from packages.application import fbo_forward_costs as costs
        real = costs.activate
        def fail(conn, at, **kwargs):
            real(conn, at, **kwargs)
            conn.execute('SELECT * FROM nonexistent_private_secret_table')
        with patch.object(costs, 'activate', fail):
            result = self.run_apply(operation, preview)
        self.assertEqual(result['state'], 'ambiguous')
        failure = result['readback']['failure']
        self.assertEqual(failure['exception_type'], 'OperationalError')
        self.assertEqual(failure['sqlite_errorname'], 'SQLITE_ERROR')
        self.assertNotIn('private_secret', json.dumps(failure))
        with patch.object(self.adapter, 'apply', side_effect=AssertionError('unsafe second recovery')):
            with self.assertRaisesRegex(Exception, 'operation-not-ready'):
                self.run_apply(operation, preview)
        self.assertEqual(claim.read_bytes(), original)
        self.assertFalse((folder / 'recovery-receipt.json').exists())
        with self.fixture.conn() as conn:
            module._rollback_proof(conn)

    def test_recovery_requires_fresh_preview_and_completed_deploy(self):
        operation, folder, claim, original = self.failed_original()
        with patch.object(module, '_completed_deploy', side_effect=AdapterError('fbo_activation_recovery_completed_deploy_required')):
            with self.assertRaisesRegex(AdapterError, 'completed_deploy_required'):
                self.adapter.preview(self.request, operation)
        preview = self.adapter.preview(self.request, operation)
        self.fixture.seed('1', [(101, 7), (102, 3)])
        with self.assertRaisesRegex(Exception, 'candidate-or-prestate-drift'):
            self.run_apply(operation, preview)
        self.assertFalse((folder / 'recovery-submitted.json').exists())
        self.assertEqual(claim.read_bytes(), original)

    def test_recovery_commit_before_receipt_loss_is_readback_only(self):
        operation, folder, claim, original = self.failed_original()
        preview = self.adapter.preview(self.request, operation)
        real = module.shared._exclusive_json
        def lose_receipt(path, value):
            if path.name == 'recovery-receipt.json':
                raise OSError('private path must not be exposed')
            return real(path, value)
        with patch.object(module.shared, '_exclusive_json', lose_receipt):
            result = self.run_apply(operation, preview)
        self.assertEqual(result['state'], 'applied')
        self.assertFalse(result['readback']['recovery_receipt_recorded'])
        self.assertEqual(result['readback']['failure']['stage'], 'post_commit_readback')
        with patch.object(self.adapter, 'apply', side_effect=AssertionError('committed recovery repeated')):
            self.assertEqual(self.run_apply(operation, preview)['state'], 'applied')
        self.assertEqual(claim.read_bytes(), original)

    def test_recovery_receipt_and_intent_cannot_be_rebound(self):
        operation, folder, claim, original = self.failed_original()
        preview = self.adapter.preview(self.request, operation)
        self.assertEqual(self.run_apply(operation, preview)['state'], 'applied')
        receipt_path = folder / 'recovery-receipt.json'
        receipt_bytes = receipt_path.read_bytes()
        for field in ('operation_id', 'request_digest', 'prestate_sha256', 'candidate_sha256',
                      'original_claim_sha256', 'recovery_intent_sha256'):
            with self.subTest(field=field):
                receipt = json.loads(receipt_bytes); receipt[field] = 'foreign'
                receipt_path.write_text(json.dumps(receipt))
                with self.assertRaisesRegex(AdapterError, 'recovery_receipt_changed'):
                    self.adapter.readback(self.request, operation)
        receipt_path.write_bytes(receipt_bytes)
        intent_path = folder / 'recovery-submitted.json'
        intent = json.loads(intent_path.read_bytes())
        intent['preview']['prepared']['plan']['cutoff_at'] = 'foreign'
        intent_path.write_text(json.dumps(intent))
        with self.assertRaisesRegex(AdapterError, 'recovery_intent_changed'):
            self.adapter.readback(self.request, operation)
        self.assertEqual(claim.read_bytes(), original)

    def test_foreign_business_write_inside_activation_rolls_back(self):
        plan = self.fixture.service.migration_preview(supply_ids=['1'], cutoff_at=fixtures.NOW)
        with self.fixture.conn() as conn:
            before = module.protected(conn)
        def malicious_hook(conn, now):
            conn.execute(f'UPDATE {core.BALANCES_TABLE} SET quantity=999')
            module.require(module.protected(conn) == before, 'protected_state_changed')
        with self.assertRaises(AdapterError):
            self.fixture.service.migration_apply(plan, actor='fixture', on_activate=malicious_hook)
        with self.fixture.conn() as conn:
            self.assertEqual(module.protected(conn), before)

    def test_wb_capital_and_historical_cost_mutations_roll_back(self):
        from packages.application.warehouse_functional import ensure_warehouse_functional_schema
        with self.fixture.conn() as conn:
            ensure_warehouse_functional_schema(conn)
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ('fixture', '2026-11-08', 101, '10', '10', '100', 'good', '{}', 'same', fixtures.NOW))
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ('fixture', 'wb', 101, '10', '10', '100', '10', 'good', 1, '10', '0', '0', '{}'))
            before = module.protected(conn)
        plan = self.fixture.service.migration_preview(supply_ids=['1'], cutoff_at=fixtures.NOW)
        for table in ('sheet_vitrina_v1_warehouse_wb_daily_cost', 'sheet_vitrina_v1_warehouse_functional_balances'):
            with self.subTest(table=table):
                def malicious_hook(conn, now):
                    conn.execute(f'UPDATE {table} SET capital_rub=999')
                    module.require(module.protected(conn) == before, 'protected_state_changed')
                with self.assertRaises(AdapterError):
                    self.fixture.service.migration_apply(plan, actor='fixture', on_activate=malicious_hook)
                with self.fixture.conn() as conn:
                    self.assertEqual(module.protected(conn), before)


if __name__ == '__main__':
    unittest.main()
