"""One fixed, dormant full-cycle transport. IDs prove identity, never ownership.

Preparation/status are read-only. The sole launcher journals before its one POST;
unknown transport outcomes require the same readback, including after rollover.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from zoneinfo import ZoneInfo

from packages.application import business_data_schedule_profile as profile

PATH = '/v1/business-data-cycle/dispatch'
BASE_URL = 'http://127.0.0.1:8765'
APP = Path('/opt/wb-core-runtime/app')
RUNTIME = Path('/opt/wb-core-runtime/state')
HISTORY_CONTRACT = Path('artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json')
RECORD = '.business-data-cycle-dispatch.json'
LOCK = '.business-data-cycle-dispatch.lock'
MAXIMUM = 16384
TERMINAL = {'complete', 'degraded', 'failed', 'interrupted'}


def selected(runtime):
    return profile.load_selector(Path(runtime).resolve()) is not None


def legacy_refusal(runtime):
    if not selected(runtime):
        return None
    return {'status': 'cycle_managed', 'accepted': False, 'source_effects_started': False,
            'profile_id': profile.PROFILE_ID, 'message': 'This producer is managed by the full 3h cycle.'}


def require_source_dispatch(runtime):
    """Only an actual live same-process cycle lease exempts canonical stages."""
    if not selected(runtime):
        return
    from packages.application.business_data_heavy_admission import current_heavy_owner
    owner = current_heavy_owner(Path(runtime).resolve())
    if owner is None or owner.operation != 'cycle':
        raise RuntimeError('source producer is managed by the selected full 3h cycle')


def latest_slot(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError('cycle dispatch requires an aware server clock')
    local = now.astimezone(ZoneInfo('Asia/Yekaterinburg'))
    return local.replace(hour=local.hour // 3 * 3, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return 'sha256:' + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _encode(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _secret():
    # Same session-signing boundary as the fixed authenticated HTTP adapter.
    value = os.environ.get('WB_CORE_WEB_AUTH_SESSION_SECRET', '').strip()
    if not value:
        raise RuntimeError('cycle dispatch requires configured web authentication')
    return value.encode()


def _identity(runtime):
    if Path(runtime).resolve() != RUNTIME or Path(__file__).resolve().parents[2] != APP:
        raise RuntimeError('cycle dispatch requires the fixed deployed app/runtime')
    from packages.application.finance_backup_handoff import _safe_file
    sha = _safe_file(APP / '.wb-core-runtime-sha', maximum=128).decode().strip()
    if re.fullmatch('[0-9a-f]{40}', sha) is None:
        raise RuntimeError('cycle dispatch deployed identity is invalid')
    return sha


def history_config():
    from packages.application.finance_backup_handoff import _safe_file
    from packages.application.sheet_vitrina_v1_cycle import CycleHistoryConfig
    contract = APP / HISTORY_CONTRACT
    value = json.loads(_safe_file(contract, maximum=65536))
    return CycleHistoryConfig(candidate_root=Path(value['candidate_root']), runtime_contract=contract,
                              formula_epoch=value['formula_epoch'], budget_seconds=240, max_recomputes=31)


def _decode(dispatch_id):
    if not isinstance(dispatch_id, str) or len(dispatch_id) > 2048 or dispatch_id.count('.') != 1:
        raise ValueError('invalid cycle dispatch identity')
    body, signature = dispatch_id.split('.')
    expected = _encode(hmac.digest(_secret(), body.encode(), 'sha256'))
    if not hmac.compare_digest(signature, expected):
        raise ValueError('invalid cycle dispatch signature')
    try:
        value = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('invalid cycle dispatch envelope') from exc
    if (not isinstance(value, dict) or set(value) != {'schema', 'profile', 'deployed_sha', 'slot', 'history', 'request_key', 'request_fingerprint', 'cycle_id'}
            or value['schema'] != 1 or value['profile'] != profile.fingerprint(profile.PROFILE)
            or re.fullmatch('[0-9a-f]{40}', str(value['deployed_sha'])) is None
            or re.fullmatch('[0-9a-f]{32}', str(value['cycle_id'])) is None):
        raise ValueError('invalid fixed cycle dispatch envelope')
    return value


def prepare(runtime, now):
    """Bounded metadata and diagnostics; observing a debt never launches it."""
    from packages.application.business_data_cycle_wakeup import diagnostics
    result = _prepare(runtime, now)
    wakeup = diagnostics(runtime, now)
    return {**result, 'maintenance_wakeup': wakeup} if wakeup else result


def _prepare(runtime, now):
    """No lock, directory, receipt, constructor or source acceptance."""
    sha = _identity(runtime)
    if not selected(runtime):
        return {'status': 'legacy_profile', 'accepted': False}
    readiness = profile.activation_readiness(Path(runtime))
    states = profile.effective_core(Path(runtime))
    if not readiness['ready'] or states[profile.WAREHOUSE_TIMER][0] != 'enabled':
        return {'status': 'blocked', 'accepted': False, 'blockers': readiness['blockers'] or ['master_owner_disabled']}
    from packages.application.business_data_cycle_wakeup import policy, catchup_slot
    refusal = policy(runtime, now)
    if refusal:
        return {'status': refusal, 'accepted': False, 'source_effects_started': False}
    if catchup_slot(runtime, now):
        from packages.application.business_data_heavy_admission import heavy_admission_status
        from packages.application.finance_backup_handoff import cycle_backup_priority
        if not heavy_admission_status(runtime)['idle']:
            return {'status': 'busy', 'accepted': False, 'source_effects_started': False}
        if cycle_backup_priority(runtime, now=now)['priority']:
            return {'status': 'backup_priority', 'accepted': False, 'source_effects_started': False}
    config = history_config()
    slot = latest_slot(now)
    scope = {'schema': 1, 'profile': profile.fingerprint(profile.PROFILE), 'deployed_sha': sha,
             'slot': slot, 'history': config.fingerprint()}
    key = 'selected-cycle:' + hashlib.sha256(_canonical(scope).encode()).hexdigest()
    from packages.application.sheet_vitrina_v1_cycle import CycleReceiptStore, CycleConflict
    store = CycleReceiptStore(runtime, lambda: '')
    try:
        prior = store.matching(request_key=key, slot_utc=slot, config=config)
    except CycleConflict:
        return {'status': 'blocked', 'accepted': False, 'blockers': ['slot_owned_by_another_contract']}
    if prior is not None and prior['request_key'] != key:
        # A deploy in an already owned slot must not journal a fresh uncertain
        # identity. Wait for a future slot; never attach the old request's ACK.
        return {'status': 'blocked', 'accepted': False, 'blockers': ['slot_owned_by_another_deployed_request']}
    _, request_fp, cycle_id, _ = store._request_identity(request_key=key, slot_utc=slot, config=config)
    scope.update(request_key=key, request_fingerprint=request_fp, cycle_id=cycle_id)
    body = _encode(_canonical(scope).encode())
    identity = body + '.' + _encode(hmac.digest(_secret(), body.encode(), 'sha256'))
    return {'status': 'prepared', 'accepted': False, 'dispatch_id': identity}


def readback(runtime, dispatch_id):
    _identity(runtime)
    value = _decode(dispatch_id)
    from packages.application.sheet_vitrina_v1_cycle import CycleReceiptStore
    receipt = CycleReceiptStore(runtime, lambda: '').read(value['cycle_id'])
    if receipt is None:
        return {'status': 'unknown', 'accepted': None, 'dispatch_id': dispatch_id}
    if receipt['request_key'] != value['request_key'] or receipt['request_fingerprint'] != value['request_fingerprint']:
        raise RuntimeError('cycle dispatch receipt identity changed')
    return {'status': receipt['status'], 'accepted': True, 'dispatch_id': dispatch_id, 'receipt': {key: receipt.get(key) for key in ('cycle_id', 'request_key', 'request_fingerprint', 'job_id', 'status', 'current_stage', 'error_code')}}


def _still_current(entrypoint, value):
    # Called only inside canonical acceptance serialization, after actual EX.
    return (value['deployed_sha'] == _identity(entrypoint.runtime.runtime_dir)
            and value['history'] == history_config().fingerprint()
            and selected(entrypoint.runtime.runtime_dir)
            and value['slot'] == latest_slot(entrypoint.now_factory()))


def readback_fenced(entrypoint, dispatch_id):
    """Absent+expired proof shares actual acceptance EX, without provisioning it."""
    runtime = Path(entrypoint.runtime.runtime_dir).resolve()
    value = _decode(dispatch_id)
    from packages.application.business_data_heavy_admission import LOCK_FILENAME, _private_descriptor
    with entrypoint.operator_jobs._lock:
        fd = None
        try:
            try:
                fd = _private_descriptor(runtime / LOCK_FILENAME, create=False)
            except (OSError, RuntimeError):
                # Missing is not never-created: a locked inode may have been
                # removed/replaced. Read exact ACK if present; absence is unknown.
                return readback(runtime, dispatch_id)
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return readback(runtime, dispatch_id)  # absent remains unknown
            result = readback(runtime, dispatch_id)
            if result['accepted'] is None and not _still_current(entrypoint, value):
                # Recheck after all bounded metadata reads, while EX still held.
                # A replacement pathname must never inherit old-inode authority.
                try:
                    actual = (runtime / LOCK_FILENAME).lstat()
                    observed = os.fstat(fd)
                except OSError:
                    return result
                if (not stat.S_ISREG(actual.st_mode) or actual.st_mode & 0o077
                        or (actual.st_dev, actual.st_ino) != (observed.st_dev, observed.st_ino)):
                    return result
                return {'status': 'expired_not_accepted', 'accepted': False, 'dispatch_id': dispatch_id}
            return result
        finally:
            if fd is not None:
                os.close(fd)


def dispatch(entrypoint, payload):
    if not isinstance(payload, dict) or set(payload) != {'dispatch_id'}:
        raise ValueError('only a server-issued cycle dispatch identity is accepted')
    runtime = entrypoint.runtime.runtime_dir
    value = _decode(payload['dispatch_id'])
    prior = readback(runtime, payload['dispatch_id'])
    if prior['accepted'] is True:
        return prior
    current = prepare(runtime, entrypoint.now_factory())
    if current.get('dispatch_id') != payload['dispatch_id']:
        return {'status': 'not_accepted', 'accepted': False, 'reason': current['status'], 'dispatch_id': payload['dispatch_id']}
    def admission_guard():
        from packages.application.business_data_cycle_wakeup import policy
        return (_still_current(entrypoint, value)
                and policy(runtime, entrypoint.now_factory, expected_slot=value['slot']) is None)
    result = entrypoint._start_sheet_cycle_job(request_key=value['request_key'], slot_utc=value['slot'], history_config=history_config(),
        _dispatch_guard=admission_guard)
    # Actual acceptance is proved by the canonical receipt, not a matching job name.
    proof = readback(runtime, payload['dispatch_id'])
    if proof['accepted'] is True:
        return proof
    if result.get('accepted') is False or result.get('single_flight'):
        return {'status': 'not_accepted', 'accepted': False, 'dispatch_id': payload['dispatch_id'], 'reason': result.get('status', 'busy')}
    return proof


def _read_record(runtime):
    try:
        fd = os.open(runtime / RECORD, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > MAXIMUM:
            raise RuntimeError('cycle transport record exceeds private bound')
        result = json.loads(os.read(fd, MAXIMUM + 1))
    finally:
        os.close(fd)
    if (not isinstance(result, dict) or set(result) != {'schema', 'dispatch_id', 'phase', 'last_status'}
            or result['schema'] != 1 or result['phase'] not in {'uncertain', 'accepted', 'terminal'}
            or not isinstance(result['last_status'], str) or len(result['last_status']) > 128):
        raise RuntimeError('cycle transport record is invalid')
    _decode(result['dispatch_id'])
    return result


def _write_record(runtime, value):
    raw = _canonical(value).encode()
    if len(raw) > MAXIMUM:
        raise RuntimeError('cycle transport record exceeds bound')
    fd, name = tempfile.mkstemp(prefix=RECORD + '.', dir=runtime)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, runtime / RECORD)
        directory = os.open(runtime, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _launcher_lock(runtime):
    # Only the selected accepted launcher provisions its one private lock.
    fd = os.open(runtime / LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise RuntimeError('cycle launcher lock is unsafe')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _request(method, dispatch_id=None):
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode
    from apps.sheet_vitrina_v1_auto_refresh_tick import _build_web_auth_cookie
    url = BASE_URL + PATH
    data = None
    if method == 'POST':
        data = _canonical({'dispatch_id': dispatch_id}).encode()
    elif dispatch_id:
        url += '?' + urlencode({'dispatch_id': dispatch_id})
    request = Request(url, data=data, method=method, headers={'Cookie': _build_web_auth_cookie(os.environ), 'Content-Type': 'application/json'})
    with urlopen(request, timeout=15) as response:
        raw = response.read(MAXIMUM + 1)
    if len(raw) > MAXIMUM:
        raise RuntimeError('cycle dispatch response exceeds bound')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError('cycle dispatch response is invalid')
    return value


def launch(runtime):
    """Fixed warehouse launcher: short maintenance SH, no heavy/domain lease."""
    runtime = Path(runtime).resolve()
    _identity(runtime)
    if not selected(runtime):
        raise RuntimeError('selected cycle launcher cannot dispatch the legacy profile')
    try:
        with _launcher_lock(runtime):
            record = _read_record(runtime)
            if record and record['phase'] != 'terminal':
                # Resolve this same operation first. Unknown/active never
                # authorizes another slot or resend of the old POST.
                try:
                    result = _request('GET', record['dispatch_id'])
                except Exception:
                    return {'status': 'unknown', 'accepted': None, 'dispatch_id': record['dispatch_id']}
                if result.get('dispatch_id') != record['dispatch_id']:
                    raise RuntimeError('cycle transport readback differs')
                terminal = (result.get('accepted') is True and result.get('status') in TERMINAL) or (
                    result.get('accepted') is False and result.get('status') == 'expired_not_accepted')
                phase = 'terminal' if terminal else ('accepted' if result.get('accepted') is True else 'uncertain')
                record = dict(record, phase=phase, last_status=result.get('status', 'unknown'))
                _write_record(runtime, record)
                if not terminal:
                    return result
                # A positively terminal old operation must not consume the
                # sole next 3h timer invocation. Prepare once below, then at
                # most one POST for a different server-issued slot identity.
            prepared = _request('GET')
            if prepared.get('status') != 'prepared':
                return prepared
            identity = prepared['dispatch_id']
            _decode(identity)
            if record and record['dispatch_id'] == identity:
                return _request('GET', identity)
            # Durable write precedes send; process death before send is conservatively uncertain.
            record = {'schema': 1, 'dispatch_id': identity, 'phase': 'uncertain', 'last_status': 'unknown'}
            _write_record(runtime, record)
            try:
                result = _request('POST', identity)
            except Exception:
                return {'status': 'unknown', 'accepted': None, 'dispatch_id': identity}
            if result.get('dispatch_id') != identity:
                raise RuntimeError('cycle transport acceptance identity differs')
            known_noaccept = result.get('accepted') is False and result.get('status') == 'not_accepted'
            terminal = known_noaccept or (result.get('accepted') is True and result.get('status') in TERMINAL)
            phase = 'terminal' if terminal else ('accepted' if result.get('accepted') is True else 'uncertain')
            _write_record(runtime, dict(record, phase=phase, last_status=result.get('status', 'unknown')))
            return result
    except BlockingIOError:
        return {'status': 'busy', 'accepted': False, 'source_effects_started': False}
