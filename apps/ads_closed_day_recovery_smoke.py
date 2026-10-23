#!/usr/bin/env python3
"""Offline one-submit, drift, rollback and non-target preservation proof."""
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.ads_historical_recovery_smoke import FakeOfficialSource, _seed_database
from apps.ads_closed_day_recovery import AdsClosedDayRecoveryAdapter
from apps.production_apply_launcher import execute
from packages.application.ads_historical_recovery import (
    AdsHistoricalRecovery, AdsHistoricalRecoveryScope,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_write_lock, WarehouseFunctionalBusyError,
)

NAME = 'ads_closed_day_recovery_v1'
DAY = '2026-09-01'


def fixture(root):
    runtime = root / 'state'
    (runtime / 'generations' / 'test').mkdir(parents=True)
    (runtime / 'evidence').mkdir()
    (root / 'app').mkdir()
    (root / 'app' / '.wb-core-runtime-sha').write_text('a' * 40)
    manifest = {'canonical_source': 'split', 'manifest_sha256': 'sha256:' + 'b' * 64,
                'operational': {'relative_path': 'generations/test/operational.sqlite3'}}
    (runtime / 'storage_generation_manifest.json').write_text(json.dumps(manifest))
    db = runtime / manifest['operational']['relative_path']
    _seed_database(db, existing_date=date(2026, 8, 31), non_target_date=date(2026, 10, 16))
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE finance_sentinel(value TEXT)")
        conn.execute("INSERT INTO finance_sentinel VALUES('october-cost-unchanged')")
        conn.execute("INSERT INTO temporal_source_closure_state VALUES('ads_compact',?,'yesterday_closed','closure_exhausted',4,NULL,'prior',NULL,NULL,NULL)", (DAY,))
        conn.execute("INSERT INTO temporal_source_slot_snapshots VALUES('ads_compact',?,'accepted_current_snapshot','2026-09-01T11:00:00Z','{}')", (DAY,))
    scope = AdsHistoricalRecoveryScope.build(nm_ids=[245720334], target_dates=[date.fromisoformat(DAY)])
    recovery = AdsHistoricalRecovery(db_path=db, source=FakeOfficialSource(empty_date=date(2026, 8, 29)))
    plan = recovery.plan(scope)
    assert plan['status'] == 'ready', plan['blockers']
    path = runtime / 'evidence' / 'source-plan.json'
    path.write_text(json.dumps(plan))
    request = {'runtime_dir': str(runtime), 'runtime_sha': 'a' * 40,
               'storage_manifest_sha256': manifest['manifest_sha256'], 'hostname': socket.gethostname(),
               'reviewed_plan_path': str(path), 'plan_fingerprint': plan['fingerprint'],
               'nm_ids': [245720334], 'dates': [DAY]}
    return runtime, db, request


def envelope(adapter, request, operation, action='preview', **extra):
    return execute(action=action, adapter_name=NAME, operation_id=operation, request=request,
                   adapters={NAME: adapter}, **extra)


def apply(adapter, request, operation, preview):
    return envelope(adapter, request, operation, 'apply', expected_prestate=preview['prestate_sha256'],
                    expected_candidate=preview['candidate_sha256'])


