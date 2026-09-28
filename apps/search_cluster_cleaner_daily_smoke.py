#!/usr/bin/env python3
"""Synthetic daily scheduler, owner authority, queue and restart smoke."""
from __future__ import annotations

from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator
from packages.application.search_cluster_cleaner_daily import ZONE,DailyCleanerScheduler,history,policy_ready,save_schedules,schedules
from apps.search_cluster_cleaner_self_service_worker import armed_cycle,heartbeat_while_busy,ready_cycle,report_health
from packages.contracts.search_cluster_cleaner import CleanerError,Principal,Target
from packages.domain.search_cluster_sources import union_snapshot

NOW=datetime(2026,9,27,22,46,tzinfo=timezone.utc)  # 03:46 EKT; both close slots are in the first window.


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
    cleaner.clock=lambda:'2026-09-26T22:00:00+00:00'  # Before synthetic due slots.
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
        current=[NOW]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:current[0])
        first=scheduler.tick()
        assert first['selected_count']==1,first
        assert source.calls==1
        assert scheduler.tick() is None
        rows=history(cleaner,owner)['items']
        assert len(rows)==2 and {x['state'] for x in rows}=={'queued','start_wait'},rows
        # Restart with the same slot identity must not create a duplicate batch.
        restarted=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:current[0])
        assert restarted.tick() is None
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
        current[0]=datetime(2026,9,27,23,0,10,tzinfo=timezone.utc)  # First slot +15 min.
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
        cleaner.clock=lambda:'2026-09-26T23:30:00+00:00'
        revision=schedules(cleaner,owner,'monolith')['revision']
        save_schedules(cleaner,owner,'monolith',dict(request_id='close-slots-add-disabled',expected_revision=revision,
                       schedules=[dict(id='first',time='03:45',enabled=True),dict(id='second',time='03:46',enabled=True),
                                  dict(id='third',time='05:00',enabled=False)]))
        assert all(r['state']=='pending' for r in history(cleaner,owner)['items'])
        cleaner.record_manual_batch('manual-long-batch',state='complete',stage='finished',current_index=1)
        first=scheduler.tick()
        assert first['selected_count']==1 and source.calls==1,first
        cleaner.record_manual_batch(first['batch_id'],state='complete',stage='finished',current_index=1)
        second=scheduler.tick()
        assert second['selected_count']==1 and source.calls==2 and second['batch_id']!=first['batch_id'],second
        with cleaner.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-batches'",(cleaner.key,)).fetchone()[0]==3

    with Sandbox() as box:
        cleaner=provision(box);owner=Principal('owner',True,True,True)
        cleaner.clock=lambda:'2026-09-27T12:00:00+00:00'  # 17:00 EKT, after today's 03:45.
        save_schedules(cleaner,owner,'monolith',dict(request_id='enable-after-due',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        target=Target(11,101,name='Active CPM',contract_verified=True)
        source=Source([target]);current=[datetime(2026,9,27,12,1,tzinfo=timezone.utc)]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=[dict(advert_id=11,nm_id=101,state='verified')],now=lambda:current[0])
        assert scheduler.observe_deployment_blocked()==0
        assert scheduler.tick() is None
        assert history(cleaner,owner)['items']==[] and source.calls==0
        with cleaner.store.read() as c:
            activated_at=c.execute('SELECT created_at FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',
                                   (cleaner.key,'default')).fetchone()[0]
        cleaner.clock=lambda:'2026-09-27T22:45:20+00:00'  # After tomorrow's due, within its first window.
        save_schedules(cleaner,owner,'monolith',dict(request_id='add-other-slot-after-due',expected_revision=2,
                       schedules=[dict(id='default',time='03:45',enabled=True),dict(id='other',time='04:30',enabled=False)]))
        with cleaner.store.read() as c:
            assert c.execute('SELECT created_at FROM cleaner_daily_schedules WHERE account=? AND schedule_id=?',
                             (cleaner.key,'default')).fetchone()[0]==activated_at
        current[0]=datetime(2026,9,27,22,45,30,tzinfo=timezone.utc)
        assert scheduler.tick()['selected_count']==1
        rows=history(cleaner,owner)['items']
        assert len(rows)==1 and rows[0]['local_date']=='2026-09-28' and rows[0]['state']=='queued',rows

    with Sandbox() as box:
        cleaner=provision(box)
        # Bootstrap owner may differ from the configured cleaner owner.
        bootstrap=Principal('operator',True,True,True,site_owner=True)
        save_schedules(cleaner,bootstrap,'monolith',dict(request_id='bootstrap-schedule-1',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        source=Source([Target(11,101,name='Active CPM',contract_verified=True)])
        admitted=[dict(advert_id=11,nm_id=101,state='verified')]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:datetime(2026,9,27,22,45,30,tzinfo=timezone.utc),bootstrap_owner_username='operator')
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
        current=[datetime(2026,9,28,18,58,30,tzinfo=timezone.utc)]  # 23:58:30 EKT.
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


def deployment_blocked_flow() -> None:
    class ReadOnlyQueue:
        def pending_jobs(self):return []
        def pending_batches(self):return []
        def tick(self):raise AssertionError('armed worker consumed a queue')

    with Sandbox() as box:
        cleaner=provision(box);owner=Principal('owner',True,True,True)
        save_schedules(cleaner,owner,'monolith',dict(request_id='blocked-two-slots',expected_revision=1,
                       schedules=[dict(id='first',time='03:45',enabled=True),dict(id='second',time='03:46',enabled=True)]))
        source=Source([Target(11,101,name='Active CPM',contract_verified=True)])
        current=[datetime(2026,9,26,22,44,tzinfo=timezone.utc)]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=[dict(advert_id=11,nm_id=101,state='verified')],now=lambda:current[0])
        armed_cycle(ReadOnlyQueue(),ReadOnlyQueue(),scheduler)
        assert history(cleaner,owner)['items']==[]
        current[0]=datetime(2026,9,26,22,46,10,tzinfo=timezone.utc)
        armed_cycle(ReadOnlyQueue(),ReadOnlyQueue(),scheduler)
        rows=history(cleaner,owner)['items']
        assert len(rows)==2 and all(r['state']=='deployment_blocked' and 'Выпуск' in r['reason']
                                    and r['checked_campaigns'] is None and r['checked_keys'] is None
                                    and r['excluded'] is None and r['returned'] is None for r in rows),rows
        assert source.calls==0
        original_transaction=cleaner.store.transaction
        cleaner.store.transaction=lambda:(_ for _ in ()).throw(AssertionError('duplicate observation opened RW transaction'))
        try:armed_cycle(ReadOnlyQueue(),ReadOnlyQueue(),scheduler)
        finally:cleaner.store.transaction=original_transaction
        with cleaner.store.read() as c:
            assert c.execute('SELECT count(*) FROM cleaner_daily_occurrences WHERE account=?',(cleaner.key,)).fetchone()[0]==2
            assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-batches'",(cleaner.key,)).fetchone()[0]==0
        current[0]=datetime(2026,9,26,22,47,tzinfo=timezone.utc)
        assert not ready_cycle(ReadOnlyQueue(),ReadOnlyQueue(),scheduler)
        assert source.calls==0 and all('Ожидает повторной' in r['reason'] for r in history(cleaner,owner)['items'])
        current[0]=datetime(2026,9,26,23,0,10,tzinfo=timezone.utc)  # First slot +15 min.
        assert ready_cycle(ReadOnlyQueue(),ReadOnlyQueue(),scheduler)
        rows=history(cleaner,owner)['items']
        assert {r['state'] for r in rows}=={'deployment_blocked','queued'} and source.calls==1,rows
        first=next(r for r in rows if r['state']=='queued')['batch_id']
        assert scheduler.tick() is None and source.calls==1
        cleaner.record_manual_batch(first,state='complete',stage='finished',current_index=1)
        current[0]=datetime(2026,9,26,23,1,10,tzinfo=timezone.utc)  # Second slot +15 min.
        second=scheduler.tick()
        assert second['selected_count']==1 and second['batch_id']!=first and source.calls==2,second
        with cleaner.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-batches'",(cleaner.key,)).fetchone()[0]==2

    with Sandbox() as box:
        cleaner=provision(box);owner=Principal('owner',True,True,True)
        save_schedules(cleaner,owner,'monolith',dict(request_id='blocked-missed-slot',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        source=Source([Target(11,101,name='Active CPM',contract_verified=True)])
        current=[datetime(2026,9,26,22,45,10,tzinfo=timezone.utc)]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        now=lambda:current[0])
        armed_cycle(ReadOnlyQueue(),ReadOnlyQueue(),scheduler)
        current[0]=datetime(2026,9,26,23,10,tzinfo=timezone.utc)
        assert not ready_cycle(ReadOnlyQueue(),ReadOnlyQueue(),scheduler)
        assert history(cleaner,owner)['items'][0]['details']['next_retry_at']=='2026-09-26T23:15:00+00:00'
        current[0]=datetime(2026,9,27,0,47,tzinfo=timezone.utc)  # Beyond +120 min and grace.
        assert not ready_cycle(ReadOnlyQueue(),ReadOnlyQueue(),scheduler)
        row=history(cleaner,owner)['items'][0]
        assert row['state']=='not_started' and 'Окна запуска' in row['reason'] and row['checked_keys'] is None,row
        assert source.calls==0 and scheduler.tick() is None
        with cleaner.store.read() as c:
            assert c.execute('SELECT count(*) FROM cleaner_daily_occurrences WHERE account=?',(cleaner.key,)).fetchone()[0]==1

    with Sandbox() as box:
        cleaner=provision(box);owner=Principal('owner',True,True,True)
        save_schedules(cleaner,owner,'monolith',dict(request_id='blocked-maintenance-slot',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',now=lambda:datetime(2026,9,26,22,46,tzinfo=timezone.utc))
        (box.runtime/'.business-data-maintenance.json').write_text(json.dumps(dict(phase='holding')))
        armed_cycle(ReadOnlyQueue(),ReadOnlyQueue(),scheduler)
        assert history(cleaner,owner)['items']==[]


def finite_retry_regressions() -> None:
    due=datetime(2026,9,26,22,45,tzinfo=timezone.utc)
    target=Target(11,101,name='Active CPM',contract_verified=True)
    admitted=[dict(advert_id=11,nm_id=101,state='verified')]
    owner=Principal('owner',True,True,True)

    with tempfile.TemporaryDirectory() as directory:
        admission=Path(directory)
        report_health(admission,'armed')
        before=json.loads((admission/'self-service-worker-health.json').read_text())['updated_at']
        stop=threading.Event()
        heartbeat=threading.Thread(target=heartbeat_while_busy,args=(admission,stop,'armed'),daemon=True)
        heartbeat.start()
        time.sleep(2.2)
        stop.set();heartbeat.join(2)
        after=json.loads((admission/'self-service-worker-health.json').read_text())
        assert after['state']=='armed' and after['updated_at']>before and not heartbeat.is_alive(),after

    with Sandbox() as box:
        cleaner=provision(box)
        save_schedules(cleaner,owner,'monolith',dict(request_id='late-process-schedule',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        source=Source([target]);current=[due+timedelta(minutes=70)]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:current[0])
        assert scheduler.observe_deployment_blocked()==1
        row=history(cleaner,owner)['items'][0]
        assert row['details']['last_attempt_index']==3 and row['details']['next_retry_at']==(due+timedelta(minutes=120)).isoformat(),row
        assert scheduler.tick() is None and source.calls==0
        assert history(cleaner,owner)['items'][0]['reason'].startswith('Ожидает повторной')
        current[0]=due+timedelta(minutes=120,seconds=30)
        assert scheduler.tick()['selected_count']==1 and source.calls==1
        assert scheduler.tick() is None and source.calls==1
        with cleaner.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-batches'",(cleaner.key,)).fetchone()[0]==1

    with Sandbox() as box:
        cleaner=provision(box)
        save_schedules(cleaner,owner,'monolith',dict(request_id='exhausted-schedule',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        source=Source([target]);scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
            now=lambda:due+timedelta(minutes=121))
        assert scheduler.tick()['state']=='not_started'
        row=history(cleaner,owner)['items'][0]
        assert row['state']=='not_started' and row['checked_keys'] is None and source.calls==0,row

    with Sandbox() as box:
        cleaner=provision(box)
        save_schedules(cleaner,owner,'monolith',dict(request_id='catalog-retry-schedule',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        source=Source([target],errors=['catalog_unavailable']);current=[due+timedelta(seconds=30)]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:current[0])
        assert scheduler.tick()['state']=='start_wait' and source.calls==1
        current[0]=due+timedelta(minutes=10)
        assert scheduler.tick() is None and source.calls==1
        current[0]=due+timedelta(minutes=15,seconds=10)
        assert scheduler.tick()['state']=='start_wait' and source.calls==2
        restarted=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:current[0])
        assert restarted.tick() is None and source.calls==2
        current[0]=due+timedelta(minutes=70)
        assert restarted.tick() is None and source.calls==2
        assert history(cleaner,owner)['items'][0]['details']['next_retry_at']==(due+timedelta(minutes=120)).isoformat()
        source.errors=[];current[0]=due+timedelta(minutes=120,seconds=20)
        assert restarted.tick()['selected_count']==1 and source.calls==3

    with Sandbox() as box:
        cleaner=provision(box)
        save_schedules(cleaner,owner,'monolith',dict(request_id='pause-schedule',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        source=Source([target]);current=[due+timedelta(seconds=20)]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:current[0])
        assert scheduler.observe_deployment_blocked()==1
        (box.runtime/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=False,revision=2)))
        scheduler.observe_deployment_blocked()
        assert history(cleaner,owner)['items'][0]['state']=='skipped'
        (box.runtime/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=3)))
        current[0]=due+timedelta(minutes=15,seconds=10)
        assert scheduler.tick() is None and source.calls==0

    # A temporary catalog failure must not survive an explicit master pause.
    # The same cancellation applies to older catalog_wait and unadmitted
    # pending intents in both ready and armed worker cycles.
    for waiting_state in ('start_wait','catalog_wait','pending'):
        with Sandbox() as box:
            cleaner=provision(box)
            save_schedules(cleaner,owner,'monolith',dict(request_id='pause-prestart-'+waiting_state,expected_revision=1,
                           schedules=[dict(id='default',time='03:45',enabled=True)]))
            source=Source([target],errors=['temporary']);current=[due+timedelta(seconds=20)]
            scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                            fixture_admission=admitted,now=lambda:current[0])
            assert scheduler.tick()['state']=='start_wait' and source.calls==1
            if waiting_state!='start_wait':
                with cleaner.store.transaction() as c:
                    c.execute("UPDATE cleaner_daily_occurrences SET state=? WHERE account=?",
                              (waiting_state,cleaner.key))
            (box.runtime/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=False,revision=2)))
            if waiting_state=='start_wait':
                maintenance=box.runtime/'.business-data-maintenance.json'
                maintenance.write_text(json.dumps(dict(phase='holding')))
                assert scheduler.tick() is None
                assert history(cleaner,owner)['items'][0]['state']=='start_wait'
                maintenance.write_text(json.dumps(dict(phase='restored')))
                assert scheduler.tick() is None
            else:assert scheduler.observe_deployment_blocked()==0
            row=history(cleaner,owner)['items'][0]
            assert row['state']=='skipped' and row['reason']=='Общая пауза автообновлений',row
            original_transaction=cleaner.store.transaction
            cleaner.store.transaction=lambda:(_ for _ in ()).throw(AssertionError('repeated pause opened RW transaction'))
            try:assert scheduler.observe_deployment_blocked()==0
            finally:cleaner.store.transaction=original_transaction
            (box.runtime/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=3)))
            source.errors=[];current[0]=due+timedelta(minutes=15,seconds=10)
            assert scheduler.tick() is None and source.calls==1
            with cleaner.store.read() as c:
                assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-batches'",(cleaner.key,)).fetchone()[0]==0

    with Sandbox() as box:
        cleaner=provision(box)
        save_schedules(cleaner,owner,'monolith',dict(request_id='reenable-schedule',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
        cleaner.start_manual_batch(dict(request_id='busy-for-reenable',selected_categories=['active'],
            targets=[dict(advert_id=11,nm_id=101)]),owner,
            snapshot=eligibility_rows(cleaner,'monolith',[target],fixture_admission=admitted))
        source=Source([target]);current=[due+timedelta(seconds=20)]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:current[0])
        assert scheduler.tick()['state']=='waiting_for_queue'
        cleaner.clock=lambda:(due+timedelta(seconds=30)).isoformat()
        save_schedules(cleaner,owner,'monolith',dict(request_id='disable-pending',expected_revision=2,
                       schedules=[dict(id='default',time='03:45',enabled=False)]))
        cleaner.clock=lambda:(due+timedelta(seconds=40)).isoformat()
        save_schedules(cleaner,owner,'monolith',dict(request_id='reenable-pending',expected_revision=3,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        assert scheduler.tick() is None
        assert history(cleaner,owner)['items'][0]['state']=='skipped' and source.calls==0

    with Sandbox() as box:
        cleaner=provision(box)
        save_schedules(cleaner,owner,'monolith',dict(request_id='durable-queue-schedule',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
        cleaner.start_manual_batch(dict(request_id='busy-beyond-retries',selected_categories=['active'],
            targets=[dict(advert_id=11,nm_id=101)]),owner,
            snapshot=eligibility_rows(cleaner,'monolith',[target],fixture_admission=admitted))
        release=[True];source=Source([target]);current=[due+timedelta(seconds=20)]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:current[0],deployment_check=lambda:release[0])
        assert scheduler.tick()['state']=='waiting_for_queue'
        assert history(cleaner,owner)['items'][0]['details']['queue_admitted'] is True
        cleaner.record_manual_batch('busy-beyond-retries',state='complete',stage='finished',current_index=1)
        current[0]=due+timedelta(minutes=130);release[0]=False
        assert scheduler.tick()['state']=='waiting_for_queue' and source.calls==0
        release[0]=True
        assert scheduler.tick()['selected_count']==1 and source.calls==1

    with Sandbox() as box:
        cleaner=provision(box)
        save_schedules(cleaner,owner,'monolith',dict(request_id='midnight-retry',expected_revision=1,
                       schedules=[dict(id='late',time='23:58',enabled=True)]))
        midnight_due=datetime(2026,9,28,18,58,tzinfo=timezone.utc)
        current=[midnight_due+timedelta(seconds=20)]
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',now=lambda:current[0])
        assert scheduler.observe_deployment_blocked()==1
        current[0]=midnight_due+timedelta(minutes=15,seconds=10)
        scheduler.observe_deployment_blocked()
        row=history(cleaner,owner)['items'][0]
        assert row['local_date']=='2026-09-28' and row['details']['last_attempt_index']==1,row
        assert row['details']['next_retry_at']==(midnight_due+timedelta(minutes=30)).isoformat()
        current[0]=midnight_due+timedelta(minutes=121)
        scheduler.observe_deployment_blocked()
        rows=history(cleaner,owner)['items']
        assert len(rows)==1 and rows[0]['state']=='not_started',rows

    with Sandbox() as box:
        cleaner=provision(box)
        save_schedules(cleaner,owner,'monolith',dict(request_id='midnight-catalog-retry',expected_revision=1,
                       schedules=[dict(id='late',time='23:58',enabled=True)]))
        midnight_due=datetime(2026,9,28,18,58,tzinfo=timezone.utc)
        current=[midnight_due+timedelta(seconds=20)]
        source=Source([target],errors=['catalog_unavailable'])
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:source,
                                        fixture_admission=admitted,now=lambda:current[0])
        assert scheduler.tick()['state']=='start_wait' and source.calls==1
        source.errors=[];current[0]=midnight_due+timedelta(minutes=15,seconds=10)
        assert scheduler.tick()['selected_count']==1 and source.calls==2
        row=history(cleaner,owner)['items'][0]
        assert row['local_date']=='2026-09-28' and row['state']=='queued',row

    with Sandbox() as box:
        cleaner=provision(box)
        save_schedules(cleaner,owner,'monolith',dict(request_id='flip-release-schedule',expected_revision=1,
                       schedules=[dict(id='default',time='03:45',enabled=True)]))
        release=[True]
        def flip_source():
            class FlippingSource(Source):
                def catalog(self,*,with_statuses=False):
                    result=super().catalog(with_statuses=with_statuses)
                    release[0]=False
                    return result
            return FlippingSource([target])
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=flip_source,
                                        fixture_admission=admitted,now=lambda:due+timedelta(seconds=20),
                                        deployment_check=lambda:release[0])
        assert scheduler.tick()['state']=='deployment_blocked'
        with cleaner.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-batches'",(cleaner.key,)).fetchone()[0]==0
        assert history(cleaner,owner)['items'][0]['state']=='deployment_blocked'


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
        original_clock=cleaner.clock
        cleaner.clock=lambda:'2026-09-27T22:00:00+00:00'
        status,result,_=fixture.request('/daily-schedules',command)
        cleaner.clock=original_clock
        assert status==202 and result['revision']==2,(status,result)
        assert fixture.request('/daily-schedules')[1]['schedules'][0]['enabled'] is True
        # The integration fixture timestamps the saved schedule with its live
        # clock. Keep this synthetic due slot after that timestamp on any day.
        due_day=datetime.fromisoformat(cleaner.clock()).astimezone(ZONE)+timedelta(days=1)
        due_now=due_day.replace(hour=3,minute=45,second=30,microsecond=0).astimezone(timezone.utc)
        scheduler=DailyCleanerScheduler(cleaner,generation='monolith',source_factory=lambda:CleanerWbSource.from_env(cleaner.account),
                                        fixture_admission=fixture.web._fixture_approved_targets,now=lambda:due_now,
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
    check();deployment_blocked_flow();finite_retry_regressions();integrated_flow();print('search cluster cleaner daily smoke: ok')
