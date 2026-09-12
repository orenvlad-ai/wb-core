#!/usr/bin/env python3
"""Real loopback HTTP: session/owner/CSRF, durable commands and WB wait isolation."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time
import urllib.request
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.search_cluster_cleaner_web_fixture import running_fixture, OWNER, GENERATION, FIXTURE_NOW, PROFILE
from packages.application.search_cluster_cleaner_worker import CleanerWorker
from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.application.business_data_write_barrier import acquire_barrier
from packages.contracts.search_cluster_cleaner import Target
from packages.domain.search_cluster_sources import union_snapshot


def run():
    checks=[]
    def check(name, condition):
        assert condition, name
        checks.append(name)
    with running_fixture('confirmed') as f:
        summary=f.request('/summary')[1]
        check('D_connected_transport_and_counts',summary['transport_enabled'] and summary['confirmed']==dict(automatic=2,manual=1,late_automatic=0,late_manual=1))
        check('D_original_scan_unchanged',summary['last_scan']['summary']['confirmed_automatic']==2 and summary['last_scan']['summary']['confirmed_manual']==0)
        history=f.request('/history')[1]['items']
        check('D_late_manual_history',any(e['kind']=='late_confirmation' and e['facts']['confirmed_manual']==1 for e in history))
    with running_fixture() as f:
        s=f.request('/summary')[1]
        check('ordinary_saved_summary',s['pending_count']==3 and s['last_scan']['state']=='complete')
        check('private_no_store',f.request('/summary')[2].get('Cache-Control')=='private, no-store')
        count=f.count('cleaner_requests');reader=f.login('reader');admin=f.login('admin');noads=f.login('noads')
        command=dict(request_id='http-read-only-user',expected_revision=s['settings']['revision'],schedule_time='08:00')
        check('unauthenticated_denied',f.request('/summary',opener=urllib.request.build_opener())[0]==401)
        check('reader_read_allowed',f.request('/summary',opener=reader)[0]==200)
        check('reader_direct_mutation_denied',f.request('/settings',command,opener=reader)[0]==403)
        check('admin_not_owner_denied',f.request('/settings',command,opener=admin)[0]==403)
        check('ads_permission_required',f.request('/summary',opener=noads)[0]==403)
        cases=[{'Origin':None},{'Origin':'https://foreign.example'},{'Sec-Fetch-Site':'cross-site'},{'Sec-Fetch-Site':'same-site'},{'X-WB-Keyword-Cleaner-CSRF':None},{'Content-Type':'text/plain'},{'Origin':'https://spoof.example','X-Forwarded-Host':'spoof.example','X-Forwarded-Proto':'https','Host':'spoof.example'}]
        for i,headers in enumerate(cases):
            check('csrf_case_'+str(i),f.request('/settings',command,headers=headers)[0]==403)
        check('security_no_saved_commands',f.count('cleaner_requests')==count)
        for field in ['actor','owner','account','seller_id','account_scope']:
            check('client_identity_'+field+'_denied',f.request('/runs',dict(request_id='http-spoof-identity',**{field:'owner'}))[0]==422)
        code,result,_=f.request('/settings',command)
        check('settings_202',code==202 and result['schedule_time']=='08:00')
        check('same_request_replays',f.request('/settings',command)[1]==result and f.count('cleaner_requests')==count+1)
        check('digest_conflict_409',f.request('/settings',dict(command,schedule_time='09:00'))[0]==409)
        check('stale_revision_409',f.request('/settings',dict(command,request_id='http-stale-revision'))[0]==409)
        check('request_recovery_owner',f.request('/requests/'+command['request_id'])[1]==result)
        check('request_recovery_other_actor_hidden',f.request('/requests/'+command['request_id'],opener=reader)[0]==404)
        item=f.request('/reviews')[1]['items'][0]
        before=f.count('cleaner_manual_overrides')
        decision=dict(request_id='http-decision-0001',decision='allow',expected_revision=item['revision'])
        with f.cleaner.store.transaction() as c:
            c.execute("UPDATE cleaner_observations SET observed_state='excluded' WHERE review_id=?",(item['review_id'],))
        code,result,_=f.request('/reviews/'+item['review_id']+'/decision',decision)
        check('historically_excluded_allow_never_returns',code==202 and bool(result['already_excluded']) and f.count('cleaner_write_operations')==0)
        check('decision_saved_once',f.request('/reviews/'+item['review_id']+'/decision',decision)[0]==202 and f.count('cleaner_manual_overrides')==before+1)
        check('stale_review_409',f.request('/reviews/'+item['review_id']+'/decision',dict(decision,request_id='http-stale-review'))[0]==409)
        new_profile={k:v for k,v in PROFILE.items() if k not in {'nm_id','version'}}
        code,draft,_=f.request('/profiles/102/versions',dict(request_id='http-profile-draft',expected_revision=0,profile=new_profile))
        check('draft_inactive',code==202 and f.request('/profiles/102')[1]['active_version'] is None)
        check('activate_exact_version',f.request('/profiles/102/activate',dict(request_id='http-profile-active',expected_revision=draft['profile_revision'],version=draft['profile_version']))[0]==202)
        check('profile_activation_revision_conflict',f.request('/profiles/102/activate',dict(request_id='http-profile-stale',expected_revision=draft['profile_revision'],version=draft['profile_version']))[0]==409)
        with patch.dict(os.environ,{'WB_CORE_WEB_AUTH_USERNAME':'','WB_CORE_WEB_AUTH_PASSWORD_HASH':'','WB_CORE_WEB_AUTH_SESSION_SECRET':'','WB_CORE_WEB_AUTH_REQUIRED':'0'}):
            check('auth_disabled_closed',f.request('/settings',command)[0]==403 and f.request('/summary')[0]==403)
        original_owner=f.cleaner.owner_username;f.cleaner.owner_username=''
        check('owner_missing_off',not f.request('/summary')[1]['settings']['enabled'] and f.request('/settings',command)[0]==403)
        f.cleaner.owner_username=original_owner
        f.web.generation='other-generation'
        check('generation_mismatch_off',not f.request('/summary')[1]['settings']['enabled'] and f.request('/runs',dict(request_id='http-generation-run'))[0]==409)
        f.web.generation=GENERATION
        check('patch_delete_are_405',all(f.request('/settings',{},method=method)[0]==405 for method in ('PATCH','DELETE')))
        # No GET may install/migrate tables or call the worker/read source.
        with patch.object(CleanerStore,'initialize',side_effect=AssertionError('GET initialized schema')):
            check('get_does_not_initialize',f.request('/summary')[0]==200)
        entered=threading.Event();ended=threading.Event()
        class SlowRead:
            def catalog(self):return [Target(10101,101,contract_verified=True)],[]
            def snapshot(self,target):
                entered.set();time.sleep(30);ended.set()
                return union_snapshot(target,list_entry=dict(active=[],excluded=[],archived=[]),stats_queries=[],minus_queries=[],observed_at=FIXTURE_NOW,source_times={s:FIXTURE_NOW for s in ('list','statistics','minus')})
        code,run,_=f.request('/runs',dict(request_id='http-slow-source-run'))
        check('manual_run_202',code==202)
        check('parallel_click_reuses_slot',f.request('/runs',dict(request_id='http-second-click'))[1]['run_id']==run['run_id'])
        worker_result=[];worker=threading.Thread(target=lambda:worker_result.append(CleanerWorker(f.cleaner,SlowRead(),generation=GENERATION).tick()))
        worker.start();assert entered.wait(5)
        durations=[]
        for _ in range(100):
            start=time.perf_counter();code,body,_=f.request('/summary');durations.append((time.perf_counter()-start)*1000);assert code==200 and body['current_work']
        check('100_http_gets_finish_before_30s_source',not ended.is_set())
        p95=statistics.quantiles(durations,n=100)[94]
        check('http_summary_p95_at_most_500ms',p95<=500)
        worker.join(timeout=35);check('fixture_worker_finished',not worker.is_alive() and bool(worker_result))
        count=f.count('cleaner_requests')
        acquire_barrier(f.runtime_dir,window_id='cleaner-c-fixture',window_kind='snapshot',plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic-fixture',actor='fixture',reason='HTTP barrier test')
        check('maintenance_post_423_no_mutation',f.request('/settings',dict(request_id='http-maintenance',expected_revision=f.request('/summary')[1]['settings']['revision'],enabled=False))[0]==423 and f.count('cleaner_requests')==count)
        check('maintenance_get_200',f.request('/summary')[0]==200)
    with running_fixture('unready') as f:
        s=f.request('/summary')[1]
        check('no_baseline_stays_off',not s['settings']['enabled'] and f.request('/settings',dict(request_id='http-unready-on',expected_revision=1,enabled=True))[0]==409)
        f.entrypoint.cleaner_web=CleanerWeb()
        count=f.count('cleaner_requests')
        check('no_configuration_safe_get',f.request('/summary')[1]['configuration']['configured'] is False and f.count('cleaner_requests')==count)
    return dict(passed=len(checks),checks=checks,http_get_count=100,source_wait_seconds=30,http_roundtrip_p95_ms=round(p95,3),http_roundtrip_max_ms=round(max(durations),3),wb_writes=0,synthetic_wb_posts=2)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);args=parser.parse_args();result=run();print(json.dumps(result,ensure_ascii=False,indent=2))
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
