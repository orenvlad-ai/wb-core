"""Read-only receipts for commands retained in native SPP job files.

No second queue, reconciliation, provider call, or worker is introduced here.
"""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

DOMAIN = 'spp_test_jobs'
LABEL = 'СПП: задания проверки'
PATH = '/v1/sheet-vitrina-v1/prices/spp-test/status'
IDENTITY = re.compile(r'spp-start:[A-Za-z0-9_-]{16,80}\Z')
MAX_JOBS = 10000
MAX_JOB_BYTES = 8 * 1024 * 1024


class Rejected(ValueError):
    """Only a proven precommit rejection, never a retained-file read error."""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def job_id(request_id, scope):
    basis=dict(request_id=request_id,**{k:getattr(scope,k) for k in ('native_actor','actor','seller_id','account_scope')})
    return 'op_' + digest(basis)


@dataclass(frozen=True)
class SppScope:
    runtime_dir: Path
    db_path: Path
    native_actor: str
    actor: str
    seller_id: str
    account_scope: str

    @classmethod
    def from_entrypoint(cls, app, *, native_actor, actor):
        if not native_actor or not actor:return None
        owner, surface = app.spp_tester_block, app.change_registry_read_surface
        if not surface or not surface.seller_id:
            return None
        if (Path(owner.runtime_dir).resolve() != Path(app.runtime.runtime_dir).resolve() or
            Path(surface.store_registry.resolve('operational')).resolve() != Path(app.runtime.db_path).resolve() or
            surface.account_scope != 'seller-portal-primary'):
            raise ValueError('spp_native_binding_invalid')
        writer = owner.writer_registry
        if writer and (Path(writer.repository.runtime_dir).resolve() != Path(app.runtime.runtime_dir).resolve() or
                       writer.seller_id != surface.seller_id or writer.account_scope != surface.account_scope):
            raise ValueError('spp_native_binding_invalid')
        return cls(Path(app.runtime.runtime_dir).resolve(), Path(app.runtime.db_path).resolve(),
                   native_actor, actor, surface.seller_id, surface.account_scope)


def command(payload, *, scope):
    if not isinstance(scope, SppScope) or not scope.actor or not scope.native_actor or not scope.seller_id or scope.account_scope != 'seller-portal-primary':
        raise Rejected('spp_start_scope_required')
    keys = {'request_id','nmID','price_count','prices','confirm_live_price_change','restore_baseline'}
    if set(payload) != keys or not isinstance(payload.get('request_id'), str) or not IDENTITY.fullmatch(payload['request_id']):
        raise Rejected('spp_start_command_invalid')
    prices = payload['prices']
    if type(payload['nmID']) is not int or payload['nmID'] <= 0 or type(payload['price_count']) is not int or not isinstance(prices, list) or not 1 <= len(prices) <= 6 or payload['price_count'] != len(prices):
        raise Rejected('spp_start_command_invalid')
    if payload['confirm_live_price_change'] is not True or payload['restore_baseline'] is not True:
        raise Rejected('spp_start_command_invalid')
    for value in prices:
        if type(value) not in (int,float) or not Decimal(str(value)).is_finite() or Decimal(str(value)) <= 0 or Decimal(str(value)).as_tuple().exponent < -2:
            raise Rejected('spp_start_command_invalid')
    request = {k:payload[k] for k in keys if k != 'request_id'}
    if len(encoded(request)) > 4096:
        raise Rejected('spp_start_command_invalid')
    return dict(request_id=payload['request_id'], native_actor=scope.native_actor, actor=scope.actor,
        seller_id=scope.seller_id, account_scope=scope.account_scope, request=request, request_digest=digest(request))


def proof(cmd, *, job_id, accepted_at):
    result = dict(command=cmd, job_id=job_id, accepted_at=accepted_at)
    return dict(result, proof_digest=digest(result))


def preflight_proof(job):
    value=dict(acceptance_digest=job['operator_acceptance']['proof_digest'], baseline=job['baseline'],
               measurement_plan=job['input']['measurement_plan'])
    return dict(value,proof_digest=digest(value))


def visible(job, scope):
    if not isinstance(scope, SppScope):return False
    cmd = (job.get('operator_acceptance') or {}).get('command') or {}
    return all(cmd.get(k) == getattr(scope,k) for k in ('native_actor','actor','seller_id','account_scope'))


