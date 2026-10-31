"""Real GROUP reader keeps dated official quantity independent of valuation."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dataclasses import asdict
from datetime import datetime,timezone
from decimal import Decimal
import unittest
from unittest.mock import patch
from apps import fbs_accounting_degraded_smoke as degraded_fixture
from apps.fbs_accounting_degraded_smoke import make_plan,save,heavy_admitted
from packages.application.web_vitrina_official_fbs import SOURCE,add_official_wb_quantities,is_official_inventory_quantity_metric
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.web_vitrina_window_read_context import window_read_context
from packages.application.calculation_parameters_v4 import ProxyV4ParametersBlock
from packages.application.calculation_parameters import CalculationParametersBlock
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item

class GroupsTest(unittest.TestCase):
    def test_current_full_reader_strict_members_and_nontargets(self):
        fixture=degraded_fixture.DegradedTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        day='2026-09-10';now=datetime(2026,9,10,14,tzinfo=timezone.utc)
        plan=make_plan(as_of_date='2026-09-09',day=day)
        model=dict(available=True,quantity_only=True,date=day,source=SOURCE,
            generation_id='complete-generation',generation_digest='sha256:complete',
            captured_at='2026-09-10T13:59:00Z',functional_version_id='',facilities=['ff-1'],
            skus={1:dict(facilities={'ff-1':Decimal(8)},fbs_quantity=Decimal(8))},
            total=dict(facilities={'ff-1':Decimal(8)},fbs_quantity=Decimal(8)))
        model=add_official_wb_quantities(model,dict(date=day,snapshot_id='wb1',source_digest='sha256:wb1',
            version_id='version1',observed_at='2026-09-10T13:58:00Z',quantity_basis='physical_quantity_only',
            skus={'1':5},total=5))
        with heavy_admitted(fixture.root,operation='cycle'),fixture.scope(),patch(
                'packages.application.web_vitrina_official_fbs.build_current_official_fbs_estimate',return_value=model):
            save(fixture.runtime,plan,now=now)
        # Initialize fixture schemas before entering the real pinned reader.
        with patch('packages.application.sheet_vitrina_v1_web_vitrina.build_current_official_fbs_estimate',return_value=model):
            SheetVitrinaV1WebVitrinaBlock(runtime=fixture.runtime,now_factory=lambda:now).build(
                page_route='/',read_route='/read',as_of_date=plan.as_of_date)
        ProxyV4ParametersBlock(runtime=fixture.runtime);CalculationParametersBlock(runtime=fixture.runtime)
        def read(*, members=(1,), enabled=True, clock=now):
            with window_read_context(fixture.runtime.db_path,runtime_dir=fixture.runtime.runtime_dir),patch(
                    'packages.application.vitrina_catalog.reporting_groups',return_value=[{'group_key':'a','label':'A','display_order':1,'is_active':True}]),patch(
                    'packages.application.vitrina_catalog.reporting_config',return_value=([ConfigV2Item(n,True,str(n),'a',n) for n in members],{})),patch(
                    'packages.application.sheet_vitrina_v1_web_vitrina.build_current_official_fbs_estimate',return_value=model):
                if enabled:
                    return SheetVitrinaV1WebVitrinaBlock(runtime=fixture.runtime,now_factory=lambda:clock,group_blocks=True).build(page_route='/',read_route='/read',as_of_date=plan.as_of_date)
                with patch('packages.application.web_vitrina_official_fbs.restore_official_quantity_groups',side_effect=lambda rows,**kwargs:rows):
                    return SheetVitrinaV1WebVitrinaBlock(runtime=fixture.runtime,now_factory=lambda:clock,group_blocks=True).build(page_route='/',read_route='/read',as_of_date=plan.as_of_date)
        before={r.row_id:r for r in read(enabled=False).rows};after={r.row_id:r for r in read().rows}
        expected={'total_inventory_fbs_total_qty_v1':8,'total_inventory_wb_total_qty_v1':5,'total_stock_total':13}
        for metric,value in expected.items():
            key='GROUP:a|'+metric
            self.assertEqual(before[key].values_by_date[day],'')
            self.assertEqual(after[key].values_by_date[day],value)
            self.assertEqual(after[key].presentation_by_date[day]['member_nm_ids'],[1])
        for key,row in before.items():
            changed=row.scope_kind=='GROUP' and is_official_inventory_quantity_metric(row.metric_key)
            if not changed:self.assertEqual(asdict(after[key]),asdict(row),key)
            else:
                self.assertEqual({k:v for k,v in after[key].values_by_date.items() if k!=day},
                                 {k:v for k,v in row.values_by_date.items() if k!=day})
        group={r.metric_key:r for r in read(members=(1,2)).rows if r.scope_kind=='GROUP'}
        for metric in expected:self.assertEqual(group[metric].values_by_date[day],'')
        historical_clock=datetime(2026,9,11,14,tzinfo=timezone.utc)
        historical_before={r.row_id:r for r in read(enabled=False,clock=historical_clock).rows}
        historical_after={r.row_id:r for r in read(clock=historical_clock).rows}
        for metric,value in expected.items():
            key='GROUP:a|'+metric
            self.assertEqual(historical_before[key].values_by_date[day],'')
            self.assertEqual(historical_after[key].values_by_date[day],value)
            self.assertEqual(historical_after[key].presentation_by_date[day]['source_as_of_date'],day)
        for key,row in historical_before.items():
            if not (row.scope_kind=='GROUP' and is_official_inventory_quantity_metric(row.metric_key)):
                self.assertEqual(asdict(historical_after[key]),asdict(row),key)
        missing_history={r.metric_key:r for r in read(members=(1,2),clock=historical_clock).rows if r.scope_kind=='GROUP'}
        for metric in expected:self.assertEqual(missing_history[metric].values_by_date[day],'')

if __name__=='__main__':unittest.main()
