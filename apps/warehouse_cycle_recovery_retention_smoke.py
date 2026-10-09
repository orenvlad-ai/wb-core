#!/usr/bin/env python3
"""Disposable owned-path cycles with actual T2 artifacts and native retention."""
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from threading import Thread
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.warehouse_recovery_retention_smoke import _seed_domain, _create_t2
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry, RecoveryPolicyError
from packages.application.warehouse_recovery_sync_retention import _next_checkpoint_budget
from packages.application.warehouse_update_journal import WarehouseUpdateJournal
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock, WarehouseFunctionalBusyError, WarehouseJobOwnershipError


class OwnedRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='owned-cycle-retention-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'
        self.db = self.root / 'registry_upload_runtime.sqlite3'
        _seed_domain(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(slot INTEGER PRIMARY KEY,version_id TEXT)')
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,'fixture-active')")
            conn.execute('CREATE TABLE sheet_vitrina_v1_warehouse_functional_versions(version_id TEXT PRIMARY KEY,business_effective_date TEXT,effective_at TEXT,published_at TEXT,plan_fingerprint TEXT)')
        self.now = [datetime.now(timezone.utc)]
        self.registry = WarehouseRecoveryRegistry(runtime_dir=self.root, db_path=self.db, clock=lambda:self.now[0])
        self.index = 0
        self.applies = 0
        noop = lambda *a, **k: {}
        self.rt = SimpleNamespace(runtime_dir=self.root, db_path=self.db,
            finalize_completed_wb_transit_cost_recalculations=noop)
        self.journal = WarehouseUpdateJournal(db_path=self.db, runtime_dir=self.root)
        self.entry = SimpleNamespace(runtime=self.rt, warehouse_update_journal=self.journal,
            calculation_parameters_block=SimpleNamespace(prepare_functional_economics_backup=noop,
                process_pending_targeted_recalculations=noop,publish_current_functional_economics=noop),
            wb_supplies_block=SimpleNamespace(sync_functional_sources=noop,
                collect_all_due_transit_costs=noop,reconcile_functional_ff_state=noop),
            our_wb_cost_block=SimpleNamespace(materialize_wb_supply_cost_layers=noop),
            warehouse_functional_block=SimpleNamespace(build_sync_plan=lambda:{'plan_fingerprint':'fixture','diff':{}},
                apply_plan=self.apply,record_failed_sync=noop),inventory_planning=SimpleNamespace(current=noop),
            wb_finance_weekly_block=SimpleNamespace(seller_id='fixture',recalculate_stale_cost_weeks=noop),
            now_factory=lambda:self.now[0],activated_at_factory=lambda:self.now[0].isoformat())

    def add_copy(self):
        _create_t2(self.registry, index=self.index)
        self.index += 1
        self.now[0] += timedelta(seconds=1)

    def apply(self, *args, **kwargs):
        with warehouse_functional_write_lock(self.root):
            return self._apply_locked()

    def _apply_locked(self):
        self.applies += 1
        # Before rotation must preserve the two newest rollback copies.
        for op in self.registry.plan_retention()['protected_operation_ids']:
            record=self.registry.get_operation(op)
            self.assertTrue(all(Path(a['path']).is_file() for a in record['artifacts'] if a.get('path')))
        self.add_copy()
        return {'active_version':{'version_id':'fixture-'+str(self.index)}}

    def ports(self, stack):
        for module,name,result in (
            ('operator_ff_overhead','drain',{}), ('operator_ff_overhead','reconcile',{}),
            ('operator_ff_overhead','capture_pending_completion',{}),('operator_ff_overhead','complete_current_cycle',{}),
            ('operator_warehouse_documents','drain',{'request_ids':[]}),('operator_warehouse_documents','reconcile',{}),
            ('operator_fulfillment_services','reconcile',{}),('operator_supplier_processing','reconcile',{}),
            ('fbs_accounting_runtime','refresh',{}),('fbs_accounting_runtime','current_publication_receipt',{}),
            ('fbs_accounting_runtime','load',({},'fixture-book'))):
            stack.enter_context(patch('packages.application.'+module+'.'+name,return_value=result))
        # Real registry, artifacts, retention plan/CAS/deletion and journal.
        stack.enter_context(patch('packages.application.warehouse_recovery_sync_retention.WarehouseRecoveryRegistry',return_value=self.registry))

    def cycle(self):
        with ExitStack() as stack:
            self.ports(stack)
            stack.enter_context(heavy_admitted(self.root,operation='retention-cycle-fixture'))
            owner=stack.enter_context(warehouse_functional_job_lock(self.root))
            return RegistryUploadHttpEntrypoint._handle_owned_warehouse_manual_sync_request(self.entry,
                owner_token=owner['owner_token'])

    def test_sequential_owned_cycles_rotate_native_artifacts_and_keep_receipts(self):
        foreign=self.registry.checkpoint_root/'foreign.keep'
        foreign.parent.mkdir(parents=True,exist_ok=True);foreign.write_text('unowned')
        released = False
        for _ in range(7):
            result=self.cycle()
            self.assertEqual(result['status'],'success')
            if self.index == 1:
                self.assertEqual(result['recovery_retention_before']['next_checkpoint_budget']['status'],'unknown')
            plan=self.registry.plan_retention()
            self.assertLessEqual(plan['retained_t2_count'],3)
            self.assertGreaterEqual(plan['retained_t2_count'],min(2,self.index))
            released |= result['recovery_retention_after']['applied']
            self.assertTrue(foreign.exists())
        self.assertTrue(released)
        self.assertEqual(self.applies,7)
        self.assertEqual(len(list(self.registry.checkpoint_root.glob('recovery_*.sqlite3'))),3)
        readback=WarehouseUpdateJournal(db_path=self.db,runtime_dir=self.root).public_status()
        phases={p['phase_key']:p for p in readback['phases']}
        for key in ('recovery_retention_before','recovery_retention_after'):
            self.assertEqual(phases[key]['status'],'success')
            self.assertIn('next_checkpoint_budget',phases[key]['details'])
        before=(self.applies,list(self.registry.checkpoint_root.glob('*.sqlite3')))
        self.journal.public_status() # Reading failed/successful jobs never applies.
        self.assertEqual(before,(self.applies,list(self.registry.checkpoint_root.glob('*.sqlite3'))))

    def test_protected_quarantine_or_failed_copy_blocks_without_apply_or_unlink(self):
        for lifecycle in ('quarantined','failed_recoverable'):
            self.add_copy()
            op=self.registry.list_operations(limit=1)[0]
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('UPDATE sheet_vitrina_v1_recovery_operations SET lifecycle_state=? WHERE operation_id=?',
                    (lifecycle,op['operation_id']))
            files={p:p.read_bytes() for p in self.registry.checkpoint_root.iterdir() if p.is_file()}
            with self.assertRaisesRegex(RuntimeError,'unresolved protected T2'):
                self.cycle()
            self.assertEqual(self.applies,0)
            self.assertTrue(all(p.read_bytes()==v for p,v in files.items()))
            phases={p['phase_key']:p for p in self.journal.public_status()['phases']}
            self.assertEqual(phases['recovery_retention_before']['status'],'failed')
            self.assertEqual(phases['functional_publication']['status'],'pending')

    def test_partial_retention_fails_before_business_apply(self):
        for _ in range(4): self.add_copy()
        with patch.object(self.registry,'apply_retention',return_value={'status':'partial_failure'}):
            with self.assertRaisesRegex(RuntimeError,'could not prove'):
                self.cycle()
        self.assertEqual(self.applies,0)

    def test_business_failure_skips_after_rotation(self):
        with patch.object(self.entry.warehouse_functional_block,'apply_plan',side_effect=ValueError('fixture-publication-failure')):
            with self.assertRaisesRegex(ValueError,'fixture-publication-failure'):self.cycle()
        phases={p['phase_key']:p for p in self.journal.public_status()['phases']}
        self.assertEqual(phases['recovery_retention_after']['status'],'pending')
        self.assertEqual(phases['functional_publication']['status'],'failed')

    def test_native_checkpoint_capacity_guard_still_stops_publication(self):
        with patch('packages.application.warehouse_recovery_policy.shutil.disk_usage',
                   return_value=SimpleNamespace(free=4*1024**3)):
            with self.assertRaisesRegex(RecoveryPolicyError,'capacity hard stop'):
                self.cycle()
        self.assertEqual(self.applies,1) # Attempted once, before any business mutation.
        self.assertEqual(len(list(self.registry.checkpoint_root.glob('recovery_*.sqlite3'))),0)
        phases={p['phase_key']:p for p in self.journal.public_status()['phases']}
        self.assertEqual(phases['functional_publication']['status'],'failed')
        self.assertEqual(phases['recovery_retention_after']['status'],'pending')

    def test_after_rotation_error_is_durable_without_business_retry(self):
        for _ in range(3):self.add_copy()
        # No candidate before; fourth publication makes after retention eligible.
        with patch.object(self.registry,'apply_retention',side_effect=RecoveryPolicyError('fixture-retention-failure')):
            with self.assertRaisesRegex(RecoveryPolicyError,'fixture-retention-failure'):self.cycle()
        self.assertEqual(self.applies,1)
        restarted=WarehouseUpdateJournal(db_path=self.db,runtime_dir=self.root)
        phases={p['phase_key']:p for p in restarted.public_status()['phases']}
        self.assertEqual(phases['dependent_replay_economics']['status'],'success')
        self.assertEqual(phases['recovery_retention_after']['status'],'failed')
        self.assertEqual(phases['recovery_retention_after']['last_error'],'fixture-retention-failure')
        self.assertEqual(self.applies,1)

    def test_other_protected_states_and_writer_serialization(self):
        protected={}
        for state in ('planned','reserved','writing','verified','mutation_running'):
            self.add_copy();op=self.registry.list_operations(limit=1)[0]
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('UPDATE sheet_vitrina_v1_recovery_operations SET lifecycle_state=? WHERE operation_id=?',
                    (state,op['operation_id']))
            protected.update({Path(a['path']):Path(a['path']).read_bytes() for a in op['artifacts'] if a.get('path')})
        original=self.registry.plan_retention
        def check_serialized():
            observed=[]
            def other_writer():
                try:
                    with warehouse_functional_write_lock(self.root,blocking=False):observed.append('unexpected-owner')
                except WarehouseFunctionalBusyError:observed.append('busy')
            thread=Thread(target=other_writer);thread.start();thread.join(timeout=2)
            self.assertFalse(thread.is_alive());self.assertEqual(observed,['busy'])
            return original()
        with patch.object(self.registry,'plan_retention',side_effect=check_serialized):
            self.cycle()
        for _ in range(3):self.cycle()
        self.assertTrue(all(path.read_bytes()==content for path,content in protected.items()))

    def test_budget_uses_real_same_filesystem_reservations_and_never_proves_admission(self):
        GiB=1024**3
        self.add_copy()
        operation_id=self.registry.list_operations(limit=1)[0]['operation_id']
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_recovery_capacity_reservations SET state='released' WHERE operation_id=?",(operation_id,))
        # Disposable native reservation: all its bytes+reserve reduce warning headroom.
        with patch('packages.application.warehouse_recovery_policy.shutil.disk_usage',return_value=SimpleNamespace(free=32*GiB)):
            self.registry._reserve_capacity(operation_id=operation_id,required_bytes=2*GiB,
                target_root=self.registry.checkpoint_root)
        with patch('packages.application.warehouse_recovery_policy.shutil.disk_usage',return_value=SimpleNamespace(free=14*GiB)):
            budget=_next_checkpoint_budget(self.registry,{'projection':{'recent_checkpoint_bytes':9*GiB}})
        self.assertEqual(budget['status'],'warning')
        self.assertEqual(budget['active_same_filesystem_reserved_bytes'],6*GiB)
        self.assertEqual(budget['available_after_reservations_bytes'],8*GiB)
        self.assertEqual(budget['operational_reserve_bytes'],4*GiB)
        self.assertEqual(budget['estimated_headroom_bytes'],-5*GiB)
        self.assertFalse(budget['admission_proven'])
        # Activated-volume floor comes from capacity authority; never substitute generic512MiB.
        fake=SimpleNamespace(capacity_status=lambda:{'operational_reserve_bytes':512*1024**2,
            'hard_stop_watermark_bytes':8*GiB,'available_after_reservations_bytes':14*GiB,
            'reserved_bytes':0,'expired_reservation_count':0})
        self.assertEqual(_next_checkpoint_budget(fake,{'projection':{'recent_checkpoint_bytes':7*GiB}})['status'],'warning')
        unknown=_next_checkpoint_budget(fake,{})
        self.assertEqual(unknown['status'],'unknown');self.assertIsNone(unknown['checkpoint_estimate_bytes'])
        self.assertIsNone(unknown['estimated_headroom_bytes']);self.assertFalse(unknown['admission_proven'])

    def test_old_accepted_run_gets_only_additive_phases_under_its_live_owner(self):
        with heavy_admitted(self.root,operation='old-accepted-fixture'),warehouse_functional_job_lock(self.root):
            job,_=self.journal.accept(request_key='fixture-old',request_scope='fixture',payload_fingerprint='fixture',request_payload_json='{}')
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_update_phases WHERE phase_key LIKE 'recovery_retention_%'")
            self.assertTrue(self.journal.claim(job['durable_run_id']))
            self.journal.phase_started(job['durable_run_id'],'recovery_retention_before')
            self.journal.phase_finished(job['durable_run_id'],'recovery_retention_before',details={'status':'no_change'})
            self.journal.finish(job['durable_run_id'],status='failed',error='fixture-terminal')
            with self.assertRaises(WarehouseJobOwnershipError):
                self.journal.phase_started(job['durable_run_id'],'recovery_retention_after')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            keys=[r[0] for r in conn.execute('SELECT phase_key FROM sheet_vitrina_v1_warehouse_update_phases WHERE run_id=?',(job['durable_run_id'],))]
        self.assertIn('recovery_retention_before',keys);self.assertNotIn('recovery_retention_after',keys)
        self.assertEqual(self.applies,0)

    def test_missing_admission_cannot_rotate_or_start_journal(self):
        with self.assertRaisesRegex(RuntimeError,'live heavy producer ownership'):
            RegistryUploadHttpEntrypoint._handle_owned_warehouse_manual_sync_request(self.entry,owner_token='foreign')
        with heavy_admitted(self.root,operation='foreign-owner-fixture'):
            with self.assertRaises(WarehouseJobOwnershipError):
                RegistryUploadHttpEntrypoint._handle_owned_warehouse_manual_sync_request(self.entry,owner_token='foreign')
        self.assertEqual(self.applies,0)
        self.assertEqual(self.journal.public_status()['phases'],[])


if __name__=='__main__': unittest.main()
