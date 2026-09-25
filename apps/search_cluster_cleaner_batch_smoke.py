#!/usr/bin/env python3
"""Synthetic durable batch ordering, eligibility, restart and owner isolation."""
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
from apps.search_cluster_cleaner_write_fixture import FakeWB,Clock
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.search_cluster_cleaner import batch_child_id
from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator,batch_status,batch_item_detail
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.application.search_cluster_cleaner_self_service import LocalStageEAdapter,ManualCleanerCoordinator
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
from packages.adapters.search_cluster_cleaner_wb import AccountLimiter
from packages.adapters.official_api_runtime import OfficialApiRuntimeConfig
from packages.contracts.search_cluster_cleaner import CleanerError,Principal,Target


class Source:
    def __init__(self,targets):self.targets=targets;self.calls=[]
    def monotonic(self):return 0.0
    def _adverts(self,ids,deadline,*,strict=True):
        self.calls.append(tuple(ids))
        values=[row for row in self.targets if row.advert_id in ids]
        if strict:return values
        missing=['adverts_missing:'+str(i) for i in ids if i not in {row.advert_id for row in values}]
        return values,missing


def rejects(action,code):
    try:action()
    except CleanerError as exc:assert exc.code==code,(exc.code,code)
    else:raise AssertionError('expected '+code)


