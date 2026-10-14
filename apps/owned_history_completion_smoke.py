"""Native readback fences plus Linux completion, without source replay."""
from contextlib import ExitStack, closing
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


class DerivedReadbackFenceTests(unittest.TestCase):
    """Real native capture/compiler/object reads; kernel authentication is separate."""

    def fixture(self, journal_mode='DELETE'):
        from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import BUNDLE_FIXTURE, _build_plan
        from apps.ready_publication_fixture import save_ready_fixture
        from datetime import timedelta
        from apps import web_vitrina_owned_history_worker as child
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        from packages.application.ready_publication import ensure_publication_schema
        from packages.application.search_cluster_cleaner_store import install_schema
        from packages.application.owned_history_worker_capability import file_digest
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        runtime = RegistryUploadDbBackedRuntime(directory / 'runtime')
        accepted = runtime.ingest_bundle(json.loads(BUNDLE_FIXTURE.read_text()), activated_at='2026-04-21T15:00:00Z')
        self.assertEqual(accepted.status, 'accepted')
        RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir, runtime=runtime)
        current = runtime.load_current_state()
        enabled = [item for item in current.config_v2 if item.enabled]
        for offset in range(14):
            day = (NOW.date() - timedelta(days=13-offset)).isoformat()
            save_ready_fixture(runtime, current_state=current, refreshed_at=day + 'T15:05:00Z',
                plan=_build_plan(as_of_date=day, first_nm_id=enabled[0].nm_id,
                    second_nm_id=enabled[1].nm_id, first_group=enabled[0].group))
        keeper = self.enterContext(closing(sqlite3.connect(runtime.db_path)))
        self.assertEqual(keeper.execute('PRAGMA journal_mode=' + journal_mode).fetchone()[0], journal_mode.lower())
        ensure_publication_schema(keeper)
        install_schema(keeper)
        keeper.execute("""INSERT INTO cleaner_runs(run_id,account,kind,trigger,state,phase,
            created_at,captured_versions,targets) VALUES('fixture','fixture','scan','manual',
            'running','scan',?,'{}','[]')""", (NOW.isoformat(),))
        # An accepted publication is also consumed by the canonical adapter but
        # is intentionally not covered by the input-revision alarm table.
        key = keeper.execute('SELECT bundle_version,as_of_date FROM sheet_vitrina_v1_ready_snapshots ORDER BY as_of_date DESC LIMIT 1').fetchone()
        keeper.execute("""INSERT INTO sheet_vitrina_v1_ready_publications
            (operation_id,attempt_id,kind,bundle_version,as_of_date,expected_digest,
             inputs_json,book_required,ready_required,state,book_version,after_digest,created_at,finished_at)
            VALUES('fixture-publication','fixture','fixture',?,?,'expected','{}',0,1,
                'complete',NULL,'fixture-after','created','finished')""", key)
        keeper.commit()
        keeper.execute('SELECT 1 FROM sqlite_master').fetchone()  # Keep WAL family open.
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'candidate'
        root.mkdir(mode=0o700)
        contract = json.loads((ROOT / 'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json').read_text())
        contract['candidate_root'] = str(root)
        contract_path = root / 'contract.json'
        contract_path.write_text(json.dumps(contract))
        cap = dict(runtime_dir=str(runtime.runtime_dir), candidate_root=str(root),
            runtime_contract=str(contract_path), runtime_contract_digest=file_digest(contract_path),
            formula_epoch=contract['formula_epoch'], inner_seconds=60, max_recomputes=31,
            now=NOW.isoformat(), source=supervisor.source_binding(runtime.runtime_dir),
            anchor=None, mode='capture', invocation='fixture-capture')
        # execute() itself and its real source/store checks run unchanged. Only
        # inherited Linux FDs are outside this portable test's authority scope.
        self.enterContext(patch.object(child, '_bound'))
        cap['anchor'] = child.execute(cap)['anchor']
        cap.update(mode='portion', invocation='fixture-portion')
        result = child.execute(cap)
        self.assertEqual(result['status'], 'published')
        intent = json.loads((root / 'proofs' / 'owned-portion-fixture-portion.json').read_text())
        cap.update(mode='verify', invocation='fixture-verify',
            target={key: intent[key] for key in ('fresh_catalog', 'merged_catalog', 'target')})
        return runtime, root, cap

    def cleaner_commit(self, runtime):
        # The production journal/phase writes, with real native schema, finish
        # independently of the readback. DELETE must not retain an outer SH.
        with closing(sqlite3.connect(runtime.db_path, timeout=.2)) as writer:
            writer.execute("""INSERT INTO cleaner_events(event_id,account,run_id,kind,created_at,facts)
                VALUES('fixture-event','fixture','fixture','self_service_finished',?,'{}')""", (NOW.isoformat(),))
            writer.execute("UPDATE cleaner_runs SET state='finished',phase='finished' WHERE run_id='fixture'")
            writer.commit()

    def during_first_object(self, mutation):
        from packages.application import owned_history_native_ack as ack
        native = ack._object
        calls = []
        def read(*args):
            result = native(*args)
            if not calls:
                mutation()
            calls.append(args[1])
            return result
        return patch.object(ack, '_object', side_effect=read), calls

    def test_cleaner_commit_during_native_object_readback_delete_and_wal(self):
        from apps import web_vitrina_owned_history_worker as child
        from packages.application.owned_history_native_ack import source_stamp
        for mode in ('DELETE', 'WAL'):
            with self.subTest(journal_mode=mode):
                runtime, root, cap = self.fixture(mode)
                before = source_stamp(runtime.runtime_dir, runtime.db_path)
                pointer = (root / 'history' / 'CURRENT.json').read_bytes()
                hook, calls = self.during_first_object(lambda: self.cleaner_commit(runtime))
                with hook:
                    observed = child.execute(cap)
                self.assertEqual(observed['status'], 'verified')
                self.assertTrue(observed['terminal'])
                self.assertEqual(len(calls), 14)
                self.assertEqual(len(observed['completed']), 14)
                self.assertEqual((root / 'history' / 'CURRENT.json').read_bytes(), pointer)
                self.assertNotEqual(observed['source_stamp'], before)
                self.assertEqual(observed['source_stamp'], source_stamp(runtime.runtime_dir, runtime.db_path))
                with closing(sqlite3.connect(runtime.db_path)) as conn:
                    self.assertEqual(conn.execute('SELECT count(*) FROM cleaner_events').fetchone()[0], 1)
                    self.assertEqual(conn.execute("SELECT phase FROM cleaner_runs WHERE run_id='fixture'").fetchone()[0], 'finished')

    def test_actual_ready_dependency_and_revision_aba_refuse(self):
        from apps import web_vitrina_owned_history_worker as child
        for restore in (False, True):
            with self.subTest(restore_original_value=restore):
                runtime, root, cap = self.fixture()
                pointer = (root / 'history' / 'CURRENT.json').read_bytes()
                def change():
                    with closing(sqlite3.connect(runtime.db_path, timeout=.2)) as conn:
                        old = conn.execute('SELECT bundle_version,as_of_date,refreshed_at FROM sheet_vitrina_v1_ready_snapshots ORDER BY as_of_date LIMIT 1').fetchone()
                        rows = conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date').fetchall()
                        revision = conn.execute('SELECT revision FROM sheet_vitrina_v1_ready_revisions WHERE bundle_version=? AND as_of_date=?', old[:2]).fetchone()[0]
                        conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='2099-01-01T00:00:00Z' WHERE bundle_version=? AND as_of_date=?", old[:2])
                        conn.commit()
                        if restore:
                            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=? WHERE bundle_version=? AND as_of_date=?', (old[2], *old[:2]))
                            conn.commit()
                            self.assertEqual(conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date').fetchall(), rows)
                            self.assertGreater(conn.execute('SELECT revision FROM sheet_vitrina_v1_ready_revisions WHERE bundle_version=? AND as_of_date=?', old[:2]).fetchone()[0], revision)
                hook, calls = self.during_first_object(change)
                with hook, self.assertRaisesRegex(HistoryDelegationError, 'history_worker_anchor_changed'):
                    child.execute(cap)
                self.assertEqual(len(calls), 14)
                self.assertEqual((root / 'history' / 'CURRENT.json').read_bytes(), pointer)

    def test_unalarmed_consumed_publication_change_refuses(self):
        from apps import web_vitrina_owned_history_worker as child
        runtime, root, cap = self.fixture()
        def change():
            with closing(sqlite3.connect(runtime.db_path, timeout=.2)) as conn:
                alarms = conn.execute('SELECT * FROM sheet_vitrina_v1_ready_input_revisions ORDER BY source_table').fetchall()
                conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET after_digest='changed' WHERE operation_id='fixture-publication'")
                conn.commit()
                self.assertEqual(conn.execute('SELECT * FROM sheet_vitrina_v1_ready_input_revisions ORDER BY source_table').fetchall(), alarms)
        hook, calls = self.during_first_object(change)
        with hook, self.assertRaisesRegex(HistoryDelegationError, 'history_worker_anchor_changed'):
            child.execute(cap)
        self.assertEqual(len(calls), 14)

    def test_cleaner_in_final_capture_and_parent_handoff_still_refuses(self):
        from apps import web_vitrina_owned_history_worker as child
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
        runtime, root, cap = self.fixture()
        native = LiveNativeAdapter.capture
        calls = []
        def capture(adapter):
            result = native(adapter)
            calls.append(result)
            if len(calls) == 2:
                self.cleaner_commit(runtime)
            return result
        with patch.object(LiveNativeAdapter, 'capture', new=capture), \
                self.assertRaisesRegex(HistoryDelegationError, 'history_source_changed_during_readback'):
            child.execute(cap)
        self.assertEqual(calls, [cap['anchor']['vector']] * 2)
        # A genuine verified result cannot be consumed after even an unrelated
        # subsequent write. The parent keeps its existing no-database guard.
        observed = child.execute(cap)
        with closing(sqlite3.connect(runtime.db_path)) as conn:
            conn.execute("UPDATE cleaner_runs SET summary='{} ' WHERE run_id='fixture'")
            conn.commit()
        from types import SimpleNamespace
        owner = SimpleNamespace(runtime=runtime)
        with self.assertRaisesRegex(HistoryDelegationError, 'history_source_changed_after_readback'):
            supervisor._OwnedHistoryWorker._finish_completion(owner, observed, None, portions=1)

    def test_current_pointer_change_during_object_readback_refuses(self):
        from apps import web_vitrina_owned_history_worker as child
        runtime, root, cap = self.fixture()
        path = root / 'history' / 'CURRENT.json'
        def change():
            value = json.loads(path.read_text())
            value['previous'] = 'f' * 64
            path.write_text(json.dumps(value))
        hook, calls = self.during_first_object(change)
        with hook, self.assertRaisesRegex(HistoryDelegationError, 'history_readback_changed_during_verify'):
            child.execute(cap)
        self.assertEqual(len(calls), 14)

    def test_other_physical_sources_and_operational_replacement_stay_strict(self):
        from packages.application.owned_history_native_ack import source_stamp, stamp_after_derived_read
        from packages.application.storage_registry import MANIFEST_FILENAME
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'operational.sqlite3'
            db.write_bytes(b'fixture')
            for name in ('fbs-snapshot-accounting.sqlite3', 'fbs-snapshot-accounting.sqlite3-wal',
                         MANIFEST_FILENAME, '.auto-updates-policy.json', '.web-vitrina-fbs-lifecycle-last-good.json'):
                with self.subTest(source=name):
                    path = root / name
                    before = source_stamp(root, db)
                    path.write_bytes(b'new')
                    with self.assertRaisesRegex(HistoryDelegationError, 'history_source_changed_during_readback'):
                        stamp_after_derived_read(root, db, before)
                    before = source_stamp(root, db)
                    path.write_bytes(b'ABA')
                    path.write_bytes(b'new')
                    with self.assertRaisesRegex(HistoryDelegationError, 'history_source_changed_during_readback'):
                        stamp_after_derived_read(root, db, before)
            before = source_stamp(root, db)
            replacement = root / 'replacement'
            replacement.write_bytes(db.read_bytes())
            replacement.replace(db)
            with self.assertRaisesRegex(HistoryDelegationError, 'history_source_changed_during_readback'):
                stamp_after_derived_read(root, db, before)


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
    def test_separate_operator_authorities_and_exact_closed_dates_before_spawn(self):
        from types import SimpleNamespace
        from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog
        with tempfile.TemporaryDirectory() as directory:
            runtime = simple_runtime(directory)
            before = logical_source_digest(runtime.db_path)
            with ownership(runtime, Path(directory)/'candidate') as worker, patch.object(supervisor, '_POPEN') as spawn:
                with self.assertRaisesRegex(HistoryDelegationError, 'multiple_source_authorities'):
                    worker.complete(NOW, historical_receipt=object(), policy_receipt=object())
                closed = ClosedBacklog(SimpleNamespace(block=SimpleNamespace(runtime=runtime)))
                with patch.object(closed, 'publication_dates', return_value=('2026-04-01',)):
                    with self.assertRaisesRegex(HistoryDelegationError, 'closed_receipt_context_changed'):
                        worker.complete(NOW, backfill_dates=('2026-04-01', '2026-04-02'), closed_receipt=closed)
                self.assertFalse(spawn.called)
                self.assertIsNone(worker._process)
                self.assertFalse(worker._completion_used)
            self.assertEqual(logical_source_digest(runtime.db_path), before)

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
