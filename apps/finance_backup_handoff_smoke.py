#!/usr/bin/env python3
"""Offline fixed-service handoff with real canonical disposable backup execution."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from apps.business_data_heavy_admission_smoke import initial_backup, make_due, actor, sign_write
from apps.finance_storage_backup_rotation_smoke import DEPLOYED_SHA
from packages.application import finance_backup_handoff as handoff
from packages.application.business_data_heavy_admission import heavy_admitted, heavy_admission_status
from packages.application.finance_backup_admission import (
    BackupAdmissionStateError, STATE_FILENAME, _read_intent, _update,
    backup_admission_priority, defer_backup_admission,
)
from packages.application.finance_storage_backup_rotation import FinanceStorageBackupRotation, scheduled_rotation


class FakeService:
    """Injected harness only; no real systemd or external backup destinations."""
    def __init__(self, runtime, *, mode='canonical'):
        self.runtime = runtime
        self.mode = mode
        self.starts = 0
        self.sha = DEPLOYED_SHA
        self.active = False
        self.invocation = '0' * 32
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = None

    def inspect(self):
        return {'identity': 'sha256:fixed-fixture', 'deployed_sha': self.sha,
                'active': self.active, 'terminal': not self.active,
                'invocation_id': self.invocation, 'result': 'success'}

    def start(self):
        # Another process can acquire the actual lock: parent never held EX.
        assert heavy_admission_status(self.runtime)['idle']
        self.starts += 1
        self.invocation = format(self.starts, '032x')
        if self.mode == 'timeout_before':
            raise subprocess.TimeoutExpired('fake fixed unit', 3)
        if self.mode == 'gated':
            self.entered.set()
            assert self.release.wait(5)
        self.active = True
        try:
            with patch.dict(os.environ, {'INVOCATION_ID': self.invocation}):
                self.result = scheduled_rotation(self.runtime, deployed_sha=self.sha,
                    require_distinct_device=False, require_backup_mountpoint=False)
        finally:
            self.active = False
        if self.mode == 'timeout_after':
            raise subprocess.TimeoutExpired('fake fixed unit', 3)


def due_fixture(runtime):
    raw, operational, root, rotation, result = initial_backup(runtime)
    make_due(root)
    with closing(sqlite3.connect(raw)) as conn:
        conn.execute("UPDATE backup_fixture_raw SET value='due'")
        conn.commit()
    return raw, operational, root, rotation, result


def waiting_intent(runtime):
    state = backup_admission_priority(runtime)
    _update(runtime, lambda previous: {'contract_version': 'finance_backup_admission_v1',
        'status': 'waiting', 'deployed_sha': DEPLOYED_SHA, 'request_id': 'sha256:fixture',
        **{key: state.get(key) for key in ('current_fingerprint', 'policy_fingerprint')}})


class BackupHandoffSmoke(unittest.TestCase):
    def test_natural_completion_or_invocation_drift_before_dispatch_never_resends(self):
        for race in ('complete', 'complete_same_invocation', 'invocation_only'):
            with self.subTest(race=race), tempfile.TemporaryDirectory() as temp:
                runtime = Path(temp) / 'runtime'
                due_fixture(runtime)
                service = FakeService(runtime)
                inspect = service.inspect
                calls = []
                def raced_inspect():
                    calls.append(True)
                    if len(calls) == 2:
                        if race != 'complete_same_invocation':
                            service.invocation = 'a' * 32
                        if race != 'invocation_only':
                            with patch.dict(os.environ, {'INVOCATION_ID': service.invocation}):
                                result = scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA,
                                    require_distinct_device=False, require_backup_mountpoint=False)
                            self.assertEqual(result['status'], 'completed')
                    return inspect()
                with patch.object(handoff, '_FixedBackupService', return_value=service), \
                        patch.object(service, 'inspect', side_effect=raced_inspect):
                    result = handoff.handoff_cycle_backup(runtime)
                self.assertEqual(service.starts, 0)
                self.assertEqual(result['backup_status'],
                    'outcome_unknown' if race == 'invocation_only' else 'resolved')
                self.assertEqual(_read_intent(runtime)['launch']['phase'], 'prepared')

    def test_submitted_receipt_cannot_update_a_new_request_with_same_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            due_fixture(runtime)
            service = FakeService(runtime)
            original = []
            def replace_during_start():
                original.append(_read_intent(runtime)['request_id'])
                _update(runtime, lambda previous: {**previous, 'request_id': 'sha256:later-request'})
            with patch.object(handoff, '_FixedBackupService', return_value=service), \
                    patch.object(service, 'start', side_effect=replace_during_start):
                result = handoff.handoff_cycle_backup(runtime)
            self.assertEqual(result['request_id'], original[0])
            self.assertEqual(result['backup_status'], 'outcome_unknown')
            self.assertEqual(_read_intent(runtime)['request_id'], 'sha256:later-request')
            self.assertEqual(_read_intent(runtime)['launch']['phase'], 'prepared')

    def test_real_canonical_due_backup_is_launched_once_before_any_cycle_effects(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            _, _, root, _, _ = due_fixture(runtime)
            service = FakeService(runtime)
            before = json.loads((root / 'current.json').read_text())
            with patch.object(handoff, '_FixedBackupService', return_value=service):
                result = handoff.handoff_cycle_backup(runtime)
                self.assertFalse(result['accepted'])
                self.assertFalse(result['source_effects_started'])
                self.assertEqual(result['backup_status'], 'resolved')
                self.assertEqual(service.result['status'], 'completed')
                self.assertFalse(handoff.cycle_backup_priority(runtime)['priority'])
            after = json.loads((root / 'current.json').read_text())
            self.assertNotEqual(before['backup_id'], after['backup_id'])
            self.assertEqual(service.starts, 1)
            self.assertEqual(_read_intent(runtime)['status'], 'resolved')

    def test_timeout_before_acceptance_stays_unknown_across_restart_and_idle(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            due_fixture(runtime)
            service = FakeService(runtime, mode='timeout_before')
            with patch.object(handoff, '_FixedBackupService', return_value=service):
                first = handoff.handoff_cycle_backup(runtime)
                self.assertEqual(first['backup_status'], 'outcome_unknown')
                again = handoff.handoff_cycle_backup(runtime)
                self.assertEqual(again['request_id'], first['request_id'])
                self.assertEqual(service.starts, 1)
            code = "from pathlib import Path;import sys;from packages.application.finance_backup_admission import _read_intent;print(_read_intent(Path(sys.argv[1]))['launch']['phase'])"
            self.assertEqual(subprocess.check_output([sys.executable, '-c', code, str(runtime)], cwd=ROOT).decode().strip(), 'outcome_unknown')
            # Natural canonical service is the recovery path; no blind forced resend.
            result = scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA,
                require_distinct_device=False, require_backup_mountpoint=False)
            self.assertEqual(result['status'], 'completed')
            with patch.object(handoff, '_FixedBackupService', return_value=service):
                self.assertFalse(handoff.cycle_backup_priority(runtime)['priority'])
            self.assertEqual(service.starts, 1)

    def test_timeout_after_canonical_acceptance_reads_resolved_same_request(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            due_fixture(runtime)
            service = FakeService(runtime, mode='timeout_after')
            with patch.object(handoff, '_FixedBackupService', return_value=service):
                result = handoff.handoff_cycle_backup(runtime)
                self.assertEqual(result['backup_status'], 'resolved')
                self.assertEqual(result['canonical_ack']['status'], 'completed')
                self.assertFalse(handoff.cycle_backup_priority(runtime)['priority'])
            self.assertEqual(service.starts, 1)

    def test_prepared_crash_after_selected_backup_blocks_until_canonical_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            due_fixture(runtime)
            service = FakeService(runtime)
            original = FinanceStorageBackupRotation.apply
            def crash_apply(instance, *args, **kwargs):
                return original(instance, *args, **kwargs, fault_at='after_current_selected')
            with patch.object(handoff, '_FixedBackupService', return_value=service):
                with patch.object(FinanceStorageBackupRotation, 'apply', crash_apply):
                    with self.assertRaisesRegex(RuntimeError, 'after current selection'):
                        handoff.handoff_cycle_backup(runtime)
                intent = _read_intent(runtime)
                self.assertEqual(intent['launch']['phase'], 'prepared')
                self.assertEqual(intent['status'], 'waiting')
                self.assertTrue(handoff.cycle_backup_priority(runtime)['priority'])
                self.assertEqual(handoff.handoff_cycle_backup(runtime)['backup_status'], 'outcome_unknown')
                self.assertEqual(service.starts, 1)
                with patch.object(FinanceStorageBackupRotation, 'build_plan', side_effect=AssertionError('pending replan')), \
                     patch('packages.application.finance_storage_backup_rotation._copy_sqlite', side_effect=AssertionError('copy resend')):
                    result = scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA,
                        require_distinct_device=False, require_backup_mountpoint=False)
                self.assertEqual(result['status'], 'completed')
                self.assertFalse(handoff.cycle_backup_priority(runtime)['priority'])
                self.assertEqual(service.starts, 1)

    def test_atomic_concurrent_reservation_no_duplicate_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            due_fixture(runtime)
            service = FakeService(runtime, mode='gated')
            results = []
            with patch.object(handoff, '_FixedBackupService', return_value=service):
                thread = threading.Thread(target=lambda: results.append(handoff.handoff_cycle_backup(runtime)))
                thread.start()
                self.assertTrue(service.entered.wait(5))
                second = handoff.handoff_cycle_backup(runtime)
                self.assertEqual(second['backup_status'], 'outcome_unknown')
                self.assertEqual(service.starts, 1)
                service.release.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results[0]['backup_status'], 'resolved')

    def test_real_process_reservation_prevents_second_start(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            due_fixture(runtime)
            code = """import sys,json
