"""Real Ready-only commit/readback/rollback, source races and non-target guards."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from decimal import Decimal
import json,sqlite3,sys,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.ready_publication_smoke import make_plan
from apps.fbs_inventory_presentation_smoke import snapshot
from apps.shared_sku_cost_smoke import fbs,wb
from apps.fbs_snapshot_cost_smoke import capture as cost_capture
from packages.application.fbs_snapshot_cost import evaluate_candidate,close_candidate_period
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application import fbs_accounting_runtime as accounting,management_accounting_reconciliation as owner
from packages.application.calculation_parameters import DEFAULT_PROXY_PARAMETERS
from packages.application.registry_upload_db_backed_runtime import _serialize_sheet_vitrina_plan
from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter
from packages.application.web_vitrina_management_history import digest,data_sheet
DAY='2026-09-08'

class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.db=self.root/'registry_upload_runtime.sqlite3'
  state=close_candidate_period(evaluate_candidate(fbs(),cost_capture(DAY)),DAY,today='2026-09-09')
  presentation=snapshot(state,wb(DAY),day=DAY).payload()
  self.book=dict(schema=accounting.SCHEMA,active=True,effective_date='2026-09-07',state=state,presentations={DAY:presentation},
   shared_days={DAY:build_shared_cost_day(state,wb(DAY),DAY)},wb_days={},retained_days={})
  self.version=accounting._save_book(self.root,self.book,expected=None,operation_id='fixture')
  plan=make_plan(as_of_date=DAY,day='2026-09-09')
  plan=accounting.materialize(plan,runtime_dir=self.root,book=self.book,book_version=self.version,ready_target=dict(bundle_version='b',as_of_date=DAY))
  p4=SimpleNamespace(version_id='v4',buyout_rate=Decimal('.8'),included_expense_rate=Decimal('.3'),retained_share=Decimal('.7'))
  from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
  from packages.application.web_vitrina_management_history import recalculate_dated_proxy
  projected=recalculate_dated_proxy(json.loads(_serialize_sheet_vitrina_plan(plan)),day=DAY,parameters=(DEFAULT_PROXY_PARAMETERS,p4),operation_id='existing')['plan']
  plan=_deserialize_sheet_vitrina_plan(json.dumps(projected))
  metadata=deepcopy(plan.metadata);metadata[owner.METADATA_KEY]=dict(reason=owner.REASON,dates=[DAY],book_version='old')
  metadata['server_cell_presentation']['SKU:1|our_wb_unit_cost_rub'][DAY].update(reason_code=owner.REASON,state='unavailable')
  plan=replace(plan,metadata=metadata)
  self.before=_serialize_sheet_vitrina_plan(plan)
  with self.conn() as c:
   c.executescript('CREATE TABLE registry_upload_current_state(slot INTEGER PRIMARY KEY,bundle_version TEXT); INSERT INTO registry_upload_current_state VALUES(1,\'b\'); CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version TEXT,as_of_date TEXT,plan_json TEXT,snapshot_id TEXT,plan_version TEXT,activated_at TEXT,refreshed_at TEXT,PRIMARY KEY(bundle_version,as_of_date));')
   c.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?,?,?)',('b',DAY,self.before,plan.snapshot_id,'v1','a','r'));c.commit()
  p4=SimpleNamespace(version_id='v4',buyout_rate=Decimal('.8'),included_expense_rate=Decimal('.3'),retained_share=Decimal('.7'))
  patched=patch.object(owner,'dated_parameters',return_value=(DEFAULT_PROXY_PARAMETERS,p4));patched.start();self.addCleanup(patched.stop)
  with self.conn() as c:source,_,_=owner.capture(self.root,c,dates=[DAY],ready_dates=[DAY])
  self.request=dict(runtime_dir=str(self.root),runtime_sha='a'*40,source=source,source_sha256=digest(source),dates=[DAY],reconcile_recovered_accounting=True)
  self.adapter=WebVitrinaManagementHistoryAdapter();patched=patch.object(self.adapter,'target',return_value=(self.root,self.db));patched.start();self.addCleanup(patched.stop)
 def conn(self):
  c=sqlite3.connect(self.db);c.row_factory=sqlite3.Row;return closing(c)
 def ready(self):
  with self.conn() as c:return c.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0]
 def test_real_commit_readback_and_exact_rollback(self):
  preview=self.adapter.preview(self.request,'repair');self.assertEqual(len(preview['candidate']['updates']),1);self.assertEqual(preview['candidate']['changes'],[])
  self.adapter.apply(self.request,'repair',preview);self.assertEqual(self.adapter.readback(self.request,'repair')['state'],'applied')
  actual=json.loads(self.ready());self.assertNotIn(owner.METADATA_KEY,actual['metadata']);self.assertEqual(data_sheet(actual),data_sheet(json.loads(self.before)))
  self.assertEqual(accounting.load(self.root),(self.book,self.version))
  self.adapter.rollback(self.request,'repair');self.assertEqual(self.ready(),self.before)
 def test_ready_and_book_cas_and_past_scope(self):
  preview=self.adapter.preview(self.request,'repair')
  with self.conn() as c:c.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='drift'");c.commit()
  with self.assertRaisesRegex(ValueError,'candidate-cas-drift'):self.adapter.apply(self.request,'repair',preview)
  with self.conn() as c:c.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='r'");c.commit()
  changed=deepcopy(self.book);changed['note']='new revision';accounting._save_book(self.root,changed,expected=self.version,operation_id='changed')
  with self.assertRaisesRegex(ValueError,'source-compare-and-swap'):self.adapter.apply(self.request,'repair',preview)
  with self.conn() as c:
   with self.assertRaisesRegex(ValueError,'past-dates-only'):owner.capture(self.root,c,dates=['2999-01-01'],ready_dates=['2999-01-01'])
 def test_numeric_change_and_unhealthy_book_block(self):
  raw=json.loads(self.before);data_sheet(raw)['rows'][0][2]=99999
  with self.conn() as c:c.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(json.dumps(raw),));c.commit()
  with self.conn() as c:source,_,_=owner.capture(self.root,c,dates=[DAY],ready_dates=[DAY])
  req={**self.request,'source':source,'source_sha256':digest(source)}
  with self.assertRaisesRegex(ValueError,'numeric-data-change'):self.adapter.preview(req,'bad')
  bad=deepcopy(self.book);bad['publication_error']={'reason':'incomplete'};accounting._save_book(self.root,bad,expected=self.version,operation_id='bad')
  with self.conn() as c:
   with self.assertRaisesRegex(ValueError,'healthy-book-required'):owner.capture(self.root,c,dates=[DAY],ready_dates=[DAY])
 def test_uncertain_submit_rolls_back_without_resubmit(self):
  import apps.web_vitrina_management_history as module
  from apps.production_apply_contract import AmbiguousSubmit
  preview=self.adapter.preview(self.request,'repair');real=module.replace_ready
  def fail(*args,**kwargs):
   real(*args,**kwargs)
   raise RuntimeError('injected after Ready update')
  with patch.object(module,'replace_ready',side_effect=fail),self.assertRaises(AmbiguousSubmit):
   self.adapter.apply(self.request,'repair',preview)
  self.assertEqual(self.ready(),self.before)
  self.assertEqual(self.adapter.readback(self.request,'repair')['state'],'ambiguous')
  with self.assertRaises(AmbiguousSubmit):self.adapter.apply(self.request,'repair',preview)
  self.assertEqual(self.ready(),self.before)
  with self.assertRaisesRegex(ValueError,'operation-request-changed'):
   self.adapter.rollback({**self.request,'reconcile_recovered_accounting':False},'repair')

 def test_adjacent_unbound_day_is_preserved(self):
  raw=json.loads(self.before);adjacent='2026-09-09'
  raw['metadata'][owner.METADATA_KEY]['dates'].append(adjacent)
  raw['metadata']['server_cell_presentation']['SKU:1|our_wb_unit_cost_rub'][adjacent]=dict(reason_code=owner.REASON,state='unavailable')
  encoded=json.dumps(raw)
  with self.conn() as c:c.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(encoded,));c.commit()
  with self.conn() as c:source,_,_=owner.capture(self.root,c,dates=[DAY],ready_dates=[DAY])
  req={**self.request,'source':source,'source_sha256':digest(source)}
  preview=self.adapter.preview(req,'own-only');after=json.loads(preview['candidate']['updates'][0]['after_plan_json'])
  self.assertEqual(after['metadata'][owner.METADATA_KEY]['dates'],[adjacent])
  self.assertEqual(after['metadata']['server_cell_presentation']['SKU:1|our_wb_unit_cost_rub'][adjacent],raw['metadata']['server_cell_presentation']['SKU:1|our_wb_unit_cost_rub'][adjacent])
  self.assertEqual(data_sheet(after),data_sheet(raw))

 def test_existing_presentation_binding_and_reason_are_not_replaced(self):
  for field in ('presentation','reason'):
   raw=json.loads(self.before)
   if field=='presentation':raw['metadata']['fbs_accounting_bindings'][DAY]['presentation_version']='different'
   else:raw['metadata'][owner.METADATA_KEY]['reason']='other_incident'
   with self.conn() as c:c.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(json.dumps(raw),));c.commit()
   with self.conn() as c:source,_,_=owner.capture(self.root,c,dates=[DAY],ready_dates=[DAY])
   request={**self.request,'source':source,'source_sha256':digest(source)}
   with self.assertRaisesRegex(ValueError,'saved-presentation-binding-changed|recognized-suppression-required'):
    self.adapter.preview(request,'bad-'+field)

 def test_readback_identity_drift_and_request_mismatch(self):
  preview=self.adapter.preview(self.request,'repair');self.adapter.apply(self.request,'repair',preview)
  with self.conn() as c:c.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET snapshot_id='different'");c.commit()
  self.assertEqual(self.adapter.readback(self.request,'repair')['state'],'ambiguous')
  with self.assertRaisesRegex(ValueError,'rollback-cas-drift'):self.adapter.rollback(self.request,'repair')
  with self.assertRaisesRegex(ValueError,'operation-request-changed'):self.adapter.readback({**self.request,'runtime_sha':'b'*40},'repair')

if __name__=='__main__':unittest.main()
