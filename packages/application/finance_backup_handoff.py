"""Dormant fixed Finance backup service handoff before cycle acceptance.

One existing signed intent reserves one dispatch. Ambiguous starts are read back,
never resent on negative idle alone. No caller command/path or ownership tokens.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from packages.application.finance_backup_admission import (
    BackupAdmissionStateError, _read, _read_intent, _update, backup_admission_priority,
    defer_backup_admission,
)
from packages.application.finance_storage_snapshot_retention import ARCHIVE_RELATIVE_ROOT, _fingerprint

UNIT = 'wb-core-finance-backup-rotation.service'
RUNTIME = Path('/opt/wb-core-runtime/state')
APP = Path('/opt/wb-core-runtime/app')
UNIT_ARTIFACT = Path('artifacts/registry_upload_http_entrypoint/systemd') / UNIT
SHA_FILE = '.wb-core-runtime-sha'
PROPERTIES = ('LoadState', 'ActiveState', 'SubState', 'Result', 'MainPID', 'Job',
              'InvocationID', 'FragmentPath', 'DropInPaths', 'NeedDaemonReload',
              'WorkingDirectory', 'ExecStart', 'User', 'Group', 'DynamicUser',
              'Environment', 'EnvironmentFiles', 'RootDirectory', 'RootImage',
              'Type', 'ExecStartPre', 'ExecStartPost', 'ExecCondition')
EMPTY_ARRAY_PROPERTIES = {'EnvironmentFiles': 'a(sb)',
                          'ExecCondition': 'a(sasbttttuii)',
                          'ExecStartPre': 'a(sasbttttuii)',
                          'ExecStartPost': 'a(sasbttttuii)'}


def _safe_file(path: Path, *, maximum: int = 65536) -> bytes:
    # Fixed deployment paths must have no symlink/writable ancestor substitution.
    for parent in (path, *path.parents):
        value = parent.lstat()
        if stat.S_ISLNK(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
            raise BackupAdmissionStateError('fixed backup execution identity is unsafe')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_size > maximum:
            raise BackupAdmissionStateError('fixed backup execution file exceeds bound')
        return os.read(fd, maximum + 1)
    finally:
        os.close(fd)


class _FixedBackupService:
    """Only production service implementation; fake harness replaces this class."""
    def __init__(self, runtime: Path):
        if runtime != RUNTIME or Path(__file__).resolve().parents[2] != APP:
            raise BackupAdmissionStateError('backup handoff requires the fixed deployed runtime/app')
        self.runtime = runtime

    def _show(self) -> dict:
        command = ['/usr/bin/systemctl', 'show', UNIT, '--no-pager',
                   '--property=' + ','.join(PROPERTIES)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=True)
        if len(result.stdout) > 32768:
            raise BackupAdmissionStateError('fixed backup service metadata exceeds bound')
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        missing = set(PROPERTIES) - values.keys()
        if missing - EMPTY_ARRAY_PROPERTIES.keys():
            raise BackupAdmissionStateError('fixed backup service metadata incomplete')
        if missing:
            # systemctl omits empty array properties even with --all on the
            # deployed systemd. Absence alone is not proof of an empty setting:
            # require the actual Service properties and their exact D-Bus types.
            names = sorted(missing)
            command = ['/usr/bin/busctl', '--system', '--json=short', 'get-property',
                       'org.freedesktop.systemd1',
                       '/org/freedesktop/systemd1/unit/wb_2dcore_2dfinance_2dbackup_2drotation_2eservice',
                       'org.freedesktop.systemd1.Service', *names]
            arrays = subprocess.run(command, capture_output=True, text=True, timeout=3, check=True)
            if len(arrays.stdout) > 32768 or len(arrays.stdout.splitlines()) != len(names):
                raise BackupAdmissionStateError('fixed backup service array proof incomplete')
            for name, line in zip(names, arrays.stdout.splitlines(), strict=True):
                try:
                    proof = json.loads(line)
                except (ValueError, TypeError) as exc:
                    raise BackupAdmissionStateError('fixed backup service array proof invalid') from exc
                if proof != {'type': EMPTY_ARRAY_PROPERTIES[name], 'data': []}:
                    raise BackupAdmissionStateError('fixed backup service array override or type refused')
                values[name] = ''
        return values

    def inspect(self) -> dict:
        values = self._show()
        sha = _safe_file(APP / SHA_FILE, maximum=128).decode().strip()
        if re.fullmatch(r'[0-9a-f]{40}', sha) is None:
            raise BackupAdmissionStateError('fixed backup deployed SHA is invalid')
        fragment = Path(values['FragmentPath'])
        expected = _safe_file(APP / UNIT_ARTIFACT)
        if (values['LoadState'] != 'loaded' or values['DropInPaths'] or values['NeedDaemonReload'] != 'no'
                or fragment not in {Path('/etc/systemd/system') / UNIT, Path('/usr/lib/systemd/system') / UNIT}
                or _safe_file(fragment) != expected
                or values['WorkingDirectory'] != str(APP) or values['User'] not in {'', 'root'}
                or values['Group'] not in {'', 'root'} or values['DynamicUser'] != 'no'
                or values['Type'] != 'oneshot'
                or any(values[key] for key in ('Environment', 'EnvironmentFiles', 'RootDirectory', 'RootImage',
                                               'ExecStartPre', 'ExecStartPost', 'ExecCondition'))):
            raise BackupAdmissionStateError('fixed backup service override/execution identity refused')
        argv = '/usr/bin/python3 apps/finance_storage_backup_rotation.py --runtime-dir ' + str(RUNTIME) + ' --deployed-sha-file ' + str(APP / SHA_FILE)
        match = re.search(r'argv\[\]=(.*?) ; ignore_errors=(yes|no)', values['ExecStart'])
        if not match or match.group(1) != argv or match.group(2) != 'no' or values['ExecStart'].count('argv[]=') != 1:
            raise BackupAdmissionStateError('fixed backup loaded command differs from canonical artifact')
        # Bounded hashes of the actual canonical entry files; never SQLite/backup bytes.
        identity = {'unit_sha256': hashlib.sha256(expected).hexdigest(), 'deployed_sha': sha,
                    'app': str(APP), 'runtime': str(RUNTIME), 'fragment': str(fragment),
                    'code_sha256': {name: hashlib.sha256(_safe_file(APP / name, maximum=2 * 1024 * 1024)).hexdigest()
                     for name in ('apps/finance_storage_backup_rotation.py',
                                  'packages/application/finance_storage_backup_rotation.py',
                                  'packages/application/finance_backup_admission.py',
                                  'packages/application/finance_backup_handoff.py')}}
        return {'identity': _fingerprint(identity), 'deployed_sha': sha,
                'active': values['ActiveState'] in {'active', 'activating', 'deactivating'} or values['Job'] not in {'', '0'},
                'terminal': values['ActiveState'] in {'inactive', 'failed'} and values['Job'] in {'', '0'} and values['MainPID'] == '0',
                'invocation_id': values['InvocationID'], 'result': values['Result']}

    def start(self) -> None:
        # Lost/failed command response is ambiguous, even if the unit is now idle.
        subprocess.run(['/usr/bin/systemctl', 'start', '--no-block', UNIT],
                       capture_output=True, timeout=3, check=True)


def _validated_state(runtime: Path, deployed_sha: str, *, now=None) -> dict:
    state = backup_admission_priority(runtime, now=now)
    if not state['ready']:
        raise BackupAdmissionStateError(state.get('error', state['reason']))
    pending = state.get('pending_transaction')
    intent = state.get('intent')
    if pending and pending['deployed_sha'] != deployed_sha:
        raise BackupAdmissionStateError('pending backup belongs to another deployed SHA')
    if pending:
        # Validate the bounded stored plan before dispatch; canonical apply still
        # owns all detailed CAS/provenance/copy/recovery guards after acquisition.
        fingerprint = pending['plan_fingerprint']
        transaction = _read(runtime / ARCHIVE_RELATIVE_ROOT / 'transactions' / (fingerprint.removeprefix('sha256:') + '.json'))
        plan = transaction.get('reviewed_plan')
        if (pending['phase'] not in {'started', 'pre_gc_complete', 'operational_copied', 'raw_copied',
                                    'replacement_verified', 'current_selected', 'post_gc_complete'}
                or not isinstance(plan, dict) or plan.get('deployed_sha') != deployed_sha
                or plan.get('fingerprint') != fingerprint
                or _fingerprint({key: value for key, value in plan.items() if key != 'fingerprint'}) != fingerprint):
            raise BackupAdmissionStateError('pending backup phase/stored reviewed plan is unproven')
    if intent and intent['status'] == 'waiting' and intent['deployed_sha'] != deployed_sha:
        raise BackupAdmissionStateError('waiting backup belongs to another deployed SHA')
    if intent and intent['status'] == 'waiting' and intent.get('launch'):
        state = {**state, 'priority': True, 'reason': 'awaiting_canonical_backup_ack'}
    ack = (intent or {}).get('canonical_ack') or {}
    proof = ack.get('not_due_proof') or {}
    # Canonical not_due can override only this exact still-stable plan identity,
    # before RPO. A source/policy/selector change invalidates it immediately.
    instant = now or datetime.now(timezone.utc)
    if (intent and intent['status'] == 'resolved' and ack.get('deployed_sha') == deployed_sha
            and ack.get('status') == 'not_due' and proof.get('plan_fingerprint')
            and not pending and state.get('deadline_at')
            and instant < datetime.fromisoformat(state['deadline_at'])
            and all(proof.get(key) and proof[key] == state.get(key) for key in
                    ('source_stat_fingerprint', 'current_fingerprint', 'policy_fingerprint'))):
        state = {**state, 'priority': False, 'reason': 'canonical_not_due'}
    return state


def cycle_backup_priority(runtime_dir: Path, *, now=None) -> dict:
    """Bounded RO precheck. Inert/no-due paths never inspect/provision systemd."""
    runtime = Path(runtime_dir).resolve()
    state = backup_admission_priority(runtime, now=now)
    if not state['ready']:
        raise BackupAdmissionStateError(state.get('error', state['reason']))
    waiting_launch = (state.get('intent') or {}).get('status') == 'waiting' and (state.get('intent') or {}).get('launch')
    if not state['priority'] and not waiting_launch:
        return state
    service = _FixedBackupService(runtime)
    view = service.inspect()
    return _validated_state(runtime, view['deployed_sha'], now=now)


def handoff_cycle_backup(runtime_dir: Path) -> dict:
    """Return before cycle acceptance/effects, releasing cycle heavy before call."""
    from packages.application.business_data_heavy_admission import current_heavy_owner
    runtime = Path(runtime_dir).resolve()
    if current_heavy_owner(runtime) is not None:
        raise BackupAdmissionStateError('backup service cannot launch under a parent heavy owner')
    service = _FixedBackupService(runtime)
    view = service.inspect()  # Fail before reservation/start for unsafe deploy/unit.
    state = _validated_state(runtime, view['deployed_sha'])
    response = {'status': 'backup_priority', 'accepted': False, 'source_effects_started': False,
                'backup_unit': UNIT, 'reason': state['reason']}
    if not state['priority']:
        return {**response, 'status': 'backup_not_due', 'reason': state['reason']}
    if not state.get('policy_fingerprint'):
        raise BackupAdmissionStateError('pending backup has no approved policy')
    if view['active']:
        return {**response, 'backup_status': 'running', 'invocation_id': view['invocation_id']}
    if not view['terminal']:
        return {**response, 'backup_status': 'unknown'}
    if not ((state.get('intent') or {}).get('status') == 'waiting' and (state.get('intent') or {}).get('launch')):
        defer_backup_admission(runtime, deployed_sha=view['deployed_sha'])
    reserved = []
    def reserve(previous):
        if not previous or previous['status'] != 'waiting' or previous['deployed_sha'] != view['deployed_sha']:
            raise BackupAdmissionStateError('backup request changed before dispatch reservation')
        launch = previous.get('launch')
        ack = previous.get('canonical_ack') or {}
        if launch:
            if (launch.get('unit') != UNIT or launch.get('deployed_sha') != view['deployed_sha']
                    or launch.get('execution_identity') != view['identity']
                    or type(launch.get('attempt')) is not int or not 1 <= launch['attempt'] < 64):
                raise BackupAdmissionStateError('backup launch identity/bound differs')
            # Only a positively acknowledged no-backup-effects attempt may be
            # dispatched again after the exact unit has definitively stopped.
            if not (ack.get('status') == 'deferred' and ack.get('mutation_count') == 0
                    and ack.get('launch_attempt') == launch['attempt']
                    and ack.get('invocation_id') and ack['invocation_id'] == view['invocation_id']):
                return None
        attempt = int((launch or {}).get('attempt', 0)) + 1
        proof = {'attempt': attempt, 'phase': 'prepared', 'unit': UNIT,
                 'deployed_sha': view['deployed_sha'], 'execution_identity': view['identity'],
                 'prepared_at': datetime.now(timezone.utc).isoformat(),
                 'prior_invocation_id': view['invocation_id'],
                 'pending_plan_fingerprint': (state.get('pending_transaction') or {}).get('plan_fingerprint')}
        reserved.append(attempt)
        return {**previous, 'launch': proof}
    reservation = _update(runtime, reserve)
    intent = _read_intent(runtime)
    if not reserved:
        return {**response, 'backup_status': 'outcome_unknown', 'request_id': intent['request_id']}
    # A natural timer can finish after reservation. Recheck both its invocation
    # and the exact prepared intent; observing idle alone cannot authorize start.
    recheck = service.inspect()
    dispatch_allowed = []
    observed_intent = []
    def verify_prepared(previous):
        observed_intent.append(previous)
        if (previous and previous.get('fingerprint') == reservation['fingerprint']
                and previous.get('request_id') == reservation['request_id']
                and previous['status'] == 'waiting'
                and previous.get('launch') == reservation['launch']
                and previous['launch']['phase'] == 'prepared'
                and recheck['identity'] == view['identity']
                and recheck['deployed_sha'] == view['deployed_sha']
                and recheck['terminal'] and not recheck['active']
                and recheck['invocation_id'] == view['invocation_id']):
            dispatch_allowed.append(True)
        return None
    _update(runtime, verify_prepared)
    if not dispatch_allowed:
        observed = observed_intent[0] or {}
        resolved = (observed.get('request_id') == reservation['request_id']
                    and observed.get('status') == 'resolved')
        return {**response, 'backup_status': 'resolved' if resolved else 'outcome_unknown',
                'request_id': reservation['request_id'],
                'canonical_ack': observed.get('canonical_ack') if resolved else None,
                'invocation_id': recheck['invocation_id']}
    # Do not hold the metadata lock across systemctl: the canonical service must
    # be able to acknowledge immediately. This is a checked external dispatch,
    # not an atomic transaction with a concurrent natural systemd timer.
    error = None
    try:
        service.start()
    except (OSError, subprocess.SubprocessError) as exc:
        error = type(exc).__name__
    def submitted(previous):
        if (not previous or previous.get('request_id') != reservation['request_id']
                or previous.get('fingerprint') != reservation['fingerprint']
                or previous.get('launch') != reservation['launch']):
            return None
        launch = {**previous['launch'], 'phase': 'outcome_unknown' if error else 'submitted'}
        if error:
            launch['dispatch_error'] = error
        return {**previous, 'launch': launch}
    _update(runtime, submitted)
    # Completion is acknowledged only by the canonical function, never systemctl
    # success/idle. Read-only response carries the same durable request.
    observed = service.inspect()
    final = _read_intent(runtime)
    if not final or final.get('request_id') != reservation['request_id']:
        return {**response, 'request_id': reservation['request_id'],
                'backup_status': 'outcome_unknown', 'invocation_id': observed['invocation_id']}
    return {**response, 'request_id': final['request_id'],
            'backup_status': 'resolved' if final['status'] == 'resolved' else 'outcome_unknown' if error else 'dispatched',
            'invocation_id': observed['invocation_id'], 'canonical_ack': final.get('canonical_ack')}
