"""Offline midnight regression: fresh dated epochs, native sources and journal.

Provider ports are synthetic; source capture/derive, Ready CAS, accounting
preparation fences, warehouse ownership and durable journals remain real.
"""
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import sheet_vitrina_v1_cycle_wiring_smoke as wiring
from apps import sheet_vitrina_v1_cycle_smoke as fault
from apps import warehouse_cycle_recovery_retention_smoke as warehouse_fixture
from apps import fbs_accounting_runtime_smoke as accounting_fixture
from packages.application.sheet_vitrina_v1_cycle import (
    CycleDateRollover, CycleStageFailure, CycleReceiptStore, StageProof,
    require_cycle_business_date, run_cycle,
)
from packages.application.sheet_vitrina_v1_cycle_sources import SheetVitrinaCycleSources
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint as Entry
from packages.application.ready_publication import canonical, digest
from packages.business_time import current_business_date_iso, default_business_as_of_date


class AdvancingEntry(wiring.FixedDomainEntry):
    def __init__(self, case, *, crossing='finance_sources', failure=None):
        super().__init__(case)
        self.clock = [wiring.NOW.replace(hour=18, minute=59)]
        self.now_factory = lambda: self.clock[0]
        case.block.now_factory = self.now_factory
        self.crossing, self.failure, self.crossed = crossing, failure, False
        self.adapters = []

    def _cycle_sources(self):
        adapter = SheetVitrinaCycleSources(self.case.block,
            expected_business_date=current_business_date_iso(self.clock[0]))
        self.adapters.append(adapter)
        return adapter

    def _cycle_as_of_date(self):
        return default_business_as_of_date(self.clock[0])

    def mark(self, name):
        self.events.append(name)
        day = current_business_date_iso(self.clock[0])
        if name == self.crossing and not self.crossed:
            self.crossed = True
            self.clock[0] += timedelta(minutes=2)
            if self.failure is not None:
                raise self.failure
        return StageProof({name + '_fixture_version': digest(name + ':' + day)})

    def _cycle_validate_predecessors(self, finance, fbs, material):
        day = current_business_date_iso(self.clock[0])
        assert finance == {'finance_sources_fixture_version': digest('finance_sources:' + day)}
        assert fbs == {'fbs_generation_fixture_version': digest('fbs_generation:' + day)}
        assert material['warehouse_fixture_version'] == digest('warehouse:' + day)

    def _cycle_history(self, config, receipt, ready, *, backfill_dates=(), closed_receipt=None, before_child=None):
        if before_child is not None:before_child()
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter, update_live_history
        self.events.append('rolling14')
        _, store, _ = self.case.native()
        today = current_business_date_iso(self.clock[0])
        start = (date.fromisoformat(today) - timedelta(days=13)).isoformat()
        adapter = LiveNativeAdapter(db_path=self.runtime.db_path, runtime_dir=self.runtime.runtime_dir,
            cache_dir=self.case.root / 'midnight-proofs', now=self.clock[0],
            date_from=min((start, *backfill_dates)), date_to=today, formula_epoch=config.formula_epoch)
        result = update_live_history(adapter=adapter, runtime=self.runtime, store=store,
            rolling14=True, backfill_dates=backfill_dates, max_recomputes=31)
        assert result['status'] == 'published', result
        if backfill_dates:
            with patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'):
                closed_receipt.acknowledge(adapter=adapter, store=store, history_config=config,
                                         backfill_dates=backfill_dates)
        self.history_store = store
        return StageProof({'history_edition': store._current()['current']})


