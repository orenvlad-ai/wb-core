#!/usr/bin/env python3
"""Local browser review of daily settings and a completed loopback FakeWB run."""
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
    args=parser.parse_args()
    with running_integration_fixture(args.port) as (fixture,_fake,errors):
        cleaner=fixture.cleaner
        owner=Principal('owner',True,True,True,site_owner=True)
        (fixture.runtime_dir/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=1)))
        revision=schedules(cleaner,owner,'monolith')['revision']
        save_schedules(cleaner,owner,'monolith',dict(request_id='daily-web-fixture-schedule',expected_revision=revision,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:CleanerWbSource.from_env(cleaner.account),
                                        fixture_admission=fixture.web._fixture_approved_targets,
                                        now=lambda:datetime(2026,9,27,22,47,tzinfo=timezone.utc),bootstrap_owner_username='owner')
        scheduler.tick()
        deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            state=history(cleaner,owner)['items'][0]['state']
            if state in {'complete','partial','failed'}:break
            time.sleep(.25)
        scheduler.reconcile_finished()
        print(json.dumps(dict(settings_url=fixture.base_url+'/sheet-vitrina-v1/settings#auto-updates',
                              cleaner_url=fixture.url,username='owner',password=PASSWORD,
                              synthetic_wb=True,history=history(cleaner,owner)['items'][0],worker_errors=errors),
                         ensure_ascii=False),flush=True)
        try:threading.Event().wait()
        except KeyboardInterrupt:pass


if __name__=='__main__':main()
