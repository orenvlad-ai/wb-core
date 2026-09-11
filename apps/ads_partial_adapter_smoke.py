"""Offline exact source pins, atomic Ads publication, CAS and same-operation readback."""
import json
import socket
import sqlite3
import sys
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime,timezone
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.ads_partial_publication import AdsPartialPublicationAdapter,CONTROL_FILES
from apps.ads_partial_publication_smoke import DAY,OBS
from apps.ads_daily_report_contract_smoke import campaign
from packages.contracts.source_attempt_diagnostics import source_digest
from packages.application.web_vitrina_management_history import digest
from packages.application.vitrina_economics import METRICS,TOTALS
from packages.contracts.registry_upload_bundle_v1 import MetricV2Item,ConfigV2Item
@dataclass
class Parameters:
 buyout_rate:Decimal=Decimal('.8')
 retained_share:Decimal=Decimal('.7')
 included_expense_rate:Decimal=Decimal('.3')
 version_id:str='fixture-dated'

def source():
 def observation(path,payload):return dict(path=path,payload=payload,payload_digest=source_digest(payload),status=200,started_at='2026-09-11T18:10:00Z',finished_at=OBS)
 catalog={'all':2,'adverts':[{'status':9,'count':2,'advert_list':[{'advertId':5},{'advertId':6}]}]}
 return {'date':DAY,'catalog':observation('/adv/v1/promotion/count',catalog),'observations':[observation('/adv/v3/fullstats?ids=6&beginDate='+DAY+'&endDate='+DAY,None),observation('/adv/v3/fullstats?ids=5&beginDate='+DAY+'&endDate='+DAY,[campaign(day=DAY)])]}

