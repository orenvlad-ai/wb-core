#!/usr/bin/env python3
"""Disposable localhost UI walkthrough for full cleaner reconciliation.

All cleaner outcomes are synthetic HTTP receipts. No WB client, worker, or
scheduled task is started. The normal authenticated page and cleaner routes
remain in use so browser review covers navigation, session, and CSRF handling.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.search_cluster_cleaner_web_fixture import PASSWORD, running_fixture
from packages.application import search_cluster_cleaner_batch as batch_module
from packages.contracts.search_cluster_cleaner import CleanerError

STAMP = '2026-09-27T09:00:00Z'
TARGETS = [
    dict(advert_id=10101,nm_id=101,campaign_name='Тестовая кампания 10101',product_title='Прозрачное стекло · iPhone 16 Pro Max',status='active',status_code=9,payment_type='cpm',eligible=True,reason=None,admitted=True,profile_ready=True),
    dict(advert_id=10102,nm_id=102,campaign_name='Тестовая кампания 10102',product_title='Матовое стекло · iPhone 16 Pro Max',status='paused',status_code=11,payment_type='cpm',eligible=True,reason=None,admitted=True,profile_ready=True),
    dict(advert_id=10104,nm_id=104,campaign_name='Тестовая кампания 10104',product_title='Стекло с удержанием',status='active',status_code=9,payment_type='cpm',eligible=False,reason='target_held',admitted=True,profile_ready=True),
    dict(advert_id=10103,nm_id=101,campaign_name='Завершённая кампания',product_title='Прозрачное стекло · iPhone 16 Pro Max',status='completed',status_code=7,payment_type='cpm',eligible=False,reason='unsupported_campaign_status',admitted=True,profile_ready=True),
]
CATEGORIES = {name:dict(selectable=name in ('active','paused'),reason=None if name in ('active','paused') else 'unsupported_campaign_status') for name in ('active','paused','completed','archive')}


def decision(query, state, observed, verdict, reason, *, before=None, desired=None, controversial=False, technical_reason=None, rule='APPROVED_RULE'):
    return dict(query=query,state=state,observed_state=observed,verdict=verdict,rule_id=rule,reason=reason,source='rules_ambiguous' if controversial else 'approved',before=before or observed,desired=desired or ('active' if verdict=='allow' else 'excluded'),controversial=controversial,technical_reason=technical_reason)


class Scenario:
    def __init__(self, fixture):
        self.fixture=fixture
        self.original_summary=fixture.web.summary
        self.original_reviews=fixture.web.reviews
        self.lock=threading.Lock()
        self.runs=[]
        self.posts=[]

    def summary(self, principal):
        body=self.original_summary(principal)
        with self.lock:
            if self.runs:
                run=self.runs[-1]
                body['last_manual_batch']=dict(batch_id=run['id'],state=self.phase(run),created_at=STAMP,updated_at=STAMP)
                body['last_manual_job']=None
                if self.phase(run) in ('complete','partial'):
                    body['pending_count']=0
        return body

    def reviews(self,principal,**params):
        with self.lock:
            resolved=bool(self.runs and self.phase(self.runs[-1]) in ('complete','partial'))
        return dict(items=[],next_cursor=None) if resolved else self.original_reviews(principal,**params)

    def eligibility(self, principal, *, refresh=False):
        principal.require_read()
        return dict(items=TARGETS,loading=False,error=None,categories=CATEGORIES,
                    counts=dict(total=4,eligible=2,profile_required=0,ineligible=2,unknown=0,selectable_active=1,selectable_paused=1))

    def start(self, payload, principal):
        self.fixture.web.require_mutation(principal)
        targets=payload.get('targets') or []
        allowed={(10101,101),(10102,102)}
        if not targets or any((item.get('advert_id'),item.get('nm_id')) not in allowed for item in targets):
            raise CleanerError('batch_target_ineligible','Выбрана недоступная пара',409)
        with self.lock:
            if self.runs and self.posts[-1]['request_id']==payload.get('request_id'):
                return dict(batch_id=self.runs[-1]['id'],state='queued',selected_count=len(targets),created_at=STAMP)
            number=len(self.runs)+1
            run=dict(id=f'synthetic-reconcile-{number:02d}',number=number,start=time.monotonic(),targets=targets)
            self.runs.append(run)
            self.posts.append(payload)
            return dict(batch_id=run['id'],state='queued',selected_count=len(targets),created_at=STAMP)

    def phase(self, run):
        elapsed=time.monotonic()-run['start']
        return 'queued' if elapsed<0.7 else 'running' if elapsed<3.7 else 'partial' if run['number']==1 and len(run['targets'])>1 else 'complete'

    def status(self, batch_id, principal):
        principal.require_read()
        with self.lock:
            run=next((item for item in self.runs if item['id']==batch_id),None)
            if not run:raise CleanerError('not_found','Группа не найдена',404)
            phase=self.phase(run)
            ready=phase in ('complete','partial')
            items=[]
            for index,target in enumerate(run['targets']):
                source=next(item for item in TARGETS if item['advert_id']==target['advert_id'] and item['nm_id']==target['nm_id'])
                partial=run['number']==1 and target['advert_id']==10102
                state=('partial' if partial else 'complete' if run['number']==1 else 'no_change') if ready else 'running' if index==0 and phase=='running' else 'queued'
                item=dict(index=index,advert_id=source['advert_id'],nm_id=source['nm_id'],campaign_name=source['campaign_name'],product_title=source['product_title'],selected_status=source['status'],state=state,stage='finished' if ready else 'fetching',job_id=f"{run['id']}-child-{index}" if ready else None,write_run_id=f"{run['id']}-write-{index}" if ready and run['number']==1 and not partial else None,
                          checked_total=(5 if not partial else 2) if ready and run['number']==1 else 2 if ready else None,
                          confirmed_excluded=1 if ready and run['number']==1 and not partial else 0 if ready else None,
                          returned=1 if ready and run['number']==1 and not partial else 0 if ready else None,
                          unchanged=2 if ready and run['number']==1 and not partial else 1 if ready and partial else 2 if ready else None,
                          controversial=1 if ready and run['number']==1 and not partial else 0 if ready else None,
                          allowed=2 if ready and run['number']==1 and not partial else 2 if ready else None,
                          review_count=0 if ready else None,pending_count=0 if ready else None,
                          deferred_count=1 if ready and partial else 0 if ready else None,
                          already_excluded=0 if ready else None,delivery_state='confirmed' if ready else 'unknown',
                          error_code='statistics_missing' if ready and partial else None)
                items.append(item)
            done=len(items) if ready else 0
            return dict(batch_id=batch_id,state=phase,stage='finished' if ready else 'fetching',selected_count=len(items),done_count=done,
                        completed_count=sum(item['state'] in ('complete','no_change') for item in items) if ready else 0,
                        no_change_count=len(items) if ready and run['number']>1 else 0,
                        partial_count=sum(item['state']=='partial' for item in items) if ready else 0,
                        failed_count=0,held_count=0,skipped_count=0,not_started_count=0 if ready else len(items),
                        current_index=0 if not ready else len(items),current_target=items[0] if not ready else None,
                        created_at=STAMP,updated_at='2026-09-27T09:00:04Z' if ready else '2026-09-27T09:00:01Z',items=items,error=None,error_code=None)

    def detail(self,batch_id,index,principal):
        principal.require_read()
        with self.lock:
            run=next((item for item in self.runs if item['id']==batch_id),None)
            if not run or index>=len(run['targets']):raise CleanerError('not_found','Пара не найдена',404)
            partial=run['targets'][index]['advert_id']==10102
            first=run['number']==1
            if first and not partial:
                scan=[
                    decision('старый исключённый одобренный ключ','pending_return','excluded','allow','Ранее одобренное правило',desired='active'),
                    decision('новая чужая модель iphone 17','pending_exclude','active','exclude','Другая модель',desired='excluded',rule='WRONG_MODEL'),
                    decision('неоднозначное стекло promax','allow','active','allow','Смысл неоднозначен; оставлено',controversial=True,rule='AMBIGUOUS'),
                    decision('ранее спорный, сейчас разрешён','allow','active','allow','Одобренное правило имеет приоритет',rule='APPROVED_ALLOW'),
                    decision('обычное подходящее стекло','allow','active','allow','Соответствует товару',rule='MATCH'),
                ]
                phrases=[dict(query=scan[0]['query'],target='10101:101',state='confirmed',confirmed_at=STAMP,action='return',before='excluded',desired='active',confirmed_state='active',reason=scan[0]['reason'],controversial=False),
                         dict(query=scan[1]['query'],target='10101:101',state='confirmed',confirmed_at=STAMP,action='exclude',before='active',desired='excluded',confirmed_state='excluded',reason=scan[1]['reason'],controversial=False)]
            elif first and partial:
                scan=[decision('ключ только в списке WB','pending_exclude','active','exclude','Нет статистики WB',desired='excluded',technical_reason='statistics_missing'),
                      decision('точная фраза без изменений','allow','active','allow','Соответствует товару')]
                phrases=[]
            else:
                scan=[decision('старый исключённый одобренный ключ','allow','active','allow','Правило уже выполнено'),
                      decision('новая чужая модель iphone 17','already_excluded','excluded','exclude','Правило уже выполнено')]
                phrases=[]
            return dict(job=dict(state='partial' if first and partial else 'complete' if first else 'no_change',scan_decisions=scan,write_run_id=f'{batch_id}-write-{index}' if phrases else None),
                        run=dict(effective_state='complete',phrases=phrases) if phrases else None)

    def csv_bytes(self,principal):
        principal.require_owner(self.fixture.cleaner.owner_username)
        out=StringIO()
        writer=csv.writer(out)
        writer.writerow(['target','query','before','desired','actual','confirmed','confirmed_at','reason','rule','rules_version','rules_digest','first_seen','last_seen','operation_id'])
        writer.writerow(['10101:101','неоднозначное стекло promax','active','active','active','true',STAMP,'Смысл неоднозначен; оставлено','AMBIGUOUS','fixture-v1','synthetic-digest',STAMP,STAMP,''])
        return ('\ufeff'+out.getvalue()).encode('utf-8')


@contextmanager
def running_reconcile_fixture(port=0,scenario_class=Scenario):
    with running_fixture('normal',port) as fixture:
        with fixture.cleaner.store.transaction() as db:
            db.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(fixture.cleaner.key,))
        scenario=scenario_class(fixture)
        with patch.object(fixture.web,'summary',scenario.summary),patch.object(fixture.web,'reviews',scenario.reviews),patch.object(fixture.web,'batch_eligibility',scenario.eligibility),patch.object(fixture.web,'start_manual_batch',scenario.start),patch.object(batch_module,'batch_status',lambda cleaner,batch_id,principal:scenario.status(batch_id,principal)),patch.object(batch_module,'batch_item_detail',lambda cleaner,batch_id,index,principal:scenario.detail(batch_id,index,principal)),patch.object(fixture.cleaner,'controversial_csv',scenario.csv_bytes):
            yield fixture,scenario


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve',action='store_true',required=True)
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    with running_reconcile_fixture(args.port) as (fixture,scenario):
        print(json.dumps(dict(url=fixture.url,username='owner',password=PASSWORD,synthetic=True,flow='Выберите приостановленную пару, запустите сверку, откройте детали и CSV; повторный запуск покажет отсутствие изменений.'),ensure_ascii=False),flush=True)
        try:threading.Event().wait()
        except KeyboardInterrupt:pass


if __name__=='__main__':main()
