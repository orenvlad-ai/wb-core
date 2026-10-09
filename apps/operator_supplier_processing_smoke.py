"""Real supplier source/preparation -> functional -> accounting/ready -> Finance.

Disposable native fixtures only. No mocked publisher or downstream proof table.
"""
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json,sqlite3,sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.fulfillment_recalc_intents_smoke import fixture
from apps.warehouse_targeted_replay_smoke import _seed_functional
from apps.wb_finance_weekly_cost_cutover_smoke import _row
from apps.ready_publication_smoke import make_plan,save
from apps.cny_ledger_smoke import _save_payment
from apps.supplier_preparation_intents_smoke import HEADER,LINES
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application import operator_supplier_shipments as source,supplier_preparation_intents as intents,operator_supplier_processing as processing
from packages.application.operator_supplier_cost_proof import read_native_proof
from packages.application.warehouse_functional import WarehouseFunctionalBlock
from packages.application.cny_ledger import CnyLedgerBlock
from packages.application import fbs_accounting_runtime as accounting
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
NOW='2026-07-24T10:00:00Z';MOMENT=datetime.fromisoformat(NOW.replace('Z','+00:00'));DAY=MOMENT.date().isoformat()

def seed(raw):
    rt, block = fixture(raw)
    from packages.application.fulfillment_services import _ensure_schema as ensure_fulfillment
    with _connect(rt.db_path) as conn:ensure_fulfillment(conn);conn.commit()
    # Disposable concurrency contour permits real commits under pinned RO readers.
    with sqlite3.connect(rt.db_path) as conn: conn.execute('PRAGMA journal_mode=WAL')
    bundle = json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text())
    rt.ingest_bundle(bundle, activated_at=NOW)
    _seed_functional(rt)
    from packages.application.canonical_cost_engine import CanonicalCostEngine
    CanonicalCostEngine(runtime=rt)
    with _connect(rt.db_path) as conn:
        conn.execute('DELETE FROM sheet_vitrina_v1_warehouse_functional_balances')
        conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id<>'base'")
        conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id<>'base'")
        conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES('base','ff',1,'30','10','300','30','opening',0,'0','0','0','{}')")
        conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_versions SET version_kind='functional_cutover',effective_at=?,published_at=?,created_at=?,business_effective_date='2026-07-01' WHERE version_id='base'", (NOW,NOW,NOW))
        conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_cutovers SET cutover_at='2026-07-01T00:00:00Z'")
        raw_wb = [{'nmId': 1, 'warehouseId': 1, 'stockCount': 30, 'inWayToClient': 0, 'inWayFromClient': 0}]
        conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_snapshots SET snapshot_date=?,fetched_at=?,requested_nm_ids_json='[1]',raw_rows_json=?,items_json=?,raw_row_count=1,raw_rows_digest=?",
            (DAY, NOW, json.dumps(raw_wb), json.dumps([{'nm_id':1,'quantity':'30','in_way_to_client':'0','in_way_from_client':'0','wb_contour_quantity':'30'}]), source.digest(raw_wb)))
        conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_opening_cost_map SELECT cutover_id,1,'10','10','direct_24_06','{}','fixture',? FROM sheet_vitrina_v1_warehouse_functional_cutovers", (NOW,))
        conn.execute("INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions VALUES('fixture',1,'2026-07-01','fixture','2026-07-01','0',0,'0',0,0,'fixture','{}',1,?,NULL)", (NOW,))
        # Saved official source, real schemas/readers. Zero FBS needs no invented WAC.
        conn.execute('DELETE FROM sheet_vitrina_v1_nomenclature_items')
        conn.execute("INSERT INTO sheet_vitrina_v1_nomenclature_items(item_id,nm_id,is_active,is_hidden,our_sku,vendor_code,barcode,nomenclature_name,product_type,match_key,aliases_json,created_at,updated_at) VALUES('sku1',1,1,0,'SKU1','VC1','BAR1','Fixture','glass','SKU1','[]',?,?)", (NOW,NOW))
        conn.execute("INSERT INTO sheet_vitrina_v1_ff_facilities VALUES('A','A','Fixture',1,'Europe/Moscow',?,?)", (NOW,NOW))
        conn.execute("INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings(mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,created_at,created_by) VALUES('A',1,'A','mapping',1,?,'fixture')", (NOW,))
        catalog = {'complete':True,'requested_chrt_count':1,'active_nm_id_count':1}
        warehouses = {'complete':True,'warehouse_count':1,'warehouses':[{'mapping_id':'A','facility_id':'A','seller_warehouse_id':1}]}
        conn.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_warehouse_registry_runs(run_id,status,complete,started_at,completed_at,warehouse_count,office_count,source_digest,policy_version,catalog_scope_json,warehouse_scope_json,generation_digest) VALUES('g1','success',1,?,?,1,1,'source','complete_catalog_stable_http200_omission_zero_v1',?,?, 'generation')", (NOW,NOW,json.dumps(catalog),json.dumps(warehouses)))
        conn.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_stock_snapshot_runs(run_id,registry_run_id,seller_warehouse_id,status,complete,snapshot_at,requested_chrt_count,returned_chrt_count,identity_scope_json,source_digest,explicit_chrt_count,omitted_zero_count,dense_row_count) VALUES('stock','g1',1,'success',1,?,1,1,'[]','stock-digest',1,0,1)", (NOW,))
        conn.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_stock_snapshot_rows VALUES('stock',1,101,1,0,'evidence','explicit_wb_row')")
        conn.execute("INSERT INTO sheet_vitrina_v1_ff_pool_feature_epochs VALUES(1,1,1,'fixture',?,'{}')", (NOW,))
        conn.execute("INSERT INTO sheet_vitrina_v1_ff_pool_cutover_manifests VALUES('fixture-cutover','fixture','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',?,'2026-07-06',1,'fixture','fixture','fixture',0,'fixture','fixture','fixture','fixture','fixture','opening','fixture',?,'{}')", (NOW,NOW))
        for pool in ('FBS','FBO'):
            conn.execute("INSERT INTO sheet_vitrina_v1_ff_pool_balances VALUES('A',?,1,1,0,'0',NULL,'opening',?)", (pool,NOW))
        conn.commit()
    with _connect(rt.db_path) as conn:
        conn.execute('DELETE FROM sheet_vitrina_v1_wb_supplies');conn.commit()
    return rt


