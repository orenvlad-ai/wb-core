#!/usr/bin/env python3
"""Explicit single product tick; no service/timer and no activation switch.

The external admission is default-closed. Setup and reviewed recovery activation
are separate release actions; environment values alone cannot grant a dispatch.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.application.search_cluster_cleaner_worker import product_tick
from packages.application.search_cluster_cleaner_admission import AdmissionGuard
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',action='store_true',help='Run one isolated synthetic tick against a loopback WB server')
    parser.add_argument('--runtime-dir',type=Path)
    parser.add_argument('--admission-dir',type=Path)
    parser.add_argument('--max-targets',type=int,default=10000)
    args=parser.parse_args()
    if args.fixture:
        if args.runtime_dir or args.admission_dir:parser.error('--fixture owns temporary isolated paths')
        from apps.search_cluster_cleaner_write_fixture import fixture
        with fixture() as local:
            local.start()
            result=product_tick(local.app,local.source,local.guard,generation='g1',max_targets=args.max_targets,monotonic=local.clock.monotonic)
            print(json.dumps(dict(synthetic=True,result=result,synthetic_wb_posts=len(local.fake.writes)),ensure_ascii=False))
        return
    if not args.runtime_dir or not args.admission_dir:parser.error('--runtime-dir and --admission-dir are required outside --fixture')
    # Existing setup constructor remains server-owned; it never sets readiness
    # or clears either hold. Registry schema must be installed by the release.
    web=CleanerWeb.from_env(args.runtime_dir);cleaner=web.require_service()
    source=CleanerWbSource.from_env(cleaner.account)
    guard=AdmissionGuard(args.admission_dir,cleaner.store)
    print(json.dumps(product_tick(cleaner,source,guard,generation=web.generation,max_targets=args.max_targets),ensure_ascii=False))


if __name__=='__main__':main()
