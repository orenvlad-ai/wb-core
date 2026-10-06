"""Linux synthetic completion, exact dated progress and no source replay."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.owned_history_worker_smoke import ownership, NOW, logical_source_digest, simple_runtime
from packages.application import owned_history_worker as supervisor
from packages.application.owned_history_worker_capability import HistoryDelegationError
from packages.application.web_vitrina_history_store import HistoryStore
from packages.application.business_data_heavy_admission import heavy_admission_status, current_heavy_owner


@unittest.skipUnless(sys.platform == 'linux', 'actual inherited kernel authority required')
class CompletionTests(unittest.TestCase):
    def fixture(self):
        from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
        from packages.application.ready_publication import ensure_publication_schema
        fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=14, now=NOW)
        self.enterContext(fixture)
        keeper = self.enterContext(sqlite3.connect(fixture.entrypoint.runtime.db_path))
        keeper.execute('PRAGMA journal_mode=WAL'); ensure_publication_schema(keeper); keeper.commit()
        keeper.execute('SELECT 1 FROM sqlite_master').fetchone()
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'candidate'
        return fixture.entrypoint.runtime, root, keeper

    def test_real_many_portions_and_exact_current_no_source_writes(self):
        runtime, root, keeper = self.fixture()
        before = logical_source_digest(runtime.db_path)
        with ownership(runtime, root, seconds=60) as worker:
            worker.config.max_recomputes = 1
            result = worker.complete(NOW, total_seconds=180)
            self.assertEqual(result['portions'], 14)
            self.assertEqual(result['completed'], 14)
            self.assertIsNone(worker._process)
            self.assertFalse(heavy_admission_status(runtime.runtime_dir)['idle'])
            store = HistoryStore(root / 'history')
            self.assertEqual(result['edition_id'], store._current()['current'])
            self.assertFalse((store.root / 'PENDING.json').exists())
            self.assertEqual(len(store.edition()['days']), 14)
            with self.assertRaisesRegex(HistoryDelegationError, 'already_consumed'):
                worker.complete(NOW)
        self.assertEqual(before, logical_source_digest(runtime.db_path))
        with ownership(runtime, root, seconds=60) as worker:
            same = worker.complete(NOW, total_seconds=120)
            self.assertEqual(same['portions'], 0)
            self.assertEqual(same['edition_id'], result['edition_id'])

    def test_standalone_fixed_child_preserves_range_one_portion_and_owner(self):
        from types import SimpleNamespace
        from packages.application.business_data_heavy_admission import heavy_admitted
        from packages.application.web_vitrina_snapshot_admission import ApiJobMarkers
        runtime, root, keeper = self.fixture()
        root.mkdir(mode=0o700)
        for name in ('.web-vitrina-finished-builder.lock', '.wb-finance-daily-worker.lock'):
            (runtime.runtime_dir/name).touch(mode=0o600)
        ApiJobMarkers(runtime.runtime_dir)  # Actual live API owner, no job to exempt.
        contract = json.loads((Path(__file__).resolve().parents[1]/'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json').read_text())
        contract['candidate_root'] = str(root)
        path = root/'contract.json'; path.write_text(json.dumps(contract))
        config = SimpleNamespace(candidate_root=root,runtime_contract=path,formula_epoch=contract['formula_epoch'],budget_seconds=60,max_recomputes=31)
        before = logical_source_digest(runtime.db_path)
        with heavy_admitted(runtime.runtime_dir,operation='history'), \
             patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'), \
             patch('apps.web_vitrina_finished_snapshot_build.systemd_admission',return_value='idle'):
            with supervisor.standalone_history_worker(runtime=runtime,config=config) as worker:
                result = worker.complete(NOW, source_range=('2026-03-01','2026-04-20'),total_seconds=180,max_portions=1)
                self.assertEqual(result['portions'],1)
                self.assertEqual(len(worker._anchor['source_dates']),51)
                self.assertEqual(len(worker._anchor['scope_dates']),14)
                self.assertIsNone(worker._process)
                self.assertFalse(heavy_admission_status(runtime.runtime_dir)['idle'])
        self.assertEqual(logical_source_digest(runtime.db_path), before)
        self.assertTrue(heavy_admission_status(runtime.runtime_dir)['idle'])

    def test_unknown_after_current_switch_verifies_without_second_compile(self):
        runtime, root, keeper = self.fixture()
        with ownership(runtime, root, seconds=60) as worker:
            receive = supervisor.receive_message
            invoke = worker._invoke
            calls = []
            def wrapped(mode, now, anchor):
                calls.append(mode)
                if mode != 'portion': return invoke(mode, now, anchor)
                def lose(channel):
                    receive(channel)
                    raise HistoryDelegationError('fixture_lost_after_commit')
                with patch.object(supervisor, 'receive_message', side_effect=lose):
                    return invoke(mode, now, anchor)
            with patch.object(worker, '_invoke', side_effect=wrapped):
                result = worker.complete(NOW, total_seconds=120)
            self.assertEqual(calls, ['capture', 'verify', 'portion', 'verify'])
            self.assertEqual(result['portions'], 1)
            self.assertEqual(result['edition_id'], HistoryStore(root / 'history')._current()['current'])

    def test_no_dated_progress_stops_even_if_cache_grows(self):
        runtime, root, keeper = self.fixture()
        with ownership(runtime, root, seconds=60) as worker:
            invoke = worker._invoke; calls = []
            def wrapped(mode, now, anchor):
                calls.append(mode)
                if mode == 'portion':
                    (root / 'proofs' / 'unrelated-cache').write_bytes(b'x' * 4096)
                    return {'status':'outcome_unknown','readback':{}}
                return invoke(mode, now, anchor)
            with patch.object(worker, '_invoke', side_effect=wrapped):
                with self.assertRaisesRegex(HistoryDelegationError, 'no_dated_progress'):
                    worker.complete(NOW, total_seconds=120)
            self.assertEqual(calls, ['capture', 'verify', 'portion', 'verify'])
            self.assertFalse((root / 'history' / 'CURRENT.json').exists())

    def test_portion_count_bound_preserves_proven_pending(self):
        runtime, root, keeper = self.fixture()
        with ownership(runtime, root, seconds=60) as worker:
            worker.config.max_recomputes = 1
            with self.assertRaisesRegex(HistoryDelegationError, 'portion_limit'):
                worker.complete(NOW, total_seconds=120, max_portions=2)
            pending = json.loads((root / 'history' / 'PENDING.json').read_text())
            self.assertEqual(len(pending['day_proofs']), 2)
            self.assertFalse((root / 'history' / 'CURRENT.json').exists())
            self.assertIsNone(worker._process)
        original_refs = dict(pending['refs'])
        with ownership(runtime, root, seconds=60) as resumed:
            recovered = resumed.complete(NOW,total_seconds=120)
            self.assertEqual(recovered['portions'],1)
            edition = HistoryStore(root/'history').edition()
            self.assertTrue(all(edition['days'][day]==ref for day,ref in original_refs.items()))

    def test_new_supervisor_after_source_drift_reuses_only_proven_days(self):
        runtime, root, keeper = self.fixture()
        with ownership(runtime,root,seconds=60) as worker:
            worker.config.max_recomputes=1
            with self.assertRaisesRegex(HistoryDelegationError,'portion_limit'):
                worker.complete(NOW,total_seconds=120,max_portions=2)
        pending_path=root/'history'/'PENDING.json'
        pending=json.loads(pending_path.read_text())
        reused=dict(pending['refs'])
        # Independent accepted source revision between separately admitted jobs.
        keeper.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='2026-04-20T18:00:00Z' WHERE as_of_date='2026-04-09'")
        keeper.commit()
        before=logical_source_digest(runtime.db_path)
        with ownership(runtime,root,seconds=60) as recovered:
            result=recovered.complete(NOW,total_seconds=120)
            self.assertEqual(result['portions'],1)
            self.assertNotEqual(recovered._anchor['vector'],pending['vector'])
        edition=HistoryStore(root/'history').edition()
        self.assertTrue(all(edition['days'][day]==ref for day,ref in reused.items()))
        self.assertEqual(before,logical_source_digest(runtime.db_path))
        # Corrupt matching old progress is refused before new pending/intent
        # mutation; source drift is not permission to discard bad evidence.
        keeper.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='2026-04-20T19:00:00Z'")
        keeper.commit()
        with ownership(runtime,root,seconds=60) as worker:
            worker.config.max_recomputes=1
            with self.assertRaisesRegex(HistoryDelegationError,'portion_limit'):
                worker.complete(NOW,total_seconds=120,max_portions=2)
        corrupted=json.loads(pending_path.read_text())
        corrupted['refs'][sorted(worker._anchor['scope_dates'])[0]]='0'*64
        pending_path.write_text(json.dumps(corrupted))
        keeper.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='2026-04-20T20:00:00Z' WHERE as_of_date='2026-04-09'")
        keeper.commit()
        original=pending_path.read_bytes()
        current=(root/'history'/'CURRENT.json').read_bytes()
        with ownership(runtime,root,seconds=60) as refused:
            with self.assertRaisesRegex(HistoryDelegationError,'portion_failed'):
                refused.complete(NOW,total_seconds=120)
        self.assertEqual(pending_path.read_bytes(),original)
        self.assertEqual((root/'history'/'CURRENT.json').read_bytes(),current)

    def test_corrupt_durable_object_is_not_progress(self):
        runtime, root, keeper = self.fixture()
        with ownership(runtime, root, seconds=60) as worker:
            worker.config.max_recomputes = 1
            invoke = worker._invoke
            def wrapped(mode, now, anchor):
                result = invoke(mode, now, anchor)
                if mode == 'portion':
                    pending = json.loads((root / 'history' / 'PENDING.json').read_text())
                    path = root / 'history' / 'objects' / (next(iter(pending['refs'].values())) + '.sqlite3')
                    with sqlite3.connect(path) as conn:
                        conn.execute("UPDATE metadata SET payload='{}'"); conn.commit()
                return result
            with patch.object(worker, '_invoke', side_effect=wrapped):
                with self.assertRaisesRegex(HistoryDelegationError, 'readback_unproven'):
                    worker.complete(NOW, total_seconds=120)
            self.assertIsNone(worker._process)
            self.assertFalse((root / 'history' / 'CURRENT.json').exists())


@unittest.skipUnless(sys.platform == 'linux', 'actual inherited kernel authority required')
class ClosedCompletionTests(unittest.TestCase):
    def test_actual_child_native_ack_two_old_dates_and_retained_main(self):
        from apps import sheet_vitrina_v1_closed_backlog_smoke as fixture
        from packages.application.web_vitrina_snapshot_admission import ApiJobMarkers
        from packages.application.owned_history_native_ack import consume_closed_history_ack
        from types import SimpleNamespace
        with patch.object(fixture, 'DAYS', ['2026-04-01', '2026-04-02']):
            case = fixture.BacklogTests(); case.setUp()
            try:
                case.backlog.collect(fixture.CYCLE)
                main = case.main()
                for day in fixture.DAYS:
                    case.publish(case.backlog.compose(main, day)); case.backlog.record_ready(day)
                case.publish(main)
                adapter, store, config = case.native()
                root = config.candidate_root
                for name in ('.web-vitrina-finished-builder.lock', '.wb-finance-daily-worker.lock'):
                    (case.runtime.runtime_dir / name).touch(mode=0o600)
                markers = ApiJobMarkers(case.runtime.runtime_dir)
                marker = markers.start('closed-history-fixture', 'cycle')
                cycle = {**markers.owner, 'job_id':'closed-history-fixture', 'operation':'cycle'}
                counters = {key:list(v.request_dates) for key,v in case.counters.items()}
                from packages.application.web_vitrina_history_live_adapter import update_live_history
                # A real immutable intervening archive day must remain byteexact.
                initial = update_live_history(adapter=adapter, runtime=case.runtime, store=store,
                    rolling14=True, backfill_dates=['2026-04-04'], group_blocks=True,
                    metric_start_dates=json.loads(config.runtime_contract.read_text()).get('metric_start_dates', {}))
                self.assertEqual(initial['status'], 'published')
                gap_ref = store.edition()['days']['2026-04-04']
                gap_bytes = (store.root / 'objects' / (gap_ref+'.sqlite3')).read_bytes()
                before = logical_source_digest(case.runtime.db_path)
                config = SimpleNamespace(**vars(config)); config.max_recomputes = 1; config.budget_seconds = 60
                # Actual terminal readback followed by an independent short
                # source commit must not authorize acknowledgement of latest.
                receipt_before = case.backlog.path.read_bytes()
                acknowledge = case.backlog._acknowledge_verified_native
                def commit_between(proof, *, backfill_dates):
                    from dataclasses import replace
                    with self.assertRaisesRegex(HistoryDelegationError, 'owner_or_target_changed'):
                        consume_closed_history_ack(replace(proof), case.runtime.runtime_dir,
                            proof.receipt_digest, backfill_dates)
                    with sqlite3.connect(case.runtime.db_path) as conn:
                        conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='2026-04-20T10:00:00Z' WHERE as_of_date='2026-04-19'")
                        conn.commit()
                    return acknowledge(proof, backfill_dates=backfill_dates)
                with patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'), \
                     patch('apps.web_vitrina_finished_snapshot_build.systemd_admission', return_value='idle'), \
                     patch.object(case.backlog, '_acknowledge_verified_native', side_effect=commit_between):
                    with supervisor.owned_history_worker(runtime=case.runtime, config=config, cycle_owner=cycle) as worker:
                        with self.assertRaisesRegex(HistoryDelegationError, 'owner_or_target_changed'):
                            worker.complete(fixture.NOW, backfill_dates=tuple(fixture.DAYS),
                                closed_receipt=case.backlog, total_seconds=180)
                self.assertEqual(case.backlog.path.read_bytes(), receipt_before)
                before = logical_source_digest(case.runtime.db_path)
                with patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'), \
                     patch('apps.web_vitrina_finished_snapshot_build.systemd_admission', return_value='idle'):
                    with supervisor.owned_history_worker(runtime=case.runtime, config=config, cycle_owner=cycle) as worker:
                        result = worker.complete(fixture.NOW, backfill_dates=tuple(fixture.DAYS),
                            closed_receipt=case.backlog, total_seconds=240)
                        self.assertEqual(result['completed'], 16)
                        self.assertGreaterEqual(result['portions'], 1)
                        self.assertLessEqual(result['portions'], 16)
                        self.assertTrue(result['closed_ack'])
                        self.assertIsNone(worker._closed_proof)
                        with self.assertRaisesRegex(HistoryDelegationError, 'not_supervised'):
                            consume_closed_history_ack({'terminal':True}, case.runtime.runtime_dir, '', tuple(fixture.DAYS))
                markers.finish(marker)
                self.assertEqual(counters, {key:list(v.request_dates) for key,v in case.counters.items()})
                self.assertEqual(before, logical_source_digest(case.runtime.db_path))
                self.assertEqual(case.runtime.load_sheet_vitrina_ready_snapshot().as_of_date, '2026-04-19')
                value = case.backlog.status()
                self.assertTrue(all(v['state']=='acknowledged' for v in value['dates'].values()))
                self.assertEqual(set(store.edition()['days']), set(worker._anchor['scope_dates']) | {'2026-04-04'})
                self.assertEqual(store.edition()['days']['2026-04-04'], gap_ref)
                self.assertEqual((store.root/'objects'/(gap_ref+'.sqlite3')).read_bytes(), gap_bytes)
            finally:
                case.doCleanups()


@unittest.skipUnless(sys.platform == 'linux', 'kernel FD supervisor required')
class CompletionLimitTests(unittest.TestCase):
    def test_total_deadline_and_invalid_limits_never_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=simple_runtime(directory)
            with ownership(runtime,Path(directory)/'candidate') as worker, patch.object(supervisor,'_POPEN') as spawn:
                for seconds,count in ((0,20),(3601,20),(60,21),(60,1.5)):
                    with self.assertRaisesRegex(HistoryDelegationError,'limits_invalid'):
                        worker.complete(NOW,total_seconds=seconds,max_portions=count)
                with self.assertRaisesRegex(HistoryDelegationError,'total_deadline'):
                    worker.complete(NOW,total_seconds=.01)
                self.assertFalse(spawn.called)
                self.assertIsNone(worker._process)
                self.assertFalse(heavy_admission_status(runtime.runtime_dir)['idle'])
            self.assertTrue(heavy_admission_status(runtime.runtime_dir)['idle'])


class StandaloneBoundaryTests(unittest.TestCase):
    def test_real_process_busy_precedes_constructors(self):
        import subprocess, time
        from types import SimpleNamespace
        from packages.application.business_data_heavy_admission import HeavyAdmissionBusy
        from apps import web_vitrina_history_candidate_build as builder
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'runtime'; runtime.mkdir()
            ready = Path(directory) / 'ready'
            code = ("from packages.application.business_data_heavy_admission import heavy_admitted; "
                    "from pathlib import Path; import sys; "
                    "ctx=heavy_admitted(sys.argv[1],operation='backup'); ctx.__enter__(); "
                    "Path(sys.argv[2]).write_text('held'); sys.stdin.read(); ctx.__exit__(None,None,None)")
            child = subprocess.Popen([sys.executable,'-c',code,str(runtime),str(ready)], stdin=subprocess.PIPE)
            try:
                until = time.monotonic()+10
                while not ready.exists() and time.monotonic()<until: time.sleep(.01)
                self.assertTrue(ready.exists())
                args=SimpleNamespace(runtime_dir=runtime, maintenance_window_id='')
                with patch.object(builder,'StoreRegistry',side_effect=AssertionError('constructor before admission')):
                    with self.assertRaises(HeavyAdmissionBusy): builder.run_admitted(args)
                self.assertEqual(set(runtime.iterdir()), {runtime/'.business-data-procedure-admission.lock',
                    runtime/'.business-data-heavy-admission.lock'})
            finally:
                child.communicate(timeout=10)
            self.assertTrue(heavy_admission_status(runtime)['idle'])

    def test_explicit_held_manual_exception_is_unchanged(self):
        from types import SimpleNamespace
        from apps import web_vitrina_history_candidate_build as builder
        from packages.application.business_data_procedure_admission import MaintenanceAdmissionBlocked
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory)
            args=SimpleNamespace(runtime_dir=runtime,manual=True,maintenance_window_id='exact-window')
            held={'active':True,'phase':'held','hold_confirmed':True,'window_kind':'maintenance_pause','window_id':'exact-window'}
            with patch.object(builder,'barrier_status',return_value=held):
                with builder.history_procedure_admission(args):
                    self.assertIsNone(current_heavy_owner(runtime))
                args.maintenance_window_id='wrong'
                with self.assertRaises(MaintenanceAdmissionBlocked):
                    with builder.history_procedure_admission(args): self.fail('wrong held window admitted')
            self.assertEqual(list(runtime.iterdir()), [])

if __name__ == '__main__': unittest.main()
