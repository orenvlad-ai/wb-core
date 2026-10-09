"""Native auto-complaint schedule source receipts, never a run executor."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from uuid import uuid4
from packages.application.operator_feedback_analysis_settings import NativePromptLock, encoded, digest, safe_path

DOMAIN = 'feedback_complaint_schedules'
LABEL = 'Расписание авто-жалоб'
PATH = '/v1/sheet-vitrina-v1/feedbacks/automation/schedules'
FILENAME = 'sheet_vitrina_v1_feedbacks_auto_complaints.json'
LEDGER = 'operator_schedule_receipts'
GENERATION = 'operator_schedule_generation'
FIELDS = ('id','enabled','local_time_hhmm','timezone','first_lookback_hours','overlap_hours','hard_cap_per_run')
MAX_RECEIPTS = 512
MAX_LEDGER_BYTES = 8 * 1024 * 1024
MAX_READ_BYTES = 64 * 1024 * 1024
IDENTITY = re.compile(r'complaint-schedules:[a-zA-Z0-9_-]{16,80}\Z')


class SourceRejected(ValueError):
    def __init__(self, code):
        self.code=code
        super().__init__(code)


def raw(path):
    safe_path(path)
    if path.name == FILENAME:
        from packages.application.operator_complaint_source_projection import read
        try:
            return read(path, max_projection_bytes=MAX_READ_BYTES)
        except ValueError as exc:
            if str(exc) == 'complaint_source_projection_projection_bound':
                raise ValueError('complaint_schedules_source_size_exceeded') from exc
            raise
    try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:return {}
    with os.fdopen(fd,'rb') as stream:data=stream.read(MAX_READ_BYTES+1)
    if len(data)>MAX_READ_BYTES:raise ValueError('complaint_schedules_source_size_exceeded')
    result=json.loads(data)
    if not isinstance(result,dict):raise ValueError('complaint_schedules_source_shape_invalid')
    return result


def intent(schedules):
    from packages.application.sheet_vitrina_v1_feedbacks_auto_complaints import _normalize_schedule
    from datetime import datetime, timezone
    # Native normalization without a newly generated ID or a runtime operand.
    fixed=datetime(2026,1,1,tzinfo=timezone.utc)
    result=[]
    for item in schedules:
        if not isinstance(item,dict) or not str(item.get('id') or '').strip():raise ValueError('complaint_schedules_source_id_missing')
        normalized=_normalize_schedule(item,now='2026-01-01T00:00:00Z',now_factory=lambda:fixed)
        result.append({key:normalized[key] for key in FIELDS})
    if len({item['id'] for item in result})!=len(result):raise ValueError('complaint_schedules_source_duplicate_id')
    return result


def material(value):
    return dict(schedules=intent(value.get('schedules',[])),**{GENERATION:value.get(GENERATION,'legacy')})


def revision(value):return digest(material(value))


def command(payload, *, actor, account, account_scope):
    from packages.application.sheet_vitrina_v1_feedbacks_auto_complaints import DEFAULT_HARD_CAP_PER_RUN
    keys={'operation_id','expected_source_revision','schedules','disable_schedule_id'}
    if set(payload)-keys:raise SourceRejected('complaint_schedules_command_invalid')
    identity=payload.get('operation_id');expected=payload.get('expected_source_revision')
    if not isinstance(identity,str) or not IDENTITY.fullmatch(identity):raise SourceRejected('complaint_schedules_identity_invalid')
    if not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected):raise SourceRejected('complaint_schedules_revision_required')
    if not actor or not account or not account_scope:raise SourceRejected('complaint_schedules_native_scope_required')
    if ('schedules' in payload)==('disable_schedule_id' in payload):raise SourceRejected('complaint_schedules_command_invalid')
    request={key:value for key,value in payload.items() if key!='operation_id'}
    if 'disable_schedule_id' in request:
        identity=request['disable_schedule_id']
        if not isinstance(identity,str) or not identity.strip() or len(identity)>80 or identity!=identity.strip():raise SourceRejected('complaint_schedules_command_invalid')
    else:
        rows=request['schedules']
        if not isinstance(rows,list) or len(rows)>64:raise SourceRejected('complaint_schedules_command_invalid')
        for row in rows:
            if not isinstance(row,dict) or set(row)!=set(FIELDS) or type(row.get('enabled')) is not bool:raise SourceRejected('complaint_schedules_command_invalid')
            if not isinstance(row['id'],str) or not row['id'].strip() or len(row['id'])>80 or row['id']!=row['id'].strip():raise SourceRejected('complaint_schedules_command_invalid')
            if not isinstance(row['local_time_hhmm'],str) or not isinstance(row['timezone'],str) or len(row['timezone'])>80:raise SourceRejected('complaint_schedules_command_invalid')
            for key,low,high in (('first_lookback_hours',1,168),('overlap_hours',0,72),('hard_cap_per_run',1,DEFAULT_HARD_CAP_PER_RUN)):
                if type(row[key]) is not int or not low<=row[key]<=high:raise SourceRejected('complaint_schedules_command_invalid')
        try:
            if intent(rows)!=rows:raise ValueError('normalization mismatch')
        except (ValueError,TypeError) as exc:raise SourceRejected('complaint_schedules_command_invalid') from exc
    if len(encoded(request))>80*1024:raise SourceRejected('complaint_schedules_command_size_exceeded')
    return dict(operation_id=payload['operation_id'],domain=DOMAIN,actor=actor,account=account,account_scope=account_scope,
                request=request,request_digest=digest(request))


def records(value):
    rows=value.get(LEDGER,[])
    if not isinstance(rows,list) or len(rows)>MAX_RECEIPTS or len(encoded(rows))>MAX_LEDGER_BYTES:raise ValueError('complaint_schedules_retained_proof_invalid')
    seen=set()
    for row in rows:
        if not isinstance(row,dict) or row.get('proof_digest')!=digest({k:v for k,v in row.items() if k!='proof_digest'}):raise ValueError('complaint_schedules_retained_proof_invalid')
        cmd=row.get('command',{})
        rebuilt=command(dict(cmd.get('request',{}),operation_id=cmd.get('operation_id')),actor=cmd.get('actor'),account=cmd.get('account'),account_scope=cmd.get('account_scope'))
        if cmd!=rebuilt or cmd['operation_id'] in seen:raise ValueError('complaint_schedules_retained_proof_invalid')
        before,after=row.get('before'),row.get('after')
        if not isinstance(before,dict) or not isinstance(after,dict) or row.get('before_revision')!=revision(before) or row.get('after_revision')!=revision(after) or cmd['request']['expected_source_revision']!=revision(before):raise ValueError('complaint_schedules_retained_proof_invalid')
        expected=before['schedules'] if 'disable_schedule_id' in cmd['request'] else cmd['request']['schedules']
        if 'disable_schedule_id' in cmd['request']:
            target=cmd['request']['disable_schedule_id']
            if target not in {item['id'] for item in expected}:raise ValueError('complaint_schedules_retained_proof_invalid')
            expected=[dict(item,enabled=False) if item['id']==target else item for item in expected]
        if after['schedules']!=expected or not row.get('accepted_at') or not after.get(GENERATION) or after.get(GENERATION)==before.get(GENERATION):raise ValueError('complaint_schedules_retained_proof_invalid')
        seen.add(cmd['operation_id'])
    return rows


def retained(value,cmd):
    for row in records(value):
        if row['command']['operation_id']==cmd['operation_id']:
            if row['command']!=cmd:raise SourceRejected('complaint_schedules_identity_conflict')
            return row
    return None


def prepare(previous,new,cmd=None, *, accepted_at):
    rows=records(previous);updated=dict(new,**{GENERATION:uuid4().hex,LEDGER:rows})
    if cmd:
        if cmd['request']['expected_source_revision']!=revision(previous):raise SourceRejected('complaint_schedules_revision_stale')
        row=dict(command=cmd,accepted_at=accepted_at,before=material(previous),after=material(updated),
                 before_revision=revision(previous),after_revision=revision(updated))
        row['proof_digest']=digest(row);updated[LEDGER]=[*rows,row]
        if len(updated[LEDGER])>MAX_RECEIPTS or len(encoded(updated[LEDGER]))>MAX_LEDGER_BYTES:raise SourceRejected('complaint_schedules_capacity_exceeded')
        records(updated)
    return updated


def atomic_write(path,value):
    # Native run lifecycle still owns its existing retention/bounds. No new
    # whole-file capacity gate may strand terminal proof AFTER an external write.
    safe_path(path);data=encoded(value)+b'\n';temp=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:temp.unlink(missing_ok=True)


def public(row):
    cmd=row['command']
    return dict(contract_name='operator_operations_v1',operation_id=cmd['operation_id'],domain=DOMAIN,title_ru=LABEL,
                actor=cmd['actor'],accepted_at=row['accepted_at'],native_state='source_saved',state='completed',
                primary_effect='source_saved',durable_saved=True,execution_required=False,external_confirmed=False,
                calculation_completed=False,resubmit_allowed=False,request=cmd['request'],saved_source=row['after'],
                reason_ru='Расписание сохранено. Это не подтверждает запуск или отправку жалоб WB.',fields=[],
                source_ref=dict(entity_id=cmd['operation_id'],filename=FILENAME,account=cmd['account'],account_scope=cmd['account_scope'],
                                before_revision=row['before_revision'],after_revision=row['after_revision'],proof_digest=row['proof_digest']))


@dataclass(frozen=True)
class ScheduleScope:
    runtime_dir:Path
    actor:str
    account:str
    account_scope:str
    @classmethod
    def from_entrypoint(cls,app, *, actor):
        runtime=Path(app.runtime.runtime_dir).resolve();owner=app.feedbacks_auto_complaints_block.store;surface=app.change_registry_read_surface
        if surface is None:raise SourceRejected('complaint_schedules_native_scope_required')
        if owner.path.resolve()!=runtime/FILENAME or Path(surface.runtime_dir).resolve()!=runtime:raise ValueError('complaint_schedules_native_binding_invalid')
        return cls(runtime,actor,str(surface.seller_id or ''),str(surface.account_scope or ''))


def items(*,selected,scope):
    if DOMAIN not in selected or not isinstance(scope,ScheduleScope) or not scope.actor or not scope.account:return []
    value=raw(scope.runtime_dir.resolve()/FILENAME);rows=value.get(LEDGER,[])
    if not isinstance(rows,list):raise ValueError('complaint_schedules_retained_proof_invalid')
    own=[row for row in rows if isinstance(row,dict) and isinstance(row.get('command'),dict) and row['command'].get('actor')==scope.actor and row['command'].get('account')==scope.account and row['command'].get('account_scope')==scope.account_scope]
    return [public(row) for row in records({LEDGER:own})]
