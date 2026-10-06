#!/usr/bin/env python3
"""Offline finite operator producer exclusion, real receipt/start lifetime tests."""
from __future__ import annotations
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from apps.business_data_heavy_producers_smoke import busy
from apps import supplier_shipment_factual_date_correction as factual_cli
from apps import warehouse_cost_unified_recovery as unified_cli
from apps.ff_pool_dense_fbs_smoke import _enable_writer
from packages.application.business_data_heavy_admission import (
    HeavyAdmissionBusy, HeavyAdmissionLease, heavy_admitted, heavy_admission_status, require_heavy_owner,
)
from packages.application.business_data_procedure_admission import (
    MaintenanceAdmissionBlocked, admission_idle, initialize_admission,
)
from packages.application.business_data_write_barrier import acquire_barrier
from packages.application.ff_pool_dense_fbs import DenseFbsService
from packages.application.ff_pool_surfaces import FfPoolSurface
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypoint as Entry, SheetVitrinaV1OperatorJobStore as Jobs,
)
from packages.application.supplier_shipment_factual_correction import (
    CORRECTION_TABLE, SupplierShipmentFactualCorrectionBlock,
)
from packages.application.warehouse_targeted_replay import WarehouseTargetedSupplierReplay

STAMP = '2026-07-25T10:00:00Z'


def hold_pause(root):
    acquire_barrier(root, window_id='operator-smoke', window_kind='maintenance_pause',
        plan_fingerprint='sha256:'+'a'*64, approval_reference='offline-fixture', actor='offline-actor', reason='fixture')


def fixture(root):
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=root)
    runtime.save_supplier_shipment(header={'shipment_id':'fixture', 'created_at':STAMP,
        'updated_at':STAMP, 'shipment_date':'2026-07-17', 'actual_shipment_date':'2026-07-20',
        'order_status':'in_transit', 'invoice_no':'OFFLINE', 'invoice_date':'2026-07-17'}, lines=[])
    return runtime, *entry_for_runtime(runtime)


def entry_for_runtime(runtime):
    root = runtime.runtime_dir
    block = SupplierShipmentFactualCorrectionBlock(runtime=runtime, timestamp_factory=lambda:STAMP)
    supplier = SimpleNamespace(sanitize_supplier_write_payload=lambda payload:dict(payload),
        factual_dates_change_required=lambda *args:True,
        factual_date_change_required=lambda *args:True,
        desired_actual_shipment_date=lambda *args:'2026-07-21',
        factual_date_correction_has_other_changes=lambda *args,**kwargs:False,
        update_shipment=Mock(return_value={'status':'saved'}))
    jobs = Jobs(lambda:STAMP, runtime_dir=root)
    entry = SimpleNamespace(runtime=runtime, supplier_shipments_block=supplier,
        supplier_shipment_factual_correction_block=block, operator_jobs=jobs,
        handle_supplier_shipments_detail_request=Mock(return_value={'status':'zero_change'}))
    return block, entry


def confirm(entry):
    entry.supplier_shipments_block.validate_factual_dates_confirmation = lambda *args: {
        'payload': {'mutation_payload': {'actual_shipment_date': '2026-07-21'}}}
    entry.handle_supplier_shipments_patch_request = lambda *args, **kwargs: Entry.handle_supplier_shipments_patch_request(entry, *args, **kwargs)
    entry.activated_at_factory = lambda: STAMP
    return Entry._handle_supplier_factual_dates_confirm_request(entry, 'fixture',
        {'confirmation_token': 'offline-same-token'}, actor='offline')


def submit(entry, *, acceptance=False):
    payload={'actual_shipment_date':'2026-07-21'}
    if acceptance:payload['actual_ff_acceptance_date']='2026-07-23'
    return Entry.handle_supplier_shipments_patch_request(entry, 'fixture', payload,
        confirmed_factual_dates=True, actor='offline')


