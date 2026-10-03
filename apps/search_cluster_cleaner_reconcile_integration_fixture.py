#!/usr/bin/env python3
"""Authenticated local UI -> real cleaner batch/Stage E -> loopback FakeWB.

The only network destinations are the local UI and fake WB servers. Work is
created by an explicit owner click; the background worker only advances saved
manual jobs. Both SQLite stores and the approved package are disposable.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from apps import search_cluster_cleaner_stage_e as stage_e
from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash
from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from apps.search_cluster_cleaner_web_fixture import Fixture,PASSWORD
from apps.search_cluster_cleaner_write_fixture import FakeWB,Clock
from apps.sheet_vitrina_v1_ads_smoke import FakePromotionSource,_seed_runtime,_build_ads_block,NOW
from packages.adapters.official_api_runtime import OfficialApiRuntimeConfig
from packages.adapters.registry_upload_http_entrypoint import build_registry_upload_http_server,DEFAULT_SHEET_WEB_VITRINA_UI_PATH
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource,AccountLimiter
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator
from packages.application.search_cluster_cleaner_self_service import LocalStageEAdapter,ManualCleanerCoordinator
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.contracts.search_cluster_cleaner import Principal,Target,digest,query_hash
from packages.domain.search_cluster_sources import union_snapshot

LEGACY_QUERY='стекло iphone 16 pro max без салфетки'


@contextmanager
def running_integration_fixture(port=0):
    with Sandbox() as box:
        # Approved baseline grants a return for a phrase already in minus.
        row=dict(advert_id=11,nm_id=101,query='OLD',decision='allow',observed_state='excluded',provenance={'synthetic':True})
        box.package['rows'].append(row)
        box.package['rows_digest']='sha256:'+digest(box.package['rows'])
        from apps.search_cluster_cleaner_stage_e_recovery_smoke import semantic_fixture_card
        card=semantic_fixture_card();characteristics=card['characteristics']
        raw=json.dumps(dict(cards=[dict(card,card_digest='sha256:'+'1'*64)]),sort_keys=True).encode()
        card_path=box.admission/'card-source-approved.json';card_path.write_bytes(raw);card_path.chmod(0o600)
        box.package['provenance']['fresh_cards_sha256']='sha256:'+hashlib.sha256(raw).hexdigest()
        box.write_package()
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        assert box.execute('readback')['state']=='applied'
        ChangeRegistryRepository(box.runtime).initialize_schema()
        runtime=_seed_runtime(box.runtime)
        fake=FakeWB()
        fake.targets[11]['active'].append(LEGACY_QUERY)
        fake.targets[11]['stats'].append(LEGACY_QUERY)
        clock=Clock();clock.base=datetime.now(timezone.utc)+timedelta(seconds=1)
        with fake.server() as wb_url:
            service=box.service();service.clock=clock
            # A prior policy left this exact phrase as a question. The next
            # full scan must replace it with its current automatic decision.
            owner=Principal('owner',True,True,True)
            target=Target(11,101,contract_verified=True)
            seed=service.start_run(dict(request_id='fixture-legacy-manual-scan',advert_id=11,nm_id=101),owner)
            claimed=service.claim_exact_manual_run(run_id=seed['run_id'],targets=[target],generation='monolith',production_operation_id='fixture-legacy-scan')
            observed=clock()
            snapshot=union_snapshot(target,list_entry=dict(active=[LEGACY_QUERY],excluded=['OLD'],archived=[]),stats_queries=[LEGACY_QUERY],minus_queries=['OLD'],observed_at=observed,source_times={name:observed for name in ('list','statistics','minus')})
            service.record_snapshot(seed['run_id'],claimed['worker_token'],'monolith',snapshot,manual_only=True)
            service.finish_run(seed['run_id'],claimed['worker_token'],'monolith',manual_only=True)
            with service.store.transaction() as db:
                old=db.execute('SELECT * FROM cleaner_observations WHERE account=? AND target=? AND query_hash=?',
                               (service.key,target.key,query_hash(LEGACY_QUERY))).fetchone()
                assert old is not None
                service._review(db,dict(old),'Старый вопрос владельцу')
                service._sync_reviews(db)
                # Production regression: an old drift hold must not prevent
                # the owner UI from starting the scan that can reconcile it.
                db.execute('INSERT INTO cleaner_target_holds VALUES(?,?,?,?)',
                           (service.key,target.key,'external_state_drift',clock()))
            assert service.reviews(owner)['items']
            source=CleanerWbSource(account=service.account,runtime=OfficialApiRuntimeConfig('synthetic',wb_url,2),fixture=True,
                                   clock=clock,monotonic=clock.monotonic,limiter=AccountLimiter(monotonic=clock.monotonic,sleep=clock.advance))
            original_cleaner=stage_e.KeywordCleaner
            env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG','PYTHONPATH','PLAYWRIGHT_BROWSERS_PATH') if key in os.environ}
            env.update(WB_CORE_WEB_AUTH_REQUIRED='1',WB_CORE_WEB_AUTH_USERNAME='owner',WB_CORE_WEB_AUTH_PASSWORD_HASH=_password_hash(PASSWORD),WB_CORE_WEB_AUTH_SESSION_SECRET='synthetic-reconcile-integration-secret')
            with patch.dict(os.environ,env,clear=True), \
                 patch.object(CleanerWbSource,'from_env',return_value=source), \
                 patch.object(stage_e,'fetch_current_card',side_effect=lambda nm_id:dict(card,subject_id=1571,characteristics=list(reversed(characteristics)))), \
                 patch.object(stage_e,'KeywordCleaner',side_effect=lambda *args,**kwargs:original_cleaner(*args,clock=clock,**kwargs)):
                admitted=[dict(advert_id=11,nm_id=101,state='verified',campaign_name='Реальная тестовая кампания')]
                web=CleanerWeb(service,generation='monolith',approved_targets=admitted)
                web.worker_status=lambda:'ready'
                web.worker_alive=lambda:True
                entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=box.runtime,runtime=runtime,now_factory=lambda:NOW,cleaner_web=web,
                                                       ads_block=_build_ads_block(runtime,box.runtime,FakePromotionSource(),write_enabled=False))
                password_hash=_password_hash(PASSWORD)
                entrypoint.handle_sheet_vitrina_user_create_request(dict(user_id='fixture-reader',username='reader',display_name='reader',role='operator',allowed_sections=['ads'],manage_users=False,password_hash=password_hash,is_active=True,created_at='2026-09-27T09:00:00Z',updated_at='2026-09-27T09:00:00Z'))
                config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=port,upload_path='/v1/registry-upload',sheet_plan_path='/v1/sheet-vitrina-v1/plan',sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',sheet_status_path='/v1/sheet-vitrina-v1/status',sheet_operator_ui_path='/v1/sheet-vitrina-v1/operator',runtime_dir=box.runtime)
                server=build_registry_upload_http_server(config,entrypoint=entrypoint)
                server.RequestHandlerClass.log_message=lambda *args:None
                fixture=Fixture();fixture.runtime_dir=box.runtime;fixture.cleaner=service;fixture.web=web;fixture.server=server
                fixture.base_url='http://127.0.0.1:'+str(server.server_port)
                fixture.url=fixture.base_url+DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=ads&ads_tab=keyword-cleaner'
                stop=threading.Event();errors=[]
                adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
                parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted,bootstrap_owner_username='owner')
                child=ManualCleanerCoordinator(service,adapter,bootstrap_owner_username='owner')
                def worker():
                    while not stop.is_set():
                        try:
                            if parent.pending_batches():parent.tick()
                            if child.pending_jobs():child.tick()
                        except Exception as exc:
                            errors.append(repr(exc))
                        stop.wait(0.3)
                server_thread=threading.Thread(target=server.serve_forever,daemon=True)
                worker_thread=threading.Thread(target=worker,daemon=True)
                server_thread.start();worker_thread.start()
                try:
                    fixture.owner=fixture.login()
                    yield fixture,fake,errors
                finally:
                    stop.set();server.shutdown();server.server_close();server_thread.join(5);worker_thread.join(5)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve',action='store_true',required=True)
    parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args()
    with running_integration_fixture(args.port) as (fixture,fake,errors):
        print(json.dumps(dict(url=fixture.url,username='owner',password=PASSWORD,synthetic_wb=True,flow='Нажмите «Массовая чистка», запустите выбранную пару, дождитесь реального readback FakeWB, откройте детали и CSV.'),ensure_ascii=False),flush=True)
        try:threading.Event().wait()
        except KeyboardInterrupt:pass
        if errors:print(json.dumps(dict(worker_errors=errors),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
