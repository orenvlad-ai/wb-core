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


def main():
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw)
        runtime=root/'state';runtime.mkdir()
        admission=root/'admission';admission.mkdir()
        store=Store()
        cleaner=SimpleNamespace(store=store,key='seller:scope')
        child=SimpleNamespace(calls=0,consumed=False)
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
        assert child.calls==1
        assert json.loads((admission/'self-service-worker-health.json').read_text())['state']=='ready'

        wake=worker.IdleWake(cleaner,runtime)
        local=datetime.now(worker.ZONE)
        next_slot=(local+timedelta(minutes=1)).replace(second=0,microsecond=0)
        store.connection.execute('INSERT INTO cleaner_daily_schedules VALUES(?,?,?,?,1)',
            (cleaner.key,'default',next_slot.strftime('%H:%M'),(local-timedelta(days=1)).isoformat()))
        past_slot=(local-timedelta(minutes=1)).strftime('%H:%M')
        store.connection.execute('INSERT INTO cleaner_daily_schedules VALUES(?,?,?,?,1)',
            (cleaner.key,'paused-slot',past_slot,(local-timedelta(days=1)).isoformat()))
        with patch.object(worker,'ROOT',root):
            wake.refresh(wake.event_sequence(),wake.environment_version())
            assert abs(wake.next_due-next_slot.timestamp())<1
            assert not wake.changed()
            with patch.object(worker.time,'time',return_value=wake.next_due):assert wake.changed()
            today=local.date().isoformat()
            retry=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat()
            store.connection.execute('INSERT INTO cleaner_daily_occurrences VALUES(?,?,?,?,?)',
                (cleaner.key,'default',today,'start_wait',json.dumps({'next_retry_at':retry})))
            wake.refresh(wake.event_sequence(),wake.environment_version())
            assert abs(wake.next_due-datetime.fromisoformat(retry).timestamp())<1
            rollover=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
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
    print('search cluster cleaner idle smoke: ok')


if __name__=='__main__':main()
