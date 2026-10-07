"""An exact deploy owns an already-held pause; it never pauses or resumes it.

The durable owner survives coordinator/SSH loss. Prepared ownership can be
cancelled, but mutation_started cannot: recovery must finish the same deploy.
This module can be sent by the trusted runner before the first protected sync.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys

from packages.application.business_data_procedure_admission import admission_idle
from packages.application import business_data_maintenance_pause as pause
from packages.application import business_data_write_barrier as barrier

FILENAME = '.business-data-deploy-owner.json'
SCHEMA = 'business_data_deploy_owner_v1'
ACTIVE = {'prepared', 'mutation_started'}
PHASES = ACTIVE | {'complete', 'cancelled'}
ENV_OPERATION = 'WB_CORE_RELEASE_OPERATION_ID'


def _sha(value):
    if re.fullmatch('[0-9a-f]{40}', value or '') is None:
        raise RuntimeError('deploy protection requires exact SHA')
    return value


def load(runtime):
    path = Path(runtime) / FILENAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
            raise RuntimeError('deploy owner is not a bounded private file')
        value = json.loads(os.read(fd, 16385))
    finally:
        os.close(fd)
    fields = {'schema', 'phase', 'operation_id', 'window_id', 'baseline_fingerprint',
              'expected_sha', 'updated_at', 'fingerprint'}
    if (not isinstance(value, dict) or set(value) != fields or value['schema'] != SCHEMA
            or value['phase'] not in PHASES or value['fingerprint'] != _fingerprint(value)):
        raise RuntimeError('deploy owner is invalid')
    barrier._validate_identifier(value['operation_id'], label='deploy operation')
    barrier._validate_identifier(value['window_id'], label='deploy window')
    barrier._validate_fingerprint(value['baseline_fingerprint'])
    if value['expected_sha']:
        _sha(value['expected_sha'])
    elif value['phase'] in {'mutation_started', 'complete'}:
        raise RuntimeError('deploy owner SHA is absent')
    return value


def _fingerprint(value):
    return pause.fingerprint({key: val for key, val in value.items() if key != 'fingerprint'})


def _save(runtime, value):
    value = dict(value, updated_at=pause.now_iso())
    value['fingerprint'] = _fingerprint(value)
    barrier._atomic_write_private_json(Path(runtime) / FILENAME, value)
    barrier._append_private_audit(Path(runtime) / pause.AUDIT_FILENAME, {
        'event': 'deploy_' + value['phase'], 'operation_id': value['operation_id'],
        'window_id': value['window_id'], 'expected_sha': value['expected_sha'],
        'captured_at': value['updated_at'], 'fingerprint': value['fingerprint']})
    return value


def assert_releasable(runtime):
    """Called inside the existing restore/barrier locks, before their mutations."""
    value = load(runtime)
    if value and value['phase'] in ACTIVE:
        raise RuntimeError('maintenance pause is owned by an unfinished deploy')


def _selected(runtime):
    from packages.application.business_data_schedule_profile import load_selector
    return load_selector(Path(runtime).resolve()) is not None


def _binding(runtime, owner=None):
    state = pause.load_state(Path(runtime))
    current = barrier.barrier_status(Path(runtime))
    if (not state or state['phase'] != 'held' or current.get('active') is not True
            or current.get('phase') != 'held' or current.get('window_kind') != 'maintenance_pause'
            or current.get('hold_confirmed') is not True
            or current.get('window_id') != state['window_id']
            or current.get('plan_fingerprint') != state['baseline_fingerprint']):
        raise RuntimeError('deploy requires an already held maintenance pause')
    if owner and (owner['window_id'] != state['window_id']
                  or owner['baseline_fingerprint'] != state['baseline_fingerprint']):
        raise RuntimeError('deploy pause binding changed')
    proof = admission_idle(Path(runtime))
    if not proof['ready'] or not proof['idle']:
        raise RuntimeError('deploy pause has not drained admitted writers')
    return state


@contextmanager
def _locked(runtime):
    import inspect
    from apps.business_data_maintenance import _ExclusiveRestoreLock
    # Owner maintenance shares both existing locks with ordinary restore and
    # direct barrier release. It may inspect/finish its own active owner.
    # The first release can bootstrap from a verified held old pause; the old
    # restore lock does not yet know the owner keyword. Never bypass its lock.
    options = {'_deploy_owner': True} if '_deploy_owner' in inspect.signature(_ExclusiveRestoreLock).parameters else {}
    with _ExclusiveRestoreLock(runtime, **options), barrier._BarrierLock(runtime):
        yield


def claim(runtime, operation, *, quiet_reader):
    runtime = Path(runtime).resolve()
    barrier._validate_identifier(operation, label='deploy operation')
    if not _selected(runtime) and load(runtime) is None:
        return {'status': 'legacy_profile'}
    with _locked(runtime):
        old = load(runtime)
        state = _binding(runtime, old if old and old['phase'] in ACTIVE else None)
        result = quiet_reader()
        if result.get('quiet') is not True:
            raise RuntimeError('deploy requires fresh held/quiet proof')
        pause._unchanged(state, result)
        if old and old['phase'] in ACTIVE:
            if old['operation_id'] != operation:
                raise RuntimeError('another deploy owns the maintenance pause')
            return old
        if old and old['operation_id'] == operation:
            raise RuntimeError('terminal deploy operation cannot be restarted')
        return _save(runtime, dict(schema=SCHEMA, phase='prepared', operation_id=operation,
            window_id=state['window_id'], baseline_fingerprint=state['baseline_fingerprint'],
            expected_sha='', updated_at='', fingerprint=''))


def start(runtime, operation, expected_sha, *, recovery=False):
    runtime = Path(runtime).resolve()
    _sha(expected_sha)
    if not _selected(runtime) and load(runtime) is None:
        return {'status': 'legacy_profile'}
    with _locked(runtime):
        value = load(runtime)
        if not value or value['operation_id'] != operation or value['phase'] not in ACTIVE:
            raise RuntimeError('same prepared deploy ownership is required')
        _binding(runtime, value)
        if value['expected_sha'] and value['expected_sha'] != expected_sha:
            raise RuntimeError('deploy owner SHA changed')
        if value['phase'] == 'mutation_started':
            if recovery:
                # Canonical recovery has its own reviewed, one-shot stage claim.
                return value
            raise RuntimeError('deploy mutation already started; same-operation readback/recovery required')
        return _save(runtime, dict(value, phase='mutation_started', expected_sha=expected_sha))


def require_owner(runtime, operation=None, expected_sha=None):
    runtime = Path(runtime).resolve()
    value = load(runtime)
    if not _selected(runtime) and value is None:
        return None
    operation = operation or os.environ.get(ENV_OPERATION, '')
    if (not value or not operation or value['operation_id'] != operation
            or value['phase'] != 'mutation_started'
            or (expected_sha and value['expected_sha'] != _sha(expected_sha))):
        raise RuntimeError('same active deploy ownership is required')
    _binding(runtime, value)
    return value


def cancel_prepared(runtime, operation, fingerprint):
    """Exact preview-bound cancellation releases ownership, never the pause."""
    with _locked(Path(runtime)):
        value = load(runtime)
        if (not value or value['operation_id'] != operation or value['phase'] != 'prepared'
                or value['fingerprint'] != fingerprint):
            raise RuntimeError('prepared deploy cancellation proof changed')
        _binding(runtime, value)
        return _save(runtime, dict(value, phase='cancelled'))


def finish(runtime, operation, expected_sha, *, app_dir, registry_reader):
    """Only exact successful installed markers and a live registry end the owner."""
    if not _selected(Path(runtime)) and load(runtime) is None:
        return {'status': 'legacy_profile'}
    with _locked(Path(runtime)):
        value = load(runtime)
        if (not value or value['operation_id'] != operation or value['expected_sha'] != _sha(expected_sha)
                or value['phase'] not in {'mutation_started', 'complete'}):
            raise RuntimeError('same active/completed deploy ownership is required')
        if value['phase'] != 'complete':
            _binding(Path(runtime), value)
        app = Path(app_dir)
        sha = (app / '.wb-core-runtime-sha').read_text().strip()
        meta = json.loads((app / '.wb-core-deploy.json').read_text())
        service = registry_reader()
        if (sha != expected_sha or meta.get('commit') != expected_sha
                or meta.get('deployment_complete') is not True
                or service.get('active_state') != 'active' or int(service.get('main_pid') or 0) <= 0):
            raise RuntimeError('exact final deployment readback is unproven')
        return value if value['phase'] == 'complete' else _save(runtime, dict(value, phase='complete'))


def _clients(runtime, env_file):
    from apps.business_data_maintenance import (
        SystemdClient, RuntimeScheduleClient, _read_env_file, _build_web_auth_cookie)
    env = _read_env_file(Path(env_file))
    client = RuntimeScheduleClient(base_url=env.get('BUSINESS_DATA_MAINTENANCE_BASE_URL') or
        f"http://{env.get('REGISTRY_UPLOAD_HTTP_HOST', '127.0.0.1')}:{env.get('REGISTRY_UPLOAD_HTTP_PORT', '8765')}",
        cookie=_build_web_auth_cookie(env))
    systemd = SystemdClient()
    return systemd, lambda: pause.readback(Path(runtime), systemd=systemd,
        activity_reader=lambda: client._request(pause.ACTIVITY_PATH))


def remote_shell(action, *, app_dir, runtime_dir, env_file, operation, expected_sha='', bootstrap=False):
    """Trusted source bootstrap before sync; all arguments remain shell-quoted.

    Existing installed pause/profile/admission APIs prove the bootstrap boundary
    independently before the candidate owner source can run. No force switch.
    """
    source = Path(__file__).read_text()
    args = [action, '--app-dir', str(app_dir), '--runtime-dir', str(runtime_dir),
            '--env-file', str(env_file), '--operation', operation]
    if expected_sha:
        args.extend(['--expected-sha', expected_sha])
    if action not in {'claim', 'start'} and not bootstrap:
        return (f"cd {shlex.quote(str(app_dir))} && {ENV_OPERATION}={shlex.quote(operation)} "
                + 'python3 -m packages.application.business_data_deploy_protection '
                + shlex.join(args))
    bootstrap = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(app_dir)!r}); "
        "from packages.application.business_data_schedule_profile import load_selector; "
        "from packages.application.business_data_maintenance_pause import load_state; "
        "from packages.application.business_data_write_barrier import barrier_status; "
        "from packages.application.business_data_procedure_admission import admission_idle; "
        f"runtime=Path({str(runtime_dir)!r}); selected=load_selector(runtime) is not None; "
        "state=load_state(runtime) if selected else None; "
        "held=barrier_status(runtime) if selected else {}; "
        "idle=admission_idle(runtime) if selected else {}; "
        "assert not selected or (state and state['phase']=='held' and held.get('active') is True "
        "and held.get('phase')=='held' and held.get('hold_confirmed') is True "
        "and held.get('window_kind')=='maintenance_pause' "
        "and held.get('window_id')==state['window_id'] "
        "and held.get('plan_fingerprint')==state['baseline_fingerprint'] "
        "and idle.get('ready') is True and idle.get('idle') is True), 'deploy requires existing held/quiet pause'; "
        f"namespace={{'__name__':'deploy_owner_bootstrap','__file__':{str(Path(app_dir) / 'packages/application/business_data_deploy_protection.py')!r}}}; "
        f"exec(compile({source!r}, namespace['__file__'], 'exec'), namespace); "
        f"sys.exit(namespace['main']({args!r}))")
    return f"WB_CORE_RELEASE_OPERATION_ID={shlex.quote(operation)} python3 -c {shlex.quote(bootstrap)}"


def guarded_shell(command, *, receiver=False, **identity):
    operation = identity['operation']
    return (f"export {ENV_OPERATION}={shlex.quote(operation)}; "
            + remote_shell('check', **identity) + ' >/dev/null && '
            + (command if receiver else '(\n' + command + '\n)'))


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('claim', 'start', 'recovery-start', 'check', 'status', 'cancel-prepared', 'finish'))
    parser.add_argument('--runtime-dir', type=Path, required=True)
    parser.add_argument('--app-dir', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--expected-sha', default='')
    parser.add_argument('--fingerprint', default='')
    args = parser.parse_args(argv)
    # Bootstrap the trusted source only inside an independently proven existing
    # held pause. No installed helper or force flag is required before first sync.
    if args.action == 'claim':
        if _selected(args.runtime_dir) or load(args.runtime_dir) is not None:
            _binding(args.runtime_dir)
            systemd, quiet = _clients(args.runtime_dir, args.env_file)
        else:
            quiet = lambda: {}
        result = claim(args.runtime_dir, args.operation, quiet_reader=quiet)
    elif args.action in {'start', 'recovery-start'}:
        result = start(args.runtime_dir, args.operation, args.expected_sha, recovery=args.action == 'recovery-start')
    elif args.action == 'check':
        result = require_owner(args.runtime_dir, args.operation, args.expected_sha) or {'status': 'legacy_profile'}
    elif args.action == 'status':
        result = load(args.runtime_dir) or {'status': 'absent'}
    elif args.action == 'cancel-prepared':
        result = cancel_prepared(args.runtime_dir, args.operation, args.fingerprint)
    else:
        from apps.business_data_maintenance import SystemdClient
        systemd = SystemdClient()
        def registry():
            state = systemd.unit_state('wb-core-registry-http.service')
            return dict(active_state=state['is_active'], main_pid=state['properties'].get('MainPID'))
        result = finish(args.runtime_dir, args.operation, args.expected_sha,
                        app_dir=args.app_dir, registry_reader=registry)
    print(json.dumps(result, sort_keys=True))
    return 0


def main(argv=None):
    try:
        return _main(argv)
    except Exception as exc:
        # Provider/HTTP exceptions may carry response bodies. Report a small
        # code only; no traceback, response text, credential or auth material.
        print(json.dumps({'status': 'deploy_protection_blocked',
                          'error_code': type(exc).__name__, 'pause_released': False}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