def durable_jobs(runtime):
    with closing(sqlite3.connect(runtime.db_path)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",(CORRECTION_TABLE,)).fetchone():return []
        conn.row_factory=sqlite3.Row
        return [dict(row) for row in conn.execute('SELECT * FROM '+CORRECTION_TABLE)]


class OperatorProducersSmoke(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        initialize_admission(self.root)
        with heavy_admitted(self.root, operation='fixture'):pass

    def idle(self):
        self.assertTrue(heavy_admission_status(self.root)['idle'])
        self.assertTrue(admission_idle(self.root)['idle'])

    def test_active_facility_busy_before_writer_or_first_acceptance_and_quick_paths(self):
        surface=FfPoolSurface(db_path=self.root/'missing.db', runtime_dir=self.root)
        with busy(self.root), patch('packages.application.warehouse_functional_lock.warehouse_functional_write_lock') as writer, \
             patch.object(surface,'_create_facility_locked',return_value={'quick':True}) as create, \
             patch.object(surface,'_update_facility_locked',return_value={'quick':True}) as update:
            for payload in ({'request_id':'a'},{'active':True},{'active':1}):
                with self.assertRaises(HeavyAdmissionBusy):surface.create_facility(payload,actor='offline')
            with self.assertRaises(HeavyAdmissionBusy):surface.update_facility('f',{'active':True},actor='offline')
            writer.assert_not_called();create.assert_not_called();update.assert_not_called()
            self.assertTrue(surface.create_facility({'active':False},actor='offline')['quick'])
            self.assertTrue(surface.update_facility('f',{'name':'metadata'},actor='offline')['quick'])
            self.assertTrue(surface.update_facility('f',{'active':False},actor='offline')['quick'])
        self.assertFalse((self.root/'missing.db').exists());self.idle()

    def test_real_inactive_facility_commits_while_other_process_holds_heavy(self):
        runtime,_,_=fixture(self.root)
        with closing(sqlite3.connect(runtime.db_path)) as conn:_enable_writer(conn);conn.commit()
        surface=FfPoolSurface(db_path=runtime.db_path,runtime_dir=self.root)
        with busy(self.root):
            result=surface.create_facility({'request_id':'inactive-fixture','name':'OFFLINE','active':False},actor='offline')
            facility=result['facility']
            self.assertFalse(facility['active'])
            renamed=surface.update_facility(facility['facility_id'],{'request_id':'rename-fixture',
                'expected_updated_at':facility['updated_at'],'name':'RENAMED'},actor='offline')
            self.assertEqual(renamed['facility']['name'],'RENAMED')
        self.idle()

    def test_owned_surface_dense_activation_reenters_and_other_thread_is_busy(self):
        surface=FfPoolSurface(db_path=self.root/'missing.db',runtime_dir=self.root)
        dense=DenseFbsService(db_path=self.root/'missing.db',runtime_dir=self.root)
        seen=[]
        def activate(*args,**kwargs):
            require_heavy_owner(self.root)
            errors=[]
            other=threading.Thread(target=lambda:self._capture_busy(errors,lambda:dense.activate_facility(
                facility_id='f',expected_updated_at='v',request_id='r',request_identity='i',actor='offline')))
            other.start();other.join(5);self.assertEqual(errors,[HeavyAdmissionBusy]);seen.append('owned')
            return {'status':'owned'}
        with patch.object(surface,'_create_facility_locked',side_effect=activate), \
             patch('packages.application.warehouse_functional_lock.warehouse_functional_write_lock'):
            self.assertEqual(surface.create_facility({},actor='offline')['status'],'owned')
        self.assertEqual(seen,['owned']);self.idle()

    @staticmethod
    def _capture_busy(errors,fn):
        try:fn()
        except BaseException as exc:errors.append(type(exc))

    def test_canonical_dense_zero_and_factual_targeted_busy_before_scan_state_domain(self):
        dense=DenseFbsService(db_path=self.root/'missing.db',runtime_dir=self.root)
        runtime=SimpleNamespace(runtime_dir=self.root,db_path=self.root/'missing.db')
        factual=SupplierShipmentFactualCorrectionBlock(runtime=runtime)
        replay=WarehouseTargetedSupplierReplay(runtime=runtime)
        with busy(self.root), patch.object(factual,'get_job') as get, \
             patch.object(factual,'_targeted_replay_available') as inspect, \
             patch.object(replay,'_apply_locked') as apply:
            for fn in (lambda:dense.activate_facility(facility_id='f',expected_updated_at='v',request_id='r',request_identity='i',actor='offline'),
                       lambda:dense.apply_zero_repair_plan({},confirm_fingerprint='',approval_reference='',actor=''),
                       lambda:factual.run_job('job'),
                       lambda:factual.apply(shipment_id='f',new_actual_shipment_date='2026-07-21',actor='offline',fingerprint='f',backup_dir=self.root),
                       lambda:replay.apply({},confirm_fingerprint='f')):
                with self.assertRaises(HeavyAdmissionBusy):fn()
            get.assert_not_called();inspect.assert_not_called();apply.assert_not_called()
        self.assertFalse(runtime.db_path.exists());self.idle()

    def test_async_busy_and_maintenance_reject_before_durable_job(self):
        runtime,block,entry=fixture(self.root)
        with busy(self.root), patch.object(block,'create_job',wraps=block.create_job) as create:
            with self.assertRaises(HeavyAdmissionBusy):submit(entry)
            create.assert_not_called()
        self.assertEqual(durable_jobs(runtime),[]);self.assertEqual(entry.operator_jobs._jobs,{})
        hold_pause(self.root)
        with patch.object(block,'create_job',wraps=block.create_job) as create:
            with self.assertRaises(MaintenanceAdmissionBlocked):submit(entry)
            create.assert_not_called()
        self.assertEqual(durable_jobs(runtime),[])

    def test_async_start_constructor_failures_cancel_cleanup_under_lease(self):
        for where in ('constructor','start'):
            for failure in (RuntimeError,KeyboardInterrupt,SystemExit):
                with self.subTest(where=where,failure=failure.__name__), tempfile.TemporaryDirectory() as temp:
                    root=Path(temp);initialize_admission(root)
                    runtime,block,entry=fixture(root)
                    original=block._set_job_state
                    def terminal(*args,**kwargs):
                        self.assertFalse(heavy_admission_status(root)['idle'])
                        return original(*args,**kwargs)
                    name='packages.application.business_data_procedure_admission.admitted_thread' if where=='constructor' else 'threading.Thread.start'
                    with patch.object(block,'_set_job_state',side_effect=terminal),patch(name,side_effect=failure('offline-no-start')):
                        with self.assertRaises(failure):submit(entry)
                    jobs=durable_jobs(runtime)
                    self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['status'],'error')
                    self.assertEqual(jobs[0]['phase'],'failed');self.assertEqual(jobs[0]['error_code'],failure.__name__)
                    self.assertTrue(jobs[0]['completed_at']);self.assertEqual(entry.operator_jobs._threads,{})
                    self.assertTrue(heavy_admission_status(root)['idle']);self.assertTrue(admission_idle(root)['idle'])

    def test_pause_between_durable_create_and_child_admission_terminalizes_no_start(self):
        runtime,block,entry=fixture(self.root)
        original=block.create_job
        def create(*args,**kwargs):
            result=original(*args,**kwargs);hold_pause(self.root);return result
        with patch.object(block,'create_job',side_effect=create):
            with self.assertRaises(MaintenanceAdmissionBlocked):submit(entry)
        jobs=durable_jobs(runtime);self.assertEqual(jobs[0]['status'],'error')
        self.assertEqual(jobs[0]['error_code'],'MaintenanceAdmissionBlocked')
        self.assertTrue(heavy_admission_status(self.root)['idle'])

    def test_async_real_job_holds_heavy_through_acceptance_tail_and_terminal(self):
        runtime,block,entry=fixture(self.root)
        entered,finish=threading.Event(),threading.Event()
        seen=[]
        def apply(**kwargs):require_heavy_owner(self.root);seen.append('apply');return {'applied':True}
        def acceptance(*args,**kwargs):
            require_heavy_owner(self.root);seen.append('acceptance');entered.set();self.assertTrue(finish.wait(5));return {'saved':True}
        with patch.object(block,'dry_run',return_value={'fingerprint':'sha256:fixture'}), \
             patch.object(block,'apply',side_effect=apply),patch.object(entry.supplier_shipments_block,'update_shipment',side_effect=acceptance):
            result=submit(entry,acceptance=True)
            self.assertTrue(entered.wait(5));thread=entry.operator_jobs._threads[result['job']['job_id']]
            try:
                self.assertEqual(durable_jobs(runtime)[0]['status'],'success')
                self.assertFalse(heavy_admission_status(self.root)['idle']);self.assertFalse(admission_idle(self.root)['idle'])
                with self.assertRaises(HeavyAdmissionBusy):
                    with heavy_admitted(self.root,operation='other'):pass
            finally:finish.set();thread.join(5)
        self.assertEqual(seen,['apply','acceptance']);self.assertEqual(entry.operator_jobs.get(result['job']['job_id'])['status'],'success');self.idle()

    def test_actual_running_job_error_terminalizes_and_releases_without_acceptance_update(self):
        runtime,block,entry=fixture(self.root)
        source_before=runtime.load_supplier_shipment('fixture')
        with patch.object(block,'dry_run',side_effect=ValueError('offline-failure-before-apply')):
            result=submit(entry,acceptance=True)
            entry.operator_jobs._threads[result['job']['job_id']].join(5)
        job=durable_jobs(runtime)[0]
        self.assertEqual(job['status'],'error');self.assertEqual(job['phase'],'failed')
        self.assertEqual(job['error_code'],'ValueError');self.assertTrue(job['completed_at'])
        self.assertEqual(runtime.load_supplier_shipment('fixture'),source_before)
        entry.supplier_shipments_block.update_shipment.assert_not_called()
        self.assertEqual(entry.operator_jobs.get(result['job']['job_id'])['status'],'error');self.idle()

    def test_constructor_cleanup_storage_failure_retains_cancellation_and_closes_lease(self):
        runtime,block,entry=fixture(self.root)
        with patch.object(block,'_set_job_state',side_effect=sqlite3.OperationalError('offline-store-unavailable')), \
             patch('packages.application.business_data_procedure_admission.admitted_thread',side_effect=KeyboardInterrupt('no-start')):
            with self.assertRaises(KeyboardInterrupt) as error:submit(entry)
        self.assertIn('OperationalError',' '.join(error.exception.__notes__))
        # No successful terminal write is claimed for the unavailable store.
        self.assertEqual(durable_jobs(runtime)[0]['status'],'queued')
        self.assertEqual(entry.operator_jobs._jobs,{});self.idle()

    def test_failed_no_start_cleanup_repeat_confirm_and_fresh_process_are_readback_only(self):
        for where in ('constructor', 'start'):
            with self.subTest(where=where), tempfile.TemporaryDirectory() as temp:
                root=Path(temp);runtime,block,entry=fixture(root)
                before=runtime.load_supplier_shipment('fixture')
                name=('packages.application.business_data_procedure_admission.admitted_thread'
                    if where=='constructor' else 'threading.Thread.start')
                with patch.object(block,'_set_job_state',side_effect=sqlite3.OperationalError('store-unavailable')), \
                     patch(name,side_effect=KeyboardInterrupt('proven-no-start')):
                    with self.assertRaises(KeyboardInterrupt):confirm(entry)
                saved=durable_jobs(runtime);self.assertEqual(len(saved),1)
                self.assertEqual(saved[0]['status'],'queued')
                with patch.object(block,'run_job') as run,patch.object(entry.operator_jobs,'start') as start, \
                     patch.object(runtime,'complete_supplier_confirmation_preview') as complete:
                    for _ in range(2):
                        result=confirm(entry)
                        self.assertEqual(result['status'],'needs_review')
                        self.assertTrue(result['requires_review'])
                        self.assertEqual(result['reason'],'worker_execution_unproven')
                        self.assertEqual(result['correction']['correction_id'],saved[0]['correction_id'])
                        self.assertEqual(result['correction']['status'],'queued')
                    safe=Entry.handle_supplier_shipments_patch_request(entry,'fixture',
                        {'actual_shipment_date':'2026-07-21'},confirmed_factual_dates=True,
                        supplier_safe=True,actor='offline')
                    self.assertEqual(safe['status'],'needs_review');self.assertTrue(safe['requires_review'])
                    self.assertEqual(safe['reason'],'worker_execution_unproven')
                    run.assert_not_called();start.assert_not_called();complete.assert_not_called()
                # A constructor failure has no operator job. A proven start
                # failure retains its terminal local job, but neither is alive.
                self.assertEqual(result['job'] is None,where=='constructor')
                if where=='start':self.assertEqual(result['job']['status'],'error')
                code="""
import json,sys
from pathlib import Path
from unittest.mock import patch
from apps.business_data_heavy_operator_producers_smoke import entry_for_runtime,confirm
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(sys.argv[1]))
block,entry=entry_for_runtime(runtime)
with patch.object(block,'run_job') as run,patch.object(entry.operator_jobs,'start') as start,patch.object(runtime,'complete_supplier_confirmation_preview') as complete:
    result=confirm(entry)
    assert not run.called and not start.called and not complete.called
    assert entry.operator_jobs._jobs=={} and entry.operator_jobs._threads=={}
print(json.dumps(result))
"""
                child=subprocess.run([sys.executable,'-c',code,str(root)],cwd=ROOT,
                    capture_output=True,text=True,timeout=20,check=True)
                restarted=json.loads(child.stdout)
                self.assertEqual(restarted['status'],'needs_review')
                self.assertEqual(restarted['correction']['correction_id'],saved[0]['correction_id'])
                self.assertIsNone(restarted['job'])
                self.assertEqual(durable_jobs(runtime),saved)
                self.assertEqual(runtime.load_supplier_shipment('fixture'),before)
                self.assertTrue(heavy_admission_status(root)['idle']);self.assertTrue(admission_idle(root)['idle'])

    def test_terminal_thread_unwind_is_not_worker_proof_or_preview_acceptance(self):
        runtime,block,entry=fixture(self.root)
        before=runtime.load_supplier_shipment('fixture')
        released,finish=threading.Event(),threading.Event()
        actual_close=HeavyAdmissionLease.close
        def gated_close(lease):
            actual_close(lease)
            if (lease.operation=='supplier_factual_date_correction'
                    and threading.current_thread() is not threading.main_thread()):
                released.set();self.assertTrue(finish.wait(5))
        with patch.object(HeavyAdmissionLease,'close',gated_close), \
             patch.object(block,'_set_job_state',side_effect=sqlite3.OperationalError('store unavailable')):
            first=submit(entry)
            thread=entry.operator_jobs._threads[first['job']['job_id']]
            try:
                self.assertTrue(released.wait(5))
                saved=durable_jobs(runtime)
                self.assertEqual(saved[0]['status'],'queued')
                self.assertEqual(entry.operator_jobs.get(first['job']['job_id'])['status'],'error')
                self.assertTrue(thread.is_alive());self.assertTrue(heavy_admission_status(self.root)['idle'])
                self.assertFalse(entry.operator_jobs.factual_correction_execution(
                    saved[0]['correction_id'])['worker_present'])
                with patch.object(runtime,'complete_supplier_confirmation_preview',
                    side_effect=lambda **kw:{'result':kw['result']}) as complete, \
                     patch.object(entry.operator_jobs,'start') as start:
                    result=confirm(entry)
                    self.assertEqual(result['status'],'needs_review')
                    self.assertEqual(result['reason'],'worker_execution_unproven')
                    self.assertEqual(result['correction']['correction_id'],saved[0]['correction_id'])
                    complete.assert_not_called();start.assert_not_called()
                self.assertEqual(durable_jobs(runtime),saved)
                self.assertEqual(runtime.load_supplier_shipment('fixture'),before)
            finally:
                finish.set();thread.join(5)
        self.idle()

    def test_running_factual_cancellation_is_existing_needs_review_without_commit_claim(self):
        for failure in (KeyboardInterrupt,SystemExit):
            with self.subTest(failure=failure.__name__),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);runtime,block,entry=fixture(root)
                captured=[]
                with patch.object(block,'dry_run',side_effect=failure('offline-running-cancel')), \
                     patch('threading.excepthook',side_effect=lambda args:captured.append(args.exc_type)):
                    result=submit(entry)
                    entry.operator_jobs._threads[result['job']['job_id']].join(5)
                jobs=durable_jobs(runtime);self.assertEqual(jobs[0]['status'],'needs_review')
                self.assertEqual(jobs[0]['phase'],'requires_review');self.assertTrue(jobs[0]['completed_at'])
                self.assertEqual(captured,[failure]);self.assertEqual(entry.operator_jobs.get(result['job']['job_id'])['status'],'error')
                self.assertTrue(heavy_admission_status(root)['idle']);self.assertTrue(admission_idle(root)['idle'])

    def test_native_spawn_uncertainty_keeps_durable_job_and_full_lifetime_before_bootstrap(self):
        runtime,block,entry=fixture(self.root)
        bootstrap,entered,finish=threading.Event(),threading.Event(),threading.Event()
        original_bootstrap=threading.Thread._bootstrap;original_start=threading.Thread.start;threads=[]
        def gated(thread):self.assertTrue(bootstrap.wait(5));original_bootstrap(thread)
        def interrupted(thread):
            threads.append(thread)
            thread._started.wait=lambda timeout=None:(_ for _ in ()).throw(KeyboardInterrupt('native-start-uncertain'))
            original_start(thread)
        def apply(**kwargs):require_heavy_owner(self.root);entered.set();self.assertTrue(finish.wait(5));return {'applied':True}
        with patch.object(block,'dry_run',return_value={'fingerprint':'sha256:fixture'}),patch.object(block,'apply',side_effect=apply):
            with patch('threading.Thread._bootstrap',gated),patch('threading.Thread.start',interrupted):
                with self.assertRaises(KeyboardInterrupt):submit(entry)
            try:
                self.assertEqual(durable_jobs(runtime)[0]['status'],'queued')
                self.assertEqual(len(entry.operator_jobs.maintenance_live_jobs()),1)
                correction_id=durable_jobs(runtime)[0]['correction_id']
                self.assertTrue(entry.operator_jobs.factual_correction_execution(correction_id)['worker_present'])
                self.assertFalse(entry.operator_jobs.factual_correction_execution('copied-id')['worker_present'])
                self.assertFalse(heavy_admission_status(self.root)['idle']);self.assertFalse(admission_idle(self.root)['idle'])
                with self.assertRaises(HeavyAdmissionBusy):submit(entry)
                self.assertEqual(len(durable_jobs(runtime)),1)
                bootstrap.set();self.assertTrue(entered.wait(5));self.assertEqual(durable_jobs(runtime)[0]['status'],'running')
            finally:bootstrap.set();finish.set();threads[0].join(5)
        self.assertEqual(durable_jobs(runtime)[0]['status'],'success')
        self.assertFalse(entry.operator_jobs.factual_correction_execution(correction_id)['worker_present'])
        self.idle()

    def test_actual_http_facility_and_confirmed_factual_busy_return409_without_acceptance(self):
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        from apps.warehouse_current_sync_job_smoke import entry_fixture
        from packages.adapters.registry_upload_http_entrypoint import (
            build_registry_upload_http_server, DEFAULT_FF_POOL_FACILITIES_PATH,DEFAULT_SUPPLIER_SHIPMENTS_PATH,
        )
        from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
        entry,_,_=entry_fixture(self.root)
        runtime,block,fake=fixture(self.root)
        entry.ff_pool_surface=FfPoolSurface(db_path=runtime.db_path,runtime_dir=self.root)
        entry._supplier_confirmation_lock=threading.RLock()
        entry.supplier_shipments_block=fake.supplier_shipments_block
        entry.supplier_shipment_factual_correction_block=block
        entry.supplier_shipments_block.validate_factual_dates_confirmation=lambda *args:{
            'payload':{'mutation_payload':{'actual_shipment_date':'2026-07-21'}}}
        config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=0,runtime_dir=self.root,
            upload_path='/v1/registry/upload',sheet_plan_path='/v1/sheet-vitrina-v1/plan',
            sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',sheet_status_path='/v1/sheet-vitrina-v1/status',
            sheet_operator_ui_path='/v1/sheet-vitrina-v1/operator')
        server=build_registry_upload_http_server(config,entrypoint=entry)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with busy(self.root),patch.object(block,'create_job',wraps=block.create_job) as create, \
                 patch.object(entry.runtime,'complete_supplier_confirmation_preview') as complete:
                for path,body in ((DEFAULT_FF_POOL_FACILITIES_PATH,{'request_id':'http-fixture','name':'OFFLINE'}),
                    (DEFAULT_SUPPLIER_SHIPMENTS_PATH+'/fixture/factual-dates/confirm',{'confirmation_token':'fixture-token'})):
                    request=Request(f'http://127.0.0.1:{server.server_port}{path}',data=json.dumps(body).encode(),
                        headers={'Content-Type':'application/json','X-WB-FF-Pool-CSRF':'1','Sec-Fetch-Site':'same-origin'},method='POST')
                    with self.subTest(path=path),self.assertRaises(HTTPError) as error:urlopen(request,timeout=5)
                    self.assertEqual(error.exception.code,409)
                    payload=json.loads(error.exception.read());self.assertEqual(payload['status'],'busy')
                    self.assertFalse(payload['accepted']);self.assertFalse(payload['source_effects_started'])
                create.assert_not_called();complete.assert_not_called()
            self.assertEqual(durable_jobs(runtime),[])
        finally:server.shutdown();server.server_close();thread.join(5)
        self.idle()

    def test_cli_outer_admission_precedes_constructor_audit_and_domain(self):
        args=SimpleNamespace(runtime_dir=str(self.root),apply=True)
        with busy(self.root), patch.object(factual_cli,'RegistryUploadDbBackedRuntime') as constructor, \
             patch.object(unified_cli,'RegistryUploadDbBackedRuntime') as unified_constructor, \
             patch.object(unified_cli,'_parser') as parser:
            parser.return_value.parse_args.return_value=args
            with self.assertRaises(HeavyAdmissionBusy):factual_cli.run(args)
            with self.assertRaises(HeavyAdmissionBusy):unified_cli.main([])
            with patch.object(unified_cli,'_ensure_audit_schema') as audit:
                with self.assertRaises(HeavyAdmissionBusy):unified_cli.apply_plan(SimpleNamespace(runtime_dir=self.root),args,{'would_change':True})
                audit.assert_not_called()
            constructor.assert_not_called();unified_constructor.assert_not_called()
        with busy(self.root),patch.object(factual_cli,'_run_admitted',return_value={'mode':'dry_run'}):
            args.apply=False;self.assertEqual(factual_cli.run(args)['mode'],'dry_run')
        self.idle()


if __name__=='__main__':unittest.main()
