#!/usr/bin/env python3
"""Synthetic files/systemd only, using actual native pause/deploy/barrier APIs."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import timedelta
import json
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

    def _run(self, args):
        if args[0] == 'show':
            value = self.states[args[1]]
            props = dict(value['properties'],UnitFileState=value['is_enabled'],ActiveState=value['is_active'])
            return SimpleNamespace(returncode=0,stdout='\n'.join(k+'='+v for k,v in props.items()),stderr='')
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
            fragment.write_bytes(data)
            value['properties'].update(FragmentPath=str(fragment), DropInPaths='')
            if unit == formula.UNIT:
                exec_line = next(line for line in data.decode().splitlines() if line.startswith('ExecStart='))[10:]
                value['properties']['ExecStart'] = '{ path=/usr/bin/python3 ; argv[]=' + exec_line + ' ; ignore_errors=no ; }'
        self.dropin = self.units / (formula.UNIT + '.d') / '10-fixture.conf'
        self.dropin.parent.mkdir(); self.dropin.write_bytes(b'[Service]\nEnvironment=UNRELATED_FIXTURE=1\n')
        self.systemd.states[formula.UNIT]['properties']['DropInPaths'] = str(self.dropin)
        self.systemd.states['wb-core-registry-http.service'] = dict(is_active='active',is_enabled='enabled',
            properties=dict(LoadState='loaded',MainPID='42',SubState='running'))
        from packages.application.business_data_schedule_profile import WAREHOUSE_TIMER
        self.systemd.states[WAREHOUSE_TIMER].update(is_enabled='enabled',is_active='active')
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
