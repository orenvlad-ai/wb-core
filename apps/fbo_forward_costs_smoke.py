#!/usr/bin/env python3
"""Forward FBO cost admission, exact money conservation and idempotent posting."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from decimal import Decimal as D
import json
import sqlite3
from tempfile import TemporaryDirectory
from packages.application import fbo_forward_costs as f
from packages.application.warehouse_functional import WarehouseLine, _supply_downstream_component_index, reconcile_discrepancies, roll_periodic_wac
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _connect
from packages.application.fulfillment_services import FulfillmentServicesBlock
from apps.sheet_vitrina_v1_fulfillment_services_smoke import _wb_supply_row,_build_workbook,_valid_row,_storage_row

NOW='2026-11-09T10:00:00Z'

def seed_dispatch(conn,supply_id,composition):
    conn.execute("CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_fbo_supply_lifecycle_current(cache_key TEXT PRIMARY KEY,supply_id TEXT,admitted_at TEXT,admission TEXT,state_json TEXT)")
    frozen=[{'nm_id':int(nm),'quantity_delta':-qty,'capital_delta_rub':str(-qty*10),'wac_rub':'10'} for nm,qty in composition.items()]
    saved={'operation_id':'frozen-dispatch-'+supply_id,'composition':composition,'frozen_costs':frozen}
    conn.execute('INSERT INTO sheet_vitrina_v1_fbo_supply_lifecycle_current VALUES(?,?,?,?,?)',('supply:'+supply_id,supply_id,NOW,'new_source',json.dumps(saved)))


def lines():
    return [WarehouseLine('wb',1,D(40),D(4000),D(40),'confirmed',{},wb_quantity=D(30),wb_in_way_to_client=D(7),wb_in_way_from_client=D(3))]

def state(conn):
    return {'effects':[dict(r) for r in conn.execute(f'SELECT * FROM {f.EFFECTS} ORDER BY effect_id')],
            'order_boundaries':{'1':{'status':'ready','business_date':NOW[:10],'order_count':'5','unit_cost_rub':'100'}}}

def snapshot():
    return {'snapshot_id':'wb-current-1','requested_nm_ids':[1,2],
            'items':[{'nm_id':1,'quantity':'30','in_way_to_client':'7','in_way_from_client':'3'},
                     {'nm_id':2,'quantity':'0','in_way_to_client':'0','in_way_from_client':'0'}]}

def test_late_invoice():
    with TemporaryDirectory() as raw:
        rt=RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
        supply=_wb_supply_row('1001',accepted_quantity=80,quantity_added=100,cost_total=200)
        supply['raw_goods']=[{'nmID':1,'quantity':60,'acceptedQuantity':50},{'nmID':2,'quantity':40,'acceptedQuantity':30}]
        rt.save_wb_supply_rows(rows=[supply],warehouses=[{'ID':777,'name':'WB'}],synced_at=NOW)
        block=FulfillmentServicesBlock(runtime=rt,timestamp_factory=lambda:NOW)
        before=block.upload_xlsx(_build_workbook([_valid_row('1001')]))
        baseline=block.approved_overlay_by_supply()
        with _connect(rt.db_path) as conn:
            seed_dispatch(conn,'1001',{'1':60,'2':40})
            f.activate(conn,NOW)
            assert f.activate(conn,NOW)['status']=='already_active'
            assert conn.execute(f'SELECT COUNT(*) FROM {f.EFFECTS}').fetchone()[0]==0
        # WB's later mutable goods now differ from the immutable dispatch.
        supply['raw_goods'][0]['quantity']=20
        supply['raw_goods'][1]['quantity']=80
        rt.save_wb_supply_rows(rows=[supply],warehouses=[],synced_at=NOW)
        late=block.upload_xlsx(_build_workbook([_valid_row('1001'),_storage_row()]))
        upload_id=late['upload']['upload_id']
        assert block.approved_overlay_by_supply()==baseline,'late invoice must not enter old accepted layer'
        with _connect(rt.db_path) as conn:
            effects=state(conn)['effects']
            assert sorted(D(r['amount_rub']) for r in effects)==[D(840),D(1260)],effects
            assert sum(D(r['amount_rub']) for r in effects)==D(2100),'full invoice, not 80% accepted'
            revision=effects[0]['source_revision']
            f.route_request(conn,upload_id,revision,NOW)
            assert conn.execute(f'SELECT COUNT(*) FROM {f.EFFECTS}').fetchone()[0]==2
            # Missing today operation boundary blocks positive stock; zero is terminal.
            blocked,updates=f.apply_pending(lines(),{'effects':effects},snapshot=snapshot(),posted_at=NOW)
            assert blocked==lines() and [r['status'] for r in updates]==['unallocated']
            valued,updates=f.apply_pending(lines(),state(conn),snapshot=snapshot(),posted_at=NOW)
            assert valued[0].quantity==D(40) and valued[0].capital==D(5260) and valued[0].wac==D('131.5')
            assert sorted(r['status'] for r in updates)==['posted','unallocated']
            saved=json.loads(next(r for r in updates if r['status']=='posted')['snapshot_json'])
            assert D(saved['wb_quantity'])==40 and D(saved['before_capital_rub'])==4000 and D(saved['after_capital_rub'])==5260
            conn.execute('SAVEPOINT rollback_money')
            f.commit_updates(conn,updates)
            conn.execute('ROLLBACK TO rollback_money')
            assert all(r['status']=='pending' for r in state(conn)['effects'])
            f.commit_updates(conn,updates)
            posted=f.load_posted_effects(conn)
            assert len(posted)==1 and posted[0]['snapshot']['order_boundary']['order_count']=='5'
            valued2,updates2=f.apply_pending(valued,state(conn),snapshot=snapshot(),posted_at=NOW)
            assert valued2==valued and updates2==[],'repeat cannot add money again'
            # A later receipt of zero-stock SKU does not consume its old unallocated fee.
            future=snapshot();future['items'][1]['quantity']='10'
            future_lines=valued+[WarehouseLine('wb',2,D(10),D(1000),D(10),'confirmed',{})]
            assert f.apply_pending(future_lines,state(conn),snapshot=future,posted_at=NOW)==(future_lines,[])
        block.delete_upload(upload_id)
        with _connect(rt.db_path) as conn:
            negative=[dict(r) for r in conn.execute(f"SELECT * FROM {f.EFFECTS} WHERE status='pending'")]
            assert sum(D(r['amount_rub']) for r in negative)==D(-2100), negative
            assert all(r['reason']=='negative_revision_requires_review' for r in negative)
        assert block.approved_overlay_by_supply()==baseline,'deleting late document cannot retroactively alter base'
        # Deleting an OLD accepted invoice also freezes the original overlay.
        block.delete_upload(before['upload']['upload_id'])
        assert block.approved_overlay_by_supply()==baseline


def test_doprinato():
    audit=[]
    balances,unmatched=reconcile_discrepancies(discrepancies=[{'nm_id':1,'quantity':'10','capital':'300'}],
        doprinato=[{'source_id':'new:1','nm_id':1,'quantity':'12','business_date':NOW[:10]}],audit=audit)
    assert balances==[] and unmatched[0]['quantity']=='2'
    events=f.doprinato_events(audit,{'config':{'activated_at':NOW}},posted_at=NOW,business_date=NOW[:10])
    assert len(events)==1 and events[0]['quantity']=='10' and events[0]['capital_rub']=='300'
    # WB's old cost (100) must not replace the matched buffer cost (30).
    assert roll_periodic_wac(quantity=40,capital=4000,quantity_delta=10,capital_delta=events[0]['capital_rub'])[2]==D(86)
    assert roll_periodic_wac(quantity=0,capital=0,quantity_delta=10,capital_delta=events[0]['capital_rub'])[2]==D(30)
    assert f.doprinato_events(audit,{'doprinato_events':events},posted_at=NOW,business_date=NOW[:10])==[]
    old={'baselines':[{'kind':'doprinato','identity':'new:1','payload_json':'{"quantity":"12"}'}]}
    assert f.doprinato_events(audit,old,posted_at=NOW,business_date=NOW[:10])==[]
    # Simultaneous final acceptance removed the discrepancy: no second inbound.
    audit=[]
    reconcile_discrepancies(discrepancies=[],doprinato=[{'source_id':'new:1','nm_id':1,'quantity':'10'}],audit=audit)
    assert f.doprinato_events(audit,{'config':{}},posted_at=NOW,business_date=NOW[:10])==[]


def test_frozen_selected_pool_chain():
    from packages.application.warehouse_functional import replay_ff_cost_pools, compose_supply_costs
    # Two addressed pools were aggregated: selected100@10 plus other100@50.
    # The selected source debit must transfer1000, not the aggregate WAC30.
    pools,outbound=replay_ff_cost_pools(opening_pools={1:{'quantity':D(200),'capital':D(6000),'operations':[]}},
        operations=[{'operation_id':'selected-debit','created_at':NOW,'source_type':'wb_supply','source_object_id':'selected'}],
        lines=[{'operation_id':'selected-debit','nm_id':1,'quantity_delta':'-100','raw':{'cost_snapshot':{
            'unit_cost_rub':'10','capital_delta_rub':'-1000','quality':'exact_selected_fbo_pool',
            'provenance':{'contract':'fbo_supply_lifecycle_v1','facility_id':'selected','pool':'FBO'}}}}],
        supplier_flow_costs={},cost_map={},boundary='2026-11-01T00:00:00Z',expected_quantities={1:D(100)})
    assert pools[1]['capital']==D(5000) and outbound[('selected',1)]==10
    known=_supply_downstream_component_index([{'wb_supply_id':'selected','nm_id':1,
        'transit_cost_status':'unknown','ff_services_per_unit_rub':'2','ff_storage_per_unit_rub':'1',
        'transit_per_unit_rub':None,'wb_acceptance_per_accepted_unit_rub':None}],forward_supply_ids={'selected'})[('selected',1)]
    pre,accepted=compose_supply_costs(outbound_ff_wac=outbound[('selected',1)],
        pre_acceptance_addon=known['pre_acceptance_addon'],acceptance_addon=known['acceptance_addon'])
    assert pre==accepted==13
    #80 accepted carries1040, buffer20 carries260; exact full1300 conserved.
    audit=[]
    balances,_=reconcile_discrepancies(discrepancies=[{'nm_id':1,'quantity':'20','capital':str(20*pre)}],
        doprinato=[{'source_id':'extra:1','nm_id':1,'quantity':'20','business_date':NOW[:10]}],audit=audit)
    events=f.doprinato_events(audit,{'config':{}},posted_at=NOW,business_date=NOW[:10])
    assert balances==[] and D(events[0]['capital_rub'])==260
    q,c,w=roll_periodic_wac(quantity=80,capital=80*accepted,quantity_delta=events[0]['quantity'],capital_delta=events[0]['capital_rub'])
    assert (q,c,w)==(D(100),D(1300),D(13))
    # Later WB folds this additional receipt into the original acceptance.
    audit=[]
    reconcile_discrepancies(discrepancies=[],doprinato=[{'source_id':'extra:1','nm_id':1,'quantity':'20'}],audit=audit)
    ordinary=[{'nm_id':1,'quantity':'20','capital_rub':'260'}]
    offset=f.doprinato_events(audit,{'doprinato_events':events},posted_at=NOW,business_date=NOW[:10],accepted_events=ordinary)
    assert len(offset)==1 and D(offset[0]['quantity'])==-20 and D(offset[0]['capital_rub'])==-260
    assert sum(D(e['quantity']) for e in ordinary+offset)==0 and sum(D(e['capital_rub']) for e in ordinary+offset)==0


def test_semantic_invoice_duplicate():
    from io import BytesIO
    from zipfile import ZipFile
    with TemporaryDirectory() as raw:
        rt=RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
        supply=_wb_supply_row('1001',accepted_quantity=10,quantity_added=10,cost_total=0)
        rt.save_wb_supply_rows(rows=[supply],warehouses=[],synced_at=NOW)
        block=FulfillmentServicesBlock(runtime=rt,timestamp_factory=lambda:NOW)
        with _connect(rt.db_path) as conn:
            seed_dispatch(conn,'1001',{'1':10});f.activate(conn,NOW)
        payload=_build_workbook([_valid_row('1001')])
        first=block.upload_xlsx(payload)
        altered=BytesIO(payload)
        with ZipFile(altered,'a') as archive:archive.comment=b'same invoice re-saved by vendor'
        second=block.upload_xlsx(altered.getvalue())
        assert first['upload']['upload_id']!=second['upload']['upload_id']
        with _connect(rt.db_path) as conn:
            effects=state(conn)['effects']
            assert len(effects)==2
            assert sum(r['reason']=='semantic_invoice_duplicate_requires_review' for r in effects)==1
            valued,updates=f.apply_pending(lines(),state(conn),snapshot=snapshot(),posted_at=NOW)
            assert len(updates)==1 and valued[0].capital==D(5575),'different XLSX bytes must not charge identical invoice twice'


def test_mixed_invoice():
    with TemporaryDirectory() as raw:
        rt=RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
        one=_wb_supply_row('1001',accepted_quantity=10,quantity_added=10,cost_total=0)
        two=_wb_supply_row('1002',accepted_quantity=0,quantity_added=20,cost_total=0)
        two['status_id']=3
        rt.save_wb_supply_rows(rows=[one,two],warehouses=[],synced_at=NOW)
        block=FulfillmentServicesBlock(runtime=rt,timestamp_factory=lambda:NOW)
        with _connect(rt.db_path) as conn:
            seed_dispatch(conn,'1001',{'1':10})
            f.activate(conn,NOW)
        doc=block.upload_xlsx(_build_workbook([_valid_row('1001'),_valid_row('1002'),_storage_row()]))
        overlay=block.approved_overlay_by_supply()
        assert '1001' not in overlay and D(str(overlay['1002']['amount_with_vat_total']))==1890
        with _connect(rt.db_path) as conn:
            effects=[dict(r) for r in conn.execute(f'SELECT * FROM {f.EFFECTS}')]
            assert len(effects)==1 and D(effects[0]['amount_rub'])==1785
            assert f.notification_items(conn)[0]['type']=='fbo_cost_unallocated'
            # The original ordinary preacceptance expense is retained on retry
            # after acceptance: it must not become a second late charge.
        two['status_id']=5;two['accepted_quantity']=20;two['raw_goods'][0]['acceptedQuantity']=20
        rt.save_wb_supply_rows(rows=[two],warehouses=[],synced_at=NOW)
        with _connect(rt.db_path) as conn:
            f.route_request(conn,doc['upload']['upload_id'],doc['upload']['file_sha256'],NOW)
            assert conn.execute(f'SELECT COUNT(*) FROM {f.EFFECTS}').fetchone()[0]==1


def test_independent_known_fees():
    row={'wb_supply_id':'forward','nm_id':1,'transit_cost_status':'unknown',
         'ff_services_per_unit_rub':'15','ff_storage_per_unit_rub':'3',
         'transit_per_unit_rub':None,'wb_acceptance_per_accepted_unit_rub':None}
    assert _supply_downstream_component_index([row])=={},'inactive historical baseline unchanged'
    known=_supply_downstream_component_index([row],forward_supply_ids={'forward'})[('forward',1)]
    assert known['pre_acceptance_addon']==D(18) and known['acceptance_addon']==0
    assert known['missing_cost_components']==['wb_transit','wb_paid_acceptance']


def test_real_warehouse_publication():
    from unittest.mock import patch
    from types import SimpleNamespace
    from apps.fulfillment_recalc_intents_smoke import fixture
    from apps.warehouse_targeted_replay_smoke import _seed_functional
    from packages.application.canonical_cost_engine import CanonicalCostEngine
    from packages.application.our_wb_costs import OurWbCostBlock
    from packages.application.warehouse_functional import WarehouseFunctionalBlock
    stamp='2026-07-06T08:00:00Z'
    boundary=SimpleNamespace(capture_order_boundary=lambda conn,nm:{'status':'ready','business_date':stamp[:10],'order_count':'2','unit_cost_rub':'30'})
    with TemporaryDirectory() as raw, patch.dict(sys.modules,{'packages.application.fbo_forward_cost_consumers':boundary}):
        rt,upload=fixture(raw)
        _seed_functional(rt)
        CanonicalCostEngine(runtime=rt)
        with _connect(rt.db_path) as conn:
            conn.execute('DELETE FROM sheet_vitrina_v1_warehouse_functional_balances')
            conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id<>'base'")
            conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id<>'base'")
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES('base','ff',1,'30','10','300','30','opening',0,'0','0','0','{}')")
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_versions SET version_kind='functional_cutover',effective_at=?,business_effective_date='2026-07-01' WHERE version_id='base'",(stamp,))
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_cutovers SET cutover_at='2026-07-01T00:00:00Z'")
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_snapshots SET snapshot_date='2026-07-06',fetched_at=?,requested_nm_ids_json='[1]',raw_rows_json='[]',items_json=?,raw_row_count=0",(stamp,json.dumps([{'nm_id':1,'quantity':'30'}])))
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_opening_cost_map SELECT cutover_id,1,'10','10','direct_24_06','{}','fixture',? FROM sheet_vitrina_v1_warehouse_functional_cutovers",(stamp,))
            conn.execute("INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions VALUES('fixture',1,'2026-07-01','fixture','2026-07-01','0',0,'0',0,0,'fixture','{}',1,?,NULL)",(stamp,))
        for operation_id,supply_id,delta in [('opening','',30),('debit-1001','1001',-10),('debit-1002','1002',-20)]:
            rt.create_ff_stock_operation(operation_id=operation_id,operation_type='manual_receipt' if delta>0 else 'auto_writeoff',
                source_type='manual_excel' if delta>0 else 'wb_supply',source_key=operation_id,source_object_id=supply_id,
                created_at='2026-07-01T00:00:00Z' if delta>0 else '2026-07-05T00:00:00Z',lines=[{'nm_id':1,'quantity_delta':delta}])
        costs=OurWbCostBlock(runtime=rt,timestamp_factory=lambda:stamp)
        costs.materialize_wb_supply_cost_layers()
        warehouse=WarehouseFunctionalBlock(runtime=rt,timestamp_factory=lambda:stamp)
        first=warehouse.build_targeted_recovery_plan(affected_nm_ids=[1],stable_source_ids=['fixture'])
        initial=warehouse.apply_plan(first,confirm_fingerprint=first['plan_fingerprint'])
        old=next(r for r in initial['balances'] if r['warehouse_key']=='wb')
        assert D(old['capital_rub'])==900
        with _connect(rt.db_path) as conn:
            historical=[tuple(r) for r in conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE as_of_date<'2026-07-06' ORDER BY as_of_date,nm_id")]
            f.activate(conn,stamp)
        late=upload.upload_xlsx(_build_workbook([_valid_row('1001'),_storage_row()]))
        costs.materialize_wb_supply_cost_layers()
        # A complete captured WB source avoids network while running the real
        # warehouse calculation, optimistic source gate, transaction and readback.
        plan=warehouse._build_plan(kind='hourly_wb_sync',wb_payload=warehouse._last_good_wb_payload())
        assert len(plan['fbo_forward_cost_updates'])==1
        published=warehouse.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'])
        current=next(r for r in published['balances'] if r['warehouse_key']=='wb')
        assert D(current['quantity'])==30 and D(current['capital_rub'])==3000,current
        with _connect(rt.db_path) as conn:
            assert [tuple(r) for r in conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE as_of_date<'2026-07-06' ORDER BY as_of_date,nm_id")]==historical,'closed rows must be byte-for-byte unchanged'
            assert len(f.load_posted_effects(conn))==1
            event=conn.execute("SELECT quantity,capital_rub FROM sheet_vitrina_v1_warehouse_functional_events WHERE event_type='wb_late_fulfillment_cost'").fetchone()
            assert D(event[0])==0 and D(event[1])==2100
        repeated=warehouse._build_plan(kind='hourly_wb_sync',wb_payload=warehouse._last_good_wb_payload())
        assert repeated['fbo_forward_cost_updates']==[]
        again=warehouse.apply_plan(repeated,confirm_fingerprint=repeated['plan_fingerprint'])
        assert D(next(r for r in again['balances'] if r['warehouse_key']=='wb')['capital_rub'])==3000
        print('real activated warehouse apply: 900+2100=3000; historical bytes unchanged; repeated sync capital unchanged')


if __name__=='__main__':
    test_independent_known_fees();test_doprinato();test_frozen_selected_pool_chain();test_mixed_invoice();test_semantic_invoice_duplicate();test_late_invoice();test_real_warehouse_publication()
    print('FBO forward costs: known fees independent; matched buffer capital; full invoice/current contour; zero stock terminal; repeat/corrections/legacy baseline protected: OK')
