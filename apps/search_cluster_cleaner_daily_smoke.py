#!/usr/bin/env python3
"""Synthetic daily scheduler, owner authority, queue and restart smoke."""
from __future__ import annotations

from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator
from packages.application.search_cluster_cleaner_daily import DailyCleanerScheduler,history,policy_ready,save_schedules,schedules
from apps.search_cluster_cleaner_self_service_worker import ready_cycle
from packages.contracts.search_cluster_cleaner import CleanerError,Principal,Target
from packages.domain.search_cluster_sources import union_snapshot

NOW=datetime(2026,9,27,22,47,tzinfo=timezone.utc)  # 03:47 EKT


class Source:
    def __init__(self,targets,errors=(),statuses=None):
        self.targets=targets;self.errors=list(errors);self.statuses=statuses or {t.advert_id:t.status for t in targets};self.calls=0
    def catalog(self,*,with_statuses=False):
        self.calls+=1
        return self.targets,self.errors,self.statuses
    def monotonic(self):return 0.0
    def _adverts(self,ids,deadline,*,strict=True):
        rows=[t for t in self.targets if t.advert_id in ids]
        return rows if strict else (rows,[])


def provision(box):
    preview=box.execute('preview')
    box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
    cleaner=box.service()
    ChangeRegistryRepository(box.runtime).initialize_schema()
    (box.runtime/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=1)))
    return cleaner


