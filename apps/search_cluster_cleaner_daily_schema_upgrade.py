#!/usr/bin/env python3
"""Explicit, identity-bound schema upgrade; inspection is read-only by default."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner_store import CleanerStore, SCHEMA_VERSION
from packages.application.storage_registry import StoreRegistry
from packages.contracts.search_cluster_cleaner import Account


def run(*,runtime_dir:Path,config_path:Path,apply:bool=False) -> dict:
    config=json.loads(config_path.read_text(encoding='utf-8'))
    if set(config)!={'seller_id','account_scope','generation','owner_username','approved_package_path'}:
        raise RuntimeError('stage E identity is incomplete')
    account=Account(config['seller_id'],config['account_scope'])
    store=CleanerStore(StoreRegistry(runtime_dir))
    with store.read() as c:
        c.execute('PRAGMA query_only=ON')
        settings=c.execute('SELECT generation,enabled,baseline_ready FROM cleaner_settings WHERE account=?',(account.key,)).fetchone()
        version=c.execute('SELECT version FROM cleaner_schema WHERE singleton=1').fetchone()[0]
        if (not settings or settings['generation']!=config['generation'] or settings['enabled']
                or not settings['baseline_ready'] or version not in {2,SCHEMA_VERSION}):
            raise RuntimeError('cleaner identity, baseline or schema version mismatch')
    if apply:
        cleaner=KeywordCleaner(store,account,owner_username=config['owner_username'])
        cleaner.initialize(generation=config['generation'])
    with store.read() as c:
        c.execute('PRAGMA query_only=ON')
        after=c.execute('SELECT version FROM cleaner_schema WHERE singleton=1').fetchone()[0]
        rows=[]
        if after==SCHEMA_VERSION:
            rows=[dict(r) for r in c.execute('SELECT schedule_id,local_time,enabled,owner,actor_authority,generation FROM cleaner_daily_schedules WHERE account=? ORDER BY schedule_id',(account.key,))]
    if apply and after!=SCHEMA_VERSION:raise RuntimeError('schema upgrade readback failed')
    return dict(before=version,after=after,applied=apply,timezone='Asia/Yekaterinburg',schedules=rows)


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir',type=Path,required=True)
    parser.add_argument('--config-path',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    print(json.dumps(run(runtime_dir=args.runtime_dir,config_path=args.config_path,apply=args.apply),ensure_ascii=False,sort_keys=True))


if __name__=='__main__':main()
