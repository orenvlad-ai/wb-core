#!/usr/bin/env python3
"""Activation CAS, no physical writes and atomic policy failure contracts."""
from contextlib import nullcontext
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
        self.root = self.fixture.runtime.runtime_dir
        self.db = self.fixture.runtime.db_path
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
        def fail_after_policy(conn, activated_at):
            original(conn, activated_at)
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
