"""Disposable canonical source→dated ready→native edition integration."""
from contextlib import ExitStack, closing
from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import zlib
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.sheet_vitrina_v1_refresh_read_split_smoke import CountingBlock, _build_counting_blocks, FIXTURE_BUNDLE, _NoopClosedDayWebSourceSync
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application import sheet_vitrina_v1_live_plan as live
from packages.application.sheet_vitrina_v1_cycle_sources import SheetVitrinaCycleSources
from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog, ClosedBacklogConflict
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.ready_publication import readonly, ensure_publication_schema
from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter, update_live_history
from packages.application.web_vitrina_history_store import HistoryStore, HistoryUnavailable
from packages.application.sheet_vitrina_v1_cycle import CycleHistoryConfig

NOW = datetime(2026, 4, 20, 8, tzinfo=timezone.utc)
STAMP = NOW.isoformat().replace('+00:00', 'Z')
DAYS = ['2026-04-17', '2026-04-18']
CYCLE = '1' * 32


class Source(CountingBlock):
    def execute(self, request):
        result = super().execute(request).result
        if self.source_key != 'stocks':
            prototype = vars(result.items[0]) if result.items else {}
            result.items = [SimpleNamespace(**{**prototype, 'nm_id': nm}) for nm in request.nm_ids]
            if self.source_key == 'seller_funnel_snapshot':
                for item in result.items: item.view_count = 100 + int(result.snapshot_date[-2:])
            if self.source_key == 'spp_proxy':
                for item in result.items: setattr(item, live.SPP_PROXY_METRIC_KEY, 5)
            if self.source_key == 'fin_report_daily':
                result.diagnostics = {'pagination': {'complete': True, 'terminal_status': 204}}
        return SimpleNamespace(result=result)


class BacklogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = RegistryUploadDbBackedRuntime(self.root / 'runtime')
        self.runtime.ingest_bundle(deepcopy(FIXTURE_BUNDLE), activated_at=STAMP)
        for row in FIXTURE_BUNDLE['config_v2']:
            if row['enabled']:
                self.runtime.save_nomenclature_item(dict(item_id='fixture-' + str(row['nm_id']), nm_id=row['nm_id'],
                    our_sku='fixture-' + str(row['nm_id']), is_active=True, created_at=STAMP, updated_at=STAMP))
        self.counters = {argument: Source(block.source_key) for argument, block in _build_counting_blocks().items()}
        self.counters.update(spp_proxy_block=Source('spp_proxy'), promo_live_source_block=Source('promo_by_price'))
        # Archived ONEC is deliberately unavailable, never silently excluded
        # from the full source request or turned into a zero operand.
        self.block = live.SheetVitrinaV1LivePlanBlock(self.runtime, now_factory=lambda: NOW,
            closed_day_web_source_sync=_NoopClosedDayWebSourceSync(), **self.counters)
        self.sources = SheetVitrinaCycleSources(self.block)
        self.backlog = ClosedBacklog(self.sources)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(self.block, '_sync_current_web_source_snapshot', return_value=None))
        self.stack.enter_context(patch.object(live, 'capture_mature_buyout_percent_snapshots',
            return_value=SimpleNamespace(public=lambda: {'status': 'skipped', 'fixture': True})))
        self.owner = self.stack.enter_context(heavy_admitted(self.runtime.runtime_dir, operation='cycle'))
        self.nm_ids = [v.nm_id for v in self.runtime.load_current_state().config_v2 if v.enabled]
        for day in DAYS:
            self.seed_closed_cache(day)
            self.pending(day)
        self.pending('2026-04-19'); self.pending('2026-04-20')
        for counter in self.counters.values(): counter.request_dates.clear()

    def seed_closed_cache(self, day, omit=('seller_funnel_snapshot',)):
        for counter in self.counters.values():
            key = counter.source_key
            if key in omit or key == 'onec_stocks': continue
            result = counter.execute(SimpleNamespace(nm_ids=self.nm_ids, date=day)).result
            role = live.TEMPORAL_ROLE_ACCEPTED_CURRENT if key in live.CURRENT_SNAPSHOT_ONLY_ROLLOVER_SOURCE_KEYS else live.TEMPORAL_ROLE_ACCEPTED_CLOSED
            self.runtime.save_temporal_source_slot_snapshot(source_key=key, snapshot_date=day,
                snapshot_role=role, captured_at=STAMP, payload=result)

    def pending(self, day, key='seller_funnel_snapshot', **kwargs):
        self.runtime.save_temporal_source_closure_state(source_key=key, target_date=day,
            slot_kind=live.TEMPORAL_SLOT_YESTERDAY_CLOSED, state=kwargs.get('state', live.CLOSURE_STATE_PENDING),
            attempt_count=kwargs.get('attempt_count', 0), next_retry_at=kwargs.get('next_retry_at'),
            last_reason='owned_fixture', last_attempt_at=kwargs.get('last_attempt_at'),
            last_success_at=None, accepted_at=None)

    def main(self):
        return self.sources.collect_sources(as_of_date='2026-04-19', execution_mode='auto_daily')

    def publish(self, handle):
        plan = self.sources.derive_collected(handle)
        current = self.runtime.load_current_state()
        expected = self.runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version, as_of_date=plan.as_of_date)
        self.runtime.save_sheet_vitrina_ready_snapshot(current_state=current, refreshed_at=STAMP,
            plan=plan, expected=expected, build_inputs=plan.metadata['publication_inputs'])
        return plan

    def native(self):
        # Fixture owns and provisions both real native SQLite WAL families;
        # publication/capture/compile/edition are unmocked production code.
        for path in (self.runtime.db_path, self.runtime.runtime_dir / 'fbs-snapshot-accounting.sqlite3'):
            if not path.exists(): continue
            conn = self.stack.enter_context(closing(sqlite3.connect(path)))
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
            if path == self.runtime.db_path:
                ensure_publication_schema(conn); conn.commit()
        candidate_root = (self.root / 'candidate').resolve()
        store = HistoryStore(candidate_root / 'history')
        contract_path = self.root / 'fixture-contract.json'
        contract = json.loads((ROOT / 'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json').read_text())
        contract['candidate_root'] = str(candidate_root)
        contract_path.write_text(json.dumps(contract))
        adapter = LiveNativeAdapter(db_path=self.runtime.db_path, runtime_dir=self.runtime.runtime_dir,
            cache_dir=self.root / 'proofs', now=NOW, date_from=DAYS[0], date_to='2026-04-20', formula_epoch=contract['formula_epoch'])
        config = CycleHistoryConfig(candidate_root, contract_path, contract['formula_epoch'])
        return adapter, store, config

    def test_canonical_candidate_parent_admission_and_history_child_binding(self):
        adapter, store, config = self.native()
        contract = json.loads(config.runtime_contract.read_text())
        # Actual admission executes root, reserve, epoch and all formula hash
        # checks; only the unavailable production OS mount is represented.
        with patch('apps.web_vitrina_history_candidate_build.os.path.ismount', return_value=True), \
             patch('apps.web_vitrina_history_candidate_build.subprocess.check_output', return_value=contract['mount']) as mount_probe, \
             patch('apps.web_vitrina_history_candidate_build.os.statvfs', return_value=SimpleNamespace(f_bavail=contract['reserve_bytes'] + 1, f_frsize=1)), \
             patch.object(adapter, 'capture', side_effect=AssertionError('no-obligation source capture')):
            self.assertIsNone(self.backlog.acknowledge(adapter=adapter, store=store, history_config=config))
            mount_probe.assert_called_once()
            with self.assertRaisesRegex(ClosedBacklogConflict, 'closed_native_context_changed'):
                self.backlog.acknowledge(adapter=adapter, store=HistoryStore(config.candidate_root), history_config=config)
            config.runtime_contract.write_text(json.dumps({**contract, 'candidate_root': str(store.root)}))
            with self.assertRaisesRegex(ValueError, 'history_storage_path_or_mount'):
                self.backlog.acknowledge(adapter=adapter, store=store, history_config=config)

    def test_actual_end_to_end_and_main_current_last(self):
        obligation = self.backlog.collect(CYCLE)
        self.assertEqual(list(obligation['dates']), DAYS)
        self.assertTrue(all(v['state'] == 'accepted' for v in obligation['dates'].values()), obligation)
        self.assertEqual(self.counters['seller_funnel_block'].request_dates, DAYS)
        main = self.main()
        before = {key: list(block.request_dates) for key, block in self.counters.items()}
        for day in DAYS:
            plan = self.publish(self.backlog.compose(main, day))
            # Crash after the actual ready commit, before receipt bookkeeping:
            # a new component instance reads the same complete publication.
            self.backlog = ClosedBacklog(self.sources)
            with patch.object(self.block, '_load_live_sources', side_effect=AssertionError('ready recovery refetch')):
                self.backlog.record_ready(day)
            rows = {row[1]: row for sheet in plan.sheets if sheet.sheet_name == 'DATA_VITRINA' for row in sheet.rows if len(row) >= 3}
            self.assertEqual(rows[f'SKU:{self.nm_ids[0]}|view_count'][2], 100 + int(day[-2:]))
        self.publish(main)
        self.assertEqual(self.backlog.publication_dates(), tuple(DAYS))
        self.assertEqual(before, {key: list(block.request_dates) for key, block in self.counters.items()})
        self.assertEqual(self.runtime.load_sheet_vitrina_ready_snapshot().as_of_date, '2026-04-19')
        adapter, store, config = self.native()
        footprint = lambda: {suffix: hashlib.sha256(Path(str(self.runtime.db_path) + suffix).read_bytes()).hexdigest()
                             for suffix in ('', '-wal') if Path(str(self.runtime.db_path) + suffix).exists()}
        unchanged_source = footprint()
        with patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'):
            with self.assertRaisesRegex((ClosedBacklogConflict, HistoryUnavailable), 'closed_native_publication_pending|history_not_ready'):
                self.backlog.acknowledge(adapter=adapter, store=store, history_config=config)
            result = update_live_history(adapter=adapter, runtime=self.runtime, store=store, rolling14=True,
                backfill_dates=DAYS, max_recomputes=1)
            self.assertEqual(result['status'], 'pending', result)
            with self.assertRaisesRegex((ClosedBacklogConflict, HistoryUnavailable), 'closed_native_publication_pending|history_not_ready'):
                self.backlog.acknowledge(adapter=adapter, store=store, history_config=config)
            result = update_live_history(adapter=adapter, runtime=self.runtime, store=store, rolling14=True, backfill_dates=DAYS)
            self.assertEqual(result['status'], 'published', result)
            self.assertEqual(footprint(), unchanged_source)
            # A real date/edition alone cannot acknowledge an incomplete
            # ready receipt, and a native resource refusal cannot lose demand.
            pending = self.backlog.path.read_bytes()
            ready = self.backlog.status()['dates'][DAYS[0]]['ready']
            with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
                conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET state='prepared' WHERE operation_id=? AND attempt_id=?",
                             (ready['operation_id'], ready['attempt_id']))
            with self.assertRaisesRegex(ClosedBacklogConflict, 'closed_ready_complete_receipt_missing'):
                self.backlog.acknowledge(adapter=adapter, store=store, history_config=config)
            self.assertEqual(self.backlog.path.read_bytes(), pending)
            with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
                conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET state='complete' WHERE operation_id=? AND attempt_id=?",
                             (ready['operation_id'], ready['attempt_id']))
            stable = footprint()
            adapter.max_read_bytes = 64
            with self.assertRaisesRegex(HistoryUnavailable, 'live_source_resource_limit'):
                self.backlog.acknowledge(adapter=adapter, store=store, history_config=config)
            self.assertEqual(self.backlog.path.read_bytes(), pending)
            adapter.max_read_bytes = 32 * 1024**2
            with closing(sqlite3.connect(self.runtime.db_path)) as conn:
                original = conn.execute("SELECT captured_at FROM temporal_source_slot_snapshots WHERE source_key='seller_funnel_snapshot' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'", (DAYS[0],)).fetchone()[0]
            with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
                conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at='changed' WHERE source_key='seller_funnel_snapshot' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'", (DAYS[0],))
            with self.assertRaisesRegex(ClosedBacklogConflict, 'closed_accepted_source_changed'):
                self.backlog.acknowledge(adapter=adapter, store=store, history_config=config)
            self.assertEqual(self.backlog.path.read_bytes(), pending)
            with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
                conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at=? WHERE source_key='seller_funnel_snapshot' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'", (original, DAYS[0]))
            stable = footprint()
            acknowledged = self.backlog.acknowledge(adapter=adapter, store=store, history_config=config)
            self.assertEqual(footprint(), stable)
        self.assertTrue(all(v['state'] == 'acknowledged' for v in acknowledged['dates'].values()))
        self.assertTrue(all(v['native']['edition_id'] == store._current()['current'] for v in acknowledged['dates'].values()))
        for day in DAYS:
            with closing(store._open_day(store.root / 'objects' / (store.edition()['days'][day] + '.sqlite3'))) as conn:
                cell = conn.execute('SELECT payload FROM cells WHERE row_id=?', (f'SKU:{self.nm_ids[0]}|view_count',)).fetchone()[0]
            self.assertEqual(json.loads(zlib.decompress(cell))[0], 100 + int(day[-2:]))

    def test_missing_non_due_and_changed_accepted_keep_obligation(self):
        self.backlog.collect(CYCLE); main = self.main()
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("DELETE FROM temporal_source_slot_snapshots WHERE source_key='prices_snapshot' AND snapshot_date=?", (DAYS[0],))
        with self.assertRaisesRegex(ClosedBacklogConflict, 'closed_operand_unavailable:prices_snapshot'):
            self.backlog.compose(main, DAYS[0])
        self.assertEqual(self.backlog.status()['dates'][DAYS[0]]['state'], 'accepted')
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at='changed' WHERE source_key='seller_funnel_snapshot' AND snapshot_date=?", (DAYS[0],))
        with self.assertRaisesRegex(ClosedBacklogConflict, 'closed_accepted_source_changed'):
            self.backlog.collect('2' * 32)

    def test_crash_readback_no_source_refetch_and_status_read_only(self):
        original = self.block._load_live_sources
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('after_source_acceptance')
        with patch.object(self.block, '_load_live_sources', side_effect=crash), self.assertRaisesRegex(RuntimeError, 'after_source_acceptance'):
            self.backlog.collect(CYCLE)
        self.assertEqual(self.backlog.status()['dates'][DAYS[0]]['state'], 'capturing')
        self.assertEqual(self.counters['seller_funnel_block'].request_dates, [DAYS[0]])
        self.owner.close()
        child = subprocess.run([sys.executable, __file__, 'recover', str(self.root / 'runtime')],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        response = json.loads(child.stdout)
        self.assertEqual(response['source_dates'], [DAYS[1]])
        self.owner = self.stack.enter_context(heavy_admitted(self.runtime.runtime_dir, operation='cycle'))
        other = ClosedBacklog(SheetVitrinaCycleSources(self.block))
        recovered = other.status()
        self.assertEqual(recovered['cycle_id'], CYCLE)
        self.assertEqual(self.counters['seller_funnel_block'].request_dates, [DAYS[0]])
        before = other.path.read_bytes()
        with patch.object(self.block, '_load_live_sources', side_effect=AssertionError('refetch')):
            other.collect('3' * 32); other.status()
        self.assertEqual(other.path.read_bytes(), before)
        self.assertEqual(self.counters['seller_funnel_block'].request_dates, [DAYS[0]])

    def test_uncertain_before_source_has_no_resend_even_when_due(self):
        with patch.object(self.block, '_load_live_sources', side_effect=RuntimeError('possible_unknown')):
            with self.assertRaises(RuntimeError): self.backlog.collect(CYCLE)
        with patch.object(self.block, '_load_live_sources', side_effect=AssertionError('uncertain resend')):
            # The second *planned* date can be collected, but the uncertain
            # first attempt must remain unknown across repeated reconciliations.
            value = self.backlog.status(); value['dates'].pop(DAYS[1]); self.backlog._write(value)
            self.backlog.collect('2' * 32); self.backlog.collect('3' * 32)
        self.assertEqual(self.backlog.status()['dates'][DAYS[0]]['sources']['seller_funnel_snapshot']['outcome'], 'outcome_unknown')

    def test_anchor_validation_and_digest_share_snapshot(self):
        self.backlog.collect(CYCLE)
        keeper = self.stack.enter_context(closing(sqlite3.connect(self.runtime.db_path)))
        keeper.execute('PRAGMA journal_mode=WAL')
        old = self.backlog._anchor('seller_funnel_snapshot', DAYS[0])
        original = self.block._load_slot_snapshot_status
        def race(**kwargs):
            cached = original(**kwargs)
            with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
                conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at='2026-04-20T09:00:00Z' WHERE source_key='seller_funnel_snapshot' AND snapshot_date=?", (DAYS[0],))
            return cached
        with patch.object(self.block, '_load_slot_snapshot_status', side_effect=race):
            self.assertEqual(self.backlog._valid_anchor('seller_funnel_snapshot', DAYS[0], self.nm_ids), old)
        self.assertNotEqual(self.backlog._anchor('seller_funnel_snapshot', DAYS[0]), old)

    def test_main_scope_cannot_be_partial_or_foreign(self):
        self.backlog.collect(CYCLE)
        main = self.sources.collect_sources(as_of_date='2026-04-19', source_keys=['seller_funnel_snapshot'])
        with self.assertRaisesRegex(ClosedBacklogConflict, 'closed_main_full_current_scope_required'):
            self.backlog.compose(main, DAYS[0])
        with self.assertRaisesRegex(ValueError, 'ready_collection_context_changed'):
            ClosedBacklog(SheetVitrinaCycleSources(self.block)).compose(main, DAYS[0])

    def test_mature_raw_capture_survives_composition_and_later_drift_refuses(self):
        from packages.application.ready_publication import consume_source
        self.backlog.collect(CYCLE)
        def mature(*args, **kwargs):
            payload = self.counters['sales_funnel_history_block'].execute(SimpleNamespace(nm_ids=self.nm_ids, date=DAYS[0])).result
            self.runtime.save_temporal_source_snapshot(source_key='sales_funnel_history', snapshot_date=DAYS[0], captured_at=STAMP, payload=payload)
            self.runtime.load_temporal_source_snapshot(source_key='sales_funnel_history', snapshot_date=DAYS[0])
            consume_source(source_key='sales_funnel_history', snapshot_date=DAYS[0])
            return SimpleNamespace(public=lambda: {'status': 'captured', 'fixture': True})
        with patch.object(live, 'capture_mature_buyout_percent_snapshots', side_effect=mature):
            main = self.main()
        handle = self.backlog.compose(main, DAYS[0])
        before = {key: list(block.request_dates) for key, block in self.counters.items()}
        plan = self.sources.derive_collected(handle)
        raw = [v for v in plan.metadata['publication_inputs']['consumed'].values()
               if v['source_key'] == 'sales_funnel_history' and v['snapshot_date'] == DAYS[0] and v['snapshot_role'] is None]
        self.assertEqual(len(raw), 1)
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute("UPDATE temporal_source_snapshots SET captured_at='changed' WHERE source_key='sales_funnel_history' AND snapshot_date=?", (DAYS[0],))
        with self.assertRaisesRegex(ValueError, 'ready_source_changed:sales_funnel_history'):
            self.sources.derive_collected(handle)
        self.assertEqual(before, {key: list(block.request_dates) for key, block in self.counters.items()})

    def test_malformed_receipt_status_is_bounded_and_has_no_source_effects(self):
        self.backlog.collect(CYCLE)
        malformed = self.backlog.status(); malformed['dates'][DAYS[0]]['sources']['seller_funnel_snapshot']['anchor']['digest'] = 'not-proof'
        self.backlog.path.write_text(json.dumps(malformed))
        with patch.object(self.block, '_load_live_sources', side_effect=AssertionError('status capture')):
            with self.assertRaisesRegex(ClosedBacklogConflict, 'closed_receipt_invalid'):
                self.backlog.status()
        self.assertEqual(self.counters['seller_funnel_block'].request_dates, DAYS)

    def test_backoff_fairness_excludes_today_yesterday_and_requires_owner(self):
        self.pending(DAYS[0], next_retry_at='2026-04-21T00:00:00Z')
        self.pending('2026-04-16', last_attempt_at='2026-04-19T08:00:00Z')
        self.pending('2026-04-15', last_attempt_at='2026-04-19T09:00:00Z')
        # Least-recently attempted, not oldest target date.
        self.assertEqual(list(self.backlog._due()), [DAYS[1], '2026-04-16'])
        self.owner.close()
        with self.assertRaisesRegex(RuntimeError, 'closed_backlog_requires|ownership'):
            self.backlog.collect(CYCLE)
        self.assertFalse(self.backlog.path.exists())


def recover(runtime_dir):
    runtime = RegistryUploadDbBackedRuntime(Path(runtime_dir))
    source = Source('seller_funnel_snapshot')
    block = live.SheetVitrinaV1LivePlanBlock(runtime, now_factory=lambda: NOW,
        seller_funnel_block=source, closed_day_web_source_sync=_NoopClosedDayWebSourceSync())
    with heavy_admitted(runtime.runtime_dir, operation='cycle'):
        value = ClosedBacklog(SheetVitrinaCycleSources(block)).collect('2' * 32)
    print(json.dumps(dict(receipt=value, source_dates=source.request_dates)))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'recover': recover(sys.argv[2])
    else: unittest.main()
