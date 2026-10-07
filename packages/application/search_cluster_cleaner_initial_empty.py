"""Owner-confirmed, pair-scoped first-fill evidence. Never a WB empty receipt.

The private journal is outside business backups. A fsynced prepared-operation
claim precedes the DB commit; losing the commit sacrifices availability rather
than granting another first write. Readback never uses the empty resolver.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import uuid
from packages.application.search_cluster_cleaner_onboarding import private_bytes,sha
from packages.contracts.search_cluster_cleaner import CleanerError,canonical,digest

SCHEMA='search_cluster_cleaner_initial_empty/v1'
JOURNAL='initial-empty-journal.json'
_ID=re.compile(r'[a-z0-9][a-z0-9._-]{7,79}')
_SHA=re.compile(r'sha256:[0-9a-f]{64}')


def fail(code):raise CleanerError(code,'Начальное пустое состояние требует точного допуска владельца',409)

def timestamp(value):
    try:result=datetime.fromisoformat(value.replace('Z','+00:00'))
    except (AttributeError,ValueError,TypeError):fail('initial_empty_invalid')
    if result.tzinfo is None:fail('initial_empty_invalid')
    return result


@contextmanager
def locked(directory):
    fd=os.open(directory/'initial-empty.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield
    finally:fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)


def save(directory,value):
    temp=directory/('.initial-empty-'+uuid.uuid4().hex+'.tmp')
    try:
        with os.fdopen(os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb') as stream:
            stream.write(canonical(value).encode());stream.flush();os.fsync(stream.fileno())
        os.replace(temp,directory/JOURNAL)
        fd=os.open(directory,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:temp.unlink(missing_ok=True)


def declaration(directory,evidence_id,expected_sha):
    if not _ID.fullmatch(str(evidence_id)) or not _SHA.fullmatch(str(expected_sha)):fail('initial_empty_identity_invalid')
    raw=private_bytes(directory/('initial-empty.'+evidence_id+'.json'))
    if sha(raw)!=expected_sha:fail('initial_empty_integrity')
    try:value=json.loads(raw)
    except ValueError:fail('initial_empty_invalid')
    return validate_declaration(value,evidence_id)


def validate_declaration(value,evidence_id):
    if not _ID.fullmatch(str(evidence_id)):fail('initial_empty_identity_invalid')
    required={'schema','evidence_id','account_key','generation','owner_username','confirmed_at','confirmation','scope_sha256','runtime_sha','rules_digest','targets'}
    if not isinstance(value,dict) or set(value)!=required or value['schema']!=SCHEMA or value['evidence_id']!=evidence_id:fail('initial_empty_invalid')
    targets=value['targets']
    if not isinstance(targets,list) or not 1<=len(targets)<=100:fail('initial_empty_scope_invalid')
    keys=[]
    for row in targets:
        if (not isinstance(row,dict) or set(row)!={'advert_id','nm_id','profile_fingerprint'}
                or any(type(row.get(k)) is not int or row[k]<=0 for k in ('advert_id','nm_id'))
                or not re.fullmatch(r'[0-9a-f]{64}',str(row.get('profile_fingerprint')))):fail('initial_empty_scope_invalid')
        keys.append(f"{row['advert_id']}:{row['nm_id']}")
    if len(keys)!=len(set(keys)) or value['scope_sha256']!='sha256:'+digest(sorted(targets,key=lambda r:(r['advert_id'],r['nm_id']))):fail('initial_empty_scope_invalid')
    if not isinstance(value['confirmation'],str) or not value['confirmation'].strip():fail('initial_empty_owner_confirmation_required')
    try:
        if timestamp(value['confirmed_at']).tzinfo is None:fail('initial_empty_invalid')
    except (ValueError,TypeError):fail('initial_empty_invalid')
    if not re.fullmatch(r'[0-9a-f]{40}',str(value['runtime_sha'])) or not re.fullmatch(r'[0-9a-f]{64}',str(value['rules_digest'])):fail('initial_empty_invalid')
    return value


def build_declaration(*,cleaner,evidence_id,pairs,runtime_sha,confirmed_at,confirmation):
    """Read-only preparation; caller supplies the owner's exact confirmed scope.

    Run with the deployed Python/runtime, not a workstation's rules digest.
    This returns data only: no journal, DB, network or configuration mutation.
    """
    rows=[]
    with cleaner.store.read() as c:
        generation=cleaner._settings(c)['generation']
        for pair in pairs:
            if not isinstance(pair,dict) or set(pair)!={'advert_id','nm_id'}:fail('initial_empty_scope_invalid')
            profile=cleaner._profile(c,pair['nm_id'])
            if not profile:fail('initial_empty_profile_mismatch')
            rows.append(dict(pair,profile_fingerprint=profile.semantic_fingerprint))
    rows.sort(key=lambda row:(row['advert_id'],row['nm_id']))
    value=dict(schema=SCHEMA,evidence_id=evidence_id,account_key=cleaner.key,generation=generation,
               owner_username=cleaner.owner_username.strip().casefold(),confirmed_at=confirmed_at,
               confirmation=confirmation,scope_sha256='sha256:'+digest(rows),runtime_sha=runtime_sha,
               rules_digest=cleaner.rules_digest,targets=rows)
    return validate_declaration(value,evidence_id)


def journal(directory):
    path=directory/JOURNAL
    if not path.exists():return dict(schema=SCHEMA,installations={},pairs={})
    try:value=json.loads(private_bytes(path))
    except ValueError:fail('initial_empty_integrity')
    if not isinstance(value,dict) or set(value)!={'schema','installations','pairs'} or value['schema']!=SCHEMA or not isinstance(value['installations'],dict) or not isinstance(value['pairs'],dict):fail('initial_empty_integrity')
    expected_pairs={}
    for operation,record in value['installations'].items():
        if not isinstance(record,dict) or record.get('operation_id')!=operation:fail('initial_empty_integrity')
        body=declaration(directory,record.get('evidence_id'),record.get('evidence_sha256'))
        if record.get('candidate')!=body or record.get('candidate_sha256')!='sha256:'+digest(body):fail('initial_empty_integrity')
        for row in body['targets']:
            key=f"{row['advert_id']}:{row['nm_id']}"
            if key in expected_pairs:fail('initial_empty_integrity')
            expected_pairs[key]=operation
    if set(expected_pairs)!=set(value['pairs']):fail('initial_empty_integrity')
    for key,row in value['pairs'].items():
        if (not isinstance(row,dict) or row.get('installation')!=expected_pairs[key]
                or row.get('state') not in {'available','claimed','closed'}):fail('initial_empty_integrity')
        if row['state']=='claimed' and not all(row.get(k) for k in ('operation_id','run_id','worker_token','generation','claimed_at')):fail('initial_empty_integrity')
    return value


def history(c,cleaner,directory,key,own_operation=None):
    operations=c.execute('SELECT * FROM cleaner_write_operations WHERE account=? AND target=?',(cleaner.key,key)).fetchall()
    if any(row['operation_id']!=own_operation for row in operations):return 'previous_write_operation'
    if own_operation:
        own=next((row for row in operations if row['operation_id']==own_operation),None)
        if not own or own['state']!='prepared' or own['dispatch_count']!=0:return 'previous_write_attempt'
    if c.execute("SELECT 1 FROM cleaner_observations WHERE account=? AND target=? AND observed_state='excluded'",(cleaner.key,key)).fetchone():return 'known_nonempty_minus'
    if c.execute("SELECT 1 FROM cleaner_baselines WHERE account=? AND target=? AND observed_state='excluded'",(cleaner.key,key)).fetchone():return 'known_nonempty_minus'
    for event in c.execute("SELECT kind,facts FROM cleaner_events WHERE account=? AND kind IN('initial_empty_nonempty','partial_snapshot_preview','external_state_drift','controversial_decision') AND json_extract(facts,'$.target')=?",(cleaner.key,key)):
        facts=json.loads(event['facts'])
        if (event['kind'] in {'initial_empty_nonempty','external_state_drift'}
                or event['kind']=='controversial_decision' and facts.get('before')=='excluded'
                or event['kind']=='partial_snapshot_preview' and any(row['observed_state']=='excluded' for row in facts.get('queries',[]))):return 'known_nonempty_minus'
    from packages.application.search_cluster_cleaner_admission import AdmissionGuard
    guard=AdmissionGuard(directory,cleaner.store)._load()
    if guard['account']!=cleaner.key or any(row['target']==key for row in guard['seals'].values()):return 'previous_dispatch_seal'
    return None


def bound(body,cleaner,generation,runtime_sha=None):
    if (body['account_key']!=cleaner.key or body['generation']!=generation
            or body['owner_username']!=cleaner.owner_username.strip().casefold()
            or body['rules_digest']!=cleaner.rules_digest or runtime_sha is not None and body['runtime_sha']!=runtime_sha):fail('initial_empty_binding_mismatch')
    with cleaner.store.read() as c:
        for row in body['targets']:
            profile=cleaner._profile(c,row['nm_id'])
            if not profile or profile.semantic_fingerprint!=row['profile_fingerprint']:fail('initial_empty_profile_mismatch')


def prestate(cleaner,directory,body):
    with cleaner.store.read() as c:
        settings=dict(cleaner._settings(c))
        blocked={f"{row['advert_id']}:{row['nm_id']}":history(c,cleaner,directory,f"{row['advert_id']}:{row['nm_id']}") for row in body['targets']}
    return dict(settings=settings,journal_sha256=sha(private_bytes(directory/JOURNAL)) if (directory/JOURNAL).exists() else None,blocked=blocked)


def execute(*,action,operation_id,request,cleaner,directory,generation,runtime_sha,expected_prestate='',expected_candidate='',actor='',hook=None):
    directory=Path(directory);hook=hook or (lambda stage:None)
    if directory.is_relative_to(cleaner.store.registry.runtime_dir):fail('initial_empty_path_invalid')
    evidence_id=request['evidence_id'];evidence_sha=request['evidence_sha256']
    body=declaration(directory,evidence_id,evidence_sha)
    state=journal(directory);existing=state['installations'].get(operation_id)
    if existing:
        if existing['candidate']!=body or existing['evidence_sha256']!=evidence_sha:fail('initial_empty_operation_conflict')
        if action=='readback':return dict(operation_id=operation_id,state='applied',pairs=len(body['targets']),cleaning_started=False)
        return dict(operation_id=operation_id,target='cleaner-initial-empty',scope=dict(pairs=len(body['targets'])),prestate_sha256=existing['prestate_sha256'],candidate_sha256=existing['candidate_sha256'],recovery=dict(kind='initial_empty_atomic_journal'),cleaning_started=False)
    if action=='readback':return dict(operation_id=operation_id,state='not_submitted')
    bound(body,cleaner,generation,runtime_sha)
    before=prestate(cleaner,directory,body)
    if before['settings']['enabled'] or before['settings']['generation']!=generation or not before['settings']['baseline_ready']:fail('initial_empty_manual_only')
    if any(before['blocked'].values()):fail('initial_empty_history_forbidden')
    keys=list(before['blocked'])
    if any(key in state['pairs'] for key in keys):fail('initial_empty_overlap')
    preview=dict(operation_id=operation_id,target='cleaner-initial-empty',scope=dict(pairs=len(keys)),prestate_sha256='sha256:'+digest(before),candidate_sha256='sha256:'+digest(body),recovery=dict(kind='initial_empty_atomic_journal'),cleaning_started=False)
    if action=='preview':return preview
    with locked(directory):
        # File, exact scope, settings and historical evidence are re-read at CAS.
        if declaration(directory,evidence_id,evidence_sha)!=body:fail('initial_empty_integrity')
        bound(body,cleaner,generation,runtime_sha)
        if prestate(cleaner,directory,body)!=before or expected_prestate!=preview['prestate_sha256'] or expected_candidate!=preview['candidate_sha256']:fail('initial_empty_prestate_drift')
        state=journal(directory)
        state['installations'][operation_id]=dict(operation_id=operation_id,evidence_id=evidence_id,evidence_sha256=evidence_sha,candidate=body,candidate_sha256=preview['candidate_sha256'],prestate_sha256=preview['prestate_sha256'],actor=str(actor)[:160],installed_at=cleaner.clock())
        state['pairs'].update({key:dict(installation=operation_id,state='available') for key in keys})
        save(directory,state);hook('installed')
    return dict(operation_id=operation_id,disposition='submitted',cleaning_started=False)


class InitialEmptyPolicy:
    def __init__(self,cleaner,directory,generation,run_id):
        self.cleaner,self.directory,self.generation,self.run_id=cleaner,Path(directory),generation,run_id
        self.operation=None;self.token=None

    def manual_intent(self,c,record=None):
        app=self.cleaner
        run=c.execute('SELECT * FROM cleaner_runs WHERE account=? AND run_id=?',(app.key,self.run_id)).fetchone()
        if not run or run['kind'] not in {'scan','manual_apply'} or run['trigger'] not in {'manual_exact','manual_exact_candidates'}:return False
        if run['kind']=='manual_apply':
            event=c.execute("SELECT facts FROM cleaner_events WHERE account=? AND run_id=? AND kind='manual_apply_prepared'",(app.key,self.run_id)).fetchone()
            if not event:return False
            scan=json.loads(event[0])['scan_run_id']
        else:scan=self.run_id
        intent=c.execute("SELECT facts,created_at FROM cleaner_events WHERE account=? AND run_id=? AND kind='self_service_requested'",(app.key,scan)).fetchone()
        if not intent:return False
        facts=json.loads(intent[0]);batch=facts.get('batch_id')
        if (facts.get('account_key')!=app.key or facts.get('generation')!=self.generation
                or record and timestamp(intent['created_at'])<timestamp(record['installed_at'])):return False
        daily=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cleaner_daily_occurrences'").fetchone()
        return not (batch and daily and c.execute('SELECT 1 FROM cleaner_daily_occurrences WHERE account=? AND batch_id=?',(app.key,batch)).fetchone())

    def resolve(self,snapshot):
        app=self.cleaner;t=snapshot.target
        if not (self.directory/JOURNAL).exists():return snapshot
        with app.store.read() as c,locked(self.directory):
            state=journal(self.directory);pair=state['pairs'].get(t.key)
            if not pair:return snapshot
            if snapshot.minus or 'excluded' in snapshot.queries.values():
                if pair['state']!='closed':pair.update(state='closed',closed_at=app.clock(),reason='nonempty_observed');save(self.directory,state)
                return snapshot
            # Historical evidence must not gate ordinary authoritative WB
            # snapshots after rules/profiles change. Bind only an actual use.
            if (pair['state']=='closed' or t.unsupported_reason or snapshot.reasons!=('minus_pair_omitted',) or not self.manual_intent(c,state['installations'][pair['installation']])
                    or app._settings(c)['enabled']):return snapshot
            own=self.operation if pair['state']=='claimed' else None
            if pair['state']=='claimed':
                if not own or any(pair.get(k)!=v for k,v in dict(operation_id=own,run_id=self.run_id,worker_token=self.token,generation=self.generation).items()):return snapshot
                operation=c.execute('SELECT * FROM cleaner_write_operations WHERE operation_id=? AND account=?',(own,app.key)).fetchone()
                if not operation or operation['worker_token']!=self.token or operation['run_id']!=self.run_id:return snapshot
            reason=history(c,app,self.directory,t.key,own)
            if reason:
                pair.update(state='closed',closed_at=app.clock(),reason=reason);save(self.directory,state)
                return snapshot
            body=state['installations'][pair['installation']]['candidate']
            bound(body,app,self.generation)
            proof=dict(evidence_id=body['evidence_id'],evidence_sha256=state['installations'][pair['installation']]['evidence_sha256'],installation=pair['installation'],target=t.key,owner_username=body['owner_username'],confirmed_at=body['confirmed_at'],basis='owner_confirmed_initial_empty')
            return replace(snapshot,complete=True,reasons=(),initial_empty_evidence=proof)

    def claim(self,c,operation,run_id,token,snapshot):
        app=self.cleaner;key=snapshot.target.key
        if run_id!=self.run_id or not self.manual_intent(c):fail('initial_empty_manual_intent_required')
        with locked(self.directory):
            state=journal(self.directory);pair=state['pairs'].get(key)
            if not pair or pair['state']!='available' or pair['installation']!=snapshot.initial_empty_evidence['installation']:fail('initial_empty_claim_lost')
            record=state['installations'][pair['installation']]
            bound(record['candidate'],app,self.generation)
            if not self.manual_intent(c,record) or snapshot.initial_empty_evidence['evidence_sha256']!=record['evidence_sha256']:fail('initial_empty_binding_mismatch')
            if history(c,app,self.directory,key,operation):fail('initial_empty_history_forbidden')
            pair.update(state='claimed',operation_id=operation,run_id=run_id,worker_token=token,generation=self.generation,claimed_at=app.clock())
            save(self.directory,state) # Durable before the operational COMMIT.
            self.operation,self.token=operation,token