def check() -> None:
    with Sandbox() as box:
        cleaner=provision(box)
        maintenance=box.runtime/'.business-data-maintenance.json'
        for phase in ('holding','prepared','held','restoring','unknown'):
            maintenance.write_text(json.dumps(dict(phase=phase)))
            assert policy_ready(box.runtime)[0] is False,phase
        maintenance.write_text(json.dumps(dict(phase='restored')))
        assert policy_ready(box.runtime)[0] is True

    with Sandbox() as box:
        cleaner=provision(box)
        owner=Principal('owner',True,True,True)
        stranger=Principal('reader',True,True,True)
        assert schedules(cleaner,owner,'monolith')['schedules'][0]['time']=='03:45'
        try:save_schedules(cleaner,stranger,'monolith',dict(request_id='reader-schedule-1',expected_revision=1,schedules=[]))
        except CleanerError:pass
        else:raise AssertionError('non-owner edited schedules')
        slots=[dict(id='default',time='03:45',enabled=True),dict(id='later',time='03:46',enabled=True)]
        save_schedules(cleaner,owner,'monolith',dict(request_id='owner-schedules-1',expected_revision=1,schedules=slots))
        targets=[Target(11,101,name='Active CPM',contract_verified=True),
                 Target(12,101,name='Paused CPM',status=11,contract_verified=True)]
        source=Source(targets,errors=['adverts_missing:90'],statuses={11:9,12:11,90:7})
        admitted=[dict(advert_id=11,nm_id=101,state='verified')]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:NOW)
        first=scheduler.tick()
        assert first['selected_count']==1,first
        assert source.calls==1
        assert scheduler.tick()['state']=='waiting_for_queue'
        rows=history(cleaner,owner)['items']
        assert len(rows)==2 and {x['state'] for x in rows}=={'queued','pending'},rows
        # Restart with the same slot identity must not create a duplicate batch.
        restarted=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:NOW)
        assert restarted.tick()['state']=='waiting_for_queue'
        with cleaner.store.read() as c:
            assert c.execute('SELECT count(*) FROM cleaner_daily_occurrences WHERE account=?',(cleaner.key,)).fetchone()[0]==2
            assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-batches'",(cleaner.key,)).fetchone()[0]==1
        parent=BatchCleanerCoordinator(cleaner,generation='monolith',source_factory=lambda:source,fixture_admission=admitted)
        child=parent.tick()
        assert child['items'][0]['job_id']
        # A completed child frees the same queue; no second batch overlaps it.
        job=cleaner.manual_job(child['items'][0]['job_id'],owner)
        claimed=cleaner.claim_exact_manual_run(run_id=job['scan_run_id'],targets=[targets[0]],generation='monolith',production_operation_id='synthetic-daily-scan')
        observed=cleaner.clock()
        snapshot=union_snapshot(targets[0],list_entry=dict(active=[],excluded=[],archived=[]),stats_queries=[],minus_queries=[],
                                observed_at=observed,source_times={key:observed for key in ('list','statistics','minus')})
        cleaner.record_snapshot(job['scan_run_id'],claimed['worker_token'],'monolith',snapshot,manual_only=True)
        cleaner.finish_run(job['scan_run_id'],claimed['worker_token'],'monolith',manual_only=True)
        cleaner.record_manual_job(job['job_id'],state='no_change',stage='finished')
        parent.tick();parent.tick()
        second=restarted.tick()
        assert second['selected_count']==1,second
        with cleaner.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-batches'",(cleaner.key,)).fetchone()[0]==2
        # Disabling a time preserves in-flight work but prevents a fresh intent.
        revision=schedules(cleaner,owner,'monolith')['revision']
        save_schedules(cleaner,owner,'monolith',dict(request_id='owner-schedules-2',expected_revision=revision,
                       schedules=[dict(id='default',time='03:45',enabled=False)]))
        assert len(history(cleaner,owner)['items'])==2

    with Sandbox() as box:
        cleaner=provision(box);owner=Principal('owner',True,True,True)
        save_schedules(cleaner,owner,'monolith',dict(request_id='close-slots',expected_revision=1,
                       schedules=[dict(id='first',time='03:45',enabled=True),dict(id='second',time='03:46',enabled=True)]))
        target=Target(11,101,name='Active CPM',contract_verified=True)
        admitted=[dict(advert_id=11,nm_id=101,state='verified')]
        from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
        cleaner.start_manual_batch(dict(request_id='manual-long-batch',selected_categories=['active'],
            targets=[dict(advert_id=11,nm_id=101)]),owner,
            snapshot=eligibility_rows(cleaner,'monolith',[target],fixture_admission=admitted))
        current=[datetime(2026,9,26,22,45,30,tzinfo=timezone.utc)]
        source=Source([target])
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:current[0])
        class IdleChild:
            def pending_jobs(self):return []
            def tick(self):raise AssertionError('no child job')
        class ManualBusy:
            def pending_batches(self):return ['manual-long-batch']
            def tick(self):return None
        assert ready_cycle(IdleChild(),ManualBusy(),scheduler)
        current[0]=datetime(2026,9,26,22,46,30,tzinfo=timezone.utc)
        assert ready_cycle(IdleChild(),ManualBusy(),scheduler)
        current[0]=datetime(2026,9,26,23,30,tzinfo=timezone.utc)  # 04:30 EKT
        assert ready_cycle(IdleChild(),ManualBusy(),scheduler)
        rows=history(cleaner,owner)['items']
        assert len(rows)==2 and all(r['state']=='pending' for r in rows) and source.calls==0,rows
        cleaner.record_manual_batch('manual-long-batch',state='complete',stage='finished',current_index=1)
        first=scheduler.tick()
        assert first['selected_count']==1 and source.calls==1,first
        cleaner.record_manual_batch(first['batch_id'],state='complete',stage='finished',current_index=1)
        second=scheduler.tick()
        assert second['selected_count']==1 and source.calls==2 and second['batch_id']!=first['batch_id'],second
        with cleaner.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-batches'",(cleaner.key,)).fetchone()[0]==3

    with Sandbox() as box:
        cleaner=provision(box)
        # Bootstrap owner may differ from the configured cleaner owner.
        bootstrap=Principal('operator',True,True,True,site_owner=True)
        save_schedules(cleaner,bootstrap,'monolith',dict(request_id='bootstrap-schedule-1',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        source=Source([Target(11,101,name='Active CPM',contract_verified=True)])
        admitted=[dict(advert_id=11,nm_id=101,state='verified')]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:NOW,bootstrap_owner_username='operator')
        assert scheduler.tick()['selected_count']==1
        assert history(cleaner,bootstrap)['items'][0]['state']=='queued'

    with Sandbox() as box:
        cleaner=provision(box);owner=Principal('owner',True,True,True)
        save_schedules(cleaner,owner,'monolith',dict(request_id='owner-schedules-3',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        (box.runtime/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=False,revision=2)))
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',now=lambda:NOW)
        assert scheduler.tick()['state']=='skipped'
        assert scheduler.tick() is None
        assert history(cleaner,owner)['items'][0]['discovered_campaigns'] is None

    with Sandbox() as box:
        cleaner=provision(box);owner=Principal('owner',True,True,True)
        with cleaner.store.transaction() as c:
            c.execute('DROP TABLE cleaner_daily_occurrences')
            c.execute('DROP TABLE cleaner_daily_schedules')
            c.execute('UPDATE cleaner_schema SET version=2 WHERE singleton=1')
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',now=lambda:NOW)
        assert scheduler.tick() is None and scheduler.reconcile_finished() is None
        assert not schedules(cleaner,owner,'monolith')['can_edit']
        assert history(cleaner,owner)['available'] is False
        try:save_schedules(cleaner,owner,'monolith',dict(request_id='v2-denied',expected_revision=1,schedules=[]))
        except CleanerError as exc:assert exc.code=='schedule_schema_required'
        else:raise AssertionError('v2 unexpectedly accepted a schedule mutation')

    with Sandbox() as box:
        cleaner=provision(box);owner=Principal('owner',True,True,True)
        save_schedules(cleaner,owner,'monolith',dict(request_id='busy-schedules',expected_revision=1,
                       schedules=[dict(id='late',time='23:58',enabled=True)]))
        target=Target(11,101,name='Active CPM',contract_verified=True)
        admitted=[dict(advert_id=11,nm_id=101,state='verified')]
        from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
        cleaner.start_manual_batch(dict(request_id='busy-manual-batch-0001',selected_categories=['active'],
            targets=[dict(advert_id=11,nm_id=101)]),owner,
            snapshot=eligibility_rows(cleaner,'monolith',[target],fixture_admission=admitted))
        current=[datetime(2026,9,28,18,59,tzinfo=timezone.utc)]  # 23:59 EKT
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:Source([target]),
                                        fixture_admission=admitted,now=lambda:current[0])
        class IdleChild:
            def pending_jobs(self):return []
            def tick(self):raise AssertionError('no child job')
        class ManualBusy:
            def pending_batches(self):return ['busy-manual-batch-0001']
            def tick(self):return None
        assert ready_cycle(IdleChild(),ManualBusy(),scheduler)
        assert history(cleaner,owner)['items'][0]['state']=='pending'
        current[0]=datetime(2026,9,28,19,30,tzinfo=timezone.utc)  # 00:30 next EKT day
        cleaner.record_manual_batch('busy-manual-batch-0001',state='complete',stage='finished',current_index=1)
        class IdleBatch:
            def pending_batches(self):return []
            def tick(self):raise AssertionError('no old batch')
        assert ready_cycle(IdleChild(),IdleBatch(),scheduler)
        result=history(cleaner,owner)['items'][0]
        assert result['local_date']=='2026-09-28' and result['state']=='queued',result


