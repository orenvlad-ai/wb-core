#!/usr/bin/env python3
"""Run saved cleaner jobs and owner-enabled daily slots on the trusted server.

The process owns one flock and can recover a saved
apply claim only by readback of that exact Production Apply operation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from apps.wb_fbs_warehouse_registry import _load_env_file
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.application.search_cluster_cleaner_self_service import LocalStageEAdapter,ManualCleanerCoordinator
from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator
from packages.application.search_cluster_cleaner_daily import DailyCleanerScheduler
from packages.application.search_cluster_cleaner_daily import ZONE


def deployment_ready() -> bool:
    try:
        marker=(ROOT/'.wb-core-runtime-sha').read_text(encoding='utf-8').strip()
        deployment=json.loads((ROOT/'.wb-core-deploy.json').read_text(encoding='utf-8'))
        return bool(marker and deployment.get('commit')==marker and deployment.get('deployment_complete') is True)
    except (OSError,ValueError):return False


def report_health(admission_dir:Path,state:str,reason:str='') -> None:
    path=admission_dir/'self-service-worker-health.json'
    temporary=admission_dir/'.self-service-worker-health.tmp'
    value=dict(pid=os.getpid(),state=state,reason=reason,updated_at=time.time())
    temporary.write_text(json.dumps(value,sort_keys=True,separators=(',',':')),encoding='utf-8')
    temporary.chmod(0o600)
    os.replace(temporary,path)


def heartbeat_while_busy(admission_dir:Path, stop:threading.Event, state:str='busy') -> None:
    # One guarded batch can take longer than the release probe's freshness
    # window. The live worker keeps its busy receipt fresh until it finishes.
    while not stop.wait(2):
        report_health(admission_dir,state)


def ready_cycle(coordinator, batch_coordinator, daily) -> bool:
    """Record due intent even while the shared manual queue is busy."""
    jobs=coordinator.pending_jobs()
    batches=batch_coordinator.pending_batches()
    due=daily.tick()
    if jobs:coordinator.tick()
    if batches:batch_coordinator.tick()
    reconciled=daily.reconcile_finished()
    waiting=bool(batches and hasattr(batch_coordinator,'waiting_only') and batch_coordinator.waiting_only())
    return bool(reconciled or jobs or batches and not waiting or due and due.get('state') not in {
        'skipped','missed','no_targets','waiting_for_queue'})


def armed_cycle(coordinator, batch_coordinator, daily) -> None:
    """Expose due slots while deployment is blocked; never consume work."""
    coordinator.pending_jobs()
    batch_coordinator.pending_batches()
    daily.observe_deployment_blocked()


class IdleWake:
    """Watch cheap durable signals while no cleaner work is pending.

    The event sequence is sampled before a full cycle. An enqueue racing with
    that cycle therefore remains visible on the next probe.
    """
    def __init__(self, cleaner, runtime_dir:Path):
        self.cleaner=cleaner
        self.runtime_dir=runtime_dir
        self.sequence=-1
        self.environment=None
        self.next_due=0.0

    def event_sequence(self) -> int:
        with self.cleaner.store.read() as c:
            row=c.execute('SELECT MAX(sequence) FROM cleaner_events WHERE account=?',(self.cleaner.key,)).fetchone()
        return int(row[0] or 0)

    def environment_version(self) -> tuple:
        paths=(ROOT/'.wb-core-runtime-sha',ROOT/'.wb-core-deploy.json',
               self.runtime_dir/'.auto-updates-policy.json',
               self.runtime_dir/'.business-data-maintenance.json',
               self.runtime_dir/'.business-data-write-barrier.json')
        return tuple((p.stat().st_mtime_ns,p.stat().st_size) if p.exists() else None for p in paths)

    def refresh(self, sequence_before_cycle:int, environment_before_cycle:tuple) -> None:
        self.sequence=sequence_before_cycle
        self.environment=environment_before_cycle
        now=datetime.now(timezone.utc)
        local=now.astimezone(ZONE)
        today=local.date().isoformat()
        try:
            with self.cleaner.store.read() as c:
                schedules=c.execute('SELECT schedule_id,local_time FROM cleaner_daily_schedules WHERE account=? AND enabled=1',
                                    (self.cleaner.key,)).fetchall()
                occurrences={row['schedule_id']:row for row in c.execute(
                    'SELECT schedule_id,state,details FROM cleaner_daily_occurrences WHERE account=? AND local_date=?',
                    (self.cleaner.key,today))}
                older=c.execute('''SELECT state,details FROM cleaner_daily_occurrences
                    WHERE account=? AND local_date<? AND state IN ('pending','catalog_wait','start_wait','deployment_blocked')
                    ORDER BY local_date DESC LIMIT 48''',(self.cleaner.key,today)).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):raise
            # The pre-release worker may start before the schedule migration.
            self.next_due=now.timestamp()+30
            return
        deadlines=[]
        for item in older:
            retry=json.loads(item['details']).get('next_retry_at')
            deadlines.append(datetime.fromisoformat(retry.replace('Z','+00:00')).timestamp() if retry else now.timestamp())
        for schedule in schedules:
            hour,minute=map(int,schedule['local_time'].split(':'))
            due=local.replace(hour=hour,minute=minute,second=0,microsecond=0)
            item=occurrences.get(schedule['schedule_id'])
            if item is None:
                if due.astimezone(timezone.utc)<now:
                    # The full cycle just tried this slot. A paused policy or
                    # unready baseline can deliberately leave no occurrence;
                    # the policy/event signal wakes us if that changes.
                    due+=timedelta(days=1)
            elif item['state'] in {'pending','catalog_wait','start_wait','deployment_blocked'}:
                retry=json.loads(item['details']).get('next_retry_at')
                if retry:
                    due=datetime.fromisoformat(retry.replace('Z','+00:00')).astimezone(ZONE)
                else:deadlines.append(now.timestamp());continue
            else:due+=timedelta(days=1)
            deadlines.append(due.timestamp())
        self.next_due=max(now.timestamp()+2,min(deadlines)) if deadlines else float('inf')

    def changed(self) -> bool:
        return (time.time()>=self.next_due or self.event_sequence()!=self.sequence
                or self.environment_version()!=self.environment)


def run(*,runtime_dir:Path,env_file:Path,admission_dir:Path,poll_seconds:float=2.0) -> None:
    admission_dir=admission_dir.resolve()
    lock_path=admission_dir/'self-service-worker.lock'
    fd=os.open(lock_path,os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        _load_env_file(env_file.resolve())
        web=CleanerWeb.from_env(runtime_dir)
        cleaner=web.require_service()
        adapter=LocalStageEAdapter(runtime_dir=runtime_dir,env_file=env_file,admission_dir=admission_dir)
        bootstrap_owner_username=os.environ.get('WB_CORE_WEB_AUTH_USERNAME','')
        coordinator=ManualCleanerCoordinator(cleaner,adapter,bootstrap_owner_username=bootstrap_owner_username)
        batch_coordinator=BatchCleanerCoordinator(cleaner,generation=web.generation,bootstrap_owner_username=bootstrap_owner_username)
        daily=DailyCleanerScheduler(cleaner,generation=web.generation,bootstrap_owner_username=bootstrap_owner_username,
                                    deployment_check=deployment_ready)
        wake=IdleWake(cleaner,runtime_dir)
        idle=False
        while True:
            try:
                if idle and not wake.changed():
                    report_health(admission_dir,'ready' if deployment_ready() else 'armed')
                    time.sleep(poll_seconds)
                    continue
                sequence_before_cycle=wake.event_sequence()
                environment_before_cycle=wake.environment_version()
            except Exception as exc:
                report_health(admission_dir,'storage_wait',type(exc).__name__)
                idle=False
                time.sleep(poll_seconds)
                continue
            if not deployment_ready():
                try:
                    # The final deploy marker is still false. Prove that the
                    # exact initialized worker can read its durable queue,
                    # without consuming or executing any pending job.
                    report_health(admission_dir,'armed')
                    stop=threading.Event()
                    heartbeat=threading.Thread(target=heartbeat_while_busy,args=(admission_dir,stop,'armed'),daemon=True)
                    heartbeat.start()
                    try:armed_cycle(coordinator,batch_coordinator,daily)
                    finally:
                        stop.set()
                        heartbeat.join()
                    report_health(admission_dir,'armed')
                    idle=True
                except Exception as exc:
                    report_health(admission_dir,'storage_wait',type(exc).__name__)
                    idle=False
            else:
                try:
                    waiting=batch_coordinator.waiting_only()
                    report_health(admission_dir,'waiting_wb' if waiting else 'busy')
                    stop=threading.Event()
                    heartbeat=threading.Thread(target=heartbeat_while_busy,args=(admission_dir,stop,
                                                'waiting_wb' if waiting else 'busy'),daemon=True)
                    heartbeat.start()
                    try:
                        from packages.application.business_data_procedure_admission import admitted_write
                        with admitted_write(runtime_dir):
                            busy=ready_cycle(coordinator,batch_coordinator,daily)
                    finally:
                        stop.set()
                        heartbeat.join()
                    report_health(admission_dir,'busy' if busy else 'waiting_wb' if batch_coordinator.waiting_only() else 'ready')
                    idle=not busy and not batch_coordinator.pending_batches() and not coordinator.pending_jobs()
                except Exception as exc:
                    # The exact intent stays durable. Report the failure while
                    # the supervisor keeps this process available for recovery.
                    from packages.application.business_data_procedure_admission import MaintenanceAdmissionBlocked
                    report_health(admission_dir,'maintenance' if isinstance(exc,MaintenanceAdmissionBlocked) else 'storage_wait',type(exc).__name__)
                    idle=False
            if idle:
                try:wake.refresh(sequence_before_cycle,environment_before_cycle)
                except Exception as exc:
                    report_health(admission_dir,'storage_wait',type(exc).__name__)
                    idle=False
            time.sleep(poll_seconds)
    finally:
        os.close(fd)


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir',type=Path,default=Path('/opt/wb-core-runtime/state'))
    parser.add_argument('--env-file',type=Path,default=Path('/opt/wb-ai/.env'))
    parser.add_argument('--admission-dir',type=Path,default=Path('/var/lib/wb-core/search-cluster-cleaner-admission'))
    args=parser.parse_args()
    run(runtime_dir=args.runtime_dir,env_file=args.env_file,admission_dir=args.admission_dir)


if __name__=='__main__':main()
