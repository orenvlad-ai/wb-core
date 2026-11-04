"""Read-only exact formula-delta proof while the original pause remains HELD.

A premerge claim retains the prior completed owner proof before replacing that
owner. Only the same PREPARED operation may consume it, before runtime mutation.
Once deployment completes, prove original baseline -> current canonical epoch
fresh again. Neither path writes a pause, baseline, timer, barrier or resume.
"""
from pathlib import Path
from packages.application import business_data_deploy_protection as deploy
from packages.application import business_data_maintenance_pause as pause
from packages.application import business_data_formula_resume as formula
from packages.application import business_data_write_barrier as barrier
from packages.application import business_data_schedule_profile as schedule

FILENAME = '.business-data-held-formula-deploy-claim.json'
SCHEMA = 'business_data_held_formula_deploy_claim_v1'


def _prepared_plan(runtime, owner, app_dir, unit_directory):
    proof = schedule.signed(schedule.read_private(runtime / FILENAME), SCHEMA)
    plan = schedule.signed(proof['plan'], formula.PLAN_SCHEMA)
    previous = plan['authority']['owner']
    if (set(proof) != {'schema_version', 'operation_id', 'plan', 'fingerprint'}
            or owner['phase'] != 'prepared' or owner['expected_sha']
            or proof['operation_id'] != owner['operation_id']
            or plan['runtime_dir'] != str(runtime) or plan['app_dir'] != str(Path(app_dir).absolute())
            or plan['unit_directory'] != str(Path(unit_directory).absolute())
            or plan['window_id'] != owner['window_id']
            or plan['baseline_fingerprint'] != owner['baseline_fingerprint']
            or pause.fingerprint(plan['baseline']) != owner['baseline_fingerprint']
            or previous.get('schema') != deploy.SCHEMA or previous.get('phase') != 'complete'
            or previous.get('fingerprint') != deploy._fingerprint(previous)
            or previous.get('operation_id') != plan['operation_id']
            or previous.get('operation_id') == owner['operation_id']
            or previous.get('window_id') != owner['window_id']
            or previous.get('baseline_fingerprint') != owner['baseline_fingerprint']
            or previous.get('expected_sha') != plan['expected_sha']):
        raise RuntimeError('held formula prepared claim binding differs')
    return plan


def unchanged(runtime, state, current, *, app_dir, systemd, activity_reader,
              unit_directory=Path('/etc/systemd/system'), proc_root=Path('/proc')):
    """Return an exact plan only for canonical formula drift; never resume."""
    try:
        pause._unchanged(state, current)
        return None
    except RuntimeError:
        pass
    runtime = Path(runtime).resolve()
    if current.get('quiet') is not True:
        raise RuntimeError('held formula requires complete quiet proof')
    owner = deploy.load(runtime)
    if not owner or deploy._binding(runtime, owner) != state:
        raise RuntimeError('held formula pause binding changed')
    schedule.assert_no_partial_transition(runtime)
    formula.assert_no_partial_transition(runtime)
    if owner['phase'] == 'complete':
        return formula.preview(runtime, operation_id=owner['operation_id'],
            window_id=state['window_id'], expected_sha=owner['expected_sha'],
            app_dir=app_dir, unit_directory=unit_directory, systemd=systemd,
            activity_reader=activity_reader, proc_root=proc_root)
    plan = _prepared_plan(runtime, owner, app_dir, unit_directory)
    # Only this private, operation-bound retained proof can supply the prior
    # completed owner. All installed files/configuration/idle evidence is fresh.
    old, fresh = formula._observe(runtime, plan, systemd, activity_reader, proc_root,
        _completed_owner=plan['authority']['owner'])
    if old != state or fresh.get('quiet') is not True:
        raise RuntimeError('held formula retained claim quiet proof changed')
    return plan


def retain_claim(runtime, operation, owner, plan):
    """Inside native owner locks, before PREPARED replaces a completed owner."""
    if owner['phase'] != 'complete':
        return
    proof = dict(schema_version=SCHEMA, operation_id=operation, plan=plan)
    proof['fingerprint'] = pause.fingerprint(proof)
    path = Path(runtime) / FILENAME
    previous = schedule.read_private(path, optional=True)
    if previous and previous != proof and previous.get('operation_id') != owner['operation_id']:
        raise RuntimeError('unconsumed held formula claim proof exists')
    barrier._atomic_write_private_json(path, proof)
