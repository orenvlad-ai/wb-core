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


def prepared_fixture(box: Sandbox):
    box.package['manual_admission'].append(dict(box.package['manual_admission'][0], advert_id=12))
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


def main():
    with Sandbox() as box:
        card=prepared_fixture(box)
        fake=FakeWB();fake.targets[12]=copy.deepcopy(fake.targets[11])
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
                catalog=source._adverts([11,12],source.monotonic()+120)
                admitted=[dict(advert_id=aid,nm_id=101,state='verified') for aid in (11,12)]
                snapshot=eligibility_rows(service,'monolith',catalog,fixture_admission=admitted)
                batch_id='synthetic-prepared-recovery-0001'
                service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],
                    targets=[dict(advert_id=aid,nm_id=101) for aid in (11,12)]),owner,snapshot=snapshot)
                adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                                               fixture_admission=admitted)
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
                                               fixture_admission=admitted)
                child=ManualCleanerCoordinator(service,adapter)
                service.record_manual_job(job_id,next_readback_at=0)
                resumed=child.tick()
                assert resumed['stage']=='write_previewing' and resumed['state']=='running',resumed
                with guard._lock():assert not guard._load().get('manual_capability')
                assert service.exact_manual_run_unclaimed(write_run,external_id,Target(12,101))
                request=dict(mode='manual',run_id=write_run,targets=[dict(advert_id=12,nm_id=101)])
                with patch.object(stage_e,'fetch_current_card',side_effect=lambda nm_id:dict(card,title='Unapproved drift')):
                    try:adapter.preview(request,external_id)
                    except CleanerError as exc:assert exc.code=='current_card_drift',exc.code
                    else:raise AssertionError('recovered run bypassed fresh card verification')
                assert len(fake.writes)==1
                fake.targets[12]['minus'].append('New old WB exclusion after rollback')
                time.sleep(max(0,resumed['next_readback_at']-time.time()))
                refreshed=child.tick()
                assert refreshed['stage']=='write_ready' and refreshed['write_prestate']!=original_preview,refreshed
                fake.write_modes[12]='timeout' # WB saves set-minus, HTTP response is lost.
                for _ in range(80):
                    if parent.pending_batches():parent.tick()
                    if child.pending_jobs():child.tick()
                    current=batch_status(service,batch_id,owner)
                    if current['state'] in {'complete','partial','failed'}:break
                    active=current['items'][current['current_index']] if current['current_index']<2 else None
                    if active and active['job_id']:
                        saved=service.manual_job(active['job_id'],owner)
                        if saved.get('next_readback_at',0)>time.time():
                            time.sleep(min(2.1,saved['next_readback_at']-time.time()))
                else:raise AssertionError('batch did not settle after proven-unsent recovery')
                assert current['state']=='complete' and current['done_count']==2,current
                assert [item['state'] for item in current['items']]==['complete','complete']
                assert [write['advert_id'] for write in fake.writes]==[11,12],fake.writes
                detail=service.run_detail(write_run,owner)
                assert detail['effective_state']=='complete' and all(row['state']=='confirmed' for row in detail['phrases'])
                assert {row['state'] for row in detail['write_operations']}=={'cancelled_before_send','confirmed'}
                with service.store.read() as c:
                    ops=c.execute('SELECT operation_id,dispatch_count FROM cleaner_write_operations WHERE run_id=?',(write_run,)).fetchall()
                    assert sorted(row['dispatch_count'] for row in ops)==[0,1]
                    assert c.execute("SELECT count(*) FROM cleaner_events WHERE account=? AND kind='self_service_batch_requested' AND json_extract(facts,'$.batch_id')=?",
                                     (service.key,batch_id)).fetchone()[0]==1
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,
                                               fixture_admission=admitted)
                child=ManualCleanerCoordinator(service,adapter)
                assert not parent.pending_batches() and not child.pending_jobs()
                assert len(fake.writes)==2
    scan_finish_cases()
    print('search cluster cleaner write recovery smoke: ok')


if __name__=='__main__':main()
