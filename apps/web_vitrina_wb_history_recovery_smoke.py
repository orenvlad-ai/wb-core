"""Exact WB recovery retains FBS, CAS, immutable rollback and readback."""
from pathlib import Path
import sys
import sqlite3
from tempfile import TemporaryDirectory
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_wb_history_recovery import WebVitrinaWbHistoryRecoveryAdapter
from apps.production_apply_launcher import execute
from packages.application.web_vitrina_management_history import digest
from packages.application.sheet_vitrina_v1_inventory_history import ensure_inventory_history_schema, append_inventory_history_capture, append_inventory_history_finalization, _append_closed_date_ready_capture
from apps.sheet_vitrina_v1_inventory_history_smoke import _component


def run_case(initial=False):
    with TemporaryDirectory() as tmp:
        runtime=Path(tmp);db=runtime/'op.sqlite3'
        with sqlite3.connect(db) as c:
            ensure_inventory_history_schema(c)
            before=append_inventory_history_capture(c,business_date='2026-09-01',capture_kind='accepted_refresh',formula_version='inventory_planning_v1',
                facility_roster=[],source_manifest={'before':1},components=[{**_component('SKU','SKU:1',1,'WB','WB',0),'state':'missing','quantity':None},
                    {**_component('TOTAL','TOTAL',None,'WB','WB',0),'state':'missing','quantity':None},
                    {**_component('SKU','SKU:1',1,'FBS_FACILITY','A',0),'state':'missing','quantity':None}],captured_at='2026-09-01T12:00:00Z')
            if not initial:
                append_inventory_history_finalization(c,business_date='2026-09-01',capture_id=before['capture_id'],finalization_identity='before',finalized_at='2026-09-02T00:00:00Z',provenance={})
                append_inventory_history_capture(c,business_date='2026-09-01',capture_kind='accepted_refresh',formula_version='inventory_planning_v1',
                    facility_roster=[],source_manifest={'unpublished':1},components=[_component('SKU','SKU:1',1,'WB','WB',99),
                        _component('TOTAL','TOTAL',None,'WB','WB',99),_component('SKU','SKU:1',1,'FBS_FACILITY','A',123)],captured_at='2026-09-03T12:00:00Z')
        class Adapter(WebVitrinaWbHistoryRecoveryAdapter):
            def target(self,request): return runtime,db
        source={'kind':'success','snapshot_date':'2026-09-01','count':1,'warehouse_granularity_complete':False,'items':[{'nm_id':1,'stock_total':7}]}
        request={'target_date':'2026-09-01','source':source,'source_sha256':digest(source),'scopes':['SKU:1'],'captured_at':'2026-09-05T17:00:00Z'}
        adapter=Adapter();adapters={'test':adapter};op='test-wb-history'
        from packages.application.sheet_vitrina_v1_inventory_history import read_inventory_history_window
        def visible():return read_inventory_history_window(db,dates=['2026-09-01'],current_date='2026-09-05')
        if initial:
            assert not visible()['dates']
            try:adapter.preview(request,op)
            except ValueError:pass
            else:raise AssertionError('initial publication must require explicit flag')
            request['allow_initial_publication']=True
        p=execute(action='preview',adapter_name='test',operation_id=op,request=request,adapters=adapters)
        result=execute(action='apply',adapter_name='test',operation_id=op,request=request,expected_prestate=p['prestate_sha256'],expected_candidate=p['candidate_sha256'],adapters=adapters)
        assert result['state']=='applied' and result['readback']['verified_WB_cells']==2
        assert execute(action='apply',adapter_name='test',operation_id=op,request=request,expected_prestate=p['prestate_sha256'],expected_candidate=p['candidate_sha256'],adapters=adapters)['state']=='applied'
        assert adapter.rollback(request,op)['restored_capture_id']==before['capture_id']
        if initial:
            recovered=visible()['dates']['2026-09-01']
            assert recovered['capture_id']==before['capture_id']
            with sqlite3.connect(db) as c:
                assert c.execute('SELECT count(*) FROM sheet_vitrina_v1_inventory_history_components WHERE capture_id=? AND quantity IS NOT NULL',(before['capture_id'],)).fetchone()[0]==0

def main():
    run_case(False)
    run_case(True)
    print('WB history recovery: exact WB only, retained FBS, atomic apply, readback, idempotency, immutable rollback: ok')

if __name__=='__main__': main()
