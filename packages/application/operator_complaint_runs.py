"""Exact manual admission/readback over the native auto-complaint run store.

No provider, schedule executor or second queue. Automatic runs have no manual
principal receipt. A completed run never means that WB accepted every complaint.
"""
import json,re
from dataclasses import dataclass
from pathlib import Path
from packages.application import operator_feedback_complaint_schedules as schedules
from packages.application.operator_feedback_analysis_settings import encoded,digest
DOMAIN='feedback_complaint_run'
META='operator_manual_command'
PATH='/v1/sheet-vitrina-v1/feedbacks/automation/run-now'
COMPLETION_RESERVE=8*1024*1024
MAX_BYTES=64*1024*1024
# The existing typed schedule owner can grow its immutable receipt ledger to
# 8MiB; reserve 512KiB for 64 normalized runtime schedules including
# native terminal stats (14 bounded JSON numbers and a 200-character reason).
CONFIG_METADATA_CAPACITY=512*1024
CONFIG_CAPACITY=schedules.MAX_LEDGER_BYTES+CONFIG_METADATA_CAPACITY
IDENTITY=re.compile(r'complaint-run:[a-f0-9]{32}\Z')
OPERANDS=('schedule_id','trigger_source','due_at','timezone','window_base_from','window_fetch_from','window_to','overlap_hours','hard_cap_per_run')
ACTIVE={'queued','running'}
SUCCESS={'completed','no_new_feedbacks','no_low_rating_feedbacks','no_ai_candidates','hard_cap_reached'}

class NotSaved(ValueError):
    def __init__(self,code):
        self.code=code
        message={'complaint_run_native_busy':'Другой запуск ещё выполняется. Новый запуск не сохранён.',
            'complaint_run_source_revision_stale':'Расписание изменилось. Обновите его перед новым запуском.',
            'complaint_run_schedule_missing':'Выбранное расписание больше не существует. Запуск не сохранён.',
            'complaint_run_capacity_exceeded':'Хранилище запусков заполнено. Новый запуск не сохранён.',
            'complaint_run_identity_conflict':'Этот запрос относится к другому действию или пользователю.'}.get(code,'Запуск не сохранён: параметры или источник запроса не подтверждены.')
        super().__init__(message)

def command(payload,scope):
    if not isinstance(payload,dict) or set(payload)!={'operation_id','schedule_id','expected_source_revision'}:raise NotSaved('complaint_run_command_invalid')
    identity=payload['operation_id'];schedule=payload['schedule_id'];revision=payload['expected_source_revision']
    if not isinstance(identity,str) or not IDENTITY.fullmatch(identity):raise NotSaved('complaint_run_identity_invalid')
    if not isinstance(schedule,str) or not schedule.strip() or schedule!=schedule.strip() or len(schedule)>80:raise NotSaved('complaint_run_schedule_required')
    if not isinstance(revision,str) or not re.fullmatch('[a-f0-9]{64}',revision):raise NotSaved('complaint_run_source_revision_required')
    if not scope.actor or not scope.account or not scope.account_scope:raise NotSaved('complaint_run_scope_required')
    return dict(operation_id=identity,actor=scope.actor,account=scope.account,account_scope=scope.account_scope,
        request={k:v for k,v in payload.items() if k!='operation_id'})

@dataclass(frozen=True)
class RunScope:
    runtime_dir:Path
    actor:str
    account:str
    account_scope:str
    @classmethod
    def from_entrypoint(cls,app,*,actor):
        if app.change_registry_read_surface is None:raise NotSaved('complaint_run_scope_required')
        scope=schedules.ScheduleScope.from_entrypoint(app,actor=actor)
        return cls(scope.runtime_dir,scope.actor,scope.account,scope.account_scope)

def retain(runs):
    manual=[r for r in runs if r.get(META)]
    legacy=[r for r in runs if not r.get(META)][-200:]
    keep={id(r) for r in manual+legacy}
    return [r for r in runs if id(r) in keep]

def reserve(payload):
    # Automatic outcomes retain their original native representation. Only
    # operator-retained evidence consumes the bounded RO projection budget.
    projection={**payload,'runs':[r for r in payload.get('runs',[]) if META in r]}
    config_size=len(encoded({key:payload.get(key,default) for key,default in (('schedules',[]),(schedules.LEDGER,[]))}))
    config_reserve=max(0,CONFIG_CAPACITY-config_size)
    if len(encoded(projection))+config_reserve+sum(max(0,COMPLETION_RESERVE-len(encoded(r))) for r in payload.get('runs',[]) if r.get(META) and r.get('status') in ACTIVE)+COMPLETION_RESERVE>MAX_BYTES:
        raise NotSaved('complaint_run_capacity_exceeded')