def verify(job):
    try:
        p = job['operator_acceptance']; cmd = p['command']
        scope = SppScope(Path('.'),Path('.'),cmd['native_actor'],cmd['actor'],cmd['seller_id'],cmd['account_scope'])
        rebuilt = command(dict(cmd['request'],request_id=cmd['request_id']),scope=scope)
        if cmd != rebuilt or p != proof(cmd,job_id=job_id(cmd['request_id'],scope),accepted_at=job['created_at']):
            raise ValueError()
        if job['job_id'] != p['job_id'] or job['actor'] != cmd['native_actor'] or job['nmID'] != cmd['request']['nmID']:
            raise ValueError()
        inp=job['input']
        if inp['target_prices'] != cmd['request']['prices'] or inp['price_count'] != cmd['request']['price_count'] or inp['restore_baseline'] is not True:
            raise ValueError()
        if job.get('baseline') and job.get('operator_preflight') != preflight_proof(job):
            raise ValueError()
        return p
    except (ValueError,TypeError,KeyError) as exc:
        raise ValueError('spp_retained_proof_invalid') from exc


def raw_job(path):
    try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:return None
    with os.fdopen(fd,'rb') as stream:data=stream.read(MAX_JOB_BYTES+1)
    if len(data)>MAX_JOB_BYTES:raise ValueError('spp_retained_job_size_exceeded')
    value=json.loads(data)
    if not isinstance(value,dict):raise ValueError('spp_retained_job_shape_invalid')
    return value


def jobs_dir(scope):
    return scope.runtime_dir / 'sheet_vitrina_v1_prices' / 'spp_tests' / 'jobs'


def read_job(scope, request_id):
    if not isinstance(request_id,str) or not IDENTITY.fullmatch(request_id):raise ValueError('spp_request_identity_invalid')
    if not isinstance(scope,SppScope):return None
    job=raw_job(jobs_dir(scope)/(job_id(request_id,scope)+'.json'))
    if not job or not visible(job,scope):return None
    verify(job)
    if job['operator_acceptance']['command']['request_id'] != request_id:raise ValueError('spp_retained_proof_invalid')
    return job