class MidnightIntegration(unittest.TestCase):
    def test_full_new_day_sources_ready_and_native_history_replace_completed_old_epoch(self):
        case = wiring.WiringTests('test_real_full_core_chain_and_frozen_14_union_old2')
        case.setUp(); self.addCleanup(case.doCleanups)
        entry = AdvancingEntry(case.case, crossing='warehouse')
        store = CycleReceiptStore(entry.runtime.runtime_dir, lambda: entry.clock[0].isoformat())
        receipt, lease = store.accept(request_key='midnight-full-source-fixture',
            slot_utc=entry.clock[0].isoformat(), config=case.config, now=entry.clock[0])
        self.addCleanup(lease.close)
        original_identity = {key: receipt[key] for key in ('cycle_id', 'request_key', 'request_fingerprint', 'slot_utc')}
        transitions = []
        original_write = store.write
        def write(value):
            original_write(value)
            if value.get('epoch_transition'):
                observed = store.read(value['cycle_id'])
                transitions.append(observed)
                self.assertEqual(observed['status'], 'running')
                self.assertIsNone(observed['finished_at'])
                from packages.application import business_data_cycle_dispatch as dispatch
                identity = dict(schema=1, cycle_id=value['cycle_id'], request_key=value['request_key'],
                                request_fingerprint=value['request_fingerprint'])
                with patch.object(dispatch, '_identity', return_value='synthetic-sha'), \
                     patch.object(dispatch, '_decode', return_value=identity):
                    transport = dispatch.readback(entry.runtime.runtime_dir, 'synthetic-dispatch')
                self.assertEqual(transport['status'], 'running')
                self.assertIsNone(transport['receipt']['finished_at'])
        with patch.object(store, 'write', side_effect=write):
            result = run_cycle(entry, store, receipt, case.config, lambda _: None)
        self.assertIn(result['status'], {'complete', 'degraded'})
        self.assertEqual(result['business_date'], '2026-04-21')
        self.assertEqual(result['accepted_business_date'], '2026-04-20')
        self.assertEqual({key: result[key] for key in original_identity}, original_identity)
        self.assertEqual(len(entry.adapters), 2)
        self.assertTrue(transitions)
        self.assertNotIn('epoch_transition', result)
        archive_raw = (store.root / result['rollover']['archive_file']).read_text()
        self.assertEqual(digest(archive_raw), result['rollover']['archive_digest'])
        archive = json.loads(archive_raw)
        self.assertEqual(archive['epoch_disposition'], 'rolled_over')
        warehouse = next(s for s in archive['stages'] if s['stage'] == 'warehouse')
        self.assertEqual(warehouse['status'], 'complete')
        self.assertEqual(warehouse['versions'], {'warehouse_fixture_version': digest('warehouse:2026-04-20')})
        self.assertEqual(result['final_versions']['warehouse_fixture_version'], digest('warehouse:2026-04-21'))
        old_api = next(s for s in archive['stages'] if s['stage'] == 'api_sources')
        new_api = next(s for s in result['stages'] if s['stage'] == 'api_sources')
        self.assertEqual({s['date'] for s in json.loads(old_api['versions']['source_capture_outcomes'])},
                         {'2026-04-19', '2026-04-20'})
        self.assertEqual({s['date'] for s in json.loads(new_api['versions']['source_capture_outcomes'])},
                         {'2026-04-20', '2026-04-21'})
        self.assertEqual(result['summary']['rollover'], result['rollover'])
        self.assertEqual(entry.runtime.load_sheet_vitrina_ready_snapshot().as_of_date, '2026-04-20')
        self.assertEqual(entry.events.count('rolling14'), 1)
        self.assertTrue(all(i['state'] == 'acknowledged' for i in case.case.backlog.status()['dates'].values()))

    def test_ready_boundary_after_derive_is_refused_before_publication_intent(self):
        case = wiring.WiringTests('test_real_full_core_chain_and_frozen_14_union_old2')
        case.setUp(); self.addCleanup(case.doCleanups)
        clock = [wiring.NOW]
        entry = Entry(runtime=case.case.runtime, runtime_dir=case.case.runtime.runtime_dir,
            now_factory=lambda: clock[0], activated_at_factory=lambda: wiring.STAMP,
            refreshed_at_factory=lambda: wiring.STAMP)
        handle = case.case.main()
        plan = case.case.sources.derive_collected(handle)
        runtime = entry.runtime
        with sqlite3.connect(runtime.db_path) as conn:
            before = conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications').fetchone()[0]
        original_save = runtime.save_sheet_vitrina_ready_snapshot
        def crossing_save(**kwargs):
            clock[0] += timedelta(days=1)
            return original_save(**kwargs)
        for dated in (False, True):
            clock[0] = wiring.NOW
            with patch.object(runtime, 'save_sheet_vitrina_ready_snapshot', side_effect=crossing_save):
                with self.assertRaises(CycleDateRollover):
                    if dated:
                        entry._cycle_publish_dated_ready(plan, expected_business_date='2026-04-20')
                    else:
                        entry._cycle_publish_ready(plan)
        with sqlite3.connect(runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications').fetchone()[0], before)


class RolloverRefusalTests(fault.CycleTests):
    # Only these tests are loaded by main; inherited ordinary regressions remain
    # owned by the original smoke suite.
    def advancing_fake(self):
        fake = fault.CycleFake(self.root)
        clock = [fault.NOW]
        fake.now_factory = lambda: clock[0]
        fake.sheet_plan_block.collected_source_summary = lambda handle: {
            **fault.summary(), 'business_date': current_business_date_iso(clock[0])}
        return fake, clock

    def run_owned(self, fake, receipt):
        with heavy_admitted(self.root, operation='cycle'):
            return run_cycle(fake, self.store, receipt, self.config, lambda _: None)

    def test_unknown_or_missing_proof_is_never_retried_on_changed_calendar(self):
        for failure in ('unknown', 'missing'):
            with self.subTest(failure=failure):
                receipt, lease = self.accepted(key='midnight-' + failure,
                    slot=(fault.NOW + timedelta(hours=3 if failure == 'missing' else 0)).isoformat())
                fake, clock = self.advancing_fake()
                def finance():
                    fake.events.append('finance_sources')
                    clock[0] += timedelta(days=1)
                    if failure == 'unknown':
                        raise sqlite3.OperationalError('synthetic unknown commit')
                    return StageProof({})
                fake._cycle_finance_sources = finance
                try:
                    with self.assertRaises((sqlite3.OperationalError, CycleStageFailure)):
                        self.run_owned(fake, receipt)
                finally: lease.close()
                self.assertNotIn('rollover', receipt)
                self.assertEqual(fake.events.count('finance_sources'), 1)
                self.assertEqual(receipt['status'], 'failed')

    def test_second_rollover_is_bounded_and_process_loss_does_not_resume(self):
        receipt, lease = self.accepted(key='midnight-twice')
        fake, clock = self.advancing_fake()
        original = fake._cycle_finance_sources
        def finance():
            proof = original()
            clock[0] += timedelta(days=1)
            return proof
        fake._cycle_finance_sources = finance
        try:
            with self.assertRaisesRegex(CycleStageFailure, 'cycle_rollover_limit'):
                self.run_owned(fake, receipt)
        finally: lease.close()
        self.assertEqual(fake.events.count('finance_sources'), 2)
        self.assertEqual(receipt['status'], 'failed')
        self.assertNotIn('epoch_transition', receipt)
        self.assertFalse(any(stage['status'] == 'running' for stage in receipt['stages']))
        # A crash after an archive/new-epoch write remains interrupted; reading
        # and dedup of the original request never start source effects.
        receipt['status'] = 'running'; self.store.write(receipt)
        events = list(fake.events)
        with patch('packages.application.sheet_vitrina_v1_cycle.process_identity', return_value='other-process'):
            self.assertEqual(self.store.read(receipt['cycle_id'])['status'], 'interrupted')
            prior, second = self.accepted(key='midnight-twice')
        self.assertIsNone(second)
        self.assertEqual(prior['status'], 'interrupted')
        self.assertEqual(fake.events, events)

    def test_completed_history_proof_survives_predecessor_date_check_race(self):
        receipt, lease = self.accepted(key='midnight-history-boundary')
        fake, clock = self.advancing_fake()
        completed, crossed = [False], [False]
        original_history = fake._cycle_history
        original_validate = fake._cycle_validate_predecessors
        def history(*args, **kwargs):
            proof = original_history(*args, **kwargs)
            completed[0] = True
            return proof
        def validate(*args):
            original_validate(*args)
            if completed[0] and not crossed[0]:
                crossed[0] = True
                expected = current_business_date_iso(clock[0])
                clock[0] += timedelta(days=1)
                require_cycle_business_date(expected, clock[0])
        fake._cycle_history = history
        fake._cycle_validate_predecessors = validate
        try:
            result = self.run_owned(fake, receipt)
        finally: lease.close()
        self.assertEqual(result['status'], 'complete')
        archive = json.loads((self.store.root / result['rollover']['archive_file']).read_text())
        native = next(s for s in archive['stages'] if s['stage'] == 'rolling14')
        self.assertEqual(native['status'], 'complete')
        self.assertEqual(native['versions'], {'rolling14_version': 'verified'})
        self.assertEqual(fake.events.count('rolling14'), 2)

    def test_crash_after_archive_before_epoch_write_cannot_resume_or_recollect(self):
        from packages.application import sheet_vitrina_v1_cycle as cycle
        receipt, lease = self.accepted(key='midnight-crash-window')
        fake, clock = self.advancing_fake()
        original_finance = fake._cycle_finance_sources
        def finance():
            proof = original_finance()
            clock[0] += timedelta(days=1)
            return proof
        fake._cycle_finance_sources = finance
        original_write = self.store.write
        def stop_after_archive(value):
            # roll_over has fsynced the archive and directory before reaching
            # this first canonical new-epoch write. Cancellation leaves the
            # owning process alive, so owner-loss projection cannot rescue it.
            if value.get('rollover') and value['status'] == 'running':
                raise KeyboardInterrupt('synthetic cancellation after durable archive')
            original_write(value)
        try:
            with patch.object(self.store, 'write', side_effect=stop_after_archive):
                with self.assertRaises(KeyboardInterrupt): self.run_owned(fake, receipt)
        finally: lease.close()
        archive = json.loads(next(self.store.root.glob('*.rollover-0.json')).read_text())
        self.assertEqual(archive['stages'][2]['status'], 'complete')
        self.assertEqual(archive['stages'][2]['versions'], {'finance_sources_version': 'verified'})
        events = list(fake.events)
        saved = self.store.read(receipt['cycle_id'])
        self.assertEqual(saved['status'], 'interrupted')
        self.assertFalse(any(stage['status'] == 'running' for stage in saved['stages']))
        self.assertNotIn('epoch_transition', saved)
        with patch.object(cycle, 'process_identity', return_value='lost-owner'):
            saved = self.store.read(receipt['cycle_id'])
            self.assertEqual(saved['status'], 'interrupted')
            self.assertTrue(saved['rollover']['archive_digest'])
            prior, second = self.accepted(key='midnight-crash-window')
        self.assertIsNone(second)
        self.assertEqual(prior['status'], 'interrupted')
        self.assertEqual(fake.events, events)


class OfficialFbsMidnightTests(unittest.TestCase):
    def test_completed_capture_uses_real_age_and_foreign_stale_or_incomplete_still_refuse(self):
        from apps import wb_fbs_complete_snapshot_smoke as fixture
        from packages.application.wb_fbs_warehouse_registry import (
            WbFbsWarehouseRegistry, STOCK_RUNS_TABLE, WAREHOUSE_MAPPINGS_TABLE,
        )
        from packages.application.official_fbs_stock_read import read_complete_official_fbs_stock
        from packages.application.ready_publication import readonly
        from packages.application.sheet_vitrina_v1_cycle import FbsGenerationFailure
        # All provider identities in this regression are synthetic.
        with patch.multiple(fixture, MOSCOW_WAREHOUSE_ID=41001, MOSCOW_OFFICE_ID=51001,
                            ORENBURG_WAREHOUSE_ID=41002, ORENBURG_OFFICE_ID=51002):
            for corruption in (None, 'stale', 'foreign_mapping', 'incomplete', 'mixed_days'):
                with self.subTest(corruption=corruption), TemporaryDirectory() as temp:
                    db = Path(temp) / 'operational.sqlite3'
                    fixture._seed(db)
                    clock = fixture.Clock()
                    clock.value = datetime(2026, 8, 11, 18, 59, tzinfo=timezone.utc)
                    source = fixture.OfficialSource()
                    registry = WbFbsWarehouseRegistry(db_path=db, runtime_dir=db.parent, timestamp_factory=clock,
                        source=source, catalog_source=fixture.CatalogSource())
                    clock.value = datetime(2026, 8, 11, 18, 57, tzinfo=timezone.utc)
                    registry.collect()  # Establish the ordinary registry schema/prior sequence.
                    if corruption == 'incomplete':
                        registry.source = fixture.ConfirmationSource('amount')
                    clock.value = datetime(2026, 8, 11, 18, 59, tzinfo=timezone.utc)
                    collect = registry.collect
                    def finished():
                        result = collect()
                        clock.value += timedelta(hours=10) if corruption == 'stale' else timedelta(minutes=2)
                        if corruption in {'foreign_mapping', 'mixed_days'}:
                            with sqlite3.connect(db) as conn:
                                if corruption == 'foreign_mapping':
                                    conn.execute('DROP TRIGGER wb_fbs_warehouse_mapping_no_update')
                                    conn.execute(f'UPDATE {WAREHOUSE_MAPPINGS_TABLE} SET active=0')
                                else:
                                    conn.execute('DROP TRIGGER wb_fbs_stock_runs_no_update')
                                    conn.execute(f'UPDATE {STOCK_RUNS_TABLE} SET snapshot_at=? WHERE seller_warehouse_id=41002',
                                                 (clock.value.isoformat(),))
                        return result
                    entry = SimpleNamespace(runtime=SimpleNamespace(db_path=db),
                        wb_fbs_warehouse_registry=registry, now_factory=lambda: clock.value)
                    with patch.object(registry, 'collect', side_effect=finished) as collected:
                        if corruption is None:
                            proof = Entry._cycle_fbs_generation(entry)
                            self.assertEqual(proof.versions['fbs_business_date'], '2026-08-11')
                            self.assertEqual(current_business_date_iso(clock.value), '2026-08-12')
                            # The ordinary new-day reader still refuses this
                            # prior-day generation; only its completed old-epoch
                            # proof can be archived before full recollection.
                            with readonly(db) as conn, self.assertRaisesRegex(ValueError, 'official_snapshot_not_fresh_current_day'):
                                read_complete_official_fbs_stock(conn, universe=None, day='2026-08-12', now=clock.value)
                        else:
                            with self.assertRaises((ValueError, FbsGenerationFailure)):
                                Entry._cycle_fbs_generation(entry)
                        self.assertEqual(collected.call_count, 1)


class WarehouseMidnightTests(warehouse_fixture.OwnedRetentionTests):
    def test_date_boundary_before_or_during_apply_preserves_domain_receipts(self):
        for boundary in ('capture', 'apply'):
            with self.subTest(boundary=boundary), ExitStack() as stack:
                self.ports(stack)
                self.now[0] = datetime(2026, 8, 11, 18, 59, tzinfo=timezone.utc)
                expected = current_business_date_iso(self.now[0])
                original_apply = self.entry.warehouse_functional_block.apply_plan
                def build(**kwargs):
                    if boundary == 'capture': self.now[0] += timedelta(minutes=2)
                    return {'plan_fingerprint': 'synthetic-plan', 'diff': {}}
                def apply(*args, **kwargs):
                    result = original_apply(*args, **kwargs)
                    self.now[0] += timedelta(minutes=2)
                    return result
                stack.enter_context(patch.object(self.entry.warehouse_functional_block, 'build_sync_plan', side_effect=build))
                applied = stack.enter_context(patch.object(self.entry.warehouse_functional_block, 'apply_plan', side_effect=apply))
                accounting = stack.enter_context(patch('apps.warehouse_functional_runner._publish_fbs_snapshot_accounting'))
                stack.enter_context(heavy_admitted(self.root, operation='cycle'))
                owner = stack.enter_context(warehouse_functional_job_lock(self.root))
                with self.assertRaises(CycleDateRollover):
                    Entry._handle_owned_warehouse_manual_sync_request(self.entry, owner_token=owner['owner_token'],
                        before_capture=lambda now: require_cycle_business_date(expected, now))
                accounting.assert_not_called()
                self.assertEqual(applied.call_count, int(boundary == 'apply'))
            journal = self.journal.public_status()
            self.assertEqual(journal['manual_updates']['status'], 'failed')
            publication = next(p for p in journal['phases'] if p['phase_key'] == 'functional_publication')
            self.assertEqual(publication['status'], 'failed')
            if boundary == 'apply':
                evidence = publication['details']['completed_effects']
                self.assertTrue(evidence['functional_version'].startswith('fixture-'))
                self.assertTrue(evidence['accounting_publication_not_started'])


class AccountingMidnightTests(accounting_fixture.RuntimeTests):
    def test_boundary_after_snapshot_pin_does_not_invalidate_or_close_saved_day(self):
        from packages.application import fbs_accounting_runtime as accounting
        from packages.application import ready_publication
        self.opening()
        before = accounting.load(self.root)
        real_material = ready_publication.capture_material
        clock = [datetime(2026, 9, 7, 18, 59, tzinfo=timezone.utc)]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return clock[0]
        def pin(conn):
            result = real_material(conn)
            clock[0] += timedelta(minutes=2)
            return result
        with patch.object(accounting, 'datetime', Clock), \
             patch.object(ready_publication, 'capture_material', side_effect=pin), \
             patch.object(accounting, '_record_prepare_failure', wraps=accounting._record_prepare_failure) as invalidation:
            with self.assertRaises(CycleDateRollover):
                accounting.refresh(self.root,
                    before_capture=lambda now: require_cycle_business_date('2026-09-07', now))
        invalidation.assert_not_called()
        self.assertEqual(accounting.load(self.root), before)


if __name__ == '__main__':
    suite = unittest.TestSuite()
    for cls in (MidnightIntegration, RolloverRefusalTests, OfficialFbsMidnightTests, WarehouseMidnightTests, AccountingMidnightTests):
        for name in cls.__dict__:
            if name.startswith('test_'): suite.addTest(cls(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
