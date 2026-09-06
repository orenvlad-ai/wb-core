"""Authoritative form receipt, backdate/history, atomicity and durable replay checks."""
from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import warehouse_fbs_material_rematerialization_smoke as material_fixture
from apps.our_wb_costs_smoke import _seed_supplier_shipment, _seed_financial_inputs, SUPPLIER_BARCODE
from packages.application.ff_pool_cutover import ensure_ff_pool_cutover_schema, MANIFESTS_TABLE
from packages.application.ff_pool_documents import FfPoolDocumentService, DOCUMENTS_TABLE
from packages.application.ff_pool_documents_xlsx import build_china_acceptance_form_manifest, FfPoolXlsxError
from packages.application.ff_pool_foundation import BALANCES_TABLE, canonical_decimal_ratio_text
from packages.application.ff_pool_surfaces import FfPoolSurface, FfPoolSurfaceError
from packages.application.warehouse_fbs_material_rematerialization import WarehouseFbsMaterialError

DAY = '2026-09-06'
SOURCE_DAY = '2026-09-01'
NOW = DAY + 'T12:00:00Z'
NM_ID = 497413000
FACILITY = material_fixture.FACILITY_ID
SHIPMENT = 'sup_form_receipt'


def _fixture(root: Path):
    with patch.object(material_fixture, 'TARGET_NM_ID', NM_ID), patch.object(material_fixture, 'DAY', DAY), patch.object(material_fixture, 'NOW', NOW):
        runtime = material_fixture._seed(root, mixed=False)
    runtime.save_nomenclature_item({'item_id':'form-sku','is_active':True,'our_sku':'SKU-1','nm_id':NM_ID,'barcode':SUPPLIER_BARCODE,'nomenclature_name':'SKU 1','purchase_price_yuan':90,'created_at':NOW,'updated_at':NOW})
    _seed_supplier_shipment(runtime, shipment_id=SHIPMENT, actual_ff_acceptance_date='')
    _seed_financial_inputs(runtime, shipment_id=SHIPMENT)
    with sqlite3.connect(runtime.db_path) as conn:
        ensure_ff_pool_cutover_schema(conn)
        columns = conn.execute(f'PRAGMA table_info({MANIFESTS_TABLE})').fetchall()
        values = {row[1]: (0 if row[2] == 'INTEGER' else 'fixture') for row in columns}
        values.update(cutover_id='form_fixture_cutover',manifest_digest='sha256:'+'a'*64,deployed_sha='a'*40,cutover_at=NOW,business_date=DAY,feature_epoch=1,created_at=NOW,manifest_json='{}')
        conn.execute(f'INSERT INTO {MANIFESTS_TABLE} ({",".join(values)}) VALUES ({",".join("?" for _ in values)})', list(values.values()))
        conn.execute("UPDATE sheet_vitrina_v1_supplier_shipments SET target_facility_id=?,actual_shipment_date='2026-08-31',order_status='in_transit' WHERE shipment_id=?", (FACILITY,SHIPMENT))
        conn.execute('''INSERT INTO sheet_vitrina_v1_ready_snapshots
            SELECT bundle_version,activated_at,?,'pinned-past-ready',plan_version,refreshed_at,replace(plan_json,?,?)
            FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?''',(SOURCE_DAY,DAY,SOURCE_DAY,DAY))
        conn.commit()
    surface = FfPoolSurface(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir, timestamp_factory=lambda: NOW)
    assert surface.china_acceptance_form(SHIPMENT)['activation']['effective']
    return runtime, surface


def _payload(surface, mode='FBS'):
    source = surface.china_acceptance_form(SHIPMENT)
    assert source['lines'] == [{'nm_id':NM_ID,'sku':'SKU-1','barcode':SUPPLIER_BARCODE,'quantity':10}]
    return {'request_id':'form:'+mode,'shipment_id':SHIPMENT,'source_revision':source['source_revision'],'business_date':SOURCE_DAY,'facility_id':FACILITY,'mode':mode}


def _history(conn, versions):
    marks = ','.join('?' for _ in versions)
    rows = conn.execute(f'SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id IN ({marks}) ORDER BY version_id,warehouse_key,nm_id', versions).fetchall()
    ready = conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date').fetchall()
    return hashlib.sha256(json.dumps([rows, ready], default=str, sort_keys=True).encode()).hexdigest()


