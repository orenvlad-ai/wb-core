"""Exact timer restore after one canonical, completed deploy formula-pin delta.

No unit, formula, owner policy or baseline writes. Ordinary exact-prior resume
remains strict. This separate authority owns only the existing restore/barrier
locks, private proof and restoration of original timer enabled/active pairs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
from datetime import datetime

from packages.application import business_data_maintenance_pause as pause
from packages.application import business_data_deploy_protection as deploy
from packages.application import business_data_write_barrier as barrier
from packages.application import business_data_schedule_profile as schedule

UNIT = 'wb-core-web-vitrina-finished-snapshot.service'
UNIT_RELATIVE = 'artifacts/registry_upload_http_entrypoint/systemd/' + UNIT
CONTRACT_RELATIVE = 'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json'
TARGET_RELATIVE = 'artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json'
DIRECTORY = '.business-data-formula-resume'
PLAN_SCHEMA = 'business_data_formula_resume_plan_v1'
STATE_SCHEMA = 'business_data_formula_resume_transition_v1'
RECEIPT_SCHEMA = 'business_data_formula_resume_receipt_v1'
PHASES = {'prepared', 'restoring', 'committed', 'released'}
EPOCH = r'wbc0069k16-reviewed-native-v1:[0-9a-f]{64}'
PIN = re.compile(r'--formula-epoch (' + EPOCH + r')(?=\s|;|$)')
CONFIG_PROPERTIES = ('FragmentPath', 'DropInPaths', 'ExecStart', 'Triggers', 'Persistent',
    'TimersCalendar', 'TimersMonotonic', 'AccuracyUSec', 'RandomizedDelayUSec', 'RemainAfterElapse')


def _validate_duration(value):
    if value == '0':
        return
    # systemd usec_t is uint64; UINT64_MAX is the unsupported infinity sentinel.
    # Native constants: systemd/v255/src/basic/time-util.h (month = 2629800s).
    units = {'month': 2629800000000, 'w': 604800000000, 'd': 86400000000,
             'h': 3600000000, 'min': 60000000, 's': 1000000, 'ms': 1000, 'us': 1}
    if len(value) > 256:
        raise RuntimeError('formula resume invalid loaded duration')
    total, previous = 0, -1
    for token in value.split(' '):
        match = re.fullmatch(r'(0|[1-9][0-9]{0,19})(?:\.([0-9]{1,6}))?(month|w|d|h|min|s|ms|us)', token)
        if match is None:
            raise RuntimeError('formula resume invalid loaded duration')
        whole, fraction, unit = match.groups()
        rank = tuple(units).index(unit)
        if rank <= previous:
            raise RuntimeError('formula resume invalid loaded duration')
        previous = rank
        usecs = int(whole) * units[unit]
        if fraction:
            numerator, denominator = int(fraction) * units[unit], 10 ** len(fraction)
            if numerator % denominator:
                raise RuntimeError('formula resume invalid loaded duration')
            usecs += numerator // denominator
        if usecs == 0 or total + usecs >= (1 << 64) - 1:
            raise RuntimeError('formula resume invalid loaded duration')
        total += usecs


def _validate_calendar(value):
    # Only the normalized operand families present in our native readback.
    # Validation never rewrites/sorts the retained static schedule text.
    parts = value.split(' ')
    if len(parts) not in {2, 3} or (len(parts) == 3 and parts[2] not in {
            'UTC', 'Europe/Moscow', 'Asia/Yekaterinburg', 'Asia/Tbilisi'}):
        raise RuntimeError('formula resume unsupported loaded calendar')
    if parts[0] != '*-*-*':
        try:
            if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', parts[0]) is None or datetime.strptime(parts[0], '%Y-%m-%d').strftime('%Y-%m-%d') != parts[0]:
                raise ValueError('invalid date')
        except ValueError:
            raise RuntimeError('formula resume invalid loaded calendar date') from None
    fields = parts[1].split(':')
    if len(fields) != 3:
        raise RuntimeError('formula resume unsupported loaded calendar')
    for index, (field, maximum) in enumerate(zip(fields, (23, 59, 59))):
        if field == '*':
            continue
        if '/' in field:
            match = re.fullmatch(r'([0-9]{2})/([1-9][0-9]?)', field)
            if index != 0 or match is None or int(match[1]) > maximum or int(match[2]) > maximum:
                raise RuntimeError('formula resume invalid loaded calendar range')
        else:
            if re.fullmatch(r'[0-9]{2}(?:,[0-9]{2})*', field) is None:
                raise RuntimeError('formula resume invalid loaded calendar range')
            values = [int(v) for v in field.split(',')]
            if any(v > maximum for v in values) or values != sorted(set(values)):
                raise RuntimeError('formula resume invalid loaded calendar range')


def _loaded_records(value, property_name):
    """Parse the complete native serialization, retaining every static operand.

    Only documented runtime tails are separated. Unknown shapes do not fall
    back to raw equality, even when two malformed observations are identical.
    """
    if not isinstance(value, str) or len(value) > 65536 or re.search(r'[\x00-\x1f\x7f]', value):
        raise RuntimeError('formula resume malformed loaded property: ' + property_name)
    if value == '':
        return ()
    duration = r'(?:0|(?:[0-9]+(?:\.[0-9]+)?(?:month|w|d|h|min|s|ms|us)(?: [0-9]+(?:\.[0-9]+)?(?:month|w|d|h|min|s|ms|us))*))'
    timestamp = r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} UTC'
    if property_name == 'ExecStart':
        pattern = (r'\{ path=(?P<path>[^;{}]+) ; argv\[\]=(?P<argv>[^;{}]+) ; ignore_errors=(?P<ignore>yes|no)'
            r' ; start_time=\[(?P<start>n/a|' + timestamp + r')\] ; stop_time=\[(?P<stop>n/a|' + timestamp + r')\]'
            r' ; pid=(?P<pid>0|[1-9][0-9]*) ; code=(?P<code>\(null\)|exited|killed|dumped)'
            r' ; status=(?P<status>[0-9]+(?:/[0-9]+)?) \}')
    elif property_name == 'TimersCalendar':
        pattern = r'\{ OnCalendar=(?P<operand>[^;{}]+) ; next_elapse=(?P<next>\(null\)|' + timestamp + r') \}'
    elif property_name == 'TimersMonotonic':
        pattern = r'\{ (?P<trigger>OnActiveUSec|OnBootUSec|OnStartupUSec|OnUnitActiveUSec|OnUnitInactiveUSec)=(?P<operand>' + duration + r') ; next_elapse=(?P<next>' + duration + r') \}'
    else:
        raise RuntimeError('formula resume unknown loaded property')
    result, offset = [], 0
    while offset < len(value):
        match = re.compile(pattern).match(value, offset)
        if match is None:
            raise RuntimeError('formula resume malformed loaded property: ' + property_name)
        fields = match.groupdict()
        for field in ('start', 'stop', 'next'):
            observed = fields.get(field, '')
            if re.fullmatch(timestamp, observed):
                try:
                    parsed = datetime.strptime(observed, '%a %Y-%m-%d %H:%M:%S UTC')
                    if parsed.strftime('%a %Y-%m-%d %H:%M:%S UTC') != observed:
                        raise ValueError('weekday mismatch')
                except ValueError:
                    raise RuntimeError('formula resume invalid loaded timestamp') from None
        if property_name == 'ExecStart':
            if not fields['path'].startswith('/') or fields['path'].strip() != fields['path'] or fields['argv'].strip() != fields['argv']:
                raise RuntimeError('formula resume invalid loaded command')
            if len(fields['pid']) > 10 or int(fields['pid']) > 2147483647 or any(
                    len(v) > 3 or int(v) > 255 for v in fields['status'].split('/')):
                raise RuntimeError('formula resume invalid loaded execution number')
            if fields['code'] == '(null)':
                if (fields['pid'], fields['start'], fields['stop'], fields['status']) != ('0', 'n/a', 'n/a', '0/0'):
                    raise RuntimeError('formula resume unproven loaded execution state')
            elif (fields['pid'] == '0' or 'n/a' in (fields['start'], fields['stop'])
                    or datetime.strptime(fields['stop'], '%a %Y-%m-%d %H:%M:%S UTC') < datetime.strptime(fields['start'], '%a %Y-%m-%d %H:%M:%S UTC')):
                raise RuntimeError('formula resume unproven loaded execution state')
            static = (fields['path'], fields['argv'], fields['ignore'])
        elif property_name == 'TimersCalendar':
            # Native systemctl expands these captured calendars to date/time
            # operands; retain their exact timezone and all schedule tokens.
            _validate_calendar(fields['operand'])
            static = ('OnCalendar', fields['operand'])
        else:
            _validate_duration(fields['operand'])
            _validate_duration(fields['next'])
            static = (fields['trigger'], fields['operand'])
        if static in result:
            raise RuntimeError('formula resume duplicate loaded record')
        result.append(static)
        offset = match.end()
        if offset < len(value):
            if value[offset:offset + 2] != ' {':
                raise RuntimeError('formula resume extra loaded tokens')
            offset += 1
    return tuple(result)


def _loaded_configuration(properties):
    result = {}
    for key in CONFIG_PROPERTIES:
        present = key in properties
        value = properties.get(key)
        if present and key in {'ExecStart', 'TimersCalendar', 'TimersMonotonic'}:
            value = _loaded_records(value, key)
        result[key] = (present, value)  # Missing and empty are different evidence.
    return result


def _same_unit_configuration(original, actual):
    left, right = original.get('properties') or {}, actual.get('properties') or {}
    digest = left.get('UnitContentDigest')
    if not isinstance(digest, str) or re.fullmatch(r'sha256:[0-9a-f]{64}', digest) is None:
        raise RuntimeError('formula resume loaded content digest absent/invalid')
    for properties in (left, right):
        if (not isinstance(properties.get('FragmentPath'), str) or not properties['FragmentPath'].startswith('/')
                or not isinstance(properties.get('DropInPaths'), str)):
            raise RuntimeError('formula resume loaded fragment/dropin evidence absent')
    return digest == right.get('UnitContentDigest') and _loaded_configuration(left) == _loaded_configuration(right)


def _bytes(path: Path, bound: int = 1024 * 1024) -> bytes:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeError('formula resume evidence path is not exact/non-symlink')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > bound:
            raise RuntimeError('formula resume evidence exceeds regular-file bound')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(bound + 1)
        if len(data) > bound:
            raise RuntimeError('formula resume evidence exceeds read bound')
        return data
    finally:
        os.close(fd)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _path(runtime, operation_id):
    if barrier._validate_identifier(operation_id, label='formula resume operation') != operation_id:
        raise RuntimeError('formula resume operation must be exact')
    root = runtime / DIRECTORY
    if any(p.is_symlink() for p in (root, *root.parents)) or (root.exists() and (
            not root.is_dir() or root.stat().st_mode & 0o077)):
        raise RuntimeError('formula resume directory is unsafe')
    return root / (operation_id + '.json')


def load(runtime, operation_id):
    value = schedule.read_private(_path(Path(runtime).resolve(), operation_id), optional=True)
    if value is None:
        return None
    if (value.get('schema_version') != STATE_SCHEMA or value.get('phase') not in PHASES
            or value.get('fingerprint') != pause.fingerprint({k: v for k, v in value.items() if k != 'fingerprint'})):
        raise RuntimeError('formula resume transition is invalid')
    plan = value.get('plan') or {}
    if (plan.get('schema_version') != PLAN_SCHEMA or plan.get('operation_id') != operation_id
            or plan.get('runtime_dir') != str(Path(runtime).resolve())
            or plan.get('fingerprint') != pause.fingerprint({k: v for k, v in plan.items() if k != 'fingerprint'})
            or plan.get('baseline_fingerprint') != pause.fingerprint(plan.get('baseline'))):
        raise RuntimeError('formula resume immutable plan is invalid')
    if value['phase'] == 'released':
        prove_recorded_release(value, value.get('release_proof'))
    return value


def assert_no_partial_transition(runtime, *, recovering_operation=''):
    runtime = Path(runtime).resolve()
    root = _path(runtime, 'inventory-probe').parent
    if not root.exists():
        return
    for index, path in enumerate(root.iterdir()):
        if index >= 128 or path.suffix != '.json':
            raise RuntimeError('formula resume transition inventory is unsafe/excessive')
        if load(runtime, path.stem)['phase'] != 'released' and path.stem != recovering_operation:
            raise RuntimeError('formula resume requires exact same-operation recovery')


def _save(runtime, state, event):
    path = _path(runtime, state['plan']['operation_id'])
    # Refuse foreign state or symlink replacement even for a terminal checkpoint.
    previous = load(runtime, state['plan']['operation_id'])
    if previous and (previous['plan'] != state['plan'] or previous['actor'] != state['actor']):
        raise RuntimeError('formula resume frozen identity differs')
    state['updated_at'] = pause.now_iso()
    state['fingerprint'] = pause.fingerprint({k: v for k, v in state.items() if k != 'fingerprint'})
    if len(barrier._canonical_json(state).encode()) > 2 * 1024 * 1024:
        raise RuntimeError('formula resume durable proof exceeds bound')
    audit = runtime / '.business-data-formula-resume-audit.jsonl'
    if audit.is_symlink() or (audit.exists() and (not audit.is_file() or audit.stat().st_mode & 0o077)):
        raise RuntimeError('formula resume audit is unsafe')
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700)
    barrier._atomic_write_private_json(path, state)
    barrier._append_private_audit(audit, dict(event=event, operation_id=state['plan']['operation_id'],
        phase=state['phase'], captured_at=state['updated_at'], plan_fingerprint=state['plan']['fingerprint'],
        proof=state['plan'] if event == 'prepared' else state.get('receipt') if event == 'committed' else None))


def _authority(runtime, operation_id, window_id, expected_sha, app, unit_directory, systemd):
    deploy._sha(expected_sha)
    owner = deploy.load(runtime)
    if (not owner or owner['phase'] != 'complete' or owner['operation_id'] != operation_id
            or owner['window_id'] != window_id or owner['expected_sha'] != expected_sha):
        raise RuntimeError('formula resume requires same completed canonical deploy owner')
    marker = _bytes(app / '.wb-core-runtime-sha', 256)
    metadata_bytes = _bytes(app / '.wb-core-deploy.json', 16384)
    metadata = json.loads(metadata_bytes)
    if (marker.decode().strip() != expected_sha or metadata.get('schema_version') != 'wb_core_deploy_metadata_v2'
            or metadata.get('commit') != expected_sha or metadata.get('deployment_complete') is not True):
        raise RuntimeError('formula resume completed runtime markers differ')
    target_bytes = _bytes(app / TARGET_RELATIVE)
    target = json.loads(target_bytes)
    if (target.get('target_status') != 'active' or target.get('target_id') != 'wb_core_eu_hosted_runtime_active'
            or target.get('target_dir') != str(app) or target.get('systemd_unit_directory') != str(unit_directory)
            or (target.get('runtime_env') or {}).get('REGISTRY_UPLOAD_RUNTIME_DIR') != str(runtime)
            or target.get('systemd_units_source_dir') != 'artifacts/registry_upload_http_entrypoint/systemd'
            or not any(v.get('name') == UNIT for v in target.get('managed_systemd_units', []))):
        raise RuntimeError('formula resume canonical europe managed target differs')
    registry = systemd.unit_state('wb-core-registry-http.service')
    if (registry.get('is_active') != 'active' or (registry.get('properties') or {}).get('LoadState') != 'loaded'
            or int((registry.get('properties') or {}).get('MainPID') or 0) <= 0):
        raise RuntimeError('formula resume live registry proof is absent')
    contract_bytes = _bytes(app / CONTRACT_RELATIVE)
    contract = json.loads(contract_bytes)
    hashes = contract.get('formula_code_hashes')
    if not isinstance(hashes, dict) or not 1 <= len(hashes) <= 128:
        raise RuntimeError('formula resume formula hash set is invalid')
    total = 0
    for relative, expected in hashes.items():
        path = Path(relative)
        if (path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'packages'
                or re.fullmatch('[0-9a-f]{64}', str(expected)) is None):
            raise RuntimeError('formula resume formula path/hash is invalid')
        data = _bytes(app / path, 16 * 1024 * 1024)
        total += len(data)
        if total > 64 * 1024 * 1024 or _hash(data) != expected:
            raise RuntimeError('formula resume actual formula source hash differs/bound exceeded')
    # This is the native history formula-epoch algorithm, with no mount/write work.
    epoch = 'wbc0069k16-reviewed-native-v1:' + _hash(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode())
    if contract.get('formula_epoch') != epoch:
        raise RuntimeError('formula resume current formula epoch differs')
    return dict(owner=owner, marker_sha256=_hash(marker), metadata_sha256=_hash(metadata_bytes),
        target_sha256=_hash(target_bytes), contract_sha256=_hash(contract_bytes), formula_code_hashes=hashes,
        new_epoch=epoch, registry_main_pid=int(registry['properties']['MainPID']))


def _delta(baseline, current, app, unit_directory, epoch):
    original = baseline['units'][UNIT]['properties']
    actual = current['units'][UNIT]['properties']
    fragment = unit_directory / UNIT
    if (original.get('FragmentPath') != str(fragment) or actual.get('FragmentPath') != str(fragment)
            or original.get('DropInPaths', '') != actual.get('DropInPaths', '')
            or not original.get('UnitContentDigest')):
        raise RuntimeError('formula resume baseline fragment/dropin/digest binding differs')
    old_matches = PIN.findall(original.get('ExecStart', ''))
    new_matches = PIN.findall(actual.get('ExecStart', ''))
    if len(old_matches) != 1 or new_matches != [epoch] or old_matches[0] == epoch:
        raise RuntimeError('formula resume requires exactly one changed loaded epoch')
    old = old_matches[0]
    try:
        _loaded_records(original['ExecStart'], 'ExecStart')
        _loaded_records(actual['ExecStart'], 'ExecStart')
    except RuntimeError as exc:
        raise RuntimeError('formula resume loaded ExecStart has another delta') from exc
    before_config, after_config = _loaded_configuration(original), _loaded_configuration(actual)
    restored = tuple((path, argv.replace(epoch, old, 1), ignore)
                     for path, argv, ignore in after_config['ExecStart'][1])
    if restored != before_config['ExecStart'][1]:
        raise RuntimeError('formula resume loaded ExecStart has another delta')
    for key in CONFIG_PROPERTIES:
        if key != 'ExecStart' and after_config[key] != before_config[key]:
            raise RuntimeError('formula resume non-epoch loaded property differs: ' + key)
    installed = _bytes(fragment)
    if installed != _bytes(app / UNIT_RELATIVE):
        raise RuntimeError('formula resume installed fragment is not canonical deploy artifact')
    literal = ('--formula-epoch ' + epoch).encode()
    if installed.count(literal) != 1 or PIN.findall(installed.decode()) != [epoch]:
        raise RuntimeError('formula resume canonical fragment pin is not singular')
    reconstructed = installed.replace(literal, ('--formula-epoch ' + old).encode(), 1)
    paths = [fragment, *map(Path, shlex.split(actual.get('DropInPaths', '')))]
    if len(set(paths)) != len(paths) or len(paths) > 32:
        raise RuntimeError('formula resume loaded content inventory is invalid')
    after = [dict(path=str(p), sha256=_hash(installed if p == fragment else _bytes(p))) for p in paths]
    before = [dict(v, sha256=_hash(reconstructed)) if v['path'] == str(fragment) else v for v in after]
    if (pause.fingerprint(before) != original['UnitContentDigest']
            or pause.fingerprint(after) != actual.get('UnitContentDigest')):
        raise RuntimeError('formula resume aggregate baseline reconstruction differs')
    return dict(unit=UNIT, old_epoch=old, new_epoch=epoch, old_fragment_sha256=_hash(reconstructed),
        installed_fragment_sha256=_hash(installed), before_contents=before, after_contents=after,
        old_unit_digest=pause.fingerprint(before), new_unit_digest=pause.fingerprint(after))


def _observe(runtime, plan, systemd, activity_reader, proc_root, *, final=False):
    old = pause.load_state(runtime)
    if (not old or old['window_id'] != plan['window_id'] or old['baseline'] != plan['baseline']
            or old['baseline_fingerprint'] != plan['baseline_fingerprint']):
        raise RuntimeError('formula resume original pause baseline/window differs')
    held_services = {u: [v['is_enabled'], v['is_active']] for u, v in
        (old.get('hold_readback') or {}).get('units', {}).items() if u.endswith('.service')}
    if held_services != plan.get('held_service_states') or set(held_services) != {
            u for u in plan['baseline']['units'] if u.endswith('.service')}:
        raise RuntimeError('formula resume exact held service-state proof differs')
    current = pause.readback(runtime, systemd=systemd, activity_reader=activity_reader, proc_root=proc_root)
    if (current['controls'] != plan['baseline']['controls'] or not current['admission']['ready']
            or not current['admission']['idle'] or current['activity']['jobs']
            or current['writer_processes'] or current['live_services']):
        raise RuntimeError('formula resume raw controls/idle writer proof differs')
    for unit, original in plan['baseline']['units'].items():
        actual = current['units'][unit]
        if unit != UNIT:
            try:
                same = _same_unit_configuration(original, actual)
            except RuntimeError as exc:
                raise RuntimeError('formula resume foreign unit configuration differs: ' + unit) from exc
            if not same:
                raise RuntimeError('formula resume foreign unit configuration differs: ' + unit)
        if unit.endswith('.timer'):
            pair = [original['is_enabled'], original['is_active']]
            allowed_enabled = {pair[0]} if final else {'disabled', pair[0]}
            allowed_active = {pair[1]} if final else {'inactive', pair[1]}
            if actual['is_enabled'] not in allowed_enabled or actual['is_active'] not in allowed_active:
                raise RuntimeError('formula resume timer state is outside exact restore: ' + unit)
        elif [actual['is_enabled'], actual['is_active']] != held_services[unit]:
            raise RuntimeError('formula resume held service enabled/activity differs: ' + unit)
    app, directory = Path(plan['app_dir']), Path(plan['unit_directory'])
    authority = _authority(runtime, plan['operation_id'], plan['window_id'], plan['expected_sha'], app, directory, systemd)
    if authority != plan['authority'] or _delta(plan['baseline'], current, app, directory, authority['new_epoch']) != plan['delta']:
        raise RuntimeError('formula resume exact deployment/configuration proof changed')
    return old, current


def preview(runtime_dir, *, operation_id, window_id, expected_sha, app_dir,
            unit_directory=Path('/etc/systemd/system'), systemd, activity_reader, proc_root=Path('/proc')):
    runtime, app, directory = Path(runtime_dir).resolve(), Path(app_dir).absolute(), Path(unit_directory).absolute()
    _path(runtime, operation_id)
    schedule.assert_no_partial_transition(runtime)
    assert_no_partial_transition(runtime)
    old = pause.load_state(runtime)
    held = barrier.barrier_status(runtime)
    if (not old or old['phase'] != 'held' or old['window_id'] != window_id
            or not held.get('active') or held.get('phase') != 'held' or not held.get('hold_confirmed')
            or held.get('window_kind') != 'maintenance_pause' or held.get('window_id') != window_id
            or held.get('plan_fingerprint') != old['baseline_fingerprint']):
        raise RuntimeError('formula resume requires exact held pause/barrier')
    current = pause.readback(runtime, systemd=systemd, activity_reader=activity_reader, proc_root=proc_root)
    if not current['quiet']:
        raise RuntimeError('formula resume preview requires complete held quiet proof')
    authority = _authority(runtime, operation_id, window_id, expected_sha, app, directory, systemd)
    if authority['owner']['baseline_fingerprint'] != old['baseline_fingerprint']:
        raise RuntimeError('formula resume deploy owner baseline differs')
    plan = dict(schema_version=PLAN_SCHEMA, operation_id=operation_id, window_id=window_id,
        runtime_dir=str(runtime), app_dir=str(app), unit_directory=str(directory), expected_sha=expected_sha,
        baseline=old['baseline'], baseline_fingerprint=old['baseline_fingerprint'], authority=authority,
        held_service_states={u: [v['is_enabled'], v['is_active']] for u, v in old['hold_readback']['units'].items()
            if u.endswith('.service')},
        delta=_delta(old['baseline'], current, app, directory, authority['new_epoch']))
    _observe(runtime, plan, systemd, activity_reader, proc_root)
    plan['fingerprint'] = pause.fingerprint(plan)
    return plan


def prove_committed(runtime, operation_id, *, systemd, activity_reader, proc_root=Path('/proc')):
    state = load(runtime, operation_id)
    if not state or state['phase'] not in {'committed', 'released'}:
        raise RuntimeError('formula resume committed transition is absent')
    old, current = _observe(runtime, state['plan'], systemd, activity_reader, proc_root, final=True)
    receipt = state.get('receipt') or {}
    if (receipt.get('schema_version') != RECEIPT_SCHEMA or receipt.get('status') != 'restored'
            or receipt.get('exact_target_state_restored') is not True or receipt.get('exact_prior_state_restored') is not False
            or receipt.get('operation_id') != operation_id or receipt.get('target_fingerprint') != state['plan']['fingerprint']
            or receipt.get('baseline_fingerprint') != old['baseline_fingerprint']
            or receipt.get('expected_sha') != state['plan']['expected_sha']
            or receipt.get('window_id') != old['window_id'] or receipt.get('delta') != state['plan']['delta']
            or receipt.get('control_signature') != pause.fingerprint(current['controls'])):
        raise RuntimeError('formula resume committed receipt differs')
    if set(receipt.get('units') or {}) != set(current['units']):
        raise RuntimeError('formula resume committed unit inventory differs')
    for unit, actual in current['units'].items():
        saved = (receipt.get('units') or {}).get(unit) or {}
        if not _same_unit_configuration(saved, actual):
            raise RuntimeError('formula resume committed unit receipt differs')
        if unit.endswith('.timer') and [saved.get('is_enabled'), saved.get('is_active')] != [actual['is_enabled'], actual['is_active']]:
            raise RuntimeError('formula resume committed timer receipt differs')
    return state, old, current


def prove_recorded_release(state, released_barrier):
    """Dated release evidence only. No current source, owner, service or idle read.

    After admission opens, legitimate work can change every live observation.
    This historical proof finishes metadata only, never re-restores/re-releases.
    """
    plan, receipt = state['plan'], state.get('receipt') or {}
    if (state['phase'] not in {'committed', 'released'}
            or receipt.get('schema_version') != RECEIPT_SCHEMA or receipt.get('status') != 'restored'
            or receipt.get('exact_target_state_restored') is not True or receipt.get('exact_prior_state_restored') is not False
            or receipt.get('operation_id') != plan['operation_id'] or receipt.get('window_id') != plan['window_id']
            or receipt.get('baseline_fingerprint') != plan['baseline_fingerprint']
            or receipt.get('target_fingerprint') != plan['fingerprint'] or receipt.get('expected_sha') != plan['expected_sha']
            or receipt.get('delta') != plan['delta']
            or not receipt.get('captured_at')
            or receipt.get('control_signature') != pause.fingerprint(plan['baseline']['controls'])):
        raise RuntimeError('formula resume historical receipt binding differs')
    proof = released_barrier or {}
    restore = proof.get('restore') or {}
    if (proof.get('schema_version') != barrier.SCHEMA_VERSION or proof.get('phase') != 'released'
            or proof.get('active') is not False or not proof.get('hold_confirmed')
            or proof.get('window_kind') != 'maintenance_pause' or proof.get('window_id') != plan['window_id']
            or proof.get('plan_fingerprint') != plan['baseline_fingerprint'] or proof.get('released_by') != state['actor']
            or not proof.get('released_at') or proof.get('state_fingerprint') != barrier._fingerprint(
                {k: v for k, v in proof.items() if k != 'state_fingerprint'})
            or restore.get('status') != 'restored' or restore.get('target_kind') != 'canonical_formula_epoch'
            or restore.get('exact_target_state_restored') is not True or restore.get('exact_prior_state_restored') is not False
            or restore.get('operation_id') != plan['operation_id'] or restore.get('target_fingerprint') != plan['fingerprint']
            or restore.get('readback_fingerprint') != barrier._fingerprint(receipt)):
        raise RuntimeError('formula resume exact historical barrier release differs')
    return receipt


def _complete_bookkeeping(runtime, state, released_barrier, fault):
    receipt = prove_recorded_release(state, released_barrier)
    old = pause.load_state(runtime)
    plan = state['plan']
    if (not old or old['window_id'] != plan['window_id'] or old['baseline'] != plan['baseline']
            or old['baseline_fingerprint'] != plan['baseline_fingerprint']):
        raise RuntimeError('formula resume original baseline changed before release bookkeeping')
    if old.get('restore_readback') != receipt:
        raise RuntimeError('formula resume retained pause receipt differs before bookkeeping')
    if old['phase'] != 'restored':
        if old['phase'] not in {'held', 'restoring'}:
            raise RuntimeError('formula resume original pause phase differs')
        old.update(phase='restored', restore_readback=receipt)
        pause.save_state(runtime, old, 'formula_target_released')
    fault('pause_restored')
    state.update(phase='released', release_proof=released_barrier)
    _save(runtime, state, 'released')
    return receipt


def _retain_pause_receipt(runtime, state, old):
    """Native wakeup reads this exact proof immediately after admission opens."""
    plan, receipt = state['plan'], state['receipt']
    if (old['window_id'] != plan['window_id'] or old['baseline'] != plan['baseline']
            or old['baseline_fingerprint'] != plan['baseline_fingerprint']
            or old['phase'] not in {'held', 'restoring'}):
        raise RuntimeError('formula resume pre-release pause binding differs')
    retained = old.get('restore_readback')
    if retained is not None and retained != receipt:
        raise RuntimeError('formula resume retained pause receipt differs')
    if retained is None:
        old['restore_readback'] = receipt
        # Phase/baseline remain original until release. This is the same native
        # proof-before-release ordering as ordinary exact-prior resume.
        pause.save_state(runtime, old, 'formula_target_receipt_retained')


def apply(runtime_dir, *, reviewed_plan, expected_fingerprint, actor, reason,
          systemd, activity_reader, proc_root=Path('/proc'), _fault=None):
    runtime, plan = Path(runtime_dir).resolve(), reviewed_plan
    actor = barrier._validate_actor(actor)
    if not str(reason).strip() or len(reason) > 1000:
        raise RuntimeError('formula resume audited bounded reason required')
    if (plan.get('schema_version') != PLAN_SCHEMA or plan.get('runtime_dir') != str(runtime)
            or plan.get('fingerprint') != expected_fingerprint or expected_fingerprint != pause.fingerprint(
                {k: v for k, v in plan.items() if k != 'fingerprint'})):
        raise RuntimeError('formula resume reviewed plan differs')
    fault = _fault or (lambda point: None)
    state = load(runtime, plan['operation_id'])
    if state and (state['plan'] != plan or state['actor'] != actor):
        raise RuntimeError('formula resume same-operation identity differs')
    if state and state['phase'] == 'released':
        # A later lawful window/deploy/cycle is independent. Return the saved
        # dated receipt, not a false claim about current timers/configuration.
        return prove_recorded_release(state, state.get('release_proof'))
    with pause._ExclusiveRestoreLock(runtime):
        schedule.assert_no_partial_transition(runtime)
        assert_no_partial_transition(runtime, recovering_operation=plan['operation_id'])
        state = load(runtime, plan['operation_id'])
        if state and (state['plan'] != plan or state['actor'] != actor):
            raise RuntimeError('formula resume same-operation identity differs')
        if state and state['phase'] == 'released':
            return prove_recorded_release(state, state.get('release_proof'))
        # No admission/owner/runtime read once the exact barrier was released.
        # New-pause admission is blocked until this bookkeeping is durable.
        released = barrier._load_state(runtime)
        if state and released and released.get('phase') == 'released':
            return _complete_bookkeeping(runtime, state, released, fault)
        if not state:
            fresh = preview(runtime, operation_id=plan['operation_id'], window_id=plan['window_id'],
                expected_sha=plan['expected_sha'], app_dir=Path(plan['app_dir']), unit_directory=Path(plan['unit_directory']),
                systemd=systemd, activity_reader=activity_reader, proc_root=proc_root)
            if fresh != plan:
                raise RuntimeError('formula resume target changed after preview')
            with barrier._BarrierLock(runtime):
                held = barrier.barrier_status(runtime)
                if not held.get('active') or held.get('phase') != 'held' or held.get('window_id') != plan['window_id']:
                    raise RuntimeError('formula resume held binding changed before intent')
                state = dict(schema_version=STATE_SCHEMA, plan=plan, phase='prepared', actor=actor, reason=reason)
                _save(runtime, state, 'prepared')
            fault('prepared')
        old, current = _observe(runtime, plan, systemd, activity_reader, proc_root)
        held = barrier.barrier_status(runtime)
        if (held.get('window_id') != plan['window_id'] or held.get('plan_fingerprint') != plan['baseline_fingerprint']
                or held.get('window_kind') != 'maintenance_pause' or not held.get('hold_confirmed')
                or held.get('phase') not in {'held', 'restoring', 'released'}):
            raise RuntimeError('formula resume exact barrier identity differs')
        if held.get('active') and state['phase'] not in {'committed', 'released'}:
            state['phase'] = 'restoring'; _save(runtime, state, 'restoring')
            barrier.mark_barrier_restoring(runtime, window_id=plan['window_id'], plan_fingerprint=plan['baseline_fingerprint'])
            for unit in pause.TIMERS:
                _observe(runtime, plan, systemd, activity_reader, proc_root)
                wanted = plan['baseline']['units'][unit]
                for field, positive, negative in (('is_enabled', 'enable', 'disable'), ('is_active', 'start', 'stop')):
                    _observe(runtime, plan, systemd, activity_reader, proc_root)
                    actual = systemd.unit_state(unit)
                    if actual[field] != wanted[field]:
                        action = positive if wanted[field] in {'enabled', 'active'} else negative
                        result = systemd._run([action, unit])
                        if result is not None and result.returncode != 0:
                            raise RuntimeError('formula resume timer command indeterminate/failed')
                        fault('timer:' + action + ':' + unit)
                        observed = systemd.unit_state(unit)
                        if observed[field] != wanted[field]:
                            raise RuntimeError('formula resume timer command readback differs')
                        _observe(runtime, plan, systemd, activity_reader, proc_root)
            old, after = _observe(runtime, plan, systemd, activity_reader, proc_root, final=True)
            state['receipt'] = dict(schema_version=RECEIPT_SCHEMA, status='restored', exact_target_state_restored=True,
                exact_prior_state_restored=False, operation_id=plan['operation_id'], window_id=plan['window_id'],
                baseline_fingerprint=plan['baseline_fingerprint'], target_fingerprint=plan['fingerprint'],
                expected_sha=plan['expected_sha'], delta=plan['delta'], control_signature=pause.fingerprint(after['controls']),
                units=after['units'], captured_at=pause.now_iso(), skipped_cycles_replayed=False)
            state['phase'] = 'committed'; _save(runtime, state, 'committed'); fault('committed')
        # A missing/released barrier cannot grant authority to finish partial work.
        state, old, current = prove_committed(runtime, plan['operation_id'], systemd=systemd,
            activity_reader=activity_reader, proc_root=proc_root)
        _retain_pause_receipt(runtime, state, old)
        fault('receipt_retained')
        barrier.release_formula_target_barrier(runtime, operation_id=plan['operation_id'], actor=actor, reason=reason,
            systemd=systemd, activity_reader=activity_reader, proc_root=proc_root)
        fault('barrier_released')
        return _complete_bookkeeping(runtime, state, barrier._load_state(runtime), fault)
