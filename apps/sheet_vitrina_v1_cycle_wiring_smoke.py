"""Actual core source→dated ready→native chain with fixed domain-stage fixtures.

Finance/FBS/warehouse domain internals are independently covered by cycle_smoke;
this disposable harness checks their one-shot place in the new owned sequence.
No provider or production operation is invoked.
"""
from contextlib import closing
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps import sheet_vitrina_v1_closed_backlog_smoke as closed_fixture
from apps.sheet_vitrina_v1_closed_backlog_smoke import Source, NOW, STAMP
from packages.application.sheet_vitrina_v1_cycle import CycleReceiptStore, CycleStageFailure, StageProof, run_cycle, STAGES
from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog, ClosedBacklogConflict
from packages.application.sheet_vitrina_v1_cycle_sources import SheetVitrinaCycleSources
from packages.application.wb_buyer_authenticated_observations import begin_run, append_observation, finish_run, classify_observation, load_daily_projection, load_source_requested_nm_ids
from packages.application.sheet_vitrina_v1_authenticated_buyer import projection_payload
from packages.application import sheet_vitrina_v1_live_plan as live
from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter, update_live_history
from packages.application.ready_publication import canonical, digest

OLD = ('2026-04-01', '2026-04-02')


class FullTransport(Source):
    def execute(self, request):
        if not hasattr(request, 'nm_ids'):
            request = SimpleNamespace(**vars(request), nm_ids=self.nm_ids)
        return super().execute(request)


