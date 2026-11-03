"""Offline evidence inspection for one 583 History completion, never execution.

Saved artifact integrity and agreement are distinguishable from current native
source authority. No runtime, owner, writer, compiler or collector is constructed.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
from zoneinfo import ZoneInfo

SCHEMA = '583_history_completion_preflight_case_v1'
CYCLE = '8f7eb43fe00f982f49b822ee2ec953ea'
OPERATION = 'wbc-583-finalization-preview-20261103'
RECIPE = 'sha256:fe4ff164f80a0993217332b8722585692d5a412b2df9b14c20eca15e75fefc08'
HISTORICAL_BOOK = 'sha256:f48c9ff09d1f0a4bf6c5fd9a4fe65ab9d9572b88c559054eb9919070faeb3897'
MEMORY_FIX_COMMIT = '54227e003d1bbbb6524a7c7065dd374a87fe66a4'
MEMORY_FIX_EPOCH = 'wbc0069k16-reviewed-native-v1:a998323263d1a737eda5a3bd6ea917384fe5f60b2a1c4013551f5981215f676c'
STAGES = ('closed_sources','api_sources','finance_sources','fbs_generation','warehouse',
          'daily_projection','closed_ready','final_ready','rolling14')
MISSING_INTERFACES = (
    'exact_history_tail_admission_while_maintenance_remains_held',
    'fresh_native_job_owner_and_fixed_fd_history_handoff',
    'fresh_predecessor_ready_and_closed_source_validation',
    'read_only_native_finance_validation_of_all_current_candidate_weeks',
    'preservation_readback_of_the_seven_completed_native_confirmations',
    'history_ack_and_original_583_finalizer_tail_entry',
)
MAX_JSON_BYTES = 2 * 1024**2
MAX_RAW_BYTES = 768 * 1024**2


class PreflightEvidenceError(ValueError):
    pass


def _require(condition, code):
    if not condition: raise PreflightEvidenceError(code)


def _hash(value):
    value = value.removeprefix('sha256:') if isinstance(value,str) else ''
    _require(len(value)==64 and all(c in '0123456789abcdef' for c in value), 'artifact_sha256_invalid')
    return value


def _pairs(values):
    result={}
    for key,value in values:
        _require(key not in result,'duplicate_json_key')
        result[key]=value
    return result


def _json(raw):
    def invalid(_): raise PreflightEvidenceError('nonfinite_json_number')
    return json.loads(raw,object_pairs_hook=_pairs,parse_constant=invalid)


def _file(path, *, maximum, expected=None, decode=True):
    path=Path(path)
    _require(path==path.resolve(),'artifact_symlink_forbidden')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_size<=maximum,'artifact_size_or_type_invalid')
        hashed=hashlib.sha256();parts=[];size=0
        while chunk:=stream.read(64*1024):
            size+=len(chunk);_require(size<=maximum,'artifact_size_or_type_invalid')
            hashed.update(chunk)
            if decode: parts.append(chunk)
        after=os.fstat(stream.fileno())
    identity=lambda item:(item.st_dev,item.st_ino,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
    _require(identity(before)==identity(after) and size==before.st_size,'artifact_changed_during_read')
    digest=hashed.hexdigest()
    if expected is not None: _require(digest==_hash(expected),'artifact_sha256_mismatch')
    return (_json(b''.join(parts)) if decode else None),dict(sha256=digest,bytes=size)


def _time(value):
    try: parsed=datetime.fromisoformat(value)
    except (ValueError,TypeError): raise PreflightEvidenceError('aware_timestamp_required') from None
    _require(parsed.utcoffset() is not None,'aware_timestamp_required')
    return parsed


def inspect_case(case_path, *, now):
    """Return a non-executable report; every input is an explicit local file."""
    case_path=Path(case_path).resolve();now=_time(now) if isinstance(now,str) else now
    _require(isinstance(now,datetime) and now.utcoffset() is not None,'aware_timestamp_required')
    case,_=_file(case_path,maximum=32*1024)
    _require(isinstance(case,dict) and case.get('schema_version')==SCHEMA,'case_schema_invalid')
    refs=case.get('artifacts',{})
    _require(isinstance(refs,dict) and {'cycle_receipt','current_metadata'}<=set(refs),'case_artifacts_missing')
    known={'cycle_receipt','current_metadata','finance_receipt','captured_evidence','ready_observation',
           'parent_inputs','child_inputs','runtime_contract','target_runtime_contract'}
    _require(set(refs)<=known,'case_artifact_unknown')
    values={};artifacts={}
    for name,ref in refs.items():
        _require(isinstance(ref,dict) and set(ref)=={'path','sha256'},'artifact_reference_invalid')
        path=Path(ref['path']);path=path if path.is_absolute() else case_path.parent/path
        raw=name in {'parent_inputs','child_inputs'}
        values[name],artifacts[name]=_file(path,maximum=MAX_RAW_BYTES if raw else MAX_JSON_BYTES,
                                         expected=ref['sha256'],decode=not raw)
    cycle=values['cycle_receipt'];metadata=values['current_metadata']
    _require(isinstance(cycle,dict) and isinstance(metadata,dict),'case_artifact_object_required')
    checks=[];conflicts=[]
    def check(name, passed, *, scope, missing=(), **detail):
        status='unknown' if passed is None else 'verified' if passed and scope=='saved_artifact' else 'planned' if passed else 'unknown'
        checks.append(dict(name=name,status=status,scope=scope,matches=passed,missing=list(missing),**detail))
        if passed is False: conflicts.append(name)
    def compare(name, observed, expected, *, scope='offline_observation'):
        observed=observed if isinstance(observed,dict) else {}
        missing=[key for key in expected if observed.get(key) is None]
        differences=[key for key,value in expected.items() if key not in missing
                     and (type(observed[key]) is not type(value) or observed[key]!=value)]
        matches=False if differences else None if missing else True
        check(name,matches,scope=scope,
              missing=missing,different_fields=differences)
        return matches
    check('artifact_byte_integrity',True,scope='saved_artifact',artifact_count=len(artifacts))
    stages=cycle.get('stages',[])
    _require(isinstance(stages,list),'cycle_stages_invalid')
    exact_stages=isinstance(stages,list) and [s.get('stage') for s in stages if isinstance(s,dict)]==list(STAGES)
    prefix=exact_stages and all(s.get('status') in {'complete','degraded'} and s.get('finished_at')
        and isinstance(s.get('versions'),dict) and s['versions'] for s in stages[:8])
    check('original_cycle_finished_prefix',bool(cycle.get('cycle_id')==CYCLE and prefix),scope='saved_artifact',
          completed_prefix_count=sum(s.get('status') in {'complete','degraded'} for s in stages[:8] if isinstance(s,dict)),
          degraded_stages=[s['stage'] for s in stages[:8] if isinstance(s,dict) and s.get('status')=='degraded'])
    check('original_history_outcome_unfinished',exact_stages and stages[-1].get('status') in {'running','interrupted','failed'},
          scope='saved_artifact',original_owner_pid=cycle.get('owner_pid'),original_job_id=cycle.get('job_id'),
          original_history_status=stages[-1].get('status') if exact_stages else None)
    compare('old_owner_observation',metadata.get('old_owner'),dict(pid=cycle.get('owner_pid'),proc_exists=False))
    versions=cycle.get('final_versions',{})
    capture=metadata.get('capture',{})
    captured=_time(capture['observed_at']) if capture.get('observed_at') else None
    check('capture_clock',None if captured is None else captured<=now,scope='offline_observation',
          captured_at=capture.get('observed_at'),age_seconds=(now-captured).total_seconds() if captured else None)
    day=now.astimezone(ZoneInfo('Asia/Yekaterinburg')).date().isoformat()
    check('cycle_business_day',cycle.get('business_date')==day,scope='offline_observation',
          cycle_day=cycle.get('business_date'),requested_day=day)
    maintenance=metadata.get('maintenance',{})
    window=maintenance.get('window_id')
    compare('held_maintenance_observation',maintenance,dict(phase='held',barrier_phase='held',
        barrier_window_id=window,hold_confirmed=True,quiet=True))
    check('held_maintenance_identity',bool(window and maintenance.get('baseline_fingerprint')),
          scope='offline_observation')
    publications=metadata.get('publications',{})
    parent=publications.get('parent',{});child=publications.get('child',{})
    compare('original_parent_identity',parent,dict(operation_id=OPERATION,attempt_id='1',
        kind='fbs_temporary_allocation_effect_intent_v1',state='prepared',recipe_digest=RECIPE))
    compare('published_child_identity',child,dict(operation_id=OPERATION+':publication',attempt_id='1',
        kind='fbs_temporary_allocation_publication_v1',state='complete',book_version=HISTORICAL_BOOK))
    for name,row in [('parent_inputs',parent),('child_inputs',child)]:
        pin=artifacts.get(name)
        if pin:
            compare(name+'_captured_raw_pin',row,dict(inputs_sha256=pin['sha256'],inputs_bytes=pin['bytes']))
        else:
            check(name+'_captured_raw_pin',None,scope='offline_observation',missing=[name])
    active=metadata.get('active',{})
    compare('active_book_and_functional_predecessor',active,{
        'book_version':versions.get('fbs_book'),'functional_version':versions.get('functional_version')})
    ready_versions=stages[7].get('versions',{}) if exact_stages else {}
    ready_keys=('bundle_version','as_of_date','snapshot_id','plan_version','refreshed_at',
                'publication_operation_id','publication_attempt_id','ready_fingerprint')
    compare('final_ready_predecessor',metadata.get('ready'),{key:ready_versions.get(key) for key in ready_keys})
    ready=metadata.get('ready',{})
    compare('ready_saved_publication_readback',ready,dict(publication_state='complete',actual_ready_digest=ready.get('after_digest')))
    observed_ready=values.get('ready_observation')
    if isinstance(observed_ready,dict):
        compare('ready_observation_provenance',observed_ready,dict(ready_digest=ready.get('actual_ready_digest'),
            ready_fingerprint=ready.get('ready_fingerprint')))
        compare('ready_observation_publication',observed_ready.get('publication'),dict(state='complete',
            after_digest=ready.get('after_digest'),operation_id=ready.get('publication_operation_id'),
            attempt_id=ready.get('publication_attempt_id')))
    history_current=metadata.get('history_current') or {}
    check('retained_history_observation',None if 'history_pending_exists' not in metadata else True,
          scope='offline_observation',current_edition=history_current.get('current'),
          pending_exists=metadata.get('history_pending_exists'),native_date_objects_verified=False)
    warehouse=metadata.get('warehouse',{})
    compare('warehouse_predecessor',warehouse,dict(run_id=versions.get('warehouse_run'),status='success',
        functional_version_id=versions.get('functional_version')))
    phases=warehouse.get('phases')
    phase_statuses=list(phases.values()) if isinstance(phases,dict) else [p.get('status') for p in phases] if isinstance(phases,list) else []
    phase_statuses=[value.get('status') if isinstance(value,dict) else value for value in phase_statuses]
    check('warehouse_phase_observation',all(value=='success' for value in phase_statuses) if phase_statuses else None,
          scope='offline_observation',observed_phase_count=len(phase_statuses))
    finance=values.get('finance_receipt')
    finance_detail=dict(live_validation='not_performed',coverage='unknown',checked_week_count=None,
                        recalculated_week_count=None,receipt_provenance_verified=False)
    if isinstance(finance,dict):
        provenance=metadata.get('finance_provenance',{})
        compare('saved_finance_provenance',provenance,dict(warehouse_run_id=versions.get('warehouse_run'),
            phase_key='dependent_replay_economics',receipt_sha256=artifacts['finance_receipt']['sha256']))
        source=values.get('captured_evidence',{})
        source_warehouse=(source.get('warehouse_run') or {}) if isinstance(source,dict) else {}
        bound=compare('finance_captured_warehouse_binding',source_warehouse,dict(run_id=versions.get('warehouse_run'),
            status='success',functional_version_id=versions.get('functional_version')),scope='saved_artifact')
        original=source_warehouse.get('result_json')
        raw_finance=_json(original).get('wb_finance_cost_recalculation') if isinstance(original,str) else None
        authentic=(json.dumps(raw_finance,sort_keys=True,separators=(',',':'))==json.dumps(finance,sort_keys=True,separators=(',',':'))) if raw_finance is not None else None
        check('finance_matches_captured_warehouse_result',authentic,scope='saved_artifact',
              missing=[] if raw_finance is not None else ['captured_evidence.warehouse_run.result_json'])
        compare('saved_finance_consistency',finance,dict(fingerprint=versions.get('weekly_cost_fingerprint'),
            non_target_preserved=True,post_verify_stale_week_count=0,accounting_version_unchanged=True,
            accounting_version_before=active.get('book_version'),accounting_version=active.get('book_version'),
            source_advanced_after_apply=False))
        check('saved_finance_status',finance.get('status') in {'applied','already_current'},scope='saved_artifact')
        weeks=finance.get('weeks')
        summarized=isinstance(weeks,dict) and weeks.get('details_omitted') is True
        complete=isinstance(weeks,list) and len(weeks)==finance.get('recalculated_week_count')
        checked=finance.get('checked_week_count');recalculated=finance.get('recalculated_week_count')
        valid_counts=type(checked) is int and type(recalculated) is int and 0<=recalculated<=checked
        check('saved_finance_scope_shape',bool(valid_counts and (complete or (summarized and weeks.get('item_count')==recalculated))),
              scope='saved_artifact')
        finance_detail.update(receipt_provenance_verified=authentic is True and bound is True,
            coverage='saved_target_scope_only' if complete else 'summarized_target_scope' if summarized else 'unknown',
            checked_week_count=finance.get('checked_week_count'),recalculated_week_count=finance.get('recalculated_week_count'))
    else:
        check('saved_finance_receipt',None,scope='saved_artifact',missing=['finance_receipt'])
    formula=metadata.get('formula',{});contract=values.get('runtime_contract',{})
    target=values.get('target_runtime_contract',{})
    hashes=target.get('formula_code_hashes') if isinstance(target,dict) else None
    derived=('wbc0069k16-reviewed-native-v1:'+hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()) if isinstance(hashes,dict) else None
    check('reviewed_memory_target_contract',None if not target else target.get('formula_epoch')==MEMORY_FIX_EPOCH==derived,
          scope='saved_artifact',missing=[] if target else ['target_runtime_contract'])
    transition=dict(installed_epoch=formula.get('formula_epoch'),target_epoch=MEMORY_FIX_EPOCH,
        retained_contract_epoch=contract.get('formula_epoch') if isinstance(contract,dict) else None,
        memory_fix_commit=MEMORY_FIX_COMMIT,
        requires_explicit_formula_transition=None if not formula.get('formula_epoch') else formula['formula_epoch']!=MEMORY_FIX_EPOCH,
        original_cycle_history_config_fingerprint=cycle.get('history_config_fingerprint'))
    if 'runtime_contract' in artifacts and isinstance(contract,dict):
        compare('installed_formula_contract_observation',formula,dict(runtime_contract_sha256=artifacts['runtime_contract']['sha256'],
            formula_epoch=contract.get('formula_epoch')))
    else:
        check('installed_formula_contract_observation',None,scope='offline_observation',missing=['runtime_contract'])
    return dict(contract='583_history_completion_preflight_report_v1',status='preflight_only',execution_supported=False,
        ready=False,cycle_id=CYCLE,operation_id=OPERATION,recipe_digest=RECIPE,
        evidence_status='conflicting' if conflicts else 'consistent_offline_observations',
        checks=checks,conflicts=conflicts,artifacts=artifacts,finance=finance_detail,formula_transition=transition,
        missing_native_interfaces=list(MISSING_INTERFACES),
        current_source_validation='not_performed',native_history_ack='not_performed',
        finalizer='not_performed',original_receipts_modified=False)
