#!/usr/bin/env python3
"""Run only explicit saved UI cleaner jobs on the trusted server.

No scheduler is called. The process owns one flock and can recover a saved
apply claim only by readback of that exact Production Apply operation.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from apps.wb_fbs_warehouse_registry import _load_env_file
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.application.search_cluster_cleaner_self_service import LocalStageEAdapter,ManualCleanerCoordinator


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


def run(*,runtime_dir:Path,env_file:Path,admission_dir:Path,poll_seconds:float=2.0) -> None:
    admission_dir=admission_dir.resolve()
    lock_path=admission_dir/'self-service-worker.lock'
    fd=os.open(lock_path,os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        _load_env_file(env_file.resolve())
        cleaner=CleanerWeb.from_env(runtime_dir).require_service()
        adapter=LocalStageEAdapter(runtime_dir=runtime_dir,env_file=env_file,admission_dir=admission_dir)
        coordinator=ManualCleanerCoordinator(cleaner,adapter)
        while True:
            if not deployment_ready():
                try:
                    # The final deploy marker is still false. Prove that the
                    # exact initialized worker can read its durable queue,
                    # without consuming or executing any pending job.
                    coordinator.pending_jobs()
                    report_health(admission_dir,'armed')
                except Exception as exc:
                    report_health(admission_dir,'storage_wait',type(exc).__name__)
            else:
                try:
                    if coordinator.pending_jobs():
                        report_health(admission_dir,'busy')
                        coordinator.tick()
                    report_health(admission_dir,'ready')
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