def main():
    with TemporaryDirectory() as raw:
        runtime, db, request = fixture(Path(raw))
        adapter = AdsClosedDayRecoveryAdapter()
        operation = 'ads-closed-day-smoke-success'
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        with adapter.context(request)[2]._connect(read_only=True) as conn:
            assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
        preview = envelope(adapter, request, operation)
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before
        assert not adapter.backup_path(runtime, operation).exists()
        bad = deepcopy(request); bad['runtime_sha'] = 'c' * 40
        try:
            envelope(adapter, bad, operation)
        except ValueError:
            pass
        else:
            raise AssertionError('runtime drift accepted')
        path = Path(request['reviewed_plan_path']); original = path.read_text()
        plan = json.loads(original); plan['target_manifest'][0]['payload_json'] = '{}'
        path.write_text(json.dumps(plan))
        try:
            envelope(adapter, request, operation)
        except ValueError:
            pass
        except RuntimeError:
            pass
        else:
            raise AssertionError('tampered source accepted')
        path.write_text(original)
        held, release = Event(), Event()
        def foreign_writer():
            with warehouse_functional_write_lock(runtime):
                held.set()
                release.wait(5)
        writer = Thread(target=foreign_writer)
        writer.start()
        assert held.wait(5)
        try:
            apply(adapter, request, operation, preview)
        except WarehouseFunctionalBusyError:
            pass
        else:
            raise AssertionError('foreign canonical writer did not block apply')
        finally:
            release.set()
            writer.join()
        assert not adapter.backup_path(runtime, operation).exists()
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before
        with patch('packages.application.ads_historical_recovery.create_coherent_sqlite_backup',
                   side_effect=AssertionError('whole database backup must not run')):
            result = apply(adapter, request, operation, preview)
        assert result['state'] == 'applied', result
        backup = adapter.backup_path(runtime, operation)
        image = json.loads(backup.read_text())
        assert backup.stat().st_mode & 0o777 == 0o600
        assert image['target_before']['snapshots'] == []
        assert image['target_before']['closures'][0]['attempt_count'] == 4
        with sqlite3.connect(db) as conn:
            assert conn.execute('SELECT value FROM finance_sentinel').fetchone()[0] == 'october-cost-unchanged'
            assert conn.execute("SELECT payload_json FROM temporal_source_slot_snapshots WHERE snapshot_date=? AND snapshot_role='accepted_current_snapshot'", (DAY,)).fetchone()[0] == '{}'
            assert conn.execute("SELECT state,attempt_count FROM temporal_source_closure_state WHERE target_date=?", (DAY,)).fetchone() == ('success', 5)
            assert conn.execute('SELECT COUNT(*) FROM ads_historical_recovery_audit').fetchone()[0] == 1
        committed = hashlib.sha256(db.read_bytes()).hexdigest()
        assert apply(adapter, request, operation, preview)['state'] == 'applied'
        assert hashlib.sha256(db.read_bytes()).hexdigest() == committed

    with TemporaryDirectory() as raw:
        runtime, db, request = fixture(Path(raw))
        adapter = AdsClosedDayRecoveryAdapter()
        preview = envelope(adapter, request, 'ads-closed-day-smoke-drift')
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at='changed' WHERE snapshot_date='2026-10-16'")
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        try:
            apply(adapter, request, 'ads-closed-day-smoke-drift', preview)
        except ValueError:
            pass
        else:
            raise AssertionError('non-target source drift accepted')
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before
        assert not adapter.backup_path(runtime, 'ads-closed-day-smoke-drift').exists()

    with TemporaryDirectory() as raw:
        runtime, db, request = fixture(Path(raw))
        adapter = AdsClosedDayRecoveryAdapter()
        operation = 'ads-closed-day-smoke-rollback'
        preview = envelope(adapter, request, operation)
        original_context = adapter.context
        def failing_context(req):
            context = original_context(req)
            context[2].failure_injector = lambda _count: (_ for _ in ()).throw(RuntimeError('injected'))
            return context
        adapter.context = failing_context
        result = apply(adapter, request, operation, preview)
        assert result['state'] == 'ambiguous', result
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM temporal_source_slot_snapshots WHERE snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'", (DAY,)).fetchone()[0] == 0
            assert conn.execute("SELECT state,attempt_count FROM temporal_source_closure_state WHERE target_date=?", (DAY,)).fetchone() == ('closure_exhausted', 4)
        try:
            apply(adapter, request, operation, preview)
        except RuntimeError:
            pass
        else:
            raise AssertionError('attempted operation was submitted again')
    print('ads closed-day recovery smoke: ok')


if __name__ == '__main__':
    main()
