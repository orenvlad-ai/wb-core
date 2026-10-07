"""One durable maintenance debt; fixed cadence and the canonical one-POST transport.

Only barrier release arms it. Status/prepare are read-only. The existing HTTP
server's service hook performs bounded coordination outside its request thread.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time

from packages.application import business_data_write_barrier as barrier

FILENAME = '.business-data-cycle-wakeup.json'
SCHEMA = 'business_data_cycle_wakeup_v1'
PHASES = {'pending', 'accepted', 'wait_next_slot', 'superseded'}


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise RuntimeError('maintenance wakeup requires an aware timestamp')
    return parsed


def _private_json(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        actual = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_size > maximum or info.st_uid != os.geteuid()
                or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino)):
            raise RuntimeError('maintenance wakeup metadata exceeds private bound')
        return json.loads(os.read(fd, maximum + 1))
    finally:
        os.close(fd)


def load(runtime):
    try:
        value = _private_json(Path(runtime) / FILENAME, 16384)
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or set(value) != {'schema', 'window_id', 'plan_fingerprint', 'started_at', 'missed_slot', 'phase', 'last_status'}
            or value['schema'] != SCHEMA or value['phase'] not in PHASES
            or not isinstance(value['last_status'], str) or len(value['last_status']) > 128):
        raise RuntimeError('invalid maintenance wakeup record')
    barrier._validate_identifier(value['window_id'], label='window_id')
    barrier._validate_fingerprint(value['plan_fingerprint'])
    instant(value['started_at']); instant(value['missed_slot'])
    return value


def slot_receipt(runtime, slot):
    """Bounded canonical slot metadata, including honest owner-lost semantics."""
    from packages.application.sheet_vitrina_v1_cycle import CycleReceiptStore
    name = 'slot-' + hashlib.sha256(slot.encode()).hexdigest()[:32] + '.json'
    try:
        pointer = _private_json(Path(runtime) / 'sheet-vitrina-cycles' / name, 1024)
    except FileNotFoundError:
        return None
    receipt = CycleReceiptStore(runtime, lambda: '').read(pointer['cycle_id'])
    if receipt is None or receipt.get('slot_utc') != slot:
        raise RuntimeError('maintenance missed-slot proof is invalid')
    return receipt


def arm_before_release(runtime, state, released_at, restore_readback):
    """Called under barrier lock, after exact restore proof, before admission opens.

    No baseline edits. Atomic durable debt precedes release, so a restart in the
    final bookkeeping gap sees the same intent. An already accepted slot is never
    replayed, irrespective of its terminal/uncertain outcome.
    """
    from packages.application.business_data_cycle_dispatch import latest_slot, selected
    from packages.application.business_data_schedule_profile import WAREHOUSE_TIMER
    if (state.get('window_kind') != 'maintenance_pause'
            or restore_readback.get('exact_prior_state_restored') is not True or not selected(runtime)):
        return
    owner = (restore_readback.get('units') or {}).get(WAREHOUSE_TIMER) or {}
    if (owner.get('is_enabled'), owner.get('is_active')) != ('enabled', 'active'):
        return  # A deliberately disabled owner did not miss a scheduled launch.
    from apps.business_data_maintenance import POLICY_FILENAME
    from packages.application.business_data_schedule_profile import read_private, required_phase_readiness
    if read_private(Path(runtime) / POLICY_FILENAME).get('master_desired') is not True or not required_phase_readiness(Path(runtime))['ready']:
        return
    slot = latest_slot(instant(released_at))
    if instant(slot) < instant(state['started_at']) or slot_receipt(runtime, slot) is not None:
        return
    value = {'schema': SCHEMA, 'window_id': state['window_id'], 'plan_fingerprint': state['plan_fingerprint'],
             'started_at': state['started_at'], 'missed_slot': slot, 'phase': 'pending', 'last_status': 'awaiting_exact_release'}
    barrier._atomic_write_private_json(Path(runtime) / FILENAME, value)


def _bound(state, value):
    return bool(state and all(state.get(key) == value[key]
                for key in ('window_id', 'plan_fingerprint', 'started_at')))


def policy(runtime, now, *, expected_slot=None):
    """Read-only admission policy, also used under actual heavy EX.

    Only the slot missed in this exact maintenance window is constrained. A
    distinct later scheduled slot keeps its normal timing semantics.
    """
    from packages.application.business_data_cycle_dispatch import latest_slot
    def clock_check():
        current = now() if callable(now) else now
        return current, 'slot_expired' if expected_slot and latest_slot(current) != expected_slot else None
    value = load(runtime)
    if value is None:
        return clock_check()[1]
    state = barrier._load_state(runtime)
    if not _bound(state, value):
        return clock_check()[1]  # New barrier owns the boundary; old debt is stale.
    if state.get('active') or state.get('phase') != 'released':
        return 'maintenance_not_released'
    from packages.application.business_data_maintenance_pause import load_state
    pause = load_state(runtime)
    proof = (pause or {}).get('restore_readback') or {}
    if (not pause or pause['window_id'] != value['window_id'] or pause['plan_fingerprint'] != value['plan_fingerprint']
            or barrier._fingerprint(proof) != (state.get('restore') or {}).get('readback_fingerprint')
            or proof.get('status') != 'restored'):
        return 'exact_resume_not_proven'
    # At actual acceptance the clock is read only after bounded metadata proof.
    now, expired = clock_check()
    if expired:
        return expired
    if latest_slot(now) != value['missed_slot']:
        return None
    if value['phase'] in {'wait_next_slot', 'superseded'}:
        return 'wait_next_slot'
    remaining = (instant(value['missed_slot']) + timedelta(hours=3) - now).total_seconds()
    return 'wait_next_slot' if remaining < 7200 else None


def catchup_slot(runtime, now):
    from packages.application.business_data_cycle_dispatch import latest_slot
    value = load(runtime)
    state = barrier._load_state(runtime)
    return bool(value and _bound(state, value)
                and value['missed_slot'] == latest_slot(now))


def _settle(runtime, expected, phase, status):
    with barrier._BarrierLock(runtime):
        actual = load(runtime)
        if actual != expected:
            return  # Another window/worker owns newer evidence.
        state = barrier._load_state(runtime)
        if not _bound(state, expected):
            phase, status = 'superseded', 'new_maintenance_window'
        if actual['phase'] == phase and actual['last_status'] == str(status)[:128]:
            return
        barrier._atomic_write_private_json(Path(runtime) / FILENAME,
            dict(actual, phase=phase, last_status=str(status)[:128]))


def coordinate(runtime, now):
    """One bounded service tick. Never called by a status/preflight request."""
    runtime = Path(runtime).resolve()
    value = load(runtime)
    if value is None or value['phase'] != 'pending':
        return
    state = barrier._load_state(runtime)
    if not _bound(state, value):
        _settle(runtime, value, 'superseded', 'new_maintenance_window')
        return
    if state.get('phase') == 'released' and slot_receipt(runtime, value['missed_slot']) is not None:
        _settle(runtime, value, 'accepted', 'canonical_slot_already_accepted')
        return  # A normal timer won; even an interrupted receipt is not replayed.
    if now > instant(value['missed_slot']) + timedelta(hours=1):
        _settle(runtime, value, 'wait_next_slot', 'late_window_after_' + value['last_status'])
        return  # Includes rollover/restart. This worker never borrows a new slot.
    refusal = policy(runtime, now)
    if refusal == 'wait_next_slot':
        _settle(runtime, value, 'wait_next_slot', 'late_window_after_' + value['last_status'])
        return  # Deadline precedes busy/unknown reads; normal next slot remains.
    if refusal is not None:
        return
    from packages.application.business_data_cycle_dispatch import launch, _read_record
    try:
        result = launch(runtime)
    except Exception as exc:
        _settle(runtime, value, 'pending', 'launch_error_' + type(exc).__name__)
        return  # Existing journal decides whether another tick may only read.
    status = result.get('status', 'unknown')
    if result.get('accepted') is True:
        # An older terminal transport readback may precede a blocked prepare;
        # launch only returns that readback early for an active/unknown request.
        from packages.application.business_data_cycle_dispatch import _decode
        slot = _decode(result['dispatch_id'])['slot']
        if instant(slot) >= instant(value['missed_slot']):
            _settle(runtime, value, 'accepted', status)
        else:
            _settle(runtime, value, 'pending', 'busy_previous_cycle')
    elif status == 'not_accepted':
        _settle(runtime, value, 'wait_next_slot', 'single_submit_not_accepted')
    elif status == 'wait_next_slot':
        _settle(runtime, value, 'wait_next_slot', 'late_window')
    elif status in {'unknown', 'expired_not_accepted'} and result.get('dispatch_id'):
        # Keep same-operation readback until expiration proves no acceptance.
        record = _read_record(runtime)
        if record and record['phase'] == 'terminal':
            _settle(runtime, value, 'wait_next_slot', 'single_submit_exhausted')
        else:
            _settle(runtime, value, 'pending', 'uncertain_same_operation')
    else:
        _settle(runtime, value, 'pending', status)


def diagnostics(runtime, now):
    """Bounded read-only reason; observing deadline never changes durable state."""
    value = load(runtime)
    if value is None:
        return None
    state = barrier._load_state(runtime)
    outcome = value['phase']
    if not _bound(state, value):
        outcome = 'superseded'
    elif outcome == 'pending' and now > instant(value['missed_slot']) + timedelta(hours=1):
        outcome = 'wait_next_slot'  # Read-only view even before service bookkeeping.
    return {key: value[key] for key in ('window_id', 'missed_slot', 'phase', 'last_status')} | {
        'outcome': outcome, 'admission_refusal': policy(runtime, now)}


class ServiceWakeup:
    """Existing HTTP service polling; no new unit, timer, collector or job queue."""
    def __init__(self, runtime):
        self.runtime = Path(runtime)
        self.thread = None
        self.next_check = 0.0
        self.closed = False
        self.last_error = ''
        self.start_uncertain = False

    def tick(self):
        if self.closed or time.monotonic() < self.next_check or self.thread is not None and self.thread.is_alive():
            return
        if self.start_uncertain and self.thread is not None:
            started = getattr(self.thread, '_started', None)
            if started is None or not started.is_set():
                return  # A possible native child in limbo must not be duplicated.
        self.next_check = time.monotonic() + 30
        try:
            value = load(self.runtime)
            if value is None or value['phase'] != 'pending':
                return
        except Exception as exc:
            self.last_error = type(exc).__name__ + ': ' + str(exc)
            return  # Invalid metadata fails closed, without a dispatch thread.
        def run():
            try:
                coordinate(self.runtime, datetime.now(timezone.utc))
                self.last_error = ''
            except Exception as exc:
                self.last_error = type(exc).__name__ + ': ' + str(exc)
        try:
            self.start_uncertain = False
            self.thread = threading.Thread(target=run, name='maintenance-cycle-wakeup', daemon=True)
            self.thread.start()
        except Exception as exc:
            # Failure belongs to this small worker, never to serve_forever.
            # Keep a possible started/limbo child's handle. Only proven absence
            # allows another start; journal ownership still forbids POST resend.
            self.last_error = type(exc).__name__ + ': ' + str(exc)
            from packages.application.business_data_procedure_admission import thread_start_is_proven_absent
            if self.thread is not None and thread_start_is_proven_absent(self.thread):
                self.thread = None
            else:
                self.start_uncertain = True

    def close(self):
        self.closed = True  # Journal survives shutdown; no resend on restart.
