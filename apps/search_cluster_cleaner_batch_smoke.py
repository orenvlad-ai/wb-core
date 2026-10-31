#!/usr/bin/env python3
"""Synthetic durable batch ordering, eligibility, restart and owner isolation."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime,timezone,timedelta
import copy
import hashlib
import json
import sqlite3
import sys
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from apps import search_cluster_cleaner_stage_e as stage_e
from apps.search_cluster_cleaner_write_fixture import FakeWB,Clock
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.search_cluster_cleaner import KeywordCleaner,batch_child_id
from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator,batch_status,batch_item_detail,READ_RETRY_DELAYS
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.application.search_cluster_cleaner_self_service import LocalStageEAdapter,ManualCleanerCoordinator
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource,WbReadError
from packages.adapters.search_cluster_cleaner_wb import AccountLimiter
from packages.adapters.official_api_runtime import OfficialApiRuntimeConfig
from packages.contracts.search_cluster_cleaner import CleanerError,Principal,Target
from packages.domain.search_cluster_sources import union_snapshot


class Source:
    def __init__(self,targets):self.targets=targets;self.calls=[]
    def monotonic(self):return 0.0
    def count_statuses(self,deadline):return {row.advert_id:row.status for row in self.targets}
    def _adverts(self,ids,deadline,*,strict=True):
        self.calls.append(tuple(ids))
        values=[row for row in self.targets if row.advert_id in ids]
        if strict:return values
        missing=['adverts_missing:'+str(i) for i in ids if i not in {row.advert_id for row in values}]
        return values,missing


def ready_service(box):
    service=box.service()
    if not getattr(box,'_batch_registry_ready',False):
        ChangeRegistryRepository(service.store.registry.runtime_dir).initialize_schema()
        box._batch_registry_ready=True
    return service


def rejects(action,code):
    try:action()
    except CleanerError as exc:assert exc.code==code,(exc.code,code)
    else:raise AssertionError('expected '+code)


def technical_deferred_path():
    """One list-only action is logged, while other pairs remain runnable."""
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=ready_service(box);owner=Principal('owner',True,True,True)
        targets=[Target(i,101,name='CPM '+str(i),contract_verified=True) for i in (11,12)]
        admitted=[dict(advert_id=i,nm_id=101,state='verified') for i in (11,12)]
        source=Source(targets)
        frozen=eligibility_rows(service,'monolith',targets,fixture_admission=admitted)
        batch_id='synthetic-deferred-batch-0001'
        service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],
            targets=[dict(advert_id=i,nm_id=101) for i in (11,12)]),owner,snapshot=frozen)
        parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        for index,(active,stats) in enumerate(((['стекло iphone 15 pro max','Стекло iphone 14 pro max','стекло iphone 16 pro max'],['Стекло iphone 14 pro max','стекло iphone 16 pro max']),
                                                (['стекло iphone 15 pro max'],[]))):
            child=service.manual_job(parent.tick()['items'][index]['job_id'],owner)
            run=service.claim_exact_manual_run(run_id=child['scan_run_id'],targets=[targets[index]],
                generation='monolith',production_operation_id='synthetic-deferred-scan-'+str(index))
            now=service.clock();snapshot=union_snapshot(targets[index],list_entry=dict(active=active,excluded=[],archived=[]),
                stats_queries=stats,minus_queries=[],observed_at=now,source_times={name:now for name in ('list','statistics','minus')})
            service.record_snapshot(run['run_id'],run['worker_token'],'monolith',snapshot,manual_only=True)
            ended=service.finish_run(run['run_id'],run['worker_token'],'monolith',manual_only=True)
            assert ended['state']=='complete' and ended['summary']['excluded_not_executed']==1,ended
            if index==0:
                candidate=service.manual_apply_preview(run['run_id'],targets[index])
                assert len(candidate['candidates'])==1 and len(candidate['deferred'])==1,candidate
                service.record_manual_job(child['job_id'],state='complete',stage='finished')
            else:
                rejects(lambda:service.manual_apply_preview(run['run_id'],targets[index]),'manual_candidate_incomplete')
                service.record_manual_job(child['job_id'],state='failed',stage='finished',error_code='manual_candidate_incomplete')
            state=parent.tick()
            assert state['current_index']==index+1 and state['items'][index]['state']=='partial',state
            assert state['items'][index]['deferred_count']==1,state
            assert state['items'][index]['unchanged']==(1 if index==0 else 0),state
        final=parent.tick()
        assert final['state']=='partial' and final['not_started_count']==0 and final['partial_count']==2,final


def incomplete_scan_does_not_abort_other_pair():
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=ready_service(box);owner=Principal('owner',True,True,True)
        targets=[Target(i,101,name='CPM '+str(i),contract_verified=True) for i in (11,12)]
        admitted=[dict(advert_id=i,nm_id=101,state='verified') for i in (11,12)]
        batch_id='synthetic-incomplete-scan-0001'
        frozen=eligibility_rows(service,'monolith',targets,fixture_admission=admitted)
        service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],
            targets=[dict(advert_id=i,nm_id=101) for i in (11,12)]),owner,snapshot=frozen)
        parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:Source(targets),fixture_admission=admitted)
        for index in range(2):
            child=service.manual_job(parent.tick()['items'][index]['job_id'],owner)
            run=service.claim_exact_manual_run(run_id=child['scan_run_id'],targets=[targets[index]],
                generation='monolith',production_operation_id='synthetic-incomplete-scan-'+str(index))
            now=service.clock()
            snapshot=union_snapshot(targets[index],
                list_entry=(None if index==0 else dict(active=['стекло iphone 16 pro max'],excluded=[],archived=[])),
                stats_queries=[] if index==0 else ['стекло iphone 16 pro max'],minus_queries=[],observed_at=now,
                source_times={} if index==0 else {name:now for name in ('list','statistics','minus')})
            service.record_snapshot(run['run_id'],run['worker_token'],'monolith',snapshot,manual_only=True)
            ended=service.finish_run(run['run_id'],run['worker_token'],'monolith',manual_only=True)
            service.record_manual_job(child['job_id'],state='failed' if index==0 else 'no_change',stage='finished',
                                      error_code='scan_partial' if index==0 else None)
            state=parent.tick()
            assert state['current_index']==index+1,state
            assert state['items'][index]['state']==('partial' if index==0 else 'no_change'),state
            assert ended['state']==('partial' if index==0 else 'complete'),ended
        final=parent.tick()
        assert final['state']=='partial' and final['partial_count']==1 and final['no_change_count']==1,final
        assert final['not_started_count']==0,final


def drift_scan_admission():
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=ready_service(box);owner=Principal('owner',True,True,True)
        targets=[Target(i,101,contract_verified=True) for i in (11,12)]
        admitted=[dict(advert_id=i,nm_id=101,state='verified') for i in (11,12)]
        with service.store.transaction() as db:
            for target,reason in zip(targets,('external_state_drift','technical_uncertainty')):
                db.execute('INSERT INTO cleaner_target_holds VALUES(?,?,?,?)',(service.key,target.key,reason,service.clock()))
        rows=eligibility_rows(service,'monolith',targets,fixture_admission=admitted)
        assert rows[0]['eligible'] and not rows[1]['eligible'] and rows[1]['reason']=='target_held',rows
        rejects(lambda:service.start_manual_clean(dict(request_id='held-technical-start',advert_id=12,nm_id=101),owner),'target_held')
        job=service.start_manual_clean(dict(request_id='held-drift-rescan',advert_id=11,nm_id=101),owner)
        assert job['run_id']
        with service.store.read() as db:
            assert db.execute('SELECT count(*) FROM cleaner_target_holds').fetchone()[0]==2
            assert db.execute('SELECT count(*) FROM cleaner_write_operations').fetchone()[0]==0


def all_card_drift_is_failed():
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=ready_service(box);owner=Principal('owner',True,True,True)
        targets=[Target(i,101,name='CPM '+str(i),contract_verified=True) for i in (11,12)]
        admitted=[dict(advert_id=i,nm_id=101,state='verified') for i in (11,12)]
        frozen=eligibility_rows(service,'monolith',targets,fixture_admission=admitted)
        batch_id='synthetic-all-card-drift-0001'
        service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],
            targets=[dict(advert_id=i,nm_id=101) for i in (11,12)]),owner,snapshot=frozen)
        parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:Source(targets),fixture_admission=admitted)
        for index in range(2):
            child=service.manual_job(parent.tick()['items'][index]['job_id'],owner)
            assert service.stop_unsubmitted_manual_run(child['scan_run_id'])
            service.record_manual_job(child['job_id'],state='failed',stage='finished',
                                      error_code='current_card_drift',error='current_card_drift')
            result=parent.tick()
            assert result['items'][index]['error_code']=='current_card_drift',result
        final=parent.tick()
        assert final['state']=='failed' and final['error_code']=='current_card_drift',final
        assert final['partial_count']==2 and final['completed_count']==0


def archived_rejection_continues_batch():
    with Sandbox() as box:
        box.package['manual_admission'].append(dict(box.package['manual_admission'][0],advert_id=12))
        characteristics=[dict(id=746,name='Совместимость',value=['Apple','iPhone 16 Pro Max']),
                         dict(id=12223252,name='Производитель телефона',value=['Apple']),
                         dict(id=195594,name='Цвет рамки',value=['черный'])]
        card=dict(nm_id='101',title='Защитное стекло iPhone 16 Pro Max',vendor_code='(Clean) iPhone 16 Pro Max',
                  description='Защитное стекло для телефона',characteristics=characteristics)
        raw=json.dumps(dict(cards=[dict(card,card_digest='sha256:'+'1'*64)]),sort_keys=True).encode()
        path=box.admission/'card-source-approved.json';path.write_bytes(raw);path.chmod(0o600)
        box.package['provenance']['fresh_cards_sha256']='sha256:'+hashlib.sha256(raw).hexdigest();box.write_package()
        preview=box.execute('preview');box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        ChangeRegistryRepository(box.runtime).initialize_schema()
        fake=FakeWB();fake.targets[12]=copy.deepcopy(fake.targets[11]);fake.write_modes[11]='400_archived'
        before=list(fake.targets[11]['minus']);clock=Clock();clock.base=datetime.now(timezone.utc)+timedelta(seconds=1)
        with fake.server() as url:
            source=CleanerWbSource(account=ready_service(box).account,runtime=OfficialApiRuntimeConfig('synthetic',url,2),fixture=True,
                clock=clock,monotonic=clock.monotonic,limiter=AccountLimiter(monotonic=clock.monotonic,sleep=clock.advance))
            original=stage_e.KeywordCleaner
            with patch.object(stage_e.CleanerWbSource,'from_env',return_value=source), \
                 patch.object(stage_e,'fetch_current_card',side_effect=lambda nm_id:dict(card,subject_id=1571)), \
                 patch.object(stage_e,'KeywordCleaner',side_effect=lambda *a,**kw:original(*a,clock=clock,**kw)):
                service=ready_service(box);owner=Principal('owner',True,True,True)
                admitted=[dict(advert_id=i,nm_id=101,state='verified') for i in (11,12)]
                snapshot=eligibility_rows(service,'monolith',source._adverts([11,12],source.monotonic()+120),fixture_admission=admitted)
                batch_id='synthetic-archived-rejection-0001'
                service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],targets=[dict(advert_id=i,nm_id=101) for i in (11,12)]),owner,snapshot=snapshot)
                adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                for _ in range(60):
                    parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
                    child=ManualCleanerCoordinator(service,adapter)
                    if parent.pending_batches():parent.tick()
                    if child.pending_jobs():child.tick()
                    result=batch_status(service,batch_id,owner)
                    if result['state'] in {'complete','partial','failed'}:break
                else:raise AssertionError('archived rejection batch did not finish')
                assert result['state']=='partial' and result['completed_count']==1 and result['not_started_count']==0,result
                assert result['items'][0]['review_required'] and result['items'][0]['error_code']=='known_validation_rejected',result
                assert result['items'][1]['state']=='complete',result
                assert fake.targets[11]['minus']==before and [w['advert_id'] for w in fake.writes]==[11,12]
                from packages.application.search_cluster_cleaner_batch import _rejected_write_only
                rejected=service.manual_job(batch_child_id(batch_id,0),owner)
                target=dict(advert_id=11,nm_id=101)
                with service.store.read() as c:assert _rejected_write_only(c,service,rejected,target)
                # Negative evidence must not require violating the production
                # trigger that forbids a rejected operation regaining dispatch.
                class ChangedEvidence:
                    def __init__(self,c,state):self.c,self.state=c,state
                    def execute(self,sql,args):
                        rows=self.c.execute(sql,args)
                        if sql.startswith('SELECT * FROM cleaner_write_operations'):
                            values=[dict(r,state=self.state) for r in rows]
                            class Result:
                                def fetchall(self):return values
                            return Result()
                        return rows
                with service.store.read() as c:
                    for state in ('requires_review','confirmed','http_error','prepared'):
                        assert not _rejected_write_only(ChangedEvidence(c,state),service,rejected,target)
                    assert not _rejected_write_only(c,service,dict(rejected,can_recheck=True),target)
                assert not parent.pending_batches() and len(fake.writes)==2


def main():
    archived_rejection_continues_batch()
    all_card_drift_is_failed()
    drift_scan_admission()
    technical_deferred_path()
    incomplete_scan_does_not_abort_other_pair()
    # The count endpoint can include completed campaigns omitted by the detail
    # endpoint. Their absence must not hide exact active/paused candidates.
    with Sandbox() as box:
        source=object.__new__(CleanerWbSource)
        source.monotonic=time.monotonic
        count={'all':4,'adverts':[{'status':status,'type':8,'count':1,'advert_list':[{'advertId':advert}]} for advert,status in ((11,9),(12,11),(14,7),(15,-1))]}
        def advert(advert_id,status):
            return dict(id=advert_id,settings=dict(payment_type='cpm',name='Campaign '+str(advert_id)),bid_type='manual',status=status,nm_settings=[dict(nm_id=101)])
        detail_ids={11,12}
        def read(method,path,*,deadline):
            if path.endswith('/promotion/count'):return count
            if detail_ids is None:raise WbReadError('rate_limited')
            return {'adverts':[advert(aid,status) for aid,status in ((11,9),(12,11),(14,7),(15,-1)) if aid in detail_ids]}
        source._call=read
        targets,errors,statuses=source.catalog(with_statuses=True)
        assert {target.advert_id for target in targets}=={11,12}
        assert errors==['adverts_missing:14','adverts_missing:15'] and statuses=={11:9,12:11,14:7,15:-1}
        assert len(source.catalog())==2,'default catalog contract changed'
        web=CleanerWeb(ready_service(box),generation='monolith')
        owner=Principal('owner',True,True,True)
        with patch.object(CleanerWbSource,'from_env',return_value=source), \
             patch('packages.application.search_cluster_cleaner_batch_eligibility.eligibility_rows',return_value=[
                 dict(advert_id=11,nm_id=101,eligible=True,status='active',campaign_name='One',reason=None),
                 dict(advert_id=12,nm_id=101,eligible=True,status='paused',campaign_name='Two',reason=None)]):
            web._refresh_batch_catalog()
            result=web.batch_eligibility(owner)
            assert result['error'] is None and {row['advert_id'] for row in result['items']}=={11,12}
            detail_ids={12}
            web._refresh_batch_catalog()
            result=web.batch_eligibility(owner)
            assert result['error']=='campaign_catalog_unavailable' and result['items']==[]
            detail_ids={11}
            web._refresh_batch_catalog()
            assert web.batch_eligibility(owner)['error']=='campaign_catalog_unavailable'
            detail_ids=None
            web._refresh_batch_catalog()
            assert web.batch_eligibility(owner)['error']=='campaign_catalog_unavailable'
    with Sandbox() as box:
        web=CleanerWeb(ready_service(box),generation='monolith')
        owner=Principal('owner',True,True,True)
        with patch.object(CleanerWbSource,'from_env',side_effect=RuntimeError('synthetic catalog outage')) as source:
            assert web.batch_eligibility(owner)['loading']
            for _ in range(100):
                result=web.batch_eligibility(owner)
                if not result['loading']:break
                time.sleep(0.01)
            assert result['error']=='campaign_catalog_unavailable' and not result['items'],result
            assert web._batch_catalog_at>0 and source.call_count==1
            for _ in range(3):
                assert web.batch_eligibility(owner)['error']=='campaign_catalog_unavailable'
            assert source.call_count==1,'failed catalog was retried on every poll'
            assert web.batch_eligibility(owner,refresh=True)['loading']
            for _ in range(100):
                result=web.batch_eligibility(owner)
                if not result['loading']:break
                time.sleep(0.01)
            assert result['error']=='campaign_catalog_unavailable' and source.call_count==2
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=ready_service(box);owner=Principal('owner',True,True,True);other=Principal('other',True,True,True)
        admitted=[dict(advert_id=aid,nm_id=101,state='verified') for aid in (11,12,13)]
        source=Source([Target(11,101,name='One',contract_verified=True),Target(12,101,name='Two',contract_verified=True),
                       Target(13,101,name='Three',contract_verified=True),Target(14,101,status=7,name='Completed',contract_verified=True)])
        snapshot=eligibility_rows(service,'monolith',source.targets,fixture_admission=admitted)
        assert [r['eligible'] for r in snapshot if r['advert_id'] in (11,12,13)]==[True,True,True]
        completed=next(r for r in snapshot if r['advert_id']==14)
        assert not completed['eligible'] and completed['status']=='completed'
        omitted=eligibility_rows(service,'monolith',source.targets[:2],fixture_admission=admitted+[dict(advert_id=14,nm_id=101,state='verified')])
        assert not any(r['advert_id']==14 for r in omitted),'unknown payment type is never counted as CPM'
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
        assert after['state']=='running' and after['items'][2]['state']=='partial'
        after=coordinator.tick()
        assert after['state']=='partial' and after['partial_count']==1 and after['done_count']==3
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
        service=ready_service(box);owner=Principal('owner',True,True,True)
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
        service=ready_service(box);owner=Principal('owner',True,True,True)
        admitted=[dict(advert_id=aid,nm_id=101,state='verified') for aid in (11,12)]
        source=Source([Target(11,101,name='One',contract_verified=True),Target(12,101,name='Two',contract_verified=True)])
        snapshot=eligibility_rows(service,'monolith',source.targets,fixture_admission=admitted)
        batch_id='synthetic-batch-local-retry-0001'
        service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],
                                        targets=[dict(advert_id=11,nm_id=101),dict(advert_id=12,nm_id=101)]),owner,snapshot=snapshot)
        parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        job_id=parent.tick()['items'][0]['job_id']
        child=ManualCleanerCoordinator(service,object())
        job=service.manual_job(job_id,owner)
        operation=child.operation_id(job_id,'scan')
        target=Target(11,101)
        service.claim_exact_manual_run(run_id=job['scan_run_id'],targets=[target],generation='monolith',
            production_operation_id=operation,lease_seconds=1)
        assert not service.stop_unsubmitted_manual_run(job['scan_run_id'],operation_id=operation,target=target)
        # Reproduce a committed binding followed by native unsent recovery.
        from packages.application.search_cluster_cleaner_admission import AdmissionGuard
        service.clock=lambda:(datetime.now(timezone.utc)+timedelta(seconds=5)).isoformat()
        # A recovery event for another operation cannot certify this binding.
        with service.store.transaction() as c:
            service._event(c,'stage_e_unsent_recovered',dict(production_operation_id='different-operation',
                cancelled_operations=[],kind='scan'),run_id=job['scan_run_id'])
        assert not service.exact_manual_run_unclaimed(job['scan_run_id'],operation,target)
        AdmissionGuard(box.admission,service.store).recover_unsubmitted_manual_run(cleaner=service,
            generation='monolith',production_operation_id=operation,run_id=job['scan_run_id'])
        assert not service.stop_unsubmitted_manual_run(job['scan_run_id'])
        assert not service.stop_unsubmitted_manual_run(job['scan_run_id'],operation_id='wrong',target=target)
        assert not service.stop_unsubmitted_manual_run(job['scan_run_id'],operation_id=operation,target=Target(12,101))
        service.record_manual_job(job_id,state='running',stage='scan_apply_claimed')
        assert child._retry_unclaimed(job_id,'scan',{'state':'not_submitted'})
        parked=service.manual_job(job_id,owner)
        assert parked['state']=='failed' and parked['error_code']=='local_not_submitted_retry'
        assert not child.pending_jobs()
        deferred=parent.tick()
        assert deferred['items'][0]['state']=='retry_wait' and deferred['items'][1]['job_id'] is None
        assert deferred['current_index']==1 and deferred['waiting_count']==1
        # A second child can be terminal while its parent is already partial.
        second=parent.tick()['items'][1]['job_id']
        saved=service.manual_job(second,owner)
        operation=child.operation_id(second,'scan')
        service.claim_exact_manual_run(run_id=saved['scan_run_id'],targets=[Target(12,101)],generation='monolith',
            production_operation_id=operation,lease_seconds=1)
        service.clock=lambda:(datetime.now(timezone.utc)+timedelta(seconds=15)).isoformat()
        AdmissionGuard(box.admission,service.store).recover_unsubmitted_manual_run(cleaner=service,
            generation='monolith',production_operation_id=operation,run_id=saved['scan_run_id'])
        assert child._retry_unclaimed(second,'scan',{'state':'not_submitted'})
        service.record_manual_batch(batch_id,state='partial',stage='finished')
        restarted=ManualCleanerCoordinator(service,object())
        assert not restarted.pending_jobs() and not parent.pending_batches()
        assert restarted.release_terminal_unsent_scans()==1
        assert restarted.release_terminal_unsent_scans()==0
        assert service.manual_batch_snapshot(batch_id,owner)['state']=='partial'
        # A user may adopt the queued run between discovery and recovery.
        batch2='synthetic-orphan-adopted-batch'
        service.start_manual_batch(dict(request_id=batch2,selected_categories=['active'],
            targets=[dict(advert_id=11,nm_id=101)]),owner,snapshot=snapshot)
        third=parent.tick()['items'][0]['job_id']
        saved=service.manual_job(third,owner)
        service.record_manual_job(third,state='failed',stage='finished',error_code='local_not_submitted_retry')
        service.record_manual_batch(batch2,state='partial',stage='finished')
        proof=service.exact_manual_run_unclaimed
        def racing_adoption(*args):
            result=proof(*args)
            accepted=service.start_manual_clean(dict(request_id='synthetic-orphan-new-adopter',advert_id=11,nm_id=101),owner)
            assert accepted['adopted'] and accepted['run_id']==saved['scan_run_id']
            return result
        with patch.object(service,'exact_manual_run_unclaimed',side_effect=racing_adoption):
            assert restarted.release_terminal_unsent_scans()==0
        with service.store.read() as c:
            assert c.execute('SELECT state FROM cleaner_runs WHERE run_id=?',(saved['scan_run_id'],)).fetchone()[0]=='queued'
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=ready_service(box);owner=Principal('owner',True,True,True)
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
        characteristics=[dict(id=746,name='Совместимость',value=['Apple','iPhone 16 Pro Max']),
                         dict(id=12223252,name='Производитель телефона',value=['Apple']),
                         dict(id=195594,name='Цвет рамки',value=['черный'])]
        card=dict(nm_id='101',title='Защитное стекло iPhone 16 Pro Max',
                  vendor_code='(Clean) iPhone 16 Pro Max',description='Защитное стекло для телефона',characteristics=characteristics)
        raw=json.dumps(dict(cards=[dict(card,card_digest='sha256:'+'1'*64)]),sort_keys=True).encode()
        card_path=box.admission/'card-source-approved.json';card_path.write_bytes(raw);card_path.chmod(0o600)
        box.package['provenance']['fresh_cards_sha256']='sha256:'+hashlib.sha256(raw).hexdigest()
        box.write_package()
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        ChangeRegistryRepository(box.runtime).initialize_schema()
        fake=FakeWB();fake.targets[12]=copy.deepcopy(fake.targets[11]);fake.write_modes[11]='timeout'
        clock=Clock();clock.base=datetime.now(timezone.utc)+timedelta(seconds=1)
        with fake.server() as url:
            source=CleanerWbSource(account=ready_service(box).account,runtime=OfficialApiRuntimeConfig('synthetic',url,2),fixture=True,
                clock=clock,monotonic=clock.monotonic,limiter=AccountLimiter(monotonic=clock.monotonic,sleep=clock.advance))
            original_cleaner=stage_e.KeywordCleaner
            with patch.object(stage_e.CleanerWbSource,'from_env',return_value=source), \
                 patch.object(stage_e,'fetch_current_card',side_effect=lambda nm_id:dict(card,subject_id=1571,characteristics=list(reversed(characteristics)))), \
                 patch.object(stage_e,'KeywordCleaner',side_effect=lambda *args,**kwargs:original_cleaner(*args,clock=clock,**kwargs)):
                service=ready_service(box);owner=Principal('owner',True,True,True)
                admitted=[dict(advert_id=aid,nm_id=101,state='verified') for aid in (11,12)]
                catalog=source._adverts([11,12],source.monotonic()+120)
                snapshot=eligibility_rows(service,'monolith',catalog,fixture_admission=admitted)
                command=dict(request_id='synthetic-batch-full-0001',selected_categories=['active'],
                             targets=[dict(advert_id=11,nm_id=101),dict(advert_id=12,nm_id=101)])
                accepted=service.start_manual_batch(command,owner,snapshot=snapshot)
                adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                retry_time=[time.time()]
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted,now=lambda:retry_time[0])
                child=ManualCleanerCoordinator(service,adapter)
                # A separate long-lived reader permits BEGIN IMMEDIATE but
                # makes the exact run+binding COMMIT return BUSY. The rollback
                # proves that no WB dispatch right was ever acquired.
                original_claim=KeywordCleaner.claim_exact_manual_run
                claims=[]
                def locked_claim(self,**kwargs):
                    if claims:return original_claim(self,**kwargs)
                    claims.append(kwargs['run_id'])
                    holder=sqlite3.connect(box.runtime/'registry_upload_runtime.sqlite3')
                    holder.execute('BEGIN')
                    holder.execute('SELECT count(*) FROM cleaner_settings').fetchone()
                    try:return original_claim(self,**kwargs)
                    finally:holder.rollback();holder.close()
                parent.tick()  # Persist first child before the injected lock.
                child.tick()   # Fresh preview is durable; no WB submit.
                before_rollback=service.manual_job(batch_child_id(accepted['batch_id'],0),owner)
                with patch.object(KeywordCleaner,'claim_exact_manual_run',locked_claim):
                    parked=child.tick()
                assert parked['state']=='failed' and parked['error_code']=='local_not_submitted_retry',parked
                claimed=service.manual_job(before_rollback['job_id'],owner)
                assert claimed['stage']=='finished' and not claimed.get('local_retry_attempts'),claimed
                assert service.exact_manual_run_unclaimed(claimed['scan_run_id'],child.operation_id(claimed['job_id'],'scan'),Target(11,101))
                assert not fake.writes
                assert parent.tick()['items'][0]['state']=='retry_wait'
                # Restart while the child is parked. The parent processes the
                # second pair before rearming the same first child identity.
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted,now=lambda:retry_time[0])
                child=ManualCleanerCoordinator(service,adapter)
                fake.targets[11]['minus'].append('Existing WB exclusion changed during lock')
                for _ in range(60):
                    if parent.pending_batches():parent.tick()
                    if child.pending_jobs():child.tick()
                    result=batch_status(service,accepted['batch_id'],owner)
                    if result['state'] in {'complete','partial','failed'}:break
                    if result['state']=='retry_wait':retry_time[0]+=READ_RETRY_DELAYS[0]
                    active=result['current_target']
                    if active:
                        job=result['items'][result['current_index']]['job_id']
                        if job:
                            saved=service.manual_job(job,owner)
                            if saved.get('next_readback_at',0)>time.time():time.sleep(min(2.1,saved['next_readback_at']-time.time()))
                else:raise AssertionError('composed batch did not finish')
                assert result['state']=='complete',result
                assert [item['state'] for item in result['items']]==['complete','complete'],result
                assert [write['advert_id'] for write in fake.writes]==[12,11],fake.writes
                assert all(item['confirmed_excluded'] and item['pending_count']==0 for item in result['items'])
                assert service.manual_job(claimed['job_id'],owner)['scan_run_id']!=claimed['scan_run_id']
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
                child=ManualCleanerCoordinator(service,adapter)
                assert not parent.pending_batches() and not child.pending_jobs()
                assert service.start_manual_batch(command,owner)==accepted and len(fake.writes)==2
    print('search cluster cleaner batch smoke: ok')


if __name__=='__main__':main()
