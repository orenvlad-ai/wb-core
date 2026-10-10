#!/usr/bin/env python3
"""Synthetic files/systemd only, using actual native pause/deploy/barrier APIs."""
from copy import deepcopy
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.business_data_maintenance_pause_smoke import fixture, FakeSystemd
from apps.business_data_maintenance import SystemdClient
from packages.application import business_data_formula_resume as formula
from packages.application import business_data_maintenance_pause as pause
from packages.application import business_data_deploy_protection as deploy
from packages.application import business_data_write_barrier as barrier
from packages.application import business_data_cycle_wakeup as wake

SHA = 'a' * 40
OP = 'formula-deploy-001'


class FileSystemd(FakeSystemd):
    """Loaded commands are explicit; content digests hash real fragment/dropins.

    Calls the real native SystemdClient.unit_state/content serialization. No real kernel,
    timers, WB, SSH, HTTP, production store or subprocess systemctl is used.
    """
    def unit_state(self, unit):
        return SystemdClient.unit_state(self, unit)

    def disable_now(self, unit):
        props = self.states[unit]['properties']
        for key, next_value in (('TimersCalendar', '(null)'), ('TimersMonotonic', '0')):
            if key in props:
                props[key] = re.sub(r'next_elapse=[^}]+', 'next_elapse=' + next_value + ' ', props[key])
        return super().disable_now(unit)

    def _run(self, args):
        if args == ['show', formula.BUYER_UNIT, '--property=InvocationID']:
            return SimpleNamespace(returncode=0, stdout='InvocationID=' +
                self.states[formula.BUYER_UNIT]['properties']['InvocationID'] + '\n')
        if args[0] == 'show':
            value = self.states[args[1]]
            props = dict(value['properties'],UnitFileState=value['is_enabled'],ActiveState=value['is_active'])
            return SimpleNamespace(returncode=0,stdout='\n'.join(k+'='+v for k,v in props.items()),stderr='')
        if args[0] in {'start', 'stop'} and args[1].endswith('.timer'):
            props = self.states[args[1]]['properties']
            for key in ('TimersCalendar', 'TimersMonotonic'):
                if key in props:
                    next_value = ('Thu 2026-10-08 19:40:00 UTC' if key == 'TimersCalendar' else '5min') if args[0] == 'start' else ('(null)' if key == 'TimersCalendar' else '0')
                    props[key] = re.sub(r'next_elapse=[^}]+', 'next_elapse=' + next_value + ' ', props[key])
        return super()._run(args)


class FormulaResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='formula-resume-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime, self.app, self.units = (self.root / x for x in ('runtime', 'app', 'units'))
        self.runtime.mkdir(); self.app.mkdir(); self.units.mkdir()
        _, self.activity = fixture(self.runtime)
        (self.runtime / pause.POLICY_FILENAME).chmod(0o600)
        self.systemd = FileSystemd()
        self.proc = self.root / 'proc'; self.proc.mkdir()
        guarded_buyer = self._testMethodName.startswith('test_guarded_buyer')
        if guarded_buyer:
            boot = self.proc / 'sys/kernel/random/boot_id'; boot.parent.mkdir(parents=True)
            boot.write_text('11111111-2222-3333-4444-555555555555\n')
            for relative in formula.BUYER_GUARD_SOURCES:
                path = self.app / relative; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT / relative).read_bytes())
            native_read = formula._buyer_native_read
            def read_native(command):
                if command == ['/usr/bin/systemctl', 'show', formula.BUYER_UNIT, '--property=InvocationID', '--no-pager']:
                    return self.systemd._run(['show', formula.BUYER_UNIT, '--property=InvocationID']).stdout.encode()
                return native_read(command)
            self.addCleanup(patch.stopall)
            patch.object(formula, '_buyer_native_read', side_effect=read_native).start()
        # Real canonical artifact and complete native formula path set. A tiny
        # synthetic source delta models a reviewed deploy; no formula executes.
        contract = json.loads((ROOT / formula.CONTRACT_RELATIVE).read_bytes())
        for relative in contract['formula_code_hashes']:
            path = self.app / relative; path.parent.mkdir(parents=True, exist_ok=True)
            data = (ROOT / relative).read_bytes()
            if relative == 'packages/application/fbs_snapshot_cost.py':
                data += b'\n# synthetic completed deployment fixture\n'
            path.write_bytes(data); contract['formula_code_hashes'][relative] = formula._hash(data)
        self.new_epoch = 'wbc0069k16-reviewed-native-v1:' + formula._hash(json.dumps(
            contract['formula_code_hashes'],sort_keys=True,separators=(',', ':')).encode())
        self.old_epoch = json.loads((ROOT / formula.CONTRACT_RELATIVE).read_bytes())['formula_epoch']
        self.assertNotEqual(self.old_epoch, self.new_epoch)
        contract['formula_epoch'] = self.new_epoch
        cp = self.app / formula.CONTRACT_RELATIVE; cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(contract))
        target = json.loads((ROOT / formula.TARGET_RELATIVE).read_bytes())
        target.update(target_dir=str(self.app), systemd_unit_directory=str(self.units))
        target['runtime_env']['REGISTRY_UPLOAD_RUNTIME_DIR'] = str(self.runtime)
        (self.app / formula.TARGET_RELATIVE).write_text(json.dumps(target))
        self.original_artifact = (ROOT / formula.UNIT_RELATIVE).read_bytes()
        self.new_artifact = self.original_artifact.replace(self.old_epoch.encode(), self.new_epoch.encode(), 1)
        artifact = self.app / formula.UNIT_RELATIVE; artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(self.new_artifact)
        for unit, value in self.systemd.states.items():
            fragment = self.units / unit
            data = self.original_artifact if unit == formula.UNIT else b'[Unit]\nDescription=Synthetic ordinary unit\n'
            if guarded_buyer and unit == formula.BUYER_UNIT:
                data = (ROOT / ('artifacts/registry_upload_http_entrypoint/systemd/' + unit)).read_bytes().replace(
                    b'/opt/wb-core-runtime/state', str(self.runtime).encode())
                artifact = self.app / ('artifacts/registry_upload_http_entrypoint/systemd/' + unit)
                artifact.write_bytes(data)
                value.update(is_enabled='static', is_active='failed')
                value['properties'].update(Result='exit-code', ExecMainCode='1', ExecMainStatus='1')
            fragment.write_bytes(data)
            value['properties'].update(FragmentPath=str(fragment), DropInPaths='', ExecStart='{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 apps/synthetic.py ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }')
            if unit.endswith('.timer'):
                value['properties'].pop('ExecStart')
                value['properties']['TimersCalendar'] = '{ OnCalendar=*-*-* *:00,10,20,30,40,50:00 ; next_elapse=Thu 2026-10-08 19:40:00 UTC }'
                if unit == pause.TIMERS[0]:
                    value['properties'].pop('TimersCalendar')
                    value['properties']['TimersMonotonic'] = '{ OnActiveUSec=5min ; next_elapse=2month 4w 1d 20h 13min 33.498883s }'
            if unit == formula.UNIT:
                exec_line = next(line for line in data.decode().splitlines() if line.startswith('ExecStart='))[10:]
                value['properties']['ExecStart'] = '{ path=/usr/bin/python3 ; argv[]=' + exec_line + ' ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }'
            if guarded_buyer and unit == formula.BUYER_UNIT:
                value['properties']['ExecStart'] = '{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 apps/wb_buyer_authenticated_collect.py --tick --runtime-dir ' + str(self.runtime) + ' ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }'
            if unit in {formula.UNIT, 'wb-core-sheet-vitrina-canary-restore.service'}:
                value['properties']['ExecStart'] = value['properties']['ExecStart'].replace('start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0', 'start_time=[Thu 2026-10-08 19:31:08 UTC] ; stop_time=[Thu 2026-10-08 19:31:09 UTC] ; pid=2419611 ; code=exited ; status=0')
        self.dropin = self.units / (formula.UNIT + '.d') / '10-fixture.conf'
        self.dropin.parent.mkdir(); self.dropin.write_bytes(b'[Service]\nEnvironment=UNRELATED_FIXTURE=1\n')
        self.systemd.states[formula.UNIT]['properties']['DropInPaths'] = str(self.dropin)
        self.systemd.states['wb-core-registry-http.service'] = dict(is_active='active',is_enabled='enabled',
            properties=dict(LoadState='loaded',MainPID='42',SubState='running'))
        from packages.application.business_data_schedule_profile import WAREHOUSE_TIMER
        self.systemd.states[WAREHOUSE_TIMER].update(is_enabled='enabled',is_active='active')
        if guarded_buyer:
            self.systemd.states[formula.BUYER_UNIT.replace('.service', '.timer')].update(is_enabled='enabled', is_active='active')
        self.options = dict(systemd=self.systemd,activity_reader=self.activity,proc_root=self.proc)
        self.pause_options = dict(**self.options,window_id='formula-pause-001',actor='test',reason='synthetic deploy')
        self.addCleanup(patch.stopall)
        patch.object(pause, '_cron_entries', return_value=[]).start()
        patch.object(deploy, '_selected', return_value=True).start()
        pause.pause(self.runtime, **self.pause_options)
        self.baseline = deepcopy(pause.load_state(self.runtime)['baseline'])
        deploy.claim(self.runtime, OP, quiet_reader=lambda: pause.readback(self.runtime, **self.options))
        deploy.start(self.runtime, OP, SHA)
        (self.units / formula.UNIT).write_bytes(self.new_artifact)
        props = self.systemd.states[formula.UNIT]['properties']
        props['ExecStart'] = props['ExecStart'].replace(self.old_epoch, self.new_epoch, 1)
        for unit in (formula.UNIT, 'wb-core-sheet-vitrina-canary-restore.service'):
            props = self.systemd.states[unit]['properties']
            props['ExecStart'] = re.sub(r' ; start_time=.*', ' ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }', props['ExecStart'])
        (self.app / '.wb-core-runtime-sha').write_text(SHA + '\n')
        (self.app / '.wb-core-deploy.json').write_text(json.dumps(dict(schema_version='wb_core_deploy_metadata_v2',
            commit=SHA,deployment_complete=True,deployed_at='2026-10-08T10:00:00Z')))
        deploy.finish(self.runtime, OP, SHA, app_dir=self.app, registry_reader=lambda: dict(active_state='active',main_pid=42))
        self.preview_options = dict(**self.options,operation_id=OP,window_id=self.pause_options['window_id'],
            expected_sha=SHA,app_dir=self.app,unit_directory=self.units)
        self.apply_options = dict(**self.options,actor='test',reason='reviewed formula-only resume')
        # Core tests need no schedule/owner feature migration. Dedicated wakeup
        # test below exercises selected cycle and actual native metadata policy.
        patch('packages.application.business_data_cycle_dispatch.selected',return_value=False).start()

    def prepare(self):
        return formula.preview(self.runtime, **self.preview_options)

    def apply(self, plan=None, **options):
        plan = plan or self.prepare()
        return formula.apply(self.runtime, reviewed_plan=plan, expected_fingerprint=plan['fingerprint'],
            **self.apply_options, **options)

    def guarded_buyer_fixture(self):
        plan = self.prepare()
        timer = formula.BUYER_UNIT.replace('.service', '.timer')
        with self.assertRaisesRegex(RuntimeError, 'synthetic buyer timer boundary'):
            self.apply(plan, _fault=lambda p: (_ for _ in ()).throw(RuntimeError('synthetic buyer timer boundary'))
                if p == 'timer:start:' + timer else None)
        self.assertEqual(formula.load(self.runtime, OP)['phase'], 'restoring')
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=1)
        stop = start + timedelta(seconds=1)
        fmt = lambda value: value.strftime('%a %Y-%m-%d %H:%M:%S UTC')
        props = self.systemd.states[formula.BUYER_UNIT]['properties']
        props.update(ActiveState='inactive', SubState='dead', MainPID='0', Result='success',
            ExecMainCode='1', ExecMainStatus='0', ExecMainStartTimestamp=fmt(start), InvocationID='a' * 32)
        props['ExecStart'] = re.sub(r' ; start_time=.*', ' ; start_time=[' + fmt(start) +
            '] ; stop_time=[' + fmt(stop) + '] ; pid=2668333 ; code=exited ; status=0 }', props['ExecStart'])
        self.systemd.states[formula.BUYER_UNIT]['is_active'] = 'inactive'
        self.systemd.states[timer]['properties']['LastTriggerUSec'] = fmt(start)
        description = 'WB Core account-scoped buyer price observations'
        messages = ['Starting ' + formula.BUYER_UNIT + ' - ' + description + '...',
            '{"status": "skipped_maintenance", "reason": "skipped_maintenance"}',
            formula.BUYER_UNIT + ': Deactivated successfully.', 'Finished ' + formula.BUYER_UNIT + ' - ' + description + '.']
        events = []
        for i, micros in enumerate((10000, 1300000, 1400000, 1500000)):
            event = dict(MESSAGE=messages[i], _BOOT_ID='11111111222233334444555555555555',
                _PID='2668333' if i == 1 else '1', _SYSTEMD_UNIT=formula.BUYER_UNIT if i == 1 else 'init.scope',
                __REALTIME_TIMESTAMP=str(int(start.timestamp()) * 1000000 + micros))
            event.update(__SEQNUM_ID='c' * 32, __SEQNUM=str(i + 1), __MONOTONIC_TIMESTAMP=str(micros))
            event['__CURSOR'] = 's=' + 'c' * 32 + ';i=' + format(i + 1, 'x') + ';b=' + event['_BOOT_ID'] + ';m=' + format(micros, 'x') + ';t=' + format(int(event['__REALTIME_TIMESTAMP']), 'x') + ';x=1'
            if i == 1:event['_SYSTEMD_INVOCATION_ID'] = 'a' * 32
            else:event['UNIT'] = formula.BUYER_UNIT
            events.append(event)
        return plan, events

    def buyer_journal_bytes(self, events):
        return ('\n'.join(json.dumps(v) for v in events) + '\n').encode()

    def test_guarded_buyer_native_guard_before_main_and_same_plan_resume(self):
        plan, events = self.guarded_buyer_fixture()
        from packages.application.business_data_procedure_admission import guarded_cli
        called = []
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(guarded_cli(lambda: called.append('main'), argv=['--runtime-dir', str(self.runtime)]), 0)
        self.assertEqual(called, [])
        self.assertEqual(output.getvalue().strip(), events[1]['MESSAGE'])
        with patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)):
            receipt = self.apply(plan)
        deviation = receipt['guarded_service_completions'][formula.BUYER_UNIT]
        self.assertFalse(deviation['original_service_pair_restored'])
        self.assertEqual(deviation['original_pair'], ['static', 'failed'])
        self.assertEqual(deviation['observed_pair'], ['static', 'inactive'])
        self.assertEqual(formula.load(self.runtime, OP)['plan'], plan)
        self.assertEqual(pause.load_state(self.runtime)['baseline'], self.baseline)
        self.assertEqual(pause.load_state(self.runtime)['restore_readback'], receipt)
        self.assertEqual(formula.prove_recorded_release(formula.load(self.runtime, OP), barrier._load_state(self.runtime)), receipt)

    def test_guarded_buyer_preview_and_prepared_remain_strict(self):
        plan, events = self.guarded_buyer_fixture()
        state = formula.load(self.runtime, OP)
        state['phase'] = 'prepared'; formula._save(self.runtime, state, 'synthetic_prepared_boundary')
        with patch.object(formula, '_buyer_journal', side_effect=AssertionError('must not read witness before authority')):
            with self.assertRaisesRegex(RuntimeError, 'original restoring authority'):
                formula._observe(self.runtime, plan, **self.options)
        state['phase'] = 'restoring'; formula._save(self.runtime, state, 'synthetic_restoring_boundary')
        with self.assertRaises(RuntimeError):self.prepare()

    def test_guarded_buyer_missing_stale_foreign_malformed_journal_refuses(self):
        plan, events = self.guarded_buyer_fixture()
        cases = [[], events[:3], events + [events[-1]], [None], ['bad']]
        for key, value in [('_PID', '2668334'), ('_BOOT_ID', 'b' * 32),
                ('_SYSTEMD_UNIT', 'foreign.service'), ('_SYSTEMD_INVOCATION_ID', 'b' * 32),
                ('__REALTIME_TIMESTAMP', '1'), ('MESSAGE', '{"status":"success"}')]:
            altered = deepcopy(events); altered[1][key] = value; cases.append(altered)
        swapped = deepcopy(events); swapped[2], swapped[3] = swapped[3], swapped[2]; cases.append(swapped)
        for key in ('__CURSOR', '__MONOTONIC_TIMESTAMP', '__SEQNUM', '__SEQNUM_ID'):
            altered = deepcopy(events); altered[1].pop(key); cases.append(altered)
        for key, value in [('extra', 'unknown'), ('__CURSOR', 'unknown'), ('__SEQNUM', '999'),
                ('__MONOTONIC_TIMESTAMP', '999'), ('__SEQNUM_ID', 'd' * 32),
                ('__CURSOR', None), ('__CURSOR', []), ('__SEQNUM', float('nan'))]:
            altered = deepcopy(events); altered[1][key] = value; cases.append(altered)
        raw = self.buyer_journal_bytes(events)
        duplicate = raw.replace(b'"_PID": "2668333"', b'"_PID": "2668333", "_PID": "2668333"')
        raws = [self.buyer_journal_bytes(v) for v in cases] + [b'not-json', b'x' * 65537, duplicate]
        before = deepcopy(formula.load(self.runtime, OP)); calls = list(self.systemd.calls)
        for raw in raws:
            with self.subTest(raw=raw[:80]), patch.object(formula, '_buyer_journal', return_value=raw):
                with self.assertRaises(RuntimeError):formula._observe(self.runtime, plan, **self.options)
        with patch.object(formula, '_buyer_journal', side_effect=TimeoutError('synthetic native read timeout')):
            with self.assertRaises(TimeoutError):formula._observe(self.runtime, plan, **self.options)
        self.assertEqual(formula.load(self.runtime, OP), before)
        self.assertEqual(self.systemd.calls, calls)

    def test_guarded_buyer_terminal_source_config_and_timer_refusals(self):
        plan, events = self.guarded_buyer_fixture()
        props = self.systemd.states[formula.BUYER_UNIT]['properties']
        with patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)):
            for key, value in [('MainPID', '9'), ('SubState', 'running'), ('Result', 'exit-code'),
                    ('ExecMainStatus', '1'), ('ExecMainCode', '2'),
                    ('ExecMainStartTimestamp', 'Thu 2026-10-08 19:00:00 UTC'), ('InvocationID', 'b' * 32)]:
                original = props.get(key)
                with self.subTest(key=key):
                    props[key] = value
                    with self.assertRaises(RuntimeError):formula._observe(self.runtime, plan, **self.options)
                    props[key] = original
            self.systemd.states[formula.BUYER_UNIT]['is_active'] = 'active'
            with self.assertRaises(RuntimeError):formula._observe(self.runtime, plan, **self.options)
            self.systemd.states[formula.BUYER_UNIT]['is_active'] = 'inactive'
            timer = formula.BUYER_UNIT.replace('.service', '.timer')
            self.systemd.states[timer]['is_active'] = 'inactive'
            with self.assertRaises(RuntimeError):formula._observe(self.runtime, plan, **self.options)
            self.systemd.states[timer]['is_active'] = 'active'
            for relative in formula.BUYER_GUARD_SOURCES:
                path = self.app / relative; original = path.read_bytes(); path.write_bytes(original + b'\n# foreign\n')
                with self.assertRaisesRegex(RuntimeError, 'admission source'):formula._observe(self.runtime, plan, **self.options)
                path.write_bytes(original)
            original = props['ExecStart']; props['ExecStart'] = original.replace('--tick', '--run-now')
            with self.assertRaises(RuntimeError):formula._observe(self.runtime, plan, **self.options)
            props['ExecStart'] = original
        self.assertEqual(formula.load(self.runtime, OP)['plan'], plan)

    def test_guarded_buyer_crash_after_commit_same_invocation_finishes_without_resend(self):
        plan, events = self.guarded_buyer_fixture()
        with patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)):
            with self.assertRaisesRegex(RuntimeError, 'synthetic guarded committed crash'):
                self.apply(plan, _fault=lambda p: (_ for _ in ()).throw(RuntimeError('synthetic guarded committed crash')) if p == 'committed' else None)
            calls = list(self.systemd.calls)
        reordered = [dict(reversed(list(v.items()))) for v in events]
        with patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(reordered)):
            self.apply(plan)
        self.assertEqual(self.systemd.calls, calls)
        self.assertEqual(formula.load(self.runtime, OP)['phase'], 'released')

    def test_guarded_buyer_original_control_activity_process_and_owner_guards(self):
        plan, events = self.guarded_buyer_fixture()
        calls = list(self.systemd.calls)
        state = deepcopy(formula.load(self.runtime, OP))
        with patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)):
            for target, value in [('admission_idle', {'ready':False, 'idle':False}),
                    ('_writer_processes', [{'pid':'synthetic'}])]:
                with patch.object(pause, target, return_value=value):
                    with self.assertRaisesRegex(RuntimeError, 'raw controls/idle'):formula._observe(self.runtime, plan, **self.options)
            activity = self.activity(); activity['jobs'] = [{'job_id':'synthetic-live'}]
            with self.assertRaisesRegex(RuntimeError, 'raw controls/idle'):
                formula._observe(self.runtime, plan, self.systemd, lambda:activity, self.proc)
            paths = [self.runtime / pause.POLICY_FILENAME, self.app / '.wb-core-runtime-sha',
                self.app / next(iter(plan['authority']['formula_code_hashes']))]
            for path in paths:
                original = path.read_bytes(); path.write_bytes(original + b'\n# foreign\n')
                with self.assertRaises(RuntimeError):formula._observe(self.runtime, plan, **self.options)
                path.write_bytes(original)
            owner_path = self.runtime / deploy.FILENAME; original = owner_path.read_bytes()
            owner = deploy.load(self.runtime); owner['operation_id'] = 'foreign-owner'; deploy._save(self.runtime, owner)
            with self.assertRaisesRegex(RuntimeError, 'same completed canonical deploy owner'):formula._observe(self.runtime, plan, **self.options)
            owner_path.write_bytes(original)
        self.assertEqual(formula.load(self.runtime, OP), state)
        self.assertEqual(self.systemd.calls, calls)
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])

    def test_guarded_buyer_idle_job_process_and_unit_drift_during_journal_refuses(self):
        plan, events = self.guarded_buyer_fixture()
        original = pause.readback
        current = original(self.runtime, **self.options)
        other = next(u for u in current['units'] if u.endswith('.service') and u != formula.BUYER_UNIT)
        variants = []
        for key, value in [('admission', {'ready':True, 'idle':False}),
                ('writer_processes', [{'pid':'synthetic'}]), ('live_services', [other])]:
            fresh = deepcopy(current); fresh[key] = value; variants.append(fresh)
        fresh = deepcopy(current); fresh['activity']['jobs'] = [{'job_id':'new-job'}]; variants.append(fresh)
        fresh = deepcopy(current); fresh['controls']['owner_policy'] = 'foreign'; variants.append(fresh)
        fresh = deepcopy(current); fresh['units'][other]['is_active'] = 'active'; variants.append(fresh)
        for fresh in variants:
            with patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)), \
                 patch.object(pause, 'readback', side_effect=[deepcopy(current), fresh]):
                with self.assertRaisesRegex(RuntimeError, 'post-journal'):
                    formula._observe(self.runtime, plan, **self.options)
        self.assertEqual(formula.load(self.runtime, OP)['plan'], plan)
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])

    def test_guarded_buyer_new_invocation_after_commit_refuses_old_receipt(self):
        plan, events = self.guarded_buyer_fixture()
        with patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)):
            with self.assertRaisesRegex(RuntimeError, 'synthetic guarded committed crash'):
                self.apply(plan, _fault=lambda p: (_ for _ in ()).throw(RuntimeError('synthetic guarded committed crash')) if p == 'committed' else None)
        state = deepcopy(formula.load(self.runtime, OP)); calls = list(self.systemd.calls)
        self.systemd.states[formula.BUYER_UNIT]['properties']['InvocationID'] = 'b' * 32
        events[1]['_SYSTEMD_INVOCATION_ID'] = 'b' * 32
        with patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)):
            with self.assertRaisesRegex(RuntimeError, 'retained guarded service completion differs'):
                self.apply(plan)
        self.assertEqual(formula.load(self.runtime, OP), state)
        self.assertEqual(self.systemd.calls, calls)
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])

    def test_guarded_buyer_native_journal_transport_byte_timeout_and_exit_bounds(self):
        # Real private child pipes exercise the native bounded transport; the
        # captured journalctl argv never reaches systemd/journald on this host.
        original = formula.subprocess.Popen
        commands = []
        def child(code):
            def spawn(command, **options):
                commands.append(command)
                return original([sys.executable, '-c', code], **options)
            return spawn
        with patch.object(formula.subprocess, 'Popen', side_effect=child('print("bounded")')):
            self.assertEqual(formula._buyer_journal('a' * 32, 1, 2), b'bounded\n')
        for code, reason in [('import sys;sys.stdout.write("x"*65537)', 'exceeds bound'),
                ('raise SystemExit(1)', 'read failed')]:
            with patch.object(formula.subprocess, 'Popen', side_effect=child(code)):
                with self.assertRaisesRegex(RuntimeError, reason):formula._buyer_journal('a' * 32, 1, 2)
        with patch.object(formula.subprocess, 'Popen', side_effect=child('import time;time.sleep(10)')), \
             patch.object(formula.time, 'monotonic', side_effect=[0, 6]):
            with self.assertRaisesRegex(RuntimeError, 'timeout'):formula._buyer_journal('a' * 32, 1, 2)
        self.assertTrue(all(c[:6] == ['/usr/bin/journalctl', '--no-pager', '--output=json', '--all', '-n', '5'] for c in commands))

    def buyer_reset_fields(self):
        props = self.systemd.states[formula.BUYER_UNIT]['properties']
        terminal = re.search(r' ; start_time=\[([^]]+)\] ; stop_time=\[([^]]+)\] ; pid=([1-9][0-9]*) ; code=exited ; status=0 \}$', props['ExecStart'])
        return dict(ExecMainPID=terminal[3], ExecMainStartTimestamp=terminal[1],
            ExecMainExitTimestamp=terminal[2], InvocationID=props['InvocationID'])

    @contextmanager
    def buyer_reset_native_read(self, payload):
        native_read = formula._buyer_native_read
        command = ['/usr/bin/systemctl', 'show', formula.BUYER_UNIT,
            '--property=ExecMainPID,ExecMainStartTimestamp,ExecMainExitTimestamp,InvocationID', '--no-pager']
        with patch.object(formula, '_buyer_native_read', side_effect=lambda args:
                payload() if args == command else native_read(args)):
            yield

    def test_guarded_buyer_canonical_enable_reset_terminal_resumes_same_receipt(self):
        plan, events = self.guarded_buyer_fixture()
        fields = self.buyer_reset_fields()
        native_run = self.systemd._run
        def enable_and_reload(args):
            result = native_run(args)
            if args[0] == 'enable':
                props = self.systemd.states[formula.BUYER_UNIT]['properties']
                props['ExecStart'] = re.sub(r' ; start_time=.*',
                    ' ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }', props['ExecStart'])
            return result
        from packages.application.business_data_schedule_profile import WAREHOUSE_TIMER
        self.systemd.states[WAREHOUSE_TIMER].update(is_enabled='disabled', is_active='inactive')
        payload = lambda: ('\n'.join(k + '=' + v for k, v in fields.items()) + '\n').encode()
        with self.buyer_reset_native_read(payload), patch.object(self.systemd, '_run', side_effect=enable_and_reload), \
                patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)):
            receipt = self.apply(plan)
            proof = receipt['guarded_service_completions'][formula.BUYER_UNIT]
            self.assertEqual([proof[k] for k in ('pid', 'start', 'stop')],
                [fields[k] for k in ('ExecMainPID', 'ExecMainStartTimestamp', 'ExecMainExitTimestamp')])
            self.assertFalse(proof['original_service_pair_restored'])
            self.assertIn(('enable', WAREHOUSE_TIMER), self.systemd.calls)
            calls = list(self.systemd.calls)
            self.assertEqual(self.apply(plan), receipt)
            self.assertEqual(self.systemd.calls, calls)

    def test_guarded_buyer_reset_terminal_native_fields_fail_closed(self):
        plan, events = self.guarded_buyer_fixture()
        fields = self.buyer_reset_fields()
        props = self.systemd.states[formula.BUYER_UNIT]['properties']
        props['ExecStart'] = re.sub(r' ; start_time=.*',
            ' ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }', props['ExecStart'])
        variants = []
        for key in fields:
            missing = dict(fields); missing.pop(key); variants.append(missing)
        for key, value in [('ExecMainPID', '0'), ('ExecMainPID', '2671797'),
                ('ExecMainPID', '2147483648'), ('ExecMainStartTimestamp', 'n/a'),
                ('ExecMainStartTimestamp', 'Sat 2026-10-10 00:10:08 UTC'),
                ('ExecMainExitTimestamp', 'Thu 2020-10-08 00:00:00 UTC'),
                ('ExecMainExitTimestamp', 'bad'), ('InvocationID', 'b' * 32), ('InvocationID', '')]:
            changed = dict(fields); changed[key] = value; variants.append(changed)
        variants.append(dict(fields, Unexpected='1'))
        variants.append(dict(fields, ExecMainExitTimestamp=(datetime.strptime(
            fields['ExecMainExitTimestamp'], '%a %Y-%m-%d %H:%M:%S UTC') + timedelta(seconds=10)).strftime('%a %Y-%m-%d %H:%M:%S UTC')))
        for changed in variants:
            with self.subTest(fields=changed), self.buyer_reset_native_read(lambda:
                    ('\n'.join(k + '=' + v for k, v in changed.items()) + '\n').encode()), \
                    patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)):
                with self.assertRaises(RuntimeError):
                    formula._observe(self.runtime, plan, **self.options)
        normal = '\n'.join(k + '=' + v for k, v in fields.items()) + '\n'
        for raw in (normal + 'ExecMainPID=' + fields['ExecMainPID'] + '\n', normal + '\n', '\xff'):
            with self.subTest(raw=raw), self.buyer_reset_native_read(lambda: raw.encode('latin1')):
                with self.assertRaises(RuntimeError):
                    formula._observe(self.runtime, plan, **self.options)
        self.assertEqual(formula.load(self.runtime, OP)['phase'], 'restoring')
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])

    def test_guarded_buyer_partial_reset_and_terminal_read_drift_refuse(self):
        plan, events = self.guarded_buyer_fixture()
        fields = self.buyer_reset_fields()
        props = self.systemd.states[formula.BUYER_UNIT]['properties']
        command = props['ExecStart'].split(' ; start_time=')[0]
        with patch.object(formula, '_buyer_reset_terminal') as native:
            for tail in ('[n/a] ; stop_time=[n/a] ; pid=1 ; code=(null) ; status=0/0 }',
                    '[n/a] ; stop_time=[n/a] ; pid=0 ; code=exited ; status=0 }'):
                props['ExecStart'] = command + ' ; start_time=' + tail
                with self.assertRaises(RuntimeError):
                    formula._observe(self.runtime, plan, **self.options)
            native.assert_not_called()
        props['ExecStart'] = command + ' ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }'
        reads = []
        def changed_payload():
            reads.append(1)
            current = dict(fields)
            if len(reads) > 1: current['ExecMainPID'] = '2671797'
            return ('\n'.join(k + '=' + v for k, v in current.items()) + '\n').encode()
        with self.buyer_reset_native_read(changed_payload), \
                patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)):
            with self.assertRaisesRegex(RuntimeError, 'reset terminal readback changed'):
                formula._observe(self.runtime, plan, **self.options)
        self.assertEqual(formula.load(self.runtime, OP)['phase'], 'restoring')

    def test_guarded_buyer_committed_full_terminal_then_reset_retains_exact_proof(self):
        plan, events = self.guarded_buyer_fixture()
        fields = self.buyer_reset_fields()
        with patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)):
            with self.assertRaisesRegex(RuntimeError, 'synthetic committed reset'):
                self.apply(plan, _fault=lambda p: (_ for _ in ()).throw(RuntimeError('synthetic committed reset'))
                    if p == 'committed' else None)
            saved = formula.load(self.runtime, OP)['receipt']
            calls = list(self.systemd.calls)
            props = self.systemd.states[formula.BUYER_UNIT]['properties']
            props['ExecStart'] = re.sub(r' ; start_time=.*',
                ' ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }', props['ExecStart'])
            with self.buyer_reset_native_read(lambda:
                    ('\n'.join(k + '=' + v for k, v in fields.items()) + '\n').encode()):
                self.assertEqual(self.apply(plan), saved)
                self.assertEqual(self.systemd.calls, calls)

    def test_guarded_buyer_reset_terminal_drift_during_fresh_readback_refuses(self):
        plan, events = self.guarded_buyer_fixture()
        fields = self.buyer_reset_fields()
        props = self.systemd.states[formula.BUYER_UNIT]['properties']
        props['ExecStart'] = re.sub(r' ; start_time=.*',
            ' ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }', props['ExecStart'])
        native_readback = pause.readback
        reads = []
        def fresh_then_changed(*args, **kwargs):
            result = native_readback(*args, **kwargs); reads.append(1)
            if len(reads) == 2: fields['ExecMainPID'] = '2671797'
            return result
        with self.buyer_reset_native_read(lambda:
                ('\n'.join(k + '=' + v for k, v in fields.items()) + '\n').encode()), \
                patch.object(pause, 'readback', side_effect=fresh_then_changed), \
                patch.object(formula, '_buyer_journal', return_value=self.buyer_journal_bytes(events)):
            with self.assertRaisesRegex(RuntimeError, 'reset terminal readback changed'):
                formula._observe(self.runtime, plan, **self.options)
        self.assertEqual(formula.load(self.runtime, OP)['phase'], 'restoring')

    def test_realistic_runtime_reset_and_committed_timer_rearm_keep_static_proof(self):
        plan = self.prepare()
        current = self.systemd.unit_state(pause.TIMERS[0])
        original = self.baseline['units'][pause.TIMERS[0]]
        self.assertEqual(original['properties']['TimersMonotonic'], '{ OnActiveUSec=5min ; next_elapse=2month 4w 1d 20h 13min 33.498883s }')
        self.assertEqual(current['properties']['TimersMonotonic'], '{ OnActiveUSec=5min ; next_elapse=0 }')
        self.assertTrue(formula._same_unit_configuration(original, current))
        canary = 'wb-core-sheet-vitrina-canary-restore.service'
        self.assertIn('pid=2419611 ; code=exited ; status=0', self.baseline['units'][canary]['properties']['ExecStart'])
        self.assertIn('pid=0 ; code=(null) ; status=0/0', self.systemd.unit_state(canary)['properties']['ExecStart'])
        self.assertTrue(formula._same_unit_configuration(self.baseline['units'][canary], self.systemd.unit_state(canary)))
        with self.assertRaisesRegex(RuntimeError, 'synthetic committed crash'):
            self.apply(plan, _fault=lambda point: (_ for _ in ()).throw(RuntimeError('synthetic committed crash')) if point == 'committed' else None)
        calls = list(self.systemd.calls)
        for unit in pause.TIMERS:
            props = self.systemd.states[unit]['properties']
            if 'TimersCalendar' in props:
                props['TimersCalendar'] = props['TimersCalendar'].replace('Thu 2026-10-08 19:40:00 UTC', 'Fri 2026-10-09 01:30:00 UTC')
        formula.prove_committed(self.runtime, OP, **self.options)
        self.apply(plan)
        self.assertEqual(calls, self.systemd.calls)
        self.assertEqual(self.baseline, pause.load_state(self.runtime)['baseline'])

    def test_unknown_or_malformed_full_native_shapes_never_get_equality_fallback(self):
        exec_raw = self.systemd.unit_state(formula.UNIT)['properties']['ExecStart']
        calendar = '{ OnCalendar=*-*-* 00/2:17:00 Asia/Yekaterinburg ; next_elapse=Thu 2026-10-08 21:17:00 UTC }'
        monotonic = '{ OnBootUSec=10min ; next_elapse=10min }'
        cases = [('ExecStart', value) for value in (
            exec_raw + ' extra', exec_raw + ' ' + exec_raw,
            exec_raw.replace(' ; pid=', ' ; unknown=1 ; pid='),
            exec_raw.replace('status=0/0', 'status=0/SUCCESS'),
            exec_raw.replace('pid=0', 'pid=-1'), exec_raw.replace('pid=0', 'pid=999999999999'),
            exec_raw.replace('pid=0', 'pid=42'), exec_raw.replace('ignore_errors=no', 'ignore_errors=maybe'),
            exec_raw.replace(' ; start_time=', '\n ; start_time='), exec_raw + '\x00',
            exec_raw.replace(' ; ignore_errors=', ' ; injected=1 ; ignore_errors='),
            '{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 apps/test.py ; ignore_errors=no ; }',
        )] + [('TimersCalendar', value) for value in (
            calendar + ' garbage', calendar + ' ' + calendar,
            calendar.replace('Thu 2026-10-08', 'Fri 2026-10-08'),
            calendar.replace('2026-10-08', '2026-02-30'),
            calendar.replace('next_elapse=', 'foreign='), calendar.replace(' ; next_elapse=', ' ; injected=1 ; next_elapse='),
            calendar.replace('OnCalendar=', 'Unknown='), calendar.replace('21:17:00', '25:17:00'),
            '{ OnCalendar=9999-99-99 99:99:99 Europe/Moscow ; next_elapse=(null) }',
            '{ OnCalendar=* * ; next_elapse=(null) }',
            calendar.replace('*-*-*', '2026-02-30'), calendar.replace('*-*-*', '0000-01-01'),
            calendar.replace('00/2:17:00', '24:17:00'), calendar.replace('00/2:17:00', '00:60:00'),
            calendar.replace('00/2:17:00', '00:17:60'), calendar.replace('00/2:17:00', '00/0:17:00'),
            calendar.replace('00/2:17:00', '00/24:17:00'), calendar.replace('00/2:17:00', '00:17/2:00'),
            calendar.replace('00/2:17:00', '00:17,17:00'), calendar.replace('00/2:17:00', '00:20,17:00'),
            calendar.replace('Asia/Yekaterinburg', 'Foreign/Unknown'),
        )] + [('TimersMonotonic', value) for value in (
            monotonic.replace('10min }', '-1min }'), monotonic.replace('10min }', 'unknown }'),
            monotonic.replace('OnBootUSec', 'OnForeignUSec'), monotonic + ' ', monotonic + '\r',
            monotonic.replace('next_elapse=10min', 'next_elapse=' + '9' * 69 + 'min'),
            monotonic.replace('OnBootUSec=10min', 'OnBootUSec=' + '9' * 69 + 'min'),
            monotonic.replace('next_elapse=10min', 'next_elapse=18446744073709551615us'),
            monotonic.replace('next_elapse=10min', 'next_elapse=18446744073709551616us'),
            monotonic.replace('next_elapse=10min', 'next_elapse=1min 18446744073649551615us'),
            monotonic.replace('next_elapse=10min', 'next_elapse=18446744073710s'),
            monotonic.replace('next_elapse=10min', 'next_elapse=01min'),
            monotonic.replace('next_elapse=10min', 'next_elapse=0.0000001s'),
            monotonic.replace('next_elapse=10min', 'next_elapse=0.1us'),
            monotonic.replace('next_elapse=10min', 'next_elapse=1min 1h'),
        )]
        for name, raw in cases:
            with self.subTest(property=name, raw=raw), self.assertRaises(RuntimeError):
                formula._loaded_records(raw, name)
        # Accepted finite boundary and observed normalized families retain
        # exact text; validation must not collapse different static schedules.
        for operand in ('*-*-* 00/2:17:00 Asia/Yekaterinburg', '*-*-* 00,03,06,09,12,15,18,21:00:00 Europe/Moscow',
                        '*-*-* *:00,10,20,30,40,50:00', '*-*-* 01/2:55:00 Asia/Tbilisi', '2024-02-29 23:59:59 UTC'):
            raw = '{ OnCalendar=' + operand + ' ; next_elapse=(null) }'
            self.assertEqual(formula._loaded_records(raw, 'TimersCalendar'), (('OnCalendar', operand),))
        for duration in ('18446744073709551614us', '2month 4w 1d 20h 13min 33.498883s', '0.000001s', '0'):
            formula._validate_duration(duration)
        unit = self.systemd.unit_state('wb-core-sheet-vitrina-canary-restore.service')
        for value in (None, '', 'sha256:bad'):
            bad = deepcopy(unit); bad['properties']['UnitContentDigest'] = value
            with self.subTest(digest=value), self.assertRaises(RuntimeError):
                formula._same_unit_configuration(bad, bad)
        missing = deepcopy(unit); missing['properties'].pop('ExecStart')
        empty = deepcopy(missing); empty['properties']['ExecStart'] = ''
        self.assertFalse(formula._same_unit_configuration(missing, empty))

    def test_real_command_schedule_and_loaded_flag_drift_block_without_timer_action(self):
        plan = self.prepare(); calls = list(self.systemd.calls)
        canary = 'wb-core-sheet-vitrina-canary-restore.service'
        calendar_unit = next(u for u in pause.TIMERS if 'TimersCalendar' in self.systemd.states[u]['properties'])
        changes = [
            (canary, 'ExecStart', lambda v: v.replace('path=/usr/bin/python3', 'path=/usr/bin/python4')),
            (canary, 'ExecStart', lambda v: v.replace('apps/synthetic.py', 'apps/foreign.py')),
            (canary, 'ExecStart', lambda v: v.replace('ignore_errors=no', 'ignore_errors=yes')),
            (pause.TIMERS[0], 'TimersMonotonic', lambda v: v.replace('OnActiveUSec', 'OnBootUSec')),
            (pause.TIMERS[0], 'TimersMonotonic', lambda v: v.replace('5min', '6min')),
            (calendar_unit, 'TimersCalendar', lambda v: v.replace('*:00,10,20,30,40,50:00', '*:01,10,20,30,40,50:00')),
            (calendar_unit, 'TimersCalendar', lambda v: v.replace(' ; next_elapse', ' Europe/Moscow ; next_elapse')),
        ]
        for unit, key, mutate in changes:
            props = self.systemd.states[unit]['properties']; previous = props[key]; props[key] = mutate(previous)
            try:
                with self.subTest(unit=unit, field=key), self.assertRaisesRegex(RuntimeError, 'foreign unit configuration'):
                    self.apply(plan)
                self.assertEqual(self.systemd.calls, calls)
                self.assertFalse((self.runtime / formula.DIRECTORY).exists())
            finally:
                props[key] = previous
        self.systemd.states[canary].update(is_active='active')
        self.systemd.states[canary]['properties'].update(MainPID='42', SubState='running')
        with self.assertRaisesRegex(RuntimeError, 'quiet|idle'):
            self.prepare()
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])

    def test_exact_formula_target_preserves_original_baseline_and_repeated_read(self):
        plan = self.prepare(); calls = list(self.systemd.calls)
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])
        self.assertFalse((self.runtime / formula.DIRECTORY).exists())
        self.assertEqual(plan['delta']['old_unit_digest'],self.baseline['units'][formula.UNIT]['properties']['UnitContentDigest'])
        receipt = self.apply(plan)
        self.assertFalse(receipt['exact_prior_state_restored']);self.assertTrue(receipt['exact_target_state_restored'])
        self.assertFalse(barrier.barrier_status(self.runtime)['active'])
        self.assertEqual(pause.load_state(self.runtime)['baseline'],self.baseline)
        self.assertEqual((self.units / formula.UNIT).read_bytes(),self.new_artifact)
        self.assertTrue(len(self.systemd.calls) > len(calls))
        before = {p:p.read_bytes() for p in self.runtime.rglob('*') if p.is_file()}
        calls = list(self.systemd.calls)
        self.assertEqual(self.apply(plan),receipt)
        self.assertEqual(self.systemd.calls,calls)
        self.assertEqual(before,{p:p.read_bytes() for p in self.runtime.rglob('*') if p.is_file()})

    def test_ordinary_resume_is_strict_before_and_after_target(self):
        calls = list(self.systemd.calls)
        with self.assertRaisesRegex(RuntimeError,'unit configuration changed'):
            pause.resume(self.runtime, **self.pause_options)
        self.assertEqual(calls,self.systemd.calls)
        self.apply()
        with self.assertRaisesRegex(RuntimeError,'unit configuration changed'):
            pause.resume(self.runtime, **self.pause_options)

    def test_crashes_at_all_boundary_classes_recover_without_repeating_timer_command(self):
        # Subtests share only their own transition. Each selected point crashes
        # once, then same-plan recovery consumes observed source state.
        plan = self.prepare()
        points = ['prepared','timer:enable:'+pause.TIMERS[0],'committed','receipt_retained','barrier_released']
        pending = list(points)
        def fault(point):
            if pending and point == pending[0]:
                pending.pop(0);raise RuntimeError('synthetic process interruption')
        for expected in points:
            with self.assertRaisesRegex(RuntimeError,'synthetic process'):
                self.apply(plan,_fault=fault)
            self.assertEqual(pause.load_state(self.runtime)['baseline'],self.baseline)
        self.assertEqual(pending,[])
        self.apply(plan)
        self.assertEqual(self.systemd.calls.count(('enable',pause.TIMERS[0])),1)
        self.assertEqual(formula.load(self.runtime,OP)['phase'],'released')

    def test_partial_transition_blocks_ordinary_release_even_if_fragment_reverted(self):
        plan = self.prepare()
        with self.assertRaises(RuntimeError):
            self.apply(plan,_fault=lambda p: (_ for _ in ()).throw(RuntimeError('crash')) if p=='prepared' else None)
        (self.units / formula.UNIT).write_bytes(self.original_artifact)
        props = self.systemd.states[formula.UNIT]['properties'];props['ExecStart']=props['ExecStart'].replace(self.new_epoch,self.old_epoch)
        with self.assertRaisesRegex(RuntimeError,'same-operation recovery'):
            pause.resume(self.runtime, **self.pause_options)
        with self.assertRaisesRegex(RuntimeError,'same-operation recovery'):
            barrier.release_barrier(self.runtime,window_id=plan['window_id'],plan_fingerprint=plan['baseline_fingerprint'],
                actor='test',reason='incorrect bypass',restore_readback=dict(status='restored',exact_prior_state_restored=True))

    def test_foreign_fragment_delta_fails_even_when_installed_equals_current_artifact(self):
        for p in (self.units / formula.UNIT,self.app / formula.UNIT_RELATIVE):
            p.write_bytes(p.read_bytes()+b'\nEnvironment=FOREIGN=1\n')
        with self.assertRaisesRegex(RuntimeError,'aggregate baseline reconstruction'):
            self.prepare()
        self.assertFalse((self.runtime / formula.DIRECTORY).exists())

    def test_changed_dropin_or_path_cannot_be_absorbed(self):
        self.dropin.write_bytes(b'[Service]\nEnvironment=FOREIGN=1\n')
        with self.assertRaisesRegex(RuntimeError,'aggregate baseline reconstruction'):
            self.prepare()

    def test_loaded_execstart_other_delta_rejected(self):
        self.systemd.states[formula.UNIT]['properties']['ExecStart'] += ' FOREIGN=1'
        with self.assertRaisesRegex(RuntimeError,'loaded ExecStart has another delta'):
            self.prepare()

    def test_actual_formula_code_and_contract_epoch_required(self):
        (self.app / 'packages/application/fbs_snapshot_cost.py').write_bytes(b'changed after complete')
        with self.assertRaisesRegex(RuntimeError,'actual formula source hash'):
            self.prepare()

    def test_unfinished_foreign_owner_and_incomplete_metadata_refused(self):
        native = deploy.load(self.runtime)
        deploy._save(self.runtime,dict(native,phase='mutation_started'))
        with self.assertRaisesRegex(RuntimeError,'completed canonical deploy owner'):
            self.prepare()
        deploy._save(self.runtime,dict(native,operation_id='foreign-deploy-001'))
        with self.assertRaisesRegex(RuntimeError,'completed canonical deploy owner'):
            self.prepare()
        deploy._save(self.runtime,native)
        meta=self.app / '.wb-core-deploy.json';value=json.loads(meta.read_bytes());value['deployment_complete']=False;meta.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError,'completed runtime markers'):
            self.prepare()

    def test_timeout_absence_and_registry_unknown_never_permit_resume(self):
        with self.assertRaises(TimeoutError):
            formula.preview(self.runtime,**dict(self.preview_options,activity_reader=lambda: (_ for _ in ()).throw(TimeoutError())))
        self.systemd.states['wb-core-registry-http.service']['properties']['MainPID']='0'
        with self.assertRaisesRegex(RuntimeError,'live registry proof'):
            self.prepare()
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])

    def test_symlink_evidence_and_destination_are_refused(self):
        fragment = self.units / formula.UNIT; fragment.unlink();fragment.symlink_to(self.app / formula.UNIT_RELATIVE)
        with self.assertRaisesRegex(RuntimeError,'non-symlink'):
            self.prepare()
        fragment.unlink();fragment.write_bytes(self.new_artifact)
        root=self.runtime / formula.DIRECTORY;root.symlink_to(self.root / 'foreign')
        with self.assertRaisesRegex(RuntimeError,'directory is unsafe'):
            self.prepare()

    def test_pre_apply_drift_blocks_before_first_intent_or_timer_action(self):
        plan=self.prepare();calls=list(self.systemd.calls)
        self.systemd.states[pause.TIMERS[0]]['properties']['ExecStart']='foreign loaded command'
        with self.assertRaisesRegex(RuntimeError,'foreign unit configuration'):
            self.apply(plan)
        self.assertEqual(calls,self.systemd.calls);self.assertFalse((self.runtime / formula.DIRECTORY).exists())

    def test_ambiguous_timer_write_recovery_reads_exact_state_before_actions(self):
        plan=self.prepare();self.systemd.fail_once=('enable',pause.TIMERS[0])
        with self.assertRaisesRegex(RuntimeError,'ambiguous restore'):
            self.apply(plan)
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])
        self.apply(plan)
        self.assertEqual(self.systemd.calls.count(('enable',pause.TIMERS[0])),1)

    def test_owner_window_baseline_sha_and_markers_are_exact(self):
        native=deploy.load(self.runtime)
        for overrides in ({'window_id':'foreign-pause-001'}, {'baseline_fingerprint':'sha256:'+'b'*64},
                          {'expected_sha':'b'*40}):
            with self.subTest(overrides=overrides):
                deploy._save(self.runtime,dict(native,**overrides))
                with self.assertRaises(RuntimeError):self.prepare()
                self.assertTrue(barrier.barrier_status(self.runtime)['active'])
        deploy._save(self.runtime,native)
        (self.app / '.wb-core-runtime-sha').write_text('b'*40)
        with self.assertRaisesRegex(RuntimeError,'runtime markers'):self.prepare()

    def test_raw_controls_and_post_action_drift_keep_barrier_closed(self):
        plan=self.prepare();calls=list(self.systemd.calls)
        policy=self.runtime / pause.POLICY_FILENAME;original=policy.read_bytes()
        policy.write_bytes(original+b'\n')
        with self.assertRaisesRegex(RuntimeError,'raw controls'):self.apply(plan)
        self.assertEqual(self.systemd.calls,calls)
        policy.write_bytes(original)
        def change_after_enable(point):
            if point=='timer:enable:'+pause.TIMERS[0]:
                self.dropin.write_bytes(b'[Service]\nEnvironment=FOREIGN=1\n')
        with self.assertRaisesRegex(RuntimeError,'aggregate baseline reconstruction'):
            self.apply(plan,_fault=change_after_enable)
        self.assertEqual(self.systemd.calls.count(('enable',pause.TIMERS[0])),1)
        self.assertNotIn(('start',pause.TIMERS[0]),self.systemd.calls[len(calls):])
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])

    def test_uncommitted_direct_release_and_forged_target_receipt_are_refused(self):
        plan=self.prepare()
        with self.assertRaisesRegex(RuntimeError,'committed transition is absent'):
            barrier.release_formula_target_barrier(self.runtime,operation_id=OP,**self.apply_options)
        with self.assertRaisesRegex(RuntimeError,'stop after commit'):
            self.apply(plan,_fault=lambda point: (_ for _ in ()).throw(RuntimeError('stop after commit')) if point=='committed' else None)
        state=formula.load(self.runtime,OP)
        state['receipt']['expected_sha']='b'*40
        state['fingerprint']=pause.fingerprint({k:v for k,v in state.items() if k!='fingerprint'})
        barrier._atomic_write_private_json(formula._path(self.runtime,OP),state)
        with self.assertRaisesRegex(RuntimeError,'committed receipt differs'):
            barrier.release_formula_target_barrier(self.runtime,operation_id=OP,**self.apply_options)
        self.assertTrue(barrier.barrier_status(self.runtime)['active'])

    def test_installed_canonical_bytes_and_singular_pin_are_required(self):
        fragment=self.units / formula.UNIT
        fragment.write_bytes(self.new_artifact+b'\n')
        with self.assertRaisesRegex(RuntimeError,'not canonical deploy artifact'):self.prepare()
        duplicate=self.new_artifact+('--formula-epoch '+self.new_epoch+'\n').encode()
        fragment.write_bytes(duplicate);(self.app / formula.UNIT_RELATIVE).write_bytes(duplicate)
        with self.assertRaisesRegex(RuntimeError,'pin is not singular'):self.prepare()

    def test_direct_release_requires_exact_retained_pause_receipt_before_any_release_action(self):
        plan=self.prepare()
        with self.assertRaisesRegex(RuntimeError,'committed crash'):
            self.apply(plan,_fault=lambda point: (_ for _ in ()).throw(RuntimeError('committed crash')) if point=='committed' else None)
        calls=list(self.systemd.calls)
        original=barrier._load_state(self.runtime)
        for retained in (None,dict(status='restored',operation_id='foreign')):
            old=pause.load_state(self.runtime)
            if retained is None:old.pop('restore_readback',None)
            else:old['restore_readback']=retained
            pause.save_state(self.runtime,old,'synthetic_missing_or_foreign_retained_receipt')
            with patch('packages.application.business_data_cycle_wakeup.arm_formula_before_release',
                       side_effect=AssertionError('must not arm without retained receipt')):
                with self.assertRaisesRegex(RuntimeError,'retained pause receipt differs'):
                    barrier.release_formula_target_barrier(self.runtime,operation_id=OP,**self.apply_options)
            self.assertEqual(barrier._load_state(self.runtime),original)
            self.assertEqual(self.systemd.calls,calls)
            self.assertEqual(pause.load_state(self.runtime)['baseline'],self.baseline)
        old=pause.load_state(self.runtime);old.pop('restore_readback')
        pause.save_state(self.runtime,old,'synthetic_fixture_reset')
        self.apply(plan)
        self.assertEqual(self.systemd.calls,calls)
        self.assertEqual(formula.load(self.runtime,OP)['phase'],'released')

    def test_other_service_enabled_or_activity_state_cannot_drift(self):
        unit=pause.TIMERS[0].removesuffix('.timer')+'.service'
        self.systemd.states[unit]['is_enabled']='enabled'
        with self.assertRaisesRegex(RuntimeError,'held service enabled/activity'):self.prepare()
        self.systemd.states[unit]['is_enabled']=self.baseline['units'][unit]['is_enabled']
        self.systemd.states[unit]['is_active']='failed'
        with self.assertRaisesRegex(RuntimeError,'held service enabled/activity'):self.prepare()

    def test_released_crash_recovery_completes_metadata_with_running_jobs_and_no_live_reads(self):
        plan=self.prepare()
        with self.assertRaisesRegex(RuntimeError,'crash after release'):
            self.apply(plan,_fault=lambda p: (_ for _ in ()).throw(RuntimeError('crash after release')) if p=='barrier_released' else None)
        state=formula.load(self.runtime,OP);self.assertEqual(state['phase'],'committed')
        receipt=deepcopy(state['receipt']);calls=list(self.systemd.calls)
        self.assertFalse(barrier.barrier_status(self.runtime)['active'])
        unit=pause.TIMERS[0].removesuffix('.timer')+'.service'
        self.systemd.states[unit].update(is_active='active');self.systemd.states[unit]['properties']['MainPID']='123'
        # A lawful cycle has started and current app markers may change. Neither
        # is part of this historical completion authority after admission opens.
        (self.app / '.wb-core-runtime-sha').write_text('b'*40)
        released_bytes=(self.runtime / barrier.STATE_FILENAME).read_bytes()
        self.apply_options['activity_reader']=lambda: dict(self.activity(),jobs=[{'job_id':'lawful-cycle'}])
        with patch.object(formula,'_observe',side_effect=AssertionError('no fresh equality')), \
             patch.object(wake,'arm_formula_before_release',side_effect=AssertionError('no second arm')):
            result=barrier.release_formula_target_barrier(self.runtime,operation_id=OP,**self.apply_options)
            self.assertTrue(result['idempotent'])
        with patch.object(formula,'_observe',side_effect=AssertionError('live restore proof must not run')), \
             patch.object(barrier,'release_formula_target_barrier',side_effect=AssertionError('must not release twice')):
            self.assertEqual(self.apply(plan),receipt)
        self.assertEqual(calls,self.systemd.calls)
        self.assertEqual(released_bytes,(self.runtime / barrier.STATE_FILENAME).read_bytes())
        self.assertEqual(formula.load(self.runtime,OP)['phase'],'released')

    def test_pause_restored_gap_blocks_new_window_and_same_operation_finishes(self):
        plan=self.prepare()
        with self.assertRaisesRegex(RuntimeError,'crash after pause restore'):
            self.apply(plan,_fault=lambda p: (_ for _ in ()).throw(RuntimeError('crash after pause restore')) if p=='pause_restored' else None)
        self.assertEqual(pause.load_state(self.runtime)['phase'],'restored')
        self.assertEqual(formula.load(self.runtime,OP)['phase'],'committed')
        calls=list(self.systemd.calls);pause_bytes=(self.runtime / pause.STATE_FILENAME).read_bytes()
        with self.assertRaisesRegex(RuntimeError,'same-operation recovery'):
            pause.preflight(self.runtime,systemd=self.systemd,activity_reader=self.activity)
        with self.assertRaisesRegex(RuntimeError,'same-operation recovery'):
            pause.pause(self.runtime,**dict(self.pause_options,window_id='new-pause-001'))
        with self.assertRaisesRegex(RuntimeError,'same-operation recovery'):
            barrier.acquire_barrier(self.runtime,window_id='new-snapshot-001',window_kind='snapshot',
                plan_fingerprint='sha256:'+'b'*64,approval_reference='new-snapshot-001',actor='test',reason='new boundary')
        self.assertEqual(pause_bytes,(self.runtime / pause.STATE_FILENAME).read_bytes())
        with patch.object(formula,'_observe',side_effect=AssertionError('no current read')):
            receipt=self.apply(plan)
        self.assertEqual(calls,self.systemd.calls)
        self.assertEqual(pause_bytes,(self.runtime / pause.STATE_FILENAME).read_bytes())
        self.assertEqual(formula.load(self.runtime,OP)['release_proof']['restore']['readback_fingerprint'],barrier._fingerprint(receipt))

    def test_historical_bookkeeping_never_overwrites_foreign_retained_pause_proof(self):
        plan=self.prepare()
        with self.assertRaisesRegex(RuntimeError,'release crash'):
            self.apply(plan,_fault=lambda p: (_ for _ in ()).throw(RuntimeError('release crash')) if p=='barrier_released' else None)
        old=pause.load_state(self.runtime)
        old['restore_readback']=dict(old['restore_readback'],operation_id='foreign')
        pause.save_state(self.runtime,old,'synthetic_foreign_pause_receipt')
        files={p:p.read_bytes() for p in self.runtime.rglob('*') if p.is_file()}
        calls=list(self.systemd.calls)
        with patch.object(formula,'_observe',side_effect=AssertionError('no current read')):
            with self.assertRaisesRegex(RuntimeError,'retained pause receipt differs before bookkeeping'):
                self.apply(plan)
        self.assertEqual(calls,self.systemd.calls)
        self.assertEqual(files,{p:p.read_bytes() for p in self.runtime.rglob('*') if p.is_file()})

    def test_completed_repeat_after_later_window_and_active_deploy_is_read_only(self):
        plan=self.prepare();receipt=self.apply(plan)
        pause.pause(self.runtime,**dict(self.pause_options,window_id='later-pause-001'))
        deploy.claim(self.runtime,'later-deploy-001',quiet_reader=lambda: pause.readback(self.runtime,**self.options))
        deploy.start(self.runtime,'later-deploy-001','b'*40)
        self.systemd.states[formula.UNIT]['properties']['ExecStart']='new lawful deployed command'
        calls=list(self.systemd.calls)
        files={p:p.read_bytes() for p in self.runtime.rglob('*') if p.is_file()}
        with patch.object(formula,'_observe',side_effect=AssertionError('not current equality')), \
             patch.object(pause,'_ExclusiveRestoreLock',side_effect=AssertionError('not new restore lock')):
            self.assertEqual(self.apply(plan),receipt)
        self.assertEqual(calls,self.systemd.calls)
        self.assertEqual(files,{p:p.read_bytes() for p in self.runtime.rglob('*') if p.is_file()})
        self.assertEqual(barrier.barrier_status(self.runtime)['window_id'],'later-pause-001')

    def test_new_pause_guard_is_rechecked_inside_actual_restore_lock(self):
        plan=self.prepare();self.apply(plan)
        original=(self.runtime / pause.STATE_FILENAME).read_bytes();calls=list(self.systemd.calls)
        lock=pause._ExclusiveRestoreLock
        @contextmanager
        def racing_lock(runtime):
            with lock(runtime):
                # Simulate a competing partial record becoming visible after
                # the external preflight. The in-lock admission must re-read it.
                state=formula.load(runtime,OP);state['phase']='committed'
                state['fingerprint']=pause.fingerprint({k:v for k,v in state.items() if k!='fingerprint'})
                barrier._atomic_write_private_json(formula._path(runtime,OP),state)
                yield
        with patch.object(pause,'_ExclusiveRestoreLock',racing_lock):
            with self.assertRaisesRegex(RuntimeError,'same-operation recovery'):
                pause.pause(self.runtime,**dict(self.pause_options,window_id='raced-pause-001'))
        self.assertEqual(original,(self.runtime / pause.STATE_FILENAME).read_bytes())
        self.assertEqual(calls,self.systemd.calls)
        self.apply(plan)
        self.assertEqual(pause.load_state(self.runtime)['baseline'],self.baseline)

    def test_released_recovery_rejects_foreign_window_and_wrong_readback_fingerprint(self):
        plan=self.prepare()
        with self.assertRaises(RuntimeError):
            self.apply(plan,_fault=lambda p: (_ for _ in ()).throw(RuntimeError('crash')) if p=='barrier_released' else None)
        released=barrier._load_state(self.runtime);calls=list(self.systemd.calls)
        for override in ('window','readback'):
            proof=deepcopy(released)
            if override=='window':proof['window_id']='foreign-window-001'
            else:proof['restore']['readback_fingerprint']='sha256:'+'b'*64
            proof['state_fingerprint']=barrier._fingerprint({k:v for k,v in proof.items() if k!='state_fingerprint'})
            barrier._atomic_write_private_json(self.runtime / barrier.STATE_FILENAME,proof)
            with self.assertRaisesRegex(RuntimeError,'exact historical barrier release'):
                self.apply(plan)
        self.assertEqual(calls,self.systemd.calls)
        self.assertEqual(formula.load(self.runtime,OP)['phase'],'committed')

    def test_wakeup_exact_binding_boundary_and_release_retry_never_rearms(self):
        plan=self.prepare()
        start='2026-10-08T18:55:00+00:00';released='2026-10-08T19:10:00+00:00'
        native=barrier._load_state(self.runtime);native['started_at']=start
        native['state_fingerprint']=barrier._fingerprint({k:v for k,v in native.items() if k!='state_fingerprint'})
        barrier._atomic_write_private_json(self.runtime / barrier.STATE_FILENAME,native)
        # Named warehouse owner is enabled/active in this native pause fixture.
        from packages.application.business_data_schedule_profile import WAREHOUSE_TIMER
        wanted=plan['baseline']['units'][WAREHOUSE_TIMER]
        self.assertEqual([wanted['is_enabled'],wanted['is_active']],['enabled','active'])
        with patch('packages.application.business_data_cycle_dispatch.selected',return_value=True), \
             patch('packages.application.business_data_schedule_profile.required_phase_readiness',return_value={'ready':True}), \
             patch.object(wake,'slot_receipt',return_value=None),patch.object(barrier,'_utc_now',return_value=released):
            receipt=self.apply(plan)
            debt=wake.load(self.runtime);self.assertIsNotNone(debt)
            self.assertEqual(debt['phase'],'pending')
            # 3-hour UTC slot: 19:00. Exactly7200 remaining is allowed; one
            # microsecond less stays blocked until the next ordinary slot.
            edge=wake.instant(debt['missed_slot']) + timedelta(hours=1)
            self.assertIsNone(wake.policy(self.runtime,edge))
            self.assertEqual(wake.policy(self.runtime,edge+timedelta(microseconds=1)),'wait_next_slot')
            path=self.runtime / wake.FILENAME;before=path.read_bytes();calls=list(self.systemd.calls)
            self.assertEqual(self.apply(plan),receipt);self.assertEqual(path.read_bytes(),before);self.assertEqual(calls,self.systemd.calls)
            with self.assertRaisesRegex(RuntimeError,'exact committed binding'):
                wake.arm_formula_before_release(self.runtime,native,released,dict(receipt,window_id='foreign-pause-001'))

    def test_barrier_release_crash_already_admits_native_wakeup_without_bookkeeping(self):
        plan=self.prepare()
        native=barrier._load_state(self.runtime);native['started_at']='2026-10-08T18:55:00+00:00'
        native['state_fingerprint']=barrier._fingerprint({k:v for k,v in native.items() if k!='state_fingerprint'})
        barrier._atomic_write_private_json(self.runtime / barrier.STATE_FILENAME,native)
        released='2026-10-08T19:10:00+00:00'
        with patch('packages.application.business_data_cycle_dispatch.selected',return_value=True), \
             patch('packages.application.business_data_schedule_profile.required_phase_readiness',return_value={'ready':True}), \
             patch.object(wake,'slot_receipt',return_value=None),patch.object(barrier,'_utc_now',return_value=released):
            with self.assertRaisesRegex(RuntimeError,'crash at barrier release'):
                self.apply(plan,_fault=lambda p: (_ for _ in ()).throw(RuntimeError('crash at barrier release')) if p=='barrier_released' else None)
            old=pause.load_state(self.runtime);transition=formula.load(self.runtime,OP)
            self.assertEqual(old['phase'],'held');self.assertEqual(transition['phase'],'committed')
            self.assertEqual(old['baseline'],self.baseline)
            self.assertEqual(old['restore_readback'],transition['receipt'])
            debt=wake.load(self.runtime);edge=wake.instant(debt['missed_slot'])+timedelta(hours=1)
            self.assertIsNone(wake.policy(self.runtime,edge))
            self.assertEqual(wake.policy(self.runtime,edge+timedelta(microseconds=1)),'wait_next_slot')
            # No apply bookkeeping is called: the restarted native coordinator
            # can accept its existing one-shot debt at exact7200 immediately.
            with patch('packages.application.business_data_cycle_dispatch.launch',return_value={
                    'accepted':True,'dispatch_id':'fake-exact-dispatch','status':'queued'}) as launch, \
                 patch('packages.application.business_data_cycle_dispatch._decode',return_value={'slot':debt['missed_slot']}):
                wake.coordinate(self.runtime,edge)
                launch.assert_called_once_with(self.runtime,expected_slot=debt['missed_slot'])
                self.assertEqual(wake.load(self.runtime)['phase'],'accepted')
                wake.coordinate(self.runtime,edge)
                launch.assert_called_once()
            self.assertEqual(formula.load(self.runtime,OP)['phase'],'committed')


if __name__ == '__main__':
    unittest.main()