def publish_accounting(rt, *, opening=False):
    prior, _ = accounting.load(rt.runtime_dir)
    stamp = MOMENT if prior is None else datetime.fromisoformat(prior['prepared_at'].replace('Z','+00:00'))+timedelta(seconds=1)
    prepared = accounting.prepare(rt.runtime_dir,now=stamp,opening=opening)
    if opening:
        accounting.save(rt.runtime_dir,prepared[0],expected=prepared[1],operation_id='opening')
        stamp += timedelta(seconds=1)
        prepared = accounting.prepare(rt.runtime_dir,now=stamp)
    try:
        plan = rt.load_sheet_vitrina_ready_snapshot()
    except ValueError:
        plan = make_plan(as_of_date='2026-07-05',day=DAY)
    save(rt,plan,prepared=prepared,now=stamp)
    return accounting.current_publication_receipt(rt,now=stamp)


def fails(function, reason):
    try: function()
    except ValueError as exc: assert reason in str(exc), str(exc)
    else: raise AssertionError('expected '+reason)


def separate_commit(rt):
    # A real different SQLite connection/thread commits while observers live.
    def write():
        with _connect(rt.db_path) as conn:
            conn.execute("UPDATE fixture_observer_noise SET value=value+1");conn.commit()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(write).result(timeout=5)


def prepare_source(rt):
    header={**HEADER,'shipment_id':'source','created_at':NOW,'updated_at':NOW,'invoice_date':DAY,'shipment_date':DAY,'invoice_no':'paid','expenses_complete':0}
    lines=[{**LINES[0],'internal_nm_id':1}]
    rt.save_supplier_shipment(header=header,lines=lines)
    ledger=CnyLedgerBlock(runtime=rt,timestamp_factory=lambda:NOW)
    ledger.create_opening_balance({'operation_date':DAY,'cny_amount':100,'rub_value':1000})
    _save_payment(rt,'fixture-payment','source',NOW,'100')
    ledger.replay_ledger(reason='disposable-native')
    rt.save_supplier_financial_document(document=dict(document_id='logistics',supplier_order_id='source',document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date=DAY,parse_status='confirmed',total_amount_rub=200),expense_lines=[dict(line_id='expense',amount=200,amount_rub=200,currency='RUB',category='domestic_transport',status='confirmed')])
    return header,lines


