"""Offline fixed dispatch identity, authenticated caller and launcher no-resend proofs."""
from __future__ import annotations
import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from packages.application import business_data_cycle_dispatch as d
from packages.application import business_data_schedule_profile as p
from packages.application.business_data_heavy_admission import heavy_admitted, current_heavy_owner
from packages.application.business_data_procedure_admission import initialize_admission
from packages.application.sheet_vitrina_v1_cycle import CycleHistoryConfig, CycleReceiptStore
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint as Entry
from apps.sheet_vitrina_v1_cycle_smoke import CycleFake

NOW = datetime(2026,9,29,0,59,59,tzinfo=timezone.utc)
SHA = 'c' * 40

class DispatchSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        initialize_admission(self.root)
        with heavy_admitted(self.root,operation='fixture_bootstrap'):
            pass  # Existing canonical inode, never provisioned by GET.
        from apps.business_data_maintenance import POLICY_FILENAME, POLICY_SCHEMA_VERSION
        policy = {'schema_version': POLICY_SCHEMA_VERSION, 'master_desired': True, 'processes':
                  {key: {'desired': True} for key in ('warehouse_functional', 'wb_finance_weekly', 'vitrina_refresh')}}
        policy_path = self.root / POLICY_FILENAME
        policy_path.write_text(json.dumps(policy)); policy_path.chmod(0o600)
        self.contract = self.root / 'contract.json'; self.contract.write_text('{}')
        self.config = CycleHistoryConfig(self.root/'history',self.contract,'offline',240,31)
        for context in (patch.dict(os.environ, {'WB_CORE_WEB_AUTH_SESSION_SECRET':'offline-secret'}),
                        patch.object(d,'_identity',return_value=SHA), patch.object(p,'load_selector',return_value={'offline':True}),
                        patch.object(d,'history_config',return_value=self.config)):
            context.start();self.addCleanup(context.stop)
        if sys.platform != 'linux':
            # Production process-generation proof is /proc; Mac fixture only.
            context=patch('packages.application.sheet_vitrina_v1_cycle.process_identity',return_value='offline-process-generation')
            context.start();self.addCleanup(context.stop)
        # The reviewed sequencing companion adds two closed stages. This fake
        # has no operational database or old debt; represent that exact empty
        # component, as its own cycle smoke does, without replacing run_cycle.
        from packages.application import sheet_vitrina_v1_cycle as cycle
        if hasattr(cycle, 'ClosedBacklog'):
            from apps.sheet_vitrina_v1_cycle_smoke import EmptyClosedFixture
            context = patch.object(cycle, 'ClosedBacklog', EmptyClosedFixture)
            context.start(); self.addCleanup(context.stop)

    def test_fixed_slots_readonly_prepare_code_ready_runtime_blocked_no_provision(self):
        before = sorted(x.name for x in self.root.iterdir())
        prepared = d.prepare(self.root,NOW)
        value=d._decode(prepared['dispatch_id'])
        self.assertEqual(value['slot'],'2026-09-28T22:00:00+00:00')
        self.assertEqual(value['history'],self.config.fingerprint())
        self.assertEqual(before,sorted(x.name for x in self.root.iterdir()))
        slots={d.latest_slot(NOW+timedelta(hours=n)) for n in range(24)}
        self.assertEqual(len(slots),9)  # crossing midnight adds next day's first slot
        with patch.object(p,'activation_readiness',return_value={'ready':False,'blockers':['proof_missing']}):
            self.assertEqual(d.prepare(self.root,NOW)['status'],'blocked')
        self.assertTrue(p.activation_dependencies()['ready'])
        self.assertEqual(len(p.RETIRED_TIMERS),6)

    def test_default_code_readiness_owner_and_infrastructure_refuse_before_acceptance(self):
        from apps.business_data_maintenance import POLICY_FILENAME
        from packages.application.business_data_heavy_admission import LOCK_FILENAME
        fake = CycleFake(self.root); fake.now_factory = lambda: NOW
        identity = d.prepare(self.root, NOW)['dispatch_id']
        path = self.root / POLICY_FILENAME
        original = path.read_bytes()
        value = json.loads(original); value['processes']['warehouse_functional']['desired'] = False
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError, 'mandatory cycle phase'):
            d.prepare(self.root, NOW)
        with self.assertRaisesRegex(RuntimeError, 'mandatory cycle phase'):
            d.dispatch(fake, {'dispatch_id': identity})
        self.assertEqual(fake.events, [])
        self.assertFalse((self.root / 'sheet-vitrina-cycles').exists())
        path.write_bytes(original)
        (self.root / LOCK_FILENAME).unlink()
        before = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(d.prepare(self.root, NOW)['status'], 'blocked')
        self.assertFalse(d.dispatch(fake, {'dispatch_id': identity})['accepted'])
        self.assertEqual(before, sorted(p.name for p in self.root.iterdir()))
        self.assertFalse((self.root / LOCK_FILENAME).exists())
        self.assertEqual(fake.events, [])

    def test_actual_worker_full_scope_exact_readback_across_rollover(self):
        fake=CycleFake(self.root);fake.now_factory=lambda:NOW
        prepared=d.prepare(self.root,NOW);identity=prepared['dispatch_id']
        result=d.dispatch(fake,{'dispatch_id':identity})
        self.assertTrue(result['accepted'])
        for thread in list(fake.operator_jobs._threads.values()):thread.join(5);self.assertFalse(thread.is_alive())
        fake.now_factory=lambda:NOW+timedelta(hours=4)
        old=d.readback(self.root,identity)
        self.assertEqual(old['dispatch_id'],identity)
        self.assertIn(old['status'],d.TERMINAL)
        before=list(fake.events)
        self.assertTrue(d.dispatch(fake,{'dispatch_id':identity})['accepted'])
        self.assertEqual(fake.events,before)
        self.assertIn('warehouse',before);self.assertIn('fbs_generation',before);self.assertIn('rolling14',before)
        from packages.application.sheet_vitrina_v1_cycle import STAGES
        if 'closed_sources' in STAGES:
            self.assertLess(before.index('closed_sources'), before.index('api_sources'))
            self.assertLess(before.index('closed_ready'), before.index('final_ready'))
        with self.assertRaises(ValueError):d.dispatch(fake,{'dispatch_id':identity,'as_of_date':'2000-01-01'})
        with self.assertRaises(ValueError):d.readback(self.root,identity[:-1]+'x')

    def test_deploy_in_owned_slot_does_not_prepare_different_request_or_steal_ack(self):
        fake=CycleFake(self.root);fake.now_factory=lambda:NOW
        identity=d.prepare(self.root,NOW)['dispatch_id'];d.dispatch(fake,{'dispatch_id':identity})
        for worker in list(fake.operator_jobs._threads.values()):worker.join(5)
        with patch.object(d,'_identity',return_value='d'*40):
            result=d.prepare(self.root,NOW)
            self.assertFalse(result['accepted']);self.assertNotIn('dispatch_id',result)
            self.assertEqual(result['blockers'],['slot_owned_by_another_deployed_request'])
            self.assertTrue(d.readback(self.root,identity)['accepted'])
            self.assertEqual(d.prepare(self.root,NOW+timedelta(hours=4))['status'],'prepared')

    def test_stale_unaccepted_slot_never_accepts_and_due_backup_remains_pre_acceptance(self):
        fake=CycleFake(self.root);fake.now_factory=lambda:NOW+timedelta(hours=4)
        identity=d.prepare(self.root,NOW)['dispatch_id']
        self.assertFalse(d.dispatch(fake,{'dispatch_id':identity})['accepted'])
        self.assertEqual(fake.events,[])
        fake.now_factory=lambda:NOW
        with patch('packages.application.finance_backup_handoff.cycle_backup_priority',return_value={'priority':True}), \
             patch('packages.application.finance_backup_handoff.handoff_cycle_backup',return_value={'accepted':False,'status':'backup_priority'}) as handoff:
            result=d.dispatch(fake,{'dispatch_id':identity})
        self.assertEqual(result['status'],'not_accepted');self.assertEqual(fake.events,[])
        handoff.assert_called_once()
        self.assertIsNone(CycleReceiptStore(self.root,lambda:'').read(d._decode(identity)['cycle_id']))

    def test_readiness_race_under_actual_heavy_blocks_before_receipt_and_releases(self):
        fake=CycleFake(self.root);fake.now_factory=lambda:NOW
        identity=d.prepare(self.root,NOW)['dispatch_id']
        with patch.object(p,'activation_readiness',side_effect=[{'ready':True,'blockers':[]},{'ready':False,'blockers':['owner_changed']}]):
            result=d.dispatch(fake,{'dispatch_id':identity})
        self.assertFalse(result['accepted']);self.assertEqual(fake.events,[])
        self.assertFalse((self.root/'sheet-vitrina-cycles').exists())
        with heavy_admitted(self.root,operation='finance'):
            self.assertEqual(current_heavy_owner(self.root).operation,'finance')

    def test_unknown_post_restart_and_rollover_read_same_identity_without_resend(self):
        identity=d.prepare(self.root,NOW)['dispatch_id'];calls=[]
        def request(method, dispatch_id=None):
            self.assertIsNone(current_heavy_owner(self.root))
            calls.append((method,dispatch_id))
            if method=='POST':raise TimeoutError('unknown')
            if dispatch_id:return {'status':'unknown','accepted':None,'dispatch_id':dispatch_id}
            return {'status':'prepared','dispatch_id':identity}
        with patch.object(d,'_request',side_effect=request):
            self.assertEqual(d.launch(self.root)['status'],'unknown')
            self.assertEqual(d.launch(self.root)['status'],'unknown')
        self.assertEqual([x[0] for x in calls],['GET','POST','GET'])
        # Real fresh interpreter reads durable old identity; its POST branch is forbidden.
        script="""
import os,json,sys
from pathlib import Path
from unittest.mock import patch
from packages.application import business_data_cycle_dispatch as d
r=Path(sys.argv[1]);d._identity=lambda r:'c'*40;d.selected=lambda r:True
def req(method,identity=None):
 assert method=='GET' and identity
 return {'status':'unknown','accepted':None,'dispatch_id':identity}
d._request=req
print(json.dumps(d.launch(r)))
"""
        result=subprocess.run([sys.executable,'-c',script,str(self.root)],cwd=ROOT,env=dict(os.environ),capture_output=True,text=True,check=True,timeout=10)
        self.assertEqual(json.loads(result.stdout)['dispatch_id'],identity)
        self.assertEqual(d._read_record(self.root)['phase'],'uncertain')

    def test_accepted_active_prevents_new_slot_terminal_allows_only_new_slot(self):
        first=d.prepare(self.root,NOW)['dispatch_id'];second=d.prepare(self.root,NOW+timedelta(hours=4))['dispatch_id']
        calls=[];status=['running']
        def req(method,identity=None):
            calls.append((method,identity))
            if not identity:return {'status':'prepared','dispatch_id':second}
            return {'status':status[0],'accepted':True,'dispatch_id':identity}
        d._write_record(self.root,{'schema':1,'dispatch_id':first,'phase':'accepted','last_status':'running'})
        with patch.object(d,'_request',side_effect=req):
            d.launch(self.root);self.assertEqual(calls,[('GET',first)])
            status[0]='degraded';d.launch(self.root)
        self.assertEqual(calls[-2:],[('GET',None),('POST',second)])
        self.assertEqual(d._read_record(self.root)['dispatch_id'],second)

    def test_late_old_handler_expired_absence_fence_recovers_new_slot_without_resend(self):
        fake=CycleFake(self.root);clock=[NOW];fake.now_factory=lambda:clock[0]
        identity=d.prepare(self.root,NOW)['dispatch_id'];entered=threading.Event();release=threading.Event();result=[]
        original=fake._start_sheet_cycle_job
        def slow(**kwargs):
            entered.set();self.assertTrue(release.wait(5));return original(**kwargs)
        fake._start_sheet_cycle_job=slow
        old=threading.Thread(target=lambda:result.append(d.dispatch(fake,{'dispatch_id':identity})))
        old.start();self.assertTrue(entered.wait(5));clock[0]=NOW+timedelta(hours=4)
        proof=d.readback_fenced(fake,identity)
        self.assertEqual(proof['status'],'expired_not_accepted');self.assertFalse(proof['accepted'])
        release.set();old.join(5);self.assertFalse(old.is_alive());self.assertFalse(result[0]['accepted'])
        self.assertEqual(fake.events,[])
        fake._start_sheet_cycle_job=original
        d._write_record(self.root,{'schema':1,'dispatch_id':identity,'phase':'uncertain','last_status':'unknown'})
        calls=[]
        def request(method,key=None):
            calls.append((method,key))
            if method=='POST':return d.dispatch(fake,{'dispatch_id':key})
            return d.readback_fenced(fake,key) if key else d.prepare(self.root,clock[0])
        with patch.object(d,'_request',side_effect=request):
            accepted=d.launch(self.root);self.assertTrue(accepted['accepted'])
        self.assertEqual([item[0] for item in calls],['GET','GET','POST'])
        self.assertNotEqual(calls[-1][1],identity)
        for worker in list(fake.operator_jobs._threads.values()):worker.join(5)
        self.assertIn('warehouse',fake.events)

    def test_actual_three_hour_invocation_resolves_old_and_submits_only_new_slot(self):
        fake=CycleFake(self.root);clock=[NOW];fake.now_factory=lambda:clock[0]
        calls=[]
        def request(method,key=None):
            calls.append((method,key))
            if method=='POST':return d.dispatch(fake,{'dispatch_id':key})
            return d.readback_fenced(fake,key) if key else d.prepare(self.root,clock[0])
        def finish():
            for worker in list(fake.operator_jobs._threads.values()):
                worker.join(5);self.assertFalse(worker.is_alive())
        with patch.object(d,'_request',side_effect=request):
            first=d.launch(self.root);self.assertTrue(first['accepted']);finish()
            first_id=first['dispatch_id'];first_calls=len(calls)
            self.assertEqual([m for m,k in calls],['GET','POST'])
            before=len(calls);clock[0]=NOW+timedelta(hours=3)
            second=d.launch(self.root);self.assertTrue(second['accepted']);finish()
            self.assertNotEqual(second['dispatch_id'],first_id)
            self.assertEqual([m for m,k in calls[before:]],['GET','GET','POST'])
            # Terminal in the same slot is read-only and never resends.
            before=len(calls)
            same=d.launch(self.root);self.assertEqual(same['dispatch_id'],second['dispatch_id'])
            self.assertEqual([m for m,k in calls[before:]],['GET','GET','GET'])
            self.assertEqual([(m,k) for m,k in calls if m=='POST'],
                [('POST',first_id),('POST',second['dispatch_id'])])
        self.assertEqual(fake.events.count('warehouse'),2)

    def test_three_hour_old_terminal_readback_and_new_accept_in_same_invocation(self):
        first=d.prepare(self.root,NOW)['dispatch_id'];second=d.prepare(self.root,NOW+timedelta(hours=3))['dispatch_id']
        d._write_record(self.root,{'schema':1,'dispatch_id':first,'phase':'accepted','last_status':'running'})
        calls=[]
        def request(method,key=None):
            calls.append((method,key))
            if key==first:return {'status':'complete','accepted':True,'dispatch_id':first}
            if method=='GET':return {'status':'prepared','dispatch_id':second}
            return {'status':'accepted','accepted':True,'dispatch_id':second}
        with patch.object(d,'_request',side_effect=request):
            result=d.launch(self.root)
        self.assertEqual(result['dispatch_id'],second)
        self.assertEqual(calls,[('GET',first),('GET',None),('POST',second)])
        self.assertEqual(d._read_record(self.root)['phase'],'accepted')

    def test_missing_unsafe_or_replaced_heavy_inode_never_proves_expired_noaccept(self):
        from packages.application.business_data_heavy_admission import LOCK_FILENAME
        fake=CycleFake(self.root);fake.now_factory=lambda:NOW+timedelta(hours=4)
        identity=d.prepare(self.root,NOW)['dispatch_id'];path=self.root/LOCK_FILENAME
        path.unlink()
        self.assertEqual(d.readback_fenced(fake,identity)['status'],'unknown')
        self.assertFalse(path.exists())
        path.write_bytes(b'');path.chmod(0o644)
        self.assertEqual(d.readback_fenced(fake,identity)['status'],'unknown')
        path.chmod(0o600)
        def replace_inode(*args):
            path.unlink();path.write_bytes(b'');path.chmod(0o600)
            return False
        with patch.object(d,'_still_current',side_effect=replace_inode):
            self.assertEqual(d.readback_fenced(fake,identity)['status'],'unknown')

    def test_acceptance_ex_gap_never_reports_false_expiry_to_other_instance(self):
        fake=CycleFake(self.root);clock=[NOW];fake.now_factory=lambda:clock[0]
        sibling=SimpleNamespace(runtime=fake.runtime,operator_jobs=SimpleNamespace(_lock=threading.RLock()),now_factory=lambda:clock[0])
        identity=d.prepare(self.root,NOW)['dispatch_id'];entered=threading.Event();release=threading.Event();results=[]
        original=CycleReceiptStore.accept
        def paused_accept(store,**kwargs):
            entered.set();self.assertTrue(release.wait(5));return original(store,**kwargs)
        with patch.object(CycleReceiptStore,'accept',paused_accept):
            old=threading.Thread(target=lambda:results.append(d.dispatch(fake,{'dispatch_id':identity})))
            old.start();self.assertTrue(entered.wait(5));clock[0]=NOW+timedelta(hours=4)
            # Actual EX stays held between latest guard and durable receipt;
            # another server instance cannot prove absent/no-accept here.
            self.assertEqual(d.readback_fenced(sibling,identity)['status'],'unknown')
            release.set();old.join(5);self.assertFalse(old.is_alive())
        self.assertTrue(results[0]['accepted'])
        self.assertTrue(d.readback_fenced(sibling,identity)['accepted'])
        for worker in list(fake.operator_jobs._threads.values()):worker.join(5)

    def test_cross_process_launcher_lock_no_submit(self):
        script="""
import sys
from pathlib import Path
from packages.application.business_data_cycle_dispatch import _launcher_lock
with _launcher_lock(Path(sys.argv[1])):
 print('ready',flush=True)
 sys.stdin.readline()
"""
        child=subprocess.Popen([sys.executable,'-c',script,str(self.root)],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'ready')
            with patch.object(d,'_request',side_effect=AssertionError('must not request')):
                self.assertEqual(d.launch(self.root)['status'],'busy')
        finally:
            child.communicate('release\n',timeout=5)
        self.assertEqual(child.returncode,0)

    def test_selected_legacy_and_manual_refused_before_any_source_or_queue(self):
        fake=SimpleNamespace(runtime=SimpleNamespace(runtime_dir=self.root))
        names=['handle_sheet_refresh_request','start_sheet_refresh_job','start_sheet_auto_refresh_job',
            'start_sheet_scheduled_auto_update_job','handle_sheet_auto_refresh_request',
            'handle_sheet_scheduled_auto_update_request','start_sheet_source_group_refresh_job',
            'handle_warehouse_manual_sync_start_request','handle_sheet_web_vitrina_auto_schedules_run_now_request',
            'run_sheet_temporal_closure_retry_cycle','handle_warehouse_manual_sync_request']
        import inspect
        for name in names:
            function=getattr(Entry,name)
            kwargs={}
            for key,param in inspect.signature(function).parameters.items():
                if key!='self' and param.default is inspect.Parameter.empty:
                    kwargs[key]={} if key=='payload' else 'offline'
            with self.subTest(name=name):
                self.assertEqual(function(fake,**kwargs)['status'],'cycle_managed')
        self.assertEqual(Entry.__dict__['_run_sheet_refresh'](fake,as_of_date='2000-01-01',log=None)['status'],'cycle_managed')

    def test_real_cycle_owner_only_source_exemption_no_name_or_fork_authority(self):
        with self.assertRaises(RuntimeError):d.require_source_dispatch(self.root)
        with heavy_admitted(self.root,operation='finance'):
            with self.assertRaises(RuntimeError):d.require_source_dispatch(self.root)
        with heavy_admitted(self.root,operation='cycle'):
            d.require_source_dispatch(self.root)
        script="""
import os,sys
from pathlib import Path
from packages.application import business_data_cycle_dispatch as d
from packages.application.business_data_heavy_admission import heavy_admitted
r=Path(sys.argv[1]);d.selected=lambda r:True
with heavy_admitted(r,operation='cycle'):
 d.require_source_dispatch(r)
 pid=os.fork()
 if pid==0:
  try:d.require_source_dispatch(r)
  except RuntimeError:os._exit(0)
  os._exit(9)
 assert os.waitpid(pid,0)[1]==0
"""
        subprocess.run([sys.executable,'-c',script,str(self.root)],cwd=ROOT,check=True,timeout=10)

    def test_raw_settings_projection_and_owner_disabled_no_clocks_become_intent(self):
        from apps.business_data_maintenance import POLICY_FILENAME,POLICY_SCHEMA_VERSION
        policy={'schema_version':POLICY_SCHEMA_VERSION,'master_desired':True,'processes':
            {key:{'desired':True} for key in ('warehouse_functional','wb_finance_weekly','vitrina_refresh')}}
        path=self.root/POLICY_FILENAME;path.write_text(json.dumps(policy));path.chmod(0o600)
        from packages.application.sheet_vitrina_v1_auto_refresh import DEFAULT_STATE_FILENAME
        raw={'schedules':[{'enabled':True,'local_time_hhmm':'19:00'}],'schedule_policy':{'mode':'manual'}}
        schedule=self.root/DEFAULT_STATE_FILENAME;schedule.write_text(json.dumps(raw));schedule.chmod(0o644)
        before=schedule.read_bytes()
        projected=p.project_settings(self.root,raw)
        self.assertEqual(len(projected['effective_schedules']),8)
        self.assertEqual(projected['raw_feature_intent'],raw)
        self.assertEqual(schedule.read_bytes(),before)
        raw['schedules'][0]['enabled']=False;schedule.write_text(json.dumps(raw))
        self.assertFalse(p.required_phase_readiness(self.root)['ready'])
        self.assertTrue(all(not row['enabled'] for row in p.project_settings(self.root,raw)['effective_schedules']))
        with patch.object(p,'load_selector',return_value=None):self.assertIs(p.project_settings(self.root,raw),raw)

    def test_cli_and_public_source_refused_before_constructor_or_recovery_claim(self):
        from apps import wb_finance_daily, wb_finance_weekly, wb_fbs_warehouse_registry, warehouse_functional_runner
        from packages.application.wb_fbs_warehouse_registry import WbFbsWarehouseRegistry
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        from packages.application.wb_finance_daily import WbFinanceDailyBlock
        from packages.application.registry_upload_http_entrypoint import SheetVitrinaV1OperatorJobStore
        for app in (wb_finance_daily,wb_finance_weekly):
            with patch.object(app,'_load_env'),patch.object(app,'_run_admitted',side_effect=AssertionError('constructor')), redirect_stdout(io.StringIO()):
                self.assertEqual(app.main(['tick','--runtime-dir',str(self.root)]),0)
        # Import the actual closure module, then call its real no-date CLI body.
        # Neither its runtime constructor nor a recovery plan may be reached.
        from apps import sheet_vitrina_v1_temporal_closure_retry_live as closure
        args = SimpleNamespace(dates=[], apply=False, manifest_fingerprint='', deployed_sha='',
            approval_reference='', approval_digest='', runtime_sha_marker=str(self.root/'sha'), backup_dir='')
        output = io.StringIO()
        with patch.object(closure, 'parse_args', return_value=args), \
             patch.object(closure, 'load_registry_upload_http_entrypoint_config', return_value=SimpleNamespace(runtime_dir=self.root)), \
             patch.object(closure, 'RegistryUploadHttpEntrypoint', side_effect=AssertionError('closure constructor')), \
             patch.object(closure, 'build_explicit_recovery_plan', side_effect=AssertionError('recovery plan')), \
             redirect_stdout(output):
            closure.main.__wrapped__()
        self.assertEqual(json.loads(output.getvalue())['status'], 'cycle_managed')
        # Explicit reviewed recovery never consults the ordinary tick guard.
        args.dates = ['2026-09-01']; output = io.StringIO()
        with patch.object(closure, 'parse_args', return_value=args), \
             patch.object(closure, 'load_registry_upload_http_entrypoint_config', return_value=SimpleNamespace(runtime_dir=self.root)), \
             patch.object(closure, 'build_explicit_recovery_plan', return_value={'status':'recovery_preview', '_internal':{}}), \
             patch.object(d, 'legacy_refusal', side_effect=AssertionError('explicit recovery changed')), redirect_stdout(output):
            closure.main.__wrapped__()
        self.assertEqual(json.loads(output.getvalue()), {'status':'recovery_preview'})
        # Real import, argument parser and decorated CLI in a fresh interpreter.
        script = """
import os,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from apps import sheet_vitrina_v1_temporal_closure_retry_live as closure
from packages.application import business_data_schedule_profile as profile
root=Path(sys.argv[1]);sys.argv=['closure'];os.environ['REGISTRY_UPLOAD_RUNTIME_DIR']=str(root)
with patch.object(profile,'load_selector',return_value={'offline':True}), \
     patch.object(closure,'load_registry_upload_http_entrypoint_config',return_value=SimpleNamespace(runtime_dir=root)), \
     patch.object(closure,'RegistryUploadHttpEntrypoint',side_effect=AssertionError('constructor')):
 closure.main()
"""
        before = set(self.root.iterdir())
        child = subprocess.run([sys.executable, '-c', script, str(self.root)], cwd=ROOT,
            capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(child.stdout)['status'], 'cycle_managed')
        self.assertEqual(set(self.root.iterdir()), before)
        with patch.object(wb_fbs_warehouse_registry,'_run_admitted',side_effect=AssertionError('constructor')):
            result=wb_fbs_warehouse_registry.run(SimpleNamespace(runtime_dir=str(self.root),env_file='',command='collect'))
            self.assertEqual(result['status'],'cycle_managed')
        with patch.object(warehouse_functional_runner,'_run_admitted',side_effect=AssertionError('constructor')):
            result=warehouse_functional_runner._run(SimpleNamespace(runtime_dir=str(self.root),command='manual-sync'),sqlite_busy_timeout_ms=100)
            self.assertEqual(result['status'],'cycle_managed')
            def dispatched(runtime):
                self.assertIsNone(current_heavy_owner(runtime))
                return {'status':'blocked','accepted':False}
            with patch.object(d,'launch',side_effect=dispatched) as launcher:
                result=warehouse_functional_runner._run(SimpleNamespace(runtime_dir=str(self.root),command='hourly-sync'),sqlite_busy_timeout_ms=100)
                self.assertFalse(result['accepted']);launcher.assert_called_once_with(self.root)
        block=SimpleNamespace(runtime_dir=self.root)
        with self.assertRaisesRegex(RuntimeError,'managed'):WbFbsWarehouseRegistry.collect(block)
        with self.assertRaisesRegex(RuntimeError,'managed'):WbFinanceWeeklyBlock.sync_week(block,None,None,None)
        with self.assertRaisesRegex(RuntimeError,'managed'):WbFinanceDailyBlock.tick(block,None)
        store=SheetVitrinaV1OperatorJobStore(lambda:'offline',runtime_dir=self.root)
        trap=SimpleNamespace(needs_pickup=lambda: (_ for _ in ()).throw(AssertionError('journal claim')))
        self.assertIsNone(store.resume_warehouse_pending(runtime_dir=self.root,journal=trap,runner=lambda:None))
        self.assertEqual(store.start_warehouse_if_idle(runtime_dir=self.root,journal=trap,runner=lambda:None),(None,True))

    def test_transport_private_bound_symlink_and_raw_selector_refusal(self):
        identity=d.prepare(self.root,NOW)['dispatch_id']
        record=self.root/d.RECORD
        record.write_bytes(b' '* (d.MAXIMUM+1));record.chmod(0o600)
        with self.assertRaises(RuntimeError):d._read_record(self.root)
        record.unlink();outside=self.root/'outside';outside.write_text('{}');record.symlink_to(outside)
        with self.assertRaises(OSError):d._read_record(self.root)
        fake=SimpleNamespace(runtime=SimpleNamespace(runtime_dir=self.root))
        with patch.object(p,'load_selector',side_effect=RuntimeError('malformed selector')):
            with self.assertRaisesRegex(RuntimeError,'malformed'):
                Entry.handle_sheet_web_vitrina_auto_schedules_save_request(fake,{'schedules':[]})
        self.assertEqual(outside.read_text(),'{}')

    def test_ready_exact_dated_receipt_ignores_newer_snapshot_and_no_duplicate_tails(self):
        from apps import sheet_vitrina_v1_local_derive_smoke as local
        from contextlib import closing
        import sqlite3
        case=local.LocalDeriveTests();case.setUp()
        try:
            entry=Entry(runtime_dir=case.runtime.runtime_dir,runtime=case.runtime,
                now_factory=lambda:local.NOW,activated_at_factory=lambda:'2026-05-20T08:02:00Z',
                refreshed_at_factory=lambda:'2026-05-20T08:02:00Z')
            entry.sheet_plan_block=case.block
            handle=case.sources.collect_sources(**case.kwargs);plan=case.sources.derive_collected(handle)
            with patch.object(case.block,'build_plan',side_effect=AssertionError('recollection')):
                with heavy_admitted(case.runtime.runtime_dir,operation='cycle'):
                    versions,proof=entry._cycle_publish_dated_ready(plan)
            self.assertEqual(proof.versions,versions)
            with closing(sqlite3.connect(case.runtime.db_path)) as conn,conn:
                conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots(bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json) SELECT bundle_version,activated_at,'2099-01-01','newer-snapshot',plan_version,refreshed_at,json_set(plan_json,'$.snapshot_id','newer-snapshot') FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?",(plan.as_of_date,))
            self.assertEqual(case.runtime.load_sheet_vitrina_ready_snapshot().snapshot_id,'newer-snapshot')
            entry._cycle_verify_ready(versions)
        finally:case.doCleanups()

    def test_authenticated_actual_http_route_no_disabled_auth_or_caller_fields(self):
        from packages.adapters import registry_upload_http_entrypoint as web
        from apps.sheet_vitrina_v1_auto_refresh_tick import _build_web_auth_cookie
        fake=CycleFake(self.root);fake.now_factory=lambda:NOW
        fake.handle_business_data_cycle_dispatch_request=Entry.handle_business_data_cycle_dispatch_request.__get__(fake)
        fake.handle_business_data_cycle_readback_request=Entry.handle_business_data_cycle_readback_request.__get__(fake)
        env={'WB_CORE_WEB_AUTH_REQUIRED':'1','WB_CORE_WEB_AUTH_USERNAME':'owner',
            'WB_CORE_WEB_AUTH_PASSWORD_HASH':'configured','WB_CORE_WEB_AUTH_SESSION_SECRET':'offline-secret',
            'REGISTRY_UPLOAD_HTTP_PORT':'0','REGISTRY_UPLOAD_RUNTIME_DIR':str(self.root)}
        with patch.dict(os.environ,env):
            server=web.build_registry_upload_http_server(web.load_registry_upload_http_entrypoint_config(),fake)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def stop_server():
                server.shutdown();thread.join(5);server.server_close()
            self.addCleanup(stop_server)
            base=f'http://127.0.0.1:{server.server_address[1]}'+d.PATH
            def request(method='GET',body=None,cookie=True,url=base):
                data=json.dumps(body).encode() if body is not None else None
                headers={'Cookie':_build_web_auth_cookie(os.environ)} if cookie else {}
                try:
                    with urlopen(Request(url,data=data,method=method,headers=headers),timeout=5) as response:return response.status,json.loads(response.read())
                except HTTPError as exc:return exc.code,json.loads(exc.read())
            handler=SimpleNamespace(client_address=('192.0.2.1',1234))
            with patch.object(web,'_write_json_response') as response,patch.object(web,'_ensure_admin_role',side_effect=AssertionError('remote caller')):
                self.assertFalse(web._ensure_cycle_dispatch_access(handler,SimpleNamespace(path=d.PATH)))
                self.assertEqual(response.call_args.args[1],403)
            self.assertEqual(request(cookie=False)[0],401)
            code, prepared=request();self.assertEqual(code,200);self.assertEqual(prepared['status'],'prepared')
            self.assertEqual(request(url=base+'?runtime=/tmp&slot=2000')[0],409)
            self.assertEqual(request('POST',{},url=base.replace(d.PATH,web.DEFAULT_SHEET_REFRESH_PATH))[0],409)
            self.assertEqual(request('POST',{'dispatch_id':prepared['dispatch_id'],'root':'/tmp'})[0],409)
            self.assertEqual(request('POST',[])[0],409)
            code,accepted=request('POST',{'dispatch_id':prepared['dispatch_id']});self.assertEqual(code,200);self.assertTrue(accepted['accepted'])
            for worker in list(fake.operator_jobs._threads.values()):worker.join(5)
            from urllib.parse import urlencode
            code,read=request(url=base+'?'+urlencode({'dispatch_id':prepared['dispatch_id']}));self.assertEqual(code,200);self.assertTrue(read['accepted'])
            with patch.dict(os.environ,{'WB_CORE_WEB_AUTH_PASSWORD_HASH':''}):self.assertNotEqual(request()[0],200)