def native_id(cmd):return 'auto_complaints_operator_'+cmd['operation_id'].split(':',1)[1]

def attach(run,cmd,schedule):
    material={'command':cmd,'schedule':schedule,'run_id':run['run_id'],'accepted_at':run['created_at'],
        'operands':{k:run.get(k) for k in OPERANDS}}
    return {**run,META:{**material,'proof_digest':digest(material)},'operator_completion_capacity':COMPLETION_RESERVE}

def normalize(run,normalized):
    if not run.get(META):return normalized
    normalized[META]=run[META]
    normalized['operator_completion_capacity']=run.get('operator_completion_capacity')
    if 'operator_native_terminal' in run:normalized['operator_native_terminal']=run['operator_native_terminal']
    if run.get('operator_terminal_compact'):
        normalized['operator_terminal_compact']=run['operator_terminal_compact']
        normalized['operator_full_result_digest']=run.get('operator_full_result_digest')
        return compact(normalized)
    # Bound diagnostic expansion before the source is admitted. Native row
    # business/submission decisions are unchanged; the parent keeps bounded
    # diagnostics and exact digests, while native submit reports/journal own WB.
    normalized['evidence_refs']=[str(v)[:260] for v in normalized.get('evidence_refs',[])[:200]]
    for attempt in normalized['attempts']:
        attempt['evidence_refs']=[str(v)[:260] for v in attempt.get('evidence_refs',[])[:20]]
    normalized['reason_counts']={str(k)[:240]:max(0,min(int(v),2**63-1)) for k,v in list(normalized['reason_counts'].items())[:200]}
    for key in ('status_sync_result','automation_lock','session'):
        if len(encoded(normalized.get(key,{})))>65536:
            normalized[key]={'diagnostics_digest':digest(normalized[key]),'diagnostics_omitted':True}
    for key,value in list(normalized.items()):
        if isinstance(value,int) and not isinstance(value,bool):normalized[key]=max(0,min(value,2**63-1))
    if len(encoded(normalized))>COMPLETION_RESERVE:raise ValueError('complaint_run_normalization_exceeds_reserved_capacity')
    return normalized

def validate(run):
    meta=run.get(META)
    if not isinstance(meta,dict):raise ValueError('complaint_run_native_proof_missing')
    material={k:v for k,v in meta.items() if k!='proof_digest'}
    cmd=meta.get('command') or {}
    scope=RunScope(Path('.'),cmd.get('actor'),cmd.get('account'),cmd.get('account_scope'))
    try:rebuilt=command({**cmd.get('request',{}),'operation_id':cmd.get('operation_id')},scope)
    except (NotSaved,TypeError) as exc:raise ValueError('complaint_run_native_proof_invalid') from exc
    if (cmd!=rebuilt or meta.get('proof_digest')!=digest(material) or run.get('run_id')!=native_id(cmd)
        or meta.get('run_id')!=run['run_id'] or meta.get('accepted_at')!=run['created_at']
        or meta.get('operands')!={k:run.get(k) for k in OPERANDS}
        or meta.get('schedule',{}).get('id')!=run.get('schedule_id') or run.get('trigger_source')!='manual'
        or run.get('operator_completion_capacity')!=COMPLETION_RESERVE):raise ValueError('complaint_run_native_proof_invalid')
    return cmd

def prior(payload,cmd):
    matches=[r for r in payload.get('runs',[]) if (r.get(META) or {}).get('command',{}).get('operation_id')==cmd['operation_id']]
    if len(matches)>1:raise ValueError('complaint_run_identity_ambiguous')
    if matches:
        if validate(matches[0])!=cmd:raise NotSaved('complaint_run_identity_conflict')
        return matches[0]
    return None

def compact(run):
    # Preserve native dedup/business decisions and exact job/child references;
    # verbose portal diagnostics remain in the existing native job report.
    result=dict(run)
    for key in ('status_sync_result','automation_lock','session','reason_counts'):
        result[key]={}
    result['events']=[]
    result['evidence_refs']=[run['run_id']+'_submit'] if run.get('submit_attempted_count') else []
    result['attempts']=[{key:attempt.get(key,'') for key in
        ('feedback_id','action')}
        for attempt in run.get('attempts',[])]
    return result

def terminal_material(run):
    # Retained native worker afterimage, not a current schedule/queue flag or WB
    # response. Binds immutable parent to the result of this exact native job.
    return {k:v for k,v in run.items() if k!='operator_native_terminal'}

