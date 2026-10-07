#!/usr/bin/env python3
"""Synthetic native first-fill; private scope, durable claims and strict readback."""
from pathlib import Path
import sys,json,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.search_cluster_cleaner_write_fixture import fixture,OWNER,Q1,PROFILE
from packages.application.search_cluster_cleaner_initial_empty import execute,journal,sha,SCHEMA,InitialEmptyPolicy,build_declaration
from packages.application.search_cluster_cleaner_worker import ManualCleanerWorker
from packages.application.search_cluster_cleaner_self_service import ManualCleanerCoordinator
from packages.contracts.search_cluster_cleaner import canonical,digest,CleanerError,Target
from packages.adapters.search_cluster_cleaner_wb import WbReadError

RUNTIME='a'*40
class Crash(BaseException):pass


def install(f,hook=None,body_change=None):
    directory=f.guard.directory
    with f.store.transaction() as c:
        c.execute('UPDATE cleaner_settings SET enabled=0 WHERE account=?',(f.app.key,))
        fingerprint=f.app._profile(c,101).semantic_fingerprint
    f.guard.hold(reason='manual fixture')
    rows=[dict(advert_id=11,nm_id=101,profile_fingerprint=fingerprint)]
    body=dict(schema=SCHEMA,evidence_id='synthetic-first-fill',account_key=f.app.key,generation='g1',owner_username='owner',confirmed_at=f.clock(),confirmation='Owner confirmed exact pair has never had minus phrases.',scope_sha256='sha256:'+digest(rows),runtime_sha=RUNTIME,rules_digest=f.app.rules_digest,targets=rows)
    if body_change:body_change(body)
    path=directory/'initial-empty.synthetic-first-fill.json';path.write_text(canonical(body));path.chmod(0o600)
    request=dict(mode='initial_empty',evidence_id=body['evidence_id'],evidence_sha256=sha(path.read_bytes()))
    options=dict(operation_id='synthetic-install-first-fill',request=request,cleaner=f.app,directory=directory,generation='g1',runtime_sha=RUNTIME,actor='synthetic-operator')
    preview=execute(action='preview',**options)
    execute(action='apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],hook=hook,**options)
    f.clock.advance(1)
    return options,preview


def missing_when_empty(f,always=False):
    original=f.fake.response
    def response(method,path,payload):
        status,body=original(method,path,payload)
        if path.endswith('/get-minus') and (always or not f.fake.targets[11]['minus']):return 200,{'items':[]}
        return status,body
    f.fake.response=response


def scan(f,request_id):
    t=f.source.catalog()[0][0]
    job=f.app.start_manual_clean(dict(request_id=request_id,advert_id=11,nm_id=101),OWNER)
    f.source.initial_empty_policy=InitialEmptyPolicy(f.app,f.guard.directory,'g1',job['run_id'])
    worker=ManualCleanerWorker(f.app,f.source,f.guard,generation='g1')
    before=worker.preview(run_id=job['run_id'],targets=[t])
    result=worker.execute(run_id=job['run_id'],targets=[t],expected_prestate=before['prestate_sha256'],expected_candidate=before['candidate_sha256'],production_operation_id=request_id+'-scan')
    return t,job,result


def write(f,t,job,hook=None):
    candidate=f.app.manual_apply_preview(job['run_id'],t)
    prepared=f.app.prepare_manual_apply(job['run_id'],t,candidate['candidate_sha256'],job['job_id']+'-prepare',OWNER)
    run_id=prepared['run_id']
    f.source.initial_empty_policy=InitialEmptyPolicy(f.app,f.guard.directory,'g1',run_id)
    worker=ManualCleanerWorker(f.app,f.source,f.guard,generation='g1')
    preview=worker.preview(run_id=run_id,targets=[t])
    # Hook into the native writer only when simulating a process crash.
    if hook:
        from unittest.mock import patch
        from packages.application.search_cluster_cleaner_writer import CleanerWriter
        original=CleanerWriter.__init__
        def init(self,*args,**kwargs):original(self,*args,hook=hook,**kwargs)
        with patch.object(CleanerWriter,'__init__',init):
            worker.execute(run_id=run_id,targets=[t],expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],production_operation_id=job['job_id']+'-write')
    else:
        result=worker.execute(run_id=run_id,targets=[t],expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],production_operation_id=job['job_id']+'-write')
        return run_id,result