def edit(rt,header,lines,identity,value):
    body={'request_id':identity,'invoice_no':value}
    return source.execute(rt,action='edit',payload=body,shipment_id='source',actor='alice',request_scope='alice-key',
        write=lambda context:rt.save_supplier_shipment(header={**header,'invoice_no':value},lines=lines,operator_request=context))


def publish_functional(rt):
    with closing(source.readonly(rt.db_path)) as conn:
        pending=[dict(r) for r in conn.execute(f"SELECT * FROM {processing.QUEUE} WHERE status='queued'")]
        last=conn.execute("SELECT MAX(published_at) FROM sheet_vitrina_v1_warehouse_functional_versions").fetchone()[0]
    stamp=(datetime.fromisoformat(last.replace('Z','+00:00'))+timedelta(seconds=1)).isoformat().replace('+00:00','Z')
    native=WarehouseFunctionalBlock(runtime=rt,timestamp_factory=lambda:stamp)
    plan=native.build_targeted_recovery_plan(affected_nm_ids=[1],stable_source_ids=[r['stable_source_id'] for r in pending],targeted_recalc_requests=pending)
    return native.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'])


def main():
    with TemporaryDirectory(prefix='supplier-processing-') as raw:
        rt=seed(raw);header,lines=prepare_source(rt)
        saved=edit(rt,header,lines,'supplier-native-cost-01','paid-new')
        op=saved['acceptance']['operation_id']
        assert saved['acceptance']['state']=='accepted'
        prepared=intents.drain_supplier_preparation_intents(rt,shipment_ids=['source']);assert prepared['status']=='queued',prepared
        # A source-only audit change cannot relabel or strand this exact cost
        # revision. Its immutable operator version remains distinct.
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        entry=RegistryUploadHttpEntrypoint(runtime=rt,runtime_dir=rt.runtime_dir,activated_at_factory=lambda:NOW)
        audit=entry.handle_supplier_shipments_price_check_request('source',{'request_id':'supplier-cost-price-audit'},actor='alice',request_scope='alice-key')
        assert audit['acceptance']['processing']['kind']=='source_only'
        fails(lambda:read_native_proof(rt,op,now=MOMENT),'functional_publication_pending')
        published=publish_functional(rt)
        print('FUNCTIONAL',published['status'],flush=True)
        fails(lambda:read_native_proof(rt,op,now=MOMENT),'accounting')
        publish_accounting(rt,opening=True)
        finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT)
        finance.ensure_schema();finance.ingest_week(date(2026,7,20),date(2026,7,26),[_row(1,DAY,nm_id=1)])
        native=read_native_proof(rt,op,now=MOMENT)
        assert native['status']=='verified'
        assert native['functional']['cost_state']['calculation_available']
        with heavy_admitted(rt.runtime_dir,operation='fixture'),warehouse_functional_job_lock(rt.runtime_dir):
            assert processing.record_completion(rt,op,now=MOMENT)['complete']
        value=source.read_acceptance(rt.db_path,op,request_scope='alice-key')
        assert value['state']=='completed' and value['processing']['complete']
        assert value['processing']['calculation']['quality']=='provisional'
        assert value['processing']['calculation']['expenses_complete'] is False
        print('actual source/intents/functional/accounting/ready/Finance exact completion: OK')
    coalesced_and_fences()
    factual_completion()
    wb_authority_guards()


