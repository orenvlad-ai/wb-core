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
from apps.web_vitrina_web_source_publication_smoke import observations, plan as portal_plan
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
        self.assertEqual(len(cells), 17)  # 16 portal cells and one inventory proof.
        self.assertNotIn('SKU:1|ctr', cells)
        self.assertEqual(cells['SKU:3|view_count']['2026-09-11']['missing_sku_count'], 1)
        self.assertEqual(cells['SKU:2|view_count']['2026-09-11']['missing_sku_count'], 0)
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
