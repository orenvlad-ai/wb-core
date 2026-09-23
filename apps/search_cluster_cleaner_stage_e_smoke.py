#!/usr/bin/env python3
"""Synthetic Stage E manual-only regression: exact scan, owner prepare, write."""
from __future__ import annotations
import sys, json, os, hashlib, tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from apps.search_cluster_cleaner_write_fixture import fixture, OWNER, FakeWB
from packages.application.search_cluster_cleaner_worker import ManualCleanerWorker, product_tick
from packages.contracts.search_cluster_cleaner import Account, CleanerError, Principal, Target
from packages.application.storage_registry import _implicit_manifest, manifest_payload
from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.search_cluster_cleaner_admission import AdmissionGuard
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource, AccountLimiter
from packages.adapters.official_api_runtime import OfficialApiRuntimeConfig
from apps.search_cluster_cleaner_write_fixture import PROFILE
from apps import search_cluster_cleaner_stage_e as stage_e
from apps.search_cluster_cleaner_stage_e_recovery_smoke import main as recovery_main
from packages.contracts.search_cluster_cleaner import digest

TARGET=Target(11,101,contract_verified=True)


def manual_state(f):
    f.guard.hold(reason='stage-e-synthetic')
    with f.store.transaction() as c:
        c.execute('UPDATE cleaner_settings SET enabled=0,restore_hold=1,transport_enabled=0 WHERE account=?',(f.app.key,))


def exact_scan(f, worker, request_id='stage-e-scan-0001'):
    run_id=f.app.start_run(dict(request_id=request_id,advert_id=11,nm_id=101),OWNER)['run_id']
    preview=worker.preview(run_id=run_id,targets=[TARGET])
    result=worker.execute(run_id=run_id,targets=[TARGET],expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],production_operation_id='stage-e-scan-op-0001')
    assert result['state']=='complete'
    return run_id


