#!/usr/bin/env python3
"""Run saved cleaner jobs and owner-enabled daily slots on the trusted server.

The process owns one flock and can recover a saved
apply claim only by readback of that exact Production Apply operation.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
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
    daily.reconcile_finished()
    waiting=bool(batches and hasattr(batch_coordinator,'waiting_only') and batch_coordinator.waiting_only())
    return bool(jobs or batches and not waiting or due and due.get('state') not in {
        'skipped','missed','no_targets','waiting_for_queue'})


def armed_cycle(coordinator, batch_coordinator, daily) -> None:
    """Expose due slots while deployment is blocked; never consume work."""
    coordinator.pending_jobs()
    batch_coordinator.pending_batches()
    daily.observe_deployment_blocked()


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
        while True:
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
                except Exception as exc:
                    report_health(admission_dir,'storage_wait',type(exc).__name__)
            else:
                try:
                    waiting=batch_coordinator.waiting_only()
                    report_health(admission_dir,'waiting_wb' if waiting else 'busy')
                    stop=threading.Event()
                    heartbeat=threading.Thread(target=heartbeat_while_busy,args=(admission_dir,stop,
                                                'waiting_wb' if waiting else 'busy'),daemon=True)
                    heartbeat.start()
                    try:busy=ready_cycle(coordinator,batch_coordinator,daily)
                    finally:
                        stop.set()
                        heartbeat.join()
                    report_health(admission_dir,'busy' if busy else 'waiting_wb' if batch_coordinator.waiting_only() else 'ready')
                except Exception as exc:
                    # The exact intent stays durable. Report the failure while
                    # the supervisor keeps this process available for recovery.
                    report_health(admission_dir,'storage_wait',type(exc).__name__)
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
