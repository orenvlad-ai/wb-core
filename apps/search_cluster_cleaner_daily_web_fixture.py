#!/usr/bin/env python3
"""Local browser review of daily settings and synthetic scheduled history."""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from apps.search_cluster_cleaner_reconcile_integration_fixture import running_integration_fixture
from apps.search_cluster_cleaner_web_fixture import PASSWORD
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
from packages.application.search_cluster_cleaner_daily import DailyCleanerScheduler,history,save_schedules,schedules
from packages.contracts.search_cluster_cleaner import Principal


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8767)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--deployment-blocked',action='store_true',help='show an uncompleted-release occurrence without WB work')
    mode.add_argument('--deployment-missed',action='store_true',help='show an uncompleted-release occurrence after catch-up expired')
    args=parser.parse_args()
    with running_integration_fixture(args.port) as (fixture,_fake,errors):
        cleaner=fixture.cleaner
        owner=Principal('owner',True,True,True,site_owner=True)
        (fixture.runtime_dir/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=1)))
        diagnostic=args.deployment_blocked or args.deployment_missed
        original_clock=cleaner.clock
        cleaner.clock=lambda:'2026-09-27T22:00:00+00:00'  # Before 28 Sep 03:45 EKT.
        revision=schedules(cleaner,owner,'monolith')['revision']
        save_schedules(cleaner,owner,'monolith',dict(request_id='daily-web-fixture-schedule',expected_revision=revision,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        cleaner.clock=original_clock
        current=[datetime(2026,9,27,22,46,tzinfo=timezone.utc)]
        def no_wb_source():raise AssertionError('diagnostic fixture must not contact WB')
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',
                                        source_factory=no_wb_source if diagnostic else lambda:CleanerWbSource.from_env(cleaner.account),
                                        fixture_admission=fixture.web._fixture_approved_targets,
                                        now=lambda:current[0] if diagnostic else datetime(2026,9,27,22,45,30,tzinfo=timezone.utc),
                                        bootstrap_owner_username='owner')
        if diagnostic:
            assert scheduler.observe_deployment_blocked()==1
            if args.deployment_missed:
                current[0]=datetime(2026,9,28,0,46,tzinfo=timezone.utc)  # +121 min: exhausted.
                scheduler.tick()
        else:
            scheduler.tick()
            deadline=time.monotonic()+35
            while time.monotonic()<deadline:
                state=history(cleaner,owner)['items'][0]['state']
                if state in {'complete','partial','failed'}:break
                time.sleep(.25)
            scheduler.reconcile_finished()
        print(json.dumps(dict(settings_url=fixture.base_url+'/sheet-vitrina-v1/settings#auto-updates',
                              cleaner_url=fixture.url,username='owner',password=PASSWORD,
                              scenario='deployment_blocked' if args.deployment_blocked else 'deployment_missed' if args.deployment_missed else 'completed',
                              synthetic_wb=True,history=history(cleaner,owner)['items'][0],worker_errors=errors),
                         ensure_ascii=False),flush=True)
        try:threading.Event().wait()
        except KeyboardInterrupt:pass


if __name__=='__main__':main()
