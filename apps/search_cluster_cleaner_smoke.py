#!/usr/bin/env python3
"""Offline contract/restart/concurrency tests. Synthetic data only."""
from __future__ import annotations
import concurrent.futures
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application.storage_registry import StoreRegistry
from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner_worker import CleanerWorker
from packages.contracts.search_cluster_cleaner import Account,Principal,Profile,Target,CleanerError,digest
from packages.domain.search_cluster_sources import union_snapshot
from packages.domain.search_cluster_classifier import classify

OWNER=Principal("owner",True,True,True)
READER=Principal("reader",True,True,True)
P=dict(nm_id=101,version=1,category="phone_screen_glass",models=["16 promax"],kind="clean",frame="black",source="synthetic fixture",verified_at="2026-09-10T00:00:00Z")

class Clock:
    def __init__(self): self.now=datetime(2026,9,11,1,59,tzinfo=timezone.utc)
    def __call__(self): return self.now.isoformat(timespec="microseconds")
    def advance(self,seconds): self.now+=timedelta(seconds=seconds)

class Source:
    def __init__(self,targets,clock,queries=None): self.targets,self.clock,self.queries=targets,clock,queries or {};self.calls=[]
    def catalog(self): return self.targets,[]
    def snapshot(self,t):
        self.calls.append(t.key)
        q=self.queries.get(t.key,["стекло iphone 16 pro max"])
        if q=="error": raise TimeoutError()
        return union_snapshot(t,list_entry=dict(active=q,excluded=[],archived=[]),stats_queries=q,minus_queries=[],observed_at=self.clock(),source_times={s:self.clock() for s in ("list","statistics","minus")})

class CleanerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.clock=Clock();self.registry=StoreRegistry(Path(self.tmp.name));self.store=CleanerStore(self.registry)
        self.app=KeywordCleaner(self.store,Account("seller","primary"),owner_username="owner",clock=self.clock);self.app.initialize(generation="g1")
        rows=[dict(advert_id=1,nm_id=101,query="стекло iphone 16 pro max",decision="allow",observed_state="active",provenance={"approved":"fixture"})]
        self.app.import_baseline(rows,[P],provenance={"fixture":True},expected_digest=digest(rows),ready=True)
        self.app.update_settings(dict(enabled=True,expected_revision=1,request_id="settings-0001"),OWNER)
        self.t=Target(2,101,contract_verified=True)
    def tearDown(self): self.tmp.cleanup()
    def start(self):
        rid=self.app.start_run(dict(request_id="run-00000001"),OWNER)["run_id"]
        return self.app.claim_run(generation="g1"),rid
    def snapshot(self,queries,minus=(),statistics=None):
        return union_snapshot(self.t,list_entry=dict(active=queries,excluded=list(minus),archived=[]),stats_queries=queries if statistics is None else statistics,minus_queries=list(minus),observed_at=self.clock(),source_times={s:self.clock() for s in ("list","statistics","minus")})
    def record(self,snap):
        run,rid=self.start();self.app.sync_catalog(rid,run["worker_token"],"g1",[self.t],[])
        self.app.record_snapshot(rid,run["worker_token"],"g1",snap)
        return run,rid
    def test_schema_and_baseline_pin(self):
        with self.store.read() as c:
            self.assertEqual(c.execute("PRAGMA query_only").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM cleaner_baselines").fetchone()[0],1)
        with self.assertRaises(Exception):
            with self.store.transaction() as c: c.execute("UPDATE cleaner_baselines SET observed_state='excluded'")
    def test_source_union_exact_and_missing(self):
        s=self.snapshot(["Стекло", "стекло"],minus=["old"],statistics=["Стекло","new"])
        self.assertTrue(s.complete);self.assertEqual(s.queries["new"],"statistics");self.assertEqual(len(s.queries),4)
        m=union_snapshot(self.t,list_entry=None,stats_queries=[],minus_queries=[],observed_at=self.clock(),source_times={})
        self.assertFalse(m.complete)
        n=union_snapshot(self.t,list_entry=dict(active=None,excluded=[],archived=[]),stats_queries=[],minus_queries=[],observed_at=self.clock(),source_times={s:self.clock() for s in ("list","statistics","minus")})
        self.assertFalse(n.complete)
    def test_excluded_is_not_semantic_label(self):
        run,rid=self.record(self.snapshot([],minus=["стекло iphone 16 pro max"]))
        with self.store.read() as c:
            row=c.execute("SELECT state,decision_id FROM cleaner_observations WHERE target='2:101'").fetchone()
            self.assertEqual(tuple(row),("observed_excluded",None))
        self.assertEqual(self.app.pending_candidates(rid),[])
    def test_stats_only_and_known_not_reclassified(self):
        q="стекло iphone 15 pro max"
        run,rid=self.record(self.snapshot([],statistics=[q]))
        candidates=self.app.pending_candidates(rid);self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]["observed_state"],"statistics")
        self.assertEqual(candidates[0]["query"],q)
        self.app.finish_run(rid,run["worker_token"],"g1")
        with self.store.read() as c:
            baseline=c.execute("SELECT decision_id FROM cleaner_observations WHERE target='1:101'").fetchone()[0]
        self.assertTrue(baseline)
    def test_owner_and_request_recovery(self):
        with self.assertRaises(CleanerError) as e:self.app.start_run(dict(request_id="reader-0001"),READER)
        self.assertEqual(e.exception.http_status,403)
        a=self.app.start_run(dict(request_id="run-00000001"),OWNER)
        self.assertEqual(a,self.app.start_run(dict(request_id="run-00000001"),OWNER))
        self.assertEqual(a,self.app.get_request("run-00000001",OWNER))
        with self.assertRaises(CleanerError) as e:self.app.get_request("run-00000001",READER)
        self.assertEqual(e.exception.http_status,404)
        with self.assertRaises(CleanerError):self.app.start_run(dict(request_id="run-00000001",extra=True),OWNER)
    def test_single_worker_and_stale_lease(self):
        self.app.start_run(dict(request_id="run-00000001"),OWNER)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.app.claim_run(generation="g1"),range(2)))
        self.assertEqual(sum(r is not None for r in results),1)
        old=next(r for r in results if r)
        self.clock.advance(181)
        restarted=KeywordCleaner(CleanerStore(self.registry),self.app.account,owner_username="owner",clock=self.clock)
        new=restarted.claim_run(generation="g1")
        self.assertNotEqual(new["worker_token"],old["worker_token"])
        with self.assertRaises(CleanerError):self.app.renew_lease(old["run_id"],old["worker_token"],"g1",phase="reading")
    def test_review_concurrent_decision_and_manual_job(self):
        run,rid=self.record(self.snapshot(["стекло iphone 16 ultra"]))
        self.app.finish_run(rid,run["worker_token"],"g1")
        review=self.app.reviews(OWNER)["items"][0]
        def decide(value):
            try:return self.app.decide(review["review_id"],dict(decision=value,expected_revision=review["revision"],request_id=f"decision-{value}-0001"),OWNER)
            except CleanerError as e:return e.http_status
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(decide,["exclude","allow"]))
        self.assertEqual(sum(r==409 for r in results),1)
        winner=next(r for r in results if isinstance(r,dict))
        self.assertEqual(self.app.reviews(OWNER)["items"],[])
        if winner["run_id"]:
            job=self.app.claim_run(generation="g1");self.assertEqual(job["kind"],"manual_apply")
            self.assertEqual(len(self.app.pending_candidates(job["run_id"])),1)
    def test_profile_draft_semantics_and_old_pin(self):
        before=self.app.get_profile(101,OWNER)
        p=dict(P,source="new source",verified_at=self.clock())
        d=self.app.create_profile(101,dict(profile=p,expected_revision=1,request_id="profile-new-001"),OWNER)
        self.assertEqual(self.app.get_profile(101,OWNER)["active_version"],1)
        self.app.activate_profile(101,dict(version=d["profile_version"],expected_revision=2,request_id="activate-new-01"),OWNER)
        self.assertEqual(before["versions"][0]["semantic_fingerprint"],self.app.get_profile(101,OWNER)["versions"][0]["semantic_fingerprint"])
        with self.store.read() as c:self.assertEqual(c.execute("SELECT count(*) FROM cleaner_auto_decisions").fetchone()[0],1)
    def test_schedule_dates_and_missed_days(self):
        self.assertIsNone(self.app.scheduler_tick())
        self.clock.advance(61)
        rid=self.app.scheduler_tick();self.assertEqual(rid,self.app.scheduler_tick())
        run=self.app.claim_run(generation="g1");self.app.finish_run(rid,run["worker_token"],"g1")
        self.clock.advance(3*86400)
        rid2=self.app.scheduler_tick();self.assertNotEqual(rid,rid2)
        with self.store.read() as c:self.assertEqual(c.execute("SELECT count(*) FROM cleaner_schedule_dates").fetchone()[0],2)
    def test_manual_before_schedule_rereads_early_target(self):
        run,rid=self.record(self.snapshot(["стекло iphone 16 pro max"]))
        self.assertIsNone(self.app.next_scan_target(rid,run["worker_token"],"g1"))
        self.clock.advance(61);self.assertEqual(self.app.scheduler_tick(),rid)
        self.assertEqual(self.app.next_scan_target(rid,run["worker_token"],"g1"),self.t)
    def test_budget_cursor_and_fault_isolation(self):
        targets=[Target(i,101,contract_verified=True) for i in (2,3,4)]
        source=Source(targets,self.clock,{"2:101":"error"})
        for i in range(3):
            self.app.start_run(dict(request_id=f"run-cursor-{i}"),OWNER)
            CleanerWorker(self.app,source,generation="g1",max_targets=1).tick()
        self.assertEqual(source.calls,["2:101","3:101","4:101"])
    def test_missing_profile_not_final(self):
        self.t=Target(2,999,contract_verified=True)
        run,rid=self.record(self.snapshot(["стекло iphone 16 pro max"]))
        with self.store.read() as c:self.assertEqual(c.execute("SELECT state FROM cleaner_observations WHERE target='2:999'").fetchone()[0],"profile_required")
        self.assertEqual(self.app.pending_candidates(rid),[])
    def test_review_membership_cas_and_disappeared_question(self):
        q="стекло iphone 16 ultra"
        run,rid=self.record(self.snapshot([q]));old=self.app.reviews(OWNER)["items"][0]
        self.t=Target(3,101,contract_verified=True)
        self.app.record_snapshot(rid,run["worker_token"],"g1",self.snapshot([q]))
        new=self.app.reviews(OWNER)["items"][0]
        self.assertEqual(new["campaign_count"],2);self.assertGreater(new["revision"],old["revision"])
        with self.assertRaises(CleanerError) as e:self.app.decide(old["review_id"],dict(decision="exclude",expected_revision=old["revision"],request_id="stale-decision-01"),OWNER)
        self.assertEqual(e.exception.http_status,409)
        for cid in (2,3):
            self.t=Target(cid,101,contract_verified=True)
            self.app.record_snapshot(rid,run["worker_token"],"g1",self.snapshot([],minus=[q]))
        self.assertEqual(self.app.reviews(OWNER)["items"],[])
    def test_due_retry_error_preserves_checked_counts(self):
        run,rid=self.record(self.snapshot(["стекло iphone 16 pro max"]))
        self.clock.advance(61);self.app.scheduler_tick()
        self.app.record_target_error(rid,run["worker_token"],"g1",self.t,"timeout")
        result=self.app.finish_run(rid,run["worker_token"],"g1")
        self.assertEqual(result["summary"]["new_checked"],1);self.assertEqual(result["state"],"partial")
    def test_missing_minus_with_only_statistics_holds_target(self):
        q="стекло iphone 16 pro max"
        run,rid=self.record(self.snapshot([],minus=[q]))
        self.app.record_snapshot(rid,run["worker_token"],"g1",self.snapshot([],statistics=[q]))
        self.assertEqual(self.app.summary(OWNER)["target_holds"],1)
    def test_semantic_profile_changes_invalidate_pending_override(self):
        q="стекло iphone 16 ultra"
        run,rid=self.record(self.snapshot([q]));review=self.app.reviews(OWNER)["items"][0]
        result=self.app.decide(review["review_id"],dict(decision="exclude",expected_revision=review["revision"],request_id="decision-override-01"),OWNER)
        self.assertEqual(len(self.app.pending_candidates(result["run_id"])),1)
        self.app.create_profile(101,dict(profile=dict(P,kind="anti"),expected_revision=1,request_id="new-semantic-01"),OWNER)
        self.app.activate_profile(101,dict(version=2,expected_revision=2,request_id="activate-semantic-01"),OWNER)
        self.assertEqual(self.app.pending_candidates(result["run_id"]),[])
        self.assertEqual(len(self.app.reviews(OWNER)["items"]),1)
    def test_exact_override_reused_only_in_new_same_sku(self):
        q="стекло iphone 16 ultra"
        run,rid=self.record(self.snapshot([q]));review=self.app.reviews(OWNER)["items"][0]
        self.app.decide(review["review_id"],dict(decision="allow",expected_revision=review["revision"],request_id="decision-reuse-01"),OWNER)
        self.t=Target(3,101,contract_verified=True)
        self.app.record_snapshot(rid,run["worker_token"],"g1",self.snapshot([q]))
        with self.store.read() as c:self.assertEqual(c.execute("SELECT state FROM cleaner_observations WHERE target='3:101'").fetchone()[0],"allow")
        self.t=Target(4,999,contract_verified=True)
        self.app.record_snapshot(rid,run["worker_token"],"g1",self.snapshot([q]))
        with self.store.read() as c:self.assertEqual(c.execute("SELECT state FROM cleaner_observations WHERE target='4:999'").fetchone()[0],"profile_required")
    def test_rule_activation_keeps_prior_decision_identity(self):
        q="стекло iphone 16 pro max"
        run,rid=self.record(self.snapshot([q]))
        with self.store.read() as c:old=c.execute("SELECT decision_id FROM cleaner_observations WHERE target='2:101'").fetchone()[0]
        app2=KeywordCleaner(self.store,self.app.account,owner_username="owner",clock=self.clock,rules_version="synthetic-r2",classifier=lambda q,p:dict(verdict="review",rule="SYNTHETIC",reason="fixture changed rule"))
        app2.activate_rules(expected_revision=2,provenance="synthetic rule activation")
        app2.record_snapshot(rid,run["worker_token"],"g1",self.snapshot([q]))
        with self.store.read() as c:self.assertEqual(c.execute("SELECT decision_id FROM cleaner_observations WHERE target='2:101'").fetchone()[0],old)
        self.t=Target(3,101,contract_verified=True);app2.record_snapshot(rid,run["worker_token"],"g1",self.snapshot([q]))
        with self.store.read() as c:self.assertEqual(c.execute("SELECT d.rules_version FROM cleaner_observations o JOIN cleaner_auto_decisions d USING(decision_id) WHERE o.target='3:101'").fetchone()[0],"synthetic-r2")
    def test_disabled_preserves_pending_and_generation_fences(self):
        run,rid=self.start();self.app.update_settings(dict(enabled=False,expected_revision=2,request_id="disable-0001"),OWNER)
        self.assertIsNone(self.app.claim_run(generation="g1"))
        self.assertEqual(self.app.finish_run(rid,run["worker_token"],"g1")["state"],"stopped")
        with self.assertRaises(CleanerError):self.app.claim_run(generation="different-generation")

    def test_fast_get_while_read_is_waiting(self):
        entered=threading.Event();release=threading.Event()
        source=Source([self.t],self.clock)
        original=source.snapshot
        def slow(target):entered.set();time.sleep(30);return original(target)
        source.snapshot=slow
        self.app.start_run(dict(request_id="run-slow-001"),OWNER)
        worker=threading.Thread(target=lambda:CleanerWorker(self.app,source,generation="g1").tick());worker.start();self.assertTrue(entered.wait(2))
        timings=[]
        try:
            for _ in range(100):
                start=time.perf_counter();self.app.summary(READER);timings.append(time.perf_counter()-start)
            self.assertTrue(worker.is_alive());self.assertLess(sorted(timings)[94],.5)
        finally:release.set();worker.join(32)
        self.assertFalse(worker.is_alive())
        print(json.dumps(dict(summary_gets=100,p95_ms=round(sorted(timings)[94]*1000,3),source_wait_seconds=30,network_calls=0)))

