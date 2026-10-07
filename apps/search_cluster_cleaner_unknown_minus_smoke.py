#!/usr/bin/env python3
"""Synthetic exact-pair omission: diagnostic keys never become write scope."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.search_cluster_cleaner_write_fixture import fixture,OWNER,Q1,Q2
from packages.adapters.search_cluster_cleaner_wb import WbReadError
from packages.application.search_cluster_cleaner_worker import ManualCleanerWorker
from packages.application.search_cluster_cleaner_self_service import ManualCleanerCoordinator
from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator,batch_status
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.contracts.search_cluster_cleaner import CleanerError


def minus_response(f,body):
    original=f.fake.response
    def response(method,path,payload):
        status,value=original(method,path,payload)
        return (200,body) if path.endswith('/get-minus') else (status,value)
    f.fake.response=response


def target(f):return f.source.catalog()[0][0]


def record(f,snapshot):
    rid=f.start();run=f.app.claim_run(generation='g1');token=run['worker_token']
    f.app.sync_catalog(rid,token,'g1',[snapshot.target],[])
    counts=f.app.record_snapshot(rid,token,'g1',snapshot)
    return rid,token,counts


class UnknownMinusTests(unittest.TestCase):
    def test_old_complete_pair_keeps_one_full_set_write(self):
        with fixture() as f:
            before=list(f.fake.targets[11]['minus']);f.start()
            with f.worker() as (worker,_,__):result=worker.tick()
            self.assertEqual(result['state'],'complete')
            self.assertEqual(result['summary']['confirmed_automatic'],2)
            self.assertEqual(f.fake.writes,[dict(advert_id=11,nm_id=101,norm_queries=sorted(before+[Q1,Q2]))])
            self.assertEqual(f.app.run_detail(result['run_id'],OWNER)['partial_previews'],[])

    def test_known_keys_are_preliminary_and_empty_observation_is_still_partial(self):
        for empty in (False,True):
            with self.subTest(empty=empty),fixture() as f:
                v=f.fake.targets[11];v.update(active=[] if empty else [Q1],stats=[] if empty else [Q1,'стекло iphone 16 pro max','стекло iphone 16 pro max непонятное'],minus=[])
                minus_response(f,{'items':[]});t=target(f);snapshot=f.source.snapshot(t)
                self.assertFalse(snapshot.complete);self.assertEqual(snapshot.reasons,('minus_pair_omitted',))
                self.assertEqual(snapshot.minus,());self.assertEqual(len(snapshot.queries),0 if empty else 3)
                self.assertEqual(set(snapshot.source_times),{'list','statistics','minus'})
                rid,token,counts=record(f,snapshot);result=f.app.finish_run(rid,token,'g1')
                self.assertEqual(result['state'],'partial');self.assertEqual(counts['checked_total'],0)
                self.assertEqual(result['summary']['observed_only'],0 if empty else 3)
                detail=f.app.run_detail(rid,OWNER);preview=detail['partial_previews'][0]
                self.assertFalse(preview['minus_known']);self.assertEqual(detail['targets'][0]['complete'],0)
                self.assertEqual(detail['targets'][0]['reason'],'minus_pair_omitted')
                if not empty:
                    self.assertEqual(result['summary']['preliminary_exclude'],1)
                    self.assertEqual(result['summary']['preliminary_allow'],1)
                    self.assertEqual(result['summary']['preliminary_review'],1)
                    self.assertTrue(all(q['before']=='unknown' and q['preliminary'] and not q['controversial'] for q in preview['queries']))
                self.assertEqual(f.count('cleaner_reviews'),0)
                self.assertEqual(f.count('cleaner_observations'),1) # existing baseline only
                self.assertEqual(f.count('cleaner_auto_decisions'),1) # immutable approved baseline only
                self.assertEqual(f.count('cleaner_write_operations'),0);self.assertEqual(f.fake.writes,[])
                with self.assertRaises(WbReadError) as error:f.source.read_minus(t)
                self.assertEqual(error.exception.code,'pair_missing_or_malformed')

    def test_existing_observations_decisions_and_pending_candidates_unchanged(self):
        with fixture() as f:
            t=target(f);rid,token,_=record(f,f.source.snapshot(t))
            pending=f.app.pending_candidates(rid);self.assertTrue(pending)
            f.app.finish_run(rid,token,'g1')
            before={name:f.rows(name) for name in ('cleaner_observations','cleaner_auto_decisions','cleaner_reviews','cleaner_manual_overrides','cleaner_override_heads')}
            minus_response(f,{'items':[]});snapshot=f.source.snapshot(t)
            partial,token,_=record(f,snapshot)
            for name,rows in before.items():self.assertEqual(f.rows(name),rows,name)
            self.assertEqual(f.app.pending_candidates(rid),pending)
            # Even explicit invocation with previously queued candidates cannot
            # prepare or send a replacement of an unknown full minus set.
            with f.worker() as (_,writer,__):
                with self.assertRaises(CleanerError) as error:writer.apply_target(partial,token,snapshot,pending)
            self.assertEqual(error.exception.code,'incomplete_snapshot')
            self.assertEqual(f.count('cleaner_write_operations'),0);self.assertEqual(f.fake.writes,[])

    def test_malformed_and_foreign_pairs_remain_strict(self):
        for endpoint,body,code in (
            ('get-minus',{},'pair_missing_or_malformed'),
            ('get-minus',{'items':[],'unexpected':1},'pair_missing_or_malformed'),
            ('get-minus',{'items':[{'advert_id':12,'nm_id':101,'norm_queries':[]}]},'pair_identity'),
            ('list',{'items':[{'advertId':12,'nmId':101,'normQueries':dict(active=[],excluded=[],archived=[])}]},'pair_identity'),
            ('stats',{'stats':[{'advert_id':11,'nm_id':102,'stats':[]}]},'pair_identity'),
            ('stats',{'stats':[{'advert_id':11,'nm_id':101,'stats':[{}]}]},'statistics_malformed'),
        ):
            with self.subTest(endpoint=endpoint,body=body),fixture() as f:
                t=target(f);original=f.fake.response
                def response(method,path,payload):
                    status,value=original(method,path,payload)
                    if path.endswith('/'+endpoint):return 200,body
                    if path.endswith('/get-minus'):return 200,{'items':[]}
                    return status,value
                f.fake.response=response
                with self.assertRaises(WbReadError) as error:f.source.snapshot(t)
                self.assertEqual(error.exception.code,code)
                self.assertEqual(f.fake.writes,[])

    def batch_setup(self,f):
        f.fake.targets[12]=dict(f.fake.targets[11],stats=['стекло iphone 16 pro max'],minus=[])
        with f.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0 WHERE account=?',(f.app.key,))
        targets=f.source.catalog()[0];admitted=[dict(advert_id=t.advert_id,nm_id=t.nm_id,state='verified') for t in targets]
        frozen=eligibility_rows(f.app,'g1',targets,fixture_admission=admitted)
        batch_id='unknown-minus-two-target-0001'
        f.app.start_manual_batch(dict(request_id=batch_id,selected_categories=['active'],targets=[dict(advert_id=t.advert_id,nm_id=t.nm_id) for t in targets]),OWNER,snapshot=frozen)
        parent=BatchCleanerCoordinator(f.app,generation='g1',source_factory=lambda:f.source,fixture_admission=admitted)
        return parent,targets,batch_id

    def manual_scan(self,f,child,t,coordinator):
        worker=ManualCleanerWorker(f.app,f.source,f.guard,generation='g1')
        preview=worker.preview(run_id=child['scan_run_id'],targets=[t])
        result=worker.execute(run_id=child['scan_run_id'],targets=[t],expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],production_operation_id='synthetic-'+str(t.advert_id))
        coordinator._settle(child['job_id'],'scan','failed' if result['state']=='partial' else 'no_change',{})
        return result

    def test_two_targets_unknown_minus_isolated_before_next_complete_scan(self):
        with fixture() as f:
            parent,targets,batch_id=self.batch_setup(f);original=f.fake.response
            def response(method,path,payload):
                status,body=original(method,path,payload)
                if path.endswith('/get-minus') and payload['items'][0]['advert_id']==11:return 200,{'items':[]}
                return status,body
            f.fake.response=response;adapter=Mock();coordinator=ManualCleanerCoordinator(f.app,adapter)
            first=f.app.manual_job(parent.tick()['items'][0]['job_id'],OWNER)
            self.manual_scan(f,first,targets[0],coordinator)
            advanced=parent.tick();self.assertEqual(advanced['current_index'],1)
            row=advanced['items'][0];self.assertEqual(row['state'],'partial');self.assertFalse(row['minus_known'])
            self.assertEqual(row['observed_only'],len(f.fake.targets[11]['stats'])+len(f.fake.targets[11]['minus']))
            for field in ('checked_total','confirmed_excluded','returned','unchanged'):self.assertIsNone(row[field],field)
            second=f.app.manual_job(parent.tick()['items'][1]['job_id'],OWNER)
            self.manual_scan(f,second,targets[1],coordinator);coordinator.tick()
            self.assertEqual(f.app.manual_job(second['job_id'],OWNER)['state'],'no_change')
            parent.tick();final=parent.tick()
            self.assertEqual(final['state'],'partial');self.assertEqual(final['partial_count'],1);self.assertEqual(final['no_change_count'],1)
            self.assertEqual(final['not_started_count'],0);self.assertEqual(adapter.mock_calls,[])
            self.assertEqual(f.count('cleaner_write_operations'),0);self.assertEqual(f.fake.writes,[])

    def test_batch_never_advances_unknown_minus_with_any_write_evidence_or_ambiguity(self):
        for evidence in ('prepared','submitted','unresolved','write_run','manual_prepared','ambiguous'):
            with self.subTest(evidence=evidence),fixture() as f:
                operation=None
                if evidence in ('prepared','submitted','unresolved'):
                    snapshot=f.source.snapshot(target(f));rid,token,_=record(f,snapshot)
                    with f.worker() as (_,writer,__):operation=writer.prepare(rid,token,snapshot,f.app.pending_candidates(rid),(101,))
                    f.app.finish_run(rid,token,'g1')
                parent,targets,batch_id=self.batch_setup(f)
                first=f.app.manual_job(parent.tick()['items'][0]['job_id'],OWNER)
                minus_response(f,{'items':[]});coordinator=ManualCleanerCoordinator(f.app,Mock())
                self.manual_scan(f,first,targets[0],coordinator)
                if operation:
                    with f.store.transaction() as c:c.execute('UPDATE cleaner_write_operations SET run_id=?,state=? WHERE operation_id=?',(first['scan_run_id'],evidence,operation))
                elif evidence=='write_run':f.app.record_manual_job(first['job_id'],write_run_id='synthetic-write-evidence')
                elif evidence=='ambiguous':f.app.record_manual_job(first['job_id'],state='ambiguous',stage='scan_apply_claimed',can_recheck=True)
                else:
                    with f.store.transaction() as c:f.app._event(c,'manual_apply_prepared',dict(scan_run_id=first['scan_run_id']),run_id=first['scan_run_id'])
                # A nonexistent synthetic write run is sufficient evidence to
                # refuse isolation; avoid its unrelated UI detail lookup here.
                child=f.app.manual_job(first['job_id'],OWNER)
                self.assertIsNone(parent._failed_scan_without_write(child,dict(advert_id=11,nm_id=101)))
                if evidence!='write_run':parent.tick()
                self.assertFalse(any(r['nm_id']==101 and r['advert_id']==12 for r in [f.app.manual_job(row['request_id'],OWNER) for row in f.rows('cleaner_requests') if row['route']=='manual-clean']))
                self.assertEqual(f.fake.writes,[])

    def test_exact_manual_scan_readback_blocks_prepare_and_coordinator_write(self):
        for empty in (False,True):
            with self.subTest(empty=empty),fixture() as f:
                f.fake.targets[11].update(minus=[],active=[],stats=[] if empty else [Q1])
                minus_response(f,{'items':[]});t=target(f)
                with f.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0 WHERE account=?',(f.app.key,))
                job=f.app.start_manual_clean(dict(request_id='unknown-minus-manual-0001',advert_id=11,nm_id=101),OWNER)
                worker=ManualCleanerWorker(f.app,f.source,f.guard,generation='g1')
                preview=worker.preview(run_id=job['run_id'],targets=[t])
                result=worker.execute(run_id=job['run_id'],targets=[t],expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],production_operation_id='synthetic-unknown-minus')
                self.assertEqual(result['state'],'partial')
                adapter=Mock();coordinator=ManualCleanerCoordinator(f.app,adapter)
                coordinator._settle(job['job_id'],'scan','failed',{})
                final=f.app.manual_job(job['job_id'],OWNER)
                self.assertEqual(final['state'],'partial');self.assertFalse(final['can_recheck'])
                self.assertFalse(final['scan_minus_known']);self.assertEqual(final['observed_only'],0 if empty else 1)
                self.assertEqual(final['error_code'],'minus_pair_omitted');self.assertIsNone(final.get('write_run_id'))
                with f.store.read() as c:
                    with self.assertRaises(CleanerError) as error:f.app._manual_apply_preview(c,job['run_id'],t)
                self.assertEqual(error.exception.code,'manual_scan_incomplete')
                coordinator.tick();self.assertEqual(adapter.mock_calls,[])
                self.assertEqual(f.count('cleaner_write_operations'),0);self.assertEqual(f.fake.writes,[])


if __name__=='__main__':unittest.main()
