#!/usr/bin/env python3
"""Focused offline proofs of additive admission and scoped crash recovery."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import search_cluster_cleaner_stage_e as stage_e
from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox, RUNTIME_SHA, GENERATION, semantic_fixture_card, expect_error
from packages.application import search_cluster_cleaner_onboarding as onboarding
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.application.search_cluster_cleaner_self_service import LocalStageEAdapter
from packages.domain import search_cluster_classifier as rules
from apps.production_apply_launcher import execute as launcher
from packages.contracts.search_cluster_cleaner import CleanerError, Profile, Target, Principal, canonical, digest

OP='sku-onboarding-original-0001'
EXT='synthetic-noframe-0001'


def write_private(path,value):
    raw=canonical(value).encode()
    path.write_bytes(raw);path.chmod(0o600)
    return onboarding.sha(raw)


def setup(box):
    base=semantic_fixture_card()
    base['card_digest']='sha256:'+'1'*64
    box.package['provenance']['fresh_cards_sha256']=write_private(box.admission/'card-source-approved.json',dict(cards=[base]))
    box.write_package()
    preview=box.execute('preview')
    box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
    service=box.service()
    profiles=[];cards=[]
    for nm,kind,vendor,title in ((202,'clean','Clean','Прозрачное'),(203,'matte','Matte','Матовое')):
        card=semantic_fixture_card(nm)
        card.update(subject_id=1571,title=f'{title} защитное стекло iPhone 16 Pro Max No Frame',vendor_code=f'({vendor}) iPhone 16 Pro Max No Frame')
        card['characteristics'][-1]['value']=['бесцветный']
        card['card_digest']='sha256:'+digest(card)
        cards.append(card)
        profiles.append(Profile.parse(dict(nm_id=nm,version=1,category='phone_screen_glass',models=['16 promax'],kind=kind,frame='none',source='synthetic reviewed official cards',verified_at='2026-10-07T00:00:00Z')).as_dict())
    parent=box.admission/'extensions'/EXT
    parent.mkdir(parents=True,mode=0o700);parent.parent.chmod(0o700)
    source_sha=write_private(parent/'cards.json',dict(cards=cards))
    extension=dict(schema=onboarding.SCHEMA,extension_id=EXT,seller_id='seller',account_scope='scope',generation=GENERATION,owner_username='owner',
                   base_package_sha256=onboarding.sha(box.package_path.read_bytes()),runtime_sha=RUNTIME_SHA,rules_version=service.rules_version,rules_digest=service.rules_digest,
                   projection_version=onboarding.PROJECTION_VERSION,profiles=profiles,card_source_sha256=source_sha,approved_at='2026-10-07T00:00:00Z',provenance={'synthetic':True})
    extension_sha=write_private(parent/'extension.json',extension)
    request=dict(mode='admit_profiles',extension_id=EXT,extension_sha256=extension_sha)
    fresh={int(card['nm_id']):{k:v for k,v in card.items() if k!='card_digest'} for card in cards}
    return request,fresh,extension


def old_state(box):
    private={name:hashlib.sha256((box.admission/name).read_bytes()).hexdigest() for name in ('approved-baseline-v1.json','card-source-approved.json','current-card-evidence.json','stage-e-config.json','admission.json','bootstrap-recovery.json')}
    with box.service().store.read() as c:
        rows={table:[tuple(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in
              ('cleaner_settings','cleaner_baselines','cleaner_observations','cleaner_manual_overrides','cleaner_daily_schedules','cleaner_write_operations')}
        rows['old_profiles']=[tuple(row) for row in c.execute('SELECT * FROM cleaner_profiles WHERE nm_id=101')]
        rows['old_heads']=[tuple(row) for row in c.execute('SELECT * FROM cleaner_profile_heads WHERE nm_id=101')]
    return private,rows


def preview_apply(box,request,operation=OP):
    preview=box.execute('preview',operation_id=operation,request=request)
    return box.execute('apply',operation_id=operation,request=request,expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])


def catalog():
    return [Target(10,101,contract_verified=True),Target(20,202,contract_verified=True),Target(21,202,payment_type='cpc',contract_verified=True),
            Target(22,202,bid_type='unified',contract_verified=True),Target(23,203,status=7,contract_verified=True),Target(24,203,contract_verified=True)]


def test_success_and_launcher_repeat():
    with Sandbox() as box:
        request,fresh,extension=setup(box)
        before=old_state(box)
        with patch.object(stage_e,'fetch_current_card',side_effect=lambda nm:copy.deepcopy(fresh[nm])) as fetch:
            p=box.execute('preview',operation_id=OP,request=request)
            assert not (box.admission/onboarding.JOURNAL_NAME).exists()
            assert box.service().get_profile(202,Principal('owner',True,True,True))['revision']==0
            adapter=LocalStageEAdapter(runtime_dir=box.runtime,env_file=box.env,admission_dir=box.admission)
            result=launcher(action='apply',adapter_name='search_cluster_cleaner_manual_v1',operation_id=OP,request=request,expected_prestate=p['prestate_sha256'],expected_candidate=p['candidate_sha256'],adapters={'search_cluster_cleaner_manual_v1':adapter})
            assert result['state']=='applied'
            calls=fetch.call_count
            repeat=launcher(action='apply',adapter_name='search_cluster_cleaner_manual_v1',operation_id=OP,request=request,expected_prestate=p['prestate_sha256'],expected_candidate=p['candidate_sha256'],adapters={'search_cluster_cleaner_manual_v1':adapter})
            assert repeat['state']=='applied' and fetch.call_count==calls
            expect_error('sku_admission_operation_consumed',lambda:box.execute('apply',operation_id=OP,request=request))
            rows=eligibility_rows(box.service(),GENERATION,catalog(),config_path=box.admission/'stage-e-config.json')
            assert [r['advert_id'] for r in rows]==[10,20,22,23,24]
            assert {r['advert_id']:r['reason'] for r in rows}=={10:None,20:None,22:'contract_not_verified',23:'unsupported_campaign_status',24:None}
            context=onboarding.load_context(box.package,box.admission,package_path=box.package_path,cleaner=box.service(),generation=GENERATION)
            receipt=stage_e._admitted_targets(box.package,[Target(20,202)],box.admission,context=context)[0]
            assert receipt['extension_sha256']==request['extension_sha256'] and receipt['state']=='fresh_verification_required'
            verified=stage_e._verify_fresh_card(box.package,Target(20,202),box.admission,box.service(),context=context)
            assert verified['approved_source_sha256']==extension['card_source_sha256']
            altered=copy.deepcopy(fresh[202]);altered['characteristics'][0]['value']=['Apple','iPhone 15 Pro Max']
            with patch.object(stage_e,'fetch_current_card',return_value=altered):
                try:stage_e._verify_fresh_card(box.package,Target(20,202),box.admission,box.service(),context=context)
                except CleanerError as exc:assert exc.code in {'current_card_semantics_unavailable','current_card_drift'}
                else:raise AssertionError('fresh drift admitted')
        assert old_state(box)==before
        print('additive admission: preview read-only, canonical profiles, old state unchanged, CPM guards and one-submit launcher: ok')


def test_partial_and_exact_recovery():
    for crash in ('claimed','created','activated','before_publish','published'):
        with Sandbox() as box:
            request,fresh,_=setup(box);before=old_state(box)
            def fault(phase,nm):
                if phase==crash:raise RuntimeError('synthetic process interruption')
            real=onboarding.execute
            def execute_with_fault(**kwargs):return real(**kwargs,hook=fault)
            with patch.object(stage_e,'fetch_current_card',side_effect=lambda nm:copy.deepcopy(fresh[nm])):
                p=box.execute('preview',operation_id=OP,request=request)
                with patch.object(onboarding,'execute',side_effect=execute_with_fault):
                    try:box.execute('apply',operation_id=OP,request=request,expected_prestate=p['prestate_sha256'],expected_candidate=p['candidate_sha256'])
                    except RuntimeError:pass
                    else:raise AssertionError('crash not injected')
                with patch.object(stage_e,'fetch_current_card',side_effect=AssertionError('readback must not fetch Content')):
                    rb=box.execute('readback',operation_id=OP,request=request)
                    expect_error('sku_admission_operation_consumed',lambda:box.execute('apply',operation_id=OP,request=request))
                if crash!='published':
                    assert rb['state']=='ambiguous'
                    rows=eligibility_rows(box.service(),GENERATION,catalog(),config_path=box.admission/'stage-e-config.json')
                    assert not next(r for r in rows if r['advert_id']==20)['eligible']
                    recover=dict(request,mode='admit_profiles_recover',original_operation_id=OP)
                    preview_apply(box,recover,'sku-onboarding-recover-0001')
                else:assert rb['state']=='applied'
                assert box.execute('readback',operation_id=OP,request=request)['state']=='applied'
            assert old_state(box)==before
    print('claimed/create/activation/publication crashes, closed partial admission, readback only, exact local recovery: ok')


def test_refusals_and_cas():
    with Sandbox() as box:
        request,fresh,extension=setup(box)
        with patch.object(stage_e,'fetch_current_card',side_effect=lambda nm:copy.deepcopy(fresh[nm])):
            p=box.execute('preview',operation_id=OP,request=request)
            expect_error('sku_admission_preview_drift',lambda:box.execute('apply',operation_id=OP,request=request,expected_prestate='sha256:'+'0'*64,expected_candidate=p['candidate_sha256']))
            assert not (box.admission/onboarding.JOURNAL_NAME).exists()
            duplicate=copy.deepcopy(extension);duplicate['profiles'].append(duplicate['profiles'][0])
            path=box.admission/'extensions'/EXT/'extension.json'
            duplicate_sha=write_private(path,duplicate)
            expect_error('sku_admission_profile_invalid',lambda:box.execute('preview',operation_id=OP,request=dict(request,extension_sha256=duplicate_sha)))
            drift=copy.deepcopy(extension);drift['profiles'][0]['nm_id']=101
            drift_sha=write_private(path,drift)
            expect_error('sku_admission_overlap',lambda:box.execute('preview',operation_id=OP,request=dict(request,extension_sha256=drift_sha)))
            wrong=copy.deepcopy(extension);wrong['rules_digest']='0'*64
            wrong_sha=write_private(path,wrong)
            expect_error('sku_admission_identity_mismatch',lambda:box.execute('preview',operation_id=OP,request=dict(request,extension_sha256=wrong_sha)))
            write_private(path,extension)
            bad=copy.deepcopy(fresh);bad[202]['characteristics'][-1]['value']=['черный']
            with patch.object(stage_e,'fetch_current_card',side_effect=lambda nm:bad[nm]):
                try:box.execute('preview',operation_id=OP,request=request)
                except CleanerError as exc:assert exc.code=='current_card_semantics_unavailable'
                else:raise AssertionError('fresh mismatch admitted')
            assert not (box.admission/onboarding.JOURNAL_NAME).exists()
    print('duplicate/overlap/rules identity/fresh mismatch/CAS fail before claim: ok')


def test_foreign_profile_blocks_recovery():
    with Sandbox() as box:
        request,fresh,extension=setup(box)
        before=old_state(box)
        real=onboarding.execute
        def fault(phase,nm):
            if phase=='created':raise RuntimeError('synthetic interruption after draft')
        with patch.object(stage_e,'fetch_current_card',side_effect=lambda nm:copy.deepcopy(fresh[nm])):
            p=box.execute('preview',operation_id=OP,request=request)
            with patch.object(onboarding,'execute',side_effect=lambda **kwargs:real(**kwargs,hook=fault)):
                try:box.execute('apply',operation_id=OP,request=request,expected_prestate=p['prestate_sha256'],expected_candidate=p['candidate_sha256'])
                except RuntimeError:pass
                else:raise AssertionError('crash not injected')
            box.service().create_profile(202,dict(request_id='foreign-profile-edit-0001',expected_revision=1,profile=extension['profiles'][0]),Principal('owner',True,True,True))
            recover=dict(request,mode='admit_profiles_recover',original_operation_id=OP)
            expect_error('sku_admission_profile_conflict',lambda:box.execute('preview',operation_id='sku-recover-conflict-0001',request=recover))
            assert box.execute('readback',operation_id=OP,request=request)['state']=='ambiguous'
            context=onboarding.load_context(box.package,box.admission,package_path=box.package_path,cleaner=box.service(),generation=GENERATION)
            assert 202 not in context.profiles
        assert old_state(box)==before
    print('foreign draft/profile revision blocks scoped recovery and remains unadmitted: ok')


def test_published_admission_survives_rule_update():
    with Sandbox() as box:
        request,fresh,_=setup(box)
        with patch.object(stage_e,'fetch_current_card',side_effect=lambda nm:copy.deepcopy(fresh[nm])):
            preview_apply(box,request)
        old_digest=box.service().rules_digest
        with patch.object(rules,'VOCAB',rules.VOCAB+'|syntheticnewword'):
            service=box.service()
            assert service.rules_digest!=old_digest
            rows=eligibility_rows(service,GENERATION,catalog(),config_path=box.admission/'stage-e-config.json')
            assert next(r for r in rows if r['advert_id']==10)['eligible']
            assert next(r for r in rows if r['advert_id']==20)['eligible']
            with patch.object(stage_e,'fetch_current_card',side_effect=AssertionError('historical readback fetch')):
                assert box.execute('readback',operation_id=OP,request=request)['state']=='applied'
            context=onboarding.load_context(box.package,box.admission,package_path=box.package_path,cleaner=service,generation=GENERATION)
            with patch.object(stage_e,'fetch_current_card',side_effect=lambda nm:copy.deepcopy(fresh[nm])):
                stage_e._verify_fresh_card(box.package,Target(20,202),box.admission,service,context=context)
    with Sandbox() as box:
        request,fresh,_=setup(box)
        with patch.object(rules,'VOCAB',rules.VOCAB+'|syntheticnewword'):
            expect_error('sku_admission_identity_mismatch',lambda:box.execute('preview',operation_id=OP,request=request))
        assert not (box.admission/onboarding.JOURNAL_NAME).exists()
    print('published admission/history survive later rules; new registration still binds current executable rules: ok')


def main():
    test_success_and_launcher_repeat()
    test_partial_and_exact_recovery()
    test_refusals_and_cas()
    test_foreign_profile_blocks_recovery()
    test_published_admission_survives_rule_update()
    print('search_cluster_cleaner_onboarding_smoke: ok')


if __name__=='__main__':main()
