#!/usr/bin/env python3
"""Actual FF renderer and Vitrina composition from the active published book."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from apps.sheet_vitrina_v1_inventory_planning_smoke import _seed_inventory_planning
from apps.fbs_snapshot_cost_smoke import capture
from apps.fbs_inventory_presentation_smoke import retained
from packages.application.fbs_snapshot_cost import initialize_candidate
from packages.application.fbs_accounting_runtime import save as save_accounting, SCHEMA
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot
from packages.application.web_vitrina_fbs_lifecycle_last_good import OWNER_POLICY_FILENAME, OWNER_POLICY_SCHEMA


def main():
    fixture=LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
    base=fixture.__enter__()
    try:
        runtime=fixture.entrypoint.runtime
        ids=[int(i.nm_id) for i in runtime.load_current_state().config_v2 if i.enabled]
        day='2026-04-21'
        _seed_inventory_planning(runtime.db_path,nm_ids=tuple(ids[:2]))
        (runtime.runtime_dir/OWNER_POLICY_FILENAME).write_text(json.dumps({
            'schema_version':OWNER_POLICY_SCHEMA,'revision':62,'master_desired':True,
            'processes':{'fbs_shadow':{'desired':True}}}))
        c=capture(day,extra=[dict(nm_id=ids[1],facility_id='ff-1',quantity='0')])
        c['quantity_snapshot']['rows'][0]['nm_id']=ids[0]
        c['baseline_costs']['rows'][0]['nm_id']=ids[0]
        state=initialize_candidate(c)
        wb={'contract':'shared_sku_cost_wb_source_v1','business_date':day,'complete':True,'authority_complete':True,
            'version_id':'wb-test','source_digest':'fixture','rows':[
                dict(nm_id=ids[0],quantity='500',capital_rub='100000',status='available',components={'physical':500}),
                dict(nm_id=ids[1],quantity='0',capital_rub='0',status='available',components={'physical':0})]}
        candidate=FbsInventorySnapshot(fbs_state=state,wb_capture=wb,retained=retained(wb),day=day)
        book = {"schema": SCHEMA, "active": True, "effective_date": day, "state": state,
                "shared_days": {day: build_shared_cost_day(state, wb, day)}, "wb_days": {day: wb},
                "retained_days": {day: retained(wb)}, "presentations": {day: candidate.payload()},
                "prepared_at": c["captured_at"], "source_digest": c["source_digest"]}
        save_accounting(runtime.runtime_dir, book, expected=None, operation_id="browser-activation")
        assert not fixture.entrypoint.web_vitrina_block.fbs_inventory_snapshot.payload()["candidate_only"]
        with patch('packages.application.sheet_vitrina_v1_web_vitrina.build_current_official_fbs_estimate',side_effect=AssertionError('legacy cost')):
            contract=fixture.entrypoint.web_vitrina_block.build(page_route='/sheet-vitrina-v1/vitrina',
                read_route='/v1/sheet-vitrina-v1/web-vitrina',date_from='2026-04-08',date_to=day)
        by_key={r.row_id:r for r in contract.rows}
        assert by_key[f'SKU:{ids[0]}|our_wb_unit_cost_rub'].values_by_date[day] == float(200000/1500)
        assert by_key[f'SKU:{ids[0]}|own_capital_FF_qty'].values_by_date[day] == 1000
        assert by_key['TOTAL|total_own_capital_FF_qty'].values_by_date[day] == 1000
        errors=[]
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            page=browser.new_page(viewport={'width':1440,'height':1080},color_scheme='dark')
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/sheet-vitrina-v1/vitrina?tab=warehouses&warehouse=ff')
            expect(page.locator('[data-warehouse-summary]')).to_contain_text('Доступно FBS по снимку',timeout=15000)
            expect(page.locator('[data-warehouse-summary]')).not_to_contain_text('Физический остаток')
            expect(page.locator('[data-inventory-planning-metrics]')).to_contain_text('1 000')
            expect(page.locator('[data-warehouse-balances]')).to_contain_text('FBS: 1 000')
            page.locator('[data-warehouse-balances] details').first.locator('summary').first.click()
            expect(page.locator('[data-warehouse-balances]')).to_contain_text('snapshot-'+day)
            page.locator('[data-warehouse-balances] details').first.locator('summary').first.click()
            assert not errors,errors
            output=Path(sys.argv[1]) if len(sys.argv)>1 else None
            if output:
                output.parent.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(output),full_page=True)
            browser.close()
        print('FF browser and actual Vitrina composition: PASS')
    finally:
        fixture.__exit__(None,None,None)


if __name__=='__main__':main()
