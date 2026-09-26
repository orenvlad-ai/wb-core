#!/usr/bin/env python3
"""Full synthetic UI-intent -> local Production Apply -> Stage E -> fake WB."""
from __future__ import annotations

from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from apps import search_cluster_cleaner_stage_e as stage_e
from apps import registry_upload_http_entrypoint_live as live
from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from apps.search_cluster_cleaner_write_fixture import FakeWB,Clock
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource,AccountLimiter
from packages.adapters.official_api_runtime import OfficialApiRuntimeConfig
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.search_cluster_cleaner_admission import AdmissionGuard
from packages.application.search_cluster_cleaner_self_service import LocalStageEAdapter,ManualCleanerCoordinator
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.contracts.search_cluster_cleaner import CleanerError, Principal, Target


def main() -> None:
    with Sandbox() as box:
        characteristics=[dict(id=1,name='Модель',value=['iPhone 16 Pro Max']),dict(id=2,name='Тип',value=['Обычное стекло'])]
        card=dict(nm_id='101',title='Synthetic glass',vendor_code='synthetic',description='Approved synthetic card',characteristics=characteristics)
        current_card=dict(card,characteristics=list(reversed(characteristics)))
        raw=json.dumps(dict(cards=[dict(card,card_digest='sha256:'+'1'*64)]),sort_keys=True).encode()
        card_path=box.admission/'card-source-approved.json';card_path.write_bytes(raw);card_path.chmod(0o600)
        box.package['provenance']['fresh_cards_sha256']='sha256:'+hashlib.sha256(raw).hexdigest()
        box.write_package()
        bootstrap=box.execute('preview')
        box.execute('apply',expected_prestate=bootstrap['prestate_sha256'],expected_candidate=bootstrap['candidate_sha256'])
        assert box.execute('readback')['state']=='applied'
        ChangeRegistryRepository(box.runtime).initialize_schema()
        fake=FakeWB();clock=Clock();clock.base=datetime.now(timezone.utc)+timedelta(seconds=1)
        with fake.server() as url:
            source=CleanerWbSource(account=box.service().account,runtime=OfficialApiRuntimeConfig('synthetic',url,2),fixture=True,
                clock=clock,monotonic=clock.monotonic,limiter=AccountLimiter(monotonic=clock.monotonic,sleep=clock.advance))
            original_source=stage_e.CleanerWbSource.from_env
            original_card=stage_e.fetch_current_card
            original_cleaner=stage_e.KeywordCleaner
            stage_e.CleanerWbSource.from_env=lambda account:source
            stage_e.fetch_current_card=lambda nm_id:dict(current_card)
            stage_e.KeywordCleaner=lambda *args,**kwargs:original_cleaner(*args,clock=clock,**kwargs)
            try:
                service=box.service()
                web=CleanerWeb(service,generation='monolith',approved_targets=[dict(advert_id=11,nm_id=101,state='verified',campaign_name='Synthetic campaign')])
                web.worker_status=lambda:'ready'
                with patch.object(live,'STAGE_E_CONFIG_PATH',box.admission/'stage-e-config.json'):
                    assert live._cleaner_contour_configured(SimpleNamespace(cleaner_web=web),box.runtime)
                target=Target(11,101)
                stage_e._verify_fresh_card(box.package,target,box.admission,service)
                for changed in (dict(current_card,title='Changed title'),
                                dict(current_card,vendor_code='changed'),
                                dict(current_card,characteristics=[dict(id=1,name='Модель',value=['iPhone 15']),characteristics[1]])):
                    stage_e.fetch_current_card=lambda nm_id, value=changed:value
                    try:stage_e._verify_fresh_card(box.package,target,box.admission,service)
                    except CleanerError as exc:assert exc.code=='current_card_drift',exc.code
                    else:raise AssertionError('changed current card was admitted')
                stage_e.fetch_current_card=lambda nm_id:dict(current_card)
                owner=Principal('owner',True,True,True)
                job_id='synthetic-self-service-0001'
                locked=threading.Event()
                def warehouse_writer():
                    connection=sqlite3.connect(box.runtime/'registry_upload_runtime.sqlite3')
                    connection.execute('BEGIN IMMEDIATE')
                    locked.set();time.sleep(1.5);connection.commit();connection.close()
                warehouse=threading.Thread(target=warehouse_writer)
                warehouse.start();assert locked.wait(2)
                started=time.monotonic()
                legacy=service.start_run(dict(request_id='synthetic-legacy-scan-0001',advert_id=11,nm_id=101),owner)
                warehouse.join(3)
                assert time.monotonic()-started>=1.3
                adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                coordinator=ManualCleanerCoordinator(service,adapter)
                assert coordinator.pending_jobs()==[] and not fake.calls
                accepted=web.start_manual_clean(dict(request_id=job_id,advert_id=11,nm_id=101),owner)
                assert accepted['state']=='queued' and accepted['adopted'] and accepted['run_id']==legacy['run_id'] and not fake.calls
                original_launch=coordinator._launch
                def crash_after_exact_write(action,operation_id,request,**expected):
                    receipt=original_launch(action,operation_id,request,**expected)
                    if action=='apply' and operation_id.endswith('-write'):
                        raise SystemExit('synthetic process death after exact submit')
                    return receipt
                coordinator._launch=crash_after_exact_write
                for _ in range(60):
                    try:status=coordinator.tick()
                    except SystemExit:
                        status=service.manual_job(job_id,owner)
                        assert status['stage']=='write_apply_claimed' and len(fake.writes)==1
                        coordinator=ManualCleanerCoordinator(service,adapter)
                        continue
                    if status and status['state'] in {'complete','partial','failed','no_change'}:break
                    if status and status['state']=='ambiguous':time.sleep(min(2.1,max(0,status.get('next_readback_at',0)-time.time())))
                else:raise AssertionError('manual job did not terminate: '+repr(status))
                assert status['state']=='complete',status
                assert len(fake.writes)==1,fake.writes
                detail=service.run_detail(status['write_run_id'],owner)
                assert detail['effective_state']=='complete',detail
                assert detail['phrases'] and all(row['state']=='confirmed' for row in detail['phrases'])
                assert coordinator.pending_jobs()==[]
                assert len(fake.writes)==1
                # Reload and duplicate request stay attached to the same job.
                web.worker_status=lambda:'busy'
                repeated=web.start_manual_clean(dict(request_id=job_id,advert_id=11,nm_id=101),owner)
                assert repeated==accepted and len(fake.writes)==1
                profile=service.get_profile(101,owner)
                changed_profile=dict(box.package['profiles'][0],kind='matte')
                created=service.create_profile(101,dict(request_id='synthetic-profile-change-0001',expected_revision=profile['revision'],profile=changed_profile),owner)
                service.activate_profile(101,dict(request_id='synthetic-profile-activate-0001',expected_revision=created['profile_revision'],version=created['profile_version']),owner)
                try:stage_e._verify_fresh_card(box.package,target,box.admission,service)
                except CleanerError as exc:assert exc.code=='manual_profile_mismatch',exc.code
                else:raise AssertionError('changed profile was admitted against old approved source')
                # An apparently confirmed operation cannot turn green when an
                # exact prepared candidate is absent from its item receipt.
                with service.store.transaction() as c:
                    operation=c.execute("SELECT operation_id FROM cleaner_write_operations WHERE run_id=?",(status['write_run_id'],)).fetchone()['operation_id']
                    item=c.execute("SELECT query_hash FROM cleaner_write_items WHERE operation_id=? LIMIT 1",(operation,)).fetchone()['query_hash']
                    c.execute("DELETE FROM cleaner_write_items WHERE operation_id=? AND query_hash=?",(operation,item))
                write_request=dict(mode='manual',run_id=status['write_run_id'],targets=[dict(advert_id=11,nm_id=101)])
                readback=stage_e.execute(dict(action='readback',operation_id=coordinator.operation_id(job_id,'write'),request=write_request,
                                              expected_runtime_sha=adapter.runtime_sha),runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                assert readback['state']!='applied',readback
                assert service.run_detail(status['write_run_id'],owner)['effective_state']!='complete'
            finally:
                stage_e.CleanerWbSource.from_env=original_source
                stage_e.fetch_current_card=original_card
                stage_e.KeywordCleaner=original_cleaner
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=box.service();target=Target(11,101);operation_id='synthetic-no-submit-recovery-0001'
        with service.store.transaction() as c:
            run_id=service._new_run(c,'manual_apply','manual_exact_candidates',request_id=operation_id,
                                    targets=[dict(target=target.key,query_hash='q',decision_id='d')])
        service.claim_exact_manual_run(run_id=run_id,targets=[target],generation='monolith',production_operation_id=operation_id)
        guard=AdmissionGuard(box.admission,service.store)
        with guard._lock():
            state=guard._load()
            state['manual_capability']=dict(run_id=run_id,production_operation_id=operation_id,scope_digest='synthetic')
            state['owner']=None
            guard._save(state)
        try:guard.recover_empty_manual_capability(account=service.account,generation='monolith',production_operation_id='wrong-operation',run_id=run_id)
        except CleanerError as exc:assert exc.code=='manual_capability_lost'
        else:raise AssertionError('wrong no-submit capability recovered')
        guard.recover_empty_manual_capability(account=service.account,generation='monolith',production_operation_id=operation_id,run_id=run_id)
        with guard._lock():
            state=guard._load()
            assert state['hold'] and not state.get('owner') and not state.get('manual_capability')
    from apps.search_cluster_cleaner_batch_smoke import main as batch_main
    batch_main()
    from apps.search_cluster_cleaner_drift_batch_smoke import main as drift_batch_main
    drift_batch_main()
    from apps.search_cluster_cleaner_cpm_reliability_smoke import main as reliability_main
    reliability_main()
    from apps.search_cluster_cleaner_held_evidence_smoke import main as held_evidence_main
    held_evidence_main()
    from apps.search_cluster_cleaner_write_recovery_smoke import main as write_recovery_main
    write_recovery_main()
    print('search cluster cleaner self-service smoke: ok')


if __name__=='__main__':main()
