#!/usr/bin/env python3
"""The cleaner stays quiet at idle and wakes for durable intent and slots."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import search_cluster_cleaner_self_service_worker as worker
from packages.application.business_data_write_barrier import acquire_barrier, confirm_barrier_hold, release_barrier, barrier_status


class Store:
    def __init__(self):
        self.connection=sqlite3.connect(':memory:')
        self.connection.row_factory=sqlite3.Row
        self.connection.executescript('''
            CREATE TABLE cleaner_events(sequence INTEGER PRIMARY KEY,account TEXT);
            CREATE TABLE cleaner_daily_schedules(account TEXT,schedule_id TEXT,local_time TEXT,
                                                 created_at TEXT,enabled INTEGER);
            CREATE TABLE cleaner_daily_occurrences(account TEXT,schedule_id TEXT,local_date TEXT,
                                                   state TEXT,details TEXT);
        ''')

    @contextmanager
    def read(self):yield self.connection



@contextmanager
def frozen_worker_clock(now):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    with patch.object(worker, 'datetime', FrozenDateTime), patch.object(worker.time, 'time', return_value=now.timestamp()):
        yield


def next_due_floor_regressions():
    # Existing two-second anti-spin floor is intentional. It may cross the
    # minute boundary; a fixture must not expect an exact slot less than 2s away.
    for hour, minute, second in ((16, 2, 57), (16, 2, 58), (16, 2, 59), (23, 59, 59)):
        local = datetime(2026, 10, 4, hour, minute, second, 900000, tzinfo=worker.ZONE)
        slot = (local + timedelta(minutes=1)).replace(second=0, microsecond=0)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = Store()
            try:
                cleaner = SimpleNamespace(store=store, key='seller:scope')
                store.connection.execute('INSERT INTO cleaner_daily_schedules VALUES(?,?,?,?,1)',
                    (cleaner.key, 'default', slot.strftime('%H:%M'), (local-timedelta(days=1)).isoformat()))
                wake = worker.IdleWake(cleaner, root)
                with frozen_worker_clock(local), patch.object(worker, 'ROOT', root):
                    wake.refresh(wake.event_sequence(), wake.environment_version())
                    assert abs(wake.next_due-max(local.timestamp()+2, slot.timestamp())) < 0.001
                    assert not wake.changed()
                    with patch.object(worker.time, 'time', return_value=wake.next_due):
                        assert wake.changed()
            finally:
                store.connection.close()


def maintenance_read_quiescence_regressions():
    plan='sha256:'+'a'*64
    window='cleaner-read-quiescence'
    def acquire(runtime):
        acquire_barrier(runtime,window_id=window,window_kind='maintenance_pause',plan_fingerprint=plan,
                        approval_reference='fixture-approved',actor='fixture',reason='local read quiescence test')
    for ready in (False,True):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);runtime=root/'state';runtime.mkdir();admission=root/'admission';admission.mkdir()
            store=Store();calls=[];reads=[];polls=[]
            before='\n'.join(store.connection.iterdump())
            original_read=store.read
            @contextmanager
            def read():
                assert not barrier_status(runtime)['active'],'queue read while maintenance held'
                reads.append('store')
                with original_read() as connection:yield connection
            store.read=read
            cleaner=SimpleNamespace(store=store,key='seller:scope')
            def observed(name,result=None):
                def call():
                    assert not barrier_status(runtime)['active'],name
                    calls.append(name);return result
                return call
            child=SimpleNamespace(pending_jobs=observed('jobs',[]),
                release_terminal_unsent_scans=observed('release'),tick=observed('manual'))
            batch=SimpleNamespace(pending_batches=observed('batches',[]),
                waiting_only=observed('waiting',False),tick=observed('batch'))
            daily=SimpleNamespace(tick=observed('daily'),reconcile_finished=observed('reconcile'),
                observe_deployment_blocked=observed('armed'))
            acquire(runtime)
            def sleep(_seconds):
                polls.append(True)
                if len(polls)<=2:
                    health=json.loads((admission/'self-service-worker-health.json').read_text())
                    assert health['state']==('maintenance' if ready else 'armed'),health
                    assert health['reason']=='MaintenanceAdmissionBlocked',health
                    assert not calls and not reads,(calls,reads)
                    assert '\n'.join(store.connection.iterdump())==before
                    if len(polls)==1:
                        confirm_barrier_hold(runtime,window_id=window,plan_fingerprint=plan,
                            maintenance_state=dict(schema_version='business_data_maintenance_pause_v1',phase='held',
                                                   hold_readback=dict(quiet=True)))
                    else:
                        release_barrier(runtime,window_id=window,plan_fingerprint=plan,actor='fixture',
                            reason='local exact release',restore_readback=dict(status='restored',exact_prior_state_restored=True))
                else:raise KeyboardInterrupt
            try:
                with (patch.object(worker,'ROOT',root),patch.object(worker,'_load_env_file'),
                      patch.object(worker,'deployment_ready',return_value=ready),
                      patch.object(worker.CleanerWeb,'from_env',return_value=SimpleNamespace(require_service=lambda:cleaner,generation='current')),
                      patch.object(worker,'LocalStageEAdapter'),patch.object(worker,'ManualCleanerCoordinator',return_value=child),
                      patch.object(worker,'BatchCleanerCoordinator',return_value=batch),
                      patch.object(worker,'DailyCleanerScheduler',return_value=daily),
                      patch.object(worker.time,'sleep',side_effect=sleep)):
                    try:worker.run(runtime_dir=runtime,env_file=root/'env',admission_dir=admission)
                    except KeyboardInterrupt:pass
                assert calls and reads,(calls,reads)
                assert ('daily' if ready else 'armed') in calls,calls
                assert '\n'.join(store.connection.iterdump())==before
            finally:store.connection.close()

    # The early metadata check does not replace real admission: activate the
    # barrier after that check and prove the ready body is still never entered.
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw);runtime=root/'state';runtime.mkdir();admission=root/'admission';admission.mkdir()
        cleaner=SimpleNamespace(key='seller:scope')
        def raced_sequence():acquire(runtime);return 0
        wake=SimpleNamespace(event_sequence=raced_sequence,environment_version=lambda:())
        with (patch.object(worker,'_load_env_file'),patch.object(worker,'deployment_ready',return_value=True),
              patch.object(worker.CleanerWeb,'from_env',return_value=SimpleNamespace(require_service=lambda:cleaner,generation='current')),
              patch.object(worker,'LocalStageEAdapter'),patch.object(worker,'ManualCleanerCoordinator') as coordinator,
              patch.object(worker,'BatchCleanerCoordinator') as batches,patch.object(worker,'DailyCleanerScheduler'),
              patch.object(worker,'IdleWake',return_value=wake),patch.object(worker,'ready_cycle') as body,
              patch.object(worker.time,'sleep',side_effect=KeyboardInterrupt)):
            batches.return_value.waiting_only.return_value=False
            try:worker.run(runtime_dir=runtime,env_file=root/'env',admission_dir=admission)
            except KeyboardInterrupt:pass
            body.assert_not_called()
            coordinator.return_value.release_terminal_unsent_scans.assert_not_called()
            health=json.loads((admission/'self-service-worker-health.json').read_text())
            assert health['state']=='maintenance' and health['reason']=='MaintenanceAdmissionBlocked',health


def main():
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw)
        runtime=root/'state';runtime.mkdir()
        admission=root/'admission';admission.mkdir()
        store=Store()
        cleaner=SimpleNamespace(store=store,key='seller:scope')
        child=SimpleNamespace(calls=0,consumed=False,released=0)
        def release():child.released+=1;return 0
        child.release_terminal_unsent_scans=release
        child.pending_jobs=lambda: ['manual-intent'] if store.connection.execute(
            'SELECT 1 FROM cleaner_events LIMIT 1').fetchone() and not child.consumed else []
        def consume():child.calls+=1;child.consumed=True
        child.tick=consume
        batch=SimpleNamespace(pending_batches=lambda:[],waiting_only=lambda:False,tick=lambda:None)
        daily=SimpleNamespace(ticks=0)
        def tick():daily.ticks+=1;return None
        daily.tick=tick
        daily.reconcile_finished=lambda:None
        polls=[0]
        def sleep(_seconds):
            polls[0]+=1
            if polls[0]==3:
                store.connection.execute('INSERT INTO cleaner_events(account) VALUES(?)',(cleaner.key,))
            if polls[0]>=6:raise KeyboardInterrupt
        with (patch.object(worker,'ROOT',root),patch.object(worker,'_load_env_file'),
              patch.object(worker,'deployment_ready',return_value=True),
              patch.object(worker.CleanerWeb,'from_env',return_value=SimpleNamespace(require_service=lambda:cleaner,generation='current')),
              patch.object(worker,'LocalStageEAdapter'),
              patch.object(worker,'ManualCleanerCoordinator',return_value=child),
              patch.object(worker,'BatchCleanerCoordinator',return_value=batch),
              patch.object(worker,'DailyCleanerScheduler',return_value=daily),
              patch.object(worker.time,'sleep',side_effect=sleep)):
            try:worker.run(runtime_dir=runtime,env_file=root/'env',admission_dir=admission)
            except KeyboardInterrupt:pass
        assert daily.ticks==3,daily.ticks  # cold start, manual event, then drain to idle
        assert child.calls==1 and child.released==daily.ticks
        assert json.loads((admission/'self-service-worker-health.json').read_text())['state']=='ready'

        wake=worker.IdleWake(cleaner,runtime)
        local=datetime(2026, 10, 4, 16, 2, 10, tzinfo=worker.ZONE)
        next_slot=(local+timedelta(minutes=1)).replace(second=0,microsecond=0)
        store.connection.execute('INSERT INTO cleaner_daily_schedules VALUES(?,?,?,?,1)',
            (cleaner.key,'default',next_slot.strftime('%H:%M'),(local-timedelta(days=1)).isoformat()))
        past_slot=(local-timedelta(minutes=1)).strftime('%H:%M')
        store.connection.execute('INSERT INTO cleaner_daily_schedules VALUES(?,?,?,?,1)',
            (cleaner.key,'paused-slot',past_slot,(local-timedelta(days=1)).isoformat()))
        with patch.object(worker,'ROOT',root), frozen_worker_clock(local):
            wake.refresh(wake.event_sequence(),wake.environment_version())
            assert abs(wake.next_due-next_slot.timestamp())<1
            assert not wake.changed()
            with patch.object(worker.time,'time',return_value=wake.next_due):assert wake.changed()
            today=local.date().isoformat()
            retry=(local.astimezone(timezone.utc)+timedelta(minutes=15)).isoformat()
            store.connection.execute('INSERT INTO cleaner_daily_occurrences VALUES(?,?,?,?,?)',
                (cleaner.key,'default',today,'start_wait',json.dumps({'next_retry_at':retry})))
            wake.refresh(wake.event_sequence(),wake.environment_version())
            assert abs(wake.next_due-datetime.fromisoformat(retry).timestamp())<1
            rollover=(local.astimezone(timezone.utc)+timedelta(minutes=5)).isoformat()
            prior=(local-timedelta(days=1)).date().isoformat()
            store.connection.execute('INSERT INTO cleaner_daily_occurrences VALUES(?,?,?,?,?)',
                (cleaner.key,'late',prior,'start_wait',json.dumps({'next_retry_at':rollover})))
            wake.refresh(wake.event_sequence(),wake.environment_version())
            assert abs(wake.next_due-datetime.fromisoformat(rollover).timestamp())<1
            (root/'.wb-core-deploy.json').write_text('{}')
            assert wake.changed()  # completion marker change wakes an armed worker
        receipts=iter([{'state':'complete'},{'state':'failed'},None])
        daily.reconcile_finished=lambda:next(receipts)
        assert worker.ready_cycle(child,batch,daily)
        assert worker.ready_cycle(child,batch,daily)
        assert not worker.ready_cycle(child,batch,daily)
        store.connection.close()
    next_due_floor_regressions()
    maintenance_read_quiescence_regressions()
    print('search cluster cleaner idle smoke: ok')


if __name__=='__main__':main()
