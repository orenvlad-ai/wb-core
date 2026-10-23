"""Real estimate/economics/native CAS boundary on isolated source fixtures."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.vitrina_last_known_cost_smoke import shared_day, save_book
from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter
from apps.production_apply_launcher import execute
from packages.application.calculation_parameters import DEFAULT_PROXY_PARAMETERS, PROXY_BLOCK_KEY
from packages.application.calculation_parameters_v4 import PROXY_V4_BLOCK_KEY
from packages.application.vitrina_last_known_cost import capture_source
from packages.application.vitrina_last_known_projection import project_last_known
from packages.application.web_vitrina_management_history import data_sheet, digest, non_target_digest
from packages.application.vitrina_economics import METRICS, TOTALS

DAY = '2026-10-16'
PARAMS = (DEFAULT_PROXY_PARAMETERS, SimpleNamespace(buyout_rate=Decimal('.8'),
          retained_share=Decimal('.7'), included_expense_rate=Decimal('.3'), version_id='v4-dated'))


def fixture():
    rows = []
    for scope in ('SKU:1', 'SKU:2'):
        for metric, value in [('orderSum', 1000), ('orderCount', 4), ('ads_sum', 10),
                              ('our_wb_unit_cost_rub', ''), ('stock_total', ''), ('own_product_capital_rub', '')]:
            rows.append(['test', scope+'|'+metric, value, 777])
        rows += [['test',scope+'|'+metric,0,777] for metric in METRICS]
    rows += [['test','TOTAL|'+metric,0,777] for metric in TOTALS]
    rows.append(['test','TOTAL|total_our_wb_unit_cost_rub','',777])
    return {'date_columns':[DAY,'2026-10-21'],'metadata':{},'sheets':[
        {'sheet_name':'DATA_VITRINA','header':['label','key',DAY,'2026-10-21'],'rows':rows}]}


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.runtime=Path(self.tmp.name)/'state';self.runtime.mkdir()
        save_book(self.runtime, {'2026-10-15':shared_day('2026-10-15',{'1':('resolved','10'),'2':('resolved','20')})})
        self.source=capture_source(self.runtime,dates=[DAY],bundle_version='b')

    def project(self, plan):
        return project_last_known(plan,dates=[DAY],source=self.source,parameters={DAY:PARAMS},operation_id='cost-test-20261016')

    def test_economics_without_stocks_estimate_provenance_and_partial_ads(self):
        before=fixture();after=self.project(before)
        rows={r[1]:r[2] for r in data_sheet(after['plan'])['rows']}
        self.assertEqual(rows['SKU:1|proxy_profit_4_rub'],518)
        self.assertEqual(rows['TOTAL|total_proxy_profit_4_rub'],1004)
        self.assertEqual(rows['TOTAL|total_our_wb_unit_cost_rub'],'')
        self.assertEqual(rows['SKU:1|stock_total'],'')
        self.assertEqual(rows['SKU:1|own_product_capital_rub'],'')
        self.assertTrue(all(r[3]==777 for r in data_sheet(after['plan'])['rows']))
        self.assertEqual(non_target_digest(before,after['changes']),non_target_digest(after['plan'],after['changes']))
        cell=after['plan']['metadata']['server_cell_presentation']['TOTAL|total_proxy_profit_4_rub'][DAY]
        self.assertEqual(cell['evidence']['last_known_costs']['SKU:1']['source_as_of_date'],'2026-10-15')
        self.assertEqual(self.project(after['plan'])['changes'],[])
        partial=deepcopy(before)
        next(r for r in data_sheet(partial)['rows'] if r[1]=='SKU:2|ads_sum')[2]=''
        result=self.project(partial)['plan']
        rows={r[1]:r[2] for r in data_sheet(result)['rows']}
        self.assertEqual(rows['SKU:2|proxy_profit_4_rub'],'')
        self.assertEqual(rows['TOTAL|total_proxy_profit_4_rub'],518)
        cell=result['metadata']['server_cell_presentation']['TOTAL|total_proxy_profit_4_rub'][DAY]
        self.assertEqual(cell['quality_state'],'partial')
        self.assertIn('SKU:2',cell['evidence']['missing_scope'])

    def test_native_preview_apply_readback_rollback_and_drift(self):
        db=self.runtime/'operational.sqlite3';plan=fixture()
        with sqlite3.connect(db) as c:
            c.executescript('CREATE TABLE registry_upload_current_state(slot INTEGER,bundle_version TEXT);'
                'INSERT INTO registry_upload_current_state VALUES(1,"b");'
                'CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version TEXT,activated_at TEXT,as_of_date TEXT,snapshot_id TEXT,plan_version TEXT,refreshed_at TEXT,plan_json TEXT);'
                'CREATE TABLE sheet_vitrina_v1_calculation_parameter_versions(block_key TEXT,effective_date TEXT,revision INTEGER,created_at TEXT);'
                'CREATE TABLE sheet_vitrina_v1_proxy_v4_parameter_versions(block_key TEXT,effective_date TEXT,revision INTEGER,created_at TEXT);')
            for table,block in [('sheet_vitrina_v1_calculation_parameter_versions',PROXY_BLOCK_KEY),('sheet_vitrina_v1_proxy_v4_parameter_versions',PROXY_V4_BLOCK_KEY)]:
                c.execute('INSERT INTO '+table+' VALUES(?,?,?,?)',(block,'2026-09-01',1,'2026-09-01'))
            c.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?,?,?)',('b','2026-10-16',DAY,'s1','v1','2026-10-17',json.dumps(plan)))
        adapter=WebVitrinaManagementHistoryAdapter()
        req={'runtime_dir':str(self.runtime),'runtime_sha':'a'*40,'source':self.source,'source_sha256':digest(self.source),'dates':[DAY],'last_known_as_of':True}
        with patch.object(adapter,'target',return_value=(self.runtime,db)),patch('apps.web_vitrina_management_history.parameters3',return_value=PARAMS[0]),patch('apps.web_vitrina_management_history.parameters4',return_value=PARAMS[1]):
            pre=adapter.preview(req,'last-known-test')
            wrong=deepcopy(req);wrong['source']['costs_by_date'][DAY]['SKU:1|our_wb_unit_cost_rub']['management_value']='999'
            wrong['source_sha256']=digest(wrong['source'])
            with self.assertRaisesRegex(ValueError,'source-drift'):adapter.preview(wrong,'wrong-cost-test')
            receipt=execute(action='apply',adapter_name='test',operation_id='last-known-test',request=req,expected_prestate=pre['prestate_sha256'],expected_candidate=pre['candidate_sha256'],adapters={'test':adapter})
            self.assertEqual(receipt['state'],'applied')
            self.assertEqual(adapter.readback(req,'last-known-test')['mismatches'],[])
            with patch.object(adapter,'apply',side_effect=AssertionError('duplicate-submit')):
                self.assertEqual(execute(action='apply',adapter_name='test',operation_id='last-known-test',request=req,expected_prestate=pre['prestate_sha256'],expected_candidate=pre['candidate_sha256'],adapters={'test':adapter})['state'],'applied')
            adapter.rollback(req,'last-known-test')
            with sqlite3.connect(db) as c:self.assertEqual(json.loads(c.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0]),plan)


if __name__=='__main__':unittest.main()
