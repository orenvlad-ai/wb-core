"""Retained exact current column → accepted closed sources → native Ready/History."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unittest.mock import patch

from apps import sheet_vitrina_v1_closed_backlog_smoke as backlog_fixture
from apps.sheet_vitrina_v1_closed_backlog_smoke import Source, CYCLE
from packages.application import sheet_vitrina_v1_live_plan as live
from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog, ClosedBacklogConflict
from packages.application.ready_publication import canonical, digest, readonly, ReadyPublicationConflict
from packages.application.registry_upload_db_backed_runtime import _serialize_sheet_vitrina_plan

DAY = '2026-10-10'
NOW = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
STAMP = NOW.isoformat().replace('+00:00', 'Z')


class DatedSales(Source):
    def execute(self, request):
        day = request.date; self.request_dates.append(day)
        # The latest source still has two unknown SKU; neither may become zero.
        items = [SimpleNamespace(nm_id=nm, date=day, metric=metric, value=value)
                 for nm in request.nm_ids[:-2]
                 for metric, value in [('orderCount', 5), ('orderSum', 3000), ('openCount', 20),
                    ('cartCount', 8), ('addToCartConversion', .4), ('cartToOrderConversion', .625),
                    ('addToWishlistCount', 2), ('buyoutCount', 0), ('buyoutSum', 0), ('buyoutPercent', 0)]]
        return SimpleNamespace(result=SimpleNamespace(kind='success', date_from=day, date_to=day,
            count=len(items), items=items))


class DatedFinance(Source):
    def execute(self, request):
        day = request.date; self.request_dates.append(day)
        items = [SimpleNamespace(nm_id=nm, fin_buyout_rub=700.25, fin_delivery_rub=20,
            fin_commission_wb_portal=150, fin_acquiring_fee=14, fin_loyalty_rub=0) for nm in request.nm_ids]
        return SimpleNamespace(result=SimpleNamespace(kind='success', snapshot_date=day,
            count=len(items), items=items, storage_total=55,
            diagnostics={'pagination': {'complete': True, 'terminal_status': 204}}))


class RetainedRepairTests(unittest.TestCase):
    setUpBase = backlog_fixture.BacklogTests.setUp
    seed_closed_cache = backlog_fixture.BacklogTests.seed_closed_cache
    pending = backlog_fixture.BacklogTests.pending

    def setUp(self):
        self.setUpBase()
        for key, source in [('sales_funnel_history_block', DatedSales('sales_funnel_history')),
                            ('fin_report_daily_block', DatedFinance('fin_report_daily'))]:
            self.counters[key] = source; setattr(self.block, key, source)
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute('DELETE FROM temporal_source_closure_state')
        self.block.now_factory = lambda: datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
        handle = self.sources.collect_sources(as_of_date='2026-10-09', execution_mode='auto_daily')
        original = self.sources.derive_collected(handle)
        # A native current envelope is promoted by the historical publisher:
        # as_of_date changes, while the accepted current column stays byte-exact.
        sheets = []
        metadata = deepcopy(original.metadata)
        for sheet in original.sheets:
            if sheet.sheet_name == 'DATA_VITRINA':
                rows = deepcopy(sheet.rows); index = sheet.header.index(DAY)
                for row in rows:
                    metric = row[1].split('|')[-1]
                    if metric == 'orderCount' and row[index] not in ('', None): row[index] = 1
                    if metric == 'orderSum' and row[index] not in ('', None): row[index] = 500
                    if metric.startswith('fin_') or metric.startswith('total_fin_'): row[index] = ''
                    if metric == 'weighted_price_seller_discounted': row[index] = 500
                    if row[1] == f'SKU:{self.nm_ids[0]}|ads_sum': row[index] = 100
                    if metric in ('ads_drr', 'ads_drr_total'): row[index] = .2
                partial = dict(quality_state='partial', completeness_state='unknown_scope', missing_sku_count=None,
                    source='ads_partial_observed_v1', source_as_of_date=DAY, reason='Unknown campaign contribution.')
                for identity in (f'SKU:{self.nm_ids[0]}|ads_sum', f'SKU:{self.nm_ids[0]}|ads_drr', 'TOTAL|ads_drr_total'):
                    metadata.setdefault('server_cell_presentation', {}).setdefault(identity, {})[DAY] = deepcopy(partial)
                sheet = replace(sheet, rows=rows)
            elif sheet.sheet_name == 'STATUS':
                rows = deepcopy(sheet.rows)
                for row in rows:
                    if row[0] == 'ads_compact[today_current]': row[1] = 'incomplete'
                sheet = replace(sheet, rows=rows)
            sheets.append(sheet)
        self.original = replace(original, as_of_date=DAY, sheets=sheets, metadata=metadata)
        current = self.runtime.load_current_state()
        expected = self.runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version, as_of_date=DAY)
        self.runtime.save_sheet_vitrina_ready_snapshot(current_state=current, refreshed_at=STAMP,
            plan=self.original, expected=expected, build_inputs=self.original.metadata['publication_inputs'])
        donor = replace(self.original, as_of_date='2026-10-09')
        expected_donor = self.runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version, as_of_date='2026-10-09')
        self.runtime.save_sheet_vitrina_ready_snapshot(current_state=current, refreshed_at=STAMP,
            plan=donor, expected=expected_donor, build_inputs=None)
        self.donor_raw = self.raw('2026-10-09')
        self.original = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=DAY)
        self.before_raw = self.raw(DAY)
        self.block.now_factory = lambda: NOW
        for key in ('sales_funnel_history', 'fin_report_daily'):
            source = next(s for s in self.counters.values() if s.source_key == key)
            result = source.execute(SimpleNamespace(nm_ids=self.nm_ids, date=DAY)).result
            self.runtime.save_temporal_source_slot_snapshot(source_key=key, snapshot_date=DAY,
                snapshot_role=live.TEMPORAL_ROLE_ACCEPTED_CLOSED, captured_at=STAMP, payload=result)
            self.pending(DAY, key, state=live.CLOSURE_STATE_SUCCESS)
        # Full old-day composition would incorrectly demand unrelated caches.
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("DELETE FROM temporal_source_slot_snapshots WHERE snapshot_date=? AND source_key IN ('stocks','spp_proxy')", (DAY,))
            conn.execute('DELETE FROM temporal_source_closure_state WHERE target_date<>?', (DAY,))
        for source in self.counters.values(): source.request_dates.clear()

    def raw(self, day):
        with readonly(self.runtime.db_path) as conn:
            return conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?', (day,)).fetchone()[0]

    def candidate(self):
        self.backlog.collect(CYCLE)
        main = self.sources.collect_sources(as_of_date='2026-10-11', execution_mode='auto_daily')
        counts = {key: list(s.request_dates) for key, s in self.counters.items()}
        partial = self.sources.derive_collected(self.backlog.compose(main, DAY))
        plan = self.backlog.merge_retained_ready(DAY, partial)
        self.assertEqual(counts, {key: list(s.request_dates) for key, s in self.counters.items()})
        return plan

    def publish(self, plan):
        current = self.runtime.load_current_state()
        expected = self.runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version, as_of_date=DAY)
        return self.runtime.save_sheet_vitrina_ready_snapshot(current_state=current, refreshed_at=STAMP,
            plan=plan, expected=expected, build_inputs=plan.metadata['publication_inputs'], _retained_closed_replay=True)

    def test_closed_sales_and_finance_without_fetch_preserve_other_cells(self):
        plan = self.candidate()
        self.assertEqual(list(self.backlog.status()['dates']), [DAY])
        rows = {r[1]: r[2] for s in plan.sheets if s.sheet_name == 'DATA_VITRINA' for r in s.rows}
        self.assertEqual(rows[f'SKU:{self.nm_ids[0]}|orderCount'], 5)
        self.assertEqual(rows[f'SKU:{self.nm_ids[0]}|orderSum'], 3000)
        self.assertEqual(rows[f'SKU:{self.nm_ids[0]}|fin_buyout_rub'], 700.25)
        self.assertEqual(rows['TOTAL|weighted_price_seller_discounted'], 600)
        self.assertAlmostEqual(rows[f'SKU:{self.nm_ids[0]}|ads_drr'], 100/3000)
        self.assertAlmostEqual(rows['TOTAL|ads_drr_total'], 100/3000)
        self.assertEqual(plan.metadata['server_cell_presentation']['TOTAL|ads_drr_total'][DAY]['completeness_state'], 'unknown_scope')
        self.assertEqual(plan.metadata['server_cell_presentation']['TOTAL|weighted_price_seller_discounted'][DAY]['missing_sku_count'], 2)
        self.assertEqual(plan.metadata['server_cell_presentation']['TOTAL|total_orderSum'][DAY]['missing_sku_count'], 2)
        for nm in self.nm_ids[-2:]:
            self.assertIn(rows[f'SKU:{nm}|orderCount'], ('', None))
        self.publish(plan)
        saved = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=DAY)
        self.assertEqual(saved.date_columns, [DAY])
        self.assertEqual(self.raw('2026-10-09'), self.donor_raw)
        self.assertEqual(rows, {r[1]: r[2] for s in saved.sheets if s.sheet_name == 'DATA_VITRINA' for r in s.rows})
        for key in ('fbs_accounting_bindings', 'fbs_accounting_targets', 'fbs_accounting'):
            self.assertEqual(self.original.metadata.get(key), saved.metadata.get(key))
        # Crash after Ready commit: recovery only reads the original receipt.
        self.backlog = ClosedBacklog(self.sources)
        self.backlog.record_ready(DAY)
        from packages.application.sheet_vitrina_v1_closed_ready_repair import retained_candidates
        self.assertEqual(retained_candidates(self.backlog, yesterday='2026-10-11'), {})

    def test_changed_retained_ready_and_changed_closed_source_refuse(self):
        self.backlog.collect(CYCLE)
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at='changed' WHERE source_key='sales_funnel_history' AND snapshot_date=? AND snapshot_role=?", (DAY, live.TEMPORAL_ROLE_ACCEPTED_CLOSED))
        with self.assertRaisesRegex(ClosedBacklogConflict, 'closed_accepted_source_changed'):
            self.backlog.collect('2' * 32)

    def test_candidate_cannot_change_capital_or_bindings_or_extra_metadata(self):
        plan = self.candidate()
        for mutate in ('data', 'metadata', 'binding', 'other_date'):
            bad = deepcopy(plan)
            if mutate == 'data':
                for s in bad.sheets:
                    if s.sheet_name == 'DATA_VITRINA':
                        row = next(r for r in s.rows if r[1].endswith('|stock_total')); row[2] = 999
            elif mutate == 'other_date': bad.metadata.setdefault('daily_trading_pool', {})['2026-10-09'] = ['SKU:forged']
            elif mutate == 'binding': bad.metadata['fbs_accounting_bindings'] = {'forged': True}
            else: bad.metadata['unexpected'] = 'not allowed'
            with self.assertRaisesRegex(ReadyPublicationConflict, 'closed_retained_candidate_invalid'):
                self.publish(bad)
        self.assertEqual(self.raw(DAY), self.before_raw)

    def test_missing_closed_family_waits_without_fetch_and_same_source_is_stable(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("DELETE FROM temporal_source_slot_snapshots WHERE source_key='fin_report_daily' AND snapshot_date=? AND snapshot_role=?", (DAY, live.TEMPORAL_ROLE_ACCEPTED_CLOSED))
        before = {key:list(source.request_dates) for key,source in self.counters.items()}
        value = self.backlog.collect(CYCLE)
        self.assertEqual(value['dates'], {})
        self.assertEqual(before, {key:list(source.request_dates) for key,source in self.counters.items()})
        self.assertEqual(self.raw(DAY), self.before_raw)

    def test_ready_and_source_changes_before_write_are_rejected(self):
        plan = self.candidate()
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=replace(plan_json,'Unknown campaign contribution.','Changed known archive.') WHERE as_of_date=?", (DAY,))
        with self.assertRaisesRegex(ReadyPublicationConflict, 'closed_retained_candidate_invalid'):
            self.publish(plan)
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?', (self.before_raw, DAY))
        plan = self.candidate()
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at='changed' WHERE source_key='fin_report_daily' AND snapshot_date=? AND snapshot_role=?", (DAY, live.TEMPORAL_ROLE_ACCEPTED_CLOSED))
        with self.assertRaisesRegex(ReadyPublicationConflict, 'ready_source_changed'):
            self.publish(plan)
        self.assertEqual(self.raw(DAY), self.before_raw)

    def test_retained_flag_cannot_omit_or_replace_native_build_fences(self):
        plan = self.candidate()
        current = self.runtime.load_current_state()
        expected = self.runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version, as_of_date=DAY)
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at='changed' WHERE source_key='fin_report_daily' AND snapshot_date=? AND snapshot_role=?", (DAY, live.TEMPORAL_ROLE_ACCEPTED_CLOSED))
        for inputs in (None, {}, {**plan.metadata['publication_inputs'], 'consumed':{}}):
            with self.assertRaisesRegex(ReadyPublicationConflict, 'closed_retained_build_inputs_required'):
                self.runtime.save_sheet_vitrina_ready_snapshot(current_state=current, refreshed_at=STAMP,
                    plan=plan, expected=expected, build_inputs=inputs, _retained_closed_replay=True)
        self.assertEqual(self.raw(DAY), self.before_raw)

    def test_sole_other_date_binding_cannot_be_dropped(self):
        plan = self.candidate()
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("DELETE FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-10-09'")
        with self.assertRaisesRegex(ReadyPublicationConflict, 'closed_retained_candidate_invalid'):
            self.publish(plan)
        self.assertEqual(self.raw(DAY), self.before_raw)

    def test_cycle_publisher_proves_receipt_and_preserves_degraded_status(self):
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        plan = self.candidate()
        entry = SimpleNamespace(runtime=self.runtime, now_factory=lambda:NOW, refreshed_at_factory=lambda:STAMP)
        entry._cycle_verify_ready = lambda versions: RegistryUploadHttpEntrypoint._cycle_verify_ready(entry, versions)
        versions, proof = RegistryUploadHttpEntrypoint._cycle_publish_dated_ready(entry, plan, retained_only=True)
        self.assertTrue(versions['publication_operation_id'])
        self.assertEqual(proof.warnings[0]['code'], 'dated_ready_report_degraded')
        self.backlog.record_ready(DAY)

    def test_discovery_does_not_parse_oversized_ready_in_sql(self):
        from packages.application.sheet_vitrina_v1_closed_ready_repair import retained_candidates
        statements = []
        original = readonly
        from contextlib import contextmanager
        @contextmanager
        def traced(path):
            with original(path) as conn:
                conn.set_trace_callback(statements.append)
                yield conn
        with patch('packages.application.sheet_vitrina_v1_closed_ready_repair.MAX_READY_BYTES', 64), \
             patch('packages.application.sheet_vitrina_v1_closed_ready_repair.readonly', traced):
            self.assertEqual(retained_candidates(self.backlog, yesterday='2026-10-11'), {})
        self.assertFalse(any('json_' in sql.lower() for sql in statements))
        self.assertFalse(any('select plan_json' in sql.lower() for sql in statements))

    def test_crash_before_ready_commit_recovers_without_source_collection(self):
        plan = self.candidate()
        with patch('packages.application.ready_publication.replace_ready', side_effect=RuntimeError('before_ready_commit')):
            with self.assertRaisesRegex(RuntimeError, 'before_ready_commit'):
                self.publish(plan)
        self.assertEqual(self.raw(DAY), self.before_raw)
        self.backlog = ClosedBacklog(self.sources)
        counts = {key:list(s.request_dates) for key,s in self.counters.items()}
        self.backlog.collect('2'*32)
        self.publish(plan)
        self.backlog.record_ready(DAY)
        self.assertEqual(counts, {key:list(s.request_dates) for key,s in self.counters.items()})
        with readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE as_of_date=? AND state='complete'", (DAY,)).fetchone()[0], 2)

    def test_native_warehouse_ready_change_before_first_replay_rebinds_only_same_sources(self):
        self.backlog.collect(CYCLE)
        current = self.runtime.load_current_state()
        changed = replace(self.original, snapshot_id=self.original.snapshot_id+'__native_later_ready')
        expected = self.runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version, as_of_date=DAY)
        self.runtime.save_sheet_vitrina_ready_snapshot(current_state=current, refreshed_at=STAMP,
            plan=changed, expected=expected, build_inputs=None)
        latest = self.raw(DAY)
        self.assertNotEqual(latest, self.before_raw)
        self.assertEqual(self.backlog.prepare_retained(CYCLE), ())
        self.assertEqual(self.backlog.status()['dates'][DAY]['retained_replay']['before_ready_digest'], digest(latest))
        plan = self.candidate(); self.publish(plan)
        self.backlog.record_ready(DAY)
        old_receipt = self.backlog.status()['dates'][DAY]['ready']
        self.assertEqual(self.backlog.prepare_retained('2'*32), ())
        self.assertEqual(self.backlog.status()['dates'][DAY]['ready'], old_receipt)

    def test_pending_historical_publication_defers_with_durable_demand_and_warning(self):
        self.assertEqual(self.backlog.collect(CYCLE, include_retained=False)['dates'], {})
        with patch('packages.application.fbs_accounting_historical_history.pending_receipt', return_value=object()):
            warnings = self.backlog.prepare_retained(CYCLE)
        self.assertEqual(warnings[0]['policy'], 'closed_retained_historical_pending')
        self.assertEqual(self.backlog.status()['dates'][DAY]['state'], 'accepted')
        self.assertEqual(self.backlog.publication_dates(), ())
        self.assertEqual(self.raw(DAY), self.before_raw)
        counts = {key:list(s.request_dates) for key,s in self.counters.items()}
        self.backlog.collect('2'*32, include_retained=False)
        self.assertEqual(self.backlog.prepare_retained('2'*32), ())
        self.assertNotIn('deferred_reason', self.backlog.status()['dates'][DAY])
        self.assertEqual(counts, {key:list(s.request_dates) for key,s in self.counters.items()})

    def test_real_closed_book_binding_capital_and_inventory_are_unchanged(self):
        from apps.fbs_snapshot_cost_smoke import capture
        from apps.shared_sku_cost_smoke import wb
        from apps.fbs_inventory_presentation_smoke import retained
        from packages.application import fbs_accounting_runtime as accounting
        import hashlib
        for day in (DAY, '2026-10-11'):
            image = capture(day); warehouse = wb(day)
            with patch.object(accounting, 'capture_current', return_value=image), \
                 patch.object(accounting, 'capture_wb_component', return_value=warehouse), \
                 patch.object(accounting, 'capture_retained_stages', return_value=retained(warehouse)):
                book, expected = accounting.prepare(self.runtime.runtime_dir,
                    now=datetime.fromisoformat(day+'T14:00:00+00:00'), opening=day==DAY)
                version = accounting._save_book(self.runtime.runtime_dir, book, expected=expected, operation_id='fixture-'+day)
        bound = accounting.materialize(self.original, runtime_dir=self.runtime.runtime_dir, now=NOW,
            book=book, book_version=version, ready_target={'bundle_version':self.runtime.load_current_state().bundle_version,'as_of_date':DAY})
        self.assertEqual(bound.metadata['fbs_accounting_bindings'][DAY]['book_version'], version)
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?', (_serialize_sheet_vitrina_plan(bound), DAY))
        book_bytes = hashlib.sha256(accounting.path(self.runtime.runtime_dir).read_bytes()).hexdigest()
        with readonly(self.runtime.db_path) as conn:
            inventory = [tuple(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_inventory_history_captures')]
        plan = self.candidate(); self.publish(plan)
        saved = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=DAY)
        self.assertEqual(saved.metadata['fbs_accounting_bindings'], bound.metadata['fbs_accounting_bindings'])
        self.assertEqual(book_bytes, hashlib.sha256(accounting.path(self.runtime.runtime_dir).read_bytes()).hexdigest())
        before = {r[1]:r[-1] for s in bound.sheets if s.sheet_name=='DATA_VITRINA' for r in s.rows}
        after = {r[1]:r[-1] for s in saved.sheets if s.sheet_name=='DATA_VITRINA' for r in s.rows}
        witnesses = [k for k in before if 'own_capital_' in k and before[k] not in ('',None)]
        self.assertTrue(witnesses)
        self.assertTrue(all(after[k]==before[k] for k in witnesses))
        with readonly(self.runtime.db_path) as conn:
            self.assertEqual(inventory, [tuple(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_inventory_history_captures')])

    def test_real_native_history_ack_after_crash_is_readback_and_not_second_publication(self):
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter, update_live_history
        from packages.application.web_vitrina_history_store import HistoryUnavailable
        import zlib
        plan = self.candidate(); self.publish(plan)
        self.backlog = ClosedBacklog(self.sources)
        self.backlog.record_ready(DAY)
        _, store, config = backlog_fixture.BacklogTests.native(self)
        adapter = LiveNativeAdapter(db_path=self.runtime.db_path, runtime_dir=self.runtime.runtime_dir,
            cache_dir=self.root/'proofs', now=NOW, date_from=DAY, date_to=DAY, formula_epoch=config.formula_epoch)
        with readonly(self.runtime.db_path) as conn:
            count = conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE as_of_date=?', (DAY,)).fetchone()[0]
        ready_raw = self.raw(DAY)
        with patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'):
            with self.assertRaises((ClosedBacklogConflict, HistoryUnavailable)):
                self.backlog.acknowledge(adapter=adapter, store=store, history_config=config)
            result = update_live_history(adapter=adapter, runtime=self.runtime, store=store, rolling14=False, backfill_dates=[DAY])
            self.assertEqual(result['status'], 'published', result)
            self.backlog.acknowledge(adapter=adapter, store=store, history_config=config)
        self.assertEqual(self.backlog.status()['dates'][DAY]['state'], 'acknowledged')
        with closing(store._open_day(store.root/'objects'/(store.edition()['days'][DAY]+'.sqlite3'))) as conn:
            def cell(identity):
                return json.loads(zlib.decompress(conn.execute('SELECT payload FROM cells WHERE row_id=?', (identity,)).fetchone()[0]))
            self.assertEqual(cell(f'SKU:{self.nm_ids[0]}|orderCount')[0], 5)
            self.assertEqual(cell(f'SKU:{self.nm_ids[0]}|fin_buyout_rub')[0], 700.25)
            self.assertEqual(cell('TOTAL|weighted_price_seller_discounted')[0], 600)
            self.assertAlmostEqual(cell('TOTAL|ads_drr_total')[0], 100/3000)
            self.assertIn(cell(f'SKU:{self.nm_ids[-1]}|orderCount')[0], (None, ''))
        counts = {key:list(v.request_dates) for key,v in self.counters.items()}
        self.assertEqual(self.backlog.collect('2'*32)['dates'], {})
        self.assertEqual(counts, {key:list(v.request_dates) for key,v in self.counters.items()})
        self.assertEqual(ready_raw, self.raw(DAY))
        with readonly(self.runtime.db_path) as conn:
            self.assertEqual(count, conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE as_of_date=?', (DAY,)).fetchone()[0])


if __name__ == '__main__':
    unittest.main()
