#!/usr/bin/env python3
"""A complete scan with exact WB minus drift holds one pair, not its frozen batch tail."""
from __future__ import annotations

import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from apps.search_cluster_cleaner_batch_smoke import Source, rejects
from packages.application.search_cluster_cleaner import batch_child_id
from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator, batch_status, _drift_only_scan
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.contracts.search_cluster_cleaner import Principal,Target,digest
from packages.domain.search_cluster_sources import union_snapshot


def main():
    query='аксессуары для iphone 16 pro max'
    with Sandbox() as box:
        box.package['rows']=[dict(advert_id=114,nm_id=101,query=query,decision='allow',observed_state='excluded',provenance={'approved':'synthetic'})]
        box.package['rows_digest']='sha256:'+digest(box.package['rows'])
        box.write_package()
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=box.service();owner=Principal('owner',True,True,True);other=Principal('other',True,True,True)
        ids=list(range(100,126));targets=[Target(i,101,name='Campaign '+str(i),contract_verified=True) for i in ids]
        admitted=[dict(advert_id=i,nm_id=101,state='verified') for i in ids]
        source=Source(targets)
        frozen=eligibility_rows(service,'monolith',source.targets,fixture_admission=admitted)
        batch_id='synthetic-drift-batch-0001'
        service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],targets=[dict(advert_id=i,nm_id=101) for i in ids]),owner,snapshot=frozen)
        parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        # Fourteen exact children completed before a historical minus-list drift.
        for index in range(14):
            item=parent.tick()['items'][index]
            assert item['job_id']==batch_child_id(batch_id,index)
            child=service.manual_job(item['job_id'],owner)
            assert service.stop_unsubmitted_manual_run(child['scan_run_id'])
            service.record_manual_job(child['job_id'],state='no_change',stage='finished',result='Нет новых кандидатов')
            assert parent.tick()['current_index']==index+1
        item=parent.tick()['items'][14];child=service.manual_job(item['job_id'],owner)
        run=service.claim_exact_manual_run(run_id=child['scan_run_id'],targets=[targets[14]],generation='monolith',production_operation_id='synthetic-drift-scan-op')
        now=service.clock()
        snapshot=union_snapshot(targets[14],list_entry=dict(active=[query],excluded=[],archived=[]),stats_queries=[query],minus_queries=[],observed_at=now,source_times={name:now for name in ('list','statistics','minus')})
        assert snapshot.complete
        service.record_snapshot(run['run_id'],run['worker_token'],'monolith',snapshot,manual_only=True)
        ended=service.finish_run(run['run_id'],run['worker_token'],'monolith',manual_only=True)
        assert ended['state']=='partial' and ended['summary']['pairs']==1 and ended['summary']['dry_run']
        service.record_manual_job(child['job_id'],state='failed',stage='finished',error_code='scan_partial',error='scan_partial',can_recheck=False)
        failed=service.manual_job(child['job_id'],owner)
        with service.store.read() as c:
            proof=_drift_only_scan(c,service,failed,frozen[14])
            assert proof and len(proof['queries'])==1,proof
            assert proof['queries'][0]['verdict']=='allow' and proof['queries'][0]['rule_id']=='APPROVED_BASELINE',proof
            assert c.execute("SELECT count(*) FROM cleaner_events WHERE run_id=? AND kind='external_state_drift'",(run['run_id'],)).fetchone()[0]==2
            assert not c.execute('SELECT 1 FROM cleaner_write_operations WHERE run_id=?',(run['run_id'],)).fetchone()
            assert _drift_only_scan(c,service,dict(failed,write_run_id='dispatched-op'),frozen[14]) is None
        # Old release marked the entire untouched tail skipped. Preserve this
        # immutable terminal event and explicitly continue via owner command.
        original=service.manual_batch_snapshot(batch_id,owner)
        parent._stop(original,14,code='scan_partial',message='scan_partial')
        before=batch_status(service,batch_id,owner)
        assert before['state']=='partial' and before['can_resume'] and before['resume_index']==14,before
        assert (before['done_count'],before['confirmed_count'],before['no_change_count'],before['not_started_count'],before['held_count'])==(15,0,14,11,1),before
        assert before['completed_count']==14
        assert before['failed_count']==0 and before['skipped_count']==0
        assert before['items'][14]['error_code']=='external_state_drift'
        assert before['items'][15]['error_code']=='not_started_after_failure'
        assert before['items'][14]['drift_queries'][0]['verdict']=='allow'
        requests_before=[json.dumps(service.manual_job(batch_child_id(batch_id,i),owner),sort_keys=True) for i in range(14)]
        rejects(lambda:service.resume_drift_batch(batch_id,dict(request_id='synthetic-drift-resume-other-0001'),other),'owner_required')
        bootstrap_same_name=Principal('owner',True,True,True,site_owner=True)
        rejects(lambda:service.resume_drift_batch(batch_id,dict(request_id='synthetic-drift-resume-wrong-authority-0001'),bootstrap_same_name),'batch_resume_unavailable')
        response=service.resume_drift_batch(batch_id,dict(request_id='synthetic-drift-resume-0001'),owner)
        assert response['current_index']==15 and response['review_required_target']=='114:101'
        assert service.resume_drift_batch(batch_id,dict(request_id='synthetic-drift-resume-0001'),owner)==response
        rejects(lambda:service.resume_drift_batch(batch_id,dict(request_id='synthetic-drift-resume-0002'),owner),'batch_resume_unavailable')
        saved=service.manual_batch_snapshot(batch_id,owner)
        assert saved['item_updates']['14']['review_required']
        assert not any(str(i) in saved['item_updates'] for i in range(15,26))
        assert [json.dumps(service.manual_job(batch_child_id(batch_id,i),owner),sort_keys=True) for i in range(14)]==requests_before
        with service.store.read() as c:
            kinds=[row[0] for row in c.execute("SELECT kind FROM cleaner_events WHERE account=? AND json_extract(facts,'$.batch_id')=? AND kind LIKE 'self_service_batch_%' ORDER BY sequence",(service.key,batch_id))]
        assert kinds[-1]=='self_service_batch_resumed' and 'self_service_batch_stage' in kinds[:-1]
        parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        for index in range(15,26):
            item=parent.tick()['items'][index]
            assert item['job_id']==batch_child_id(batch_id,index)
            current=service.manual_job(item['job_id'],owner)
            assert service.stop_unsubmitted_manual_run(current['scan_run_id'])
            service.record_manual_job(current['job_id'],state='no_change',stage='finished')
            assert parent.tick()['current_index']==index+1
        final=parent.tick()
        assert final['state']=='partial'
        assert (final['done_count'],final['no_change_count'],final['held_count'],final['not_started_count'])==(26,25,1,0),final
        assert final['completed_count']==25
        assert final['failed_count']==0 and final['skipped_count']==0
        assert not final['can_resume'] and final['items'][14]['review_required']
        assert not parent.pending_batches()
    with Sandbox() as box:
        # New incidents advance past only a certified scan-only drift; they
        # never need an explicit repair of the old terminal journal.
        box.package['rows']=[dict(advert_id=11,nm_id=101,query=query,decision='allow',observed_state='excluded',provenance={'approved':'synthetic'})]
        box.package['rows_digest']='sha256:'+digest(box.package['rows'])
        box.write_package()
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=box.service();owner=Principal('owner',True,True,True)
        targets=[Target(i,101,name='Campaign '+str(i),contract_verified=True) for i in (11,12)]
        source=Source(targets);admitted=[dict(advert_id=i,nm_id=101,state='verified') for i in (11,12)]
        frozen=eligibility_rows(service,'monolith',source.targets,fixture_admission=admitted)
        batch_id='synthetic-future-drift-batch-0001'
        service.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],targets=[dict(advert_id=i,nm_id=101) for i in (11,12)]),owner,snapshot=frozen)
        parent=BatchCleanerCoordinator(service,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        child=service.manual_job(parent.tick()['items'][0]['job_id'],owner)
        run=service.claim_exact_manual_run(run_id=child['scan_run_id'],targets=[targets[0]],generation='monolith',production_operation_id='synthetic-future-drift-op')
        now=service.clock()
        active=union_snapshot(targets[0],list_entry=dict(active=[query],excluded=[],archived=[]),stats_queries=[query],minus_queries=[],observed_at=now,source_times={name:now for name in ('list','statistics','minus')})
        service.record_snapshot(run['run_id'],run['worker_token'],'monolith',active,manual_only=True)
        assert service.finish_run(run['run_id'],run['worker_token'],'monolith',manual_only=True)['state']=='partial'
        service.record_manual_job(child['job_id'],state='failed',stage='finished',error_code='scan_partial',can_recheck=False)
        advanced=parent.tick()
        assert advanced['state']=='running' and advanced['current_index']==1 and advanced['items'][0]['review_required']
        second=service.manual_job(parent.tick()['items'][1]['job_id'],owner)
        assert service.stop_unsubmitted_manual_run(second['scan_run_id'])
        service.record_manual_job(second['job_id'],state='no_change',stage='finished')
        parent.tick();final=parent.tick()
        assert final['state']=='partial' and final['done_count']==2 and final['held_count']==1
    with Sandbox() as box:
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        service=box.service();owner=Principal('owner',True,True,True)
        target=Target(11,101,name='Incomplete',contract_verified=True)
        child=service.start_manual_clean(dict(request_id='synthetic-unrelated-partial-0001',advert_id=11,nm_id=101),owner)
        run=service.claim_exact_manual_run(run_id=child['run_id'],targets=[target],generation='monolith',production_operation_id='synthetic-other-scan-op')
        now=service.clock()
        incomplete=union_snapshot(target,list_entry=None,stats_queries=[],minus_queries=[],observed_at=now,source_times={})
        service.record_snapshot(run['run_id'],run['worker_token'],'monolith',incomplete,manual_only=True)
        assert service.finish_run(run['run_id'],run['worker_token'],'monolith',manual_only=True)['state']=='partial'
        service.record_manual_job(child['job_id'],state='failed',stage='finished',error_code='scan_partial',can_recheck=False)
        with service.store.read() as c:
            assert _drift_only_scan(c,service,service.manual_job(child['job_id'],owner),dict(advert_id=11,nm_id=101)) is None
    print('search_cluster_cleaner_drift_batch_smoke: PASS')

if __name__=='__main__':main()
