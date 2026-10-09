"""Immutable native AI settings commands and query-only source projection.

The owner's existing settings transaction commits this audit event. Reading it
never constructs a repository, reconciles lifecycle, starts a worker or sends WB.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from packages.application.change_registry_observer import DEFAULT_ACCOUNT_SCOPE

DOMAIN = 'autoanswers_settings'
LABEL = 'AI: режим и лимиты'
AUDIT = 'sheet_vitrina_v1_wb_autoanswers_audit_events'
ALIAS = 'operator_ai_settings'
TABLE = "(SELECT event_id operation_id,created_at accepted_at,* FROM " + ALIAS + '.' + AUDIT + ')'
PATH = '/v1/sheet-vitrina-v1/feedbacks/autoanswers/settings'
CHANGE_FIELDS = frozenset(('selector_state','master_enabled','mode','daily_cap_usd','monthly_cap_usd','hourly_cap_usd',
    'max_paid_reviews_per_hour','global_paid_review_concurrency','max_inflight_role_calls',
    'max_materialized_processing_jobs','warning_ratio'))
LIMIT_FIELDS = CHANGE_FIELDS-{'selector_state','master_enabled','mode','warning_ratio'}
FIELDS = CHANGE_FIELDS | {'expected_policy_epoch','expected_settings_revision','preview_id'}
ID_RE = re.compile(r'ai-settings:[a-zA-Z0-9_-]{16,80}\Z')


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def command(payload, *, actor, account, account_scope=DEFAULT_ACCOUNT_SCOPE):
    """HTTP adapter supplies actual principal and configured account, never body scope."""
    identity=payload.get('operation_id')
    if identity is None:return None
    if not isinstance(identity,str) or not ID_RE.fullmatch(identity) or not actor or not account or not account_scope:
        raise ValueError('operator_ai_settings_identity_invalid')
    request={key:value for key,value in payload.items() if key!='operation_id'}
    if set(request)-FIELDS or not CHANGE_FIELDS.intersection(request):
        raise ValueError('operator_ai_settings_operands_invalid')
    if type(request.get('expected_policy_epoch')) is not int or request['expected_policy_epoch']<0:
        raise ValueError('operator_ai_settings_epoch_required')
    if 'master_enabled' in request and type(request['master_enabled']) is not bool:raise ValueError('operator_ai_settings_boolean_required')
    if LIMIT_FIELDS.intersection(request) and (not isinstance(request.get('expected_settings_revision'),str) or not request['expected_settings_revision']):
        raise ValueError('operator_ai_settings_revision_required')
    encoded=canonical(request)
    if len(encoded.encode('utf-8'))>8192:raise ValueError('operator_ai_settings_operands_too_large')
    return dict(operation_id=identity,actor=str(actor),account=str(account),account_scope=str(account_scope),request=request,
        digest=hashlib.sha256(encoded.encode()).hexdigest())


def verify_command(value, *, actor, operands, target=None):
    if value is None:return
    rebuilt=command(dict(value['request'],operation_id=value['operation_id']),actor=actor,account=value['account'],account_scope=value['account_scope'])
    if rebuilt!=value:raise ValueError('operator_ai_settings_command_mismatch')
    request=value['request']
    if target is not None:
        if request.get('selector_state')!=target:raise ValueError('operator_ai_settings_target_mismatch')
        if (CHANGE_FIELDS-{'selector_state'}).intersection(request):raise ValueError('operator_ai_settings_transition_operands_invalid')
        expected={'expected_policy_epoch':request['expected_policy_epoch'],'preview_id':request.get('preview_id')}
    else:
        expected={key:val for key,val in request.items() if key in CHANGE_FIELDS or key in {'expected_policy_epoch','expected_settings_revision'}}
        selector=expected.pop('selector_state',None)
        if selector:
            expected['master_enabled']=selector!='off'
            if selector!='off':expected['mode']=selector
        if not LIMIT_FIELDS.intersection(request):expected.pop('expected_settings_revision',None)
        expected={key:val for key,val in expected.items() if val is not None}
    if any(operands.get(key)!=val for key,val in expected.items()):
        raise ValueError('operator_ai_settings_native_operand_mismatch')


def retained(conn, value):
    if value is None:return None
    row=conn.execute('SELECT * FROM '+AUDIT+' WHERE event_id=?',(value['operation_id'],)).fetchone()
    if not row:return None
    proof=json.loads(row['details_json'])
    if row['aggregate_type']!='operator_settings' or row['actor_id']!=value['actor'] or row['aggregate_id']!=value['account'] or proof.get('command')!=value:
        raise ValueError('operator_ai_settings_identity_conflict')
    return proof


def record(conn, value, *, before, after, settings, at, native_binding=None):
    if value is None:return
    from packages.application.wb_autoanswers_runtime import autoanswers_settings_revision
    from packages.contracts.wb_autoanswers import PROMPT_BUNDLE_VERSION,EVALUATION_SIGNATURE
    proof=dict(contract='operator_ai_settings_v1',command=value,before=dict(before),after=dict(after),
        saved_settings=asdict(settings),settings_revision=autoanswers_settings_revision(settings),native_binding=native_binding or {})
    conn.execute('INSERT INTO '+AUDIT+'(event_id,aggregate_type,aggregate_id,event_type,actor_type,actor_id,bundle_version,evaluation_signature,details_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (value['operation_id'],'operator_settings',value['account'],'operator_settings_saved','user',value['actor'],PROMPT_BUNDLE_VERSION,EVALUATION_SIGNATURE,canonical(proof),at))


@dataclass(frozen=True)
class SettingsSurface:
    runtime_dir: Path
    account: str
    actor: str
    account_scope: str = DEFAULT_ACCOUNT_SCOPE

    @classmethod
    def from_entrypoint(cls,entrypoint,*,actor):
        source=getattr(entrypoint,'change_registry_read_surface',None)
        account=str(getattr(source,'seller_id','') or '')
        if not account or not actor:return None
        from packages.application.wb_autoanswers_runtime import autoanswers_store_path
        runtime=Path(entrypoint.runtime.runtime_dir).resolve()
        owner=getattr(entrypoint,'autoanswers_repository',None)
        if owner is None or Path(owner.db_path).resolve()!=autoanswers_store_path(runtime).resolve():
            raise ValueError('operator_ai_settings_owner_binding_invalid')
        return cls(runtime,account,str(actor),str(source.account_scope))


def source(conn, *, selected, scope):
    if DOMAIN not in selected or not isinstance(scope,SettingsSurface) or not scope.account or not scope.actor:return None
    from packages.application.wb_autoanswers_runtime import autoanswers_store_path
    from packages.application.operator_feedback_operations import _attach,_exists
    path=autoanswers_store_path(scope.runtime_dir.resolve())
    if not _attach(conn,path,ALIAS) or not _exists(conn,ALIAS,AUDIT):return None
    return (TABLE,'operation_id','*',"aggregate_type='operator_settings' AND actor_type='user' AND actor_id=? AND aggregate_id=? AND json_extract(details_json,'$.command.account_scope')=?",
        (scope.actor,scope.account,scope.account_scope),public)


def public(conn,row):
    proof=json.loads(row['details_json']);cmd=proof.get('command') or {}
    # A malformed source never gains accepted authority merely from its flag.
    rebuilt=command(dict(cmd.get('request') or {},operation_id=row['operation_id']),actor=row['actor_id'],account=row['aggregate_id'],account_scope=cmd.get('account_scope'))
    if rebuilt!=cmd or proof.get('contract')!='operator_ai_settings_v1' or not proof.get('before') or not proof.get('after') or not proof.get('settings_revision'):
        raise ValueError('operator_ai_settings_source_proof_invalid')
    from packages.contracts.wb_autoanswers import AutoanswersSettings
    from packages.application.wb_autoanswers_runtime import autoanswers_settings_revision,_autoanswers_settings_from_row
    saved=AutoanswersSettings(**proof['saved_settings'])
    reconstructed=_autoanswers_settings_from_row(proof['after'],env={})
    if autoanswers_settings_revision(reconstructed)!=proof['settings_revision'] or autoanswers_settings_revision(saved)!=proof['settings_revision']:
        raise ValueError('operator_ai_settings_revision_invalid')
    needs_execution=bool({'selector_state','master_enabled','mode'}.intersection(cmd['request']))
    binding=proof.get('native_binding') or {}
    if cmd['request'].get('selector_state') in {'draft_only','auto_safe','auto_all'} and (not all(binding.get(k) for k in ('preview_id','sweep_id','transition_run_id')) or binding.get('preview_id')!=cmd['request'].get('preview_id')):
        raise ValueError('operator_ai_settings_transition_proof_missing')
    result=dict(contract_name='operator_operations_v1',operation_id=row['operation_id'],domain=DOMAIN,title_ru=LABEL,
        durable_saved=True,accepted_at=row['accepted_at'],actor=row['actor_id'],state='accepted' if needs_execution else 'completed',
        primary_effect='source_saved',native_state='source_saved',calculation_completed=False,external_confirmed=False,
        execution_required=needs_execution,reason_ru='Настройки сохранены. Включение или остановка исполнителя проверяется отдельно.' if needs_execution else 'Лимиты сохранены. Новое выполнение и публикация WB этим документом не подтверждаются.',
        source_ref=dict(domain=DOMAIN,entity_id=row['operation_id'],account=cmd['account'],account_scope=cmd['account_scope'],request_digest=cmd['digest'],
            settings_revision=proof['settings_revision'],policy_epoch=saved.policy_epoch,**binding),
        saved_settings=proof['saved_settings'],request=cmd['request'],fields=[dict(label='Режим',value=saved.mode if saved.master_enabled else 'off'),dict(label='Версия',value=proof['settings_revision'])],
        resubmit_allowed=False,native_path=PATH,journal_path='/sheet-vitrina-v1/operations?operation_id='+row['operation_id'],
        detail_path='/v1/sheet-vitrina-v1/operations/'+row['operation_id'])
    observed=conn.execute('SELECT event_id,details_json,created_at FROM '+ALIAS+'.'+AUDIT+" WHERE aggregate_type='operator_settings_execution' AND event_type='operator_settings_lifecycle_observed' AND actor_type='user' AND aggregate_id=? AND actor_id=? ORDER BY rowid DESC LIMIT 1",(row['operation_id'],row['actor_id'])).fetchone()
    if observed:
        execution=json.loads(observed['details_json'])
        if execution.get('command_digest')!=cmd['digest'] or execution.get('account')!=cmd['account'] or execution.get('account_scope')!=cmd['account_scope']:
            raise ValueError('operator_ai_settings_execution_binding_invalid')
        result['execution_ref']=dict(event_id=observed['event_id'],observed_at=observed['created_at'],
            digest=hashlib.sha256(observed['details_json'].encode()).hexdigest())
        result['execution_observed_at']=observed['created_at']
        result['execution_error_code']=execution.get('error_code','')
        result['execution_confirmed']=bool(needs_execution and not execution.get('error_code') and execution_proven(execution.get('lifecycle') or {},saved,binding))
        if execution.get('error_code'):
            result.update(state='needs_attention',reason_ru='Настройки сохранены. Выполнение требует внимания; повторная команда не отправляется.')
        elif result['execution_confirmed']:
            result.update(state='completed',reason_ru='Настройки сохранены. Работа исполнителя подтверждена точной проверкой его units на указанную дату. Публикация WB этим не подтверждается.')
    return result


def read(entrypoint, value):
    if value is None:return None
    from packages.application.operator_operations import read_acceptance
    scope=SettingsSurface.from_entrypoint(entrypoint,actor=value['actor'])
    result=read_acceptance(entrypoint.runtime.db_path,value['operation_id'],allowed_domains={DOMAIN},ai_settings_scope=scope)
    if result and (result['source_ref']['request_digest']!=value['digest'] or result['request']!=value['request']):
        raise ValueError('operator_ai_settings_identity_conflict')
    return result


def observe(entrypoint, value, *, lifecycle=None, error_code=None):
    """Dated native lifecycle outcome linked to the unchanged settings command.

    This appends to the existing audit after the native owner call. Retry/GET
    never calls this function. Failure to retain an observation cannot prove
    execution; the original source receipt remains readable.
    """
    if value is None:return
    repo=entrypoint.autoanswers_repository
    details=dict(command_digest=value['digest'],account=value['account'],account_scope=value['account_scope'],
        lifecycle=lifecycle or {},error_code=error_code or '')
    # Native status is bounded. Oversize diagnostic data is a missing execution
    # proof, not a truncated result or an assertion that activation succeeded.
    if len(canonical(details).encode())>65536:
        details.update(lifecycle={},error_code='lifecycle_proof_too_large')
    with repo.transaction() as conn:
        if retained(conn,value) is None:raise ValueError('operator_ai_settings_source_missing')
        repo._audit(conn,aggregate_type='operator_settings_execution',aggregate_id=value['operation_id'],
            event_type='operator_settings_lifecycle_observed',actor_type='user',actor_id=value['actor'],
            details=details,at=repo._now())


def execution_proven(lifecycle, saved, binding):
    """Require dated native unit readback; convenient lifecycle flags are insufficient."""
    from packages.application.wb_autoanswers_lifecycle import READONLY_TIMER,WORKER_TIMER,READONLY_SERVICE,WORKER_SERVICE
    if not lifecycle.get('readback_captured_at') or lifecycle.get('policy_epoch')!=saved.policy_epoch or lifecycle.get('business_mode')!=(saved.mode if saved.master_enabled else 'off'):
        return False
    if lifecycle.get('drift_status')!='matched' or lifecycle.get('lifecycle_state') not in {'running','off','suspended_by_master'}:return False
    if binding.get('transition_run_id') and lifecycle.get('transition_run_id')!=binding['transition_run_id']:return False
    components=lifecycle.get('components') or {}
    for key,timer_unit,service_unit in (('readonly_sync',READONLY_TIMER,READONLY_SERVICE),('worker',WORKER_TIMER,WORKER_SERVICE)):
        component=components.get(key) or {};timer=component.get('timer') or {};service=component.get('service') or {};properties=timer.get('properties') or {}
        if component.get('component_key')!=key or type(component.get('desired')) is not bool or component.get('drift_status')!='matched' or timer.get('unit')!=timer_unit or service.get('unit')!=service_unit or not service.get('properties'):
            return False
        enabled=timer.get('is_enabled');active=timer.get('is_active')
        if enabled not in {'enabled','enabled-runtime','disabled','masked','masked-runtime','static'} or active not in {'active','inactive'} or properties.get('UnitFileState')!=enabled or properties.get('ActiveState')!=active:return False
        actual=enabled=='enabled' and active=='active'
        if component.get('actual') is not actual or actual!=component['desired']:return False
    return True
