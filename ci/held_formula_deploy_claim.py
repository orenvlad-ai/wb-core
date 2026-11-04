"""Trusted premerge owner claim after a completed formula-only deploy.

This control-plane bootstrap is sent from trusted main, never installed as a
runtime module. The next live release must include native receipt consumption.
Only owner metadata and its proof are written; pause/barrier/baseline stay held.
"""
from pathlib import Path
import json
import shlex

FILENAME = '.business-data-held-formula-deploy-claim.json'
SCHEMA = 'business_data_held_formula_deploy_claim_v1'


def claim(runtime, operation, *, app_dir, systemd, activity_reader,
          unit_directory=Path('/etc/systemd/system'), proc_root=Path('/proc')):
    from packages.application import business_data_deploy_protection as deploy
    from packages.application import business_data_maintenance_pause as pause
    from packages.application import business_data_formula_resume as formula
    from packages.application import business_data_write_barrier as barrier
    from packages.application import business_data_schedule_profile as schedule
    runtime = Path(runtime).resolve()
    barrier._validate_identifier(operation, label='deploy operation')
    quiet = lambda: pause.readback(runtime, systemd=systemd,
        activity_reader=activity_reader, proc_root=proc_root)
    old = deploy.load(runtime)
    # Legacy, ordinary unchanged and same-operation paths retain native behavior.
    if not old or old['phase'] != 'complete':
        return deploy.claim(runtime, operation, quiet_reader=quiet)
    with deploy._locked(runtime):
        if deploy.load(runtime) != old:
            raise RuntimeError('completed deploy changed before formula claim')
        state = deploy._binding(runtime)
        if old['operation_id'] == operation:
            raise RuntimeError('terminal deploy operation cannot be restarted')
        current = quiet()
        if current.get('quiet') is not True:
            raise RuntimeError('deploy requires fresh held/quiet proof')
        try:
            pause._unchanged(state, current)
        except RuntimeError:
            # The existing exact proof checks actual source hashes, completed
            # markers, canonical fragment/dropins, loaded config and all writers.
            plan = formula.preview(runtime, operation_id=old['operation_id'],
                window_id=state['window_id'], expected_sha=old['expected_sha'],
                app_dir=Path(app_dir), unit_directory=unit_directory, systemd=systemd,
                activity_reader=activity_reader, proc_root=proc_root)
            previous = schedule.read_private(runtime / FILENAME, optional=True)
            proof = dict(schema_version=SCHEMA, operation_id=operation, plan=plan)
            proof['fingerprint'] = pause.fingerprint(proof)
            if previous and previous != proof and previous.get('operation_id') != old['operation_id']:
                raise RuntimeError('unconsumed held formula claim proof exists')
            barrier._atomic_write_private_json(runtime / FILENAME, proof)
        return deploy._save(runtime, dict(schema=deploy.SCHEMA, phase='prepared', operation_id=operation,
            window_id=state['window_id'], baseline_fingerprint=state['baseline_fingerprint'],
            expected_sha='', updated_at='', fingerprint=''))


def remote_shell(*, app_dir, runtime_dir, env_file, operation):
    source = Path(__file__).read_text()
    script = (
        "import sys,json; from pathlib import Path; "
        f"sys.path.insert(0, {str(app_dir)!r}); "
        "from apps.business_data_maintenance import SystemdClient,RuntimeScheduleClient,_read_env_file,_build_web_auth_cookie; "
        f"env=_read_env_file(Path({str(env_file)!r})); "
        "client=RuntimeScheduleClient(base_url=env.get('BUSINESS_DATA_MAINTENANCE_BASE_URL') or "
        "'http://'+env.get('REGISTRY_UPLOAD_HTTP_HOST','127.0.0.1')+':'+env.get('REGISTRY_UPLOAD_HTTP_PORT','8765'),cookie=_build_web_auth_cookie(env)); "
        "from packages.application.business_data_maintenance_pause import ACTIVITY_PATH; "
        "namespace={'__name__':'trusted_held_formula_claim'}; "
        f"exec(compile({source!r}, 'trusted_held_formula_claim', 'exec'),namespace); "
        f"result=namespace['claim'](Path({str(runtime_dir)!r}),{operation!r},app_dir=Path({str(app_dir)!r}),"
        "systemd=SystemdClient(),activity_reader=lambda:client._request(ACTIVITY_PATH)); "
        "print(json.dumps(result,sort_keys=True))")
    # Suppress exception bodies: HTTP/provider exceptions can contain credentials.
    script = "try:\n " + script + "\nexcept Exception as exc:\n print(json.dumps({'status':'deploy_protection_blocked','error_code':type(exc).__name__,'pause_released':False})); sys.exit(1)\n"
    return 'python3 -c ' + shlex.quote(script)
