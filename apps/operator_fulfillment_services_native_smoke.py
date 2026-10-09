"""Actual source -> WarehouseFunctional -> accounting/ready -> Finance publishers.

All files are disposable. No mocked publisher or synthetic downstream proof table.
"""
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.fulfillment_recalc_intents_smoke import fixture, queue
from apps.operator_cny_journal_smoke import source_snapshot, get_only_connect
from apps.sheet_vitrina_v1_fulfillment_services_smoke import _build_workbook, _valid_row, _storage_row, NOW
from apps.warehouse_targeted_replay_smoke import _seed_functional
from apps.wb_finance_weekly_cost_cutover_smoke import _row
from apps.ready_publication_smoke import make_plan, save
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application.storage_registry import StoreRegistry
from packages.application.our_wb_costs import OurWbCostBlock
from packages.application.warehouse_functional import WarehouseFunctionalBlock
from packages.application import fbs_accounting_runtime as accounting, operator_fulfillment_services as receipt
from packages.application.operator_fulfillment_services_proof import read_native_proof
from packages.application.operator_manual_ff_stock import ModernFfWorkflowRequired
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock

MOMENT = datetime.fromisoformat(NOW.replace('Z', '+00:00'))
DAY = MOMENT.date().isoformat()


def seed(raw):
    rt, block = fixture(raw)
    # Disposable concurrency contour permits real commits under pinned RO readers.
    with sqlite3.connect(rt.db_path) as conn: conn.execute('PRAGMA journal_mode=WAL')
    bundle = json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text())
    rt.ingest_bundle(bundle, activated_at=NOW)
    _seed_functional(rt)
    from packages.application.canonical_cost_engine import CanonicalCostEngine
    CanonicalCostEngine(runtime=rt)
    # Historical legacy stock exists before the native facility/pool cutover.
    for op, kind, supply, delta, stamp in (
        ('opening','manual_excel','',30,'2026-07-01T00:00:00Z'),
        ('debit-1001','wb_supply','1001',-10,'2026-07-05T00:00:00Z'),
        ('debit-1002','wb_supply','1002',-20,'2026-07-05T01:00:00Z')):
        rt.create_ff_stock_operation(operation_id=op, operation_type='manual_receipt' if delta>0 else 'auto_writeoff',
            source_type=kind,source_key=op,source_object_id=supply,created_at=stamp,lines=[{'nm_id':1,'quantity_delta':delta}])
    with _connect(rt.db_path) as conn:
        conn.execute('DELETE FROM sheet_vitrina_v1_warehouse_functional_balances')
        conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id<>'base'")
        conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id<>'base'")
        conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES('base','ff',1,'30','10','300','30','opening',0,'0','0','0','{}')")
        conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_versions SET version_kind='functional_cutover',effective_at=?,published_at=?,created_at=?,business_effective_date='2026-07-01' WHERE version_id='base'", (NOW,NOW,NOW))
        conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_cutovers SET cutover_at='2026-07-01T00:00:00Z'")
        raw_wb = [{'nmId': 1, 'warehouseId': 1, 'stockCount': 30, 'inWayToClient': 0, 'inWayFromClient': 0}]
        conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_snapshots SET snapshot_date=?,fetched_at=?,requested_nm_ids_json='[1]',raw_rows_json=?,items_json=?,raw_row_count=1,raw_rows_digest=?",
            (DAY, NOW, json.dumps(raw_wb), json.dumps([{'nm_id':1,'quantity':'30','in_way_to_client':'0','in_way_from_client':'0','wb_contour_quantity':'30'}]), receipt.digest(raw_wb)))
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
    return rt, block