class InitialEmptyTests(unittest.TestCase):
    def test_canonical_operator_install_no_wb_or_business_change_and_duplicate_readback(self):
        from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox,RUNTIME_SHA
        from apps.search_cluster_cleaner_onboarding_smoke import setup,old_state,write_private
        from apps import search_cluster_cleaner_stage_e as stage_e
        from apps.production_apply_launcher import execute as launcher
        from packages.application.search_cluster_cleaner_self_service import LocalStageEAdapter
        with Sandbox() as box:
            setup(box);service=box.service();before=old_state(box)
            body=build_declaration(cleaner=service,evidence_id='synthetic-owner-pilot',pairs=[dict(advert_id=11,nm_id=101)],runtime_sha=RUNTIME_SHA,confirmed_at=service.clock(),confirmation='Owner confirmed first launch and no minus phrases for this exact pair.')
            request=dict(mode='initial_empty',evidence_id=body['evidence_id'],evidence_sha256=write_private(box.admission/'initial-empty.synthetic-owner-pilot.json',body))
            adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
            args=dict(adapter_name='search_cluster_cleaner_manual_v1',operation_id='synthetic-owner-pilot-install',request=request,adapters={'search_cluster_cleaner_manual_v1':adapter})
            with patch.object(stage_e,'fetch_current_card',side_effect=AssertionError('install contacted WB')),patch.object(stage_e.CleanerWbSource,'from_env',side_effect=AssertionError('install constructed WB source')):
                preview=launcher(action='preview',**args)
                result=launcher(action='apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],**args)
                self.assertEqual(result['state'],'applied');self.assertFalse(result['readback']['cleaning_started'])
                self.assertEqual(launcher(action='apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],**args)['state'],'applied')
                self.assertEqual(launcher(action='readback',**args)['state'],'applied')
            self.assertEqual(old_state(box),before)
            self.assertEqual(len(journal(box.admission)['installations']),1)

    def test_exact_pair_cpm_and_bound_declaration_proof(self):
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[Q1]);options,_=install(f);missing_when_empty(f)
            t,job,_=scan(f,'native-proof-guard')
            snapshot=f.source.snapshot(t);policy=f.source.initial_empty_policy
            from dataclasses import replace
            self.assertFalse(policy.resolve(replace(snapshot,target=Target(12,101),complete=False,reasons=('minus_pair_omitted',),initial_empty_evidence=None)).complete)
            self.assertFalse(policy.resolve(replace(snapshot,target=Target(11,101,payment_type='cpc'),complete=False,reasons=('minus_pair_omitted',),initial_empty_evidence=None)).complete)
            self.assertEqual(f.fake.writes,[])
            from packages.application.search_cluster_cleaner_writer import CleanerWriter
            original=CleanerWriter.admit
            def changed_proof(writer,operation,run_id,token,fresh,membership):
                altered=dict(fresh.initial_empty_evidence,evidence_sha256='sha256:'+'0'*64)
                return original(writer,operation,run_id,token,replace(fresh,initial_empty_evidence=altered),membership)
            with patch.object(CleanerWriter,'admit',changed_proof):
                with self.assertRaises(CleanerError) as error:write(f,t,job)
            self.assertEqual(error.exception.code,'initial_empty_preflight_drift')
            self.assertEqual(f.fake.writes,[])
            self.assertNotEqual(journal(f.guard.directory)['pairs']['11:101']['state'],'available')

    def test_duplicate_prepare_does_not_cancel_first_claim_or_send_again(self):
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[Q1]);install(f);missing_when_empty(f)
            t,job,_=scan(f,'native-duplicate-prepare')
            from packages.application.search_cluster_cleaner_writer import CleanerWriter
            original=CleanerWriter.prepare
            def duplicate(writer,run_id,token,snapshot,candidates,membership):
                operation=original(writer,run_id,token,snapshot,candidates,membership)
                with self.assertRaises(CleanerError) as error:original(writer,run_id,token,snapshot,candidates,membership)
                self.assertEqual(error.exception.code,'initial_empty_claim_lost')
                row=f.rows('cleaner_write_operations')[0]
                self.assertEqual(row['operation_id'],operation);self.assertEqual(row['state'],'prepared')
                return operation
            with patch.object(CleanerWriter,'prepare',duplicate):write(f,t,job)
            self.assertEqual(len(f.fake.writes),1);self.assertEqual(f.count('cleaner_write_operations'),1)

    def test_install_identity_and_preview_cas_reject_drift(self):
        for field,value in [('runtime_sha','f'*40),('rules_digest','f'*64),('generation','other'),('account_key','other')]:
            with fixture() as f:
                f.fake.targets[11].update(minus=[],stats=[])
                with self.assertRaises(CleanerError) as error:install(f,body_change=lambda body:body.update({field:value}))
                self.assertEqual(error.exception.code,'initial_empty_binding_mismatch')
                self.assertFalse((f.guard.directory/'initial-empty-journal.json').exists())
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[]);options,_=install(f)
            path=f.guard.directory/'initial-empty.synthetic-first-fill.json'
            body=json.loads(path.read_text());body['evidence_id']='synthetic-owner-second';body['targets'][0]['advert_id']=12;body['scope_sha256']='sha256:'+digest(body['targets'])
            next_path=f.guard.directory/'initial-empty.synthetic-owner-second.json';next_path.write_text(canonical(body));next_path.chmod(0o600)
            next_options=dict(options,operation_id='synthetic-owner-second-install',request=dict(mode='initial_empty',evidence_id=body['evidence_id'],evidence_sha256=sha(next_path.read_bytes())))
            preview=execute(action='preview',**next_options)
            with f.store.transaction() as c:c.execute('UPDATE cleaner_settings SET revision=revision+1 WHERE account=?',(f.app.key,))
            with self.assertRaises(CleanerError) as error:execute(action='apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],**next_options)
            self.assertEqual(error.exception.code,'initial_empty_prestate_drift')
            self.assertNotIn('12:101',journal(f.guard.directory)['pairs'])

    def test_no_candidates_keeps_available_then_later_real_first_fill_and_strict_readback(self):
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=['стекло iphone 16 pro max'])
            options,preview=install(f);missing_when_empty(f)
            before_settings=f.rows('cleaner_settings');t,job,result=scan(f,'native-empty-no-candidates')
            self.assertEqual(result['state'],'complete');self.assertEqual(f.fake.writes,[])
            self.assertEqual(journal(f.guard.directory)['pairs']['11:101']['state'],'available')
            coordinator=ManualCleanerCoordinator(f.app,Mock());coordinator._settle(job['job_id'],'scan','no_change',{});coordinator.tick()
            self.assertEqual(f.app.manual_job(job['job_id'],OWNER)['state'],'no_change')
            self.assertEqual(f.rows('cleaner_settings'),before_settings)
            f.fake.targets[11]['stats']=[Q1];f.clock.advance(1)
            t,job,result=scan(f,'native-empty-real-candidate');self.assertEqual(result['summary']['would_exclude'],1)
            run_id,result=write(f,t,job)
            self.assertEqual(f.fake.writes,[dict(advert_id=11,nm_id=101,norm_queries=[Q1])])
            self.assertEqual(result['summary']['confirmed_pilot'],1)
            self.assertEqual(f.rows('cleaner_write_operations')[0]['dispatch_count'],1)
            self.assertEqual(json.loads(f.rows('cleaner_write_operations')[0]['versions'])['initial_empty_evidence']['basis'],'owner_confirmed_initial_empty')
            self.assertNotEqual(journal(f.guard.directory)['pairs']['11:101']['state'],'available')
            # Duplicate installation cannot rearm the pair.
            again=execute(action='preview',**options);self.assertEqual(again['candidate_sha256'],preview['candidate_sha256'])
            self.assertEqual(execute(action='readback',**options)['state'],'applied')
            f.fake.targets[11]['minus']=[]
            self.assertFalse(f.source.snapshot(t).complete)
            with self.assertRaises(WbReadError):f.source.read_minus(t)

    def test_process_crash_after_claim_and_db_rollback_never_reopen(self):
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[Q1]);install(f);missing_when_empty(f)
            t,job,_=scan(f,'native-empty-crash')
            def crash(stage,operation):
                if stage=='after_prepare':raise Crash()
            with self.assertRaises(Crash):write(f,t,job,crash)
            self.assertEqual(journal(f.guard.directory)['pairs']['11:101']['state'],'claimed')
            self.assertEqual(f.fake.writes,[])
            # Restoring only the business DB cannot erase the private claim.
            with f.store.transaction() as c:
                c.execute('DELETE FROM cleaner_write_items');c.execute('DELETE FROM cleaner_write_operations')
            f.source.initial_empty_policy=InitialEmptyPolicy(f.app,f.guard.directory,'g1',job['run_id'])
            self.assertFalse(f.source.snapshot(t).complete)

    def test_fsync_claim_survives_actual_prepare_transaction_rollback(self):
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[Q1]);install(f);missing_when_empty(f)
            t,job,_=scan(f,'native-before-commit-crash')
            from unittest.mock import patch
            from packages.application import search_cluster_cleaner_initial_empty as initial
            original=initial.save
            def crashing_save(directory,state):
                original(directory,state)
                if state['pairs']['11:101']['state']=='claimed':raise Crash()
            with patch.object(initial,'save',crashing_save):
                with self.assertRaises(Crash):write(f,t,job)
            self.assertEqual(f.count('cleaner_write_operations'),0)
            self.assertEqual(journal(f.guard.directory)['pairs']['11:101']['state'],'claimed')
            f.source.initial_empty_policy=InitialEmptyPolicy(f.app,f.guard.directory,'g1',job['run_id'])
            f.app.rules_digest='f'*64 # A foreign/stranded claim never uses stale bootstrap bindings.
            self.assertFalse(f.source.snapshot(t).complete);self.assertEqual(f.fake.writes,[])

    def test_empty_queries_repeat_without_consumption_and_daily_cannot_use_evidence(self):
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[]);install(f);missing_when_empty(f)
            for index in range(2):
                t,job,result=scan(f,'native-empty-repeat-'+str(index))
                self.assertEqual(result['summary']['checked_total'],0)
                self.assertEqual(result['state'],'complete')
                coordinator=ManualCleanerCoordinator(f.app,Mock());coordinator._settle(job['job_id'],'scan','no_change',{});coordinator.tick()
                self.assertEqual(f.app.manual_job(job['job_id'],OWNER)['state'],'no_change')
                self.assertEqual(journal(f.guard.directory)['pairs']['11:101']['state'],'available')
                f.clock.advance(1)
            from packages.application.search_cluster_cleaner_batch import BatchCleanerCoordinator
            from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
            admitted=[dict(advert_id=11,nm_id=101,state='verified')]
            frozen=eligibility_rows(f.app,'g1',[t],fixture_admission=admitted)
            batch='synthetic-daily-first-fill'
            f.app.start_manual_batch(dict(request_id=batch,selected_categories=['active'],targets=[dict(advert_id=11,nm_id=101)]),OWNER,snapshot=frozen)
            parent=BatchCleanerCoordinator(f.app,generation='g1',source_factory=lambda:f.source,fixture_admission=admitted)
            child=f.app.manual_job(parent.tick()['items'][0]['job_id'],OWNER)
            with f.store.transaction() as c:
                c.execute("INSERT INTO cleaner_daily_occurrences(account,schedule_id,local_date,due_at,state,batch_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                          (f.app.key,'synthetic-slot','2026-09-11',f.clock(),'running',batch,f.clock(),f.clock()))
            f.source.initial_empty_policy=InitialEmptyPolicy(f.app,f.guard.directory,'g1',child['scan_run_id'])
            self.assertFalse(f.source.snapshot(t).complete)
            self.assertEqual(journal(f.guard.directory)['pairs']['11:101']['state'],'available');self.assertEqual(f.fake.writes,[])

    def test_nonempty_list_with_omitted_pair_closes_durably_and_real_minus_preserved(self):
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[]);install(f);missing_when_empty(f,always=True)
            t,job,_=scan(f,'native-nonempty-fence')
            f.fake.targets[11]['minus']=['Existing exact phrase']
            snap=f.source.snapshot(t);self.assertFalse(snap.complete)
            self.assertEqual(journal(f.guard.directory)['pairs']['11:101']['state'],'closed')
            f.fake.targets[11]['minus']=[];self.assertFalse(f.source.snapshot(t).complete)
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[]);install(f);t,job,_=scan(f,'native-nonempty-full')
            f.fake.targets[11]['minus']=['Existing exact phrase']
            snap=f.source.snapshot(t);self.assertTrue(snap.complete);self.assertEqual(snap.minus,('Existing exact phrase',))
            f.fake.targets[11]['minus']=[];missing_when_empty(f);self.assertFalse(f.source.snapshot(t).complete)

    def test_historical_evidence_does_not_gate_authoritative_closed_or_legacy_snapshots(self):
        for drift in ('rules','profile'):
            with self.subTest(drift=drift),fixture() as f:
                f.fake.targets[11].update(minus=[],stats=[]);install(f)
                t,job,_=scan(f,'native-evidence-lifecycle-'+drift)
                if drift=='rules':f.app.rules_digest='f'*64
                else:
                    revision=f.app.get_profile(101,OWNER)['revision']
                    created=f.app.create_profile(101,dict(request_id='lifecycle-profile-draft',expected_revision=revision,profile=dict(PROFILE,models=['15 promax'])),OWNER)
                    f.app.activate_profile(101,dict(request_id='lifecycle-profile-activate',expected_revision=created['profile_revision'],version=created['profile_version']),OWNER)
                # Authoritative empty WB state is ordinary, even while available.
                snapshot=f.source.snapshot(t);self.assertTrue(snapshot.complete)
                self.assertIsNone(snapshot.initial_empty_evidence)
                f.fake.targets[11]['active']=[Q1]
                self.assertTrue(f.source.snapshot(t).complete) # Exact nonempty list/full empty minus.
                original=f.fake.response;missing_when_empty(f)
                with self.assertRaises(CleanerError) as error:f.source.snapshot(t)
                self.assertEqual(error.exception.code,'initial_empty_binding_mismatch' if drift=='rules' else 'initial_empty_profile_mismatch')
                self.assertEqual(journal(f.guard.directory)['pairs']['11:101']['state'],'available')
                # Legacy/no-intent missing is still unknown, without applying stale evidence.
                f.source.initial_empty_policy=InitialEmptyPolicy(f.app,f.guard.directory,'g1','legacy')
                self.assertFalse(f.source.snapshot(t).complete)
                # A real nonempty observation closes the historical declaration.
                f.source.initial_empty_policy=InitialEmptyPolicy(f.app,f.guard.directory,'g1',job['run_id'])
                f.fake.targets[11]['minus']=['Existing phrase'];self.assertTrue(f.source.snapshot(t).complete)
                self.assertEqual(journal(f.guard.directory)['pairs']['11:101']['state'],'closed')
                f.fake.targets[11]['minus']=[];self.assertFalse(f.source.snapshot(t).complete)
                f.fake.response=original;self.assertTrue(f.source.snapshot(t).complete)
                f.fake.targets[11]['active']=[];self.assertTrue(f.source.snapshot(t).complete)
                self.assertEqual(f.fake.writes,[])

    def test_unknown_readback_after_one_timeout_write_never_confirms_or_retries(self):
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[Q1]);install(f);missing_when_empty(f,always=True)
            t,job,_=scan(f,'native-unknown-readback');f.fake.mode='timeout'
            run_id,result=write(f,t,job)
            self.assertEqual(len(f.fake.writes),1);self.assertEqual(result['summary']['confirmed_pilot'],0)
            from packages.application.search_cluster_cleaner_writer import CleanerReadback
            readback=CleanerReadback(f.app,f.source,generation='g1')
            f.clock.advance(100);readback.tick()
            self.assertEqual(len(f.fake.writes),1);self.assertFalse(f.source.snapshot(t).complete)

    def test_unbound_old_daily_legacy_malformed_and_history_are_denied(self):
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[Q1]);install(f);missing_when_empty(f)
            t=f.source.catalog()[0][0]
            self.assertFalse(f.source.snapshot(t).complete) # No resolver at all.
            f.source.initial_empty_policy=InitialEmptyPolicy(f.app,f.guard.directory,'g1','legacy')
            self.assertFalse(f.source.snapshot(t).complete)
            t,job,_=scan(f,'native-malformed-first-fill')
            original=f.fake.response
            def malformed(method,path,body):
                status,value=original(method,path,body)
                if path.endswith('/list'):value['items'][0]['normQueries']['active']=None
                return status,value
            f.fake.response=malformed;self.assertFalse(f.source.snapshot(t).complete)
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[Q1])
            with f.store.transaction() as c:c.execute('UPDATE cleaner_settings SET enabled=0 WHERE account=?',(f.app.key,))
            old=f.app.start_manual_clean(dict(request_id='old-explicit-intent',advert_id=11,nm_id=101),OWNER)
            f.clock.advance(2);install(f);missing_when_empty(f)
            f.source.initial_empty_policy=InitialEmptyPolicy(f.app,f.guard.directory,'g1',old['run_id'])
            self.assertFalse(f.source.snapshot(f.source.catalog()[0][0]).complete)
        for kind,facts in [('initial_empty_nonempty',{}),('external_state_drift',{}),
                           ('controversial_decision',dict(before='excluded')),
                           ('partial_snapshot_preview',dict(queries=[dict(observed_state='excluded')]))]:
            with fixture() as f:
                with f.store.transaction() as c:f.app._event(c,kind,dict(target='11:101',**facts))
                with self.assertRaises(CleanerError) as error:install(f)
                self.assertEqual(error.exception.code,'initial_empty_history_forbidden')

    def test_install_cas_integrity_duplicate_and_atomic_readback(self):
        with fixture() as f:
            f.fake.targets[11].update(minus=[],stats=[])
            def crash(stage):raise Crash()
            with self.assertRaises(Crash):install(f,crash)
            state=journal(f.guard.directory);record=next(iter(state['installations'].values()))
            options=dict(operation_id=record['operation_id'],request=dict(mode='initial_empty',evidence_id=record['evidence_id'],evidence_sha256=record['evidence_sha256']),cleaner=f.app,directory=f.guard.directory,generation='g1',runtime_sha=RUNTIME)
            self.assertEqual(execute(action='readback',**options)['state'],'applied')
            self.assertEqual(execute(action='apply',**options)['candidate_sha256'],record['candidate_sha256'])
            path=f.guard.directory/'initial-empty.synthetic-first-fill.json';path.write_text(path.read_text()+' ')
            with self.assertRaises(CleanerError):journal(f.guard.directory)


if __name__=='__main__':unittest.main()
