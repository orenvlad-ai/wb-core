#!/usr/bin/env python3
"""Mixed immutable card evidence must not hide approved CPM SKUs or lift holds."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps import search_cluster_cleaner_stage_e as stage_e
from apps.search_cluster_cleaner_stage_e_recovery_smoke import Sandbox
from packages.application.search_cluster_cleaner_batch_eligibility import eligibility_rows
from packages.contracts.search_cluster_cleaner import CleanerError, Target


def reject(code, fn):
    try: fn()
    except CleanerError as exc: assert exc.code == code, (exc.code, code)
    else: raise AssertionError('expected '+code)


def private_json(path, value):
    raw=json.dumps(value,sort_keys=True).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return 'sha256:'+hashlib.sha256(raw).hexdigest()


def main():
    with Sandbox() as box:
        evidence=[];source=[];profiles=[];admission=[];catalog=[]
        for index in range(33):
            nm=101+index
            digest='sha256:'+format(index+1,'064x')
            verified=index<23
            state='verified' if verified else ('held_missing_kind' if index<31 else 'held_profile_mismatch')
            timestamp='2026-09-23T00:00:00Z' if verified else None
            evidence.append(dict(nm_id=nm,state=state,current_card_sha256=digest,verified_at=timestamp))
            source.append(dict(nm_id=str(nm),title='Approved glass '+str(nm),vendor_code='approved-'+str(nm),
                               description='Approved card',characteristics=[dict(id=1,name='kind',value='glass')],card_digest=digest))
            profiles.append(dict(box.package['profiles'][0],nm_id=nm))
            if verified:
                admission.append(dict(advert_id=10000+index,nm_id=nm,card_digest=digest,
                                      verified_at=timestamp,state='verified'))
            catalog.append(Target(10000+index,nm,status=9 if index<25 else 11,
                                  name='CPM '+str(index),contract_verified=True))
        # 24 historical pair admissions cover only the 23 verified SKUs.
        admission.append(dict(admission[0],advert_id=20000))
        catalog.append(Target(20000,101,status=9,name='Other CPM',contract_verified=True))
        box.package['profiles']=profiles
        box.package['manual_admission']=admission
        box.package['card_evidence_sha256']=private_json(box.admission/'current-card-evidence.json',dict(cards=evidence))
        box.package['provenance']['fresh_cards_sha256']=private_json(box.admission/'card-source-approved.json',dict(cards=source))
        box.write_package()
        preview=box.execute('preview')
        box.execute('apply',expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        package=stage_e._package(box.package_path,box.service().account,'monolith')
        assert len(stage_e._approved_card_receipts(package,box.admission))==23
        held=Target(10023,124,contract_verified=True)
        receipt=stage_e._admitted_targets(package,[held],box.admission)[0]
        assert receipt['state']=='fresh_verification_required' and receipt['verified_at'] is None
        assert receipt['basis']=='package_bound_source_fresh_check_required'
        rows=eligibility_rows(box.service(),'monolith',catalog,config_path=box.admission/'stage-e-config.json')
        assert len(rows)==34 and all(row['eligible'] and row['admitted'] for row in rows),rows
        assert sum(row['status']=='active' for row in rows)==26
        assert sum(row['status']=='paused' for row in rows)==8
        fresh={key:value for key,value in source[23].items() if key!='card_digest'}
        with patch.object(stage_e,'fetch_current_card',return_value=fresh):
            assert stage_e._verify_fresh_card(package,held,box.admission,box.service())['nm_id']==held.nm_id
        with patch.object(stage_e,'fetch_current_card',return_value=dict(fresh,title='Drift')):
            reject('current_card_drift',lambda:stage_e._verify_fresh_card(package,held,box.admission,box.service()))
        with box.service().store.transaction() as c:
            c.execute('INSERT INTO cleaner_target_holds VALUES(?,?,?,?)',
                      (box.service().key,held.key,'external_state_drift','2026-09-23T00:00:00Z'))
        rows=eligibility_rows(box.service(),'monolith',catalog,config_path=box.admission/'stage-e-config.json')
        assert next(row for row in rows if (row['advert_id'],row['nm_id'])==(held.advert_id,held.nm_id))['reason']=='target_held'

        # A newly SHA-bound source still cannot disagree with historical held evidence.
        source[23]['card_digest']='sha256:'+'f'*64
        box.package['provenance']['fresh_cards_sha256']=private_json(box.admission/'card-source-approved.json',dict(cards=source))
        box.write_package()
        package=stage_e._package(box.package_path,box.service().account,'monolith')
        reject('approved_card_source_mismatch',lambda:stage_e._admitted_targets(package,[held],box.admission))
        reject('manual_admission_unavailable',lambda:eligibility_rows(box.service(),'monolith',catalog,
                                            config_path=box.admission/'stage-e-config.json'))

        # Integrity validation includes every historical hold, even if it is not selected.
        source[23]['card_digest']=evidence[23]['current_card_sha256']
        box.package['provenance']['fresh_cards_sha256']=private_json(box.admission/'card-source-approved.json',dict(cards=source))
        for broken in (dict(evidence[23],state='unknown'),dict(evidence[23],verified_at='2026-09-23T00:00:00Z'),
                       dict(evidence[23],current_card_sha256='bad'),dict(evidence[22])):
            original=evidence[23]
            evidence[23]=broken
            box.package['card_evidence_sha256']=private_json(box.admission/'current-card-evidence.json',dict(cards=evidence))
            reject('current_card_evidence_mismatch',lambda:stage_e._approved_card_receipts(box.package,box.admission))
            evidence[23]=original
    print('search cluster cleaner held evidence smoke: ok')


if __name__=='__main__': main()
