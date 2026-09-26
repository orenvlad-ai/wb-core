#!/usr/bin/env python3
"""Governed manual-only cleaner bootstrap and exact-run entrypoint.

The process has no timer/service mode.  It is invoked only through the
one-operation production-apply envelope and keeps automatic cleaning disabled.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping
import tempfile
import stat

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from apps.wb_fbs_warehouse_registry import _load_env_file
from packages.adapters.wb_content import HttpBackedWbContentSource, _extract_cards, _extract_cursor
from packages.adapters.official_api_runtime import load_runtime_config
from packages.adapters.search_cluster_cleaner_wb import CleanerWbSource
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner_admission import AdmissionGuard
from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.application.search_cluster_cleaner_worker import ManualCleanerWorker
from packages.application.storage_registry import StoreRegistry
from packages.contracts.search_cluster_cleaner import Account, CleanerError, Principal, Target, Profile, canonical, digest, query_hash

CANONICAL_CLEANER_OBJECTS=frozenset({'cleaner_active_run','cleaner_apply_continuation','cleaner_auto_decisions','cleaner_auto_decisions_no_delete','cleaner_auto_decisions_no_update','cleaner_baselines','cleaner_baselines_no_delete','cleaner_baselines_no_update','cleaner_events','cleaner_events_no_delete','cleaner_events_no_update','cleaner_events_page','cleaner_manual_overrides','cleaner_manual_overrides_no_delete','cleaner_manual_overrides_no_update','cleaner_observation_review','cleaner_observations','cleaner_operation_dispatch_monotonic','cleaner_override_heads','cleaner_profile_heads','cleaner_profiles','cleaner_profiles_no_delete','cleaner_profiles_no_update','cleaner_readback_jobs','cleaner_requests','cleaner_requests_no_delete','cleaner_requests_no_update','cleaner_reviews','cleaner_reviews_page','cleaner_run_fifo','cleaner_run_targets','cleaner_runs','cleaner_scan_queue','cleaner_schedule_dates','cleaner_schedule_dates_no_delete','cleaner_schedule_dates_no_update','cleaner_schema','cleaner_settings','cleaner_target_holds','cleaner_unresolved_target','cleaner_write_items','cleaner_write_operations'})


def _fail(code:str): raise CleanerError(code,'Stage E manual-only precondition is not met',409)

def _config(runtime_dir:Path, *, bootstrap:bool, admission_dir:Path) -> tuple[StoreRegistry,Account,str,str,Path]:
    registry=StoreRegistry(runtime_dir.resolve());manifest=registry.load(require_files=True)
    generation=registry.generation('operational',manifest=manifest).generation_id
    package=Path(os.environ.get('CLEANER_BOOTSTRAP_PACKAGE_PATH' if bootstrap else 'CLEANER_APPROVED_PACKAGE_PATH','')).expanduser()
    if bootstrap:
        if not package.is_absolute() or not package.is_file():_fail('cleaner_stage_e_config_invalid')
        raw=_package_unbound(package,generation)
        canonical_seller=os.environ.get('SELLER_PORTAL_CANONICAL_SUPPLIER_ID','').strip()
        registry_scope=os.environ.get('CHANGE_REGISTRY_ACCOUNT_SCOPE','').strip()
        if not canonical_seller or not registry_scope or raw['seller_id']!=canonical_seller or raw['account_scope']!=registry_scope:_fail('bootstrap_account_identity_mismatch')
        return registry,Account(raw['seller_id'],raw['account_scope']),generation,str(raw['owner_username']),package.resolve()
    seller=os.environ.get('SELLER_PORTAL_CANONICAL_SUPPLIER_ID','').strip();scope=os.environ.get('CLEANER_ACCOUNT_SCOPE','').strip();configured_generation=os.environ.get('CLEANER_OPERATIONAL_GENERATION','').strip();owner=os.environ.get('CLEANER_OWNER_USERNAME','').strip()
    if not all((seller,scope,configured_generation,owner)):
        try:
            stored=json.loads((admission_dir.resolve()/'stage-e-config.json').read_text(encoding='utf-8'))
            seller,scope,configured_generation,owner=stored['seller_id'],stored['account_scope'],stored['generation'],stored['owner_username']
            package=Path(stored['approved_package_path']).expanduser()
        except (OSError,ValueError,KeyError,TypeError):pass
    if not seller or not scope or not owner or configured_generation != generation or not package.is_absolute() or not package.is_file():_fail('cleaner_stage_e_config_invalid')
    return registry,Account(seller,scope),generation,owner,package.resolve()

def _write_stage_config(directory:Path, *, account:Account, generation:str, owner:str, package:Path) -> None:
    directory.mkdir(parents=True,exist_ok=True,mode=0o700);path=directory/'stage-e-config.json'
    value=dict(seller_id=account.seller_id,account_scope=account.account_scope,generation=generation,owner_username=owner,approved_package_path=str(package))
    if path.exists():
        try:
            if json.loads(path.read_text(encoding='utf-8'))!=value:_fail('stage_e_config_conflict')
            return
        except ValueError:_fail('stage_e_config_invalid')
    temporary=directory/'.stage-e-config.tmp'
    temporary.write_text(json.dumps(value,sort_keys=True,separators=(',',':')),encoding='utf-8');os.chmod(temporary,0o600);os.replace(temporary,path)

def _package_unbound(path:Path, generation:str) -> dict:
    try:
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077:_fail('approved_package_permissions')
    except OSError:_fail('approved_package_unreadable')
    try:value=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):_fail('approved_package_unreadable')
    required={'schema','seller_id','account_scope','generation','owner_username','rows','profiles','provenance','source_sha256','rows_digest','card_evidence_sha256','manual_admission'}
    if not isinstance(value,dict) or set(value)!=required or value['schema']!='search_cluster_cleaner_approved_baseline/v1' or value['generation']!=generation or not isinstance(value['seller_id'],str) or not isinstance(value['account_scope'],str) or not isinstance(value['owner_username'],str) or not value['owner_username'].strip():_fail('approved_package_invalid')
    return value

def _package(path:Path, account:Account, generation:str) -> dict:
    value=_package_unbound(path,generation)
    if value['schema']!='search_cluster_cleaner_approved_baseline/v1' or value['seller_id']!=account.seller_id or value['account_scope']!=account.account_scope or value['generation']!=generation:_fail('approved_package_identity_mismatch')
    if (not isinstance(value['rows'],list) or not isinstance(value['profiles'],list) or not isinstance(value['provenance'],dict) or not value['provenance']
            or not isinstance(value['manual_admission'],list) or not value['manual_admission']
            or not re.fullmatch(r'sha256:[0-9a-f]{64}',str(value['source_sha256'])) or not re.fullmatch(r'sha256:[0-9a-f]{64}',str(value['card_evidence_sha256']))
            or value['rows_digest']!='sha256:'+digest(value['rows'])):_fail('approved_package_invalid')
    # Validate every semantic failure import_baseline could discover before it
    # creates any SQLite object. A malformed immutable package is never a
    # partial bootstrap state.
    try:
        profiles=[Profile.parse(row) for row in value['profiles']]
        if len({profile.nm_id for profile in profiles})!=len(profiles):_fail('approved_package_invalid')
        seen=set()
        for row in value['rows']:
            identity=(Target(row['advert_id'],row['nm_id']).key,query_hash(row['query']))
            if identity in seen or row.get('decision') not in {'allow','exclude'} or not row.get('provenance') or row.get('observed_state') not in {'active','excluded','archived','unknown'}:_fail('approved_package_invalid')
            seen.add(identity)
            if row['nm_id'] not in {profile.nm_id for profile in profiles}:_fail('approved_package_invalid')
    except (CleanerError,KeyError,TypeError,ValueError):_fail('approved_package_invalid')
    return value

def _targets(request:Mapping[str,Any]) -> list[Target]:
    rows=request.get('targets')
    if not isinstance(rows,list) or len(rows)!=1:_fail('manual_targets_invalid')
    targets=[]
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'advert_id','nm_id'} or type(row['advert_id']) is not int or type(row['nm_id']) is not int: _fail('manual_targets_invalid')
        targets.append(Target(row['advert_id'],row['nm_id'],contract_verified=True))
    if len({t.key for t in targets})!=len(targets):_fail('manual_targets_invalid')
    return targets

def _card_evidence_rows(package:dict, admission_dir:Path) -> dict[int,dict]:
    """Validate every immutable row, including historical holds (not receipts)."""
    admitted={}
    for row in package['manual_admission']:
        if (not isinstance(row,dict) or set(row)!={'advert_id','nm_id','card_digest','verified_at','state'}
                or type(row['advert_id']) is not int or row['advert_id']<=0 or type(row['nm_id']) is not int or row['nm_id']<=0
                or row.get('state')!='verified' or not isinstance(row.get('card_digest'),str)
                or not re.fullmatch(r'sha256:[0-9a-f]{64}',row['card_digest'])
                or not isinstance(row.get('verified_at'),str) or not row['verified_at']):_fail('manual_admission_invalid')
        key=f"{row['advert_id']}:{row['nm_id']}"
        if key in admitted:_fail('manual_admission_invalid')
        admitted[key]=dict(row)
    try:
        evidence_path=admission_dir/'current-card-evidence.json'
        if not stat.S_ISREG(evidence_path.stat().st_mode) or evidence_path.stat().st_mode & 0o077:_fail('current_card_evidence_permissions')
        raw=evidence_path.read_bytes()
        evidence=json.loads(raw)
    except (OSError,ValueError):_fail('current_card_evidence_unavailable')
    if 'sha256:'+hashlib.sha256(raw).hexdigest()!=package['card_evidence_sha256'] or not isinstance(evidence,dict) or not isinstance(evidence.get('cards'),list):_fail('current_card_evidence_mismatch')
    current={}
    for row in evidence['cards']:
        if (not isinstance(row,dict) or set(row)!={'nm_id','state','current_card_sha256','verified_at'}
                or type(row['nm_id']) is not int or row['nm_id']<=0 or row['nm_id'] in current
                or row['state'] not in {'verified','held_missing_kind','held_profile_mismatch'}
                or not re.fullmatch(r'sha256:[0-9a-f]{64}',str(row['current_card_sha256']))
                or (row['state']=='verified' and (not isinstance(row['verified_at'],str) or not row['verified_at']))
                or (row['state']!='verified' and row['verified_at'] is not None)):_fail('current_card_evidence_mismatch')
        current[row['nm_id']]=row
    for row in admitted.values():
        actual=current.get(row['nm_id'])
        if not actual or actual['state']!='verified' or actual['current_card_sha256']!=row['card_digest'] or actual['verified_at']!=row['verified_at']:_fail('manual_target_not_reconciled')
    return current


def _approved_card_receipts(package:dict, admission_dir:Path) -> dict[int,dict]:
    """Only previously verified SKU evidence is a historical receipt."""
    return {nm:row for nm,row in _card_evidence_rows(package,admission_dir).items() if row['state']=='verified'}


def _approved_card_source(package:dict, admission_dir:Path) -> dict[int,dict]:
    """Load only business cards whose exact source bytes are package-bound."""
    path=admission_dir/'card-source-approved.json'
    try:
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077:_fail('approved_card_source_permissions')
        raw=path.read_bytes()
        if 'sha256:'+hashlib.sha256(raw).hexdigest()!=package['provenance'].get('fresh_cards_sha256'):_fail('approved_card_source_mismatch')
        source=json.loads(raw)
        if not isinstance(source,dict) or not isinstance(source.get('cards'),list):_fail('approved_card_source_mismatch')
    except (OSError,ValueError,KeyError,TypeError):_fail('approved_card_source_unavailable')
    cards={}
    for row in source['cards']:
        try:
            nm=int(row['nm_id'])
            if (str(nm)!=str(row['nm_id']) or nm in cards or not re.fullmatch(r'sha256:[0-9a-f]{64}',str(row.get('card_digest')))
                    or not isinstance(row.get('characteristics'),list)):_fail('approved_card_source_mismatch')
        except (KeyError,TypeError,ValueError):_fail('approved_card_source_mismatch')
        cards[nm]=row
    return cards


def _admitted_targets(package:dict, targets:list[Target], admission_dir:Path) -> list[dict]:
    evidence=_card_evidence_rows(package,admission_dir)
    current={nm:row for nm,row in evidence.items() if row['state']=='verified'}
    source=_approved_card_source(package,admission_dir) if any(t.nm_id not in current for t in targets) else {}
    approved_profiles={row['nm_id'] for row in package['profiles']}
    receipts=[]
    for target in targets:
        actual=current.get(target.nm_id)
        approved=source.get(target.nm_id) if not actual else None
        if target.nm_id not in approved_profiles or (not actual and not approved):_fail('manual_target_not_reconciled')
        historical=evidence.get(target.nm_id)
        if approved and historical and approved['card_digest']!=historical['current_card_sha256']:_fail('approved_card_source_mismatch')
        receipts.append(dict(advert_id=target.advert_id,nm_id=target.nm_id,
                             card_digest=actual['current_card_sha256'] if actual else approved['card_digest'],
                             verified_at=actual['verified_at'] if actual else None,
                             state='verified' if actual else 'fresh_verification_required',
                             basis='current_card_evidence' if actual else 'package_bound_source_fresh_check_required'))
    return receipts

def fetch_current_card(nm_id:int) -> dict:
    """Read the exact current Content card; never accept a partial search page."""
    source=HttpBackedWbContentSource()
    runtime=load_runtime_config(token_env_var='WB_API_TOKEN',default_base_url='https://content-api.wildberries.ru',
                                base_url_env_var='WB_CONTENT_API_BASE_URL',default_timeout_seconds=20)
    if runtime.base_url!='https://content-api.wildberries.ru':_fail('current_card_origin_invalid')
    cursor={'limit':100};found=[]
    for _ in range(3):
        payload=source._request_json(method='POST',url=runtime.base_url+'/content/v2/get/cards/list',
                 token=runtime.token,timeout_seconds=runtime.timeout_seconds,
                 body={'settings':{'cursor':cursor,'filter':{'textSearch':str(nm_id),'withPhoto':-1}}})
        cards=_extract_cards(payload)
        found.extend(card for card in cards if str(card.get('nmID') or card.get('nmId') or card.get('nm_id') or '')==str(nm_id))
        next_cursor=_extract_cursor(payload)
        if int(next_cursor.get('total') or len(cards))<100:break
        updated_at=str(next_cursor.get('updatedAt') or '')
        next_nm=next_cursor.get('nmID') or next_cursor.get('nmId')
        if not updated_at or not next_nm:_fail('current_card_incomplete')
        cursor={'limit':100,'updatedAt':updated_at,'nmID':next_nm}
    else:_fail('current_card_incomplete')
    if len(found)!=1:_fail('current_card_missing_or_duplicate')
    card=found[0]
    if not isinstance(card.get('characteristics'),list):_fail('current_card_incomplete')
    return dict(nm_id=str(nm_id),title=card.get('title'),vendor_code=card.get('vendorCode'),
                description=card.get('description'),characteristics=card['characteristics'])

def _verify_fresh_card(package:dict,target:Target,admission_dir:Path,service:KeywordCleaner) -> dict:
    """Compare current business fields with the package-bound approved card."""
    try:
        source=_approved_card_source(package,admission_dir)
        approved=source.get(target.nm_id)
        admitted=_admitted_targets(package,[target],admission_dir)[0]
        if not approved or approved.get('card_digest')!=admitted['card_digest']:_fail('approved_card_source_mismatch')
        fresh=fetch_current_card(target.nm_id)
    except CleanerError:raise
    except Exception as exc:raise CleanerError('current_card_unavailable','Не удалось проверить актуальную карточку WB',409) from exc
    def business_fields(card:dict) -> dict:
        characteristics=card.get('characteristics')
        if not isinstance(characteristics,list):_fail('current_card_incomplete')
        ids=[]
        for row in characteristics:
            if not isinstance(row,dict) or set(row)!={'id','name','value'} or type(row['id']) is not int:_fail('current_card_incomplete')
            ids.append(row['id'])
        if len(ids)!=len(set(ids)):_fail('current_card_duplicate_characteristic')
        return dict(nm_id=str(card.get('nm_id')),title=card.get('title'),vendor_code=card.get('vendor_code'),
                    description=card.get('description'),characteristics=sorted(characteristics,key=lambda row:(row['id'],canonical(row))))
    if canonical(business_fields(approved))!=canonical(business_fields(fresh)):
        _fail('current_card_drift')
    approved_profile=next((Profile.parse(row) for row in package['profiles'] if row['nm_id']==target.nm_id),None)
    with service.store.read() as c:active_profile=service._profile(c,target.nm_id)
    if not approved_profile or not active_profile or active_profile.semantic_fingerprint!=approved_profile.semantic_fingerprint:
        _fail('manual_profile_mismatch')
    return dict(nm_id=target.nm_id,approved_source_sha256=package['provenance']['fresh_cards_sha256'],verified_at=service_clock())

def service_clock() -> str:
    from packages.contracts.search_cluster_cleaner import utcnow
    return utcnow()

def _schema_state(service:KeywordCleaner) -> dict:
    with service.store.read() as c:
        rows=[dict(row) for row in c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    cleaner=[row for row in rows if row['name'].startswith('cleaner_')]
    foreign=[row for row in rows if not row['name'].startswith('cleaner_')]
    return dict(cleaner=cleaner,foreign_schema_sha256='sha256:'+digest(foreign))

def _bootstrap_journal(path:Path, value:dict|None=None) -> dict:
    file=path/'bootstrap-recovery.json'
    if value is None:
        try:return json.loads(file.read_text(encoding='utf-8'))
        except (OSError,ValueError):return {}
    temporary=path/'.bootstrap-recovery.tmp';temporary.write_text(canonical(value),encoding='utf-8');os.chmod(temporary,0o600);os.replace(temporary,file)
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return value

@contextmanager
def _bootstrap_lock(directory:Path):
    """Serialize bootstrap/recovery mutations without opening writer rights."""
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (directory/'bootstrap.lock').open('a+b') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:_fail('bootstrap_recovery_busy')
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)

def _bootstrap_preview(service:KeywordCleaner, package:dict, operation_id:str) -> dict:
    try:
        with service.store.read() as c:
            row=c.execute('SELECT enabled,baseline_ready,restore_hold FROM cleaner_settings WHERE account=?',(service.key,)).fetchone()
    except Exception as exc:
        if 'no such table' not in str(exc):raise
        row=None
    schema=_schema_state(service)
    prestate=dict(account=service.key,settings=dict(row) if row else None,cleaner_namespace=schema['cleaner'],foreign_schema_sha256=schema['foreign_schema_sha256'])
    candidate=dict(source_sha256=package['source_sha256'],rows_digest=package['rows_digest'],profiles=digest(package['profiles']),provenance=digest(package['provenance']),manual_admission=digest(package['manual_admission']))
    return dict(operation_id=operation_id,target='cleaner-bootstrap',scope=dict(rows=len(package['rows']),profiles=len(package['profiles'])),prestate_sha256='sha256:'+digest(prestate),candidate_sha256='sha256:'+digest(candidate),recovery=dict(kind='immutable-approved-package',package_digest='sha256:'+digest(package)))

def _bootstrap_apply(service:KeywordCleaner, package:dict, generation:str, operation_id:str, expected_prestate:str, expected_candidate:str, admission_dir:Path, recovery_operation_id:str='') -> dict:
    preview=_bootstrap_preview(service,package,operation_id)
    schema=_schema_state(service);receipt=dict(account=service.key,generation=generation,operation_id=operation_id,package_digest='sha256:'+digest(package),foreign_schema_sha256=schema['foreign_schema_sha256'])
    journal=_bootstrap_journal(admission_dir)
    journal_identity=bool(journal and all(journal.get(k)==v for k,v in receipt.items()))
    if journal and not journal_identity:
        _fail('bootstrap_journal_conflict')
    if journal_identity and journal.get('foreign_schema_sha256')!=schema['foreign_schema_sha256']:
        _fail('bootstrap_recovery_mismatch')
    resuming=bool(journal and all(journal.get(k)==v for k,v in receipt.items()) and journal.get('phase') in {'initialized','imported','started'})
    if resuming: receipt=dict(journal)
    if preview['prestate_sha256']!=expected_prestate or preview['candidate_sha256']!=expected_candidate:_fail('bootstrap_preview_drift')
    current_names={row['name'] for row in schema['cleaner']}
    expected_names={row['name'] for row in journal.get('cleaner_objects',[])}
    if schema['cleaner'] and (not resuming or (expected_names and current_names!=expected_names) or (not expected_names and not current_names.issubset(CANONICAL_CLEANER_OBJECTS))):_fail('bootstrap_namespace_not_empty')
    foreign_before=schema['foreign_schema_sha256']
    if resuming and journal.get('foreign_schema_sha256')!=foreign_before:_fail('bootstrap_recovery_mismatch')
    if not resuming:
        receipt.update(phase='started',prestate_sha256=expected_prestate,cleaner_objects=[]);_bootstrap_journal(admission_dir,receipt)
    if recovery_operation_id:
        journal=_bootstrap_journal(admission_dir)
        claimed=journal.get('recovery_operation_id')
        if claimed not in {None,recovery_operation_id} and journal.get('phase')=='applied':_fail('bootstrap_recovery_claimed')
        # Claim is a separate durable step.  Preserve the complete prior
        # journal until a later phase actually advances it.
        journal['recovery_operation_id']=recovery_operation_id;_bootstrap_journal(admission_dir,journal)
        receipt['recovery_operation_id']=recovery_operation_id
    AdmissionGuard(admission_dir,service.store).initialize_held(account=service.account,generation=generation,evidence='stage_e_bootstrap:'+operation_id)
    service.initialize(generation=generation)
    receipt.update(phase='initialized',cleaner_objects=_schema_state(service)['cleaner']);_bootstrap_journal(admission_dir,receipt)
    service.import_baseline(package['rows'],package['profiles'],provenance=package['provenance'],expected_digest=digest(package['rows']),ready=True)
    receipt['phase']='imported';_bootstrap_journal(admission_dir,receipt)
    with service.store.transaction() as c:
        settings=service._settings(c)
        if settings['enabled'] or not settings['baseline_ready'] or not settings['restore_hold']:_fail('bootstrap_state_invalid')
        service._event(c,'stage_e_bootstrap',dict(operation_id=operation_id,package_digest='sha256:'+digest(package),manual_only=True,foreign_schema_sha256=foreign_before))
    if _schema_state(service)['foreign_schema_sha256']!=foreign_before:_fail('bootstrap_foreign_schema_changed')
    _write_stage_config(admission_dir,account=service.account,generation=generation,owner=service.owner_username,package=Path(os.environ['CLEANER_BOOTSTRAP_PACKAGE_PATH']).resolve())
    receipt['phase']='applied';_bootstrap_journal(admission_dir,receipt)
    return dict(operation_id=operation_id,disposition='submitted')

def execute(envelope:Mapping[str,Any], *, runtime_dir:Path, env_file:Path, admission_dir:Path) -> dict:
    if not isinstance(envelope,Mapping) or set(envelope)-{'action','operation_id','request','expected_prestate','expected_candidate','expected_runtime_sha','actor'}:_fail('envelope_invalid')
    action=str(envelope.get('action') or '');operation_id=str(envelope.get('operation_id') or '')
    request=envelope.get('request')
    if action not in {'preview','apply','readback'} or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{7,127}',operation_id) or not isinstance(request,Mapping):_fail('envelope_invalid')
    _load_env_file(env_file.resolve())
    expected_sha=str(envelope.get('expected_runtime_sha') or '')
    marker=(ROOT/'.wb-core-runtime-sha');deploy=ROOT/'.wb-core-deploy.json'
    try:deployment=json.loads(deploy.read_text(encoding='utf-8'))
    except (OSError,ValueError):deployment={}
    if re.fullmatch(r'[0-9a-f]{40}',expected_sha) is None or not marker.is_file() or marker.read_text(encoding='utf-8').strip()!=expected_sha or deployment.get('commit')!=expected_sha or deployment.get('deployment_complete') is not True:_fail('deployed_runtime_sha_mismatch')
    mode=request.get('mode')
    allowed={'bootstrap':{'mode'},'bootstrap_recover':{'mode','original_operation_id'},'manual':{'mode','run_id','targets'},'manual_prepare':{'mode','scan_run_id','targets'}}
    if mode not in allowed or set(request)!=allowed[mode]:_fail('request_invalid')
    registry,account,generation,owner,package_path=_config(runtime_dir,bootstrap=mode in {'bootstrap','bootstrap_recover'},admission_dir=admission_dir)
    service=KeywordCleaner(CleanerStore(registry),account,owner_username=owner)
    if mode in {'bootstrap','bootstrap_recover'}:
        package=_package(package_path,account,generation)
        _admitted_targets(package,[Target(row['advert_id'],row['nm_id'],contract_verified=True) for row in package['manual_admission'] if row.get('state')=='verified'],admission_dir.resolve())
        original=operation_id if mode=='bootstrap' else str(request['original_operation_id'])
        journal=_bootstrap_journal(admission_dir.resolve())
        if mode=='bootstrap_recover' and (not re.fullmatch(r'[a-z0-9][a-z0-9._-]{7,127}',original) or any(journal.get(k)!=v for k,v in dict(account=account.key,generation=generation,operation_id=original,package_digest='sha256:'+digest(package)).items())):_fail('bootstrap_recovery_mismatch')
        if action=='preview':
            result=_bootstrap_preview(service,package,original);result['operation_id']=operation_id
            if mode=='bootstrap_recover':result['recovery']=dict(kind='scoped_bootstrap_recovery',original_operation_id=original,journal_phase=journal.get('phase'))
            return result
        if action=='apply':
            with _bootstrap_lock(admission_dir.resolve()):
                result=_bootstrap_apply(service,package,generation,original,str(envelope.get('expected_prestate') or ''),str(envelope.get('expected_candidate') or ''),admission_dir.resolve(),recovery_operation_id=operation_id if mode=='bootstrap_recover' else '')
            return dict(result,operation_id=operation_id)
        try:
            with service.store.read() as c:
                events=[json.loads(row[0]) for row in c.execute("SELECT facts FROM cleaner_events WHERE account=? AND kind='stage_e_bootstrap'",(service.key,))]
                event=next((facts for facts in events if facts.get('operation_id')==original and facts.get('package_digest')=='sha256:'+digest(package)),None)
                settings=c.execute('SELECT enabled,baseline_ready,restore_hold,transport_enabled FROM cleaner_settings WHERE account=?',(service.key,)).fetchone()
                baseline=c.execute('SELECT count(*) FROM cleaner_baselines WHERE account=? AND import_digest=?',(service.key,digest(package['rows']))).fetchone()[0]
                profiles=[]
                for raw_profile in package['profiles']:
                    profile=Profile.parse(raw_profile)
                    row=c.execute('SELECT payload FROM cleaner_profiles WHERE account=? AND nm_id=? AND version=?',(service.key,profile.nm_id,profile.version)).fetchone()
                    head=c.execute('SELECT active_version FROM cleaner_profile_heads WHERE account=? AND nm_id=?',(service.key,profile.nm_id)).fetchone()
                    profiles.append(bool(row and canonical(json.loads(row[0]))==canonical(profile.as_dict()) and head and head[0]==profile.version))
        except Exception as exc:
            if 'no such table' in str(exc):return dict(operation_id=operation_id,state='not_submitted')
            raise
        expected_config=dict(seller_id=account.seller_id,account_scope=account.account_scope,generation=generation,owner_username=owner,approved_package_path=str(package_path))
        try:config=json.loads((admission_dir.resolve()/'stage-e-config.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):config={}
        try:
            guard=AdmissionGuard(admission_dir.resolve(),service.store)
            with guard._lock():state=guard._load()
            held=state['hold'] and state['account']==account.key and state['generation']==generation and not state['seals'] and not state.get('owner')
        except CleanerError:held=False
        ready=bool(event and settings and not settings['enabled'] and settings['baseline_ready'] and settings['restore_hold'] and not settings['transport_enabled'] and baseline==len(package['rows']) and all(profiles) and config==expected_config and held and event.get('foreign_schema_sha256')==_schema_state(service)['foreign_schema_sha256'])
        if mode=='bootstrap_recover' and _bootstrap_journal(admission_dir.resolve()).get('recovery_operation_id')!=operation_id:ready=False
        return dict(operation_id=operation_id,state='applied' if ready else 'not_submitted')
    if mode=='manual_prepare':
        if not isinstance(request.get('scan_run_id'),str):_fail('request_invalid')
        targets=_targets(request)
        target=targets[0]
        if action=='readback':
            # The immutable preparation event is authoritative after submit.
            # Candidate/profile/card drift must not hide its exact run ID.
            with service.store.read() as c:
                rows=c.execute("SELECT run_id,facts FROM cleaner_events WHERE account=? AND kind='manual_apply_prepared'",(service.key,)).fetchall()
            for row in rows:
                try:facts=json.loads(row['facts'])
                except (TypeError,ValueError):continue
                if facts.get('production_operation_id')==operation_id and facts.get('scan_run_id')==request['scan_run_id'] and facts.get('target')==target.key and re.fullmatch(r'sha256:[0-9a-f]{64}',str(facts.get('candidate_sha256') or '')):
                    with service.store.read() as c:
                        prepared=c.execute("SELECT kind,trigger FROM cleaner_runs WHERE account=? AND run_id=?",(service.key,row['run_id'])).fetchone()
                    if prepared and prepared['kind']=='manual_apply' and prepared['trigger']=='manual_exact_candidates':
                        return dict(operation_id=operation_id,state='applied',run_id=row['run_id'])
            return dict(operation_id=operation_id,state='not_submitted')
        package=_package(package_path,account,generation)
        # Preparation has no Worker.preview; verify exact live CPM membership
        # before the new SKU-based admission can create its write run.
        CleanerWbSource.from_env(account).refresh_target(target)
        admitted=_admitted_targets(package,targets,admission_dir.resolve())
        _verify_fresh_card(package,target,admission_dir.resolve(),service)
        preview=service.manual_apply_preview(request['scan_run_id'],target)
        # The exact card receipt is part of the caller-visible candidate even
        # though preparation itself is entirely local and has no WB write.
        outer={**preview, 'operation_id':operation_id,
               'scope':dict(target_count=1,candidate_count=len(preview['candidates']),kind='manual_apply'),
               'recovery':dict(kind='exact_manual_prepare',scan_run_id=request['scan_run_id'],operation_id=operation_id),
               'candidate_sha256':'sha256:'+digest(dict(worker_candidate=preview['candidate_sha256'],manual_admission=admitted))}
        if action=='preview':return outer
        if action!='apply' or str(envelope.get('expected_prestate') or '')!=outer['prestate_sha256'] or str(envelope.get('expected_candidate') or '')!=outer['candidate_sha256']:_fail('manual_prepare_drift')
        prepared=service.prepare_manual_apply(request['scan_run_id'],target,preview['candidate_sha256'],operation_id,Principal(owner,authenticated=True,auth_enabled=True,ads_access=True))
        return dict(operation_id=operation_id,disposition='submitted',run_id=prepared['run_id'])
    if mode!='manual' or not isinstance(request.get('run_id'),str):_fail('request_invalid')
    targets=_targets(request)
    package=_package(package_path,account,generation) if action!='readback' else None
    admitted=_admitted_targets(package,targets,admission_dir.resolve()) if package else None
    source=CleanerWbSource.from_env(account)
    worker=ManualCleanerWorker(service,source,AdmissionGuard(admission_dir.resolve(),service.store),generation=generation,
                               card_verifier=lambda target:_verify_fresh_card(package,target,admission_dir.resolve(),service)) if package else None
    def manual_preview():
        for target in targets:_verify_fresh_card(package,target,admission_dir.resolve(),service)
        result=worker.preview(run_id=request['run_id'],targets=targets)
        # Bind preview to the current approved fresh-card receipt as well as
        # the WB target snapshot. A package/card change cannot reuse a preview.
        result['_worker_prestate_sha256']=result['prestate_sha256']
        result['_worker_candidate_sha256']=result['candidate_sha256']
        result['candidate_sha256']='sha256:'+digest(dict(worker_candidate=result['_worker_candidate_sha256'],manual_admission=admitted))
        result['operation_id']=operation_id
        return result
    if action=='preview':return manual_preview()
    if action=='apply':
        outer=manual_preview()
        if outer['prestate_sha256']!=str(envelope.get('expected_prestate') or '') or outer['candidate_sha256']!=str(envelope.get('expected_candidate') or ''):_fail('manual_preview_drift')
        worker.execute(run_id=request['run_id'],targets=targets,expected_prestate=outer['_worker_prestate_sha256'],expected_candidate=outer['_worker_candidate_sha256'],production_operation_id=operation_id,reviewed_candidate=outer['candidate_sha256'])
        return dict(operation_id=operation_id,disposition='submitted')
    # Readback is the only allowed replay path after an ambiguous submit or a
    # consumed exact run. It cannot claim a run or send set-minus.
    with service.store.read() as c:
        bindings=[json.loads(row[0]) for row in c.execute("SELECT facts FROM cleaner_events WHERE account=? AND run_id=? AND kind='stage_e_manual_binding'",(service.key,request['run_id']))]
        binding=next((v for v in bindings if v.get('operation_id')==operation_id),None)
        operations=[row[0] for row in c.execute("SELECT operation_id FROM cleaner_write_operations WHERE account=? AND run_id=? AND state!='cancelled_before_send'",(service.key,request['run_id']))]
    if not binding:return dict(operation_id=operation_id,state='not_submitted')
    from packages.application.search_cluster_cleaner_writer import CleanerReadback
    for internal_operation_id in operations:CleanerReadback(service,source,generation=generation).tick(operation_id=internal_operation_id)
    # A recovery readback always recloses the database-side window. The external
    # capability is deliberately retained when a crashed owner is still
    # unresolved; it cannot grant another submit while the guard stays held.
    service.close_manual_window()
    # A crashed manual worker may leave a bound run with a prepared operation
    # but no dispatch right. The held external guard checks every prior seal,
    # fences the dead owner, and atomically cancels only the exact unsent work.
    # Returning not_submitted is safe only after its capability is also closed.
    try:
        guard=AdmissionGuard(admission_dir.resolve(),service.store)
        if guard.recover_unsubmitted_manual_run(cleaner=service,generation=generation,
                production_operation_id=operation_id,run_id=request['run_id']):
            return dict(operation_id=operation_id,state='not_submitted')
    except CleanerError:
        pass
    with service.store.read() as c:
        terminal=[row[0] for row in c.execute("SELECT operation_id FROM cleaner_write_operations WHERE account=? AND run_id=? AND state IN ('confirmed','rejected','requires_review')",(service.key,request['run_id']))]
    recovered=False
    if terminal:
        guard=AdmissionGuard(admission_dir.resolve(),service.store)
        try:
            guard.recover_manual_capability(account=account,generation=generation,production_operation_id=operation_id,terminal_operation_ids=terminal)
            recovered=True
        except CleanerError:
            # A live owner is an active admission boundary.  It is not safe to
            # report the write complete merely because WB already reflects it.
            # A prior successful recovery is also accepted only after checking
            # that no capability or owner remains in the held guard state.
            try:
                with guard._lock(): state=guard._load()
                recovered=bool(state.get('hold') and not state.get('owner') and not state.get('manual_capability'))
            except CleanerError:
                recovered=False
        if recovered:
            try:service.finalize_recovered_manual_run(run_id=request['run_id'],production_operation_id=operation_id)
            except CleanerError: recovered=False
    elif not operations:
        # A scan or a pre-dispatch refusal can crash after the exact binding but
        # before any operation exists.  It is terminal only after the lease
        # expired and the held guard has no owner/capability; this path never
        # makes a new write right or a replacement run.
        try:
            guard=AdmissionGuard(admission_dir.resolve(),service.store)
            try:guard.recover_empty_manual_capability(account=account,generation=generation,
                 production_operation_id=operation_id,run_id=request['run_id'])
            except CleanerError:pass
            with guard._lock(): state=guard._load()
            clean_guard=bool(state.get('hold') and not state.get('owner') and not state.get('manual_capability'))
            if clean_guard:
                service.close_manual_window()
                service.finalize_recovered_manual_run(run_id=request['run_id'],production_operation_id=operation_id,allow_no_operations=True)
                recovered=True
        except CleanerError:
            recovered=False
    with service.store.read() as c:
        run=c.execute('SELECT state,kind,targets FROM cleaner_runs WHERE account=? AND run_id=?',(service.key,request['run_id'])).fetchone()
        ops=c.execute("SELECT operation_id,state,target FROM cleaner_write_operations WHERE account=? AND run_id=? AND state!='cancelled_before_send'",(service.key,request['run_id'])).fetchall()
        actual={(row['target'],item['query_hash'],item['decision_id']) for row in ops for item in c.execute(
            'SELECT query_hash,decision_id FROM cleaner_write_items WHERE operation_id=?',(row['operation_id'],))}
    if not run:return dict(operation_id=operation_id,state='not_submitted')
    expected={(item['target'],item['query_hash'],item['decision_id']) for item in json.loads(run['targets'])} if run['kind']=='manual_apply' else set()
    # The run's original partial summary can record an early readback lag.
    # Once every exact write operation is confirmed, late settlement is the
    # truthful outcome; the immutable run_finished summary stays untouched.
    if recovered and ops and expected==actual and all(op['state']=='confirmed' for op in ops):return dict(operation_id=operation_id,state='applied')
    if recovered and ops and any(op['state'] in {'rejected','requires_review'} for op in ops):return dict(operation_id=operation_id,state='failed')
    if not ops and run['state']=='complete' and run['kind']=='scan':return dict(operation_id=operation_id,state='no_change')
    if not ops and run['kind']=='manual_apply' and not recovered:return dict(operation_id=operation_id,state='ambiguous')
    if not ops and run['state'] in {'partial','failed','stopped'}:
        return dict(operation_id=operation_id,state='failed')
    return dict(operation_id=operation_id,state='ambiguous')

def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument('--runtime-dir',required=True,type=Path);parser.add_argument('--env-file',required=True,type=Path);parser.add_argument('--admission-dir',required=True,type=Path);args=parser.parse_args()
    try:result=execute(json.loads(sys.stdin.read()),runtime_dir=args.runtime_dir,env_file=args.env_file,admission_dir=args.admission_dir)
    except Exception as exc:
        print(json.dumps(dict(status='blocked',error=dict(code=exc.code if isinstance(exc,CleanerError) else type(exc).__name__,message=' '.join(str(exc).split())[:300])),ensure_ascii=False,sort_keys=True));return 2
    print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0

if __name__=='__main__':raise SystemExit(main())