def external_proof(job, scope):
    """Exact native registry children, not job state booleans, prove WB effects."""
    from packages.application.operator_ff_overhead import readonly
    from packages.application.change_registry import OPERATIONS_TABLE, ITEMS_TABLE, ATTEMPT_EVENTS_TABLE
    from packages.application.change_registry_writer import price_tuple_from_wb
    # The retained command/preflight defines every required point. Outcome
    # flags may not silently shrink that set to the uploads which happened.
    cmd=job['operator_acceptance']['command']['request']
    plan=(job.get('operator_preflight') or {}).get('measurement_plan') or []
    rows=job.get('measurements') or []
    if len(plan)!=cmd['price_count'] or len(rows)!=len(plan):return False
    stages={};point_ids=set()
    for index,(point,row,price) in enumerate(zip(plan,rows,cmd['prices'])):
        point_id=row.get('point_id')
        guard=(row.get('evidence') or {}).get('prewrite_guard') or {}
        if (point.get('index')!=index+1 or point.get('target_discounted_price')!=price or
            any(row.get(k)!=point.get(k) for k in ('target_discounted_price','upload_price','expected_discounted_price')) or
            not isinstance(point_id,str) or not point_id or point_id in point_ids or
            guard.get('safe') is not True or not row.get('uploadID')):return False
        point_ids.add(point_id)
        expected=dict(price=point['upload_price'],discount=job['baseline']['discount'],discountedPrice=point['expected_discounted_price'])
        previous=job['baseline'] if index==0 else dict(price=plan[index-1]['upload_price'],discount=job['baseline']['discount'],discountedPrice=plan[index-1]['expected_discounted_price'])
        stages['measurement:'+point_id]=(previous,expected)
    # Native restore may need no write when the final measurement already is
    # the baseline. Otherwise its final baseline write cannot be omitted.
    baseline={k:job['baseline'][k] for k in ('price','discount','discountedPrice')}
    steps=(job.get('restore') or {}).get('steps') or []
    if not steps and expected!=baseline:return False
    for index,row in enumerate(steps,start=1):
        if not (row.get('upload') or {}).get('uploadID'):return False
        after=dict(price=row['price'],discount=row['discount'],discountedPrice=row['expected_discounted_price'])
        # Bind every recorded restore step to its exact native registry child;
        # an unsuccessful/blocked step does not disappear from required proof.
        stages[f"restore:{row.get('kind') or 'step'}:{index}"]=((row.get('prewrite_guard') or {}).get('current'),after)
        if index==len(steps) and (row.get('kind')!='baseline' or after!=baseline):return False
    with closing(readonly(scope.db_path)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(OPERATIONS_TABLE,)).fetchone():return False
        ops=conn.execute(f'SELECT * FROM {OPERATIONS_TABLE} WHERE correlation_id=? AND source_surface=?',(job['job_id'],'spp_tester')).fetchall()
        if len(ops)!=len(stages):return False
        remaining=set(stages)
        for op in ops:
            stage=op['native_idempotency_key'].removeprefix(job['job_id']+':')
            if not op['native_idempotency_key'].startswith(job['job_id']+':') or stage not in remaining or any(op[k]!=v for k,v in dict(actor_principal=scope.native_actor,seller_id=scope.seller_id,account_scope=scope.account_scope,apply_operation_id=job['job_id']).items()):return False
            remaining.remove(stage)
            before,after=stages[stage]
            if not before:return False
            requested=price_tuple_from_wb(price=after['price'],discount=after['discount'],seller_price=after['discountedPrice'])
            previous=price_tuple_from_wb(price=before['price'],discount=before['discount'],seller_price=before['discountedPrice']) if before else None
            children=conn.execute(f'SELECT * FROM {ITEMS_TABLE} WHERE operation_id=?',(op['operation_id'],)).fetchall()
            expected_fields={'original_price_minor','discount_bps','seller_price_minor'}
            if {c['parameter_field'] for c in children} != expected_fields or len(children)!=len(expected_fields):return False
            for child in children:
                field=child['parameter_field']
                if child['target_kind']!='price' or child['seller_id']!=scope.seller_id or child['account_scope']!=scope.account_scope or child['advert_id']!=0 or child['placement']!='' or child['nm_id']!=job['nmID'] or child['requested_value_kind']!='integer' or child['requested_value_integer']!=requested[field]:return False
                if previous and (child['before_value_kind']!='integer' or child['before_value_integer']!=previous[field]):return False
                event=conn.execute(f'SELECT * FROM {ATTEMPT_EVENTS_TABLE} WHERE change_item_id=? ORDER BY sequence_no DESC LIMIT 1',(child['change_item_id'],)).fetchone()
                if not event or not (event['state']=='confirmed' or event['state']=='resolved' and event['resolution_state']=='confirmed') or event['readback_proof_kind']!='wb_readback' or not event['readback_digest'] or event['receipt_reference']!='wb-spp:'+job['job_id']+':'+stage:return False
                if moment(event['occurred_at']) > moment(job['restore']['proof']['captured_at']):return False
    return True


def inactive_runner(job, scope):
    from packages.contracts.wb_spp_tester import SPP_TEST_ACTIVE_STATUSES
    if job.get('status') not in SPP_TEST_ACTIVE_STATUSES:return False
    path=scope.runtime_dir/'sheet_vitrina_v1_prices'/'spp_tests'/'execution.lock'
    try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:return False
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return False
        current=read_job(scope,job['operator_acceptance']['command']['request_id'])
        return bool(current and current.get('status')==job.get('status') and bool(current.get('baseline'))==bool(job.get('baseline')))
    finally:os.close(fd)


def public_error(job):
    value=str(job.get('error') or '').replace('\n',' ').replace('\r',' ')[:500]
    if any(marker in value.casefold() for marker in ('authorization','cookie','token','secret','password','/opt/','/srv/','/users/','http://','https://')):
        return 'Проверка требует внимания. Внешний результат проверяется в исходном задании.'
    return value


def moment(value):
    return datetime.fromisoformat(str(value).replace('Z','+00:00'))


def restore_verified(job):
    restore=job.get('restore') or {};proof=restore.get('proof') or {};baseline=job.get('baseline') or {}
    try:
        return bool(restore.get('restored') is True and proof.get('proof_status')=='confirmed' and
            all(proof.get(key) is True for key in ('price_matches','discount_matches','discountedPrice_matches','quarantine_absent')) and
            moment(proof['captured_at']) >= moment(job['created_at']) and
            all(Decimal(str(proof['seller_tuple'][key]))==Decimal(str(baseline[key])) for key in ('price','discount','discountedPrice')))
    except (KeyError,ValueError,TypeError,InvalidOperation):return False


