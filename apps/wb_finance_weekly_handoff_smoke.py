#!/usr/bin/env python3
"""Real disposable Finance CAS at the exact pre-write handoff boundary."""
from contextlib import closing, ExitStack
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.wb_finance_weekly_cost_cutover_smoke import _row, _seed_sources
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock, FinanceStaleCostHandoffError
from packages.application.storage_registry import atomic_write_manifest, build_manifest

START, END = date(2026, 6, 29), date(2026, 7, 5)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='finance-handoff-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.block = WbFinanceWeeklyBlock(self.root, seller_id='canonical',
            now_factory=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc))
        self.block.ensure_schema(); _seed_sources(self.block.db_path)
        self.block.ingest_week(START, END, [_row(1, '2026-07-01')])
        self.block.ingest_week(date(2026, 6, 22), date(2026, 6, 28), [_row(2, '2026-06-23', nm_id=103)])
        with sqlite3.connect(self.block.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost SET wac_rub='201',fingerprint='before' WHERE as_of_date='2026-07-01' AND nm_id=101")
            conn.execute('CREATE TABLE unrelated_status(value INTEGER)'); conn.execute('INSERT INTO unrelated_status VALUES(0)')
            conn.commit()
        self.raw_path, self.raw_table = self.block.db_path, 'wb_finance_weekly_raw_rows'

    def split(self):
        self.raw_path = self.root / 'raw.sqlite3'
        with closing(sqlite3.connect(self.block.db_path)) as main, closing(sqlite3.connect(self.raw_path)) as raw:
            main.backup(raw)
        for path, table, logical, revision, generation in (
            (self.block.db_path, 'finance_operational_schema_meta', 'operational', 'operational_v1', 'op-test'),
            (self.raw_path, 'finance_raw_schema_meta', 'finance_raw', 'finance_raw_v1', 'raw-test')):
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute(f'CREATE TABLE IF NOT EXISTS {table}(singleton INTEGER PRIMARY KEY,schema_revision TEXT,logical_store TEXT,generation_id TEXT,generation_epoch TEXT,source_fingerprint TEXT,created_at TEXT)')
                conn.execute(f'INSERT OR REPLACE INTO {table} VALUES(1,?,?,?,?,?,?)',
                    (revision, logical, generation, 'test', 'sha256:'+'a'*64, '2026-07-27T00:00:00Z'))
                if path == self.raw_path:
                    conn.execute('ALTER TABLE wb_finance_weekly_raw_rows RENAME TO finance_raw_current_rows')
        manifest = build_manifest(state='cutover', canonical_source='split', generation_epoch='test',
            raw_generation_id='raw-test', raw_relative_path=self.raw_path.name, raw_watermark='1',
            operational_generation_id='op-test', operational_relative_path=self.block.db_path.name,
            operational_watermark='1', rollback_generation_id='monolith', source_fingerprint='sha256:'+'a'*64)
        atomic_write_manifest(self.block.store_registry.manifest_path, manifest)
        self.raw_table = 'finance_raw_current_rows'

    def image(self):
        with self.block._connect_stale_cost_plan() as conn:
            return self.block._finance_target_images(conn, {('canonical', START.isoformat(), END.isoformat())})

    def run_apply(self, inject=lambda attempt: None):
        plan = self.block.plan_stale_cost_weeks(date_from=START, date_to=END)
        self.assertEqual(plan['stale_week_count'], 1)
        original_connect, original_replace = self.block._connect, self.block._replace_finance_target_images
        calls = {'writer': 0, 'dml': 0}
        def writer():
            calls['writer'] += 1; inject(calls['writer'])
            return original_connect()
        def replace(*args, **kwargs):
            calls['dml'] += 1
            return original_replace(*args, **kwargs)
        self.calls = calls
        with patch.object(self.block, '_connect', side_effect=writer), \
             patch.object(self.block, '_replace_finance_target_images', side_effect=replace):
            return self.block.apply_stale_cost_weeks(expected_fingerprint=plan['fingerprint'], date_from=START, date_to=END)

    def commit(self, sql, *, raw=False, args=()):
        with closing(sqlite3.connect(self.raw_path if raw else self.block.db_path)) as conn, conn:
            conn.execute(sql, args)

    def test_unrelated_main_handoff_one_cas_preserves_other_finance(self):
        with self.block._connect_stale_cost_plan() as conn:
            before = self.block._finance_state_digest(conn, target_keys={('canonical', START.isoformat(), END.isoformat())}, target_only=False)
        result = self.run_apply(lambda n: self.commit('UPDATE unrelated_status SET value=1') if n == 1 else None)
        self.assertEqual(self.calls, {'writer': 2, 'dml': 1})
        self.assertEqual(result['handoff_revalidation_count'], 1)
        self.assertEqual(result['handoff_guard']['changed_schemas'], ['main'])
        self.assertEqual(result['handoff_guard']['classification'], 'unrelated_to_target')
        self.assertEqual(result['non_target_digest_after'], before)
        self.assertEqual(result['post_verify_stale_week_count'], 0)

    def test_actual_main_cost_raw_report_and_target_drift_zero_dml(self):
        changes = (
            ("UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost SET wac_rub='202',fingerprint='actual' WHERE as_of_date='2026-07-01' AND nm_id=101", 'finance_dependency_changed'),
            ("UPDATE wb_finance_weekly_raw_rows SET raw_json='{}' WHERE rrd_id='1'", 'finance_dependency_changed'),
            ("UPDATE wb_finance_weekly_reports SET content_hash='actual' WHERE report_id='1'", 'finance_dependency_changed'),
            ("UPDATE wb_finance_weekly_aggregates SET metrics_json='{}' WHERE week_start='2026-06-29'", 'finance_target_changed'))
        for sql, reason in changes:
            with self.subTest(reason=reason, sql=sql):
                # Each case is independent; external target mutation is preserved, not undone.
                case = HandoffTests(); case.setUp()
                try:
                    before = case.image()
                    with self.assertRaises(FinanceStaleCostHandoffError) as caught:
                        case.run_apply(lambda n: case.commit(sql) if n == 1 else None)
                    self.assertEqual(caught.exception.reason, reason)
                    self.assertEqual(case.calls['dml'], 0)
                    if reason != 'finance_target_changed': self.assertEqual(case.image(), before)
                finally: case.doCleanups()

    def test_split_main_can_revalidate_but_any_raw_commit_is_terminal(self):
        self.split()
        result = self.run_apply(lambda n: self.commit('UPDATE unrelated_status SET value=1') if n == 1 else None)
        self.assertEqual(result['handoff_revalidation_count'], 1)
        self.assertEqual(self.calls['dml'], 1)
        self.commit("UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost SET wac_rub='203',fingerprint='next' WHERE as_of_date='2026-07-01' AND nm_id=101")
        before = self.image()
        with self.assertRaises(FinanceStaleCostHandoffError) as caught:
            self.run_apply(lambda n: self.commit('UPDATE unrelated_status SET value=2', raw=True))
        self.assertEqual(caught.exception.reason, 'finance_raw_handoff_changed')
        self.assertEqual(caught.exception.diagnostic()['changed_schemas'], ['finance_raw_store'])
        self.assertEqual(self.calls, {'writer': 1, 'dml': 0})
        self.assertEqual(self.image(), before)

    def test_repeated_main_drift_exhausts_before_dml(self):
        before = self.image()
        with self.assertRaises(FinanceStaleCostHandoffError) as caught:
            self.run_apply(lambda n: self.commit('UPDATE unrelated_status SET value=value+1'))
        self.assertEqual(caught.exception.reason, 'finance_handoff_exhausted')
        self.assertEqual(caught.exception.diagnostic()['attempt'], 2)
        self.assertEqual(self.calls, {'writer': 2, 'dml': 0})
        self.assertEqual(self.image(), before)

    def test_split_actual_raw_drift_fails_without_revalidation(self):
        self.split(); before = self.image()
        with self.assertRaises(FinanceStaleCostHandoffError) as caught:
            self.run_apply(lambda n: self.commit("UPDATE finance_raw_current_rows SET raw_json='{}' WHERE rrd_id='1'", raw=True))
        self.assertEqual(caught.exception.reason, 'finance_raw_handoff_changed')
        self.assertEqual(self.calls, {'writer':1, 'dml':0})
        self.assertEqual(self.image(), before)

    def test_writer_generation_identity_change_is_terminal(self):
        original = self.block._sqlite_persistent_identity
        calls = []
        def changed(conn):
            result = original(conn); calls.append(result)
            return result if len(calls)==1 else (('main', '/different/generation.sqlite3'),)
        before = self.image()
        with patch.object(self.block, '_sqlite_persistent_identity', side_effect=changed):
            with self.assertRaises(FinanceStaleCostHandoffError) as caught: self.run_apply()
        self.assertEqual(caught.exception.reason, 'finance_handoff_identity_changed')
        self.assertEqual(self.calls, {'writer':1, 'dml':0})
        self.assertEqual(self.image(), before)

    def test_validation_is_bracketed_and_projection_is_not_repeated(self):
        original, calls = self.block._finance_source_dependency_fingerprint, []
        def dependency(conn, **kwargs):
            result = original(conn, **kwargs)
            if kwargs.get('force_reload') and not calls:
                calls.append('changed'); self.commit('UPDATE unrelated_status SET value=1')
            return result
        with patch.object(self.block, '_finance_source_dependency_fingerprint', side_effect=dependency), \
             patch.object(self.block, '_build_week_target_projection', wraps=self.block._build_week_target_projection) as projection:
            result = self.run_apply()
        self.assertEqual(result['handoff_revalidation_count'], 1)
        self.assertEqual(projection.call_count, 1)
        self.assertEqual(self.calls, {'writer': 1, 'dml': 1})

    def test_no_retry_once_replace_has_started_even_for_typed_error(self):
        before = self.image()
        def partial(conn, **kwargs):
            conn.execute("UPDATE wb_finance_weekly_aggregates SET metrics_json='{}' WHERE week_start='2026-06-29'")
            raise FinanceStaleCostHandoffError('finance_handoff_changed', phase='writer_handoff',
                classification='unclassified_commit', attempt=1, before={'main':1}, after={'main':2})
        with patch.object(self.block, '_replace_finance_target_images', side_effect=partial):
            with self.assertRaises(FinanceStaleCostHandoffError): self.run_apply()
        self.assertEqual(self.calls, {'writer':1, 'dml':1})
        self.assertEqual(self.image(), before)

    def test_post_commit_failure_never_replays(self):
        original, calls = self.block._connect_stale_cost_plan, []
        def readonly_plan():
            calls.append(1)
            if len(calls)==3:  # run_apply plan, apply query plan, post-commit readback
                raise FinanceStaleCostHandoffError('finance_handoff_changed', phase='writer_handoff',
                    classification='unclassified_commit', attempt=1)
            return original()
        before = self.image()
        with patch.object(self.block, '_connect_stale_cost_plan', side_effect=readonly_plan):
            with self.assertRaises(FinanceStaleCostHandoffError): self.run_apply()
        self.assertEqual(self.calls, {'writer':1, 'dml':1})
        self.assertNotEqual(self.image(), before)

    def test_diagnostics_whitelist_no_secret_or_database_paths(self):
        error = FinanceStaleCostHandoffError('secret-response-body', phase='secret', classification='secret',
            attempt=999, before={'main':1, 'authorization':'secret'}, after={'main':2, 'finance_raw_store':'secret'})
        safe = error.diagnostic()
        self.assertEqual(safe['changed_schemas'], ['main'])
        self.assertNotIn('secret', json.dumps(safe)); self.assertNotIn('secret', str(error))
        safe['before_tokens']['main']=999
        self.assertEqual(error.diagnostic()['before_tokens']['main'],1)


    def test_durable_dependent_phase_retains_safe_guard_after_restart(self):
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        from packages.application.warehouse_update_journal import WarehouseUpdateJournal
        from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
        from packages.application.business_data_heavy_admission import heavy_admitted
        from packages.application import fbs_accounting_runtime as accounting
        from packages.application import operator_ff_overhead as overhead
        from packages.application import operator_warehouse_documents as documents
        error = FinanceStaleCostHandoffError('finance_handoff_exhausted', phase='writer_handoff',
            classification='unclassified_commit', attempt=2,
            before={'main':4, 'authorization':'secret'}, after={'main':5, 'raw-body':'secret'})
        def fail(): raise error
        noop = lambda *a, **k: {}
        from packages.application.warehouse_functional import ensure_warehouse_functional_schema
        with closing(sqlite3.connect(self.block.db_path)) as conn, conn:
            conn.row_factory=sqlite3.Row;ensure_warehouse_functional_schema(conn)
        journal = WarehouseUpdateJournal(db_path=self.block.db_path, runtime_dir=self.root)
        entry = SimpleNamespace(runtime=SimpleNamespace(runtime_dir=self.root,db_path=self.block.db_path),
            warehouse_update_journal=journal,
            calculation_parameters_block=SimpleNamespace(prepare_functional_economics_backup=noop,
                process_pending_targeted_recalculations=noop,publish_current_functional_economics=noop),
            wb_supplies_block=SimpleNamespace(sync_functional_sources=noop,collect_all_due_transit_costs=noop,
                reconcile_functional_ff_state=noop),
            our_wb_cost_block=SimpleNamespace(materialize_wb_supply_cost_layers=noop),
            warehouse_functional_block=SimpleNamespace(build_sync_plan=lambda:{'plan_fingerprint':'fixture'},
                apply_plan=noop,record_failed_sync=noop), inventory_planning=SimpleNamespace(current=noop),
            wb_finance_weekly_block=SimpleNamespace(recalculate_stale_cost_weeks=fail))
        with ExitStack() as stack:
            for module, name in ((overhead,'drain'),(documents,'drain'),(accounting,'refresh'),
                                 (accounting,'current_publication_receipt')):
                stack.enter_context(patch.object(module,name,side_effect=noop))
            stack.enter_context(heavy_admitted(self.root,operation='finance-diagnostic-fixture'))
            owner=stack.enter_context(warehouse_functional_job_lock(self.root))
            with self.assertRaises(FinanceStaleCostHandoffError):
                RegistryUploadHttpEntrypoint._handle_owned_warehouse_manual_sync_request(entry,
                    owner_token=owner['owner_token'])
        # A new reader sees the same SQLite receipt, after all owners have exited.
        restarted = WarehouseUpdateJournal(db_path=self.block.db_path, runtime_dir=self.root)
        durable = restarted.public_status()
        phase = next(p for p in durable['phases'] if p['phase_key']=='dependent_replay_economics')
        self.assertEqual(phase['status'],'failed')
        self.assertEqual(phase['details']['finance_failure'],error.diagnostic())
        self.assertEqual(phase['last_error'],'finance_handoff_exhausted')
        self.assertNotIn('secret',json.dumps(durable))


class SharedBookHandoffTests(unittest.TestCase):
    setUp = HandoffTests.setUp
    commit = HandoffTests.commit
    run_apply = HandoffTests.run_apply
    def make_book(self):
        from packages.application import fbs_accounting_runtime as accounting
        from packages.application.ready_publication import ensure_publication_schema
        from apps.fbs_snapshot_cost_smoke import capture
        from apps.shared_sku_cost_smoke import wb
        from apps.fbs_inventory_presentation_smoke import retained
        from packages.application.fbs_snapshot_cost import fingerprint
        image, source = capture(), wb()
        image['quantity_snapshot']['rows'][0]['nm_id']=101
        image['quantity_snapshot']['digest']=fingerprint(image['quantity_snapshot']['rows'])
        image['baseline_costs']['rows'][0]['nm_id']=101
        source['rows'][0]['nm_id']=101
        with closing(sqlite3.connect(self.block.db_path)) as conn, conn: ensure_publication_schema(conn)
        with patch.object(accounting, 'capture_current', return_value=image), \
             patch.object(accounting, 'capture_wb_component', return_value=source), \
             patch.object(accounting, 'capture_retained_stages', return_value=retained(source)):
            book, expected = accounting.prepare(self.root, now=datetime(2026,9,7,14,tzinfo=timezone.utc), opening=True)
        version = accounting.save(self.root, book, expected=expected, operation_id='activate-fixture')
        return book, version

    def september_target(self):
        self.block.ingest_week(date(2026,9,7),date(2026,9,13),[_row(7,'2026-09-07')])
        self.commit("UPDATE wb_finance_weekly_aggregates SET classifier_version='stale' WHERE week_start='2026-09-07'")
        plan = self.block.plan_stale_cost_weeks(date_from=date(2026,9,7),date_to=date(2026,9,13))
        self.assertEqual(plan['stale_week_count'],1)
        return plan

    def apply_september(self, inject):
        plan = self.september_target(); original = self.block._connect
        with patch.object(self.block, '_connect', side_effect=lambda: (inject(), original())[1]), \
             patch.object(self.block, '_replace_finance_target_images', wraps=self.block._replace_finance_target_images) as dml:
            with self.assertRaises(FinanceStaleCostHandoffError) as caught:
                self.block.apply_stale_cost_weeks(expected_fingerprint=plan['fingerprint'], date_from=date(2026,9,7), date_to=date(2026,9,13))
        self.assertEqual(caught.exception.reason,'finance_shared_cost_changed')
        self.assertEqual(dml.call_count,0)

    def test_real_shared_book_creation_and_missing_to_resolved_zero_dml(self):
        self.apply_september(self.make_book)  # None -> actual active book.
        from packages.application import fbs_accounting_runtime as accounting
        book, version = accounting.load(self.root)
        failed = deepcopy(book); failed['publication_error']='fixture source unavailable'
        version = accounting.save(self.root, failed, expected=version, operation_id='hide-open-period')
        book['prepared_at']='2026-09-07T14:02:00Z'
        self.apply_september(lambda: accounting.save(self.root, book, expected=version, operation_id='restore-open-period'))

    def test_real_shared_cost_operand_drift_zero_dml(self):
        from packages.application import fbs_accounting_runtime as accounting
        from packages.application.shared_sku_cost import build_shared_cost_day
        from packages.application.fbs_snapshot_cost import fingerprint
        from apps.shared_sku_cost_smoke import wb
        book, version = self.make_book()
        changed = deepcopy(book)
        source=wb(capital='100500');source['rows'][0]['nm_id']=101
        source['source_digest']=fingerprint(source)
        changed['shared_days']['2026-09-07']=build_shared_cost_day(book['state'],source,'2026-09-07')
        self.apply_september(lambda:accounting.save(self.root,changed,expected=version,operation_id='changed-price'))

    def test_scoped_shared_proof_keeps_policy_and_exact_day_missing_refs(self):
        from packages.application.shared_sku_cost import SharedSkuCostSnapshot
        from packages.application.fbs_snapshot_cost import fingerprint
        book, _version = self.make_book()
        first=book['shared_days']['2026-09-07']
        snapshot=SharedSkuCostSnapshot([first],effective_date='2026-09-07')
        scope=[('2026-09-07','101')]
        base=self.block._finance_shared_handoff_digest(snapshot,scope)
        later=deepcopy(first);later['business_date']='2026-09-08'
        # Recompute material identity for a genuine separate exact-date period.
        later['version_id']=fingerprint({k:v for k,v in later.items() if k!='version_id'})
        expanded=SharedSkuCostSnapshot([first,later],effective_date='2026-09-07')
        self.assertNotEqual(snapshot.version_id,expanded.version_id)
        self.assertEqual(base,self.block._finance_shared_handoff_digest(expanded,scope))
        self.assertNotEqual(self.block._finance_shared_handoff_digest(snapshot,[('2026-09-08','101')]),
            self.block._finance_shared_handoff_digest(expanded,[('2026-09-08','101')]))
        self.assertNotEqual(base,self.block._finance_shared_handoff_digest(None,scope))
        for key,value in (('effective_date','2026-09-08'),('cost_method_version','future-policy'),('candidate_only',False)):
            with self.subTest(policy=key),patch.object(SharedSkuCostSnapshot,'metadata',return_value={**snapshot.metadata(),key:value}):
                self.assertNotEqual(base,self.block._finance_shared_handoff_digest(snapshot,scope))

    def test_real_shared_day_disappearing_is_not_hidden_by_main_retry(self):
        from packages.application import fbs_accounting_runtime as accounting
        book, version = self.make_book()
        failed = deepcopy(book); failed['publication_error']='fixture source unavailable'
        self.apply_september(lambda: accounting.save(self.root, failed, expected=version, operation_id='hide-open-period'))

    def test_common_writer_owner_fences_real_book_save_through_finance_commit(self):
        from packages.application import fbs_accounting_runtime as accounting
        from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
        self.commit('PRAGMA journal_mode=WAL')  # Match the deployed concurrent-reader store.
        book, version = self.make_book()
        changed = deepcopy(book); changed['prepared_at']='2026-09-07T14:01:00Z'
        started, committed, owner_checked = threading.Event(), threading.Event(), threading.Event()
        errors=[]; threads=[]
        original = self.block._replace_finance_target_images
        def publisher():
            started.set()
            try:
                accounting.save(self.root, changed, expected=version, operation_id='concurrent-book')
                committed.set()
            except BaseException as exc: errors.append(exc)
        def replace(conn, **kwargs):
            # The real common owner is held and reentrant in this thread.
            with warehouse_functional_write_lock(self.root, blocking=False) as evidence:
                self.assertEqual(evidence['reentrant'],1.0)
            thread=threading.Thread(target=publisher); threads.append(thread);thread.start()
            self.assertTrue(started.wait(2));self.assertFalse(committed.is_set())
            owner_checked.set()
            return original(conn, **kwargs)
        with patch.object(self.block, '_replace_finance_target_images', side_effect=replace):
            result = self.run_apply()
        for thread in threads: thread.join(3)
        self.assertEqual(errors,[]);self.assertTrue(owner_checked.is_set());self.assertTrue(committed.is_set())
        self.assertEqual(result['recalculated_week_count'],1);self.assertEqual(self.calls['dml'],1)


if __name__ == '__main__': unittest.main()