def publish_functional(rt):
    OurWbCostBlock(runtime=rt,timestamp_factory=lambda:NOW).materialize_wb_supply_cost_layers()
    pending = [row for row in queue(rt) if row['status']=='queued']
    with receipt.readonly(rt.db_path) as conn:
        last = conn.execute("SELECT version.published_at FROM sheet_vitrina_v1_warehouse_functional_active active JOIN sheet_vitrina_v1_warehouse_functional_versions version USING(version_id)").fetchone()[0]
    stamp = (datetime.fromisoformat(last.replace('Z','+00:00'))+timedelta(seconds=1)).isoformat().replace('+00:00','Z')
    native = WarehouseFunctionalBlock(runtime=rt,timestamp_factory=lambda:stamp)
    plan = native.build_targeted_recovery_plan(affected_nm_ids=[1],
        stable_source_ids=[row['stable_source_id'] for row in pending],targeted_recalc_requests=pending)
    return native.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'])


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


def main():
    with TemporaryDirectory() as raw:
        rt, block = seed(raw)
        # Backdating must not admit a new legacy source after native cutover.
        with receipt.readonly(rt.db_path) as conn:
            before_manual = tuple(conn.iterdump())
        try:
            rt.create_ff_stock_operation(operation_id='blocked-opening', operation_type='manual_receipt',
                source_type='manual_excel', source_key='blocked-opening', created_at='2026-07-01T00:00:00Z',
                lines=[{'nm_id':1,'quantity_delta':30}])
        except ModernFfWorkflowRequired as exc:
            assert exc.code=='modern_workflow_required'
        else:
            raise AssertionError('legacy manual source admitted after native cutover')
        with receipt.readonly(rt.db_path) as conn:
            assert tuple(conn.iterdump())==before_manual
        print('historical legacy opening retained; post-cutover backdated manual source rejected with zero database changes')
        row = _valid_row('1001'); row[1]='Other upload'
        other = block.upload_xlsx(_build_workbook([row]))
        published = publish_functional(rt)
        assert next(row for row in published['balances'] if row['warehouse_key']=='wb')['wac_rub']=='82.5'
        publish_accounting(rt,opening=True)
        finance = WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT)
        finance.ensure_schema()
        finance.ingest_week(date(2026,6,29),date(2026,7,5),[_row(2,'2026-07-01',nm_id=1)])
        finance.ingest_week(date(2026,7,6),date(2026,7,12),[_row(1,DAY,nm_id=1)])
        with receipt.readonly(rt.db_path) as conn:
            control_before = conn.execute("SELECT metrics_json FROM wb_finance_weekly_aggregates WHERE week_start='2026-06-29'").fetchone()[0]
        target = block.upload_xlsx(_build_workbook([_valid_row('1001'),_storage_row()]))
        op = target['acceptance']['operation_id']
        fails(lambda:read_native_proof(rt,op,now=MOMENT),'functional_publication_pending')
        with _connect(rt.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_targeted_recalc_queue SET status='complete' WHERE queue_id=?",
                         (target['warehouse_targeted_recalculation']['queue_id'],));conn.commit()
        fails(lambda:read_native_proof(rt,op,now=MOMENT),'correlated_functional_proof_missing')
        with _connect(rt.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_targeted_recalc_queue SET status='queued' WHERE queue_id=?",
                         (target['warehouse_targeted_recalculation']['queue_id'],));conn.commit()
        published = publish_functional(rt)
        assert next(row for row in published['balances'] if row['warehouse_key']=='wb')['capital_rub']=='4575'
        fails(lambda:read_native_proof(rt,op,now=MOMENT),'accounting_exact_version_pending')
        publish_accounting(rt)
        fails(lambda:read_native_proof(rt,op,now=MOMENT),'finance_target_projection_pending')
        plan = finance.plan_stale_cost_weeks(date_from=date(2026,7,6),date_to=date(2026,7,12))
        assert plan['stale_week_count']==1
        applied = finance.apply_stale_cost_weeks(expected_fingerprint=plan['fingerprint'],date_from=date(2026,7,6),date_to=date(2026,7,12))
        assert applied['non_target_preserved'] and not applied['source_advanced_after_apply']
        with receipt.readonly(rt.db_path) as conn:
            assert json.loads(conn.execute("SELECT metrics_json FROM wb_finance_weekly_aggregates WHERE week_start='2026-07-06'").fetchone()[0])['cogs']=='152.5000'
            assert conn.execute("SELECT metrics_json FROM wb_finance_weekly_aggregates WHERE week_start='2026-06-29'").fetchone()[0]==control_before
        with _connect(rt.db_path) as conn:
            version_id = json.loads(conn.execute('SELECT proof_json FROM '+receipt.FUNCTIONAL_PROOFS+' WHERE operation_id=?',(op,)).fetchone()[0])['warehouse_version_id']
            original_digest = conn.execute('SELECT raw_rows_digest FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id=?',(version_id,)).fetchone()[0]
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_snapshots SET raw_rows_digest='corrupt' WHERE version_id=?",(version_id,));conn.commit()
        fails(lambda:read_native_proof(rt,op,now=MOMENT),'exact_wb_authority_incomplete')
        with _connect(rt.db_path) as conn:
            conn.execute('UPDATE sheet_vitrina_v1_warehouse_wb_snapshots SET raw_rows_digest=? WHERE version_id=?',(original_digest,version_id));conn.commit()
        # Live supply authority must cover the entire composition, even SKUs
        # outside the active catalogue. Stale native cost/ready is not completion.
        with receipt.readonly(rt.db_path) as conn:
            original_goods = conn.execute("SELECT raw_goods_json FROM sheet_vitrina_v1_wb_supplies WHERE supply_id='1001'").fetchone()[0]
        def set_goods(value):
            def write():
                with _connect(rt.db_path) as conn:
                    conn.execute("UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=? WHERE supply_id='1001'", (value,))
                    conn.commit()
            with ThreadPoolExecutor(max_workers=1) as pool: pool.submit(write).result(timeout=5)
        changed_quantity = json.loads(original_goods)
        changed_quantity[0]['quantity'] = changed_quantity[0]['acceptedQuantity'] = 20
        added_sku = json.loads(original_goods) + [{'nmID':2,'quantity':10,'acceptedQuantity':10}]
        removed_sku = [{'nmID':2,'quantity':10,'acceptedQuantity':10}]
        changed_accepted = json.loads(original_goods)
        changed_accepted[0]['acceptedQuantity'] = 5
        with heavy_admitted(rt.runtime_dir,operation='fixture-source-authority'), warehouse_functional_job_lock(rt.runtime_dir):
            for goods, reason in ((changed_quantity,'supply_cost_source_changed'),
                                  (added_sku,'supply_sku_scope_changed'),
                                  (removed_sku,'supply_sku_scope_changed'),
                                  (changed_accepted,'supply_cost_source_changed')):
                set_goods(json.dumps(goods))
                fails(lambda:read_native_proof(rt,op,now=MOMENT),reason)
                fails(lambda:receipt.record_completion(rt,op,now=MOMENT),reason)
                assert receipt.read_acceptance(rt.db_path,op)['durable_saved']
                assert receipt.read_acceptance(rt.db_path,op)['state']!='completed'
                set_goods(original_goods)
            # The exact same authority drift after RO proof must fail its live
            # observer fence before the short writer can append a completion.
            with patch.object(receipt,'_after_native_proof',side_effect=lambda:set_goods(json.dumps(changed_quantity))):
                fails(lambda:receipt.record_completion(rt,op,now=MOMENT),'handoff_changed')
            set_goods(original_goods)
            with receipt.readonly(rt.db_path) as conn:
                assert conn.execute('SELECT count(*) FROM '+receipt.COMPLETIONS+' WHERE operation_id=?',(op,)).fetchone()[0]==0
        print('full native supply authority: qty/accepted quantity/add/remove SKU drift rejected; proof-to-CAS commit fenced')
        # Normalize only the disposable WAL fixture before the existing RO interval.
        with closing(sqlite3.connect(rt.db_path)) as conn:
            assert conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone() == (0, 0, 0)
        source_paths = (rt.db_path, accounting.path(rt.runtime_dir))
        logical_before = tuple(source_snapshot(path) for path in source_paths)
        wal_paths = tuple(Path(str(path) + '-wal') for path in source_paths)
        wal_before = tuple(path.read_bytes() if path.exists() else b'' for path in wal_paths)
        before_read = (rt.db_path.read_bytes(),accounting.path(rt.runtime_dir).read_bytes())
        connect = sqlite3.connect
        registry_connect = StoreRegistry.connect
        observed, guarded = set(), set()
        def get_connect(*args, **kwargs):
            if kwargs.get('factory') is None:
                return get_only_connect(connect, *args, **kwargs)
            # The native observed factory configures its attributes after opening.
            # Admit only RO here; install the same SQL guard after native setup.
            database = args[0] if args else kwargs['database']
            assert kwargs.get('uri') is True and parse_qs(urlparse(str(database)).query).get('mode') == ['ro'], 'proof opens a source writer'
            conn = connect(*args, **kwargs); observed.add(id(conn))
            return conn
        def get_registry(registry, *args, **kwargs):
            assert kwargs['mode'] == 'ro', 'proof opens a registry writer'
            conn = registry_connect(registry, *args, **kwargs)
            database = Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve().as_uri() + '?mode=ro'
            def set_authorizer(authorize):
                # Finance's existing source-version fence reads database_list.
                # Add only that no-argument introspection to the approved guard.
                conn.set_authorizer(lambda action, one, two, db, trigger: sqlite3.SQLITE_OK
                    if action == sqlite3.SQLITE_PRAGMA and one == 'database_list' and two is None
                    else authorize(action, one, two, db, trigger))
            get_only_connect(lambda *a, **k: SimpleNamespace(execute=conn.execute, set_authorizer=set_authorizer), database, uri=True)
            guarded.add(id(conn)); return conn
        with patch.object(sqlite3, 'connect', side_effect=get_connect), patch.object(StoreRegistry, 'connect', new=get_registry):
            proof = read_native_proof(rt,op,now=MOMENT)
        assert observed <= guarded, 'unprotected observed proof connection'
        assert before_read==(rt.db_path.read_bytes(),accounting.path(rt.runtime_dir).read_bytes())
        assert tuple(path.read_bytes() if path.exists() else b'' for path in wal_paths) == wal_before
        assert tuple(source_snapshot(path) for path in source_paths) == logical_before
        assert proof['finance']['target_weeks']==[['canonical','2026-07-06','2026-07-12']]
        # Earlier warehouse work must continue through the newer coalesced book.
        earlier_proof = read_native_proof(rt,other['acceptance']['operation_id'],now=MOMENT)
        assert earlier_proof['functional']['warehouse_version_id']==proof['functional']['warehouse_version_id']
        with _connect(rt.db_path) as conn:
            conn.execute('CREATE TABLE fixture_observer_noise(value INTEGER)')
            conn.execute('INSERT INTO fixture_observer_noise VALUES(0)');conn.commit()
        original_projection = WbFinanceWeeklyBlock._build_week_target_projection
        committed = False
        def racing_projection(self,*args,**kwargs):
            nonlocal committed
            if not committed:
                committed = True;separate_commit(rt)
            return original_projection(self,*args,**kwargs)
        with heavy_admitted(rt.runtime_dir,operation='fixture-finalizer'), warehouse_functional_job_lock(rt.runtime_dir):
            with patch.object(WbFinanceWeeklyBlock,'_build_week_target_projection',new=racing_projection):
                fails(lambda:receipt.record_completion(rt,op,now=MOMENT),'read_snapshot_changed')
            with patch.object(receipt,'_after_native_proof',side_effect=lambda:separate_commit(rt)):
                fails(lambda:receipt.record_completion(rt,op,now=MOMENT),'handoff_changed')
            with receipt.readonly(rt.db_path) as conn:
                source_line = conn.execute('SELECT id,amount_with_vat FROM sheet_vitrina_v1_fulfillment_service_lines WHERE upload_id=? ORDER BY id LIMIT 1',
                    (target['upload']['upload_id'],)).fetchone()
            def move_source():
                def write():
                    with _connect(rt.db_path) as conn:
                        conn.execute('UPDATE sheet_vitrina_v1_fulfillment_service_lines SET amount_with_vat=amount_with_vat+1 WHERE id=?',(source_line[0],));conn.commit()
                with ThreadPoolExecutor(max_workers=1) as pool: pool.submit(write).result(timeout=5)
            with patch.object(receipt,'_after_native_proof',side_effect=move_source):
                fails(lambda:receipt.record_completion(rt,op,now=MOMENT),'source_changed')
            with _connect(rt.db_path) as conn:
                conn.execute('UPDATE sheet_vitrina_v1_fulfillment_service_lines SET amount_with_vat=? WHERE id=?',(source_line[1],source_line[0]));conn.commit()
            with receipt.readonly(accounting.path(rt.runtime_dir)) as conn:
                current_book = conn.execute('SELECT version FROM accounting_current').fetchone()[0]
                older_book = conn.execute('SELECT previous_version FROM accounting_revisions WHERE version=?',(current_book,)).fetchone()[0]
            def move_book_pointer():
                def write():
                    with sqlite3.connect(accounting.path(rt.runtime_dir)) as conn:
                        conn.execute('UPDATE accounting_current SET version=?',(older_book,));conn.commit()
                with ThreadPoolExecutor(max_workers=1) as pool: pool.submit(write).result(timeout=5)
            with patch.object(receipt,'_after_native_proof',side_effect=move_book_pointer):
                fails(lambda:receipt.record_completion(rt,op,now=MOMENT),'handoff_changed')
            with sqlite3.connect(accounting.path(rt.runtime_dir)) as conn:
                conn.execute('UPDATE accounting_current SET version=?',(current_book,));conn.commit()
            with receipt.readonly(rt.db_path) as conn:
                assert conn.execute('SELECT count(*) FROM '+receipt.COMPLETIONS+' WHERE operation_id=?',(op,)).fetchone()[0]==0
        with heavy_admitted(rt.runtime_dir,operation='fixture-finalizer'), warehouse_functional_job_lock(rt.runtime_dir):
            complete = receipt.record_completion(rt,op,now=MOMENT)
            assert receipt.record_completion(rt,op,now=MOMENT)['updated_at']==complete['updated_at']
            assert receipt.record_completion(rt,other['acceptance']['operation_id'],now=MOMENT)['state']=='completed'
        assert complete['state']=='completed'
        deleted = block.delete_upload(target['upload']['upload_id'])
        delete_op = deleted['acceptance']['operation_id']
        assert receipt.read_acceptance(rt.db_path,op)['native_state']=='superseded'
        assert block.approved_overlay_by_supply()['1001']['upload_ids']==[other['upload']['upload_id']]
        publish_functional(rt);publish_accounting(rt)
        plan = finance.plan_stale_cost_weeks(date_from=date(2026,7,6),date_to=date(2026,7,12))
        assert plan['stale_week_count']==1
        finance.apply_stale_cost_weeks(expected_fingerprint=plan['fingerprint'],date_from=date(2026,7,6),date_to=date(2026,7,12))
        with heavy_admitted(rt.runtime_dir,operation='fixture-delete-finalizer'), warehouse_functional_job_lock(rt.runtime_dir):
            complete = receipt.record_completion(rt,delete_op,now=MOMENT)
        assert complete['state']=='completed' and complete['source_ref']['action']=='delete'
        assert complete['publication']['proof']['functional']['overlay']['1001']['upload_ids']==[other['upload']['upload_id']]
        assert complete['publication']['proof']['functional']['overlay']['1001']['layers'][0]['ff_services_amount_total']==1575
        with receipt.readonly(rt.db_path) as conn:
            assert json.loads(conn.execute("SELECT metrics_json FROM wb_finance_weekly_aggregates WHERE week_start='2026-07-06'").fetchone()[0])['cogs']=='82.5000'
            assert conn.execute("SELECT metrics_json FROM wb_finance_weekly_aggregates WHERE week_start='2026-06-29'").fetchone()[0]==control_before
        with receipt.readonly(rt.db_path) as conn:
            assert conn.execute('PRAGMA query_only').fetchone()[0]==1
            assert not receipt._exists(conn,'fixture_native_downstream')
        print('native upload/delete completed: real functional CAS, exact WB snapshot, actual accounting/ready, actual Finance CAS; own overlay preserved')
    print('operator_fulfillment_services_native_smoke: OK')


if __name__=='__main__': main()
