#!/usr/bin/env python3
"""Disposable UI walkthrough of one deferred WB stats read and automatic resume."""
from __future__ import annotations

import argparse
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from apps.search_cluster_cleaner_reconcile_web_fixture import Scenario,TARGETS,STAMP,running_reconcile_fixture,PASSWORD
from packages.contracts.search_cluster_cleaner import CleanerError


class ResilientScenario(Scenario):
    def phase(self,run):
        elapsed=time.monotonic()-run['start']
        return 'queued' if elapsed<1 else 'running' if elapsed<5 else 'retry_wait' if elapsed<60 else 'running' if elapsed<70 else 'complete'

    def status(self,batch_id,principal):
        principal.require_read()
        with self.lock:
            run=next((item for item in self.runs if item['id']==batch_id),None)
            if not run:raise CleanerError('not_found','Группа не найдена',404)
            phase=self.phase(run);elapsed=time.monotonic()-run['start']
            finished=phase=='complete'
            waiting=phase=='retry_wait'
            next_at=(datetime.now(timezone.utc)+timedelta(seconds=max(0,60-elapsed))).isoformat() if waiting else None
            items=[]
            for index,target in enumerate(run['targets']):
                source=next(item for item in TARGETS if item['advert_id']==target['advert_id'] and item['nm_id']==target['nm_id'])
                first=index==0
                state=('no_change' if finished else 'retry_wait' if waiting and first else
                       'no_change' if elapsed>=5 and not first else 'running' if first else 'queued')
                item=dict(index=index,advert_id=source['advert_id'],nm_id=source['nm_id'],campaign_name=source['campaign_name'],
                          product_title=source['product_title'],selected_status=source['status'],state=state,stage=state,
                          job_id=f"{run['id']}-child-{index}",write_run_id=None,error_code='source_temporarily_unavailable' if state=='retry_wait' else None,
                          next_retry_at=next_at if state=='retry_wait' else None,retry_attempt=1 if state=='retry_wait' else None,
                          checked_total=2 if state=='no_change' else None,confirmed_excluded=0 if state=='no_change' else None,
                          returned=0 if state=='no_change' else None,unchanged=2 if state=='no_change' else None,
                          controversial=0 if state=='no_change' else None,allowed=2 if state=='no_change' else None,
                          review_count=0 if state=='no_change' else None,pending_count=0 if state=='no_change' else None,
                          deferred_count=0 if state=='no_change' else None,already_excluded=0 if state=='no_change' else None,
                          delivery_state='unknown',review_required=False,can_recheck=False)
                items.append(item)
            done=sum(item['state']=='no_change' for item in items)
            return dict(batch_id=batch_id,state=phase,stage=phase,selected_count=len(items),done_count=done,
                        completed_count=done,no_change_count=done,partial_count=0,failed_count=0,
                        held_count=0,skipped_count=0,not_started_count=sum(item['state']=='queued' for item in items),
                        waiting_count=sum(item['state']=='retry_wait' for item in items),next_retry_at=next_at,
                        current_index=0 if not finished and not waiting else len(items),current_target=None,
                        created_at=STAMP,updated_at=datetime.now(timezone.utc).isoformat(),items=items,error=None,error_code=None)

    def detail(self,batch_id,index,principal):
        principal.require_read()
        return dict(job=dict(state='no_change',scan_decisions=[],write_run_id=None),run=None)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve',action='store_true',required=True)
    parser.add_argument('--port',type=int,default=8767)
    args=parser.parse_args()
    with running_reconcile_fixture(args.port,scenario_class=ResilientScenario) as (fixture,_):
        print(json.dumps(dict(url=fixture.url,username='owner',password=PASSWORD,synthetic=True,
            flow='Выберите обе пары и запустите группу. Через 5 секунд одна пара будет готова, другая подождёт WB до 60-й секунды; затем начнётся повтор и к 70-й секунде группа завершится.'),ensure_ascii=False),flush=True)
        try:threading.Event().wait()
        except KeyboardInterrupt:pass


if __name__=='__main__':main()
