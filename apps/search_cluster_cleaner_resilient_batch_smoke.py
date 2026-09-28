#!/usr/bin/env python3
"""Durable batch read deferral without repeating a WB write or a completed pair."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime,timezone,timedelta
import copy
import hashlib
import json
import sys
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from apps import search_cluster_cleaner_stage_e as stage_e
from apps.search_cluster_cleaner_self_service_worker import ready_cycle
from apps.search_cluster_cleaner_batch_smoke import Source,ready_service
from apps.search_cluster_cleaner_write_fixture import FakeWB,Clock as WbClock
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner import batch_child_id
from packages.application.search_cluster_cleaner_batch import (BatchCleanerCoordinator,batch_status,
    READ_RETRY_DELAYS,READ_RETRY_POLL_GRACE)
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.application.search_cluster_cleaner_self_service import ManualCleanerCoordinator,LocalStageEAdapter
from packages.adapters.search_cluster_cleaner_wb import WbReadError,CleanerWbSource,AccountLimiter
from packages.adapters.official_api_runtime import OfficialApiRuntimeConfig
from packages.contracts.search_cluster_cleaner import Account,Principal,Target


class Clock:
    def __init__(self):self.value=1000000.0
    def __call__(self):return self.value
    def advance(self,seconds):self.value+=seconds


def prepared(count):
    box=Sandbox().__enter__()
    preview=box.execute('preview')
    box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
    cleaner=ready_service(box);owner=Principal('owner',True,True,True)
    targets=[Target(11+i,101,name=f'Pair {i}',contract_verified=True) for i in range(count)]
    admitted=[dict(advert_id=t.advert_id,nm_id=101,state='verified') for t in targets]
    source=Source(targets)
    frozen=eligibility_rows(cleaner,'monolith',targets,fixture_admission=admitted)
    batch_id='synthetic-resilient-batch-'+str(count)
    cleaner.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],
        targets=[dict(advert_id=t.advert_id,nm_id=101) for t in targets]),owner,snapshot=frozen)
    return box,cleaner,owner,source,admitted,batch_id


def fail_preview(child):
    child._launch=lambda *args,**kwargs:(_ for _ in ()).throw(WbReadError('source_temporarily_unavailable',504))
    return child.tick()


def finish_no_change(cleaner,owner,job_id):
    job=cleaner.manual_job(job_id,owner)
    assert cleaner.stop_unsubmitted_manual_run(job['scan_run_id'])
    cleaner.record_manual_job(job_id,state='no_change',stage='finished',result='No changes')


def recover_after_restart():
    box,cleaner,owner,source,admitted,batch_id=prepared(3)
    clock=Clock()
    try:
        parent=BatchCleanerCoordinator(cleaner,generation='monolith',source_factory=lambda:source,
                                       fixture_admission=admitted,now=clock)
        first=parent.tick()['items'][0]['job_id']
        child=ManualCleanerCoordinator(cleaner,object())
        assert fail_preview(child)['state']=='failed'
        deferred=parent.tick()
        assert deferred['items'][0]['state']=='retry_wait' and deferred['current_index']==1,deferred
        assert deferred['items'][0]['next_retry_at'] and deferred['waiting_count']==1
        assert child.pending_jobs()==[],'a parked child must not evade parent backoff'
        for index in (1,2):
            started=parent.tick()
            job_id=started['items'][index]['job_id']
            finish_no_change(cleaner,owner,job_id)
            parent.tick()
        waiting=parent.tick()
        assert waiting['state']=='retry_wait' and waiting['done_count']==2 and waiting['waiting_count']==1,waiting
        assert waiting['not_started_count']==0 and waiting['failed_count']==0 and parent.waiting_only()
        class IdleChild:
            def pending_jobs(self):return []
        class QueuedDaily:
            def tick(self):return dict(state='waiting_for_queue')
            def reconcile_finished(self):return None
        assert not ready_cycle(IdleChild(),parent,QueuedDaily()),'WB wait must not claim active worker work'
        old_run=cleaner.manual_job(first,owner)['scan_run_id']
        # Both coordinators are rebuilt from SQLite, before and after the due time.
        parent=BatchCleanerCoordinator(cleaner,generation='monolith',source_factory=lambda:source,
                                       fixture_admission=admitted,now=clock)
        child=ManualCleanerCoordinator(cleaner,object())
        assert parent.tick()['state']=='retry_wait' and not child.pending_jobs()
        clock.advance(READ_RETRY_DELAYS[0])
        assert parent.tick()['current_index']==0
        assert child.pending_jobs()==[first]
        new_run=cleaner.manual_job(first,owner)['scan_run_id']
        assert new_run!=old_run and cleaner.manual_job(first,owner)['scan_attempt']==1
        finish_no_change(cleaner,owner,first)
        assert parent.tick()['current_index']==1
        parent.tick();parent.tick()
        final=parent.tick()
        assert final['state']=='complete' and final['done_count']==3 and final['not_started_count']==0,final
        assert final['batch_id']==batch_id and not parent.pending_batches()
        with cleaner.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-clean'",(cleaner.key,)).fetchone()[0]==3
            assert c.execute('SELECT count(*) FROM cleaner_write_operations WHERE account=?',(cleaner.key,)).fetchone()[0]==0
    finally:box.__exit__(None,None,None)


def exhausted_and_auth_stop():
    box,cleaner,owner,source,admitted,batch_id=prepared(1)
    clock=Clock()
    try:
        parent=BatchCleanerCoordinator(cleaner,generation='monolith',source_factory=lambda:source,
                                       fixture_admission=admitted,now=clock)
        child=ManualCleanerCoordinator(cleaner,object())
        parent.tick();fail_preview(child);parent.tick();parent.tick()
        for number,delay in enumerate(READ_RETRY_DELAYS,1):
            clock.value=1000000.0+delay
            assert parent.tick()['current_index']==0
            assert cleaner.manual_job(batch_child_id(batch_id,0),owner)['scan_attempt']==number
            fail_preview(child)
            state=parent.tick()
            if number<len(READ_RETRY_DELAYS):
                assert state['items'][0]['state']=='retry_wait',state
                parent.tick()
            else:
                assert state['items'][0]['state']=='partial' and state['items'][0]['error_code']=='read_retry_exhausted',state
        final=parent.tick()
        assert final['state']=='partial' and final['partial_count']==1 and final['not_started_count']==0,final
        assert not parent.pending_batches() and child.pending_jobs()==[]
    finally:box.__exit__(None,None,None)

    # A worker waking just beyond the explicit deadline must close the parked
    # pair without creating another scan run or WB call.
    box,cleaner,owner,source,admitted,batch_id=prepared(1)
    clock=Clock()
    try:
        parent=BatchCleanerCoordinator(cleaner,generation='monolith',source_factory=lambda:source,
                                       fixture_admission=admitted,now=clock)
        child=ManualCleanerCoordinator(cleaner,object())
        parent.tick();fail_preview(child);parent.tick()
        assert parent.tick()['state']=='retry_wait'
        job_id=batch_child_id(batch_id,0)
        original_run=cleaner.manual_job(job_id,owner)['scan_run_id']
        clock.advance(READ_RETRY_DELAYS[-1]+READ_RETRY_POLL_GRACE+1)
        late=parent.tick()
        assert late['state']=='partial' and late['items'][0]['error_code']=='read_retry_exhausted',late
        assert late['waiting_count']==0 and late['next_retry_at'] is None
        assert cleaner.manual_job(job_id,owner)['scan_run_id']==original_run
        assert not parent.pending_batches() and not child.pending_jobs()
        with cleaner.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_runs WHERE account=? AND trigger='manual_exact'",(cleaner.key,)).fetchone()[0]==1
    finally:box.__exit__(None,None,None)

    box,cleaner,owner,source,admitted,batch_id=prepared(2)
    clock=Clock()
    try:
        original=source._adverts
        failures=[0]
        def flaky(ids,deadline,*,strict=True):
            if ids==[11] and not failures[0]:
                failures[0]+=1
                raise WbReadError('source_temporarily_unavailable',504)
            return original(ids,deadline,strict=strict)
        source._adverts=flaky
        parent=BatchCleanerCoordinator(cleaner,generation='monolith',source_factory=lambda:source,
                                       fixture_admission=admitted,now=clock)
        first=parent.tick()
        assert first['items'][0]['state']=='retry_wait' and first['items'][0]['job_id'] is None
        second=parent.tick()['items'][1]['job_id']
        finish_no_change(cleaner,owner,second)
        parent.tick();assert parent.tick()['state']=='retry_wait'
        clock.advance(READ_RETRY_DELAYS[0])
        assert parent.tick()['current_index']==0
        first=parent.tick()['items'][0]['job_id']
        assert first==batch_child_id(batch_id,0)
        finish_no_change(cleaner,owner,first)
        parent.tick();parent.tick()
        assert parent.tick()['state']=='complete'
        assert failures==[1]
    finally:box.__exit__(None,None,None)

    # A global auth failure after one completed sibling cancels every parked
    # read with an explicit terminal reason, while retaining that success.
    box,cleaner,owner,source,admitted,batch_id=prepared(3)
    clock=Clock()
    try:
        parent=BatchCleanerCoordinator(cleaner,generation='monolith',source_factory=lambda:source,
                                       fixture_admission=admitted,now=clock)
        child=ManualCleanerCoordinator(cleaner,object())
        parent.tick();fail_preview(child);parent.tick()
        completed=parent.tick()['items'][1]['job_id']
        finish_no_change(cleaner,owner,completed)
        parent.tick()
        parent.tick()
        child._launch=lambda *args,**kwargs:(_ for _ in ()).throw(WbReadError('unauthorized',401))
        assert child.tick()['state']=='failed'
        stopped=parent.tick()
        assert stopped['state']=='partial' and stopped['error_code']=='unauthorized',stopped
        assert [item['state'] for item in stopped['items']]==['partial','no_change','failed'],stopped
        assert stopped['items'][0]['error_code']=='batch_stopped_after_unauthorized'
        assert stopped['waiting_count']==0 and stopped['next_retry_at'] is None
        assert stopped['done_count']==3 and stopped['not_started_count']==0
        assert not parent.pending_batches()
    finally:box.__exit__(None,None,None)

    box,cleaner,owner,source,admitted,batch_id=prepared(2)
    try:
        parent=BatchCleanerCoordinator(cleaner,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        parent.tick()
        child=ManualCleanerCoordinator(cleaner,object())
        child._launch=lambda *args,**kwargs:(_ for _ in ()).throw(WbReadError('unauthorized',401))
        child.tick()
        stopped=parent.tick()
        assert stopped['state']=='failed' and stopped['error_code']=='unauthorized',stopped
        assert stopped['not_started_count']==1 and stopped['waiting_count']==0
        assert not parent.pending_batches()
    finally:box.__exit__(None,None,None)

    box,cleaner,owner,source,admitted,batch_id=prepared(2)
    clock=Clock()
    try:
        parent=BatchCleanerCoordinator(cleaner,generation='monolith',source_factory=lambda:source,
                                       fixture_admission=admitted,now=clock)
        child=ManualCleanerCoordinator(cleaner,object())
        for index in (0,1):
            assert parent.tick()['items'][index]['job_id']==batch_child_id(batch_id,index)
            fail_preview(child)
            parent.tick()
        assert parent.tick()['waiting_count']==2
        clock.advance(READ_RETRY_DELAYS[0])
        assert parent.tick()['current_index']==0
        child._launch=lambda *args,**kwargs:(_ for _ in ()).throw(WbReadError('forbidden',403))
        child.tick()
        stopped=parent.tick()
        assert stopped['state']=='partial' and stopped['error_code']=='forbidden',stopped
        assert [item['state'] for item in stopped['items']]==['failed','partial'],stopped
        assert stopped['items'][1]['error_code']=='batch_stopped_after_forbidden'
        assert stopped['waiting_count']==0 and stopped['next_retry_at'] is None
        assert not parent.pending_batches()
    finally:box.__exit__(None,None,None)


def read_http_diagnostics_and_write_budget():
    calls=[]
    class Response:
        def __init__(self,status):self.status=status;self.headers={}
    class Connection:
        def __init__(self,host,port,timeout):calls.append(timeout)
        def request(self,*args,**kwargs):pass
        def getresponse(self):return Response(504 if len(calls)==1 else 200)
        def close(self):pass
    source=CleanerWbSource(account=Account('seller','scope'),
        runtime=OfficialApiRuntimeConfig('synthetic','https://advert-api.wildberries.ru',10),
        limiter=AccountLimiter(interval=0))
    with patch('packages.adapters.search_cluster_cleaner_wb.http.client.HTTPSConnection',Connection):
        try:source._call('POST','/adv/v0/normquery/stats',{},deadline=time.monotonic()+120)
        except WbReadError as exc:
            assert (exc.code,exc.status,exc.endpoint,exc.category)==(
                'source_temporarily_unavailable',504,'stats','http')
        else:raise AssertionError('504 must remain a transient read failure')
        write=source._call('POST','/adv/v0/normquery/set-minus',{},
                           deadline=time.monotonic()+10,write=True)
        assert write.status==200
    assert 24<=calls[0]<=25 and 9<=calls[1]<=10,calls


def http_504_recovery_with_guarded_worker():
    class FlakyWB(FakeWB):
        def __init__(self):
            super().__init__();self.stats_failures=0
            self.targets[12]=copy.deepcopy(self.targets[11])
        def response(self,method,path,body):
            if path.endswith('/stats') and body['items'][0]['advert_id']==11 and not self.stats_failures:
                self.stats_failures+=1
                self.calls.append((method,path,body))
                return 504,{'error':'synthetic gateway timeout'}
            return super().response(method,path,body)

    with Sandbox() as box:
        box.package['manual_admission'].append(dict(box.package['manual_admission'][0],advert_id=12))
        characteristics=[dict(id=1,name='Модель',value=['iPhone 16 Pro Max']),
                         dict(id=2,name='Тип',value=['Обычное стекло'])]
        card=dict(nm_id='101',title='Synthetic glass',vendor_code='synthetic',
                  description='Approved synthetic card',characteristics=characteristics)
        raw=json.dumps(dict(cards=[dict(card,card_digest='sha256:'+'1'*64)]),sort_keys=True).encode()
        card_path=box.admission/'card-source-approved.json';card_path.write_bytes(raw);card_path.chmod(0o600)
        box.package['provenance']['fresh_cards_sha256']='sha256:'+hashlib.sha256(raw).hexdigest()
        box.write_package()
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=ready_service(box);owner=Principal('owner',True,True,True)
        fake=FlakyWB();wb_clock=WbClock();wb_clock.base=datetime.now(timezone.utc)+timedelta(seconds=1)
        retry_time=[time.time()]
        with fake.server() as url:
            source=CleanerWbSource(account=service.account,runtime=OfficialApiRuntimeConfig('synthetic',url,2),
                fixture=True,clock=wb_clock,monotonic=wb_clock.monotonic,
                limiter=AccountLimiter(monotonic=wb_clock.monotonic,sleep=wb_clock.advance))
            original_cleaner=stage_e.KeywordCleaner
            with patch.object(stage_e.CleanerWbSource,'from_env',return_value=source), \
                 patch.object(stage_e,'fetch_current_card',side_effect=lambda nm_id:dict(card,characteristics=list(reversed(characteristics)))), \
                 patch.object(stage_e,'KeywordCleaner',side_effect=lambda *args,**kwargs:original_cleaner(*args,clock=wb_clock,**kwargs)):
                admitted=[dict(advert_id=aid,nm_id=101,state='verified') for aid in (11,12)]
                catalog=source._adverts([11,12],source.monotonic()+120)
                frozen=eligibility_rows(service,'monolith',catalog,fixture_admission=admitted)
                batch_id='synthetic-http-504-resilient'
                service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],
                    targets=[dict(advert_id=aid,nm_id=101) for aid in (11,12)]),owner,snapshot=frozen)
                adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                    fixture_admission=admitted,now=lambda:retry_time[0])
                child=ManualCleanerCoordinator(service,adapter)
                parent.tick()
                failed=child.tick()
                assert failed['state']=='failed' and failed['error_code']=='source_temporarily_unavailable',failed
                deferred=parent.tick()
                assert deferred['items'][0]['state']=='retry_wait' and deferred['current_index']==1,deferred
                assert not fake.writes and fake.stats_failures==1
                # The second pair completes while the first child is terminal.
                for _ in range(30):
                    parent.tick()
                    if child.pending_jobs():child.tick()
                    state=batch_status(service,batch_id,owner)
                    if state['state']=='retry_wait':break
                else:raise AssertionError('independent target did not complete')
                assert state['items'][1]['state']=='complete' and state['waiting_count']==1,state
                assert [write['advert_id'] for write in fake.writes]==[12],fake.writes
                # Restart then advance only the injected parent clock.
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                    fixture_admission=admitted,now=lambda:retry_time[0])
                child=ManualCleanerCoordinator(service,adapter)
                assert parent.waiting_only() and not child.pending_jobs()
                retry_time[0]+=READ_RETRY_DELAYS[0]
                for _ in range(35):
                    parent.tick()
                    if child.pending_jobs():child.tick()
                    state=batch_status(service,batch_id,owner)
                    if state['state'] in {'complete','partial','failed'}:break
                else:raise AssertionError('deferred target did not finish')
                assert state['state']=='complete' and state['done_count']==2,state
                assert [write['advert_id'] for write in fake.writes]==[12,11],fake.writes
                assert fake.stats_failures==1 and not parent.pending_batches() and not child.pending_jobs()

if __name__=='__main__':
    recover_after_restart()
    exhausted_and_auth_stop()
    read_http_diagnostics_and_write_budget()
    http_504_recovery_with_guarded_worker()
    print('search cluster cleaner resilient batch smoke: ok')
