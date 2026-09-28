#!/usr/bin/env python3
"""Production-shaped held manual batch recovery, with no production or WB writes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps import search_cluster_cleaner_stage_e as stage_e
from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from apps.search_cluster_cleaner_write_fixture import AccountLimiter, Clock, FakeWB
from packages.adapters.official_api_runtime import OfficialApiRuntimeConfig
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.search_cluster_cleaner import KeywordCleaner, batch_child_id
from packages.application.search_cluster_cleaner_admission import AdmissionGuard
from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator, batch_status
from packages.application.search_cluster_cleaner_batch import READ_RETRY_DELAYS, READ_RETRY_POLL_GRACE
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.application.search_cluster_cleaner_self_service import LocalStageEAdapter, ManualCleanerCoordinator
from packages.application.search_cluster_cleaner_store import CleanerTransactionRolledBack
from packages.contracts.search_cluster_cleaner import CleanerError, Principal, Target


def held_commit(database: Path, call):
    """An actual external reader permits BEGIN IMMEDIATE, then blocks COMMIT."""
    reader=sqlite3.connect(database, isolation_level=None)
    reader.execute('BEGIN')
    reader.execute('SELECT count(*) FROM cleaner_settings').fetchone()
    try:
        try:call()
        except CleanerTransactionRolledBack as exc:
            assert exc.phase=='commit_rolled_back', exc
            raise
        else:raise AssertionError('external reader did not roll back COMMIT')
    finally:
        reader.rollback();reader.close()


def prepared_fixture(box: Sandbox, *, extra_adverts=(12,)):
    for advert_id in extra_adverts:
        box.package['manual_admission'].append(dict(box.package['manual_admission'][0], advert_id=advert_id))
    card=dict(nm_id='101',title='Synthetic glass',vendor_code='synthetic',
              description='Approved synthetic card',characteristics=[
                  dict(id=1,name='Модель',value=['iPhone 16 Pro Max']),
                  dict(id=2,name='Тип',value=['Обычное стекло'])])
    raw=json.dumps(dict(cards=[dict(card,card_digest='sha256:'+'1'*64)]),sort_keys=True).encode()
    path=box.admission/'card-source-approved.json';path.write_bytes(raw);path.chmod(0o600)
    box.package['provenance']['fresh_cards_sha256']='sha256:'+hashlib.sha256(raw).hexdigest()
    box.write_package()
    preview=box.execute('preview')
    box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
    ChangeRegistryRepository(box.runtime).initialize_schema()
    return card



def scan_finish_cases():
    for partial in (False, True):
        with Sandbox() as box:
            card=prepared_fixture(box)
            fake=FakeWB();clock=Clock();clock.base=datetime.now(timezone.utc)+timedelta(seconds=1)
            with fake.server() as url:
                source=CleanerWbSource(account=box.service().account,
                    runtime=OfficialApiRuntimeConfig('synthetic',url,2),fixture=True,
                    clock=clock,monotonic=clock.monotonic,
                    limiter=AccountLimiter(monotonic=clock.monotonic,sleep=clock.advance))
                original_cleaner=stage_e.KeywordCleaner
                with patch.object(stage_e.CleanerWbSource,'from_env',return_value=source), \
                     patch.object(stage_e,'fetch_current_card',side_effect=lambda nm_id:dict(card)), \
                     patch.object(stage_e,'KeywordCleaner',side_effect=lambda *a,**kw:original_cleaner(*a,clock=clock,**kw)):
                    service=box.service();owner=Principal('owner',True,True,True)
                    job_id='synthetic-scan-finish-'+('partial' if partial else 'complete')
                    job=service.start_manual_clean(dict(request_id=job_id,advert_id=11,nm_id=101),owner)
                    adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                    child=ManualCleanerCoordinator(service,adapter)
                    if partial:
                        claimed=service.claim_exact_manual_run(run_id=job['run_id'],targets=[Target(11,101)],
                            generation='monolith',production_operation_id=child.operation_id(job_id,'scan'))
                        service.record_target_error(job['run_id'],claimed['worker_token'],'monolith',
                                                    Target(11,101),'synthetic_incomplete')
                    else:
                        assert child.tick()['stage']=='scan_ready'
                        original_finish=KeywordCleaner.finish_run
                        hit=[]
                        def blocked_finish(self,run_id,*args,**kwargs):
                            with self.store.read() as c:
                                kind=c.execute('SELECT kind FROM cleaner_runs WHERE run_id=?',(run_id,)).fetchone()['kind']
                            if kind=='scan' and not hit:
                                hit.append(True)
                                return held_commit(box.runtime/'registry_upload_runtime.sqlite3',
                                                   lambda:original_finish(self,run_id,*args,**kwargs))
                            return original_finish(self,run_id,*args,**kwargs)
                        with patch.object(KeywordCleaner,'finish_run',blocked_finish):child.tick()
                        assert hit
                    with service.store.read() as c:
                        target=c.execute('SELECT state,complete FROM cleaner_run_targets WHERE run_id=?',(job['run_id'],)).fetchone()
                        run=c.execute('SELECT state FROM cleaner_runs WHERE run_id=?',(job['run_id'],)).fetchone()
                    assert run['state']=='running' and target['state']==('partial' if partial else 'done')
                    clock.advance(200)
                    receipt=stage_e.execute(dict(action='readback',operation_id=child.operation_id(job_id,'scan'),
                        request=dict(mode='manual',run_id=job['run_id'],targets=[dict(advert_id=11,nm_id=101)]),
                        expected_runtime_sha=adapter.runtime_sha),runtime_dir=box.runtime,env_file=box.env,
                        admission_dir=box.admission)
                    assert receipt['state']==('failed' if partial else 'no_change'),receipt
                    detail=service.run_detail(job['run_id'],owner)
                    assert detail['state']==('partial' if partial else 'complete')
                    if not partial:
                        summary=detail['summary']
                        assert summary['pairs']==1 and summary['new_checked']>0 and summary['dry_run']
                        assert summary['unresolved_operations']==0 and summary['confirmed_manual']==0
                    assert not fake.writes


def return_only_finish_case():
    """A rolled-back final COMMIT must not count a confirmed 1→0 as an addition."""
    with Sandbox() as box:
        card=prepared_fixture(box)
        fake=FakeWB()
        fake.targets[11]['stats']=[]
        fake.targets[11]['minus']=['стекло iphone 16 pro max']
        clock=Clock();clock.base=datetime.now(timezone.utc)+timedelta(seconds=1)
        with fake.server() as url:
            source=CleanerWbSource(account=box.service().account,
                runtime=OfficialApiRuntimeConfig('synthetic',url,2),fixture=True,
                clock=clock,monotonic=clock.monotonic,
                limiter=AccountLimiter(monotonic=clock.monotonic,sleep=clock.advance))
            original_cleaner=stage_e.KeywordCleaner
            with patch.object(stage_e.CleanerWbSource,'from_env',return_value=source), \
                 patch.object(stage_e,'fetch_current_card',side_effect=lambda nm_id:dict(card)), \
                 patch.object(stage_e,'KeywordCleaner',side_effect=lambda *a,**kw:original_cleaner(*a,clock=clock,**kw)):
                service=box.service();owner=Principal('owner',True,True,True)
                job_id='synthetic-return-finish-0001'
                job=service.start_manual_clean(dict(request_id=job_id,advert_id=11,nm_id=101),owner)
                adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                child=ManualCleanerCoordinator(service,adapter)
                for _ in range(16):
                    current=service.manual_job(job_id,owner)
                    if current['stage']=='write_ready':break
                    child.tick()
                else:raise AssertionError('return-only manual run did not reach write preview')
                write_run=current['write_run_id']
                original_finish=KeywordCleaner.finish_run
                blocked=[]
                def blocked_finish(self,run_id,*args,**kwargs):
                    if run_id==write_run and not blocked:
                        blocked.append(True)
                        return held_commit(box.runtime/'registry_upload_runtime.sqlite3',
                                           lambda:original_finish(self,run_id,*args,**kwargs))
                    return original_finish(self,run_id,*args,**kwargs)
                with patch.object(KeywordCleaner,'finish_run',blocked_finish):child.tick()
                assert blocked and fake.writes==[dict(advert_id=11,nm_id=101,norm_queries=[])],fake.writes
                with service.store.read() as c:
                    run=c.execute('SELECT state FROM cleaner_runs WHERE run_id=?',(write_run,)).fetchone()
                    op=c.execute('SELECT state,dispatch_count FROM cleaner_write_operations WHERE run_id=?',(write_run,)).fetchone()
                    assert run['state']=='complete' and op['state']=='confirmed' and op['dispatch_count']==1,(dict(run),dict(op))
                receipt=stage_e.execute(dict(action='readback',operation_id=child.operation_id(job_id,'write'),
                    request=dict(mode='manual',run_id=write_run,targets=[dict(advert_id=11,nm_id=101)]),
                    expected_runtime_sha=adapter.runtime_sha),runtime_dir=box.runtime,env_file=box.env,
                    admission_dir=box.admission)
                assert receipt['state']=='applied',receipt
                detail=service.run_detail(write_run,owner)
                assert detail['summary']['recovered_manual'] and detail['summary']['returned']==1,detail
                assert detail['summary']['confirmed_pilot']==0,detail
                assert detail['phrases'][0]['action']=='return' and detail['phrases'][0]['confirmed_state']=='allowed'
                assert len(fake.writes)==1


def batch_write_deadlines():
    for uncertain_submit in (False,True):
        with Sandbox() as box:
            card=prepared_fixture(box)
            fake=FakeWB()
            if uncertain_submit:
                fake.write_modes[11]='500'
                fake.targets[12]=copy.deepcopy(fake.targets[11])
            else:fake.codes['/adv/v0/normquery/stats']=504
            clock=Clock();clock.base=datetime.now(timezone.utc)+timedelta(seconds=1)
            with fake.server() as url:
                source=CleanerWbSource(account=box.service().account,
                    runtime=OfficialApiRuntimeConfig('synthetic',url,2),fixture=True,
                    clock=clock,monotonic=clock.monotonic,
                    limiter=AccountLimiter(monotonic=clock.monotonic,sleep=clock.advance))
                original_cleaner=stage_e.KeywordCleaner
                with patch.object(stage_e.CleanerWbSource,'from_env',return_value=source), \
                     patch.object(stage_e,'fetch_current_card',side_effect=lambda nm_id:dict(card)), \
                     patch.object(stage_e,'KeywordCleaner',side_effect=lambda *a,**kw:original_cleaner(*a,clock=clock,**kw)):
                    service=box.service();owner=Principal('owner',True,True,True)
                    ids=[11,12] if uncertain_submit else [11]
                    admitted=[dict(advert_id=aid,nm_id=101,state='verified') for aid in ids]
                    catalog=source._adverts(ids,source.monotonic()+120)
                    snapshot=eligibility_rows(service,'monolith',catalog,fixture_admission=admitted)
                    batch_id='synthetic-write-deadline-'+str(int(uncertain_submit))
                    service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],
                        targets=[dict(advert_id=aid,nm_id=101) for aid in ids]),owner,snapshot=snapshot)
                    retry_time=[time.time()]
                    parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                                                   fixture_admission=admitted,now=lambda:retry_time[0])
                    adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                    child=ManualCleanerCoordinator(service,adapter)
                    parent.tick();job_id=batch_child_id(batch_id,0)
                    if not uncertain_submit:
                        for _ in range(6):
                            failed=child.tick()
                            if failed['state']=='failed':break
                        else:raise AssertionError('initial WB read did not fail')
                        read_wait=parent.tick()
                        assert read_wait['items'][0]['state']=='retry_wait',read_wait
                        assert service.manual_batch_snapshot(batch_id,owner)['read_retry_attempts']=={'0':1}
                        parent.tick()
                        fake.codes.clear()
                        retry_time[0]+=READ_RETRY_DELAYS[0]
                        assert parent.tick()['current_index']==0
                    for _ in range(20):
                        current=service.manual_job(job_id,owner)
                        if current['stage']=='write_ready':break
                        child.tick()
                    else:raise AssertionError('write preview not reached')
                    write_run=current['write_run_id']
                    if uncertain_submit:
                        result=child.tick()
                        assert result['stage']=='write_apply_claimed' and result['state']=='ambiguous',result
                        assert len(fake.writes)==1
                        with service.store.read() as c:
                            op=c.execute('SELECT dispatch_count FROM cleaner_write_operations WHERE run_id=?',(write_run,)).fetchone()
                            assert op and op['dispatch_count']==1
                            first=c.execute("SELECT created_at FROM cleaner_events WHERE account=? AND kind='self_service_stage' AND json_extract(facts,'$.job_id')=? AND json_extract(facts,'$.stage')='write_apply_claimed' ORDER BY sequence LIMIT 1",
                                            (service.key,job_id)).fetchone()
                        # Mimic a pre-upgrade claimed job without either new
                        # timestamp. Repeated local rollbacks must not reset
                        # the deadline or instantly expire from epoch zero.
                        service.record_manual_job(job_id,batch_write_started_at=None,
                                                  batch_write_deadline_at=None,next_readback_at=0)
                        child=ManualCleanerCoordinator(service,adapter)
                        with patch.object(adapter,'readback',side_effect=CleanerTransactionRolledBack('commit_rolled_back')):
                            still_pending=child.tick()
                        assert (still_pending['state']=='ambiguous' and
                                still_pending.get('readback_attempts')==result.get('readback_attempts')),still_pending
                        service.record_manual_job(job_id,next_readback_at=0)
                        old_claim_deadline=(datetime.fromisoformat(first['created_at']).timestamp()+
                                            ManualCleanerCoordinator.BATCH_WRITE_DEADLINE_SECONDS)
                        with patch('packages.application.search_cluster_cleaner_self_service.time.time',
                                   return_value=old_claim_deadline+1):
                            expired=child.tick()
                        assert expired['state']=='partial' and expired['error_code']=='readback_unresolved',expired
                        status=parent.tick()
                        assert status['state']=='partial' and status['items'][0]['state']=='partial',status
                        assert status['items'][0]['delivery_state']=='wb_pending' and status['items'][0]['job_id']==job_id,status
                        assert status['items'][1]['state']=='skipped' and status['items'][1]['stage']=='not_started',status
                        assert status['items'][1]['job_id'] is None and status['not_started_count']==1,status
                        assert status['items'][0]['can_recheck'] and len(fake.writes)==1
                    else:
                        original_claim=KeywordCleaner.claim_exact_manual_run
                        blocked=[]
                        def blocked_claim(self,**kwargs):
                            if kwargs['run_id']==write_run and not blocked:
                                blocked.append(True)
                                return held_commit(box.runtime/'registry_upload_runtime.sqlite3',
                                                   lambda:original_claim(self,**kwargs))
                            return original_claim(self,**kwargs)
                        with patch.object(KeywordCleaner,'claim_exact_manual_run',blocked_claim):
                            result=child.tick()
                        assert blocked and result['state']=='failed' and result['error_code']=='local_not_submitted_retry',result
                        original_event=KeywordCleaner._event
                        interrupted=[]
                        def crash_park(self,c,kind,facts,**kwargs):
                            if kind=='batch_write_parked' and not interrupted:
                                interrupted.append(True)
                                raise RuntimeError('synthetic crash inside park transaction')
                            return original_event(self,c,kind,facts,**kwargs)
                        with patch.object(KeywordCleaner,'_event',crash_park):
                            try:parent.tick()
                            except RuntimeError:pass
                            else:raise AssertionError('park crash was not injected')
                        assert interrupted
                        with service.store.read() as c:
                            assert c.execute('SELECT state FROM cleaner_runs WHERE run_id=?',(write_run,)).fetchone()['state']=='queued'
                        parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                                                       fixture_admission=admitted,now=lambda:retry_time[0])
                        status=parent.tick()
                        assert status['items'][0]['state']=='retry_wait' and status['items'][0]['delivery_state']=='not_sent',status
                        snapshot=service.manual_batch_snapshot(batch_id,owner)
                        assert snapshot['write_retry_attempts']=={'0':1} and snapshot['read_retry_attempts']=={'0':1},snapshot
                        assert abs(datetime.fromisoformat(status['items'][0]['next_retry_at']).timestamp()-
                                   (retry_time[0]+READ_RETRY_DELAYS[0]))<2,status
                        assert child.pending_jobs()==[] and fake.writes==[]
                        parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                                                       fixture_admission=admitted,now=lambda:retry_time[0])
                        parent.tick()
                        retry_time[0]+=READ_RETRY_DELAYS[0]
                        assert parent.tick()['current_index']==0
                        service.record_manual_job(job_id,batch_write_deadline_at=time.time()-1)
                        assert child.tick()['state']=='failed' and fake.writes==[]
                        assert parent.tick()['items'][0]['state']=='retry_wait'
                        parent.tick()
                        retry_time[0]+=READ_RETRY_DELAYS[-1]-READ_RETRY_DELAYS[0]+READ_RETRY_POLL_GRACE+1
                        status=parent.tick()
                        assert status['items'][0]['state']=='partial' and status['items'][0]['error_code']=='write_retry_exhausted',status
                        assert status['items'][0]['delivery_state']=='not_sent' and fake.writes==[]
                        assert service.manual_job(job_id,owner)['write_run_id']==write_run
                    if parent.pending_batches():parent.tick()
                    final=batch_status(service,batch_id,owner)
                    assert final['state']=='partial' and final['waiting_count']==0,final
                    assert final['not_started_count']==(1 if uncertain_submit else 0),final
                    assert not parent.pending_batches() and not child.pending_jobs()
                    if uncertain_submit:
                        with service.store.read() as c:
                            operation=c.execute('SELECT state,dispatch_count FROM cleaner_write_operations WHERE run_id=?',(write_run,)).fetchone()
                            assert operation['dispatch_count']==1 and operation['state'] in {
                                'dispatching','submitted','unresolved','validation_rejected','rate_limited',
                                'unauthorized','forbidden','transport_ambiguous','http_error','requires_review'}
                        assert len(fake.writes)==1  # Deadline never resends an uncertain write.


def main():
    with Sandbox() as box:
        card=prepared_fixture(box,extra_adverts=(12,13))
        fake=FakeWB();fake.targets[12]=copy.deepcopy(fake.targets[11]);fake.targets[13]=copy.deepcopy(fake.targets[11])
        clock=Clock();clock.base=datetime.now(timezone.utc)+timedelta(seconds=1)
        with fake.server() as url:
            source=CleanerWbSource(account=box.service().account,
                runtime=OfficialApiRuntimeConfig('synthetic',url,2),fixture=True,
                clock=clock,monotonic=clock.monotonic,
                limiter=AccountLimiter(monotonic=clock.monotonic,sleep=clock.advance))
            original_cleaner=stage_e.KeywordCleaner
            with patch.object(stage_e.CleanerWbSource,'from_env',return_value=source), \
                 patch.object(stage_e,'fetch_current_card',side_effect=lambda nm_id:dict(card)), \
                 patch.object(stage_e,'KeywordCleaner',side_effect=lambda *a,**kw:original_cleaner(*a,clock=clock,**kw)):
                service=box.service();owner=Principal('owner',True,True,True)
                catalog=source._adverts([11,12,13],source.monotonic()+120)
                admitted=[dict(advert_id=aid,nm_id=101,state='verified') for aid in (11,12,13)]
                snapshot=eligibility_rows(service,'monolith',catalog,fixture_admission=admitted)
                batch_id='synthetic-prepared-recovery-0001'
                service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],
                    targets=[dict(advert_id=aid,nm_id=101) for aid in (11,12,13)]),owner,snapshot=snapshot)
                adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                retry_time=[time.time()]
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                                               fixture_admission=admitted,now=lambda:retry_time[0])
                child=ManualCleanerCoordinator(service,adapter)
                # First exact pair finishes and remains immutable while the
                # second is deliberately interrupted before admission. The
                # first write confirms in WB, then its final summary COMMIT
                # rolls back; exact readback must still settle it once.
                database=box.runtime/'registry_upload_runtime.sqlite3'
                original_finish=KeywordCleaner.finish_run
                finish_blocked=[]
                def blocked_finish(self,run_id,*args,**kwargs):
                    with self.store.read() as c:
                        run=c.execute('SELECT kind,targets FROM cleaner_runs WHERE run_id=?',(run_id,)).fetchone()
                    target={row['target'] for row in json.loads(run['targets'])}
                    if run['kind']=='manual_apply' and target=={'11:101'} and not finish_blocked:
                        finish_blocked.append(True)
                        return held_commit(database,lambda:original_finish(self,run_id,*args,**kwargs))
                    return original_finish(self,run_id,*args,**kwargs)
                second=None
                with patch.object(KeywordCleaner,'finish_run',blocked_finish):
                    for _ in range(60):
                        if parent.pending_batches():parent.tick()
                        if child.pending_jobs():child.tick()
                        current=batch_status(service,batch_id,owner)
                        if current['current_index']==1 and current['items'][1]['job_id']:
                            second=service.manual_job(current['items'][1]['job_id'],owner)
                            if second['stage']=='write_ready':break
                if not (current['current_index']==1 and current['items'][1]['job_id'] and second and second['stage']=='write_ready'):
                    raise AssertionError('second pair did not reach write preview')
                assert finish_blocked and current['items'][0]['state']=='complete' and len(fake.writes)==1,current
                original_preview=second['write_prestate']
                write_run=second['write_run_id'];job_id=second['job_id']
                external_id=child.operation_id(job_id,'write')
                original_renew=KeywordCleaner.renew_lease
                hit=[]
                def blocked_renew(self,*args,**kwargs):
                    if kwargs.get('phase')=='waiting_rate_limit' and not hit:
                        hit.append(True)
                        return held_commit(database,lambda:original_renew(self,*args,**kwargs))
                    return original_renew(self,*args,**kwargs)
                with patch.object(KeywordCleaner,'renew_lease',blocked_renew):
                    child.tick()
                assert hit and len(fake.writes)==1
                with service.store.read() as c:
                    run=c.execute('SELECT state,lease_expires_at FROM cleaner_runs WHERE run_id=?',(write_run,)).fetchone()
                    op=c.execute('SELECT operation_id,state,dispatch_count,target,candidate_digest FROM cleaner_write_operations WHERE run_id=?',
                                 (write_run,)).fetchone()
                    assert run['state']=='running' and op['state']=='prepared' and op['dispatch_count']==0
                    assert not c.execute('SELECT 1 FROM cleaner_readback_jobs WHERE operation_id=?',(op['operation_id'],)).fetchone()
                guard=AdmissionGuard(box.admission,service.store)
                with guard._lock():
                    state=guard._load()
                    assert op['operation_id'] not in state['seals'] and state['hold'] and not state.get('owner')
                    assert not state.get('manual_capability')
                    # A fsynced seal whose SQLite COMMIT rolled back must never
                    # be mistaken for a proven unsent operation.
                    original_state=copy.deepcopy(state)
                    state['seals'][op['operation_id']]=dict(target=op['target'],digest=op['candidate_digest'])
                    guard._save(state)
                clock.advance(200)
                try:guard.recover_unsubmitted_manual_run(cleaner=service,generation='monolith',
                    production_operation_id=external_id,run_id=write_run)
                except CleanerError as exc:assert exc.code=='restore_journal_gap',exc.code
                else:raise AssertionError('unmatched fsynced seal was ignored')
                with guard._lock():guard._save(original_state)
                assert len(fake.writes)==1
                # Crash after the cancellation/event DB COMMIT but before the
                # old held capability is cleared from the external guard.
                with guard._lock():
                    state=guard._load();state['manual_capability']=dict(run_id=write_run,
                        production_operation_id=external_id,operation_id=None)
                    guard._save(state)
                original_save=AdmissionGuard._save
                interrupted=[]
                def crash_before_guard_save(self,state):
                    if state.get('reason')=='manual_unsent_recovered' and not interrupted:
                        interrupted.append(True)
                        raise OSError('synthetic crash after DB recovery commit')
                    return original_save(self,state)
                service.record_manual_job(job_id,next_readback_at=0)
                with patch.object(AdmissionGuard,'_save',crash_before_guard_save):child.tick()
                assert interrupted and len(fake.writes)==1
                with service.store.read() as c:
                    run=c.execute('SELECT state,worker_token FROM cleaner_runs WHERE run_id=?',(write_run,)).fetchone()
                    old=c.execute('SELECT state,dispatch_count FROM cleaner_write_operations WHERE operation_id=?',
                                  (op['operation_id'],)).fetchone()
                    assert run['state']=='queued' and not run['worker_token']
                    assert old['state']=='cancelled_before_send' and old['dispatch_count']==0
                    assert c.execute("SELECT 1 FROM cleaner_events WHERE run_id=? AND kind='stage_e_unsent_recovered'",
                                     (write_run,)).fetchone()
                with guard._lock():assert guard._load().get('manual_capability')
                # Restart and read back the very same external operation. The
                # idempotent recovery closes the leaked capability, then the
                # coordinator persists a fresh-preview retry.
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                                               fixture_admission=admitted,now=lambda:retry_time[0])
                child=ManualCleanerCoordinator(service,adapter)
                service.record_manual_job(job_id,next_readback_at=0)
                parked=child.tick()
                assert parked['stage']=='finished' and parked['state']=='failed' and parked['error_code']=='local_not_submitted_retry',parked
                with guard._lock():assert not guard._load().get('manual_capability')
                assert service.exact_manual_run_unclaimed(write_run,external_id,Target(12,101))
                waiting=parent.tick()
                assert waiting['items'][1]['state']=='retry_wait' and waiting['items'][1]['delivery_state']=='not_sent',waiting
                assert child.pending_jobs()==[] and waiting['waiting_count']==1
                # The parked write run releases queue admission for an
                # independent third target while its own identity waits.
                for _ in range(40):
                    parent.tick()
                    if child.pending_jobs():child.tick()
                    sibling=batch_status(service,batch_id,owner)
                    if sibling['items'][2]['state']=='complete':break
                else:raise AssertionError('sibling did not finish while write waited')
                assert [write['advert_id'] for write in fake.writes]==[11,13],fake.writes
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                                               fixture_admission=admitted,now=lambda:retry_time[0])
                child=ManualCleanerCoordinator(service,adapter)
                for _ in range(3):
                    waiting=parent.tick()
                    if waiting['state']=='retry_wait':break
                assert waiting['state']=='retry_wait',waiting
                retry_time[0]+=5*60
                original_event=KeywordCleaner._event
                interrupted=[]
                def crash_rearm(self,c,kind,facts,**kwargs):
                    if kind=='batch_write_rearmed' and not interrupted:
                        interrupted.append(True)
                        raise RuntimeError('synthetic crash inside rearm transaction')
                    return original_event(self,c,kind,facts,**kwargs)
                with patch.object(KeywordCleaner,'_event',crash_rearm):
                    try:parent.tick()
                    except RuntimeError:pass
                    else:raise AssertionError('rearm crash was not injected')
                assert interrupted and not child.pending_jobs()
                with service.store.read() as c:
                    assert c.execute('SELECT state FROM cleaner_runs WHERE run_id=?',(write_run,)).fetchone()['state']=='stopped'
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                                               fixture_admission=admitted,now=lambda:retry_time[0])
                assert parent.tick()['current_index']==1
                resumed=service.manual_job(job_id,owner)
                assert resumed['stage']=='write_previewing' and resumed['state']=='queued',resumed
                assert resumed['write_run_id']==write_run
                request=dict(mode='manual',run_id=write_run,targets=[dict(advert_id=12,nm_id=101)])
                with patch.object(stage_e,'fetch_current_card',side_effect=lambda nm_id:dict(card,title='Unapproved drift')):
                    try:adapter.preview(request,external_id)
                    except CleanerError as exc:assert exc.code=='current_card_drift',exc.code
                    else:raise AssertionError('recovered run bypassed fresh card verification')
                assert len(fake.writes)==2
                fake.targets[12]['minus'].append('New old WB exclusion after rollback')
                refreshed=child.tick()
                assert refreshed['stage']=='write_ready' and refreshed['write_prestate']!=original_preview,refreshed
                fake.write_modes[12]='timeout' # WB saves set-minus, HTTP response is lost.
                original_readback=adapter.readback
                def locked_readback(request,operation_id):
                    if len(fake.writes)==3:
                        raise CleanerTransactionRolledBack('commit_rolled_back')
                    return original_readback(request,operation_id)
                with patch.object(adapter,'readback',locked_readback):
                    uncertain=child.tick()
                    for _ in range(2):
                        service.record_manual_job(job_id,next_readback_at=0)
                        child=ManualCleanerCoordinator(service,adapter)
                        uncertain=child.tick()
                assert uncertain['stage']=='write_apply_claimed' and uncertain['state']=='ambiguous',uncertain
                assert not uncertain.get('readback_attempts') and len(fake.writes)==3,uncertain
                # A restart must keep the same claim and read back only; a
                # local SQLite rollback is not a failed WB observation.
                child=ManualCleanerCoordinator(service,adapter)
                service.record_manual_job(job_id,next_readback_at=0)
                for _ in range(80):
                    if parent.pending_batches():parent.tick()
                    if child.pending_jobs():child.tick()
                    current=batch_status(service,batch_id,owner)
                    if current['state'] in {'complete','partial','failed'}:break
                    active=current['items'][current['current_index']] if current['current_index']<3 else None
                    if active and active['job_id']:
                        saved=service.manual_job(active['job_id'],owner)
                        if saved.get('next_readback_at',0)>time.time():
                            time.sleep(min(2.1,saved['next_readback_at']-time.time()))
                else:raise AssertionError('batch did not settle after proven-unsent recovery')
                assert current['state']=='complete' and current['done_count']==3,current
                assert [item['state'] for item in current['items']]==['complete','complete','complete']
                assert [write['advert_id'] for write in fake.writes]==[11,13,12],fake.writes
                detail=service.run_detail(write_run,owner)
                assert detail['effective_state']=='complete' and all(row['state']=='confirmed' for row in detail['phrases'])
                assert {row['state'] for row in detail['write_operations']}=={'cancelled_before_send','confirmed'}
                with service.store.read() as c:
                    ops=c.execute('SELECT operation_id,dispatch_count FROM cleaner_write_operations WHERE run_id=?',(write_run,)).fetchall()
                    assert sorted(row['dispatch_count'] for row in ops)==[0,1]
                    assert c.execute("SELECT count(*) FROM cleaner_events WHERE account=? AND kind='self_service_batch_requested' AND json_extract(facts,'$.batch_id')=?",
                                     (service.key,batch_id)).fetchone()[0]==1
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                                               fixture_admission=admitted,now=lambda:retry_time[0])
                child=ManualCleanerCoordinator(service,adapter)
                assert not parent.pending_batches() and not child.pending_jobs()
                assert len(fake.writes)==3
    scan_finish_cases()
    return_only_finish_case()
    batch_write_deadlines()
    print('search cluster cleaner write recovery smoke: ok')


if __name__=='__main__':main()
