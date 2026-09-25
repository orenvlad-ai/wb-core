#!/usr/bin/env python3
"""Read-only release readiness check for the manual cleaner contour."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from packages.application.storage_registry import StoreRegistry
from packages.contracts.search_cluster_cleaner import Account

ADMISSION=Path('/var/lib/wb-core/search-cluster-cleaner-admission')
LISTENER_BASE='http://127.0.0.1:8776'
SUMMARY_PATH='/v1/sheet-vitrina-v1/ads/keyword-cleaner/summary'


def check(*,phase:str,expected_sha:str,runtime_dir:Path,admission_dir:Path=ADMISSION,
          app_dir:Path=ROOT,timeout_seconds:float=25,sleep=time.sleep) -> dict:
    if phase not in {'before_complete','after_complete'}:raise ValueError('invalid phase')
    marker=(app_dir/'.wb-core-runtime-sha').read_text(encoding='utf-8').strip()
    deployment=json.loads((app_dir/'.wb-core-deploy.json').read_text(encoding='utf-8'))
    if marker!=expected_sha or deployment.get('commit')!=expected_sha or deployment.get('deployment_complete') is not (phase=='after_complete'):
        raise RuntimeError('cleaner release SHA or deployment phase mismatch')
    # The narrow listener must exist, preserve the same auth boundary, and
    # reject an unrelated path. No session cookie or WB token is used.
    for url,expected in ((LISTENER_BASE+SUMMARY_PATH,401),(LISTENER_BASE+'/healthz',404)):
        try:
            with urllib.request.urlopen(url,timeout=3) as response:code=response.status
        except urllib.error.HTTPError as exc:code=exc.code
        if code!=expected:raise RuntimeError('cleaner listener or auth boundary mismatch')
    config=json.loads((admission_dir/'stage-e-config.json').read_text(encoding='utf-8'))
    account=Account(config['seller_id'],config['account_scope']).key
    with StoreRegistry(runtime_dir).session('operational',mode='ro',operation='cleaner_release_probe') as conn:
        conn.execute('PRAGMA query_only=ON')
        row=conn.execute('SELECT enabled,baseline_ready,restore_hold,transport_enabled FROM cleaner_settings WHERE account=?',(account,)).fetchone()
    if not row or row['enabled']!=0 or row['baseline_ready']!=1 or row['restore_hold']!=1 or row['transport_enabled']!=0:
        raise RuntimeError('manual-only cleaner setting mismatch')
    required='ready' if phase=='after_complete' else 'armed'
    deadline=time.monotonic()+timeout_seconds
    while True:
        try:
            health=json.loads((admission_dir/'self-service-worker-health.json').read_text(encoding='utf-8'))
            pid=health.get('pid')
            if isinstance(pid,int) and pid>0:os.kill(pid,0)
            else:raise ValueError('worker PID unavailable')
            if health.get('state')==required and time.time()-float(health.get('updated_at') or 0)<10:
                return dict(ok=True,phase=phase,sha=expected_sha,listener='auth_protected',worker=required,schedule='off')
        except (OSError,ValueError,TypeError):pass
        if time.monotonic()>=deadline:raise RuntimeError('cleaner worker not ready for '+phase)
        sleep(0.5)


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=('before_complete','after_complete'),required=True)
    parser.add_argument('--expected-sha',required=True)
    parser.add_argument('--runtime-dir',type=Path,required=True)
    parser.add_argument('--admission-dir',type=Path,default=ADMISSION)
    args=parser.parse_args()
    print(json.dumps(check(phase=args.phase,expected_sha=args.expected_sha,runtime_dir=args.runtime_dir,admission_dir=args.admission_dir),sort_keys=True))


if __name__=='__main__':main()