from pathlib import Path
from unittest.mock import patch
from apps.finance_backup_handoff_smoke import FakeService
from packages.application import finance_backup_handoff as h
class ProcessService(FakeService):
 def start(self):
  print('reserved',flush=True)
  input()
  super().start()
s=ProcessService(Path(sys.argv[1]))
with patch.object(h,'_FixedBackupService',return_value=s):
 print(json.dumps(h.handoff_cycle_backup(s.runtime)),flush=True)
"""
            process = subprocess.Popen([sys.executable, '-c', code, str(runtime)], cwd=ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'reserved')
                service = FakeService(runtime)
                with patch.object(handoff, '_FixedBackupService', return_value=service):
                    self.assertEqual(handoff.handoff_cycle_backup(runtime)['backup_status'], 'outcome_unknown')
                self.assertEqual(service.starts, 0)
                output, errors = process.communicate('\n', timeout=10)
                self.assertEqual(process.returncode, 0, errors)
                self.assertEqual(json.loads(output)['backup_status'], 'resolved')
            finally:
                if process.poll() is None:
                    process.kill(); process.communicate()

    def test_dormant_cycle_preaccept_backup_and_race_recheck_release_heavy(self):
        from apps.sheet_vitrina_v1_cycle_smoke import CycleFake, CycleHistoryConfig, SLOT
        from packages.application.sheet_vitrina_v1_cycle import CycleReceiptStore
        for raced in (False, True):
            with self.subTest(raced=raced), tempfile.TemporaryDirectory() as temp:
                runtime = Path(temp) / 'runtime'
                raw, _, root, _, _ = initial_backup(runtime)
                if not raced:
                    make_due(root)
                    with closing(sqlite3.connect(raw)) as conn:
                        conn.execute("UPDATE backup_fixture_raw SET value='initial-due'"); conn.commit()
                service = FakeService(runtime)
                fake = CycleFake(runtime); fake.now_factory = lambda: datetime.now(timezone.utc)
                (runtime / 'contract.json').write_text('{}')
                config = CycleHistoryConfig(runtime / 'candidate', runtime / 'contract.json', 'epoch')
                original = handoff.cycle_backup_priority
                calls = []
                def check(*args, **kwargs):
                    state = original(*args, **kwargs)
                    calls.append(state)
                    if raced and len(calls) == 1:
                        make_due(root)
                        with closing(sqlite3.connect(raw)) as conn:
                            conn.execute("UPDATE backup_fixture_raw SET value='between-checks'"); conn.commit()
                    return state
                with patch.object(handoff, '_FixedBackupService', return_value=service), \
                     patch.object(handoff, 'cycle_backup_priority', side_effect=check), \
                     patch.object(CycleReceiptStore, 'accept', side_effect=AssertionError('premature receipt/source acceptance')):
                    result = fake._start_sheet_cycle_job(request_key='offline', slot_utc=SLOT, history_config=config)
                self.assertEqual(result['status'], 'backup_priority')
                self.assertEqual(fake.events, [])
                self.assertEqual(service.starts, 1)
                self.assertEqual(len(calls), 2 if raced else 1)
                self.assertFalse((runtime / '.sheet-vitrina-cycle').exists())

    def test_no_parent_heavy_can_launch_child(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            due_fixture(runtime)
            service = FakeService(runtime)
            with patch.object(handoff, '_FixedBackupService', return_value=service):
                with heavy_admitted(runtime, operation='cycle'):
                    with self.assertRaisesRegex(BackupAdmissionStateError, 'parent heavy'):
                        handoff.handoff_cycle_backup(runtime)
            self.assertEqual(service.starts, 0)

    def test_busy_canonical_ack_allows_later_retry_only_for_exact_terminal_invocation(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            due_fixture(runtime)
            service = FakeService(runtime)
            # Command dispatch itself is legal while another process runs heavy;
            # the canonical service rejects before scans/effects and records proof.
            with patch.object(handoff, '_FixedBackupService', return_value=service):
                with actor(runtime):
                    with patch.object(service, 'start') as start:
                        def busy_start():
                            service.starts += 1
                            service.invocation = format(service.starts, '032x')
                            with patch.dict(os.environ, {'INVOCATION_ID': service.invocation}):
                                service.result = scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA,
                                    require_distinct_device=False, require_backup_mountpoint=False)
                        start.side_effect = busy_start
                        handoff.handoff_cycle_backup(runtime)
                self.assertEqual(service.result['status'], 'deferred')
                self.assertEqual(_read_intent(runtime)['canonical_ack']['mutation_count'], 0)
                done = handoff.handoff_cycle_backup(runtime)
                self.assertEqual(done['backup_status'], 'resolved')
                self.assertEqual(service.starts, 2)

    def test_exact_same_sha_pending_plan_resumes_without_replan_or_copy_resend(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            _, _, _, rotation, _ = due_fixture(runtime)
            plan = rotation.build_plan()
            with self.assertRaisesRegex(RuntimeError, 'after current selection'):
                rotation.apply(reviewed_plan=plan, expected_fingerprint=plan['fingerprint'],
                    approval_reference='offline-fixture-approved', activate_policy=False, fault_at='after_current_selected')
            service = FakeService(runtime)
            with patch.object(handoff, '_FixedBackupService', return_value=service), \
                 patch.object(FinanceStorageBackupRotation, 'build_plan', side_effect=AssertionError('competing replan')), \
                 patch('packages.application.finance_storage_backup_rotation._copy_sqlite', side_effect=AssertionError('copy resend')):
                result = handoff.handoff_cycle_backup(runtime)
            self.assertEqual(result['backup_status'], 'resolved')
            self.assertEqual(service.result['plan_fingerprint'], plan['fingerprint'])

    def test_other_sha_pending_and_waiting_refuse_before_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            _, _, _, rotation, _ = due_fixture(runtime)
            plan = rotation.build_plan()
            with self.assertRaises(RuntimeError):
                rotation.apply(reviewed_plan=plan, expected_fingerprint=plan['fingerprint'],
                    approval_reference='offline-fixture-approved', activate_policy=False, fault_at='after_current_selected')
            service = FakeService(runtime); service.sha = 'b' * 40
            with patch.object(handoff, '_FixedBackupService', return_value=service):
                with self.assertRaisesRegex(BackupAdmissionStateError, 'another deployed SHA'):
                    handoff.handoff_cycle_backup(runtime)
            self.assertEqual(service.starts, 0)
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            due_fixture(runtime); defer_backup_admission(runtime, deployed_sha=DEPLOYED_SHA)
            with self.assertRaisesRegex(BackupAdmissionStateError, 'another deployed SHA'):
                scheduled_rotation(runtime, deployed_sha='b' * 40,
                    require_distinct_device=False, require_backup_mountpoint=False)

    def test_unknown_pending_phase_or_mutated_reviewed_plan_refuses_before_start(self):
        for mutation in ('phase', 'plan'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                runtime = Path(temp) / 'runtime'
                _, _, root, rotation, _ = due_fixture(runtime)
                plan = rotation.build_plan()
                with self.assertRaises(RuntimeError):
                    rotation.apply(reviewed_plan=plan, expected_fingerprint=plan['fingerprint'],
                        approval_reference='offline-fixture-approved', activate_policy=False, fault_at='after_current_selected')
                path = root / 'transactions' / (plan['fingerprint'].removeprefix('sha256:')+'.json')
                transaction = json.loads(path.read_text())
                if mutation == 'phase':
                    transaction['phase'] = 'unknown-new-phase'
                else:
                    transaction['reviewed_plan']['created_at'] = 'mutated-not-reviewed'
                path.write_text(json.dumps(transaction))
                service = FakeService(runtime)
                with patch.object(handoff, '_FixedBackupService', return_value=service):
                    with self.assertRaisesRegex(BackupAdmissionStateError, 'unproven'):
                        handoff.handoff_cycle_backup(runtime)
                self.assertEqual(service.starts, 0)

    def test_not_due_proof_binds_the_consumed_plan_and_invalidates_on_source_change(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            raw, _, _, _, _ = initial_backup(runtime)
            waiting_intent(runtime)
            result = scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA,
                require_distinct_device=False, require_backup_mountpoint=False)
            self.assertEqual(result['status'], 'not_due')
            intent = _read_intent(runtime)
            self.assertEqual(intent['status'], 'resolved')
            self.assertEqual(intent['canonical_ack']['not_due_proof']['plan_fingerprint'], result['plan_fingerprint'])
            self.assertEqual(handoff._validated_state(runtime, DEPLOYED_SHA)['reason'], 'canonical_not_due')
            with closing(sqlite3.connect(raw)) as conn:
                conn.execute("UPDATE backup_fixture_raw SET value='after-ack'"); conn.commit()
            self.assertNotEqual(handoff._validated_state(runtime, DEPLOYED_SHA)['reason'], 'canonical_not_due')

    def test_source_commit_between_plan_and_ack_cannot_fabricate_not_due_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            raw, _, _, _, _ = initial_backup(runtime)
            waiting_intent(runtime)
            original = FinanceStorageBackupRotation.build_plan
            def racing_plan(instance, *args, **kwargs):
                result = original(instance, *args, **kwargs)
                with closing(sqlite3.connect(raw)) as conn:
                    conn.execute("UPDATE backup_fixture_raw SET value='raced-not-consumed'"); conn.commit()
                return result
            with patch.object(FinanceStorageBackupRotation, 'build_plan', racing_plan):
                result = scheduled_rotation(runtime, deployed_sha=DEPLOYED_SHA,
                    require_distinct_device=False, require_backup_mountpoint=False)
            self.assertEqual(result['status'], 'not_due')
            intent = _read_intent(runtime)
            self.assertEqual(intent['status'], 'waiting')
            self.assertEqual(intent['canonical_ack']['status'], 'not_due_unproven')
            self.assertIsNone(intent['canonical_ack']['not_due_proof'])

    def test_priority_metadata_is_bounded_no_scan_6d7d_and_policy_inert_no_service(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            with patch.object(handoff, '_FixedBackupService', side_effect=AssertionError('service not needed')):
                self.assertFalse(handoff.cycle_backup_priority(runtime)['priority'])
            self.assertEqual(list(runtime.iterdir()), [])
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'runtime'
            _, _, root, _, _ = initial_backup(runtime)
            selector = json.loads((root / 'current.json').read_text())
            manifest = json.loads((root / 'retained' / selector['backup_id'] / 'backup_manifest.json').read_text())
            captured = datetime.fromisoformat(manifest['captured_at'].replace('Z', '+00:00'))
            with patch('packages.application.finance_storage_backup_rotation._sha256_file', side_effect=AssertionError('backup hash')), \
                 patch('packages.application.finance_storage_backup_rotation._sqlite_readback', side_effect=AssertionError('DB scan')):
                self.assertFalse(backup_admission_priority(runtime, now=captured+timedelta(days=6))['priority'])
                self.assertTrue(backup_admission_priority(runtime, now=captured+timedelta(days=7))['priority'])
                make_due(root)
                service = FakeService(runtime)
                (root / 'current.json').write_text('{}')
                with patch.object(handoff, '_FixedBackupService', return_value=service):
                    with self.assertRaises(BackupAdmissionStateError):
                        handoff.cycle_backup_priority(runtime)
                self.assertEqual(service.starts, 0)

    def test_real_fixed_service_rejects_arbitrary_runtime_and_unit_dropins_before_start(self):
        with self.assertRaises(BackupAdmissionStateError):
            handoff._FixedBackupService(Path('/tmp/not-a-deploy'))
        values = {key: '' for key in handoff.PROPERTIES}
        values.update(LoadState='loaded', FragmentPath='/etc/systemd/system/'+handoff.UNIT,
            NeedDaemonReload='no', WorkingDirectory=str(handoff.APP), DynamicUser='no', Type='oneshot',
            ActiveState='inactive', MainPID='0', Job='0', Result='success')
        argv = '/usr/bin/python3 apps/finance_storage_backup_rotation.py --runtime-dir ' + str(handoff.RUNTIME) + ' --deployed-sha-file ' + str(handoff.APP / handoff.SHA_FILE)
        values['ExecStart'] = '{ path=/usr/bin/python3 ; argv[]=' + argv + ' ; ignore_errors=no ; }'
        service = object.__new__(handoff._FixedBackupService)
        def fakefile(path, **kwargs):
            if path.name == handoff.SHA_FILE: return DEPLOYED_SHA.encode()
            return b'fixture canonical unit or bounded code'
        with patch.object(handoff, '_safe_file', side_effect=fakefile):
            with patch.object(service, '_show', return_value=values):
                self.assertTrue(service.inspect()['terminal'])
            for key, wrong in (('DropInPaths', '/etc/systemd/system/override.conf'),
                               ('NeedDaemonReload', 'yes'), ('ExecStart', '{ argv[]=/bin/true ; ignore_errors=no ; }'),
                               ('ExecStartPre', '/bin/true'), ('WorkingDirectory', '/tmp'),
                               ('User', 'untrusted'), ('Environment', 'PYTHONPATH=/tmp'), ('Type', 'simple')):
                with self.subTest(key=key), patch.object(service, '_show', return_value={**values, key: wrong}):
                    with self.assertRaises(BackupAdmissionStateError):
                        service.inspect()


if __name__ == '__main__':
    unittest.main()
