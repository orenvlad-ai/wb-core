"""Offline native Finance proof and exact retained portal group regression checks."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import sys
from types import SimpleNamespace as NS
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.finance_daily_publication_smoke import source, plan, DAY, NMS
from apps.web_vitrina_web_source_publication_smoke import observations, plan as portal_plan, scoped_observations
from apps.web_vitrina_web_source_publication import project as portal_project, source_result
from packages.application.finance_daily_publication import assemble, project, native_presentation
from packages.application.registry_upload_http_entrypoint import _preserve_closed_web_source_presentation
from packages.application.sheet_vitrina_v1_live_plan import _finance_daily_cell_presentation
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaWriteTarget


def envelope(value):
    return SheetVitrinaV1Envelope('fixture', 'fixture-ready', '2026-09-11', value['date_columns'], [], {}, [
        SheetVitrinaWriteTarget(s['sheet_name'], 'A1', 'A1:C99', 'A1:C99', 'replace', False,
                               s.get('header', []), s['rows'], len(s['rows']), len(s.get('header', [])))
        for s in value['sheets']], value['metadata'])


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.result = assemble(source(), NMS)
        self.projected = project(plan(), self.result, 'manual-fixture')['plan']
        self.rows = self.projected['sheets'][0]['rows']
        self.values = {r[1]: r[2] for r in self.rows}

    def test_native_builder_has_all_qualified_cells_and_true_source_clock(self):
        cells = _finance_daily_cell_presentation(rows=self.rows,
            slots=[NS(column_date=DAY, slot_key="yesterday_closed")], nm_ids=NMS,
            live_sources=NS(slot_lookups={"yesterday_closed":NS(fin_report_daily_result=self.result)}))
        self.assertEqual(len(cells), 466)
        for key, by_day in cells.items():
            expected = deepcopy(self.projected['metadata']['server_cell_presentation'][key][DAY])
            expected.pop('operation_id')
            self.assertEqual(by_day[DAY], expected)
        fresh = source(); fresh['report']['source_observed_at'] = '2026-09-11T14:00:00Z'
        result = assemble(fresh, NMS)
        cells = native_presentation(result, day=DAY, nm_ids=NMS, values=self.values)
        self.assertEqual({v[DAY]['source_observed_at'] for v in cells.values()}, {'2026-09-11T14:00:00Z'})

    def test_wrong_date_roster_or_value_cannot_certify_finance(self):
        for day, ids, values in [('2026-09-09', NMS, self.values), (DAY, NMS[:-1], self.values),
            (DAY, NMS, {**self.values, 'SKU:1001|fin_buyout_rub': 12345678})]:
            with self.assertRaises(ValueError):
                native_presentation(self.result, day=day, nm_ids=ids, values=values)

    def test_missing_or_legacy_source_remains_unqualified(self):
        for result in (None, NS(diagnostics={})):
            self.assertEqual(native_presentation(result, day=DAY, nm_ids=NMS, values=self.values), {})

    def test_persisted_namespace_diagnostics_are_qualified(self):
        diagnostics = json.loads(json.dumps(self.result.diagnostics), object_hook=lambda d: NS(**d))
        result = replace(self.result, diagnostics=diagnostics)
        self.assertEqual(native_presentation(result, day=DAY, nm_ids=NMS, values=self.values),
                         native_presentation(self.result, day=DAY, nm_ids=NMS, values=self.values))


class PortalTests(unittest.TestCase):
    def setUp(self):
        obs = observations()
        value, _ = portal_project(portal_plan(), obs, [1, 2, 3], 'accepted-fixture')
        self.old = envelope(value)
        self.new = replace(self.old, metadata={'neighbour': 'keep', 'server_cell_presentation': {
            'SKU:1|stock_total': {'2026-09-11': {'source': 'inventory-proof'}}}})
        self.sources = {o['source_key']: (source_result(o, [1, 2, 3]), o['source_fetched_at']) for o in obs}

    def run_keep(self, old=None):
        return _preserve_closed_web_source_presentation(self.new, previous_plan=old or self.old,
            day='2026-09-11', runtime=NS(load_temporal_source_slot_snapshot=lambda **k: self.sources[k['source_key']]))

    def test_entire_group_keeps_missing_zero_total_and_non_target(self):
        result = self.run_keep()
        cells = result.metadata['server_cell_presentation']
        self.assertEqual(result.sheets, self.new.sheets)
        self.assertEqual(len(cells), 25)  # 24 portal cells and one inventory proof.
        self.assertNotIn('SKU:1|ctr', cells)
        self.assertEqual(cells['SKU:3|view_count']['2026-09-11']['missing_sku_count'], 1)
        self.assertEqual(cells['SKU:2|view_count']['2026-09-11']['missing_sku_count'], 0)
        self.assertIn('SKU:1|orders_current', cells)
        self.assertIn('SKU:3|position_avg', cells)
        self.assertIn('TOTAL|total_orders_current', cells)
        self.assertIn('TOTAL|avg_position_avg', cells)
        for key in cells:
            if key != 'SKU:1|stock_total':
                self.assertEqual(cells[key], self.old.metadata['server_cell_presentation'][key])

    def test_new_clock_or_date_never_inherits_old_observation_proof(self):
        payload, stamp = self.sources['seller_funnel_snapshot']
        for changed in (replace(payload, source_fetched_at='2026-09-12T23:00:00Z'),
                        replace(payload, date='2026-09-10')):
            self.sources['seller_funnel_snapshot'] = changed, stamp
            self.assertNotIn('SKU:1|view_count', self.run_keep().metadata['server_cell_presentation'])

    def test_any_group_value_missing_roster_or_digest_drift_rejects_whole_group(self):
        for mode in ('value', 'scope', 'missing', 'digest'):
            old = deepcopy(self.old)
            if mode == 'value':
                next(r for r in old.sheets[0].rows if r[1] == 'SKU:1|view_count')[2] += 1
            elif mode == 'digest':
                old.metadata['server_cell_presentation']['SKU:1|view_count']['2026-09-11']['source_digest'] = 'different'
            else:
                cell = old.metadata['server_cell_presentation']['TOTAL|total_view_count']['2026-09-11']
                cell['metric_scope_evidence']['applicable_scope' if mode == 'scope' else 'missing_scope'] = []
            cells = self.run_keep(old).metadata['server_cell_presentation']
            self.assertNotIn('SKU:1|view_count', cells)
            self.assertIn('SKU:1|views_current', cells)

    def test_changed_accepted_missing_roster_cannot_carry_any_group_cell(self):
        payload, stamp = self.sources['seller_funnel_snapshot']
        self.sources['seller_funnel_snapshot'] = replace(payload, items=payload.items[:1]), stamp
        self.assertNotIn('SKU:1|view_count', self.run_keep().metadata['server_cell_presentation'])

    def test_historical_roster_uses_its_own_totals_from_union_source(self):
        value = portal_plan()
        value['sheets'][0]['rows'] = [r for r in value['sheets'][0]['rows'] if not r[1].startswith('SKU:2|')]
        value, _ = portal_project(value, observations(), [1, 3], 'accepted-historical')
        self.old = envelope(value)
        self.new = replace(self.old, metadata={})
        cells = self.run_keep().metadata['server_cell_presentation']
        self.assertIn('SKU:1|orders_current', cells)
        self.assertNotIn('SKU:2|orders_current', cells)
        self.assertEqual(cells['TOTAL|total_views_current']['2026-09-11']['missing_sku_count'], 1)
        for metric, operand in [('total_orders_current', 'orders_current'), ('avg_position_avg', 'position_avg')]:
            cell = cells['TOTAL|' + metric]['2026-09-11']
            self.assertEqual(cell['metric_scope_evidence']['applicable_scope'], ['SKU:1', 'SKU:3'])
            self.assertEqual(cell['metric_scope_evidence']['missing_scope'], ['SKU:3'])
            self.assertEqual(cell['metric_scope_evidence']['sku_metric_keys'], [operand])

    def test_dated_proof_survives_a_wider_ready_roster(self):
        day = '2026-09-11'
        edition = 'a' * 64
        obs = observations()
        value, _ = portal_project(portal_plan(), obs, [1, 2, 3], 'accepted-dated',
            history_roster={'edition_id':edition,'dates':{day:{'nm_ids':[1,3]}}})
        self.old = envelope(value)
        self.new = replace(self.old, metadata={})
        cells = self.run_keep().metadata['server_cell_presentation']
        self.assertIn('SKU:1|orders_current', cells)
        self.assertIn('SKU:3|orders_current', cells)
        self.assertNotIn('SKU:2|orders_current', cells)
        self.assertEqual(cells['TOTAL|total_orders_current'][day]['history_edition_id'], edition)
        self.assertEqual(cells['TOTAL|total_view_count'][day]['history_edition_id'], edition)
        old = deepcopy(self.old)
        old.metadata['server_cell_presentation']['SKU:1|orders_current'][day]['history_edition_id'] = 'b' * 64
        self.assertNotIn('TOTAL|total_orders_current', self.run_keep(old).metadata['server_cell_presentation'])

    def test_zero_weight_search_ctr_proof_preserves_confirmed_zero(self):
        obs = observations()
        for item in obs[1]['items']:
            item.update(views_current=0,ctr_current=0)
        value, _ = portal_project(portal_plan(), obs, [1, 2, 3], 'accepted-zero-views')
        self.old = envelope(value)
        self.new = replace(self.old, metadata={})
        self.sources['web_source_snapshot'] = source_result(obs[1], [1, 2, 3]), obs[1]['source_fetched_at']
        total = next(row for row in self.new.sheets[0].rows if row[1]=='TOTAL|avg_ctr_current')
        self.assertEqual(total[2], 0)
        self.assertIn('TOTAL|avg_ctr_current', self.run_keep().metadata['server_cell_presentation'])

    def test_legacy_search_proof_retains_only_its_original_metrics(self):
        for key in list(self.old.metadata['server_cell_presentation']):
            if key.split('|')[-1] in {'orders_current', 'position_avg', 'total_orders_current', 'avg_position_avg'}:
                del self.old.metadata['server_cell_presentation'][key]
        cells = self.run_keep().metadata['server_cell_presentation']
        self.assertIn('SKU:1|views_current', cells)
        self.assertNotIn('SKU:1|orders_current', cells)
        self.assertNotIn('TOTAL|avg_position_avg', cells)

    def test_search_proof_without_order_and_position_totals_is_retained(self):
        for key in ('TOTAL|total_orders_current', 'TOTAL|avg_position_avg'):
            del self.old.metadata['server_cell_presentation'][key]
        cells = self.run_keep().metadata['server_cell_presentation']
        self.assertIn('SKU:1|orders_current', cells)
        self.assertIn('SKU:1|position_avg', cells)
        self.assertNotIn('TOTAL|total_orders_current', cells)
        self.assertNotIn('TOTAL|avg_position_avg', cells)

    def test_search_order_or_position_drift_rejects_all_extended_proof(self):
        for key in ('SKU:1|orders_current', 'SKU:1|position_avg', 'TOTAL|total_orders_current', 'TOTAL|avg_position_avg'):
            old = deepcopy(self.old)
            next(r for r in old.sheets[0].rows if r[1] == key)[2] += 1
            cells = self.run_keep(old).metadata['server_cell_presentation']
            self.assertNotIn('SKU:1|views_current', cells)
            self.assertNotIn(key, cells)

    def test_search_total_proof_requires_both_totals_and_exact_operand(self):
        for mode in ('missing', 'wrong_operand'):
            old = deepcopy(self.old)
            proofs = old.metadata['server_cell_presentation']
            if mode == 'missing':
                del proofs['TOTAL|total_orders_current']
            else:
                proofs['TOTAL|total_orders_current']['2026-09-11']['metric_scope_evidence']['sku_metric_keys'] = ['views_current']
            self.assertNotIn('SKU:1|views_current', self.run_keep(old).metadata['server_cell_presentation'])

    def test_scoped_union_proof_stays_scoped_for_current_and_historical_rosters(self):
        obs = scoped_observations()
        self.sources = {o['source_key']: (source_result(o, [1, 2, 3]), o['source_fetched_at']) for o in obs}
        for roster in ([1, 2, 3], [1, 3]):
            value = portal_plan()
            value['sheets'][0]['rows'] = [r for r in value['sheets'][0]['rows']
                if not r[1].startswith('SKU:') or int(r[1].split('|')[0][4:]) in roster]
            value, _ = portal_project(value, obs, roster, 'accepted-scoped')
            self.old = envelope(value)
            self.new = replace(self.old, metadata={})
            cells = self.run_keep().metadata['server_cell_presentation']
            proof = cells['SKU:1|orders_current']['2026-09-11']
            self.assertEqual(proof['source_completeness'], 'scoped_complete')
            self.assertEqual(proof['search_scope'], obs[1]['search_scope'])
            self.assertEqual(cells['SKU:3|position_avg']['2026-09-11']['completeness_state'], 'partial')
            self.old.metadata['server_cell_presentation']['SKU:1|orders_current']['2026-09-11']['search_scope']['count_difference'] = 999
            self.assertNotIn('SKU:1|views_current', self.run_keep().metadata.get('server_cell_presentation', {}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
