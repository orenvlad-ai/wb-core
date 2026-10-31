#!/usr/bin/env python3
"""Unavailable accounting does not freeze unrelated dated source publication."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.ready_publication_smoke import seed, make_book, make_plan, save, NOW
from packages.application import fbs_accounting_runtime as accounting
from packages.application import fbs_accounting_degraded as degraded
from packages.application import sheet_vitrina_v1_inventory_history as history
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.ready_publication import ReadyPublicationConflict
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot


class DegradedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = seed(self.root)
        initial, expected = make_book(self.root, opening=True)
        version = accounting.save(self.root, initial, expected=expected, operation_id='opening')
        accounting._record_prepare_failure(self.root, expected=version,
            error=ValueError('incomplete_period_cannot_close'))
        self.book, self.version = accounting.load(self.root)
        self.versions = {'fbs_accounting_status': 'unavailable', 'fbs_book': self.version,
            'fbs_accounting_reason': 'incomplete_period_cannot_close'}

    def scope(self):
        return degraded.degraded_accounting_scope(self.runtime, self.versions)

    def test_owner_reason_and_version_required(self):
        with self.assertRaisesRegex(RuntimeError, 'ownership'):
            with self.scope():
                pass
        with heavy_admitted(self.root, operation='cycle'):
            for key, value in [('fbs_book', 'other'), ('fbs_accounting_reason', 'other')]:
                with self.subTest(key=key), self.assertRaisesRegex(ReadyPublicationConflict, 'proof_changed'):
                    with degraded.degraded_accounting_scope(self.runtime, {**self.versions, key: value}):
                        pass
            with self.scope():
                self.assertEqual(degraded.current_degraded_accounting(self.root)['book_version'], self.version)
                with self.assertRaisesRegex(ReadyPublicationConflict, 'runtime_changed'):
                    degraded.current_degraded_accounting(self.root / 'other')
            self.assertIsNone(degraded.current_degraded_accounting(self.root))

    def test_normal_writer_still_rejects_and_scoped_writer_preserves_book(self):
        plan = make_plan(as_of_date='2026-09-09', day='2026-09-10')
        plan = replace(plan, temporal_slots=[
            SheetVitrinaV1TemporalSlot('yesterday_closed', 'closed', '2026-09-09'),
            SheetVitrinaV1TemporalSlot('today_current', 'current', '2026-09-10')])
        now = datetime(2026, 9, 10, 14, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ReadyPublicationConflict, 'book_operand_unavailable'):
            save(self.runtime, plan, now=now)
        with heavy_admitted(self.root, operation='cycle'), self.scope():
            result = save(self.runtime, plan, now=now)
        self.assertEqual(result.status, 'success')
        self.assertNotEqual(result.semantic_status, 'success')
        self.assertTrue(any(v['source_key'] == 'fbs_snapshot_accounting' for v in result.source_outcomes))
        self.assertEqual(accounting.load(self.root), (self.book, self.version))
        saved = self.runtime.load_sheet_vitrina_ready_snapshot(plan.as_of_date)
        rows = {r[1]: r for r in saved.sheets[0].rows}
        self.assertEqual(rows['SKU:1|our_wb_unit_cost_rub'][2:], ['', ''])
        self.assertEqual(rows['SKU:1|own_capital_FF_capital_rub'][2:], ['', ''])
        self.assertEqual(rows['SKU:1|ordersCount'][2:], [11, 12])
        self.assertEqual(rows['SKU:1|foreign_metric'][2:], [314, 159])
        self.assertFalse(saved.metadata.get('fbs_accounting_bindings'))
        quality = saved.metadata[degraded.METADATA_KEY]
        self.assertEqual(quality['dates'], plan.date_columns)
        self.assertNotIn('runtime_dir', quality)
        from packages.application.sheet_vitrina_v1_web_vitrina import _build_period_snapshot
        period, _ = _build_period_snapshot(runtime=self.runtime, date_from='2026-09-09',
            date_to='2026-09-10', default_visible_snapshot=saved)
        self.assertEqual(period.metadata['server_cell_presentation']['SKU:1|our_wb_unit_cost_rub']
            ['2026-09-09']['reason_code'], 'incomplete_period_cannot_close')
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM ' + history.FINALIZATIONS_TABLE).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM ' + history.CAPTURES_TABLE).fetchone()[0], 0)
        with self.assertRaisesRegex(ReadyPublicationConflict, 'book_operand_unavailable'):
            save(self.runtime, saved, now=now)  # Persisted metadata grants nothing.

    def test_overlay_masks_all_affected_dates_preserves_closed_and_no_fake_binding(self):
        plan = make_plan(as_of_date='2026-09-07', day='2026-09-09')
        sheet = plan.sheets[0]
        rows = deepcopy(sheet.rows) + [
            ['profit', 'SKU:1|proxy_profit_4_rub', 12, 999],
            ['quantity', 'TOTAL|total_inventory_fbs_total_qty_v1', 13, 999]]
        plan = replace(plan, sheets=[replace(sheet, rows=rows, row_count=len(rows)), plan.sheets[1]],
            metadata={'fbs_accounting_bindings': {'2026-09-07': {'old': True}, '2026-09-09': {'stale': True}}})
        with heavy_admitted(self.root, operation='cycle'), self.scope():
            proof = degraded.current_degraded_accounting(self.root)
            result = degraded.materialize_degraded_accounting(plan, proof=proof)
        self.assertEqual(result.sheets[0].rows[-2][2:], [12, ''])
        self.assertEqual(result.sheets[0].rows[-1][2:], [13, ''])
        self.assertEqual(result.metadata['fbs_accounting_bindings'], {'2026-09-07': {'old': True}})
        self.assertEqual(plan.sheets[0].rows[-1][-1], 999)

    def test_history_does_not_fallback_or_finalize_prior_capture(self):
        plan = make_plan(as_of_date='2026-09-09', day='2026-09-10')
        plan = replace(plan, temporal_slots=[
            SheetVitrinaV1TemporalSlot('yesterday_closed', 'closed', '2026-09-09'),
            SheetVitrinaV1TemporalSlot('today_current', 'current', '2026-09-10')])
        with heavy_admitted(self.root, operation='cycle'), self.scope():
            plan = degraded.materialize_degraded_accounting(plan,
                proof=degraded.current_degraded_accounting(self.root))
        with sqlite3.connect(self.runtime.db_path) as conn, \
             patch.object(history, '_latest_capture_id', side_effect=AssertionError('must not select old capture')), \
             patch.object(history, '_canonical_current_wb_evidence', side_effect=AssertionError('legacy fallback')), \
             patch.object(history, '_prepare_closed_date_ready_capture', side_effect=AssertionError('closed fallback')):
            prepared = history.prepare_inventory_history_from_ready_plan(conn, plan=plan,
                bundle_version='fixture', refreshed_at=NOW.isoformat(), runtime_dir=self.root)
            result = history.capture_inventory_history_from_ready_plan(conn, plan=plan,
                bundle_version='fixture', refreshed_at=NOW.isoformat(), prepared=prepared)
        self.assertEqual(prepared['closed_date'], '')
        self.assertEqual(result['status'], 'skipped')
        self.assertFalse(result['finalization_inserted'])

    def test_recovered_book_releases_only_proven_dates(self):
        plan = make_plan(as_of_date='2026-09-09', day='2026-09-10')
        with heavy_admitted(self.root, operation='cycle'), self.scope():
            plan = degraded.materialize_degraded_accounting(plan,
                proof=degraded.current_degraded_accounting(self.root))
        recovered = {**self.book, 'publication_error': None,
            'presentations': {'2026-09-10': {'quality': 'preliminary'}}}
        repaired = degraded.reconcile_recovered_accounting(plan, book=recovered)
        self.assertEqual(repaired.metadata[degraded.METADATA_KEY]['dates'], ['2026-09-09'])
        recovered['presentations']['2026-09-09'] = {'quality': 'closed_snapshot_wac'}
        repaired = degraded.reconcile_recovered_accounting(plan, book=recovered)
        self.assertNotIn(degraded.METADATA_KEY, repaired.metadata)
        self.assertIs(degraded.reconcile_recovered_accounting(plan, book=self.book), plan)

    def test_real_recovered_save_clears_old_read_sentinels(self):
        plan = make_plan()
        sheet = plan.sheets[0]
        extra = ['obsolete', 'SKU:1|avg_own_obsolete_metric', 14, 99]
        plan = replace(plan, sheets=[replace(sheet, rows=sheet.rows + [extra],
            row_count=sheet.row_count + 1), plan.sheets[1]])
        with heavy_admitted(self.root, operation='cycle'), self.scope():
            save(self.runtime, plan)
        unavailable = self.runtime.load_sheet_vitrina_ready_snapshot(plan.as_of_date)
        recovered, expected = make_book(self.root, quantity='1900')
        accounting.save(self.root, recovered, expected=expected, operation_id='recovered')
        save(self.runtime, unavailable)
        saved = self.runtime.load_sheet_vitrina_ready_snapshot(plan.as_of_date)
        self.assertNotIn(degraded.METADATA_KEY, saved.metadata)
        self.assertFalse(any(cell.get('reason_code') == 'incomplete_period_cannot_close'
            for by_date in saved.metadata['server_cell_presentation'].values() for cell in by_date.values()))
        from packages.application.sheet_vitrina_v1_web_vitrina import _build_period_snapshot
        period, _ = _build_period_snapshot(runtime=self.runtime, date_from='2026-09-08',
            date_to='2026-09-08', default_visible_snapshot=saved)
        data = next(sheet for sheet in period.sheets if sheet.sheet_name == 'DATA_VITRINA')
        values = {row[1]: row[2] for row in data.rows}
        self.assertGreater(values['SKU:1|our_wb_unit_cost_rub'], 0)
        self.assertEqual(values['SKU:1|avg_own_obsolete_metric'], '')
        self.assertFalse(any(row and row[0] == 'fbs_snapshot_accounting'
            for sheet in saved.sheets if sheet.sheet_name == 'STATUS' for row in sheet.rows))

    def test_last_read_overlay_withholds_restored_and_group_values_without_scope(self):
        from packages.contracts.web_vitrina_contract import WebVitrinaContractRow
        def row(identity, metric):
            return WebVitrinaContractRow(identity, 1, 'SKU', 'SKU:1', 'Fixture', metric,
                metric, '', 'test', None, 1, 'number',
                {'2026-09-07': 17, '2026-09-09': 999}, {})
        rows = [row('SKU:1|our_wb_unit_cost_rub', 'our_wb_unit_cost_rub'),
                row('GROUP:test|proxy_profit_4_rub', 'proxy_profit_4_rub'),
                row('SKU:1|inventory_fbs_facility_available_qty_v1:new', 'inventory_fbs_facility_available_qty_v1:new'),
                row('SKU:1|ordersCount', 'ordersCount')]
        presentation = {'SKU:1|our_wb_unit_cost_rub': {'2026-09-09': {
            'state': 'unavailable', 'reason_code': 'incomplete_period_cannot_close'}}}
        self.assertIsNone(degraded.current_degraded_accounting(self.root))
        masked = degraded.apply_degraded_rows(rows, presentation=presentation)
        for item in masked[:3]:
            self.assertEqual(item.values_by_date, {'2026-09-07': 17, '2026-09-09': ''})
        self.assertEqual(masked[-1].values_by_date['2026-09-09'], 999)

    def test_official_quantity_survives_unavailable_book_without_valuing_it(self):
        from decimal import Decimal
        from packages.application.web_vitrina_official_fbs import SOURCE, restore_materialized_official_fbs_estimates
        from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
        from packages.contracts.web_vitrina_contract import WebVitrinaContractRow
        day = '2026-09-10'
        now = datetime(2026, 9, 10, 14, tzinfo=timezone.utc)
        plan = make_plan(as_of_date='2026-09-09', day=day)
        model = dict(available=True, quantity_only=True, date=day, source=SOURCE,
            generation_id='complete-generation', generation_digest='sha256:complete',
            captured_at='2026-09-10T13:59:00Z', functional_version_id='', facilities=['ff-1'],
            skus={1:dict(facilities={'ff-1':Decimal(8)},fbs_quantity=Decimal(8))},
            total=dict(facilities={'ff-1':Decimal(8)},fbs_quantity=Decimal(8)))
        from packages.application.web_vitrina_official_fbs import add_official_wb_quantities
        model = add_official_wb_quantities(model,dict(date=day,snapshot_id='wb1',source_digest='sha256:wb1',
            version_id='version1',observed_at='2026-09-10T13:58:00Z',quantity_basis='physical_quantity_only',
            skus={'1':5},total=5))
        with heavy_admitted(self.root, operation='cycle'), self.scope(), patch(
                'packages.application.web_vitrina_official_fbs.build_current_official_fbs_estimate', return_value=model) as source:
            save(self.runtime, plan, now=now)
        self.assertTrue(source.call_args.kwargs['quantity_only'])
        self.assertEqual(accounting.load(self.root), (self.book,self.version))
        saved = self.runtime.load_sheet_vitrina_ready_snapshot(plan.as_of_date)
        values = {r[1]:r[2:] for r in saved.sheets[0].rows}
        identity = 'TOTAL|total_inventory_fbs_total_qty_v1'
        self.assertEqual(values[identity], ['',8.0])
        self.assertEqual(values['TOTAL|total_inventory_wb_total_qty_v1'],['',5.0])
        self.assertEqual(values['TOTAL|total_stock_total'],['',13.0])
        self.assertEqual(values['SKU:1|our_wb_unit_cost_rub'],['',''])
        self.assertEqual(values['SKU:1|own_capital_FF_capital_rub'],['',''])
        self.assertFalse(saved.metadata.get('fbs_accounting_bindings'))
        with patch('packages.application.sheet_vitrina_v1_web_vitrina.build_current_official_fbs_estimate', return_value=model) as source:
            result=SheetVitrinaV1WebVitrinaBlock(runtime=self.runtime,now_factory=lambda:now).build(
                page_route='/vitrina',read_route='/read',as_of_date=plan.as_of_date)
        self.assertTrue(source.call_args.kwargs['quantity_only'])
        self.assertEqual(next(r for r in result.rows if r.row_id==identity).values_by_date[day],8.0)
        self.assertEqual(next(r for r in result.rows if r.row_id=='TOTAL|total_inventory_wb_total_qty_v1').values_by_date[day],5.0)
        self.assertEqual(next(r for r in result.rows if r.row_id=='TOTAL|total_stock_total').values_by_date[day],13.0)
        # Native history reads the frozen source date, without today's source.
        row=WebVitrinaContractRow(identity,1,'TOTAL','TOTAL','Total','total_inventory_fbs_total_qty_v1',
            'FBS','','test',None,None,'number',{'2026-09-09':'',day:''},{})
        frozen=restore_materialized_official_fbs_estimates([row],presentation=saved.metadata['server_cell_presentation'],quantity_only=True)
        frozen=degraded.apply_degraded_rows(frozen,presentation=saved.metadata['server_cell_presentation'])
        self.assertEqual(frozen[0].values_by_date,{'2026-09-09':'',day:8.0})
        for metric,amount in [('inventory_wb_total_qty_v1',5.0),('stock_total',13.0)]:
            identity2='TOTAL|total_'+metric
            row2=replace(row,row_id=identity2,metric_key='total_'+metric)
            restored=restore_materialized_official_fbs_estimates([row2],presentation=saved.metadata['server_cell_presentation'],quantity_only=True)
            checked=degraded.apply_degraded_rows(restored,presentation=saved.metadata['server_cell_presentation'])
            self.assertEqual(checked[0].values_by_date[day],amount)
            invalid=deepcopy(saved.metadata['server_cell_presentation'])
            del invalid[identity2][day]['wb_source']
            self.assertEqual(degraded.apply_degraded_rows(restored,presentation=invalid)[0].values_by_date[day],'')
        # Source label alone cannot bypass the mask or unmask a cost metric.
        broken=deepcopy(saved.metadata['server_cell_presentation'])
        del broken[identity][day]['source_digest']
        self.assertEqual(degraded.apply_degraded_rows(frozen,presentation=broken)[0].values_by_date[day],'')

    def test_older_plan_is_untouched(self):
        plan = make_plan(as_of_date='2026-09-06', day='2026-09-07')
        with heavy_admitted(self.root, operation='cycle'), self.scope():
            self.assertIs(degraded.materialize_degraded_accounting(plan,
                proof=degraded.current_degraded_accounting(self.root)), plan)


if __name__ == '__main__':
    unittest.main()
