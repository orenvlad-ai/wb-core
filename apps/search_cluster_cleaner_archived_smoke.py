"""Archive retirement through native classifier, guarded writer and readback."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unittest
from dataclasses import replace
from apps.search_cluster_cleaner_write_fixture import fixture,Q1,Q2
from packages.contracts.search_cluster_cleaner import query_hash
from packages.application.search_cluster_cleaner_archived import rejected_archived_queries


def override(f,query,revision=1,verdict='exclude'):
    with f.store.transaction() as c:
        fp=f.app._profile(c,101).semantic_fingerprint
        c.execute('INSERT INTO cleaner_manual_overrides VALUES(?,?,?,?,?,?,?,?,?)',(f.app.key,101,query_hash(query),query,revision,verdict,fp,'owner',f.clock()))
        c.execute('INSERT OR REPLACE INTO cleaner_override_heads VALUES(?,?,?,?,0)',(f.app.key,101,query_hash(query),revision))


class ArchiveTests(unittest.TestCase):
    def test_full_set_retirement_preserves_override_and_reactivation(self):
        for origin in ('override','baseline'):
            with fixture() as f:
                q='OLD'
                if origin=='override':override(f,q)
                else:
                    with f.store.transaction() as c:
                        profile=f.app._profile(c,101)
                        decision=f.app._decision(c,'11:101',q,dict(verdict='exclude',rule='APPROVED_BASELINE',reason='fixture'),profile,'baseline')
                        c.execute('INSERT INTO cleaner_baselines VALUES(?,?,?,?,?,?,?,?,?,?,?)',(f.app.key,'11:101',11,101,query_hash(q),q,'excluded',decision,'{}','fixture',f.clock()))
                f.fake.targets[11]['archived']=[q]
                before=set(f.fake.targets[11]['minus']);saved=f.rows('cleaner_manual_overrides')+f.rows('cleaner_baselines')
                f.start()
                with f.worker() as (worker,_,__):worker.tick()
                self.assertEqual(len(f.fake.writes),1)
                self.assertEqual(set(f.fake.targets[11]['minus']),(before-{q})|{Q1,Q2})
                self.assertEqual(f.rows('cleaner_manual_overrides')+f.rows('cleaner_baselines'),saved)
                op=f.rows('cleaner_write_operations')[0]
                self.assertEqual(op['state'],'confirmed');self.assertEqual(op['dispatch_count'],1)
                self.assertEqual(json.loads(op['versions'])['archive_retirement']['queries'],[q])
                self.assertEqual(f.count('change_registry_facts'),3)
                from packages.application.search_cluster_cleaner_daily import result_summary
                batch=dict(items=[dict(advert_id=11,nm_id=101,write_run_id=op['run_id'],
                    state='complete',stage='finished',confirmed_excluded=0,returned=0,
                    deferred_count=0,delivery_state='confirmed')],state='complete',failed_count=0,held_count=0)
                # Intentionally stale counters cannot produce a negative return
                # or erase confirmed exclusions: all counts use one ledger read.
                report=result_summary(f.app,batch)
                self.assertEqual((report['confirmed_excluded'],report['archived_retired'],report['confirmed_returned']),(2,1,0))

                f.clock.advance(30);f.start()
                with f.worker() as (worker,_,__):worker.tick()
                self.assertEqual(len(f.fake.writes),1)
                # The original exclude override still governs a reactivated query.
                f.fake.targets[11]['archived']=[];f.fake.targets[11]['active']=[q];f.fake.targets[11]['stats'].append(q)
                f.clock.advance(30);f.start()
                with f.worker() as (worker,_,__):worker.tick()
                self.assertEqual(len(f.fake.writes),2);self.assertIn(q,f.fake.targets[11]['minus'])
                self.assertEqual(f.rows('cleaner_manual_overrides')+f.rows('cleaner_baselines'),saved)

    def test_daily_summary_keeps_confirmed_archive_in_unresolved_operation(self):
        from packages.application.search_cluster_cleaner_daily import result_summary
        with fixture() as f:
            f.fake.targets[11]['archived']=['OLD'];f.fake.mode='extra'
            f.start()
            with f.worker() as (worker,_,__):worker.tick()
            op=f.rows('cleaner_write_operations')[0]
            self.assertNotEqual(op['state'],'confirmed')
            batch=dict(items=[dict(advert_id=11,nm_id=101,write_run_id=op['run_id'],state='partial',
                stage='finished',confirmed_excluded=2,returned=1,deferred_count=0,delivery_state='wb_pending')],
                state='partial',failed_count=0,held_count=1)
            report=result_summary(f.app,batch)
            self.assertEqual((report['confirmed_excluded'],report['archived_retired'],report['confirmed_returned']),(2,1,0))
            self.assertEqual(report['unknown_pairs'],1)

    def test_archive_and_override_drift_cancel_before_post(self):
        for mutation in ('reactivated','override','missing_minus','incomplete','legacy_snapshot'):
            with self.subTest(mutation=mutation),fixture() as f:
                q='OLD';override(f,q);f.fake.targets[11]['archived']=[q]
                original=f.source.snapshot
                def hook(stage,op):
                    if stage!='after_prepare':return
                    if mutation=='reactivated':f.fake.targets[11]['archived']=[]
                    elif mutation=='override':override(f,q,2,'allow')
                    elif mutation=='missing_minus':f.fake.targets[11]['minus'].remove(q)
                    elif mutation=='incomplete':f.source.snapshot=lambda t:replace(original(t),complete=False,reasons=('fixture_missing',))
                    else:f.source.snapshot=lambda t:replace(original(t),archived=())
                f.start()
                with f.worker(hook=hook) as (worker,_,__):worker.tick()
                self.assertEqual(f.fake.writes,[]);self.assertEqual(f.count('change_registry_operations'),0)

    def test_only_archived_without_minus_never_becomes_return(self):
        with fixture() as f:
            f.fake.targets[11]['archived']=['already archived'];f.fake.targets[11]['stats']=[]
            f.start()
            with f.worker() as (worker,_,__):worker.tick()
            self.assertEqual(f.fake.writes,[])

    def test_rejected_operation_is_retained_new_operation_retires_archive(self):
        with fixture() as f:
            q='OLD';f.fake.archived_rejection_queries=[q];f.fake.mode='400_archived'
            f.start()
            with f.worker() as (worker,_,__):worker.tick()
            old=f.rows('cleaner_write_operations')[0];self.assertEqual(old['state'],'rejected')
            self.assertEqual(f.count('cleaner_target_holds'),1)
            f.fake.mode='normal';f.fake.targets[11]['archived']=[q];f.clock.advance(30);f.start()
            with f.worker() as (worker,_,__):worker.tick()
            self.assertEqual(f.count('cleaner_target_holds'),0)
            ops=f.rows('cleaner_write_operations');self.assertEqual(len(ops),2);self.assertEqual(ops[0],old)
            self.assertEqual(ops[1]['state'],'confirmed');self.assertEqual(len(f.fake.writes),2)
            self.assertNotIn(q,f.fake.targets[11]['minus'])
            self.assertEqual(sum(r['kind']=='archived_rejection_reconciled' for r in f.rows('cleaner_events')),1)

    def test_recovery_retains_hold_without_exact_fresh_archive_evidence(self):
        for mutation in ('not_archived','minus_changed','foreign_target','stale','pending_operation','confirmed_item'):
            with self.subTest(mutation=mutation),fixture() as f:
                q='OLD';f.fake.archived_rejection_queries=[q];f.fake.mode='400_archived'
                f.start()
                with f.worker() as (worker,_,__):worker.tick()
                f.fake.mode='normal';f.fake.targets[11]['archived']=[q];f.clock.advance(30)
                if mutation=='not_archived':f.fake.targets[11]['archived']=[]
                elif mutation=='minus_changed':f.fake.targets[11]['minus'].append('external')
                elif mutation=='confirmed_item':
                    with f.store.transaction() as c:
                        c.execute('UPDATE cleaner_write_items SET confirmed_at=?',(f.clock(),))
                elif mutation=='pending_operation':
                    # Existing unresolved writes are never bypassed by archive policy.
                    with f.store.transaction() as c:
                        c.execute("INSERT INTO cleaner_write_operations SELECT 'foreign-pending',account,target,run_id,'prepared',before_json,expected_json,additions,candidate_digest,versions,worker_token,worker_generation,0,created_at,updated_at,NULL,evidence FROM cleaner_write_operations LIMIT 1")
                original=f.source.snapshot
                if mutation=='stale':f.source.snapshot=lambda t:replace(original(t),source_times=dict(list='2020-01-01T00:00:00Z',statistics=f.clock(),minus=f.clock()))
                if mutation=='foreign_target':f.source.snapshot=lambda t:replace(original(t),target=replace(t,advert_id=12))
                f.start()
                with f.worker() as (worker,_,__):worker.tick()
                self.assertEqual(f.count('cleaner_target_holds'),1);self.assertEqual(len(f.fake.writes),1)

    def test_rejection_parser_fails_closed(self):
        body=dict(detail="cannot exclude archived presets: 'OLD'",status=400,title='failed to set norm query minus',origin='camp-api-public-cache')
        receipt=dict(status=400,body_excerpt=json.dumps(body),body_truncated=False)
        self.assertEqual(rejected_archived_queries(receipt),('OLD',))
        self.assertEqual(rejected_archived_queries(dict(receipt,body_truncated=True)),())
        for detail in ("cannot exclude archived presets: 'O'LD'",'unknown rejection',"cannot exclude archived presets: 'OLD', 'OLD'"):
            self.assertEqual(rejected_archived_queries(dict(receipt,body_excerpt=json.dumps(dict(body,detail=detail)))),())

    def test_known_rejection_of_return_plan_is_terminal_without_facts(self):
        with fixture() as f:
            f.fake.targets[11]['archived']=['OLD'];f.fake.mode='400_archived'
            f.start()
            with f.worker() as (worker,_,__):worker.tick()
            self.assertEqual(f.rows('cleaner_write_operations')[0]['state'],'rejected')
            self.assertEqual(f.count('change_registry_facts'),0)
            self.assertEqual(f.rows('cleaner_readback_jobs')[0]['state'],'done')


if __name__=='__main__':unittest.main()