class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name);self.runtime=root/'state';db=self.runtime/'generations/g/db.sqlite';db.parent.mkdir(parents=True);(root/'app').mkdir();(root/'app/.wb-core-runtime-sha').write_text('a'*40)
  (self.runtime/'storage_generation_manifest.json').write_text(json.dumps({'canonical_source':'split','manifest_sha256':'m','operational':{'relative_path':'generations/g/db.sqlite'}}))
  controls={name:{'active':False,'revision':72} for name in CONTROL_FILES}
  for name,value in controls.items():(self.runtime/name).write_text(json.dumps(value))
  self.db=db;self.adapter=AdsPartialPublicationAdapter(now_factory=lambda:datetime(2026,9,11,19,30,tzinfo=timezone.utc));src=source()
  self.request={'runtime_dir':str(self.runtime),'storage_manifest_sha256':'m','runtime_sha':'a'*40,'hostname':socket.gethostname(),'control_digests':{n:digest(v) for n,v in controls.items()},'source':src,'source_sha256':digest(src),'prepared_at':'2026-09-11T19:20:00Z','bundle_version':'fixture','ready_as_of_date':'2026-09-10'}
  keys=['orderSum','orderCount','our_wb_unit_cost_rub','ads_sum',*METRICS];metrics=[MetricV2Item(k,True,'SKU',k,'metric',k,True,'number',i,'fixture') for i,k in enumerate(keys)];metrics.append(MetricV2Item('total_ads_sum',True,'TOTAL','total','metric','ads_sum',True,'number',99,'ads'))
  metrics.extend(MetricV2Item(k,True,'TOTAL',k,'metric',k,True,'number',100+i,'proxy') for i,k in enumerate(TOTALS))
  rows=[['total','TOTAL|'+k,123,''] for k in ['total_ads_sum',*TOTALS]]
  for n in (101,102):
   data={'orderSum':100,'orderCount':2,'our_wb_unit_cost_rub':10,'ads_sum':''}
   rows.extend([[str(n),f'SKU:{n}|'+k,123,data.get(k,'')] for k in keys])
  plan={'sheets':[{'sheet_name':'DATA_VITRINA','header':['label','key','2026-09-10',DAY],'rows':rows},{'sheet_name':'STATUS','header':['source_key','kind','snapshot_date','note'],'rows':[['ads_compact[yesterday_closed]','error','','old10'],['ads_compact[today_current]','error','','old11'],['fin_report_daily[today_current]','error','','finance']]}],'metadata':{},'date_columns':['2026-09-10',DAY],'temporal_slots':[{'slot_key':'yesterday_closed','column_date':'2026-09-10'},{'slot_key':'today_current','column_date':DAY}]}
  with closing(sqlite3.connect(db)) as c, c:
   c.executescript('CREATE TABLE registry_upload_current_state(slot,bundle_version,activated_at);CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version,as_of_date,plan_json,refreshed_at,activated_at,snapshot_id,plan_version,PRIMARY KEY(bundle_version,as_of_date));CREATE TABLE temporal_source_slot_snapshots(source_key,snapshot_date,snapshot_role,captured_at,payload_json,PRIMARY KEY(source_key,snapshot_date,snapshot_role));CREATE TABLE temporal_source_closure_state(source_key,target_date,slot_kind,state,attempt_count,next_retry_at,last_reason,last_attempt_at,last_success_at,accepted_at,PRIMARY KEY(source_key,target_date,slot_kind));')
   c.execute('INSERT INTO registry_upload_current_state VALUES(1,?,?)',('fixture',OBS));c.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?,?,?)',('fixture','2026-09-10',json.dumps(plan),OBS,OBS,'snapshot','v1'))
  for name,value in [('_load_metric_items',metrics),('_load_formula_items',[]),('_load_config_items',[ConfigV2Item(n,True,str(n),'g',n) for n in (101,102)]),('dated_parameters',(Parameters(),Parameters()))]:
   self.addCleanup(patch.stopall);patch('apps.ads_partial_publication.'+name,return_value=value).start()
 def snapshot(self):
  with closing(sqlite3.connect(self.db)) as c, c:return [c.execute('SELECT * FROM '+t).fetchall() for t in ('sheet_vitrina_v1_ready_snapshots','temporal_source_slot_snapshots','temporal_source_closure_state')]
 def test_atomic_apply_and_no_resubmit(self):
  before=self.snapshot();p=self.adapter.preview(self.request,'fixture-applied');self.assertEqual(before,self.snapshot())
  self.adapter.apply(self.request,'fixture-applied',p);r=self.adapter.readback(self.request,'fixture-applied');self.assertEqual(r['state'],'applied')
  after=self.snapshot();payload=json.loads(after[1][0][4]);self.assertEqual(payload['kind'],'incomplete');self.assertEqual(payload['diagnostics']['source_observed_at'],OBS)
  self.assertEqual(after[2][0][3],'closure_retrying');self.assertIsNone(after[2][0][8])
  status=json.loads(after[0][0][2])['sheets'][1]['rows'];self.assertEqual(status[0],['ads_compact[yesterday_closed]','error','','old10']);self.assertEqual(status[1][1],'incomplete');self.assertEqual(status[2],['fin_report_daily[today_current]','error','','finance'])
  with self.assertRaises(ValueError):self.adapter.apply(self.request,'fixture-applied',p)
  self.assertEqual(after,self.snapshot())
 def test_source_drift_and_ready_cas(self):
  p=self.adapter.preview(self.request,'fixture-cas');self.request['source']['observations'][1]['payload'][0]['sum']=99
  with self.assertRaisesRegex(ValueError,'source-drift'):self.adapter.apply(self.request,'fixture-cas',p)
  self.request['source']=source()
  with closing(sqlite3.connect(self.db)) as c, c:c.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='drift'")
  before=self.snapshot()
  with self.assertRaisesRegex(ValueError,'cas-drift'):self.adapter.apply(self.request,'fixture-cas',p)
  self.assertEqual(before,self.snapshot())
 def test_atomic_rollback_preserves_before_image(self):
  p=self.adapter.preview(self.request,'fixture-rollback');before=self.snapshot()
  with patch('apps.ads_partial_publication.replace_ready',side_effect=RuntimeError('synthetic before ready write')):
   with self.assertRaises(RuntimeError):self.adapter.apply(self.request,'fixture-rollback',p)
  self.assertEqual(before,self.snapshot());self.assertTrue(Path(p['recovery']['path']).is_file())
  self.assertEqual(self.adapter.readback(self.request,'fixture-rollback')['state'],'not_submitted')
 def test_committed_recovery_and_later_cycle_guard(self):
  before=self.snapshot();p=self.adapter.preview(self.request,'fixture-recover');self.adapter.apply(self.request,'fixture-recover',p)
  self.assertEqual(self.adapter.rollback(self.request,'fixture-recover')['state'],'restored');self.assertEqual(before,self.snapshot())
  p=self.adapter.preview(self.request,'fixture-newer');self.adapter.apply(self.request,'fixture-newer',p)
  with closing(sqlite3.connect(self.db)) as c, c:c.execute("UPDATE temporal_source_closure_state SET attempt_count=1")
  newer=self.snapshot()
  with self.assertRaisesRegex(ValueError,'after-image-drift'):self.adapter.rollback(self.request,'fixture-newer')
  self.assertEqual(newer,self.snapshot())
 def test_actual_clock_and_stale_operands(self):
  self.adapter.now_factory=lambda:datetime(2026,9,11,18,30,tzinfo=timezone.utc)
  with self.assertRaisesRegex(ValueError,'closed-date'):self.adapter.preview(self.request,'fixture-future')
  self.adapter.now_factory=lambda:datetime(2026,9,11,19,30,tzinfo=timezone.utc)
  with closing(sqlite3.connect(self.db)) as c, c:
   plan=json.loads(c.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0]);plan['metadata']['server_cell_presentation']={'SKU:101|orderSum':{DAY:{'state':'unavailable','source_as_of_date':DAY}}}
   c.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(json.dumps(plan),))
  p=self.adapter.preview(self.request,'fixture-stale');plan=json.loads(p['candidate']['ready_after_json']);rows={r[1]:r for r in plan['sheets'][0]['rows']}
  self.assertEqual(rows['SKU:101|proxy_profit_4_rub'][3],'')
 def test_retry_is_seen_by_ordinary_selector(self):
  p=self.adapter.preview(self.request,'fixture-due');self.adapter.apply(self.request,'fixture-due',p)
  from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock,CLOSURE_PENDING_STATES
  from types import SimpleNamespace as NS
  def states(**kwargs):
   with closing(sqlite3.connect(self.db)) as c:
    c.row_factory=sqlite3.Row;return [NS(**dict(r)) for r in c.execute('SELECT * FROM temporal_source_closure_state') if r['state'] in kwargs['states']]
  harness=NS(now_factory=lambda:datetime(2026,9,11,20,tzinfo=timezone.utc),runtime=NS(list_temporal_source_closure_states=states))
  due=SheetVitrinaV1LivePlanBlock.list_due_closed_day_retries(harness)
  self.assertEqual([x.target_date for x in due],[DAY])
 def test_controls_and_complete_preservation(self):
  (self.runtime/CONTROL_FILES[0]).write_text(json.dumps({'active':True}))
  with self.assertRaisesRegex(ValueError,'control-drift'):self.adapter.preview(self.request,'fixture-guard')
  (self.runtime/CONTROL_FILES[0]).write_text(json.dumps({'active':False,'revision':72}))
  with closing(sqlite3.connect(self.db)) as c, c:c.execute('INSERT INTO temporal_source_slot_snapshots VALUES(?,?,?,?,?)',('ads_compact',DAY,'accepted_closed_day_snapshot',OBS,json.dumps({'result':{'kind':'success','items':[]}})))
  with self.assertRaisesRegex(ValueError,'complete-snapshot-preserved'):self.adapter.preview(self.request,'fixture-full')
if __name__=='__main__':
 socket.create_connection=lambda *a,**k:(_ for _ in ()).throw(AssertionError('network forbidden'))
 unittest.main(verbosity=2)