class FixedDomainEntry:
    """Fixed domain versions; all collected/derived/ready/native steps are real."""
    def __init__(self, case, history_failure=False, missing_ack=False):
        self.case = case
        self.runtime = case.runtime
        self.now_factory = lambda: NOW
        self.events = []
        self.history_failure = history_failure
        self.missing_ack = missing_ack
        self.source_adapter = SheetVitrinaCycleSources(case.block)
        self.backfill = None
    def _cycle_sources(self):return self.source_adapter
    def _cycle_as_of_date(self):return '2026-04-19'
    def mark(self, name):
        self.events.append(name)
        return StageProof({name + '_fixture_version': digest(name)})
    def _cycle_finance_sources(self):return self.mark('finance_sources')
    def _cycle_fbs_generation(self):return self.mark('fbs_generation')
    def _cycle_warehouse(self, store, receipt, fbs):
        assert fbs
        return self.mark('warehouse')
    def _cycle_daily_projection(self):return self.mark('daily_projection')
    def _cycle_validate_predecessors(self, finance, fbs, material):
        assert finance == {'finance_sources_fixture_version':digest('finance_sources')}
        assert fbs == {'fbs_generation_fixture_version':digest('fbs_generation')}
        assert material['warehouse_fixture_version'] == digest('warehouse')
    def _publish(self, plan):
        self.events.append('ready:' + plan.as_of_date)
        current = self.runtime.load_current_state()
        expected = self.runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version, as_of_date=plan.as_of_date)
        self.runtime.save_sheet_vitrina_ready_snapshot(current_state=current, refreshed_at=STAMP,
            plan=plan, expected=expected, build_inputs=plan.metadata['publication_inputs'])
        return {'snapshot_id': plan.snapshot_id}, StageProof({'ready_' + plan.as_of_date: plan.snapshot_id})
    def _cycle_publish_dated_ready(self, plan):return self._publish(plan)
    def _cycle_publish_ready(self, plan):return self._publish(plan)
    def _cycle_history(self, config, receipt, ready, *, backfill_dates=(), closed_receipt=None):
        self.events.append('rolling14')
        self.backfill = backfill_dates
        assert type(backfill_dates) is tuple and len(backfill_dates) <= 2
        _, store, actual_config = self.case.native()
        assert config == actual_config
        # Canonical contiguous read context; HistoryStore limits writes to
        # rolling14 ∪ finite dates. Intervening gaps are not compiled/enrolled.
        rolling_start = (date(2026, 4, 20) - timedelta(days=13)).isoformat()
        adapter = LiveNativeAdapter(db_path=self.runtime.db_path, runtime_dir=self.runtime.runtime_dir,
            cache_dir=self.case.root/'bounded-proofs', now=NOW,
            date_from=min((rolling_start, *backfill_dates)), date_to='2026-04-20', formula_epoch=config.formula_epoch)
        footprint = lambda: {suffix: hashlib.sha256(Path(str(self.runtime.db_path) + suffix).read_bytes()).hexdigest()
            for suffix in ('', '-wal') if Path(str(self.runtime.db_path) + suffix).exists()}
        before = footprint()
        result = update_live_history(adapter=adapter, runtime=self.runtime, store=store,
            rolling14=True, backfill_dates=backfill_dates, max_recomputes=31)
        if self.history_failure:
            raise CycleStageFailure('fixture_native_terminal_unknown')
        if result['status'] != 'published':raise CycleStageFailure('fixture_native_pending')
        if backfill_dates and not self.missing_ack:
            with patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'):
                closed_receipt.acknowledge(adapter=adapter, store=store, history_config=config, backfill_dates=backfill_dates)
        assert footprint() == before
        self.history_store = store
        return StageProof({'history_edition': store._current()['current']})


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.case = closed_fixture.BacklogTests(); self.case.setUp(); self.addCleanup(self.case.doCleanups)
        case = self.case
        case.counters = {key: FullTransport(counter.source_key) for key, counter in case.counters.items()}
        for key, counter in case.counters.items():
            counter.nm_ids = case.nm_ids
            setattr(case.block, key, counter)
        with closing(sqlite3.connect(case.runtime.db_path)) as conn, conn:
            conn.execute('DELETE FROM temporal_source_closure_state')
        for day in OLD:
            case.seed_closed_cache(day); case.pending(day)
        case.pending('2026-04-19'); case.pending('2026-04-20')
        case.seed_closed_cache('2026-04-19', omit=())
        # Real qualified observations and frozen rosters, including current
        # bot/auth group. No source admission exception or selector is used.
        for day in (*OLD, '2026-04-19', '2026-04-20'):
            stamp = day + 'T08:00:00Z'
            begin_run(case.runtime, run_id=day, started_at=stamp, requested_nm_ids=case.nm_ids)
            buyer = dict(status='observed', session_status='authenticated_surface', authenticated_session_proof=True,
                measured_at=stamp, session_checked_at=stamp, wallet_price=139, normal_price=144,
                profile_reference='fixture-profile', auth_run_reference=day, destination_context={'dest_id':123})
            for nm in case.nm_ids:
                append_observation(case.runtime, run_id=day, observation=classify_observation(nm_id=nm, buyer=buyer, seller=None))
            finish_run(case.runtime, run_id=day, finished_at=stamp, status='completed')
            nm_ids, scope = load_source_requested_nm_ids(case.runtime, day)
            projection = load_daily_projection(case.runtime, day, nm_ids)
            projection['diagnostics']['eligible_scope_source'] = scope
            case.runtime.save_temporal_source_slot_snapshot(source_key=live.AUTHENTICATED_BUYER_SOURCE_KEY,
                snapshot_date=day, snapshot_role=live.TEMPORAL_ROLE_ACCEPTED_CLOSED,
                captured_at=STAMP, payload=projection_payload(projection, business_date=day))
        for counter in case.counters.values():counter.request_dates.clear()
        self.store = CycleReceiptStore(case.runtime.runtime_dir, lambda: STAMP)
        _, _, self.config = case.native()
        self.identity = patch('packages.application.sheet_vitrina_v1_cycle.process_identity', return_value='fixture:start')
        self.identity.start(); self.addCleanup(self.identity.stop)
    def execute(self, entry, *, suffix=''):
        receipt, lease = self.store.accept(request_key='wiring-' + self._testMethodName + suffix,
            slot_utc=(NOW + timedelta(hours=3 if suffix else 0)).isoformat(), config=self.config, now=NOW)
        try:return run_cycle(entry, self.store, receipt, self.config, lambda _:None)
        finally:lease.close()
    def register_next_bundle(self, change=None):
        bundle = deepcopy(closed_fixture.FIXTURE_BUNDLE)
        bundle['bundle_version'] += '-next'
        if change:change(bundle)
        self.assertEqual(self.case.runtime.ingest_bundle(bundle, activated_at=STAMP).status, 'accepted')
        return bundle['bundle_version']
    def assert_same_old_sources(self, before):
        after = self.case.backlog.status()
        for day, item in before['dates'].items():
            for key, source in item['sources'].items():
                self.assertEqual(source['anchor'], after['dates'][day]['sources'][key]['anchor'])
        self.assertEqual([day for day in self.case.counters['seller_funnel_block'].request_dates if day in OLD], list(OLD))
    def test_real_full_core_chain_and_frozen_14_union_old2(self):
        entry = FixedDomainEntry(self.case)
        with patch.object(entry.source_adapter, 'collect_sources', wraps=entry.source_adapter.collect_sources) as full_capture, \
             patch.object(entry.source_adapter, 'collected_source_summary', wraps=entry.source_adapter.collected_source_summary) as full_summary:
            result = self.execute(entry)
        self.assertEqual(full_capture.call_count, 1)
        self.assertEqual(set(full_capture.call_args.kwargs), {'as_of_date','log','execution_mode'})
        self.assertEqual(full_capture.call_args.kwargs['execution_mode'], 'auto_daily')
        self.assertEqual(tuple(item['stage'] for item in result['stages']), STAGES)
        self.assertEqual(entry.events, ['finance_sources','fbs_generation','warehouse','daily_projection',
            'ready:' + OLD[0], 'ready:' + OLD[1], 'ready:2026-04-19', 'rolling14'])
        self.assertEqual(entry.backfill, OLD)
        self.assertEqual(len(entry.history_store.edition()['days']), 16)
        self.assertTrue(all(day not in entry.history_store.edition()['days'] for day in ('2026-04-03','2026-04-04','2026-04-05','2026-04-06')))
        self.assertEqual(self.case.runtime.load_sheet_vitrina_ready_snapshot().as_of_date, '2026-04-19')
        closed = ClosedBacklog(entry.source_adapter).status()
        self.assertTrue(all(item['state'] == 'acknowledged' for item in closed['dates'].values()))
        # Canonical main reuses its seeded accepted yesterday, and requests
        # current exactly once. Closure excludes both main dates.
        self.assertEqual(self.case.counters['seller_funnel_block'].request_dates, [*OLD, '2026-04-20'])
        # Canonical scope includes cached groups too; zero transport calls for
        # an admitted cache are not exclusion of that source group.
        main_summary = entry.source_adapter.collected_source_summary(full_summary.call_args.args[0])
        self.assertTrue({'onec_stocks','wb_buyer_authenticated','web_source_snapshot','seller_funnel_snapshot',
            'fin_report_daily','sales_funnel_history','sf_period','stocks','ads_compact','ads_bids',
            'prices_snapshot','card_rating','spp','spp_proxy'} <= {slot['source_key'] for slot in main_summary['slots']})
        self.assertEqual(result['status'], 'degraded')
    def test_missing_old_cache_is_warning_not_old_day_success(self):
        with closing(sqlite3.connect(self.case.runtime.db_path)) as conn, conn:
            conn.execute("DELETE FROM temporal_source_slot_snapshots WHERE source_key='prices_snapshot' AND snapshot_date=?", (OLD[0],))
        entry = FixedDomainEntry(self.case); result = self.execute(entry)
        self.assertEqual(entry.backfill, (OLD[1],))
        closed = ClosedBacklog(entry.source_adapter).status()
        self.assertEqual(closed['dates'][OLD[0]]['state'], 'accepted')
        self.assertEqual(closed['dates'][OLD[1]]['state'], 'acknowledged')
        stage = next(item for item in result['stages'] if item['stage'] == 'closed_ready')
        self.assertEqual(stage['status'], 'degraded')
        self.assertTrue(any('closed_operand_unavailable:prices_snapshot' in item['policy'] for item in stage['warnings']))
        self.assertEqual(self.case.counters['seller_funnel_block'].request_dates.count(OLD[0]), 1)
    def test_unknown_source_stops_before_full_api_and_is_not_resent(self):
        with patch.object(self.case.block, '_load_live_sources', side_effect=RuntimeError('dispatch outcome unknown')):
            with self.assertRaises(RuntimeError):self.execute(FixedDomainEntry(self.case))
        backlog = ClosedBacklog(self.case.sources)
        with patch.object(self.case.block, '_load_live_sources', side_effect=AssertionError('uncertain resend')):
            value = backlog.status(); value['dates'].pop(OLD[1]); backlog._write(value)
            entry = FixedDomainEntry(self.case)
            receipt, lease = self.store.accept(request_key='next-owned-cycle', slot_utc='2026-04-20T11:00:00+00:00', config=self.config, now=NOW)
            try:
                with self.assertRaisesRegex(CycleStageFailure, 'closed_source_outcome_uncertain'):
                    run_cycle(entry,self.store,receipt,self.config,lambda _:None)
            finally:lease.close()
            self.assertEqual(entry.events, [])
    def test_known_native_publication_without_exact_ack_cannot_complete_cycle(self):
        with self.assertRaisesRegex(CycleStageFailure, 'closed_native_ack_missing'):
            self.execute(FixedDomainEntry(self.case, missing_ack=True))
        value = self.case.backlog.status()
        self.assertTrue(all(item['state']=='ready' for item in value['dates'].values()))
    def test_unknown_native_terminal_retains_ready_and_same_request_does_not_replay(self):
        entry = FixedDomainEntry(self.case, history_failure=True)
        with self.assertRaisesRegex(CycleStageFailure, 'fixture_native_terminal_unknown'):
            self.execute(entry)
        counts = {key:list(counter.request_dates) for key,counter in self.case.counters.items()}
        prior, lease = self.store.accept(request_key='wiring-' + self._testMethodName,
            slot_utc=NOW.isoformat(), config=self.config, now=NOW)
        self.assertIsNone(lease)
        self.assertEqual(prior['status'], 'failed')
        self.assertTrue(all(item['state']=='ready' for item in self.case.backlog.status()['dates'].values()))
        self.assertEqual(counts,{key:list(counter.request_dates) for key,counter in self.case.counters.items()})
    def test_domain_callback_failure_propagates_before_ready_or_history(self):
        entry = FixedDomainEntry(self.case)
        def failure(*args):
            entry.events.append('warehouse')
            raise CycleStageFailure('fixed_domain_failed')
        entry._cycle_warehouse = failure
        with self.assertRaisesRegex(CycleStageFailure, 'fixed_domain_failed'):
            self.execute(entry)
        self.assertFalse(any(event.startswith('ready:') or event == 'rolling14' for event in entry.events))
        self.assertEqual(self.case.counters['seller_funnel_block'].request_dates, [*OLD,'2026-04-20'])
    def test_clock_crossing_inside_a_stage_is_failure_before_following_effects(self):
        entry = FixedDomainEntry(self.case)
        original = entry._cycle_finance_sources
        def crossing():
            proof = original()
            entry.now_factory = lambda: NOW + timedelta(days=1)
            return proof
        entry._cycle_finance_sources = crossing
        with self.assertRaisesRegex(CycleStageFailure, 'cycle_business_date_changed'):
            self.execute(entry)
        self.assertEqual(entry.events, ['finance_sources'])
    def test_bare_verified_dictionary_cannot_ack(self):
        self.case.backlog.collect('1'*32)
        main = self.case.main()
        for day in OLD:
            self.case.publish(self.case.backlog.compose(main, day)); self.case.backlog.record_ready(day)
        # Authentication helper is not present in this base; the owned worker
        # integration supplies it. Absence is a hard failure, never a dict ack.
        with self.assertRaises((ModuleNotFoundError,ValueError,RuntimeError)):
            self.case.backlog._acknowledge_verified_native({'native':{}},backfill_dates=OLD)
        self.assertTrue(all(item['state']=='ready' for item in self.case.backlog.status()['dates'].values()))

    def test_identical_bundle_revision_reuses_accepted_sources_and_finishes_native(self):
        before = self.case.backlog.collect('1'*32)
        new_bundle = self.register_next_bundle()
        result = self.execute(FixedDomainEntry(self.case))
        value = self.case.backlog.status()
        self.assertEqual(value['collection_bundle_version'], before['bundle_version'])
        self.assertEqual(value['bundle_version'], new_bundle)
        self.assert_same_old_sources(before)
        self.assertTrue(all(item['state']=='acknowledged' for item in value['dates'].values()))
        self.assertEqual(result['status'], 'degraded')

    def test_ready_prior_bundle_is_evidence_not_new_publication_success(self):
        self.case.backlog.collect('1'*32); main = self.case.main()
        for day in OLD:
            self.case.publish(self.case.backlog.compose(main,day));self.case.backlog.record_ready(day)
        before = self.case.backlog.status()
        new_bundle = self.register_next_bundle(lambda bundle:bundle['config_v2'][0].update(display_name='new display context'))
        entry = FixedDomainEntry(self.case);self.execute(entry)
        value = self.case.backlog.status()
        self.assert_same_old_sources(before)
        for day in OLD:
            self.assertEqual(value['dates'][day]['previous_ready'], before['dates'][day]['ready'])
            self.assertEqual(value['dates'][day]['ready']['bundle_version'], new_bundle)
            self.assertNotEqual(value['dates'][day]['ready']['operation_id'], before['dates'][day]['ready']['operation_id'])
        self.assertEqual(entry.events[-4:], ['ready:'+OLD[0],'ready:'+OLD[1],'ready:2026-04-19','rolling14'])

    def test_new_scope_retained_cache_refusal_does_not_block_current_cycle(self):
        before = self.case.backlog.collect('1'*32)
        def add_sku(bundle):
            row=deepcopy(bundle['config_v2'][0]);row.update(nm_id=987654321,display_name='new active SKU',display_order=999)
            bundle['config_v2'].append(row)
        self.register_next_bundle(add_sku)
        current_ids=[v.nm_id for v in self.case.runtime.load_current_state().config_v2 if v.enabled]
        for counter in self.case.counters.values():counter.nm_ids=current_ids
        entry=FixedDomainEntry(self.case);result=self.execute(entry)
        self.assertEqual(entry.backfill, ())
        self.assertIn('ready:2026-04-19',entry.events)
        self.assertFalse(any('ready:'+day in entry.events for day in OLD))
        self.assert_same_old_sources(before)
        value=self.case.backlog.status()
        reasons=('closed_retained_scope_unavailable:','closed_operand_unavailable:','closed_operand_invalid:')
        self.assertTrue(all(item['state']=='accepted' and item['deferred_reason'].startswith(reasons)
            for item in value['dates'].values()))
        warnings=[warning for stage in result['stages'] if stage['stage'] in {'closed_sources','closed_ready'} for warning in stage['warnings']]
        self.assertTrue(all(any(warning['date']==day and warning['policy'].startswith(reasons)
            for warning in warnings) for day in OLD))

    def test_context_commit_crash_fresh_helper_has_no_accepted_source_resend(self):
        before=self.case.backlog.collect('1'*32);new_bundle=self.register_next_bundle()
        original=self.case.backlog._write
        def committed(value):
            original(value)
            if value['bundle_version']==new_bundle:raise RuntimeError('after-context-commit')
        with patch.object(self.case.backlog,'_write',side_effect=committed):
            with self.assertRaisesRegex(RuntimeError,'after-context-commit'):self.case.backlog.collect('2'*32)
        fresh=ClosedBacklog(self.case.sources)
        self.assertEqual(fresh.status()['collection_bundle_version'],before['bundle_version'])
        self.assertEqual(fresh.status()['bundle_version'],new_bundle)
        with patch.object(self.case.block,'_load_live_sources',side_effect=AssertionError('accepted old resend')):
            reconciled=fresh.collect('3'*32)
        self.assertEqual(tuple(reconciled['dates']),OLD)
        self.execute(FixedDomainEntry(self.case));self.assert_same_old_sources(before)

    def test_mixed_acknowledged_and_pending_demands_survive_bundle_revision(self):
        with closing(sqlite3.connect(self.case.runtime.db_path)) as conn,conn:
            conn.execute("DELETE FROM temporal_source_slot_snapshots WHERE source_key='prices_snapshot' AND snapshot_date=?",(OLD[0],))
        self.execute(FixedDomainEntry(self.case));before=self.case.backlog.status()
        self.assertEqual(before['dates'][OLD[1]]['state'],'acknowledged')
        self.register_next_bundle();entry=FixedDomainEntry(self.case)
        result=self.execute(entry,suffix='-next');after=self.case.backlog.status()
        self.assertEqual(after['dates'][OLD[1]],before['dates'][OLD[1]])
        self.assertEqual(after['dates'][OLD[0]]['state'],'accepted')
        self.assertEqual(entry.backfill,());self.assertIn('ready:2026-04-19',entry.events)
        self.assert_same_old_sources(before);self.assertEqual(result['status'],'degraded')

    def test_original_unknown_attempt_remains_failure_across_bundle_revision(self):
        with patch.object(self.case.block,'_load_live_sources',side_effect=RuntimeError('possible-original-dispatch')):
            with self.assertRaisesRegex(RuntimeError,'possible-original-dispatch'):self.case.backlog.collect('1'*32)
        before=self.case.backlog.status();self.register_next_bundle()
        entry=FixedDomainEntry(self.case)
        with patch.object(self.case.block,'_load_live_sources',side_effect=AssertionError('unknown resend')):
            with self.assertRaisesRegex(CycleStageFailure,'closed_source_outcome_uncertain'):self.execute(entry)
        after=self.case.backlog.status()
        self.assertEqual(after['bundle_version'],before['bundle_version']);self.assertEqual(entry.events,[])
        self.assertTrue(any(source['outcome']=='outcome_unknown' for item in after['dates'].values() for source in item['sources'].values()))

    def test_original_accepted_commit_is_reconciled_before_bundle_revision(self):
        original=self.case.block._load_live_sources
        def committed(*args,**kwargs):
            original(*args,**kwargs);raise RuntimeError('after-original-source-commit')
        with patch.object(self.case.block,'_load_live_sources',side_effect=committed):
            with self.assertRaisesRegex(RuntimeError,'after-original-source-commit'):self.case.backlog.collect('1'*32)
        before=self.case.backlog.status();self.register_next_bundle()
        # Reconcile only the first original dispatched date without selecting or
        # resending accepted sources; the second planned date is still ordinary.
        first=OLD[0]
        self.assertEqual(before['dates'][first]['state'],'capturing')
        value=self.case.backlog.collect('2'*32)
        self.assertTrue(all(source['anchor'] for source in value['dates'][first]['sources'].values()))
        self.assertEqual(self.case.counters['seller_funnel_block'].request_dates.count(first),1)

    def test_unknown_original_request_scope_cannot_rebind_to_new_scope(self):
        with patch.object(self.case.block,'_load_live_sources',side_effect=RuntimeError('original-dispatch')):
            with self.assertRaisesRegex(RuntimeError,'original-dispatch'):self.case.backlog.collect('1'*32)
        before=self.case.backlog.status()
        def add_sku(bundle):
            row=deepcopy(bundle['config_v2'][0]);row.update(nm_id=987654321,display_order=999)
            bundle['config_v2'].append(row)
        self.register_next_bundle(add_sku);entry=FixedDomainEntry(self.case)
        with patch.object(self.case.block,'_load_live_sources',side_effect=AssertionError('unknown scope resend')):
            with self.assertRaisesRegex(ClosedBacklogConflict,'closed_source_attempt_scope_changed:'):self.execute(entry)
        self.assertEqual(self.case.backlog.status(),before);self.assertEqual(entry.events,[])

    def test_changed_raw_is_not_a_bundle_deferral(self):
        before=self.case.backlog.collect('1'*32);self.register_next_bundle()
        with closing(sqlite3.connect(self.case.runtime.db_path)) as conn,conn:
            conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at='2026-04-20T09:00:00Z' WHERE source_key='seller_funnel_snapshot' AND snapshot_date=?",(OLD[0],))
        entry=FixedDomainEntry(self.case)
        with patch.object(self.case.block,'_load_live_sources',side_effect=AssertionError('effects before identity guard')):
            with self.assertRaisesRegex(ClosedBacklogConflict,'closed_accepted_source_changed:'):self.execute(entry)
        self.assertEqual(entry.events,[]);self.assertEqual(self.case.backlog.status(),before)

    def test_changed_authority_is_not_a_bundle_deferral(self):
        before=self.case.backlog.collect('1'*32);self.register_next_bundle()
        before['authority']['path'] += '.different-generation';self.case.backlog._write(before)
        entry=FixedDomainEntry(self.case)
        with patch.object(self.case.block,'_load_live_sources',side_effect=AssertionError('effects before identity guard')):
            with self.assertRaisesRegex(ClosedBacklogConflict,'closed_source_authority_changed'):self.execute(entry)
        self.assertEqual(entry.events,[]);self.assertEqual(self.case.backlog.status(),before)


if __name__=='__main__':unittest.main()
