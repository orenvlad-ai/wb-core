#!/usr/bin/env python3
"""FF, capital, direct cost consumers and read-only source boundary checks."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.shared_sku_cost_smoke import fbs, wb
from packages.application.fbs_snapshot_cost import fingerprint, evaluate_candidate
from apps.fbs_snapshot_cost_smoke import capture
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot, SOURCE, COST, capture_retained_stages
from packages.application.web_vitrina_management_history import recalculate_current_rows, TARGET_METRICS
from packages.application.calculation_parameters import DEFAULT_PROXY_PARAMETERS
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow

DAY = "2026-09-07"


def retained(source=None):
    source = source or wb()
    rows = {}
    for row in source['rows']:
        stages = {s: dict(quantity='10', capital_rub='500', wac_rub='50')
                  for s in ('PRODUCTION', 'PRODUCTION_TO_FF', 'FF_TO_WB', 'WB_ACCEPTANCE_DISCREPANCY')}
        stages['WB'] = dict(quantity=row['quantity'], capital_rub=row['capital_rub'], wac_rub='200')
        rows[str(row['nm_id'])] = {'stages': stages, 'identity': {'name':'Товар', 'sku':'sku-1', 'barcode':'100001'}}
    result = {'date': source['business_date'], 'wb_version_id':source['version_id'], 'rows':rows}
    result['source_digest'] = fingerprint(result)
    return result


def snapshot(state=None, source=None, stages=None, day=DAY):
    source = source or wb(day)
    return FbsInventorySnapshot(fbs_state=state or fbs(), wb_capture=source, retained=stages or retained(source), day=day)


def row(key, value=999, nm=1):
    scope = 'TOTAL' if nm is None else f'SKU:{nm}'
    return WebVitrinaContractRow(row_id=scope+'|'+key, row_order=1, scope_kind='TOTAL' if nm is None else 'SKU',
        scope_key=scope, scope_label='', metric_key=key, metric_label='', row_last_updated_at='', section='', group=None,
        nm_id=nm, format=None, values_by_date={DAY:value, '2026-09-06':987})


class PresentationTests(unittest.TestCase):
    def test_source_replacement_and_all_sums(self):
        s=snapshot();p=s.payload();ff=s.warehouse_detail()
        self.assertEqual(ff['warehouse']['total_quantity'],'1500')
        self.assertEqual(ff['warehouse']['total_capital_rub'],'250000')
        self.assertEqual(p['totals']['total']['quantity'],'2040')
        self.assertEqual(p['totals']['total']['capital_rub'],'352000')
        self.assertEqual(s.metrics()["total_"+COST],'175')
        self.assertEqual(s.metrics(1)[COST],'175')
        self.assertEqual(ff['warehouse']['snapshot_inventory']['fbs_quantity'],'1000')
        self.assertEqual(next(r['value'] for r in s.planning_payload()['metrics'] if r['metric_key']=='fbs_total'),1000)
        self.assertNotIn('ff_reservations', ff['warehouse'])
        self.assertIsNone(ff['balances'][0]['physical_quantity'])
        self.assertEqual(ff['version_id'],s.planning_payload()['version_id'])

    def test_weighted_locations_not_average_of_prices(self):
        state=fbs(fbo_quantity='0')
        state['baseline']['rows']['ff-2:1'] = dict(nm_id=1, facility_id='ff-2',quantity='100',capital_rub='50000',wac_rub='500')
        state['baseline']['snapshot']['rows'].append(dict(nm_id=1, facility_id='ff-2',quantity='100'))
        s=snapshot(state)
        self.assertEqual(s.metrics(1)['own_capital_FF_capital_rub'],'150000')
        self.assertEqual(s.metrics(1)['own_capital_FF_qty'],'1100')
        self.assertEqual(Decimal(s.metrics(1)['own_capital_FF_unit_cost_rub']).quantize(Decimal('.01')),Decimal('136.36'))

    def test_rows_replace_current_only_and_leave_archives(self):
        s=snapshot();rows=[row(COST),row('own_capital_FF_qty'),row('own_total_product_capital_rub'),row('unrelated'),row('own_total_paid_equivalent_qty')]
        actual=s.apply_rows(rows,business_date=DAY)
        self.assertEqual([r.values_by_date[DAY] for r in actual[:5]],[175,1500,352000,999,999])
        self.assertTrue(all(r.values_by_date['2026-09-06']==987 for r in actual[:5]))
        self.assertEqual(actual[1].presentation_by_date[DAY]['source'],SOURCE)
        self.assertTrue(actual[1].presentation_by_date[DAY]['candidate_only'])
        with self.assertRaisesRegex(ValueError,'exact_date'):
            s.apply_rows(rows,business_date='2026-09-08')

    def test_zero_and_missing_cost_never_make_old_capital_valid(self):
        s=snapshot(fbs(quantity='0',fbo_quantity='0'),wb(q='0',capital='0'))
        self.assertEqual(s.metrics(1)['own_capital_FF_capital_rub'],'0')
        self.assertIsNone(s.metrics(1)[COST])
        state=evaluate_candidate(fbs(),capture('2026-09-08'))
        state['pending_documents']=[{'document_id':'late','reason':'unresolved'}]
        s=snapshot(state,wb('2026-09-08'),day='2026-09-08')
        self.assertIsNone(s.warehouse_detail()['warehouse']['total_capital_rub'])
        self.assertEqual(s.warehouse_detail()['warehouse']['snapshot_inventory']['fbs_quantity'],'1000')
        self.assertIsNone(s.metrics(1)['own_total_product_capital_rub'])
        self.assertIsNone(s.metrics(1)[COST])

    def test_missing_wb_valuation_preserves_quantity_and_other_sku(self):
        state = fbs(fbo_quantity="0")
        state["baseline"]["rows"]["ff-1:2"] = dict(nm_id=2, facility_id="ff-1", quantity="10",
            wac_rub="900", capital_rub="9000", quality="accepted_initial_cost")
        state["baseline"]["snapshot"]["rows"].append(dict(nm_id=2, facility_id="ff-1", quantity="10"))
        source = wb()
        source.update(complete=False, authority_complete=True)
        source["rows"][0].update(status="missing", reason="wb_cost_coverage_incomplete", capital_rub=None,
                                components=dict(physical=400, to_customer=100, from_customer=0))
        source["rows"].append(dict(nm_id=2, status="available", quantity=0, capital_rub="0", components=dict(physical=0)))
        stages = retained(source)
        # Internal known-only zero must never leak as full capital.
        stages["rows"]["1"]["stages"]["WB"]["capital_rub"] = "0"
        stages["source_digest"] = fingerprint({k:v for k,v in stages.items() if k != "source_digest"})
        snap = snapshot(state, source, stages)
        self.assertEqual(snap.metrics(1)["own_capital_WB_qty"], "500")
        self.assertIsNone(snap.metrics(1)["own_capital_WB_capital_rub"])
        self.assertIsNone(snap.metrics(1)[COST])
        self.assertEqual(snap.metrics(1)["stock_total"], "1400")
        self.assertEqual(snap.metrics(2)[COST], "900")
        self.assertIsNone(snap.metrics()["total_own_total_product_capital_rub"])
        self.assertEqual(snap.metrics()["total_own_total_product_qty"], "1590")
        rows = snap.apply_rows([row(COST), row(COST, nm=2), row("own_capital_WB_qty"), row("unrelated")], business_date=DAY)
        self.assertEqual([r.values_by_date[DAY] for r in rows[:4]], ["", 900, 500, 999])
        self.assertTrue(all(r.values_by_date['2026-09-06'] == 987 for r in rows[:4]))

    def test_default_capital_lookup_keeps_sparse_known_stages_and_unknown_guard(self):
        from apps.warehouse_targeted_replay_smoke import _seed_functional, NOW
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        from packages.application.own_product_capital import OwnProductCapitalBlock
        with TemporaryDirectory() as tmp:
            owner = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
            _seed_functional(owner)
            block = OwnProductCapitalBlock(runtime=owner, timestamp_factory=lambda: NOW)
            implicit = block.load_daily_metric_lookup("2026-07-21")
            explicit = block.load_daily_metric_lookup("2026-07-21", requested_nm_ids=[101])
            self.assertEqual(implicit[101]["own_total_product_qty"], 12)
            self.assertEqual(implicit[101]["own_total_product_capital_rub"], 120)
            self.assertEqual(implicit[101]["own_avg_product_cost_rub"], 10)
            self.assertEqual(explicit[101]["own_total_product_capital_rub"], 120)
            # A present unpriced WB stage, unlike an absent empty stage, must
            # invalidate money in both public forms while preserving quantity.
            with sqlite3.connect(owner.db_path) as conn:
                conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances("
                    "version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,cost_covered_quantity,"
                    "quality,certified,wb_quantity,wb_in_way_to_client,wb_in_way_from_client,provenance_json) "
                    "VALUES('daily-2026-07-21','wb',101,'1',NULL,'0','0','wb_cost_unavailable',0,'0','1','0','{}')")
            for scope in (None, [101]):
                values = block.load_daily_metric_lookup("2026-07-21", requested_nm_ids=scope)[101]
                self.assertEqual(values["own_total_product_qty"], 13)
                self.assertIsNone(values["own_capital_WB_capital_rub"])
                self.assertIsNone(values["own_total_product_capital_rub"])
                self.assertIsNone(values["own_avg_product_cost_rub"])

    def test_functional_publication_money_requires_full_cost_coverage(self):
        from packages.application.warehouse_business_projection import _metric_rows
        from packages.application.own_product_capital import _inventory_cost_stage_evidence
        balances = [dict(nm_id=1, warehouse_key="wb", quantity="5", capital_rub="100",
                         cost_covered_quantity="1", quality="wb_cost_unavailable", certified=False),
                    dict(nm_id=2, warehouse_key="wb", quantity="2", capital_rub="200",
                         cost_covered_quantity="2", quality="periodic_snapshot_wac", certified=False)]
        projected = _metric_rows(balances, affected_nm_ids=[1,2])
        self.assertEqual(projected[1]["metrics"]["own_capital_WB_qty"], 5)
        self.assertIsNone(projected[1]["metrics"]["own_capital_WB_capital_rub"])
        self.assertEqual(projected[2]["metrics"]["own_capital_WB_capital_rub"], 200)
        self.assertEqual(projected[0]["metrics"]["total_own_capital_WB_qty"], 7)
        self.assertIsNone(projected[0]["metrics"]["total_own_capital_WB_capital_rub"])
        self.assertIsNone(projected[0]["metrics"]["total_own_total_product_capital_rub"])
        evidence = _inventory_cost_stage_evidence(balances[0], public_stage="WB")
        self.assertEqual(evidence["quantity"], "5")
        self.assertEqual(evidence["known_capital_rub"], "100")
        self.assertIsNone(evidence["capital_rub"])
        self.assertIsNone(evidence["locations"][0]["capital_rub"])
        self.assertIsNone(evidence["wac_rub"])

    def test_immutable_and_bad_binding(self):
        state=fbs();source=wb();stages=retained(source);s=snapshot(state,source,stages)
        state['baseline']['rows']['ff-1:1']['capital_rub']='1'
        stages['rows']['1']['stages']['WB']['capital_rub']='1'
        read=s.payload();read['totals']['fbs']['quantity']='1'
        self.assertEqual(s.payload()['totals']['fbs']['quantity'],'1000')
        with self.assertRaises(AttributeError):s._json='{}'
        with self.assertRaisesRegex(ValueError,'digest_mismatch'):snapshot(stages=stages)
        stages['source_digest']=fingerprint({k:v for k,v in stages.items() if k!='source_digest'})
        with self.assertRaisesRegex(ValueError,'wb_shared_cost_mismatch'):snapshot(stages=stages)

    def test_unknown_sku_cannot_keep_old_price(self):
        r=snapshot().apply_rows([row(COST,nm=999),row('own_capital_FF_qty',nm=999)],business_date=DAY)
        self.assertTrue(all(x.values_by_date[DAY]=='' for x in r))

    def test_direct_profit_follows_new_price_not_old_numeric_cell(self):
        s=snapshot();rows=[row(COST),row('orderSum',1000),row('orderCount',4),row('ads_sum',10)]
        rows += [row(k,999) for k in TARGET_METRICS if k not in (COST,'total_'+COST) and not k.startswith('total_') and not k.endswith('_total')]
        rows=s.apply_rows(rows,business_date=DAY)
        p4=SimpleNamespace(buyout_rate=Decimal('.8'), included_expense_rate=Decimal('.3'),retained_share=Decimal('.7'),version_id='p4')
        result=recalculate_current_rows(rows,business_date=DAY,parameters=(DEFAULT_PROXY_PARAMETERS,p4),original_presentation={},snapshot_id='fixture')
        values={r.metric_key:r.values_by_date[DAY] for r in result}
        self.assertEqual(values[COST],175)
        self.assertEqual(values['proxy_profit_4_rub'],-10)  # 1000*.8*.7 - 4*.8*175 - 10
        self.assertEqual(values['proxy_margin_per_unit_rub'],-3.125)
        self.assertTrue(all(r.values_by_date['2026-09-06']==987 for r in result if r.metric_key in TARGET_METRICS or r.metric_key in {'orderSum','orderCount','ads_sum'}))
        unavailable=recalculate_current_rows(rows,business_date=DAY,parameters=None,original_presentation={},snapshot_id='fixture')
        self.assertEqual(next(r.values_by_date[DAY] for r in unavailable if r.metric_key=='proxy_profit_4_rub'),'')

    def test_retained_reader_proves_zero_from_complete_document_stage(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.sqlite3'
            c=sqlite3.connect(path)
            c.executescript("""
                CREATE TABLE sheet_vitrina_v1_warehouse_functional_versions(version_id,status,business_effective_date);
                CREATE TABLE sheet_vitrina_v1_warehouse_wb_snapshots(version_id,snapshot_date);
                CREATE TABLE sheet_vitrina_v1_warehouse_business_projection_current_rows(as_of_date,nm_id,revision_id,metrics_json,presentation_json,provenance_json);
                CREATE TABLE sheet_vitrina_v1_warehouse_functional_read_models(version_id,warehouse_key,payload_json,etag);
                CREATE TABLE sheet_vitrina_v1_warehouse_functional_balances(version_id,warehouse_key,nm_id,quantity,capital_rub);
                INSERT INTO sheet_vitrina_v1_warehouse_functional_versions VALUES('v1','good','2026-09-07');
                INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots VALUES('v1','2026-09-07');
            """)
            model={'status':'ready','warehouse':{'total_quantity':'10','total_capital_rub':'100'},
                   'balances':[{'nm_id':1,'quantity':'10','capital_rub':'100'}]}
            c.execute('INSERT INTO sheet_vitrina_v1_warehouse_functional_read_models VALUES(?,?,?,?)',('v1','production',json.dumps(model),'etag1'))
            c.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES('v1','production',1,'10','100')")
            c.commit()
            before=path.read_bytes()
            with patch('packages.application.warehouse_functional._nomenclature_names_from_connection',return_value={}):
                r=capture_retained_stages(path,day=DAY,wb_version_id='v1',nm_ids=[1,2])
                self.assertIsNone(r['rows']['1']['stages']['PRODUCTION']['quantity'])  # Present record must not become zero.
                self.assertEqual(r['rows']['2']['stages']['PRODUCTION']['quantity'],'0')
                self.assertIsNone(r['rows']['2']['stages']['FF_TO_WB']['quantity'])  # No complete stage payload.
                self.assertEqual(before,path.read_bytes())
                model['warehouse']['total_quantity']='20'
                c.execute('UPDATE sheet_vitrina_v1_warehouse_functional_read_models SET payload_json=?',(json.dumps(model),));c.commit()
                r=capture_retained_stages(path,day=DAY,wb_version_id='v1',nm_ids=[2])
                self.assertIsNone(r['rows']['2']['stages']['PRODUCTION']['quantity'])
            c.close()

    def test_http_read_entrypoints_bypass_legacy_only_with_injection(self):
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        from packages.application.warehouse_functional import WarehouseFunctionalBlock
        from packages.application.inventory_planning_read_model import InventoryPlanningReadModel
        entry=object.__new__(RegistryUploadHttpEntrypoint)
        entry.web_vitrina_block=SimpleNamespace(fbs_inventory_snapshot=snapshot())
        entry.warehouse_functional_block=object.__new__(WarehouseFunctionalBlock)
        entry.inventory_planning=InventoryPlanningReadModel(db_path=Path('/does-not-exist'))
        result=entry.handle_warehouse_detail_request('ff')
        self.assertEqual(result['warehouse']['total_quantity'],'1500')
        self.assertEqual(entry.handle_inventory_planning_request()['version_id'],result['version_id'])


if __name__=='__main__':unittest.main()