def finish(run):
    validate(run)
    if run.get('status') in ACTIVE or not run.get('finished_at'):return run
    full=digest(terminal_material(run))
    run=compact({**run,'operator_terminal_compact':True,'operator_full_result_digest':full})
    return {**run,'operator_native_terminal':{'run_id':run['run_id'],
        'command_digest':run[META]['proof_digest'],'result_digest':digest(terminal_material(run))}}

def terminal_valid(run):
    proof=run.get('operator_native_terminal')
    return (isinstance(proof,dict) and proof=={'run_id':run['run_id'],
        'command_digest':run[META]['proof_digest'],'result_digest':digest(terminal_material(run))}
        and run.get('status') not in ACTIVE and bool(run.get('finished_at')))

def retained_terminal(run,runtime_dir):
    if not terminal_valid(run) or runtime_dir is None:return None
    report_path=Path(runtime_dir).resolve()/'feedbacks_auto_complaints'/run['run_id']/'sheet_vitrina_v1_feedbacks_auto_complaints_run.json'
    try:original=schedules.raw(report_path)
    except (OSError,ValueError,TypeError):return None
    if original.get('operator_terminal_compact'):return None
    if digest(terminal_material(original))!=run.get('operator_full_result_digest'):return None
    expected=compact({**original,'operator_terminal_compact':True,'operator_full_result_digest':run['operator_full_result_digest']})
    return original if terminal_material(run)==expected else None

def retained_terminal_valid(run,runtime_dir):return retained_terminal(run,runtime_dir) is not None

def public(run, runtime_dir=None):
    cmd=validate(run);status=run['status'];terminal=retained_terminal_valid(run,runtime_dir)
    if status in ACTIVE:state,reason='processing','Запуск сохранён. Ожидает завершения обработки.'
    elif terminal and status in SUCCESS:state,reason='completed','Запуск завершён. Результаты жалоб проверяются отдельно.'
    else:state,reason='needs_attention','Запуск сохранён. Его обработка требует внимания. Повторная отправка не выполняется.'
    meta=run[META]
    receipt=dict(contract_name='operator_operations_v1',operation_id=cmd['operation_id'],domain=DOMAIN,title_ru='Ручной запуск авто-жалоб',
        accepted_at=meta['accepted_at'],actor=cmd['actor'],durable_saved=True,state=state,primary_effect='native_job',
        native_state=status,external_confirmed=False,execution_required=True,resubmit_allowed=False,reason_ru=reason,request_digest=digest([cmd['request']['schedule_id'],cmd['request']['expected_source_revision']]),
        source_ref=dict(entity_id=run['run_id'],schedule_id=run['schedule_id'],account=cmd['account'],account_scope=cmd['account_scope'],proof_digest=meta['proof_digest']),
        processing=dict(native_job_terminal=terminal,submitted_count=run.get('submitted_count',0),submit_confirmed_count=run.get('submit_confirmed_count',0)),
        fields=[dict(label='Запуск',value=run['run_id'])],detail_path='/sheet-vitrina-v1/operations?operation_id='+cmd['operation_id'])
    return dict(domain=DOMAIN,operation_id=cmd['operation_id'],status='accepted',settled=True,acceptance=receipt,run_id=run['run_id'])

def items(*,selected,scope):
    if DOMAIN not in selected or not isinstance(scope,RunScope):return []
    value=schedules.raw(scope.runtime_dir.resolve()/schedules.FILENAME)
    result=[]
    for run in value.get('runs',[]):
        cmd=(run.get(META) or {}).get('command',{})
        if (cmd.get('actor'),cmd.get('account'),cmd.get('account_scope'))!=(scope.actor,scope.account,scope.account_scope):continue
        result.append(public(run,scope.runtime_dir)['acceptance'])
    return result

def read(identity,scope):
    unknown=dict(domain=DOMAIN,operation_id=identity,status='unknown',settled=False,acceptance=None)
    if not isinstance(identity,str) or not IDENTITY.fullmatch(identity):return unknown
    value=schedules.raw(scope.runtime_dir.resolve()/schedules.FILENAME)
    found=[]
    for run in value.get('runs',[]):
        cmd=(run.get(META) or {}).get('command',{})
        if (cmd.get('actor'),cmd.get('account'),cmd.get('account_scope'),cmd.get('operation_id'))==(scope.actor,scope.account,scope.account_scope,identity):found.append(run)
    if not found:return unknown
    if len(found)!=1:raise ValueError('complaint_run_identity_ambiguous')
    return public(found[0],scope.runtime_dir)
