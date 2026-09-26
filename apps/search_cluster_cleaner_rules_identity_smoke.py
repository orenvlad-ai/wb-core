#!/usr/bin/env python3
"""Cross-process rule identity and exact recovered manual write, with fake WB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from apps import search_cluster_cleaner_stage_e as stage_e
from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from apps.search_cluster_cleaner_write_fixture import AccountLimiter, Clock, FakeWB
from packages.adapters.official_api_runtime import OfficialApiRuntimeConfig
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner_admission import AdmissionGuard
from packages.application.search_cluster_cleaner_self_service import LocalStageEAdapter, ManualCleanerCoordinator
from packages.application.search_cluster_cleaner_store import CleanerStore, CleanerTransactionRolledBack
from packages.application.search_cluster_cleaner_rules_identity import executable_rules_digest
from packages.application.search_cluster_cleaner_writer import CleanerWriter
from packages.application.search_cluster_cleaner_worker import ManualCleanerWorker
from packages.application.storage_registry import StoreRegistry
from packages.contracts.search_cluster_cleaner import Account, CleanerError, Principal, Target
from packages.domain import search_cluster_classifier as rules


OLD_MARSHAL_HASH='a9bebf43bf118b28aac63fbca8464ed916067485a583486d59a72ea98cb25f46'
OLD_DECISION_MARSHAL_HASH='06b4eb49bc3e96dc6a6c974ab85dc8030ef4b9d4fe39e64b3d69de3431457619'


def source_for(url: str, clock: Clock) -> CleanerWbSource:
    return CleanerWbSource(account=Account('seller','scope'),
        runtime=OfficialApiRuntimeConfig('synthetic',url,2),fixture=True,
        clock=clock,monotonic=clock.monotonic,
        limiter=AccountLimiter(monotonic=clock.monotonic,sleep=clock.advance))


def card_fixture(box: Sandbox) -> dict:
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


def cross_process_digest() -> None:
    code=("from packages.application.search_cluster_cleaner import KeywordCleaner; "
          "from packages.contracts.search_cluster_cleaner import Account; "
          "print(KeywordCleaner(None,Account('seller','scope')).rules_digest)")
    with tempfile.TemporaryDirectory(prefix='rules-pycache-') as cache:
        values=[]
        for seed in ('1','1','2','random'):
            env=dict(os.environ,PYTHONHASHSEED=seed,PYTHONPYCACHEPREFIX=cache)
            result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,
                                  check=True,text=True,capture_output=True)
            values.append(result.stdout.strip())
        assert len(set(values))==1,values
        alternate=types.FunctionType(rules.classify.__code__.replace(co_filename='/another/checkout/rules.py'),
                                     rules.classify.__globals__)
        assert executable_rules_digest(alternate)==values[0]
        assert executable_rules_digest(lambda *_: {'verdict':'allow'})!=values[0]
        with patch.object(rules,'VOCAB',rules.VOCAB+'|synthetic_extra'):
            assert executable_rules_digest()!=values[0]
        print('cross-process stable digest:',values[0])


def child_main() -> None:
    context=json.loads(os.environ['WBC_SYNTHETIC_RULES_CONTEXT'])
    stage_e.ROOT=Path(context['app'])
    clock=Clock();clock.base=datetime.fromisoformat(context['now']);clock.seconds=0
    source=source_for(context['url'],clock)
    original_cleaner=stage_e.KeywordCleaner
    with patch.object(stage_e.CleanerWbSource,'from_env',return_value=source), \
         patch.object(stage_e,'fetch_current_card',side_effect=lambda _nm:context['card']), \
         patch.object(stage_e,'KeywordCleaner',side_effect=lambda *a,**kw:original_cleaner(*a,clock=clock,**kw)):
        service=KeywordCleaner(CleanerStore(StoreRegistry(Path(context['runtime']))),
                               Account('seller','scope'),owner_username='owner',clock=clock)
        adapter=LocalStageEAdapter(runtime_dir=Path(context['runtime']),env_file=Path(context['env']),
                                   admission_dir=Path(context['admission']))
        coordinator=ManualCleanerCoordinator(service,adapter)
        for _ in range(35):
            status=coordinator.tick()
            if status and status['state'] in {'complete','partial','failed','no_change'}:break
            if status and status.get('next_readback_at',0)>time.time():
                time.sleep(min(2.1,status['next_readback_at']-time.time()))
        else:raise AssertionError('recovered child did not settle')
        assert status['state']=='complete',status
        detail=service.run_detail(context['write_run'],Principal('owner',True,True,True))
        assert detail['effective_state']=='complete' and all(row['state']=='confirmed' for row in detail['phrases']),detail
        with service.store.read() as c:
            event=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND run_id=? AND kind='rules_semantic_revalidated'",
                            (service.key,context['write_run'])).fetchone()
            assert event and json.loads(event[0])['previous_rules_hash']==OLD_MARSHAL_HASH
            assert {item['rules_hash'] for item in json.loads(event[0])['decision_rules_hashes']}=={OLD_DECISION_MARSHAL_HASH}
        print(json.dumps(dict(state=status['state'],write_run=context['write_run'],
                              rules_digest=service.rules_digest),sort_keys=True))


def resumed_old_run() -> None:
    with Sandbox() as box:
        card=card_fixture(box)
        fake=FakeWB();clock=Clock();clock.base=datetime.now(timezone.utc)+timedelta(seconds=1)
        with fake.server() as url:
            source=source_for(url,clock)
            original_cleaner=stage_e.KeywordCleaner
            def old_cleaner(*args,**kwargs):
                service=original_cleaner(*args,clock=clock,**kwargs)
                with service.store.read() as c:
                    active=c.execute("SELECT kind FROM cleaner_runs WHERE account=? AND state IN('queued','running') ORDER BY created_at DESC LIMIT 1",
                                     (service.key,)).fetchone()
                # The old scan process and later write process imported the
                # same classifier through different source/.pyc paths. The
                # immutable decision facts genuinely carry a different hash
                # from the write run captured by the parent process.
                service.rules_digest=OLD_DECISION_MARSHAL_HASH if active and active['kind']=='scan' else OLD_MARSHAL_HASH
                return service
            with patch.object(stage_e.CleanerWbSource,'from_env',return_value=source), \
                 patch.object(stage_e,'fetch_current_card',side_effect=lambda _nm:dict(card)), \
                 patch.object(stage_e,'KeywordCleaner',side_effect=old_cleaner):
                service=box.service()
                service.rules_digest=OLD_MARSHAL_HASH
                owner=Principal('owner',True,True,True)
                job_id='synthetic-legacy-rules-0001'
                job=service.start_manual_clean(dict(request_id=job_id,advert_id=11,nm_id=101),owner)
                adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                coordinator=ManualCleanerCoordinator(service,adapter)
                for _ in range(35):
                    status=coordinator.tick()
                    if status and status['stage']=='write_ready':break
                else:raise AssertionError('old process did not prepare manual write')
                write_run=status['write_run_id']
                with service.store.read() as c:
                    decision_id=json.loads(c.execute('SELECT targets FROM cleaner_runs WHERE run_id=?',(write_run,)).fetchone()[0])[0]['decision_id']
                    decision_facts=json.loads(c.execute('SELECT facts FROM cleaner_auto_decisions WHERE decision_id=?',(decision_id,)).fetchone()[0])
                    assert decision_facts['rules_hash']==OLD_DECISION_MARSHAL_HASH
                original_renew=KeywordCleaner.renew_lease
                hit=[]
                def fail_preseal(self,*args,**kwargs):
                    if kwargs.get('phase')=='waiting_rate_limit' and not hit:
                        hit.append(True)
                        raise CleanerTransactionRolledBack('commit_rolled_back')
                    return original_renew(self,*args,**kwargs)
                with patch.object(KeywordCleaner,'renew_lease',fail_preseal):
                    coordinator.tick()
                assert hit and not fake.writes
                with service.store.read() as c:
                    op=c.execute('SELECT state,dispatch_count FROM cleaner_write_operations WHERE run_id=?',
                                 (write_run,)).fetchone()
                    assert op and op['state']=='prepared' and op['dispatch_count']==0
                    diagnostic=c.execute("SELECT facts FROM cleaner_events WHERE run_id=? AND kind='stage_e_manual_failure' ORDER BY sequence DESC LIMIT 1",
                                         (write_run,)).fetchone()
                    assert diagnostic, 'guarded failure diagnostic was not persisted'
                    safe=json.loads(diagnostic[0])
                    assert safe['code']=='CleanerTransactionRolledBack' and safe['error_type']=='CleanerTransactionRolledBack'
                    assert safe['failed_stage'].startswith('manual_write_') and set(safe)=={'failed_stage','code','error_type'}
                clock.advance(200)
                service.record_manual_job(job_id,next_readback_at=0)
                for _ in range(4):
                    status=coordinator.tick()
                    if status['stage']=='write_previewing':break
                    service.record_manual_job(job_id,next_readback_at=0)
                else:raise AssertionError('old unsent preparation was not recovered: '+repr(status))
                with service.store.read() as c:
                    run=c.execute('SELECT * FROM cleaner_runs WHERE run_id=?',(write_run,)).fetchone()
                    old=c.execute('SELECT state,dispatch_count FROM cleaner_write_operations WHERE run_id=?',(write_run,)).fetchone()
                    assert run['state']=='queued' and json.loads(run['captured_versions'])['rules_hash']==OLD_MARSHAL_HASH
                    assert old['state']=='cancelled_before_send' and old['dispatch_count']==0
                    writer=CleanerWriter(service,source,object(),generation='monolith',manual_only=True)
                    captured=json.loads(run['captured_versions'])
                    without_recovery=dict(run);without_recovery['run_id']='unbound-synthetic-run'
                    try:writer._revalidate_recovered_rules(c,without_recovery,captured)
                    except Exception as exc:assert getattr(exc,'code',None)=='rules_changed'
                    else:raise AssertionError('unrecovered run was admitted')
                    with patch.object(service,'rules_source_digest','different-source'):
                        try:writer._revalidate_recovered_rules(c,run,captured)
                        except Exception as exc:assert getattr(exc,'code',None)=='rules_changed'
                        else:raise AssertionError('changed source was admitted')
                    with patch.object(service,'classifier',return_value=dict(verdict='allow',rule='CHANGED',reason='changed')):
                        try:writer._revalidate_recovered_rules(c,run,captured)
                        except Exception as exc:assert getattr(exc,'code',None)=='rules_changed'
                        else:raise AssertionError('changed exact decision was admitted')
                assert not fake.writes
                # A later fresh preflight may fail before admission. Its new
                # prepared operation is locally cancelled, never dispatched;
                # exact readback must certify it and requeue this same run.
                service.record_manual_job(job_id,next_readback_at=0)
                assert coordinator.tick()['stage']=='write_ready'
                with patch.object(CleanerWriter,'admit',side_effect=CleanerError('preflight_stale','synthetic preflight')):
                    status=coordinator.tick()
                assert status['stage']=='write_apply_claimed',status
                with service.store.read() as c:
                    local=c.execute("SELECT operation_id,state,dispatch_count FROM cleaner_write_operations WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
                                    (write_run,)).fetchone()
                    assert local['state']=='cancelled_before_send' and local['dispatch_count']==0,dict(local)
                    assert c.execute("SELECT 1 FROM cleaner_events WHERE run_id=? AND operation_id=? AND kind='candidate_cancelled'",
                                     (write_run,local['operation_id'])).fetchone()
                clock.advance(200)
                guard=AdmissionGuard(box.admission,service.store)
                recovery_service=KeywordCleaner(service.store,Account('seller','scope'),owner_username='owner',clock=clock)
                exact_write_op=coordinator.operation_id(job_id,'write')
                persisted_job=service.manual_job(job_id,owner)
                with guard._lock():
                    state=guard._load()
                    state['manual_capability']=dict(run_id=write_run,production_operation_id=exact_write_op,
                                                    targets=['11:101'],scope_digest=ManualCleanerWorker._scope(write_run,[Target(11,101)]),
                                                    prestate_sha256=persisted_job['write_prestate'],
                                                    candidate_sha256=persisted_job['write_candidate'])
                    guard._save(state)
                with patch.object(guard,'_save',side_effect=OSError('synthetic crash after DB commit')):
                    try:guard.recover_unsubmitted_manual_run(cleaner=recovery_service,generation='monolith',
                        production_operation_id=exact_write_op,run_id=write_run)
                    except OSError:pass
                    else:raise AssertionError('guard cleanup crash was not injected')
                with service.store.read() as c:
                    assert c.execute('SELECT state FROM cleaner_runs WHERE run_id=?',(write_run,)).fetchone()[0]=='queued'
                    assert c.execute("SELECT count(*) FROM cleaner_events WHERE run_id=? AND kind='stage_e_unsent_recovered'",
                                     (write_run,)).fetchone()[0]==2
                # The next exact readback finishes the old capability cleanup
                # without another DB cancellation or external submit.
                assert guard.recover_unsubmitted_manual_run(cleaner=recovery_service,generation='monolith',
                    production_operation_id=exact_write_op,run_id=write_run)
                service.record_manual_job(job_id,next_readback_at=0)
                for _ in range(4):
                    status=coordinator.tick()
                    if status['stage']=='write_previewing':break
                    service.record_manual_job(job_id,next_readback_at=0)
                else:raise AssertionError('second local cancellation was not recovered: '+repr(status))
                with service.store.read() as c:
                    events=[json.loads(row[0]) for row in c.execute(
                        "SELECT facts FROM cleaner_events WHERE run_id=? AND kind='stage_e_unsent_recovered' ORDER BY sequence",(write_run,))]
                    assert len(events)==2 and local['operation_id'] in events[-1]['cancelled_operations'],events
                assert not fake.writes
                context=dict(app=str(box.app),runtime=str(box.runtime),env=str(box.env),
                             admission=str(box.admission),url=url,card=card,now=clock(),write_run=write_run)
            env=dict(os.environ,WBC_SYNTHETIC_RULES_CONTEXT=json.dumps(context))
            with tempfile.TemporaryDirectory(prefix='rules-child-pycache-') as cache:
                env['PYTHONPYCACHEPREFIX']=cache
                # Persisted run was made by the old source-compiled process;
                # the real resume must load newly cached bytecode, not compile
                # source again in the child that performs the fake WB write.
                subprocess.run([sys.executable,'-c',
                    'import packages.application.search_cluster_cleaner; import packages.domain.search_cluster_classifier'],
                    cwd=ROOT,env=env,check=True,capture_output=True,text=True)
                assert list(Path(cache).rglob('search_cluster_classifier.*.pyc'))
                result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--child'],
                    cwd=ROOT,env=env,check=True,text=True,capture_output=True,timeout=90)
            assert json.loads(result.stdout.splitlines()[-1])['state']=='complete',result.stdout
            assert len(fake.writes)==1,fake.writes


if __name__=='__main__':
    if '--child' in sys.argv:child_main()
    else:
        cross_process_digest()
        resumed_old_run()
        print('search cluster cleaner rules identity smoke: ok')
