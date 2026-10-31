"""Dated source selection, missing-only mask, source CAS and readback/rollback."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_official_fbs_smoke import fixture, zero_wb_snapshot
from apps.fbs_quantity_publication import FbsQuantityPublicationAdapter, capture_source, project
from apps.production_apply_launcher import execute
from packages.application.web_vitrina_management_history import digest
from packages.application.official_fbs_stock_read import read_complete_official_fbs_stock


def check_selected_policy_source_read():
    from apps.fbs_current_snapshot_policy_smoke import canonical, select
    from packages.application.fbs_current_snapshot_policy import open_current_snapshot_readonly
    with TemporaryDirectory() as tmp:
        root=Path(tmp).resolve();db=root/'generations'/'op.sqlite3';db.parent.mkdir()
        c=fixture(db);c.row_factory=sqlite3.Row
        zero_wb_snapshot(c)
        c.execute('CREATE TABLE registry_upload_current_state(slot,bundle_version)')
        c.execute("INSERT INTO registry_upload_current_state VALUES(1,'b')")
        c.execute('CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version,as_of_date,snapshot_id,plan_json)')
        day='2026-09-05';now=datetime(2026,9,5,12,5,tzinfo=timezone.utc)
        plan={'date_columns':[day],'metadata':{},'sheets':[{'sheet_name':'DATA_VITRINA','header':['label','key',day],
            'rows':[['x','SKU:1|inventory_fbs_total_qty_v1',''],['x','SKU:2|inventory_fbs_total_qty_v1','']]}]}
        c.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?)',('b',day,'s',json.dumps(plan)));c.commit()
        canonical(root,db,c);select(root)
        # Even at the canonical path, an unproven plain connection keeps30m.
        try:capture_source(c,dates=[day],bundle_version='b',nm_ids=[1,2],now=now)
        except ValueError as exc:assert 'not_fresh' in str(exc)
        else:raise AssertionError('raw connection gained selected freshness')
        with closing(open_current_snapshot_readonly(db)) as source_conn:
            source_conn.execute('BEGIN')
            source=capture_source(source_conn,dates=[day],bundle_version='b',nm_ids=[1,2],now=now)
        adapter=FbsQuantityPublicationAdapter();adapter.target=lambda request:(root,db)
        request=dict(source=source,dates=[day],source_sha256=digest(source),runtime_sha='test')
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return now
        before=c.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0]
        with patch('apps.fbs_quantity_publication.datetime',Clock):
            c.execute('BEGIN IMMEDIATE')
            candidate=adapter.build(request,'selected-quantity',c)
            assert len(candidate['changes'])==2 and c.in_transaction
            assert c.execute('PRAGMA query_only').fetchone()[0]==0  # caller ownership unchanged
            assert c.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0]==before
            c.rollback()
            # Exercise the inherited actual write path, where the writer itself
            # is plain sqlite but source admission must retain the selected age.
            p=execute(action='preview',adapter_name='test',operation_id='selected-quantity',request=request,adapters={'test':adapter})
            result=execute(action='apply',adapter_name='test',operation_id='selected-quantity',request=request,adapters={'test':adapter},
                expected_prestate=p['prestate_sha256'],expected_candidate=p['candidate_sha256'])
            assert result['state']=='applied'
        c.close()


def check_request_json_roundtrip_candidate_order():
    with TemporaryDirectory() as tmp:
        root=Path(tmp).resolve();db=root/'fixture.sqlite3'
        c=fixture(db);c.row_factory=sqlite3.Row
        zero_wb_snapshot(c)
        c.execute('CREATE TABLE registry_upload_current_state(slot,bundle_version)')
        c.execute("INSERT INTO registry_upload_current_state VALUES(1,'b')")
        c.execute('CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version,as_of_date,snapshot_id,plan_json)')
        days=['2026-09-05','2026-09-06']
        source=capture_source(c,dates=[days[0]],bundle_version='b',nm_ids=[1,2],now=datetime(2026,9,7,10,5,tzinfo=timezone.utc))
        observation=source['observations'][days[0]]
        later=json.loads(json.dumps(observation).replace(days[0],days[1]))
        # Collector visits newest generations first; the private JSON writer
        # sorts keys. Both request representations must bind one candidate.
        source['dates']=days
        source['observations']={days[1]:later,days[0]:observation}
        source['snapshot_id']='roundtrip-fixture'
        plan={'date_columns':days,'metadata':{},'sheets':[{'sheet_name':'DATA_VITRINA',
            'header':['label','key',*days],'rows':[['x',key,'',''] for key in (
                'SKU:1|inventory_fbs_total_qty_v1','SKU:2|inventory_fbs_total_qty_v1',
                'TOTAL|total_inventory_fbs_total_qty_v1','TOTAL|total_inventory_wb_total_qty_v1',
                'TOTAL|total_stock_total','SKU:1|our_wb_unit_cost_rub')]}]}
        c.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?)',('b',days[0],'s',json.dumps(plan)));c.commit()
        request=dict(source=source,dates=days,source_sha256=digest(source),runtime_sha='fixture')
        reloaded=json.loads(json.dumps(request,sort_keys=True))
        assert list(request['source']['observations'])==list(reversed(days))
        assert list(reloaded['source']['observations'])==days
        adapter=FbsQuantityPublicationAdapter();adapter.target=lambda request:(root,db)
        # Source acquisition is tested separately; isolate candidate ordering
        # while still exercising real build, full plan serialization and digest.
        with patch('apps.fbs_quantity_publication.capture_source',return_value=source):
            before=adapter.build(request,'roundtrip-operation',c)
            after=adapter.build(reloaded,'roundtrip-operation',c)
        assert before==after and digest(before)==digest(after)
        assert [change['date'] for change in before['changes']]==sorted(change['date'] for change in before['changes'])
        assert len(before['changes'])==10
        assert c.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0]==json.dumps(plan)
        c.close()


def main():
    check_request_json_roundtrip_candidate_order()
    check_selected_policy_source_read()
    with TemporaryDirectory() as tmp:
        root = Path(tmp); db = root / 'fixture.sqlite3'
        c = fixture(db); c.row_factory = sqlite3.Row
        zero_wb_snapshot(c)
        now = datetime(2026, 9, 7, 10, 5, tzinfo=timezone.utc)
        # A newer generation exists, but historical selection must stay on g1.
        c.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_warehouse_registry_runs "
                  "SELECT 'g2',2,status,complete,policy_version,catalog_scope_json,warehouse_scope_json,'new',"
                  "'2026-09-06T10:00:00Z','2026-09-06T10:01:00Z' FROM sheet_vitrina_v1_wb_fbs_warehouse_registry_runs WHERE run_id='g1'")
        c.execute('CREATE TABLE registry_upload_current_state(slot,bundle_version)')
        c.execute("INSERT INTO registry_upload_current_state VALUES(1,'b')")
        c.execute('CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version,as_of_date,snapshot_id,plan_json)')
        day = '2026-09-05'
        keys = ['SKU:1|inventory_fbs_total_qty_v1','SKU:2|inventory_fbs_total_qty_v1',
                'TOTAL|total_inventory_fbs_total_qty_v1','SKU:1|inventory_fbs_facility_available_qty_v1:A',
                'SKU:1|our_wb_unit_cost_rub','TOTAL|total_proxy_profit_4_rub','SKU:2|stock_total',
                'TOTAL|total_inventory_wb_total_qty_v1','TOTAL|total_stock_total']
        plan = {'date_columns':[day], 'metadata':{}, 'sheets':[{'sheet_name':'DATA_VITRINA',
            'header':['label','key',day], 'rows':[['x',k,3 if k.endswith(':A') else ''] for k in keys]}]}
        c.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?)',('b',day,'s',json.dumps(plan)))
        c.commit()
        source = capture_source(c, dates=[day], bundle_version='b', nm_ids=[1,2], now=now)
        assert source['observations'][day]['generation_id']=='g1'
        revised, changes = project(plan,source=source,operation_id='test-fbs-quantity')
        values={r[1]:r[2] for r in revised['sheets'][0]['rows']}
        assert values[keys[0]]==8 and values[keys[1]]==0 and values[keys[2]]==8
        assert values[keys[3]]==3 and all(values[k]=='' for k in keys[4:6])
        assert values[keys[6]]==0 and values[keys[7]]==0 and values[keys[8]]==8
        assert len(changes)==6 and project(revised,source=source,operation_id='again')[1]==[]
        mixed=deepcopy(plan);mixed['sheets'][0]['rows'][0][2]=3
        try:
            project(mixed,source=source,operation_id='mixed')
            raise AssertionError('inconsistent SKU and TOTAL admitted')
        except ValueError as exc: assert 'conflicts' in str(exc)
        try:
            read_complete_official_fbs_stock(c,universe=[1,2],day='2026-09-06',now=now,generation_id='g1')
            raise AssertionError('cross-date source admitted')
        except ValueError: pass
        adapter=FbsQuantityPublicationAdapter();adapter.target=lambda request:(root,db)
        request={'source':source,'dates':[day],'source_sha256':digest(source),'runtime_sha':'test'}
        adapters={'test':adapter};op='test-fbs-quantity'
        preview=execute(action='preview',adapter_name='test',operation_id=op,request=request,adapters=adapters)
        result=execute(action='apply',adapter_name='test',operation_id=op,request=request,adapters=adapters,
            expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        assert result['state']=='applied' and result['readback']['verified_cells']==6
        saved=c.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0]
        corrupt=json.loads(saved)
        corrupt['metadata']['server_cell_presentation'][keys[0]][day]['source_digest']='wrong'
        c.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(json.dumps(corrupt),));c.commit()
        assert adapter.readback(request,op)['state']=='ambiguous'
        c.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(saved,));c.commit()
        adapter.rollback(request,op)
        assert json.loads(c.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0])==plan
        # The exact source is re-derived before apply; changing an amount is refused.
        c.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SET amount=4 WHERE run_id='A' AND nm_id=1")
        c.commit()
        try:
            adapter.build(request,'test-fbs-drift',c)
            raise AssertionError('source drift admitted')
        except ValueError as exc: assert 'source-drift' in str(exc)
        c.close()
    print('fbs_quantity_publication_smoke: OK')


if __name__=='__main__': main()