def integrated_flow() -> None:
    """One real guarded batch against loopback FakeWB, including readback."""
    from apps.search_cluster_cleaner_reconcile_integration_fixture import running_integration_fixture
    from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
    with running_integration_fixture() as (fixture,fake,errors):
        cleaner=fixture.cleaner
        owner=Principal('owner',True,True,True,site_owner=True)
        (fixture.runtime_dir/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=1)))
        reader=fixture.login('reader')
        assert fixture.request('/daily-schedules',opener=reader)[0]==200
        command=dict(request_id='daily-integration-schedule-1',expected_revision=1,
                     schedules=[dict(id='default',time='03:45',enabled=True)])
        assert fixture.request('/daily-schedules',command,opener=reader)[0]==403
        status,result,_=fixture.request('/daily-schedules',command)
        assert status==202 and result['revision']==2,(status,result)
        assert fixture.request('/daily-schedules')[1]['schedules'][0]['enabled'] is True
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:CleanerWbSource.from_env(cleaner.account),
                                        fixture_admission=fixture.web._fixture_approved_targets,now=lambda:NOW,
                                        bootstrap_owner_username='owner')
        assert scheduler.tick()['selected_count']==1
        deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            result=history(cleaner,owner)['items'][0]
            if result['state'] in {'complete','partial','failed'}:break
            time.sleep(.25)
        else:raise AssertionError(f'scheduled batch did not finish: {result}; worker errors={errors}')
        assert scheduler.reconcile_finished()['state']=='complete'
        result=history(cleaner,owner)['items'][0]
        assert (result['discovered_campaigns'],result['checked_campaigns'],result['checked_keys'],
                result['excluded'],result['returned'])==(1,1,5,2,1),result
        assert not errors,errors
        status,public_history,_=fixture.request('/daily-schedules/history',opener=reader)
        assert status==200 and public_history['items'][0]['checked_keys']==5
        assert scheduler.tick() is None


if __name__=='__main__':
    check();integrated_flow();print('search cluster cleaner daily smoke: ok')