def _test_quantities_and_authority():
    args = {'shipment_lines':[{'nm_id':101,'quantity':10,'capital_rub':'123.456','sku':'server SKU','barcode':'00101'}], 'source_revision':'sha256:'+'b'*64,'facilities':[{'facility_id':'fac_one','code':'ONE','name':'Москва','active':True}], 'facility_id':'fac_one','mode':'split','rows':[{'nm_id':101,'accepted_quantity':10,'quantity_fbs':7,'quantity_fbo':3}]}
    result = build_china_acceptance_form_manifest(**args)['allocations'][0]
    assert result['expected_quantity'] == 10 and result['accepted_capital_rub'] == '123.456'
    assert result['sku'] == 'server SKU' and result['barcode'] == '00101'
    for mutate in (
        lambda row: row.update(quantity_fbs=-1), lambda row: row.update(quantity_fbo=1.5),
        lambda row: row.update(quantity_fbs=True), lambda row: row.update(quantity_fbs=2**63),
        lambda row: row.update(quantity_fbs='9'*5000), lambda row: row.update(quantity_fbs=8),
        lambda row: row.update(nm_id=999), lambda row: row.update(accepted_capital_rub='0.01'),
        lambda row: row.update(expected_quantity=9), lambda row: row.update(barcode='tampered'),
    ):
        invalid=copy.deepcopy(args);mutate(invalid['rows'][0])
        try: build_china_acceptance_form_manifest(**invalid)
        except FfPoolXlsxError: pass
        else: raise AssertionError(f'bad form accepted: {invalid["rows"]}')
    for rows in ([],args['rows']*2):
        try: build_china_acceptance_form_manifest(**{**args,'rows':rows})
        except FfPoolXlsxError: pass
        else: raise AssertionError('incomplete/duplicate composition accepted')


def _test_post_and_replay(mode):
    with TemporaryDirectory(prefix='ff-form-'+mode+'-') as directory:
        runtime,surface=_fixture(Path(directory))
        # Existing overhead is already in current pool capital and must remain once.
        overhead=surface.accept_pool_overhead_preview({'request_id':'prior:overhead','facility_id':FACILITY,'business_date':DAY,'scope':'FBS','amount_rub':'85553','category':'storage','comment':'Prior overhead','source_mode':'manual'},actor='fixture')
        surface.confirm_document(overhead['request_id'])
        with sqlite3.connect(runtime.db_path) as conn:
            prior_capital=Decimal(conn.execute(f'SELECT capital_rub FROM {BALANCES_TABLE} WHERE facility_id=? AND pool=\'FBS\' AND nm_id=?',(FACILITY,NM_ID)).fetchone()[0])
            # Overhead allocates to all existing FBS quantities, including the non-target SKU.
            prior_total= sum(Decimal(row[0]) for row in conn.execute(f'SELECT capital_rub FROM {BALANCES_TABLE}'))
            assert prior_total == Decimal('19530')+Decimal('420')+Decimal('85553')
            versions=[row[0] for row in conn.execute('SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_versions')]
            history_before=_history(conn,versions)
        payload=_payload(surface,mode)
        accepted=10
        if mode=='split':
            accepted=9
            payload['rows']=[{'nm_id':NM_ID,'accepted_quantity':9,'quantity_fbs':6,'quantity_fbo':3,'comment':'Одна единица недостачи'}]
        preview=surface.accept_china_form(payload,actor='fixture')
        assert preview['confirm_allowed'], preview
        alias=surface.accept_china_form({**payload,'request_id':payload['request_id']+':alias'},actor='fixture')
        assert alias['request_id']==preview['request_id']
        assert preview['preview']['summary']['expected_quantity']==10
        assert Decimal(preview['preview']['summary']['capital_normalization']['canonical_total_rub'])==Decimal(1260*accepted)
        with patch.object(FfPoolDocumentService,'_finalize_posted',side_effect=AssertionError('HTTP must not wait for replay')):
            posted=surface.confirm_document(preview['request_id'])
            assert posted['state']=='posted' and posted['document']['document_id'],posted
            repeated=surface.confirm_document(preview['request_id'])
            assert repeated['document']==posted['document']
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute('SELECT actual_ff_acceptance_date FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?',(SHIPMENT,)).fetchone()[0]==SOURCE_DAY
            assert conn.execute('SELECT business_effective_date FROM sheet_vitrina_v1_ff_stock_operations WHERE source_object_id=?',(SHIPMENT,)).fetchone()[0]==SOURCE_DAY
            doc=conn.execute(f'SELECT business_date FROM {DOCUMENTS_TABLE} WHERE document_id=?',(posted['document']['document_id'],)).fetchone()
            assert doc[0]==SOURCE_DAY
            active=conn.execute('SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1').fetchone()[0]
            version=conn.execute('SELECT business_effective_date,source_watermarks_json FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?',(active,)).fetchone()
            assert version[0]==DAY
            assert json.loads(version[1])['fbs_material_revision']['source_business_date']==SOURCE_DAY
            quantity,capital,wac=conn.execute("SELECT quantity,capital_rub,wac_rub FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? AND warehouse_key='ff' AND nm_id=?",(active,NM_ID)).fetchone()
            assert Decimal(quantity)==1953+accepted
            assert Decimal(capital)==prior_capital+Decimal(1260*accepted)
            assert wac==canonical_decimal_ratio_text(capital,quantity)
            assert _history(conn,versions)==history_before
            assert conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?',(SOURCE_DAY,)).fetchone()[0]==1
            pools=dict(conn.execute(f'SELECT pool,quantity FROM {BALANCES_TABLE} WHERE facility_id=? AND nm_id=?',(FACILITY,NM_ID)))
            assert pools['FBS']==1953+(accepted if mode=='FBS' else 6 if mode=='split' else 0)
            assert pools.get('FBO',0)==(accepted if mode=='FBO' else 3 if mode=='split' else 0)
            assert conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_supplier_ff_cost_layers WHERE supplier_shipment_id=?',(SHIPMENT,)).fetchone()[0]==0
        # Recreate service as the scheduled warehouse runner does; no browser needed.
        service=FfPoolDocumentService(db_path=runtime.db_path,runtime_dir=runtime.runtime_dir,timestamp_factory=lambda:NOW,resume=False)
        resumed=service.resume_incomplete()
        status=surface.request_status(preview['request_id'])
        assert status['state']=='complete',status
        with sqlite3.connect(runtime.db_path) as conn:
            layer=conn.execute('SELECT layer_id,accepted_ff_date FROM sheet_vitrina_v1_supplier_ff_cost_layers WHERE supplier_shipment_id=?',(SHIPMENT,)).fetchone()
            assert layer and layer[1]==SOURCE_DAY
            replay=conn.execute('SELECT cost_layer_id FROM sheet_vitrina_v1_ff_guided_acceptance_replays WHERE request_id=?',(preview['request_id'],)).fetchone()
            assert replay[0]==layer[0]
            queue=conn.execute('SELECT effective_date FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE stable_source_id=?',(f'supplier_shipment:{SHIPMENT}',)).fetchone()
            assert queue[0]==SOURCE_DAY
            assert _history(conn,versions)==history_before
            assert conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations WHERE source_object_id=?',(SHIPMENT,)).fetchone()[0]==1
        service.resume_incomplete()
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_supplier_ff_cost_layers WHERE supplier_shipment_id=?',(SHIPMENT,)).fetchone()[0]==1
        print(f'form {mode}: factual={SOURCE_DAY}; current={DAY}; WAC={wac}; immutable_history_sha256={history_before}; resumed={resumed}')