class ClassifierDevelopmentTests(unittest.TestCase):
    def test_project_edges(self):
        cases={
          "стекло iphone 16 pro max не remax":"review",
          "стекло iphone 16 pro max без салфетки":"review",
          "стекло iphone 16 pro max 3 штуки":"allow",
          "стекло iphone 16 ultra":"review",
          "стекло iphone 16 pro max 100d":"allow",
          "стекло в упаковке iphone 16 pro max":"allow",
          "стекло iphone 16 pro max в упаковке":"allow",
          "стекло с салфеткой iphone 16 pro max":"allow",
          "стекло iphone 16 pro max с салфеткой":"allow",
          "стикло iphone 16 pro max":"allow",
          "cтекло iphone 16 pro max":"allow",
          "стекло iphone 16 pro max под чехол":"allow",
          "стекло iphone 16 pro max и чехол":"exclude",
          "стекло iphone 16 pro max вырез под камеру":"allow",
          "стекло iphone 16 pro max на камеру":"exclude",
          "стекло iphone 16 pro max только не чехол":"review",
          "стекло 20":"review",
          "стекло iphone 16 pro max 2.5d":"allow",
          "стекло iphone 16 pro max 0.3 мм":"allow",
        }
        for q,expected in cases.items():
            with self.subTest(query=q):self.assertEqual(classify(q,P)["verdict"],expected)
    def test_profile_provenance_types_fail_controlled(self):
        for field,value in (("source",None),("source",42),("verified_at",123),("verified_at",None),("verified_at","2026-09-11")):
            with self.subTest(field=field,value=value):
                with self.assertRaises(CleanerError):Profile.parse(dict(P,**{field:value}))
                self.assertEqual(classify("стекло iphone 16 pro max",dict(P,**{field:value}))["verdict"],"review")

    def test_pure_and_invalid_profile(self):
        q="стекло iphone 16 pro max"
        self.assertEqual(classify(q,P),classify(q,dict(reversed(list(P.items())))))
        self.assertEqual(classify(q,dict(P,models=["20 ultra"]))["verdict"],"review")
        self.assertEqual(classify(None,P)["verdict"],"review")

if __name__=="__main__":unittest.main(verbosity=2)
