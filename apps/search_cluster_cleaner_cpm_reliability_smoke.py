#!/usr/bin/env python3
"""Synthetic CPM admission, exact-ID recovery and real SQLite COMMIT contention."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import sqlite3
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from apps import search_cluster_cleaner_stage_e as stage_e
from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from apps.search_cluster_cleaner_web_fixture import running_fixture
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.application.search_cluster_cleaner_self_service import ManualCleanerCoordinator
from packages.application.search_cluster_cleaner_web import CleanerWeb
from packages.contracts.search_cluster_cleaner import CleanerError,Principal,Target


class Source:
    def __init__(self,targets):self.targets=targets;self.calls=[]
    def monotonic(self):return 0.0
    def count_statuses(self,deadline):return {row.advert_id:row.status for row in self.targets}
    def _adverts(self,ids,deadline,*,strict=True):
        self.calls.append(tuple(ids))
        rows=[row for row in self.targets if row.advert_id in ids]
        if strict:return rows
        return rows,['adverts_missing:'+str(i) for i in ids if i not in {row.advert_id for row in rows}]


def reject(code,fn):
    try:fn()
    except CleanerError as exc:assert exc.code==code,(exc.code,code)
    else:raise AssertionError('expected '+code)


def boot(box):
    preview=box.execute('preview')
    box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])


def main():
    with Sandbox() as box:
        boot(box)
        cleaner=box.service();owner=Principal('owner',True,True,True)
        targets=[Target(10000+i,101,name='CPM '+str(i),contract_verified=True) for i in range(121)]
        targets += [Target(20001,101,payment_type='cpc',name='CPC',contract_verified=True),
                    Target(20002,101,bid_type='auto',name='Auto CPM',contract_verified=False),
                    Target(20003,102,name='No profile',contract_verified=True)]
        admitted=[dict(advert_id=row.advert_id,nm_id=row.nm_id,state='verified') for row in targets]
        rows=eligibility_rows(cleaner,'monolith',targets,fixture_admission=admitted)
        assert len([r for r in rows if r['eligible']])==121
        assert not any(r['advert_id']==20001 for r in rows),'CPC leaked into CPM projection'
        assert next(r for r in rows if r['advert_id']==20002)['reason']=='contract_not_verified'
        assert next(r for r in rows if r['advert_id']==20003)['reason']=='profile_required'
        source=Source(targets)
        web=CleanerWeb(cleaner,generation='monolith',approved_targets=admitted,batch_catalog_targets=targets)
        web.worker_status=lambda:'ready'
        selection=[dict(advert_id=row.advert_id,nm_id=row.nm_id) for row in targets[:121]]
        command=dict(request_id='cpm-batch-over-100-0001',selected_categories=['active'],targets=selection)
        with patch('packages.adapters.search_cluster_cleaner_wb.CleanerWbSource.from_env',return_value=source):
            accepted=web.start_manual_batch(command,owner)
            assert accepted['selected_count']==121
            assert [len(call) for call in source.calls]==[50,50,21],source.calls
            assert web.start_manual_batch(command,owner)==accepted
            before=len(source.calls)
            reject('batch_target_ineligible',lambda:web.start_manual_batch(dict(command,request_id='cpm-batch-forged-0001',
                   targets=[dict(advert_id=999999,nm_id=101)]),owner))
            assert len(source.calls)==before,'forged ID reached advert detail'
        with cleaner.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_requests WHERE account=? AND route='manual-batches'",(cleaner.key,)).fetchone()[0]==1

    with Sandbox() as box:
        boot(box)
        cleaner=box.service();owner=Principal('owner',True,True,True)
        target=Target(11,101,name='Exact CPM',contract_verified=True)
        admitted=[dict(advert_id=11,nm_id=101,state='verified')]
        snapshot=eligibility_rows(cleaner,'monolith',[target],fixture_admission=admitted)
        command=dict(request_id='cpm-batch-locked-0001',selected_categories=['active'],targets=[dict(advert_id=11,nm_id=101)])
        database=box.runtime/'registry_upload_runtime.sqlite3'
        holder=sqlite3.connect(database)
        holder.execute('PRAGMA journal_mode=DELETE')
        holder.execute('BEGIN')
        holder.execute('SELECT count(*) FROM cleaner_settings').fetchone()
        original=cleaner.store.transaction
        @contextmanager
        def short_transaction(**_kw):
            with original(timeout_ms=350) as c:yield c
        cleaner.store.transaction=short_transaction
        try:
            reject('storage_rolled_back',lambda:cleaner.start_manual_batch(command,owner,snapshot=snapshot))
            reject('not_found',lambda:cleaner.get_request(command['request_id'],owner))
        finally:
            holder.rollback();holder.close();cleaner.store.transaction=original
        accepted=cleaner.start_manual_batch(command,owner,snapshot=snapshot)
        assert cleaner.start_manual_batch(command,owner,snapshot=snapshot)==accepted
        reject('request_conflict',lambda:cleaner.start_manual_batch(dict(command,selected_categories=['active','paused']),owner,snapshot=snapshot))
        with cleaner.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_events WHERE account=? AND kind='self_service_batch_requested'",(cleaner.key,)).fetchone()[0]==1

    with Sandbox() as box:
        boot(box)
        cleaner=box.service();owner=Principal('owner',True,True,True)
        target=Target(11,101,name='Exact CPM',contract_verified=True)
        snapshot=eligibility_rows(cleaner,'monolith',[target],fixture_admission=[dict(advert_id=11,nm_id=101,state='verified')])
        command=dict(request_id='cpm-batch-lost-reply-0001',selected_categories=['active'],targets=[dict(advert_id=11,nm_id=101)])
        original=cleaner.store.transaction
        @contextmanager
        def lost_reply(**kw):
            with original(**kw) as c:yield c
            raise sqlite3.OperationalError('synthetic response lost after commit')
        cleaner.store.transaction=lost_reply
        try:
            try:cleaner.start_manual_batch(command,owner,snapshot=snapshot)
            except sqlite3.OperationalError:pass
            else:raise AssertionError('missing lost-response injection')
        finally:cleaner.store.transaction=original
        saved=cleaner.get_request(command['request_id'],owner)
        assert saved['batch_id']==command['request_id']
        assert cleaner.start_manual_batch(command,owner,snapshot=snapshot)==saved
        with cleaner.store.read() as c:
            assert c.execute("SELECT count(*) FROM cleaner_events WHERE account=? AND kind='self_service_batch_requested'",(cleaner.key,)).fetchone()[0]==1

    with Sandbox() as box:
        approved_card=dict(nm_id='102',title='Approved glass',vendor_code='approved-102',description='Approved card',characteristics=[])
        source_bytes=json.dumps(dict(cards=[dict(approved_card,card_digest='sha256:'+'2'*64)]),sort_keys=True).encode()
        source_path=box.admission/'card-source-approved.json';source_path.write_bytes(source_bytes);source_path.chmod(0o600)
        profile=dict(box.package['profiles'][0],nm_id=102)
        box.package['profiles'].append(profile)
        box.package['provenance']['fresh_cards_sha256']='sha256:'+hashlib.sha256(source_bytes).hexdigest()
        box.write_package();boot(box)
        package=stage_e._package(box.package_path,box.service().account,'monolith')
        receipt=stage_e._admitted_targets(package,[Target(12,102)],box.admission)[0]
        assert receipt['basis']=='package_bound_source_fresh_check_required'
        projected=eligibility_rows(box.service(),'monolith',[Target(12,102,name='New CPM',contract_verified=True)],
                                   config_path=box.admission/'stage-e-config.json')
        assert len(projected)==1 and projected[0]['eligible'] and projected[0]['admitted'],projected
        with patch.object(stage_e,'fetch_current_card',return_value=approved_card):
            verified=stage_e._verify_fresh_card(package,Target(12,102),box.admission,box.service())
            assert verified['nm_id']==102
        with patch.object(stage_e,'fetch_current_card',return_value=dict(approved_card,title='Drift')):
            reject('current_card_drift',lambda:stage_e._verify_fresh_card(package,Target(12,102),box.admission,box.service()))

    with Sandbox() as box:
        # The site bootstrap owner is a different authenticated actor from the
        # historic cleaner owner. Internal recovery must keep that real actor.
        box.package['owner_username']='codex';box.write_package();boot(box)
        base=box.service()
        cleaner=KeywordCleaner(base.store,base.account,owner_username='codex')
        site_owner=Principal('owner',True,True,False,site_owner=True)
        target=Target(11,101,name='Exact CPM',contract_verified=True)
        admitted=[dict(advert_id=11,nm_id=101,state='verified')]
        snapshot=eligibility_rows(cleaner,'monolith',[target],fixture_admission=admitted)
        command=dict(request_id='cpm-batch-bootstrap-owner-0001',selected_categories=['active'],
                     targets=[dict(advert_id=11,nm_id=101)])
        accepted=cleaner.start_manual_batch(command,site_owner,snapshot=snapshot)
        source=Source([target])
        parent=BatchCleanerCoordinator(cleaner,generation='monolith',source_factory=lambda:source,
                                       fixture_admission=admitted,bootstrap_owner_username='owner')
        status=parent.tick()
        child_id=status['items'][0]['job_id']
        assert child_id and cleaner.manual_job(child_id,site_owner)['advert_id']==11
        child=ManualCleanerCoordinator(cleaner,object(),bootstrap_owner_username='owner')
        assert child._job(child_id)['actor']=='owner'
        with cleaner.store.read() as c:
            actors=[row['actor'] for row in c.execute("SELECT actor FROM cleaner_requests WHERE account=? ORDER BY rowid",(cleaner.key,))]
        assert actors[-2:]==['owner','owner'],actors
        reject('batch_authority_mismatch',lambda:BatchCleanerCoordinator(cleaner,generation='monolith',
               source_factory=lambda:source,fixture_admission=admitted,bootstrap_owner_username='wrong').tick())
        reject('manual_job_authority_mismatch',lambda:ManualCleanerCoordinator(cleaner,object(),
               bootstrap_owner_username='wrong')._job(child_id))

    with running_fixture() as fixture:
        revision=fixture.request('/summary')[1]['settings']['revision']
        fixture.request('/settings',dict(request_id='fixture-batch-settings-off-0001',expected_revision=revision,enabled=False))
        source=Source([Target(10101,101,name='Fixture CPM',contract_verified=True)])
        command=dict(request_id='fixture-http-locked-batch-0001',selected_categories=['active'],
                     targets=[dict(advert_id=10101,nm_id=101)])
        holder=sqlite3.connect(fixture.runtime_dir/'registry_upload_runtime.sqlite3')
        holder.execute('BEGIN');holder.execute('SELECT count(*) FROM cleaner_settings').fetchone()
        original=fixture.cleaner.store.transaction
        @contextmanager
        def short_http_transaction(**_kw):
            with original(timeout_ms=350) as c:yield c
        fixture.cleaner.store.transaction=short_http_transaction
        try:
            with patch('packages.adapters.search_cluster_cleaner_wb.CleanerWbSource.from_env',return_value=source):
                code,response,_=fixture.request('/manual-batches',command)
            assert code==503 and response['code']=='storage_rolled_back',response
            assert response['definitively_not_accepted'] is True and response['request_id']==command['request_id']
        finally:
            holder.rollback();holder.close();fixture.cleaner.store.transaction=original
        assert fixture.request('/requests/'+command['request_id'])[0]==404
        with patch('packages.adapters.search_cluster_cleaner_wb.CleanerWbSource.from_env',return_value=source):
            code,accepted,_=fixture.request('/manual-batches',command)
        assert code==202 and accepted['batch_id']==command['request_id']
        assert fixture.request('/requests/'+command['request_id'])[1]==accepted
        @contextmanager
        def unknown_after_commit(**kw):
            with original(**kw) as c:yield c
            raise sqlite3.OperationalError('synthetic unknown after commit')
        fixture.cleaner.store.transaction=unknown_after_commit
        try:
            code,response,_=fixture.request('/manual-batches',command)
            assert code==503 and response['code']=='storage_unavailable' and not response.get('definitively_not_accepted')
        finally:fixture.cleaner.store.transaction=original
        assert fixture.request('/requests/'+command['request_id'])[1]==accepted
    print('search cluster cleaner CPM reliability smoke: ok')


if __name__=='__main__':main()