def _test_stale_and_atomic():
    for failure in ('stale_before_preview','stale_before_confirm','atomic_publisher_failure'):
        with TemporaryDirectory(prefix='ff-form-guard-') as directory:
            runtime,surface=_fixture(Path(directory))
            payload=_payload(surface)
            with sqlite3.connect(runtime.db_path) as conn:
                before_documents=conn.execute(f'SELECT document_id FROM {DOCUMENTS_TABLE} ORDER BY document_id').fetchall()
            if failure=='stale_before_preview':
                with sqlite3.connect(runtime.db_path) as conn: conn.execute("UPDATE sheet_vitrina_v1_supplier_shipment_lines SET qty=11 WHERE shipment_id=?",(SHIPMENT,))
                try: surface.accept_china_form(payload,actor='fixture')
                except FfPoolSurfaceError as exc: assert exc.code=='supplier_source_revision_changed'
                else: raise AssertionError('stale form accepted')
            else:
                preview=surface.accept_china_form(payload,actor='fixture')
                assert preview['confirm_allowed'],preview
                if failure=='stale_before_confirm':
                    with sqlite3.connect(runtime.db_path) as conn: conn.execute("UPDATE sheet_vitrina_v1_supplier_shipments SET cny_payment_currency_rub_cost='12000' WHERE shipment_id=?",(SHIPMENT,))
                    status=surface.confirm_document(preview['request_id'])
                    assert status['state']=='blocked' and status['error']['code']=='supplier_source_revision_changed',status
                else:
                    with patch('packages.application.ff_pool_documents.publish_fbs_pool_aggregate_revision',side_effect=WarehouseFbsMaterialError('fixture_publication_failure','injected')):
                        status=surface.confirm_document(preview['request_id'])
                    assert status['state']=='blocked',status
            with sqlite3.connect(runtime.db_path) as conn:
                assert conn.execute('SELECT actual_ff_acceptance_date FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?',(SHIPMENT,)).fetchone()[0] is None
                assert conn.execute(f'SELECT document_id FROM {DOCUMENTS_TABLE} ORDER BY document_id').fetchall()==before_documents, failure
                assert conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations WHERE source_object_id=?',(SHIPMENT,)).fetchone()[0]==0
                assert conn.execute(f'SELECT quantity FROM {BALANCES_TABLE} WHERE nm_id=?',(NM_ID,)).fetchone()[0]==1953


def main():
    _test_quantities_and_authority()
    for mode in ('FBS','FBO','split'): _test_post_and_replay(mode)
    _test_stale_and_atomic()
    print('warehouse_ff_acceptance_form_smoke: OK')


if __name__=='__main__': main()
