"""Native handoff regression group; also run by the selected worker smoke command."""
from contextlib import closing
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
from apps.owned_history_worker_smoke import ownership, NOW
from packages.application import owned_history_worker as supervisor
from packages.application.owned_history_worker_capability import HistoryDelegationError
from packages.application.web_vitrina_history_store import HistoryStore


def _completion_fixture(self):
    from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
    from packages.application.ready_publication import ensure_publication_schema
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=14, now=NOW)
    self.enterContext(fixture)
    keeper = self.enterContext(sqlite3.connect(fixture.entrypoint.runtime.db_path))
    keeper.execute('PRAGMA journal_mode=WAL'); ensure_publication_schema(keeper); keeper.commit()
    keeper.execute('SELECT 1 FROM sqlite_master').fetchone()
    root = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'candidate'
    return fixture.entrypoint.runtime, root, keeper


@unittest.skipUnless(sys.platform == "linux", "actual inherited kernel authority required")
class HandoffTests(unittest.TestCase):
    def fixture(self):
        return _completion_fixture(self)

    def _handoff_case(self, interference):
        """Real current/pending, CAS READY writes and authenticated Linux children."""
        from apps.ready_publication_fixture import save_ready_fixture
        from packages.application.search_cluster_cleaner_store import install_schema
        runtime, root, keeper = self.fixture()
        install_schema(keeper)
        keeper.execute("""INSERT INTO cleaner_runs(run_id,account,kind,trigger,state,phase,
            created_at,captured_versions,targets) VALUES('handoff','fixture','scan','manual',
            'running','scan',?,'{}','[]')""", (NOW.isoformat(),))
        keeper.commit()
        with ownership(runtime, root, seconds=60) as worker:
            worker.complete(NOW, total_seconds=180)
        store = HistoryStore(root / 'history')
        pointer = (store.root / 'CURRENT.json').read_bytes()
        current = runtime.load_current_state()
        days = sorted(store.edition()['days'])[:2]
        expected = {}
        def publish_changed_ready(day, increment):
            plan = deepcopy(runtime.load_sheet_vitrina_ready_snapshot(as_of_date=day))
            row = next(row for row in plan.sheets[0].rows if row[1] == 'TOTAL|total_orderSum')
            row[2] += increment
            save_ready_fixture(runtime, current_state=current, refreshed_at=NOW.isoformat().replace('+00:00','Z'), plan=plan)
            return row[2]
        for day in days:
            expected[day] = publish_changed_ready(day, 1)
        calls, verified = [], []
        with ownership(runtime, root, seconds=60) as worker:
            worker.config.max_recomputes = 1
            native = worker._invoke
            held_fds = worker.lock_fds
            deadline = []
            def invoke(mode, now, anchor):
                value = native(mode, now, anchor)
                calls.append(mode)
                self.assertEqual(worker.lock_fds, held_fds)
                if not deadline:
                    deadline.append(worker._completion_deadline)
                self.assertEqual(worker._completion_deadline, deadline[0])
                if mode == 'verify':
                    verified.append(value)
                    if len(verified) == 2 or (interference == 'repeated' and len(verified) == 3):
                        if interference == 'auxiliary':
                            path = runtime.runtime_dir / '.auto-updates-policy.json'
                            path.write_text('{}'); path.unlink()
                        else:
                            keeper.execute("UPDATE cleaner_runs SET phase=? WHERE run_id='handoff'", (str(len(verified)),))
                            keeper.commit()
                        if interference == 'ready_changed':
                            expected[days[1]] = publish_changed_ready(days[1], 123)
                        if interference == 'deadline':
                            worker._completion_deadline = supervisor.time.monotonic() + 1
                return value
            with patch.object(worker, '_invoke', side_effect=invoke):
                if interference == 'once':
                    result = worker.complete(NOW, total_seconds=180)
                    self.assertEqual(result['completed'], 14)
                    self.assertEqual(result['portions'], 2)
                    self.assertEqual(calls, ['capture','verify','portion','verify','verify','portion','verify'])
                    edition = store.edition()
                    for day in days:
                        with closing(store._open_day(store.root/'objects'/(edition['days'][day]+'.sqlite3'))) as conn:
                            raw = conn.execute("SELECT payload FROM cells WHERE row_id='TOTAL|total_orderSum'").fetchone()[0]
                        import zlib
                        self.assertEqual(json.loads(zlib.decompress(raw))[0], expected[day])
                    self.assertNotEqual((store.root/'CURRENT.json').read_bytes(), pointer)
                else:
                    reason = 'history_completion_readback_unproven' if interference == 'ready_changed' else 'history_source_changed_after_readback'
                    with self.assertRaisesRegex(HistoryDelegationError, reason) as caught:
                        worker.complete(NOW, total_seconds=180)
                    self.assertEqual((store.root/'CURRENT.json').read_bytes(), pointer)
                    self.assertEqual(calls.count('portion'), 1)
                    self.assertEqual(len(verified), 3 if interference in {'repeated','ready_changed'} else 2)
                    self.assertFalse(worker._ack_started)
                    if interference == 'ready_changed':
                        self.assertEqual(caught.exception.diagnostic()['reason_code'], 'history_worker_anchor_changed')
                        plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=days[1])
                        row = next(row for row in plan.sheets[0].rows if row[1]=='TOTAL|total_orderSum')
                        self.assertEqual(row[2], expected[days[1]])
                self.assertEqual(worker._handoff_revalidation_used,
                                 interference in {'once','repeated','ready_changed'})

    def test_one_operational_handoff_event_reverifies_without_replaying_portion(self):
        self._handoff_case('once')

    def test_repeated_handoff_event_stops_after_one_fresh_verify(self):
        self._handoff_case('repeated')

    def test_handoff_ready_change_retains_prior_history_and_new_ready(self):
        self._handoff_case('ready_changed')

    def test_transient_auxiliary_event_is_not_retried(self):
        self._handoff_case('auxiliary')

    def test_handoff_event_cannot_renew_completion_deadline(self):
        self._handoff_case('deadline')

    def test_unknown_published_portion_and_handoff_event_never_resubmit_write(self):
        runtime, root, keeper = self.fixture()
        keeper.execute('CREATE TABLE handoff_observation(value INTEGER)')
        keeper.execute('INSERT INTO handoff_observation VALUES(0)'); keeper.commit()
        with ownership(runtime, root, seconds=60) as worker:
            receive, invoke = supervisor.receive_message, worker._invoke
            calls = []
            def wrapped(mode, now, anchor):
                calls.append(mode)
                if mode == 'portion':
                    def lose(channel):
                        receive(channel)
                        raise HistoryDelegationError('fixture_lost_after_commit')
                    with patch.object(supervisor, 'receive_message', side_effect=lose):
                        return invoke(mode, now, anchor)
                result = invoke(mode, now, anchor)
                if mode == 'verify' and calls == ['capture','verify','portion','verify']:
                    keeper.execute('UPDATE handoff_observation SET value=1'); keeper.commit()
                return result
            with patch.object(worker, '_invoke', side_effect=wrapped):
                result = worker.complete(NOW, total_seconds=180)
            self.assertEqual(calls, ['capture','verify','portion','verify','verify'])
            self.assertEqual(result['portions'], 1)
            self.assertTrue(worker._handoff_revalidation_used)
            self.assertEqual(result['edition_id'], HistoryStore(root/'history')._current()['current'])


if __name__ == "__main__":
    unittest.main()
