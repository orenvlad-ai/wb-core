"""Additive, one-submit SKU admission over the immutable Stage E baseline.

Private reviewed files are never imported as a second baseline.  A fsynced
claim precedes domain commands; only a published claim is an admission index.
Recovery is an explicit new local operation over the same claim, never WB apply.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from packages.contracts.search_cluster_cleaner import CleanerError, Principal, Profile, canonical, digest
from packages.domain.search_cluster_card_semantics import PROJECTION_VERSION, project_card

SCHEMA = 'search_cluster_cleaner_admission_extension/v1'
JOURNAL_SCHEMA = 'search_cluster_cleaner_sku_admission_journal/v1'
JOURNAL_NAME = 'sku-admission-journal.json'
_ID = re.compile(r'[a-z0-9][a-z0-9._-]{7,79}')
_OP = re.compile(r'[a-z0-9][a-z0-9._-]{7,127}')
_SHA = re.compile(r'sha256:[0-9a-f]{64}')
MAX_BYTES = 16 * 1024 * 1024


def fail(code):
    raise CleanerError(code, 'Допуск новых товаров требует точной сверки', 409)


def private_bytes(path: Path) -> bytes:
    """Bounded no-symlink private reads, including the containing directory."""
    try:
        parent = path.parent.stat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077:
            fail('sku_admission_permissions')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > MAX_BYTES:
                fail('sku_admission_permissions')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                fail('sku_admission_file_limit')
            return raw
        finally:
            os.close(fd)
    except OSError as exc:
        raise CleanerError('sku_admission_unavailable', 'Приватный пакет допуска недоступен', 409) from exc


def sha(raw: bytes) -> str:
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def _json(path):
    try:
        return json.loads(private_bytes(path))
    except (ValueError, TypeError) as exc:
        raise CleanerError('sku_admission_invalid', 'Приватный пакет допуска повреждён', 409) from exc


def _journal(directory):
    path = directory / JOURNAL_NAME
    if not path.exists():
        return dict(schema=JOURNAL_SCHEMA, records={})
    value = _json(path)
    if not isinstance(value, dict) or set(value) != {'schema', 'records'} or value['schema'] != JOURNAL_SCHEMA or not isinstance(value['records'], dict):
        fail('sku_admission_journal_invalid')
    for operation, row in value['records'].items():
        if (not _OP.fullmatch(operation) or not isinstance(row, dict)
                or row.get('operation_id') != operation or row.get('phase') not in {'claimed', 'published', 'recovered'}
                or not _ID.fullmatch(str(row.get('extension_id') or ''))
                or not _SHA.fullmatch(str(row.get('extension_sha256') or ''))
                or not isinstance(row.get('candidate'), dict) or not isinstance(row.get('prestate'), dict)):
            fail('sku_admission_journal_invalid')
    return value


def _save(directory, journal):
    raw = canonical(journal).encode('utf-8')
    if len(raw) > MAX_BYTES:
        fail('sku_admission_file_limit')
    temp = directory / ('.sku-admission-' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, directory / JOURNAL_NAME)
        fd = os.open(directory, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if temp.exists(): temp.unlink()


@contextmanager
def _lock(directory):
    with (directory / 'sku-admission.lock').open('a+b') as handle:
        os.fchmod(handle.fileno(), 0o600)
        try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: fail('sku_admission_busy')
        try: yield
        finally: fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass
class ApprovedContext:
    profiles: dict
    cards: dict
    source_hashes: dict
    evidence: dict
    extension_hashes: dict


def base_context(package, directory):
    from apps.search_cluster_cleaner_stage_e import _approved_card_source, _card_evidence_rows
    evidence = _card_evidence_rows(package, directory)
    cards = _approved_card_source(package, directory)
    for nm, receipt in evidence.items():
        if nm not in cards or receipt['current_card_sha256'] != cards[nm]['card_digest']:
            fail('approved_card_source_mismatch')
    profiles = {p.nm_id: p for p in map(Profile.parse, package['profiles'])}
    return ApprovedContext(profiles, cards, {nm: package['provenance']['fresh_cards_sha256'] for nm in cards}, evidence, {})


def _extension(directory, extension_id, expected_sha, *, package, package_path, cleaner, generation, runtime_sha=None):
    if not _ID.fullmatch(str(extension_id)) or not _SHA.fullmatch(str(expected_sha)):
        fail('sku_admission_request_invalid')
    parent = directory / 'extensions' / extension_id
    if parent.is_symlink() or parent.parent.is_symlink(): fail('sku_admission_permissions')
    raw = private_bytes(parent / 'extension.json')
    if sha(raw) != expected_sha: fail('sku_admission_extension_drift')
    try: value = json.loads(raw)
    except ValueError: fail('sku_admission_invalid')
    required = {'schema', 'extension_id', 'seller_id', 'account_scope', 'generation', 'owner_username',
                'base_package_sha256', 'runtime_sha', 'rules_version', 'rules_digest', 'projection_version',
                'profiles', 'card_source_sha256', 'approved_at', 'provenance'}
    # Historical published claims retain their original rules provenance.
    # Only a new registration/recovery must use this exact release's rules;
    # every path still recomputes the current card/profile projection below.
    if (not isinstance(value, dict) or set(value) != required or value['schema'] != SCHEMA
            or value['extension_id'] != extension_id or value['seller_id'] != cleaner.account.seller_id
            or value['account_scope'] != cleaner.account.account_scope or value['generation'] != generation
            or value['owner_username'] != cleaner.owner_username
            or value['base_package_sha256'] != sha(private_bytes(package_path))
            or not isinstance(value['rules_version'],str) or not value['rules_version']
            or not re.fullmatch(r'[0-9a-f]{64}',str(value['rules_digest']))
            or not isinstance(value['projection_version'],str) or not value['projection_version']
            or not re.fullmatch(r'[0-9a-f]{40}', str(value['runtime_sha']))
            or (runtime_sha is not None and (value['runtime_sha'] != runtime_sha
                or value['rules_version'] != cleaner.rules_version or value['rules_digest'] != cleaner.rules_digest
                or value['projection_version'] != PROJECTION_VERSION))
            or not isinstance(value['profiles'], list) or not value['profiles'] or len(value['profiles']) > 100
            or not isinstance(value['provenance'], dict) or not value['provenance']
            or not isinstance(value['approved_at'], str) or not value['approved_at']):
        fail('sku_admission_identity_mismatch')
    profiles = [Profile.parse(p) for p in value['profiles']]
    if len({p.nm_id for p in profiles}) != len(profiles) or any(p.version != 1 for p in profiles):
        fail('sku_admission_profile_invalid')
    if {p.nm_id for p in profiles} & {p['nm_id'] for p in package['profiles']}:
        fail('sku_admission_overlap')
    source_raw = private_bytes(parent / 'cards.json')
    if sha(source_raw) != value['card_source_sha256']: fail('sku_admission_source_drift')
    try: source = json.loads(source_raw)
    except ValueError: fail('sku_admission_invalid')
    if not isinstance(source, dict) or set(source) != {'cards'} or not isinstance(source['cards'], list):
        fail('sku_admission_invalid')
    cards = {}
    for card in source['cards']:
        if not isinstance(card, dict) or set(card) != {'nm_id', 'title', 'vendor_code', 'description', 'characteristics', 'subject_id', 'card_digest'}:
            fail('sku_admission_card_invalid')
        try: nm = int(card['nm_id'])
        except (ValueError, TypeError): fail('sku_admission_card_invalid')
        if nm <= 0 or str(nm) != str(card['nm_id']) or nm in cards: fail('sku_admission_card_invalid')
        if card['card_digest'] != 'sha256:' + digest({k: v for k, v in card.items() if k != 'card_digest'}):
            fail('sku_admission_card_invalid')
        cards[nm] = card
    if set(cards) != {p.nm_id for p in profiles}: fail('sku_admission_profile_invalid')
    for p in profiles:
        projection = project_card(cards[p.nm_id], require_subject=True)
        expected = dict(version=PROJECTION_VERSION, category=p.category, models=list(p.models), kind=p.kind, frame=p.frame)
        if projection != expected: fail('sku_admission_profile_mismatch')
    return value, profiles, cards


def load_context(package, directory, *, package_path, cleaner, generation):
    """Shared read-only admission for eligibility, preview and every write."""
    context = base_context(package, directory)
    journal = _journal(directory)
    for record in journal['records'].values():
        if record['phase'] != 'published' or record.get('original_operation_id'): continue
        value, profiles, cards = _extension(directory, record['extension_id'], record['extension_sha256'],
            package=package, package_path=package_path, cleaner=cleaner, generation=generation)
        if record['candidate'] != _candidate(value, record['extension_sha256']): fail('sku_admission_journal_invalid')
        if set(cards) & set(context.cards) or {p.nm_id for p in profiles} & set(context.profiles): fail('sku_admission_overlap')
        context.profiles.update({p.nm_id: p for p in profiles})
        context.cards.update(cards)
        context.source_hashes.update({nm: value['card_source_sha256'] for nm in cards})
        context.extension_hashes.update({nm: record['extension_sha256'] for nm in cards})
    return context


def _candidate(value, extension_sha):
    return dict(extension_sha256=extension_sha, card_source_sha256=value['card_source_sha256'],
                base_package_sha256=value['base_package_sha256'], profiles_sha256='sha256:' + digest(value['profiles']),
                runtime_sha=value['runtime_sha'], rules_version=value['rules_version'], rules_digest=value['rules_digest'],
                projection_version=value['projection_version'], account=[value['seller_id'], value['account_scope']], generation=value['generation'])


def _prestate(cleaner, directory, package_path, profiles):
    with cleaner.store.read() as c:
        settings = dict(cleaner._settings(c))
        states = {}
        for p in profiles:
            head = c.execute('SELECT active_version,revision FROM cleaner_profile_heads WHERE account=? AND nm_id=?', (cleaner.key,p.nm_id)).fetchone()
            rows = [dict(r) for r in c.execute('SELECT version,fingerprint,payload,actor FROM cleaner_profiles WHERE account=? AND nm_id=? ORDER BY version', (cleaner.key,p.nm_id))]
            states[str(p.nm_id)] = dict(head=dict(head) if head else None, versions=rows)
            if c.execute('SELECT 1 FROM cleaner_observations WHERE account=? AND nm_id=? LIMIT 1', (cleaner.key,p.nm_id)).fetchone():
                fail('sku_admission_preexisting_work')
        baselines = [tuple(r) for r in c.execute('SELECT target,query_hash,import_digest,decision_id FROM cleaner_baselines WHERE account=? ORDER BY target,query_hash',(cleaner.key,))]
    if settings['generation'] != profiles_generation(directory, cleaner): fail('sku_admission_generation_mismatch')
    return dict(settings=settings, profiles=states, baseline_sha256='sha256:'+digest(baselines),
                baseline_count=len(baselines), base_package_sha256=sha(private_bytes(package_path)),
                base_evidence_sha256=sha(private_bytes(directory/'current-card-evidence.json')),
                base_cards_sha256=sha(private_bytes(directory/'card-source-approved.json')),
                journal_sha256='sha256:'+digest(_journal(directory)))


def profiles_generation(directory, cleaner):
    # Caller also binds the StoreRegistry generation. This validates the
    # server-owned identity without changing configuration or any setting.
    config = _json(directory/'stage-e-config.json')
    if (config.get('seller_id') != cleaner.account.seller_id or config.get('account_scope') != cleaner.account.account_scope
            or config.get('owner_username') != cleaner.owner_username): fail('sku_admission_identity_mismatch')
    return config.get('generation')


def _fresh(profiles, cards, fetch):
    deadline = time.monotonic() + 90
    for p in profiles:
        if time.monotonic() >= deadline: fail('sku_admission_fresh_deadline')
        fresh = fetch(p.nm_id)
        if (str(fresh.get('nm_id')) != str(p.nm_id)
                or project_card(fresh, require_subject=True) != project_card(cards[p.nm_id], require_subject=True)):
            fail('sku_admission_current_card_drift')
        if time.monotonic() >= deadline: fail('sku_admission_fresh_deadline')


def _check_owned_profiles(cleaner, profiles, root_operation, *, allow_absent):
    """Only exact domain-command receipts can explain a partial registration."""
    actor = cleaner.owner_username.strip().casefold()
    with cleaner.store.read() as c:
        for p in profiles:
            state = cleaner.get_profile(p.nm_id, Principal(actor,True,True,True))
            if not state['versions'] and state['revision'] == 0 and allow_absent: continue
            create_id = _command_id(root_operation, p.nm_id, 'create')
            request = c.execute('SELECT route,digest,outcome FROM cleaner_requests WHERE account=? AND actor=? AND request_id=?',(cleaner.key,actor,create_id)).fetchone()
            create_payload = dict(request_id=create_id, expected_revision=0, profile=p.as_dict())
            if (not request or request['route'] != f'profiles/{p.nm_id}/versions'
                    or request['digest'] != digest([request['route'],create_payload])
                    or len(state['versions']) != 1 or canonical(state['versions'][0]) != canonical(p.as_dict())):
                fail('sku_admission_profile_conflict')
            if state['active_version'] is None and state['revision'] == 1 and allow_absent: continue
            activate_id = _command_id(root_operation,p.nm_id,'activate')
            request = c.execute('SELECT route,digest FROM cleaner_requests WHERE account=? AND actor=? AND request_id=?',(cleaner.key,actor,activate_id)).fetchone()
            activate_payload = dict(request_id=activate_id,expected_revision=1,version=1)
            if (not request or request['route'] != f'profiles/{p.nm_id}/activate'
                    or request['digest'] != digest([request['route'],activate_payload])
                    or state['active_version'] != 1 or state['revision'] != 2): fail('sku_admission_profile_conflict')


def _command_id(operation,nm,phase):
    return 'sku-'+digest([operation,nm,phase])[:56]


def execute(*,action,operation_id,request,cleaner,directory,package,package_path,generation,runtime_sha,
            expected_prestate='',expected_candidate='',actor='',fetch=None,hook=None):
    directory=Path(directory);package_path=Path(package_path)
    hook=hook or (lambda *_:None)
    recover=request['mode']=='admit_profiles_recover'
    extension_id=request['extension_id'];extension_sha=request['extension_sha256']
    original=request.get('original_operation_id') if recover else operation_id
    if not _OP.fullmatch(str(original)): fail('sku_admission_request_invalid')
    journal=_journal(directory)
    record=journal['records'].get(operation_id)
    if record and action!='readback':
        if (record['extension_id']!=extension_id or record['extension_sha256']!=extension_sha
                or record.get('original_operation_id')!=(original if recover else None)):
            fail('sku_admission_operation_conflict')
        if action=='apply': fail('sku_admission_operation_consumed')
        # The launcher previews before checking readback on repeat. Return the
        # original saved proof; never perform a fresh registration or WB read.
        return dict(operation_id=operation_id,target='cleaner-sku-admission',scope=dict(profiles=len(record['prestate']['profiles'])),
                    prestate_sha256='sha256:'+digest(record['prestate']),candidate_sha256='sha256:'+digest(record['candidate']),
                    recovery=dict(kind='additive_sku_admission',original_operation_id=original),cleaning_started=False)
    if action=='readback':
        # No Content request, profile command, journal update, or new claim.
        if not record: return dict(operation_id=operation_id,state='not_submitted')
        if (record['extension_id']!=extension_id or record['extension_sha256']!=extension_sha
                or record.get('original_operation_id')!=(original if recover else None)):
            fail('sku_admission_operation_conflict')
        root=journal['records'].get(original)
        if not root or root['phase']!='published':
            return dict(operation_id=operation_id,state='ambiguous',reason='sku_admission_partial',recovery=dict(original_operation_id=original))
        value,profiles,cards=_extension(directory,extension_id,extension_sha,package=package,package_path=package_path,cleaner=cleaner,generation=generation)
        if root['candidate']!=_candidate(value,extension_sha): fail('sku_admission_operation_conflict')
        _check_owned_profiles(cleaner,profiles,original,allow_absent=False)
        return dict(operation_id=operation_id,state='applied',extension_id=extension_id,profiles=len(profiles),cleaning_started=False)
    value,profiles,cards=_extension(directory,extension_id,extension_sha,package=package,package_path=package_path,cleaner=cleaner,generation=generation,runtime_sha=runtime_sha)
    context=load_context(package,directory,package_path=package_path,cleaner=cleaner,generation=generation)
    if set(cards)&(set(context.profiles)|set(context.cards)|set(context.evidence)): fail('sku_admission_overlap')
    for claimed in journal['records'].values():
        if not claimed.get('original_operation_id') and claimed['extension_id']==extension_id and claimed['operation_id']!=original:
            fail('sku_admission_recovery_required')
        if (not claimed.get('original_operation_id') and claimed['operation_id']!=original
                and {str(p.nm_id) for p in profiles}&set(claimed['prestate']['profiles'])):
            fail('sku_admission_overlap')
    root=journal['records'].get(original)
    if recover:
        if not root or root.get('original_operation_id') or root['phase']!='claimed' or root['extension_id']!=extension_id or root['extension_sha256']!=extension_sha or root['candidate']!=_candidate(value,extension_sha):
            fail('sku_admission_recovery_mismatch')
        _check_owned_profiles(cleaner,profiles,original,allow_absent=True)
    else:
        if root: fail('sku_admission_operation_consumed')
        with cleaner.store.read() as c:
            for p in profiles:
                if c.execute('SELECT 1 FROM cleaner_profile_heads WHERE account=? AND nm_id=?',(cleaner.key,p.nm_id)).fetchone() or c.execute('SELECT 1 FROM cleaner_profiles WHERE account=? AND nm_id=?',(cleaner.key,p.nm_id)).fetchone():
                    fail('sku_admission_profile_conflict')
    if fetch is None:
        from apps.search_cluster_cleaner_stage_e import fetch_current_card
        fetch=fetch_current_card
    _fresh(profiles,cards,fetch)
    prestate=_prestate(cleaner,directory,package_path,profiles)
    if prestate['settings']['generation']!=generation or not prestate['settings']['baseline_ready']: fail('sku_admission_generation_mismatch')
    candidate=_candidate(value,extension_sha)
    preview=dict(operation_id=operation_id,target='cleaner-sku-admission',scope=dict(profiles=len(profiles),nm_ids=[p.nm_id for p in profiles]),
                 prestate_sha256='sha256:'+digest(prestate),candidate_sha256='sha256:'+digest(candidate),
                 recovery=dict(kind='additive_sku_admission',original_operation_id=original,prior_profiles_absent=not recover),cleaning_started=False)
    if action=='preview': return preview
    with _lock(directory):
        if operation_id in _journal(directory)['records']: fail('sku_admission_operation_consumed')
        current=_prestate(cleaner,directory,package_path,profiles)
        if expected_prestate!='sha256:'+digest(current) or expected_candidate!=preview['candidate_sha256'] or current!=prestate:
            fail('sku_admission_preview_drift')
        journal=_journal(directory)
        record=dict(operation_id=operation_id,extension_id=extension_id,extension_sha256=extension_sha,
                    original_operation_id=original if recover else None,candidate=candidate,prestate=prestate,
                    phase='claimed',actor=str(actor)[:160],claimed_at=cleaner.clock())
        journal['records'][operation_id]=record
        _save(directory,journal);hook('claimed',None)
        principal=Principal(cleaner.owner_username,True,True,True)
        for p in profiles:
            create_id=_command_id(original,p.nm_id,'create')
            cleaner.create_profile(p.nm_id,dict(request_id=create_id,expected_revision=0,profile=p.as_dict()),principal)
            hook('created',p.nm_id)
            activate_id=_command_id(original,p.nm_id,'activate')
            cleaner.activate_profile(p.nm_id,dict(request_id=activate_id,expected_revision=1,version=1),principal)
            hook('activated',p.nm_id)
        _check_owned_profiles(cleaner,profiles,original,allow_absent=False)
        # Re-read private immutable bytes immediately before publication.
        _extension(directory,extension_id,extension_sha,package=package,package_path=package_path,cleaner=cleaner,generation=generation,runtime_sha=runtime_sha)
        after=_prestate(cleaner,directory,package_path,profiles)
        for key in ('settings','baseline_sha256','baseline_count','base_package_sha256','base_evidence_sha256','base_cards_sha256'):
            if after[key]!=prestate[key]: fail('sku_admission_preservation_drift')
        journal=_journal(directory)
        journal['records'][original]['phase']='published'
        journal['records'][original]['published_at']=cleaner.clock()
        if recover: journal['records'][operation_id]['phase']='recovered'
        hook('before_publish',None)
        _save(directory,journal);hook('published',None)
        return dict(operation_id=operation_id,disposition='submitted',cleaning_started=False)