class MaintenanceWakeupSmoke(unittest.TestCase):
    SLOT = datetime(2026, 9, 28, 22, tzinfo=timezone.utc)

    def setUp(self):
        DispatchSmoke.setUp(self)
        from packages.application import business_data_cycle_wakeup as wake
        from packages.application import business_data_maintenance_pause as pause
        from packages.application import business_data_write_barrier as barrier
        from apps.business_data_maintenance_pause_smoke import FakeSystemd
        self.wake, self.pause, self.barrier = wake, pause, barrier
        self.systemd = FakeSystemd()
        self.systemd.states[p.WAREHOUSE_TIMER].update(is_enabled='enabled', is_active='active')
        self.proc = self.root / 'proc'; self.proc.mkdir()
        self.options = dict(systemd=self.systemd, activity_reader=lambda: {
            'contract_name': 'business_data_maintenance_activity_v1', 'admission_ready': True,
            'complete': True, 'runtime_dir': str(self.root), 'jobs': [],
            'feature_intent': {'enabled': True, 'schedules': ['01:17']}}, proc_root=self.proc,
            window_id='wakeup-test-001', actor='offline', reason='maintenance debt proof')
        for context in (patch.object(pause, '_cron_entries', return_value=[]),
                        patch('packages.application.finance_backup_handoff.cycle_backup_priority', return_value={'priority': False})):
            context.start(); self.addCleanup(context.stop)

    def hold(self, start=None):
        start = start or self.SLOT - timedelta(minutes=20)
        with patch.object(self.barrier, '_utc_now', return_value=start.isoformat()), patch.object(self.pause, 'now_iso', return_value=start.isoformat()):
            self.pause.pause(self.root, **self.options)
        self.baseline = self.pause.load_state(self.root)['baseline']

    def resume(self, when=None):
        when = when or self.SLOT + timedelta(minutes=30)
        with patch.object(self.barrier, '_utc_now', return_value=when.isoformat()), patch.object(self.pause, 'now_iso', return_value=when.isoformat()):
            return self.pause.resume(self.root, **self.options)

    def transport(self, fake, method, identity=None):
        if method == 'POST': return d.dispatch(fake, {'dispatch_id': identity})
        if identity: return d.readback_fenced(fake, identity)
        return d.prepare(self.root, fake.now_factory())

    def test_real_resume_debt_and_full_duration_acceptance_boundary(self):
        self.hold(); self.resume()
        value = self.wake.load(self.root)
        self.assertEqual(value['missed_slot'], self.SLOT.isoformat())
        self.assertEqual(self.pause.load_state(self.root)['baseline'], self.baseline)
        limit = self.SLOT + timedelta(hours=1)
        self.assertIsNone(self.wake.policy(self.root, limit))
        self.assertEqual(d.prepare(self.root, limit)['status'], 'prepared')
        self.assertEqual(d.prepare(self.root, limit + timedelta(microseconds=1))['status'], 'wait_next_slot')
        # The selected normal next slot retains its former late-start semantics.
        self.assertEqual(d.prepare(self.root, self.SLOT + timedelta(hours=5))['status'], 'prepared')
        fake = CycleFake(self.root); fake.now_factory = lambda: limit
        identity = d.prepare(self.root, limit)['dispatch_id']
        original = Entry._start_sheet_cycle_job
        def drift(**kwargs):
            fake.now_factory = lambda: limit + timedelta(microseconds=1)
            return original(fake, **kwargs)
        with patch.object(fake, '_start_sheet_cycle_job', side_effect=drift):
            self.assertFalse(d.dispatch(fake, {'dispatch_id': identity})['accepted'])
        self.assertEqual(fake.events, [])
        self.assertIsNone(CycleReceiptStore(self.root, lambda: '').read(d._decode(identity)['cycle_id']))

    def test_clock_rechecked_after_metadata_proof_and_slot_rollover(self):
        self.hold(); self.resume()
        limit = self.SLOT + timedelta(hours=1)
        reads = []
        original = self.wake.load
        def metadata(runtime):
            reads.append('metadata')
            return original(runtime)
        def clock():
            reads.append('clock')
            return limit + timedelta(microseconds=1)
        with patch.object(self.wake, 'load', side_effect=metadata):
            self.assertEqual(self.wake.policy(self.root, clock, expected_slot=self.SLOT.isoformat()), 'wait_next_slot')
        self.assertEqual(reads, ['metadata', 'clock'])
        self.assertEqual(self.wake.policy(self.root, lambda: self.SLOT + timedelta(hours=3), expected_slot=self.SLOT.isoformat()), 'slot_expired')

    def test_no_missed_slot_disabled_owner_and_already_owned_slot_do_not_arm(self):
        self.hold(self.SLOT + timedelta(minutes=1)); self.resume()
        self.assertIsNone(self.wake.load(self.root))
        self.options['window_id'] = 'wakeup-test-002'
        self.systemd.states[p.WAREHOUSE_TIMER].update(is_enabled='disabled', is_active='inactive')
        self.hold(); self.resume()
        self.assertIsNone(self.wake.load(self.root))
        self.systemd.states[p.WAREHOUSE_TIMER].update(is_enabled='enabled', is_active='active')
        fake = CycleFake(self.root); fake.now_factory = lambda: self.SLOT
        accepted = d.dispatch(fake, {'dispatch_id': d.prepare(self.root, self.SLOT)['dispatch_id']})
        self.assertTrue(accepted['accepted'])
        for worker in list(fake.operator_jobs._threads.values()): worker.join(5)
        self.options['window_id'] = 'wakeup-test-003'
        self.hold(); self.resume()
        self.assertIsNone(self.wake.load(self.root))

    def test_partial_pause_exact_restore_still_records_proven_missed_slot(self):
        start = self.SLOT - timedelta(minutes=20)
        self.systemd.fail_once = p.WAREHOUSE_TIMER
        with patch.object(self.barrier, '_utc_now', return_value=start.isoformat()), patch.object(self.pause, 'now_iso', return_value=start.isoformat()):
            with self.assertRaisesRegex(RuntimeError, 'ambiguous local command'):
                self.pause.pause(self.root, **self.options)
        self.assertFalse(self.barrier.barrier_status(self.root)['hold_confirmed'])
        self.resume()
        self.assertEqual(self.wake.load(self.root)['missed_slot'], self.SLOT.isoformat())
        self.assertFalse(self.barrier.barrier_status(self.root)['active'])

    def test_long_pause_coalesces_latest_slot_and_idempotent_resume_does_not_rearm(self):
        self.hold(self.SLOT - timedelta(hours=10)); self.resume()
        self.assertEqual(self.wake.load(self.root)['missed_slot'], self.SLOT.isoformat())
        original = self.wake.load(self.root)
        self.wake._settle(self.root, original, 'accepted', 'complete')
        self.assertTrue(self.resume()['idempotent'])
        self.assertEqual(self.wake.load(self.root)['phase'], 'accepted')
        self.assertEqual(self.pause.load_state(self.root)['baseline'], self.baseline)

    def test_debt_durable_before_release_and_after_release_bookkeeping_crash(self):
        self.hold()
        write = self.barrier._atomic_write_private_json
        def fail_release(path, value):
            if path.name == self.barrier.STATE_FILENAME and value.get('phase') == 'released':
                self.assertIsNotNone(self.wake.load(self.root))
                raise RuntimeError('crash before release')
            return write(path, value)
        with patch.object(self.barrier, '_atomic_write_private_json', side_effect=fail_release):
            with self.assertRaisesRegex(RuntimeError, 'crash before release'): self.resume()
        with patch.object(d, 'launch', side_effect=AssertionError('active barrier launched')):
            self.wake.coordinate(self.root, self.SLOT + timedelta(minutes=30))
        save = self.pause.save_state
        def fail_bookkeeping(runtime, state, event):
            if event == 'pause_released':
                state['phase'] = 'restoring'
                raise RuntimeError('crash after release')
            return save(runtime, state, event)
        with patch.object(self.pause, 'save_state', side_effect=fail_bookkeeping):
            with self.assertRaisesRegex(RuntimeError, 'crash after release'): self.resume()
        self.assertFalse(self.barrier.barrier_status(self.root)['active'])
        self.assertIsNone(self.wake.policy(self.root, self.SLOT + timedelta(minutes=30)))
        self.assertEqual(self.pause.load_state(self.root)['phase'], 'restoring')
        self.assertTrue(self.resume()['idempotent'])
        self.assertEqual(self.pause.load_state(self.root)['baseline'], self.baseline)

    def test_unknown_backup_busy_and_deadline_do_not_resend_or_stay_pending(self):
        self.hold(); self.resume()
        now = self.SLOT + timedelta(minutes=30)
        with heavy_admitted(self.root, operation='finance'):
            self.assertEqual(d.prepare(self.root, now)['status'], 'busy')
        with patch('packages.application.finance_backup_handoff.cycle_backup_priority', return_value={'priority': True}):
            self.assertEqual(d.prepare(self.root, now)['status'], 'backup_priority')
        fake = CycleFake(self.root); fake.now_factory = lambda: now
        calls = []
        def request(method, identity=None):
            calls.append(method)
            if method == 'POST': raise TimeoutError('unknown one POST')
            return self.transport(fake, method, identity)
        with patch.object(d, '_request', side_effect=request):
            self.wake.coordinate(self.root, now)
            self.wake.coordinate(self.root, now)
            self.assertEqual(calls.count('POST'), 1)
            self.assertEqual(self.wake.load(self.root)['last_status'], 'uncertain_same_operation')
            # Deadline checked before unknown readback/busy, with no extra POST/GET.
            old = list(calls)
            self.wake.coordinate(self.root, self.SLOT + timedelta(hours=1, microseconds=1))
            self.assertEqual(calls, old)
        self.assertEqual(self.wake.load(self.root)['phase'], 'wait_next_slot')
        self.assertIn('uncertain', self.wake.load(self.root)['last_status'])
        self.assertEqual(d.prepare(self.root, now)['status'], 'wait_next_slot')

    def test_concurrent_normal_launcher_and_service_restart_share_one_submit(self):
        self.hold(); self.resume()
        now = self.SLOT + timedelta(minutes=30)
        fake = CycleFake(self.root); fake.now_factory = lambda: now
        calls = []
        def request(method, identity=None):
            calls.append(method)
            return self.transport(fake, method, identity)
        with patch.object(d, '_request', side_effect=request):
            worker = threading.Thread(target=d.launch, args=(self.root,)); worker.start()
            self.wake.coordinate(self.root, now); worker.join(5)
            self.wake.coordinate(self.root, now)  # Fresh coordinator after restart.
            self.assertEqual(calls.count('POST'), 1)
            self.assertEqual(self.wake.load(self.root)['phase'], 'accepted')
        for worker in list(fake.operator_jobs._threads.values()): worker.join(5)
        self.assertEqual(fake.events.count('warehouse'), 1)

    def test_backup_wait_then_accept_and_normal_slot_receipt_satisfies_debt(self):
        self.hold(); self.resume()
        now = self.SLOT + timedelta(minutes=30)
        fake = CycleFake(self.root); fake.now_factory = lambda: now
        calls = []
        def request(method, identity=None):
            calls.append(method)
            return self.transport(fake, method, identity)
        with patch.object(d, '_request', side_effect=request):
            with patch('packages.application.finance_backup_handoff.cycle_backup_priority', return_value={'priority': True}):
                self.wake.coordinate(self.root, now)
                self.assertNotIn('POST', calls)
                self.assertEqual(self.wake.load(self.root)['last_status'], 'backup_priority')
            d.launch(self.root)  # Normal scheduled launcher wins before wake tick.
            self.wake.coordinate(self.root, now)
            self.assertEqual(calls.count('POST'), 1)
        self.assertEqual(self.wake.load(self.root)['phase'], 'accepted')
        self.assertEqual(self.wake.load(self.root)['last_status'], 'canonical_slot_already_accepted')
        for worker in list(fake.operator_jobs._threads.values()): worker.join(5)

    def test_prepare_rollover_rejects_distinct_signed_slot_before_journal_or_post(self):
        self.hold(); self.resume()
        snapshot = self.SLOT + timedelta(minutes=30)
        fake = CycleFake(self.root); fake.now_factory = lambda: snapshot
        calls = []
        def request(method, identity=None):
            calls.append(method)
            if method == 'GET' and identity is None:
                fake.now_factory = lambda: self.SLOT + timedelta(hours=3, minutes=1)
            return self.transport(fake, method, identity)
        with patch.object(d, '_request', side_effect=request):
            self.wake.coordinate(self.root, snapshot)
            self.assertEqual(calls, ['GET'])
            self.assertIsNone(d._read_record(self.root))
            self.assertEqual(fake.events, [])
            self.assertFalse((self.root / 'sheet-vitrina-cycles').exists())
            value = self.wake.load(self.root)
            self.assertEqual(value['phase'], 'wait_next_slot')
            self.assertEqual(value['last_status'], 'prepared_slot_differs_from_maintenance_debt')
            # The ordinary timer, with no debt binding, retains its normal slot.
            result = d.launch(self.root)
            self.assertTrue(result['accepted'])
            self.assertEqual(d._decode(result['dispatch_id'])['slot'], (self.SLOT + timedelta(hours=3)).isoformat())
            self.assertEqual(calls.count('POST'), 1)
        for worker in list(fake.operator_jobs._threads.values()): worker.join(5)

    def test_rollover_after_prepare_before_post_rejects_old_identity_without_new_cycle(self):
        self.hold(); self.resume()
        snapshot = self.SLOT + timedelta(minutes=30)
        fake = CycleFake(self.root); fake.now_factory = lambda: snapshot
        calls = []
        def request(method, identity=None):
            calls.append(method)
            if method == 'POST':
                fake.now_factory = lambda: self.SLOT + timedelta(hours=3, minutes=1)
            return self.transport(fake, method, identity)
        with patch.object(d, '_request', side_effect=request):
            self.wake.coordinate(self.root, snapshot)
            record = d._read_record(self.root)
            self.assertEqual(d._decode(record['dispatch_id'])['slot'], self.SLOT.isoformat())
            self.assertEqual(record['phase'], 'terminal')
            self.assertEqual(record['last_status'], 'not_accepted')
            self.assertEqual(fake.events, [])
            self.assertFalse((self.root / 'sheet-vitrina-cycles').exists())
            self.assertEqual(self.wake.load(self.root)['phase'], 'wait_next_slot')
            self.wake.coordinate(self.root, snapshot)
            self.assertEqual(calls.count('POST'), 1)  # Old identity never resent.

    def test_bound_launch_reads_unknown_old_record_without_new_prepare_or_submit(self):
        self.hold(); self.resume()
        old = d.prepare(self.root, self.SLOT - timedelta(hours=3))['dispatch_id']
        d._write_record(self.root, {'schema': 1, 'dispatch_id': old, 'phase': 'uncertain', 'last_status': 'unknown'})
        def request(method, identity=None):
            self.assertEqual((method, identity), ('GET', old))
            return {'status': 'unknown', 'accepted': None, 'dispatch_id': old}
        with patch.object(d, '_request', side_effect=request) as transport:
            self.wake.coordinate(self.root, self.SLOT + timedelta(minutes=30))
            self.wake.coordinate(self.root, self.SLOT + timedelta(minutes=30))
            self.assertEqual(transport.call_count, 2)
        self.assertEqual(d._read_record(self.root)['dispatch_id'], old)
        self.assertEqual(self.wake.load(self.root)['last_status'], 'uncertain_same_operation')

    def test_invalid_prepared_identity_never_journals_or_spends_one_post(self):
        self.hold(); self.resume()
        with patch.object(d, '_request', return_value={'status': 'prepared', 'dispatch_id': 'invalid.identity'}) as transport:
            self.wake.coordinate(self.root, self.SLOT + timedelta(minutes=30))
        transport.assert_called_once_with('GET')
        self.assertIsNone(d._read_record(self.root))
        self.assertEqual(self.wake.load(self.root)['phase'], 'pending')
        self.assertEqual(self.wake.load(self.root)['last_status'], 'launch_error_ValueError')

    def test_restart_after_rollover_expires_old_debt_without_borrowing_new_slot(self):
        self.hold(); self.resume()
        for age in (timedelta(hours=3), timedelta(hours=4, minutes=30)):
            value = self.wake.load(self.root)
            self.wake._settle(self.root, value, 'pending', 'busy')
            now = self.SLOT + age
            with patch.object(d, 'launch', side_effect=AssertionError('old debt launched new slot')):
                self.wake.coordinate(self.root, now)
            self.assertEqual(self.wake.load(self.root)['phase'], 'wait_next_slot')
            self.assertEqual(d.prepare(self.root, now)['status'], 'prepared')

    def test_service_hook_only_background_pending_not_status_or_empty_runtime(self):
        from packages.adapters.registry_upload_http_entrypoint import RegistryUploadHttpServer
        self.hold(); self.resume()
        fake = CycleFake(self.root); fake.now_factory = lambda: self.SLOT + timedelta(minutes=30)
        # Status remains metadata-only even with durable pending work.
        with patch.object(self.wake, 'coordinate') as coordinate:
            Entry.handle_business_data_cycle_dispatch_request(fake)
            coordinate.assert_not_called()
        handler = type('Handler', (), {'runtime_entrypoint': fake})
        server = RegistryUploadHttpServer(('127.0.0.1', 0), handler)
        thread_ids = []
        try:
            with patch.object(self.wake, 'coordinate', side_effect=lambda *args: thread_ids.append(threading.get_ident())):
                server.service_actions()
                server._maintenance_cycle_wakeup.thread.join(5)
                self.assertEqual(len(thread_ids), 1)
                self.assertNotEqual(thread_ids[0], threading.get_ident())
                server.service_actions()  # Same process hook has bounded polling.
                self.assertEqual(len(thread_ids), 1)
                replacement = self.wake.ServiceWakeup(self.root)
                replacement.tick(); replacement.thread.join(5)
                self.assertEqual(len(thread_ids), 2)  # Startup recovers pending intent.
                replacement.close(); replacement.next_check = 0; replacement.tick()
                self.assertEqual(len(thread_ids), 2)
        finally:
            server.server_close()
        empty = self.wake.ServiceWakeup(self.root / 'missing')
        empty.tick(); self.assertIsNone(empty.thread)

    def test_service_thread_start_failure_does_not_escape_or_duplicate_possible_worker(self):
        self.hold(); self.resume()
        wakeup = self.wake.ServiceWakeup(self.root)
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('proven no start')):
            wakeup.tick()  # No exception escapes to HTTPServer.service_actions.
        self.assertIsNone(wakeup.thread)
        self.assertIn('proven no start', wakeup.last_error)
        self.assertEqual(self.wake.load(self.root)['phase'], 'pending')
        entered, release = threading.Event(), threading.Event()
        original = threading.Thread.start
        def started_then_throws(thread):
            original(thread)
            self.assertTrue(entered.wait(2))
            raise RuntimeError('started then throws')
        def coordinate(*args):
            entered.set(); release.wait(3)
        wakeup.next_check = 0
        with patch.object(self.wake, 'coordinate', side_effect=coordinate) as run, patch.object(threading.Thread, 'start', autospec=True, side_effect=started_then_throws) as start:
            wakeup.tick()
            handle = wakeup.thread
            self.assertTrue(handle.is_alive())
            self.assertIn('started then throws', wakeup.last_error)
            wakeup.next_check = 0; wakeup.tick()
            self.assertIs(wakeup.thread, handle)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(run.call_count, 1)
            release.set(); handle.join(5)
        # Unknown native start without a bootstrapped child also retains handle.
        class LimboThread:
            def __init__(self, **kwargs): self._started = threading.Event()
            def is_alive(self): return False
            def start(self): raise RuntimeError('native start uncertain')
        wakeup = self.wake.ServiceWakeup(self.root)
        with patch.object(threading, 'Thread', side_effect=LimboThread) as create, patch('packages.application.business_data_procedure_admission.thread_start_is_proven_absent', return_value=False):
            wakeup.tick(); handle = wakeup.thread
            wakeup.next_check = 0; wakeup.tick()
            self.assertIs(wakeup.thread, handle)
            self.assertEqual(create.call_count, 1)
        self.assertEqual(self.wake.load(self.root)['phase'], 'pending')

    def test_known_race_refusal_has_explicit_wait_next_slot_outcome(self):
        self.hold(); self.resume()
        now = self.SLOT + timedelta(minutes=30)
        fake = CycleFake(self.root); fake.now_factory = lambda: now
        def request(method, identity=None):
            if method == 'POST': return {'status': 'not_accepted', 'accepted': False, 'dispatch_id': identity}
            return self.transport(fake, method, identity)
        with patch.object(d, '_request', side_effect=request) as request_mock:
            self.wake.coordinate(self.root, now)
            self.wake.coordinate(self.root, now)
            self.assertEqual([x.args[0] for x in request_mock.call_args_list].count('POST'), 1)
        self.assertEqual(self.wake.load(self.root)['phase'], 'wait_next_slot')
        self.assertEqual(self.wake.load(self.root)['last_status'], 'single_submit_not_accepted')

    def test_new_maintenance_does_not_erase_same_slot_deadline_fence(self):
        self.hold(); self.resume()
        early = self.SLOT + timedelta(minutes=30)
        identity = d.prepare(self.root, early)['dispatch_id']
        self.options['window_id'] = 'wakeup-test-002'
        self.hold(self.SLOT + timedelta(minutes=40))
        with patch.object(d, 'launch', side_effect=AssertionError('old coordinator launched')):
            self.wake.coordinate(self.root, self.SLOT + timedelta(minutes=41))
        self.assertEqual(self.wake.load(self.root)['phase'], 'superseded')
        late = self.SLOT + timedelta(minutes=90)
        self.resume(late)  # New window has no distinct missed fixed slot.
        self.assertFalse(self.barrier.barrier_status(self.root)['active'])
        self.assertEqual(self.wake.policy(self.root, late), 'wait_next_slot')
        self.assertEqual(d.prepare(self.root, late)['status'], 'wait_next_slot')
        fake = CycleFake(self.root); fake.now_factory = lambda: late
        result = d.dispatch(fake, {'dispatch_id': identity})
        self.assertFalse(result['accepted'])
        self.assertEqual(fake.events, [])
        calls = []
        def request(method, dispatch_id=None):
            calls.append(method)
            return self.transport(fake, method, dispatch_id)
        with patch.object(d, '_request', side_effect=request):
            self.assertEqual(d.launch(self.root)['status'], 'wait_next_slot')
        self.assertEqual(calls, ['GET'])
        self.assertIsNone(d._read_record(self.root))
        fake.now_factory = lambda: self.SLOT + timedelta(hours=3)
        self.assertEqual(d.prepare(self.root, fake.now_factory())['status'], 'prepared')
        self.assertIsNone(self.wake.policy(self.root, fake.now_factory()))

    def test_early_new_window_after_prepare_blocks_coordinator_post_admission(self):
        self.hold(); self.resume()
        snapshot = self.SLOT + timedelta(minutes=30)
        fake = CycleFake(self.root); fake.now_factory = lambda: snapshot
        calls = []
        def request(method, identity=None):
            calls.append(method)
            if method == 'POST':
                self.options['window_id'] = 'wakeup-test-002'
                self.hold(self.SLOT + timedelta(minutes=40))
                self.resume(self.SLOT + timedelta(minutes=50))
                fake.now_factory = lambda: self.SLOT + timedelta(minutes=50)
            return self.transport(fake, method, identity)
        with patch.object(d, '_request', side_effect=request):
            self.wake.coordinate(self.root, snapshot)
        self.assertEqual(calls, ['GET', 'POST'])
        self.assertEqual(fake.events, [])
        self.assertFalse((self.root / 'sheet-vitrina-cycles').exists())
        self.assertEqual(d._read_record(self.root)['last_status'], 'not_accepted')
        self.assertEqual(self.wake.load(self.root)['phase'], 'superseded')
        self.assertEqual(d.prepare(self.root, fake.now_factory())['status'], 'wait_next_slot')

    def test_early_new_window_between_dispatch_prepare_and_actual_ex_blocks_old_identity(self):
        self.hold(); self.resume()
        early = self.SLOT + timedelta(minutes=30)
        identity = d.prepare(self.root, early)['dispatch_id']
        fake = CycleFake(self.root); fake.now_factory = lambda: early
        original = Entry._start_sheet_cycle_job
        def changed_window(**kwargs):
            self.options['window_id'] = 'wakeup-test-002'
            self.hold(self.SLOT + timedelta(minutes=40))
            self.resume(self.SLOT + timedelta(minutes=50))
            fake.now_factory = lambda: self.SLOT + timedelta(minutes=50)
            return original(fake, **kwargs)
        with patch.object(fake, '_start_sheet_cycle_job', side_effect=changed_window):
            result = d.dispatch(fake, {'dispatch_id': identity})
        self.assertFalse(result['accepted'])
        self.assertEqual(fake.events, [])
        self.assertFalse((self.root / 'sheet-vitrina-cycles').exists())
        self.assertEqual(self.wake.policy(self.root, fake.now_factory()), 'wait_next_slot')
        self.assertIsNone(self.wake.policy(self.root, self.SLOT + timedelta(hours=3)))

    def test_coordinator_readback_of_distinct_slot_does_not_ack_old_debt(self):
        self.hold(); self.resume()
        future = self.SLOT + timedelta(hours=3)
        identity = d.prepare(self.root, future)['dispatch_id']
        with patch.object(d, 'launch', return_value={'status': 'running', 'accepted': True, 'dispatch_id': identity}) as launch:
            self.wake.coordinate(self.root, self.SLOT + timedelta(minutes=30))
        launch.assert_called_once_with(self.root, expected_slot=self.SLOT.isoformat())
        self.assertEqual(self.wake.load(self.root)['phase'], 'wait_next_slot')
        self.assertEqual(self.wake.load(self.root)['last_status'], 'distinct_slot_already_owned')

    def test_reused_window_id_and_plan_cannot_revive_old_pending_debt(self):
        self.hold(); self.resume()
        old = self.wake.load(self.root)
        self.hold(self.SLOT + timedelta(minutes=31))
        self.assertEqual(self.barrier._load_state(self.root)['window_id'], old['window_id'])
        self.assertEqual(self.barrier._load_state(self.root)['plan_fingerprint'], old['plan_fingerprint'])
        with patch.object(d, 'launch', side_effect=AssertionError('stale generation launched')):
            self.wake.coordinate(self.root, self.SLOT + timedelta(minutes=35))
        self.assertEqual(self.wake.load(self.root)['phase'], 'superseded')
        self.resume(self.SLOT + timedelta(minutes=40))
        self.assertEqual(self.wake.load(self.root)['phase'], 'superseded')
        self.assertEqual(self.wake.policy(self.root, self.SLOT + timedelta(minutes=45)), 'wait_next_slot')

    def test_new_maintenance_supersedes_old_pending_and_readonly_diagnostics_no_effects(self):
        self.hold(); self.resume()
        now = self.SLOT + timedelta(minutes=30)
        self.options['window_id'] = 'wakeup-test-002'
        self.hold(self.SLOT + timedelta(minutes=31))
        before = {x.name: x.read_bytes() for x in self.root.iterdir() if x.is_file()}
        with patch.object(d, 'launch', side_effect=AssertionError('status launched')):
            self.wake.diagnostics(self.root, now)
            d.prepare(self.root, now)
            self.assertEqual(before, {x.name: x.read_bytes() for x in self.root.iterdir() if x.is_file()})
            self.wake.coordinate(self.root, now)
        self.assertEqual(self.wake.load(self.root)['phase'], 'superseded')
        self.resume()
        self.assertEqual(self.wake.load(self.root)['phase'], 'superseded')


if __name__=='__main__':unittest.main()
