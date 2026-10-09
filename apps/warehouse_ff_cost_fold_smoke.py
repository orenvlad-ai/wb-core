"""Parity against the exact FF fold frozen from a46d0dad (not the new helper)."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application import warehouse_functional as native

OLD = ROOT / 'apps/fixtures/warehouse_ff_fold_a46d0dad.json'
OLD_SHA = '1839c3887689fd896837ed9cd7721fa4283f5eb140169589983c65b903b5d13c'
OLD_SOURCE = json.loads(OLD.read_text())['python_source']
D = Decimal


def opening(q='100', c='10000'):
    return {1: {'quantity': D(q), 'capital': D(c), 'operations': [], 'opening_version_id': 'opening'}}


def operation(identity, source='adjustment', obj='', stamp='2026-07-24T12:00:00Z', **extra):
    return {'operation_id': identity, 'created_at': stamp, 'source_type': source, 'source_object_id': obj, **extra}


def line(identity, q, cost=None, adjustment=None):
    raw = {}
    if cost is not None:
        raw['cost_snapshot'] = {'unit_cost_rub': cost, 'capital_delta_rub': str(D(q)*D(cost)), 'quality': 'exact', 'provenance': {'native_document': identity}}
    if adjustment is not None:
        raw['cost_adjustment'] = {'capital_delta_rub': adjustment, 'allocation_basis_quantity': '100', 'document_id': identity, 'business_date': '2026-07-24', 'source_revision': 'revision'}
    return {'operation_id': identity, 'nm_id': 1, 'quantity_delta': q, 'raw': raw}


def original(inputs):
    env = dict(vars(native))
    env.update(ff_pools=deepcopy(inputs['opening_pools']), ff_outbound_wac_by_supply_nm={},
               capture={'ff_operations': inputs['operations'], 'ff_lines': inputs['lines']},
               cutover={'cutover_at': inputs['boundary']}, ff_qty=inputs['expected_quantities'],
               supplier_flow_costs=inputs['supplier_flow_costs'], cost_map=inputs['cost_map'])
    exec(compile(OLD_SOURCE, str(OLD), 'exec'), env)
    return env['ff_pools'], env['ff_outbound_wac_by_supply_nm']


class NativeParity(unittest.TestCase):
    def test_pinned_bytes(self):
        self.assertEqual(hashlib.sha256(OLD_SOURCE.encode()).hexdigest(), OLD_SHA)
        self.assertNotIn('replay_ff_cost_pools', OLD_SOURCE)

    def test_original_fold_parity(self):
        cases = [
            ('legacy inflow', {}, [operation('in','supplier_shipment','shipment')], [line('in','100')], '100', {}),
            ('frozen inflow ignores changed allocation', {}, [operation('in','supplier_shipment','shipment')], [line('in','100','100')], '100', {}),
            ('legacy proportional WB debit', opening(), [operation('out','wb_supply','supply')], [line('out','-25')], '75', {}),
            ('frozen WB debit', opening(), [operation('out','wb_supply','supply')], [line('out','-25','90')], '75', {}),
            ('cancel original frozen cost', opening('75','7500'), [operation('return','wb_supply_cancel','supply')], [line('return','25','100')], '100', {}),
            ('missing supplier cost', {}, [operation('in','supplier_shipment','missing')], [line('in','100')], '100', {}),
            ('missing seed', {}, [operation('in')], [line('in','1')], '1', {'cost_map': {}}),
            ('seed', {}, [operation('in')], [line('in','1')], '1', {}),
            ('empty outbound', {}, [operation('out')], [line('out','-1')], '0', {}),
            ('negative quantity', opening(), [operation('out')], [line('out','-101')], '-1', {}),
            ('negative money', opening(), [operation('out')], [line('out','-50','250')], '50', {}),
            ('zero qty residual', opening(), [operation('out')], [line('out','-100','90')], '0', {}),
            ('all consumed exact zero', opening(), [operation('out')], [line('out','-100','100')], '0', {}),
            ('decimal proportional', opening('3','10'), [operation('out')], [line('out','-1')], '2', {}),
            ('cost only', opening(), [operation('cost',operation_type='ff_overhead_allocation',business_effective_date='2026-07-24')], [line('cost','0',adjustment='100')], '100', {}),
            ('negative cost only', opening(), [operation('cost')], [line('cost','0',adjustment='-10000')], '100', {}),
            ('wrong cost basis', opening('99','9900'), [operation('cost')], [line('cost','0',adjustment='100')], '99', {}),
            ('cutover exclusion', opening(), [operation('old',stamp='2026-07-20T00:00:00Z')], [line('old','10')], '100', {'boundary': '2026-07-20T00:00:00Z'}),
            ('expected quantity mismatch', opening(), [], [], '99', {}),
            ('same timestamp stable order', {}, [operation('in','supplier_shipment','shipment'),operation('out','wb_supply','supply')], [line('in','100'),line('out','-25')], '75', {}),
        ]
        for name, pools, ops, rows, qty, extra in cases:
            with self.subTest(name=name):
                inputs = dict(opening_pools=pools, operations=ops, lines=rows,
                    supplier_flow_costs={('shipment',1):(D(100),D(11000),'native',{'supplier':'shipment'})},
                    cost_map={1:SimpleNamespace(ff_unit_cost=D(100))}, boundary='', expected_quantities={1:D(qty)})
                inputs.update(extra)
                before = deepcopy(inputs)
                outcomes = []
                for evaluator in (original, lambda args:native.replay_ff_cost_pools(**args)):
                    try:
                        outcomes.append(('result', evaluator(inputs)))
                    except Exception as exc:
                        outcomes.append(('error', type(exc), str(exc)))
                self.assertEqual(outcomes[0], outcomes[1])
                self.assertEqual(inputs, before)
                if name == 'frozen inflow ignores changed allocation':
                    self.assertEqual(outcomes[1][1][0][1]['capital'], D(10000))
                if name == 'all consumed exact zero':
                    self.assertEqual(outcomes[1][1][0][1]['capital'], D(0))


if __name__ == '__main__':
    unittest.main()