def main():
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=box.service();owner=Principal('owner',True,True,True);other=Principal('other',True,True,True)
        admitted=[dict(advert_id=aid,nm_id=101,state='verified') for aid in (11,12,13)]
        source=Source([Target(11,101,name='One',contract_verified=True),Target(12,101,name='Two',contract_verified=True),
                       Target(13,101,name='Three',contract_verified=True),Target(14,101,status=7,name='Completed',contract_verified=True)])
        snapshot=eligibility_rows(service,'monolith',source.targets,fixture_admission=admitted)
        assert [r['eligible'] for r in snapshot if r['advert_id'] in (11,12,13)]==[True,True,True]
        completed=next(r for r in snapshot if r['advert_id']==14)
        assert not completed['eligible'] and completed['status']=='completed'
        web=CleanerWeb(service,generation='monolith',approved_targets=admitted,batch_catalog_targets=source.targets)
        web.worker_status=lambda:'ready'
        selected=[dict(advert_id=aid,nm_id=101) for aid in (11,12,13)]
        payload=dict(request_id='synthetic-batch-0001',selected_categories=['active'],targets=selected)
        with patch.object(CleanerWbSource,'from_env',return_value=source):
            accepted=web.start_manual_batch(payload,owner)
            assert accepted['state']=='queued' and accepted['selected_count']==3
            # Lost response and stale worker readiness cannot create a second intent.
            web.worker_status=lambda:'down'
            assert web.start_manual_batch(payload,owner)==accepted
        rejects(lambda:service.start_run(dict(request_id='synthetic-batch-overlap-run-0001',advert_id=99,nm_id=101),owner),'manual_batch_active')
        rejects(lambda:service.start_manual_clean(dict(request_id='synthetic-batch-overlap-single-0001',advert_id=11,nm_id=101),owner),'manual_batch_active')
        rejects(lambda:service.start_manual_batch(dict(payload,request_id='synthetic-batch-overlap-parent-0001'),owner,snapshot=snapshot),'manual_batch_active')
        rejects(lambda:service.decide('synthetic-review',dict(request_id='synthetic-batch-overlap-decision-0001',decision='exclude',expected_revision=1),owner),'manual_batch_active')
        rejects(lambda:service.manual_batch_snapshot(accepted['batch_id'],other),'not_found')
        rejects(lambda:batch_status(service,accepted['batch_id'],other),'not_found')
        coordinator=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        assert coordinator.pending_batches()==[accepted['batch_id']]
        rejects(lambda:service.start_manual_clean(dict(request_id=batch_child_id(accepted['batch_id'],1),advert_id=12,nm_id=101),owner,
                                                  batch_id=accepted['batch_id'],batch_index=1),'batch_child_invalid')
        first=coordinator.tick()
        assert first['items'][0]['job_id']==batch_child_id(accepted['batch_id'],0)
        assert first['items'][0]['new_checked'] is None and first['items'][1]['state']=='queued'
        rejects(lambda:service.start_run(dict(request_id='synthetic-batch-overlap-run-0002',advert_id=99,nm_id=101),owner),'manual_batch_active')
        with service.store.read() as c:
            assert c.execute("SELECT state FROM cleaner_runs WHERE account=? AND run_id=?",(service.key,service.manual_job(first['items'][0]['job_id'],owner)['scan_run_id'])).fetchone()['state']=='queued'
        # Process death after child creation: the new coordinator attaches to
        # the exact saved child rather than enqueuing another one.
        coordinator=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        coordinator.tick()
        with service.store.read() as c:
            count=c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-clean'",(service.key,)).fetchone()[0]
        assert count==1
        child=service.manual_job(first['items'][0]['job_id'],owner)
        assert service.stop_unsubmitted_manual_run(child['scan_run_id'])
        service.record_manual_job(child['job_id'],state='no_change',stage='finished',result='Нет новых кандидатов')
        after=coordinator.tick()
        assert after['current_index']==1 and after['no_change_count']==1
        # A formerly active campaign has become paused. Active-only batch
        # must skip that frozen pair, never broaden the selected category.
        source.targets=[Target(11,101,name='One',contract_verified=True),Target(12,101,status=11,name='Two',contract_verified=True),
                        Target(13,101,name='Three',contract_verified=True)]
        after=coordinator.tick()
        assert after['items'][1]['state']=='skipped' and after['items'][1]['error_code']=='selected_status_changed'
        assert after['current_index']==2
        # Third pair is still eligible; its own child starts with a disjoint ID.
        after=coordinator.tick()
        third=after['items'][2]
        assert third['job_id']==batch_child_id(accepted['batch_id'],2)
        assert third['job_id']!=child['job_id']
        run=service.manual_job(third['job_id'],owner)
        assert service.stop_unsubmitted_manual_run(run['scan_run_id'])
        service.record_manual_job(run['job_id'],state='failed',stage='finished',error_code='synthetic_scan_error',error='Synthetic read failure')
        after=coordinator.tick()
        assert after['state']=='partial' and after['failed_count']==1 and after['done_count']==3
        assert not coordinator.pending_batches()
        detail=batch_item_detail(service,accepted['batch_id'],1,owner)
        assert detail['item']['error_code']=='selected_status_changed' and detail['job'] is None
        # Completion does not erase the exact child or permit cross-owner reads.
        assert service.manual_job(child['job_id'],owner)['state']=='no_change'
        rejects(lambda:batch_item_detail(service,accepted['batch_id'],0,other),'not_found')
        # Unsupported category and missing exact SKU never pass selection.
        rejects(lambda:service.start_manual_batch(dict(request_id='synthetic-batch-0002',selected_categories=['completed'],targets=[selected[0]]),owner,snapshot=snapshot),'batch_selection_invalid')
        rejects(lambda:service.start_manual_batch(dict(request_id='synthetic-batch-0003',selected_categories=['active'],targets=[dict(advert_id=14,nm_id=101)]),owner,snapshot=snapshot),'batch_target_ineligible')
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=box.service();owner=Principal('owner',True,True,True)
        admitted=[dict(advert_id=aid,nm_id=101,state='verified') for aid in (11,12)]
        source=Source([Target(11,101,name='One',contract_verified=True),Target(12,101,name='Two',contract_verified=True)])
        snapshot=eligibility_rows(service,'monolith',source.targets,fixture_admission=admitted)
        batch_id='synthetic-batch-recheck-0001'
        payload=dict(request_id=batch_id,selected_categories=['active'],targets=[dict(advert_id=11,nm_id=101),dict(advert_id=12,nm_id=101)])
        service.start_manual_batch(payload,owner,snapshot=snapshot)
        coordinator=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        # Simulate a crash after the durable dispatching cursor, before child
        # creation. Restart can only create the deterministic first child.
        service.record_manual_batch(batch_id,state='running',stage='dispatching',current_index=0)
        coordinator=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        created=coordinator.tick();job_id=created['items'][0]['job_id']
        assert job_id==batch_child_id(batch_id,0)
        service.record_manual_job(job_id,state='partial',stage='scan_apply_claimed',can_recheck=True,
                                  next_readback_at=0,error_code='readback_unresolved')
        paused=coordinator.tick()
        assert paused['state']=='attention_required' and paused['current_index']==0 and paused['done_count']==0
        assert paused['items'][1]['job_id'] is None
        assert coordinator.tick()['state']=='attention_required'
        service.recheck_manual_job(job_id,dict(request_id='synthetic-recheck-0001'),owner)
        class ReadbackOnly:
            pass
        child_worker=ManualCleanerCoordinator(service,ReadbackOnly())
        actions=[]
        def readback_only(action,operation_id,request,**expected):
            actions.append(action)
            assert action=='readback' and operation_id.endswith('-scan')
            return {'state':'applied'}
        child_worker._launch=readback_only
        result=child_worker.tick()
        assert result['stage']=='classifying' and actions==['readback']
        assert service.stop_unsubmitted_manual_run(result['scan_run_id'])
        service.record_manual_job(job_id,state='no_change',stage='finished',can_recheck=False,result='Нет новых кандидатов')
        advanced=coordinator.tick()
        assert advanced['current_index']==1 and advanced['state']=='running'
        second=coordinator.tick()
        assert second['items'][1]['job_id']==batch_child_id(batch_id,1)
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=box.service();owner=Principal('owner',True,True,True)
        batch_id='synthetic-batch-collision-0001'
        preempt=service.start_manual_clean(dict(request_id=batch_child_id(batch_id,0),advert_id=99,nm_id=101),owner)
        assert service.stop_unsubmitted_manual_run(preempt['run_id'])
        service.record_manual_job(preempt['job_id'],state='no_change',stage='finished')
        admitted=[dict(advert_id=11,nm_id=101,state='verified')]
        source=Source([Target(11,101,name='One',contract_verified=True)])
        snapshot=eligibility_rows(service,'monolith',source.targets,fixture_admission=admitted)
        service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],targets=[dict(advert_id=11,nm_id=101)]),owner,snapshot=snapshot)
        coordinator=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        status=coordinator.tick()
        assert status['state']=='failed' and status['items'][0]['error_code']=='batch_child_collision'
        with service.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-clean'",(service.key,)).fetchone()[0]==1
    with Sandbox() as box:
        # Compose the new parent with the unchanged Stage E guarded path for
        # two exact targets. Fake WB observes one submit per child, including
        # after both coordinators are reconstructed from their journals.
        box.package['manual_admission'].append(dict(box.package['manual_admission'][0],advert_id=12))
        characteristics=[dict(id=1,name='Модель',value=['iPhone 16 Pro Max']),dict(id=2,name='Тип',value=['Обычное стекло'])]
        card=dict(nm_id='101',title='Synthetic glass',vendor_code='synthetic',description='Approved synthetic card',characteristics=characteristics)
        raw=json.dumps(dict(cards=[dict(card,card_digest='sha256:'+'1'*64)]),sort_keys=True).encode()
        card_path=box.admission/'card-source-approved.json';card_path.write_bytes(raw);card_path.chmod(0o600)
        box.package['provenance']['fresh_cards_sha256']='sha256:'+hashlib.sha256(raw).hexdigest()
        box.write_package()
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        ChangeRegistryRepository(box.runtime).initialize_schema()
        fake=FakeWB();fake.targets[12]=copy.deepcopy(fake.targets[11]);clock=Clock();clock.base=datetime.now(timezone.utc)+timedelta(seconds=1)
        with fake.server() as url:
            source=CleanerWbSource(account=box.service().account,runtime=OfficialApiRuntimeConfig('synthetic',url,2),fixture=True,
                clock=clock,monotonic=clock.monotonic,limiter=AccountLimiter(monotonic=clock.monotonic,sleep=clock.advance))
            original_cleaner=stage_e.KeywordCleaner
            with patch.object(stage_e.CleanerWbSource,'from_env',return_value=source), \
                 patch.object(stage_e,'fetch_current_card',side_effect=lambda nm_id:dict(card,characteristics=list(reversed(characteristics)))), \
                 patch.object(stage_e,'KeywordCleaner',side_effect=lambda *args,**kwargs:original_cleaner(*args,clock=clock,**kwargs)):
                service=box.service();owner=Principal('owner',True,True,True)
                admitted=[dict(advert_id=aid,nm_id=101,state='verified') for aid in (11,12)]
                catalog=source._adverts([11,12],source.monotonic()+120)
                snapshot=eligibility_rows(service,'monolith',catalog,fixture_admission=admitted)
                command=dict(request_id='synthetic-batch-full-0001',selected_categories=['active'],
                             targets=[dict(advert_id=11,nm_id=101),dict(advert_id=12,nm_id=101)])
                accepted=service.start_manual_batch(command,owner,snapshot=snapshot)
                adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
                child=ManualCleanerCoordinator(service,adapter)
                for _ in range(60):
                    if parent.pending_batches():parent.tick()
                    if child.pending_jobs():child.tick()
                    result=batch_status(service,accepted['batch_id'],owner)
                    if result['state'] in {'complete','partial','failed'}:break
                    active=result['current_target']
                    if active:
                        job=result['items'][result['current_index']]['job_id']
                        if job:
                            saved=service.manual_job(job,owner)
                            if saved.get('next_readback_at',0)>time.time():time.sleep(min(2.1,saved['next_readback_at']-time.time()))
                else:raise AssertionError('composed batch did not finish')
                assert result['state']=='complete',result
                assert [item['state'] for item in result['items']]==['complete','complete'],result
                assert [write['advert_id'] for write in fake.writes]==[11,12],fake.writes
                assert all(item['confirmed_excluded'] and item['pending_count']==0 for item in result['items'])
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
                child=ManualCleanerCoordinator(service,adapter)
                assert not parent.pending_batches() and not child.pending_jobs()
                assert service.start_manual_batch(command,owner)==accepted and len(fake.writes)==2
    print('search cluster cleaner batch smoke: ok')


if __name__=='__main__':main()
