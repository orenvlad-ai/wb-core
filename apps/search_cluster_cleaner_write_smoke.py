#!/usr/bin/env python3
"""D connected/fault acceptance: only disposable DBs and loopback fake WB."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.search_cluster_cleaner_write_fixture import fixture,OWNER,PROFILE,Q1,Q2,FakeWB
from packages.application.search_cluster_cleaner_worker import CleanerWorker,product_tick
from packages.application.search_cluster_cleaner_writer import CleanerReadback
from packages.application.search_cluster_cleaner_admission import AdmissionGuard,process_identity
from packages.application.change_registry import ChangeRegistryRepository,target_identity,canonical_digest
from packages.application.business_data_write_barrier import acquire_barrier
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource,AccountLimiter,WbReadError
from packages.adapters.official_api_runtime import OfficialApiRuntimeConfig
from packages.contracts.search_cluster_cleaner import CleanerError,canonical,query_hash


class Crash(BaseException):pass


class WriteTests(unittest.TestCase):
    def test_connected_stats_only_exact_full_set(self):
        with fixture() as f:
            original=list(f.fake.targets[11]['minus']);rid=f.start()
            with f.worker() as (worker,_,__):result=worker.tick()
            self.assertEqual(len(f.fake.writes),1);self.assertEqual(f.fake.writes[0]['norm_queries'],sorted(original+[Q1,Q2]))
            self.assertEqual(result['summary']['confirmed_automatic'],2);self.assertFalse(result['summary']['dry_run'])
            self.assertEqual(f.count('change_registry_items'),2);self.assertEqual(f.count('change_registry_facts'),2)
            self.assertEqual(f.count('cleaner_write_items'),2);self.assertEqual(f.app.summary(OWNER)['confirmed']['automatic'],2)
            self.assertTrue(all(r['query_hash'] for r in f.rows('change_registry_items')))
            self.assertEqual(f.fake.targets[11]['active'],[])
            f.clock.advance(20);f.start()
            with f.worker() as (worker,_,__):worker.tick()
            self.assertEqual(len(f.fake.writes),1);self.assertEqual(f.count('change_registry_facts'),2)

    def test_limits_1000_1001_zero(self):
        for old_count,expected_calls in [(998,1),(999,0),(1000,0)]:
            with self.subTest(old_count=old_count),fixture() as f:
                f.fake.targets[11]['minus']=['Старое '+str(i) for i in range(old_count)]
                if old_count==1000:f.fake.targets[11]['stats']=[]
                f.start()
                with f.worker() as (worker,_,__):worker.tick()
                self.assertEqual(len(f.fake.writes),expected_calls)
                if expected_calls:self.assertEqual(len(f.fake.writes[0]['norm_queries']),1000)
                else:self.assertEqual(f.count('change_registry_items'),0)

    def test_fake200_partial_missingold_extra_timeout(self):
        for mode,confirmed,hold,opstate in [('noop',0,0,'unresolved'),('partial',1,0,'unresolved'),('missing_old',2,1,'unresolved'),('extra',2,1,'unresolved'),('timeout',2,0,'confirmed')]:
            with self.subTest(mode=mode),fixture() as f:
                f.fake.mode=mode;f.start()
                with f.worker() as (worker,_,__):result=worker.tick()
                self.assertEqual(result['summary']['confirmed_automatic'],confirmed)
                self.assertEqual(f.count('change_registry_facts'),confirmed);self.assertEqual(f.count('cleaner_target_holds'),hold)
                self.assertEqual(f.rows('cleaner_write_operations')[0]['state'],opstate)
                f.clock.advance(400);f.start()
                with f.worker() as (worker,_,__):worker.tick()
                self.assertEqual(len(f.fake.writes),1)

    def test_http_errors_no_write_retry_redirect(self):
        for mode in ['429','500','401','403','302']:
            with self.subTest(mode=mode),fixture() as f:
                f.fake.mode=mode;f.start()
                with f.worker() as (worker,_,__):worker.tick()
                f.clock.advance(400);f.start()
                with f.worker() as (worker,_,__):worker.tick()
                self.assertEqual(len(f.fake.writes),1);self.assertEqual(f.count('change_registry_facts'),0)
                self.assertEqual(f.rows('cleaner_write_operations')[0]['dispatch_count'],1)

    def test_stale_guard_zero_dispatch(self):
        for mutation in ['disable','settings','profile','rules','override','lease','generation','preflight_age','candidate','maintenance']:
            with self.subTest(mutation=mutation),fixture() as f:
                def hook(stage,op):
                    if stage!='before_admission':return
                    if mutation in {'disable','settings'}:
                        revision=f.app.summary(OWNER)['settings']['revision']
                        f.app.update_settings(dict(request_id='race-settings-001',expected_revision=revision,enabled=mutation!='disable',schedule_time='08:00'),OWNER)
                    elif mutation=='preflight_age':f.clock.advance(35)
                    elif mutation=='profile':
                        f.app.create_profile(101,dict(request_id='race-profile-001',expected_revision=1,profile={k:v for k,v in PROFILE.items() if k not in {'nm_id','version'}}),OWNER)
                        f.app.activate_profile(101,dict(request_id='race-activate-001',expected_revision=2,version=2),OWNER)
                    elif mutation=='maintenance':
                        acquire_barrier(f.store.registry.runtime_dir,window_kind='snapshot',actor='fixture',plan_fingerprint='sha256:'+'a'*64,reason='synthetic writer guard',window_id='fixture-window',approval_reference='fixture-approval')
                    else:
                        with f.store.transaction() as c:
                            if mutation=='rules':c.execute("UPDATE cleaner_settings SET rules_version='stale' WHERE account=?",(f.app.key,))
                            if mutation=='lease':c.execute("UPDATE cleaner_runs SET worker_token='new-owner' WHERE state='running'")
                            if mutation=='generation':c.execute("UPDATE cleaner_settings SET generation='g2' WHERE account=?",(f.app.key,))
                            if mutation=='candidate':c.execute("UPDATE cleaner_write_operations SET expected_json='[]' WHERE operation_id=?",(op,))
                            if mutation=='override':
                                fp=f.app._profile(c,101).semantic_fingerprint
                                c.execute('INSERT INTO cleaner_manual_overrides VALUES(?,?,?,?,?,?,?,?,?)',(f.app.key,101,query_hash(Q1),Q1,1,'allow',fp,'owner',f.clock()))
                                c.execute('INSERT INTO cleaner_override_heads VALUES(?,?,?,?,0)',(f.app.key,101,query_hash(Q1),1))
                f.start()
                with f.worker(hook=hook) as (worker,_,__):worker.tick()
                self.assertEqual(len(f.fake.writes),0);self.assertEqual(f.count('change_registry_operations'),0)

    def test_fresh_target_statistics_and_minus_drift(self):
        for mutation in ['membership','stats_missing','archived','minus','unsupported','renamed_target']:
            with self.subTest(mutation=mutation),fixture() as f:
                def hook(stage,op):
                    if stage!='after_prepare':return
                    v=f.fake.targets[11]
                    if mutation=='membership':v['members'].append(102)
                    if mutation=='stats_missing':v['stats'].remove(Q1)
                    if mutation=='archived':v['archived']=[Q1]
                    if mutation=='minus':v['minus'].append('Внешнее новое исключение')
                    if mutation=='unsupported':v['bid']='unified'
                    if mutation=='renamed_target':v['members']=[102]
                f.start()
                with f.worker(hook=hook) as (worker,_,__):worker.tick()
                self.assertEqual(len(f.fake.writes),0);self.assertEqual(f.count('change_registry_operations'),0)

    def test_registry_and_operational_failure_zero_post(self):
        for point in ['registry','before_seal','before_commit']:
            with self.subTest(point=point),fixture() as f:
                def fail(*args,**kwargs):raise sqlite3.OperationalError('synthetic storage failure')
                def hook(stage,op):
                    if stage==point:fail()
                f.start()
                with f.worker(hook=hook) as (worker,writer,__):
                    if point=='registry':writer.registry.prepare_search_cluster_operation_in_transaction=fail
                    with self.assertRaises(sqlite3.OperationalError):worker.tick()
                self.assertEqual(len(f.fake.writes),0);self.assertEqual(f.count('change_registry_operations'),0)
                self.assertEqual(f.count('change_registry_items'),0)

    def test_crash_boundaries_are_readback_only(self):
        for point,count in [('after_prepare',0),('before_commit',0),('after_commit',0),('before_network',0),('after_network',1),('after_receipt',1)]:
            with self.subTest(point=point),fixture() as f:
                def hook(stage,op):
                    if stage==point:raise Crash(point)
                f.start()
                with self.assertRaises(Crash),f.worker(hook=hook) as (worker,_,__):worker.tick()
                self.assertEqual(len(f.fake.writes),count)
                f.clock.advance(400)
                if point=='before_commit':
                    result=product_tick(f.app,f.source,f.guard,generation='g1');self.assertEqual(result['reason'],'restore_journal_gap')
                else:
                    with f.worker() as (worker,_,__):worker.tick()
                    self.assertEqual(len(f.fake.writes),1 if point=='after_prepare' else count)
                if count:self.assertEqual(f.count('change_registry_facts'),2)

    def test_restored_queued_backup_lost_postbackup_journal(self):
        with fixture() as f:
            f.start();backup=sqlite3.connect(str(f.root/'queued-backup.db'))
            with f.store.read() as c:c.backup(backup)
            with f.worker() as (worker,_,__):worker.tick()
            self.assertEqual(len(f.fake.writes),1)
            # Actual loss of the operational interval; all post-backup op rows
            # disappear, not merely a restore_hold flag toggled in the same DB.
            with f.store.registry.session('operational',mode='rw',operation='synthetic-restore') as c:backup.backup(c)
            backup.close();self.assertEqual(f.count('cleaner_write_operations'),0)
            self.assertEqual(f.rows('cleaner_runs')[0]['state'],'queued')
            result=product_tick(f.app,f.source,f.guard,generation='g1')
            self.assertEqual(result['reason'],'restore_journal_gap');self.assertEqual(len(f.fake.writes),1)
            with self.assertRaisesRegex(CleanerError,'Восстановленная'):
                f.guard.activate(account=f.account,generation='g1',evidence='cannot replace missing journal with a flag')

    def test_live_process_and_default_hold(self):
        with fixture() as f:
            with f.guard.session(account=f.account,generation='g1'):
                with self.assertRaises(CleanerError):
                    f.guard.activate(account=f.account,generation='g2',evidence='fixture')
            state=f.guard._load();state['owner']=process_identity();f.guard._save(state)
            with self.assertRaises(CleanerError):
                f.guard.activate(account=f.account,generation='g1',evidence='fixture')
            closed=AdmissionGuard(f.root/'unconfigured-server',f.store)
            with self.assertRaises(CleanerError):
                with closed.session(account=f.account,generation='g1'):pass

    def test_late_readback_disabled_finished_immutable(self):
        with fixture() as f:
            f.fake.mode='partial';rid=f.start()
            with f.worker() as (worker,_,readback):result=worker.tick()
            original=f.app.run_detail(rid,OWNER)['summary'];event=next(r for r in f.rows('cleaner_events') if r['kind']=='run_finished')
            f.app.update_settings(dict(request_id='disable-after-001',expected_revision=2,enabled=False),OWNER)
            f.clock.advance(25)
            readback.tick() # same partial query adds evidence, no second fact
            self.assertEqual(f.count('change_registry_facts'),1);self.assertEqual(f.count('change_registry_search_cluster_readbacks'),2)
            f.fake.targets[11]['minus']=f.fake.writes[0]['norm_queries'];f.clock.advance(25)
            final=readback.tick();self.assertTrue(final['late']);self.assertEqual(f.count('change_registry_facts'),2)
            detail=f.app.run_detail(rid,OWNER);self.assertEqual(detail['summary'],original);self.assertEqual(detail['settlement']['confirmed_automatic'],1)
            self.assertEqual(next(r for r in f.rows('cleaner_events') if r['kind']=='run_finished'),event)
            self.assertEqual(f.app.summary(OWNER)['confirmed']['late_automatic'],1)
            self.assertEqual(f.app.summary(OWNER)['unresolved_count'],0);self.assertEqual(len(f.fake.writes),1)

    def test_unresolved_a_does_not_hold_b(self):
        with fixture() as f:
            f.fake.mode='noop';f.start()
            with f.worker() as (worker,_,__):worker.tick()
            f.fake.targets[12]=copy.deepcopy(f.fake.targets[11]);f.fake.targets[12]['minus']=[];f.fake.mode='normal';f.clock.advance(400);f.start()
            with f.worker() as (worker,_,__):result=worker.tick()
            self.assertEqual([w['advert_id'] for w in f.fake.writes],[11,12]);self.assertEqual(f.app.summary(OWNER)['unresolved_count'],1)
            self.assertIsNone(f.app.summary(OWNER)['current_work'])

    def test_manual_after_scan_and_continuation(self):
        with fixture() as f:
            f.fake.targets[11]['stats']=['стекло iphone 16 pro max без салфетки'];f.fake.targets[12]=copy.deepcopy(f.fake.targets[11]);f.start()
            # Completed scan with an unresolved semantic question, no writer yet.
            result=CleanerWorker(f.app,f.source,generation='g1',monotonic=f.clock.monotonic).tick()
            review=f.app.reviews(OWNER)['items'][0]
            command=dict(request_id='manual-exclude-001',expected_revision=review['revision'],decision='exclude')
            accepted=f.app.decide(review['review_id'],command,OWNER)
            self.assertEqual(accepted,f.app.decide(review['review_id'],command,OWNER))
            with f.worker(max_targets=1) as (worker,_,__):first=worker.tick()
            self.assertEqual(first['summary']['confirmed_manual'],1);self.assertIn('continuation_run_id',first['summary'])
            with f.worker() as (worker,_,__):second=worker.tick()
            self.assertEqual(second['summary']['confirmed_manual'],1);self.assertEqual(len(f.fake.writes),2)
            self.assertEqual(f.app.summary(OWNER)['confirmed']['manual'],2)
            self.assertEqual(f.app.summary(OWNER)['last_scan']['run_id'],result['run_id'])
            self.assertEqual(f.app.summary(OWNER)['last_scan']['summary']['confirmed_automatic'],0)

    def test_manual_disable_requeues_only_undispatched(self):
        with fixture() as f:
            f.fake.targets[11]['stats']=['стекло iphone 16 pro max без салфетки'];f.fake.targets[12]=copy.deepcopy(f.fake.targets[11]);f.start()
            CleanerWorker(f.app,f.source,generation='g1',monotonic=f.clock.monotonic).tick();review=f.app.reviews(OWNER)['items'][0]
            f.app.decide(review['review_id'],dict(request_id='manual-disable-001',expected_revision=review['revision'],decision='exclude'),OWNER)
            fired=[]
            def hook(stage,op):
                if stage=='after_receipt' and not fired:
                    fired.append(True);f.app.update_settings(dict(request_id='disable-live-001',expected_revision=2,enabled=False),OWNER)
            with f.worker(hook=hook) as (worker,_,__):result=worker.tick()
            self.assertEqual(len(f.fake.writes),1);self.assertIn('continuation_run_id',result['summary'])
            child=next(r for r in f.rows('cleaner_runs') if r['run_id']==result['summary']['continuation_run_id'])
            self.assertEqual(len(json.loads(child['targets'])),1)

    def test_readback_crash_recovers_same_operation(self):
        with fixture() as f:
            f.fake.mode='partial';f.start()
            with f.worker() as (worker,_,__):worker.tick()
            f.fake.targets[11]['minus']=f.fake.writes[0]['norm_queries'];f.clock.advance(25)
            def crash(*args):raise Crash()
            rb=CleanerReadback(f.app,f.source,generation='g1',hook=crash)
            with self.assertRaises(Crash):rb.tick()
            f.clock.advance(100);CleanerReadback(f.app,f.source,generation='g1').tick()
            self.assertEqual(len(f.fake.writes),1);self.assertEqual(f.count('change_registry_facts'),2)

    def test_slow_response_body_deadline(self):
        with fixture() as f:
            target=f.source.catalog()[0][0]
            f.fake.slow=True
            source=CleanerWbSource(account=f.account,runtime=OfficialApiRuntimeConfig('fixture',f.source.runtime.base_url,.15),fixture=True,limiter=AccountLimiter(interval=0))
            started=time.monotonic()
            with self.assertRaises(WbReadError):source.read_minus(target)
            self.assertLess(time.monotonic()-started,.6)

    def test_catalog_validation_missing_null_and_unsupported(self):
        for variant in ['null_bid','cpc','unified','missing_pair','null_minus','malformed_count','duplicate_count']:
            with self.subTest(variant=variant),fixture() as f:
                original=f.fake.response
                def response(method,path,body):
                    status,value=original(method,path,body)
                    if variant=='null_bid':f.fake.targets[11]['bid']=None
                    if variant=='cpc':f.fake.targets[11]['payment']='cpc'
                    if variant=='unified':f.fake.targets[11]['bid']='unified'
                    if variant=='missing_pair' and path.endswith('/list'):value={'items':[]}
                    if variant=='null_minus' and path.endswith('/get-minus'):value['items'][0]['norm_queries']=None
                    if variant=='malformed_count' and path.endswith('/count'):value['all']=0
                    if variant=='duplicate_count' and path.endswith('/count'):
                        value['adverts'][0]['advert_list']*=2;value['adverts'][0]['count']=2;value['all']=2
                    return status,value
                f.fake.response=response;f.start()
                with f.worker() as (worker,_,__):worker.tick()
                self.assertEqual(len(f.fake.writes),0)

    def test_catalog_partial_keeps_valid_target(self):
        with fixture() as f:
            original=f.fake.response
            def response(method,path,body):
                code,value=original(method,path,body)
                if path.endswith('/count'):
                    value['all']=2;value['adverts'][0]['count']=2;value['adverts'][0]['advert_list'].append(dict(advertId=12))
                return code,value
            f.fake.response=response;f.start()
            with f.worker() as (worker,_,__):result=worker.tick()
            self.assertEqual(len(f.fake.writes),1);self.assertEqual(result['state'],'partial')
            self.assertEqual(result['summary']['confirmed_automatic'],2)

    def test_total_target_budget_includes_initial_reads_and_rate_wait(self):
        with fixture() as f:
            def hook(stage,op):
                if stage=='after_prepare':f.clock.advance(112)
                if stage=='before_admission':f.clock.advance(8)
            f.start()
            with f.worker(hook=hook) as (worker,_,__):worker.tick()
            self.assertEqual(len(f.fake.writes),0);self.assertEqual(f.count('change_registry_operations'),0)

    def test_account_readback_auth_stops_new_scan(self):
        with fixture() as f:
            f.fake.mode='noop';f.start()
            with f.worker() as (worker,_,__):worker.tick()
            f.clock.advance(400);f.fake.codes['/adv/v0/normquery/get-minus']=401
            f.fake.targets[12]=copy.deepcopy(f.fake.targets[11]);f.start();before=len(f.fake.calls)
            with f.worker() as (worker,_,__):result=worker.tick()
            self.assertEqual(result['reason'],'unauthorized');self.assertEqual(len(f.fake.writes),1)
            self.assertTrue(all(not path.endswith('/count') for _,path,_ in f.fake.calls[before:]))

    def test_manual_account_error_retains_undispatched_tail(self):
        with fixture() as f:
            f.fake.targets[11]['stats']=['стекло iphone 16 pro max без салфетки'];f.fake.targets[12]=copy.deepcopy(f.fake.targets[11]);f.start()
            CleanerWorker(f.app,f.source,generation='g1',monotonic=f.clock.monotonic).tick();review=f.app.reviews(OWNER)['items'][0]
            f.app.decide(review['review_id'],dict(request_id='manual-auth-001',expected_revision=review['revision'],decision='exclude'),OWNER)
            f.fake.mode='401'
            with f.worker() as (worker,_,__):result=worker.tick()
            self.assertEqual(len(f.fake.writes),1);self.assertIn('continuation_run_id',result['summary'])
            child=next(r for r in f.rows('cleaner_runs') if r['run_id']==result['summary']['continuation_run_id'])
            self.assertEqual(len(json.loads(child['targets'])),1)
            self.assertEqual(json.loads(child['targets'])[0]['target'],'12:101')

    def test_statistics_limiter_and_retry_after(self):
        with fixture() as f:
            target=f.source.catalog()[0][0];f.source.snapshot(target);first=f.clock.seconds;f.source.snapshot(target)
            self.assertGreaterEqual(f.clock.seconds-first,6.1)
            f.fake.codes['/adv/v0/normquery/get-minus']=429
            with self.assertRaises(WbReadError) as caught:f.source.read_minus(target)
            self.assertEqual(caught.exception.retry_after,7)
            previous=f.clock.seconds;f.fake.codes.clear();f.source.read_minus(target)
            self.assertGreaterEqual(f.clock.seconds-previous,7)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);args,rest=parser.parse_known_args()
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(WriteTests)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(dict(tests=result.testsRun,success=result.wasSuccessful(),failures=[str(t) for t,_ in result.failures],errors=[str(t) for t,_ in result.errors]),indent=2))
    sys.exit(not result.wasSuccessful())