def coalesced_and_fences():
    with TemporaryDirectory(prefix='supplier-fences-') as raw:
        rt=seed(raw);header,lines=prepare_source(rt)
        old=edit(rt,header,lines,'supplier-coalesced-old','old')['acceptance']['operation_id']
        current=edit(rt,header,lines,'supplier-coalesced-new','new')['acceptance']['operation_id']
        intents.drain_supplier_preparation_intents(rt,shipment_ids=['source'])
        publish_functional(rt);publish_accounting(rt,opening=True)
        finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT)
        finance.ensure_schema();finance.ingest_week(date(2026,7,20),date(2026,7,26),[_row(1,DAY,nm_id=1)])
        fails(lambda:read_native_proof(rt,old,now=MOMENT),'source_changed')
        assert source.read_acceptance(rt.db_path,old,request_scope='alice-key')['state']!='completed'
        read_native_proof(rt,current,now=MOMENT)
        # Actual native WB rematerialization after a source change still cannot
        # prove a publication which consumed the old goods/number operands.
        from apps.sheet_vitrina_v1_fulfillment_services_smoke import _seed_wb_supplies
        from packages.application.our_wb_costs import OurWbCostBlock
        _seed_wb_supplies(rt)
        OurWbCostBlock(runtime=rt,timestamp_factory=lambda:NOW).materialize_wb_supply_cost_layers()
        fails(lambda:read_native_proof(rt,current,now=MOMENT),'native_wb_source_changed')
        with _connect(rt.db_path) as conn:
            conn.execute('DELETE FROM sheet_vitrina_v1_wb_supplies');conn.commit()
        fails(lambda:read_native_proof(rt,current,now=MOMENT),'native_cost_projection_changed')
        with _connect(rt.db_path) as conn:
            conn.execute('DELETE FROM sheet_vitrina_v1_wb_supply_cost_layers');conn.commit()
        read_native_proof(rt,current,now=MOMENT)
        with _connect(rt.db_path) as conn:
            operation=processing.operation(conn,current)
            version=conn.execute(f'SELECT version_id FROM {processing.FUNCTIONAL} WHERE operation_id=?',(current,)).fetchone()[0]
            original_capital=conn.execute("SELECT capital_rub FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? AND warehouse_key='production' AND nm_id=1",(version,)).fetchone()[0]
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET capital_rub='9999' WHERE version_id=? AND warehouse_key='production' AND nm_id=1",(version,));conn.commit()
        fails(lambda:read_native_proof(rt,current,now=MOMENT),'balance_publication_changed')
        with _connect(rt.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET capital_rub=? WHERE version_id=? AND warehouse_key='production' AND nm_id=1",(original_capital,version));conn.commit()
            actual=conn.execute("SELECT rub_value_delta FROM sheet_vitrina_v1_cny_ledger_operations WHERE source_order_id='source' AND operation_type='supplier_payment_out'").fetchone()[0]
            conn.execute("UPDATE sheet_vitrina_v1_cny_ledger_operations SET rub_value_delta='-1001' WHERE source_order_id='source' AND operation_type='supplier_payment_out'");conn.commit()
        fails(lambda:read_native_proof(rt,current,now=MOMENT),'cost_operands_changed')
        with _connect(rt.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_cny_ledger_operations SET rub_value_delta=? WHERE source_order_id='source' AND operation_type='supplier_payment_out'",(actual,));conn.commit()
        with _connect(rt.db_path) as conn:
            conn.execute('CREATE TABLE fixture_observer_noise(value INTEGER)');conn.execute('INSERT INTO fixture_observer_noise VALUES(0)');conn.commit()
        original=finance.__class__._build_week_target_projection
        def commit_during(block,*args,**kwargs):
            result=original(block,*args,**kwargs);separate_commit(rt);return result
        with patch.object(finance.__class__,'_build_week_target_projection',commit_during):
            fails(lambda:read_native_proof(rt,current,now=MOMENT),'read_snapshot_changed')
        with heavy_admitted(rt.runtime_dir,operation='fixture'),warehouse_functional_job_lock(rt.runtime_dir):
            with patch.object(processing,'_after_native_proof',side_effect=lambda:separate_commit(rt)):
                fails(lambda:processing.record_completion(rt,current,now=MOMENT),'handoff_changed')
            assert not source.read_acceptance(rt.db_path,current,request_scope='alice-key')['processing']['complete']
            assert processing.record_completion(rt,current,now=MOMENT)['complete']
            with patch('packages.application.operator_supplier_cost_proof.read_native_proof',side_effect=AssertionError('completed action rerun')):
                assert processing.record_completion(rt,current,now=MOMENT)['complete']
        print('coalesced exact revision; actual concurrent commit during proof and handoff; retry/idempotent completion: OK')


def factual_completion():
    from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
    from packages.application import operator_supplier_factual_dates as facts
    with TemporaryDirectory(prefix='supplier-factual-cost-') as raw:
        rt=seed(raw);header,lines=prepare_source(rt)
        header.update(actual_shipment_date=DAY,order_status='in_transit')
        rt.save_supplier_shipment(header=header,lines=lines)
        intents.drain_supplier_preparation_intents(rt,shipment_ids=['source']);publish_functional(rt)
        publish_accounting(rt,opening=True)
        entry=RegistryUploadHttpEntrypoint(runtime=rt,runtime_dir=rt.runtime_dir,activated_at_factory=lambda:NOW)
        preview=entry.handle_supplier_factual_dates_preview_request('source',{'actual_shipment_date':'2026-07-23'})
        body={'request_id':'supplier-factual-cost-native','confirmation_token':preview['confirmation_token']}
        saved=entry.handle_supplier_factual_dates_confirm_request('source',body,actor='alice',request_scope='alice-key')
        op=saved['acceptance']['operation_id']
        result=facts.consume(rt,block=entry.supplier_shipment_factual_correction_block)
        assert result['requests'][0]['status']=='success',result
        intents.drain_supplier_preparation_intents(rt,shipment_ids=['source']);publish_functional(rt);publish_accounting(rt)
        finance=WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT)
        finance.ensure_schema();finance.ingest_week(date(2026,7,20),date(2026,7,26),[_row(1,DAY,nm_id=1)])
        read_native_proof(rt,op,now=MOMENT)
        with heavy_admitted(rt.runtime_dir,operation='fixture'),warehouse_functional_job_lock(rt.runtime_dir):
            from apps import warehouse_functional_runner as runner
            with patch.object(runner,'block_from_env',return_value=finance):
                downstream=runner._recalculate_downstream_finance_cost(rt,now=MOMENT)
            assert downstream['supplier_operation_completion']['operations'][0]['complete'],downstream
        exact=facts.read(rt.db_path,body['request_id'],shipment_id='source',request_scope='alice-key')
        assert exact['acceptance']['operation_id']==op and exact['acceptance']['physical_applied']
        assert exact['acceptance']['state']=='completed' and exact['acceptance']['processing']['complete']
        assert exact['acceptance']['processing']['calculation']['version_id']
        print('actual factual native job -> targeted apply -> new preparation -> published exact cost -> same-ID completed GET: OK')


def wb_authority_guards():
    from packages.application.operator_supplier_cost_proof import validate_wb_projection
    from packages.application.our_wb_costs import OurWbCostBlock
    from apps.sheet_vitrina_v1_fulfillment_services_smoke import _seed_wb_supplies
    with TemporaryDirectory(prefix='supplier-wb-authority-') as raw:
        rt=seed(raw);_seed_wb_supplies(rt)
        OurWbCostBlock(runtime=rt,timestamp_factory=lambda:NOW).materialize_wb_supply_cost_layers()
        scope={'affected_nm_ids':[1],'effective_date':'2026-07-24'}
        with closing(source.readonly(rt.db_path)) as conn:validate_wb_projection(conn,scope)
        with _connect(rt.db_path) as conn:
            old=conn.execute("SELECT raw_goods_json FROM sheet_vitrina_v1_wb_supplies WHERE supply_id='1001'").fetchone()[0]
            goods=json.loads(old);goods[0]['quantity']=20;goods[0]['acceptedQuantity']=20
            conn.execute("UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=? WHERE supply_id='1001'",(json.dumps(goods),));conn.commit()
        def verify():
            with closing(source.readonly(rt.db_path)) as conn:validate_wb_projection(conn,scope)
        fails(verify,'cost_projection_pending')
        with _connect(rt.db_path) as conn:
            goods=json.loads(old);goods.append({'nmID':2,'quantity':10,'acceptedQuantity':10})
            conn.execute("UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=? WHERE supply_id='1001'",(json.dumps(goods),));conn.commit()
        fails(verify,'sku_scope_changed')
        OurWbCostBlock(runtime=rt,timestamp_factory=lambda:NOW).materialize_wb_supply_cost_layers()
        verify()
        print('actual native WB layer denominator and full inactive-SKU closure; corrected native rematerialization: OK')


if __name__=='__main__':main()