def public(job, *, scope):
    p=verify(job);cmd=p['command'];status=str(job.get('status') or '')
    restore=job.get('restore') or {};rows=job.get('measurements') or []
    complete=(status=='complete' and job.get('result_status')=='success' and
        len(rows)==cmd['request']['price_count'] and all(row.get('status')=='ok' and row.get('target_discounted_price')==price for row,price in zip(rows,cmd['request']['prices'])) and
        restore_verified(job) and
        external_proof(job,scope))
    inactive=inactive_runner(job,scope)
    interrupted=inactive and not job.get('baseline')
    if interrupted:state,reason='needs_attention','Задание сохранено, но проверка до изменения цен была прервана. Повторный запуск автоматически не выполняется.'
    elif inactive:state,reason='needs_attention','Проверка прервана. Требуется проверить восстановление исходной цены.'
    elif complete:state,reason='completed','Проверка завершена. Изменения и восстановление цены подтверждены WB.'
    elif status in {'failed','preflight_rejected','manual_restore_required','interrupted_restored','complete'}:state,reason='needs_attention',public_error(job) or 'Результат проверки требует внимания. Повторная отправка не выполняется.'
    else:state,reason='processing','Задание сохранено. Проверка и восстановление цены ещё не подтверждены.'
    result=dict(contract_name='operator_operations_v1',operation_id=cmd['request_id'],domain=DOMAIN,title_ru=LABEL,
        durable_saved=True,primary_effect='native_job',state=state,native_state=status,accepted_at=p['accepted_at'],actor=cmd['actor'],
        source_ref=dict(domain=DOMAIN,entity_id=job['job_id'],revision=p['proof_digest'],action='start',
            request_digest=cmd['request_digest'],seller_id=cmd['seller_id'],account_scope=cmd['account_scope']),
        reason_ru=reason,external_confirmed=complete,interrupted_before_write=interrupted,recovery_restore_available=bool(inactive and job.get('baseline')),restoration_confirmed=restore_verified(job),
        resubmit_allowed=False,retry_owner='native_spp_worker',
        fields=[dict(label='Товар WB',value=str(cmd['request']['nmID'])),dict(label='Цен проверить',value=str(cmd['request']['price_count']))])
    from packages.application.operator_operations import _common
    return _common(result)


def scope_key(scope):
    return digest({k:getattr(scope,k) for k in ('native_actor','actor','seller_id','account_scope')}) if isinstance(scope,SppScope) else ''


def response(job, *, scope, recovered):
    from packages.application.wb_spp_tester import WbSppTesterBlock
    acceptance=public(job,scope=scope)
    native=WbSppTesterBlock._job_public_payload(job)
    native['operator_verified_complete']=acceptance['state']=='completed'
    native['error']=public_error(job)
    for row in native.get('measurements',[]):row['registry_confirmed']=acceptance['external_confirmed']
    return dict(contract_name='sheet_vitrina_v1_spp_test_start',request_id=job['operator_acceptance']['command']['request_id'],
        job=native,acceptance=acceptance,recovered=recovered)


def items(*, selected, scope, db_path=None):
    if DOMAIN not in selected or not isinstance(scope,SppScope):return []
    if db_path is not None and Path(db_path).resolve()!=scope.db_path.resolve():raise ValueError('spp_native_store_binding_invalid')
    path=jobs_dir(scope)
    if not path.exists():return []
    result=[]
    for file in path.glob('op_*.json'):
        if not re.fullmatch('op_[a-f0-9]{64}',file.stem):continue
        job=raw_job(file)
        if job and visible(job,scope):result.append(public(job,scope=scope))
    return result


def read(*, identity, selected, scope, db_path=None):
    if DOMAIN not in selected or not IDENTITY.fullmatch(str(identity)):return None
    if isinstance(scope,SppScope) and db_path is not None and Path(db_path).resolve()!=scope.db_path.resolve():raise ValueError('spp_native_store_binding_invalid')
    job=read_job(scope,identity)
    return public(job,scope=scope) if job else None