def main():
    recovery_main()
    # Actual entrypoint bootstrap: the env contains only the pre-bootstrap
    # seller/scope/package facts and performs no WB operation.
    with tempfile.TemporaryDirectory(prefix='stage-e-entry-') as raw:
        root=Path(raw);app_dir=root/'app';app_dir.mkdir();sha='a'*40
        (app_dir/'.wb-core-runtime-sha').write_text(sha);(app_dir/'.wb-core-deploy.json').write_text(json.dumps(dict(commit=sha,deployment_complete=True)))
        runtime=root/'state';runtime.mkdir();(runtime/'registry_upload_runtime.sqlite3').touch();(runtime/'storage_generation_manifest.json').write_text(json.dumps(manifest_payload(_implicit_manifest())))
        admission=root/'admission';admission.mkdir(mode=0o700)
        rows=[dict(advert_id=1,nm_id=101,query='baseline',decision='allow',observed_state='active',provenance={'synthetic':True})]
        evidence=dict(cards=[dict(nm_id=101,state='verified',current_card_sha256='sha256:'+'1'*64,verified_at='2026-09-23T00:00:00Z')]);evidence_raw=json.dumps(evidence).encode();(admission/'current-card-evidence.json').write_bytes(evidence_raw);os.chmod(admission/'current-card-evidence.json',0o600)
        package=dict(schema='search_cluster_cleaner_approved_baseline/v1',seller_id='seller',account_scope='scope',generation='monolith',owner_username='owner',rows=rows,profiles=[PROFILE],provenance={'synthetic':True},source_sha256='sha256:'+'0'*64,rows_digest='sha256:'+digest(rows),card_evidence_sha256='sha256:'+hashlib.sha256(evidence_raw).hexdigest(),manual_admission=[dict(advert_id=11,nm_id=101,card_digest='sha256:'+'1'*64,verified_at='2026-09-23T00:00:00Z',state='verified')])
        package_path=admission/'approved-baseline-v1.json';package_path.write_text(json.dumps(package));os.chmod(package_path,0o600)
        env=root/'env';env.write_text('SELLER_PORTAL_CANONICAL_SUPPLIER_ID=seller\nCHANGE_REGISTRY_ACCOUNT_SCOPE=scope\nCLEANER_BOOTSTRAP_PACKAGE_PATH='+str(package_path)+'\n')
        old_root=stage_e.ROOT;stage_e.ROOT=app_dir
        try:
            envelope=dict(operation_id='stage-e-bootstrap-0001',request=dict(mode='bootstrap'),expected_runtime_sha=sha)
            preview=stage_e.execute(dict(envelope,action='preview'),runtime_dir=runtime,env_file=env,admission_dir=admission)
            assert stage_e.execute(dict(envelope,action='apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256']),runtime_dir=runtime,env_file=env,admission_dir=admission)['disposition']=='submitted'
            assert stage_e.execute(dict(envelope,action='readback'),runtime_dir=runtime,env_file=env,admission_dir=admission)['state']=='applied'
            # The remaining three production-apply modes use the same real
            # entrypoint, including config fallback after bootstrap.
            store=CleanerStore(stage_e.StoreRegistry(runtime));service=KeywordCleaner(store,Account('seller','scope'),owner_username='owner')
            ChangeRegistryRepository(runtime).initialize_schema()
            fake=FakeWB()
            with fake.server() as url:
                source=CleanerWbSource(account=service.account,runtime=OfficialApiRuntimeConfig('synthetic-not-a-token',url,2),limiter=AccountLimiter(),fixture=True)
                original=stage_e.CleanerWbSource.from_env;stage_e.CleanerWbSource.from_env=lambda account: source
                try:
                    run=service.start_run(dict(request_id='stage-e-entry-scan-0001',advert_id=11,nm_id=101),Principal('owner',True,True,True))['run_id']
                    manual=dict(mode='manual',run_id=run,targets=[dict(advert_id=11,nm_id=101)])
                    scan=stage_e.execute(dict(action='preview',operation_id='stage-e-entry-scan-0001',request=manual,expected_runtime_sha=sha),runtime_dir=runtime,env_file=env,admission_dir=admission)
                    stage_e.execute(dict(action='apply',operation_id='stage-e-entry-scan-0001',request=manual,expected_runtime_sha=sha,expected_prestate=scan['prestate_sha256'],expected_candidate=scan['candidate_sha256']),runtime_dir=runtime,env_file=env,admission_dir=admission)
                    assert stage_e.execute(dict(action='readback',operation_id='stage-e-entry-scan-0001',request=manual,expected_runtime_sha=sha),runtime_dir=runtime,env_file=env,admission_dir=admission)['state']=='no_change'
                    prepare=dict(mode='manual_prepare',scan_run_id=run,targets=[dict(advert_id=11,nm_id=101)])
                    prepared_preview=stage_e.execute(dict(action='preview',operation_id='stage-e-entry-prep-0001',request=prepare,expected_runtime_sha=sha),runtime_dir=runtime,env_file=env,admission_dir=admission)
                    prepared=stage_e.execute(dict(action='apply',operation_id='stage-e-entry-prep-0001',request=prepare,expected_runtime_sha=sha,expected_prestate=prepared_preview['prestate_sha256'],expected_candidate=prepared_preview['candidate_sha256']),runtime_dir=runtime,env_file=env,admission_dir=admission)
                    assert stage_e.execute(dict(action='readback',operation_id='stage-e-entry-prep-0001',request=prepare,expected_runtime_sha=sha),runtime_dir=runtime,env_file=env,admission_dir=admission)['state']=='applied'
                    write=dict(mode='manual',run_id=prepared['run_id'],targets=[dict(advert_id=11,nm_id=101)])
                    write_preview=stage_e.execute(dict(action='preview',operation_id='stage-e-entry-write-0001',request=write,expected_runtime_sha=sha),runtime_dir=runtime,env_file=env,admission_dir=admission)
                    stage_e.execute(dict(action='apply',operation_id='stage-e-entry-write-0001',request=write,expected_runtime_sha=sha,expected_prestate=write_preview['prestate_sha256'],expected_candidate=write_preview['candidate_sha256']),runtime_dir=runtime,env_file=env,admission_dir=admission)
                    assert stage_e.execute(dict(action='readback',operation_id='stage-e-entry-write-0001',request=write,expected_runtime_sha=sha),runtime_dir=runtime,env_file=env,admission_dir=admission)['state']=='applied'
                    assert len(fake.writes)==1
                finally:stage_e.CleanerWbSource.from_env=original
        finally:stage_e.ROOT=old_root
    with fixture() as f:
        manual_state(f)
        # A disabled generic composition cannot consume the explicit UI job or
        # contact WB. The supported path is ManualCleanerWorker only.
        worker=ManualCleanerWorker(f.app,f.source,f.guard,generation='g1')
        queued=f.app.start_run(dict(request_id='stage-e-queued-0001',advert_id=11,nm_id=101),OWNER)['run_id']
        before_calls=len(f.fake.calls);before_writes=len(f.fake.writes)
        assert product_tick(f.app,f.source,f.guard,generation='g1')['state']=='held'
        assert len(f.fake.calls)==before_calls and len(f.fake.writes)==before_writes
        with f.store.read() as c:assert c.execute("SELECT state FROM cleaner_runs WHERE run_id=?",(queued,)).fetchone()['state']=='queued'
        scan_run=queued
        preview=worker.preview(run_id=scan_run,targets=[TARGET])
        assert worker.execute(run_id=scan_run,targets=[TARGET],expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],production_operation_id='stage-e-scan-op-0001')['state']=='complete'
        discovered=f.app.manual_apply_preview(scan_run,TARGET)
        assert len(discovered['candidates'])==2
        prepared=f.app.prepare_manual_apply(scan_run,TARGET,discovered['candidate_sha256'],'stage-e-prepare-0001',OWNER)
        write_preview=worker.preview(run_id=prepared['run_id'],targets=[TARGET])
        outcome=worker.execute(run_id=prepared['run_id'],targets=[TARGET],expected_prestate=write_preview['prestate_sha256'],expected_candidate=write_preview['candidate_sha256'],production_operation_id='stage-e-write-op-0001',reviewed_candidate=write_preview['candidate_sha256'])
        assert outcome['state']=='complete' and len(f.fake.writes)==1
        summary=f.app.summary(OWNER)
        assert not summary['settings']['enabled'] and not summary['transport_enabled'] and summary['settings']['restore_hold']
        with f.guard._lock():assert f.guard._load()['hold'] and not f.guard._load().get('manual_capability')
        # The exact prepared run is consumed: a replay does not create another
        # POST and must use the production readback path instead.
        try:worker.execute(run_id=prepared['run_id'],targets=[TARGET],expected_prestate=write_preview['prestate_sha256'],expected_candidate=write_preview['candidate_sha256'],production_operation_id='stage-e-write-op-0001')
        except CleanerError as exc:assert exc.code=='manual_run_not_ready'
        else:raise AssertionError('same manual scope could submit again')
        assert len(f.fake.writes)==1
    with fixture() as f:
        manual_state(f);worker=ManualCleanerWorker(f.app,f.source,f.guard,generation='g1')
        run=f.app.start_run(dict(request_id='stage-e-drift-scan-0001',advert_id=11,nm_id=101),OWNER)['run_id']
        preview=worker.preview(run_id=run,targets=[TARGET]);f.fake.targets[11]['minus'].append('drift after preview')
        try:worker.execute(run_id=run,targets=[TARGET],expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],production_operation_id='stage-e-drift-op-0001')
        except CleanerError as exc:assert exc.code=='manual_preview_drift'
        else:raise AssertionError('changed preview was accepted')
        assert not f.fake.writes
    print('search cluster cleaner Stage E smoke: ok')


if __name__=='__main__':main()
