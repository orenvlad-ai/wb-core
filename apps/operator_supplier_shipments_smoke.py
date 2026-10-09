"""Real supplier sources: immutable operator versions, recovery, CAS and RO reads."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from packages.application import operator_supplier_shipments as receipts, supplier_preparation_intents as intents
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _connect
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from apps.sheet_vitrina_v1_supplier_shipments_http_smoke import _seed_target_facilities, _build_invoice_fixture, TARGET_FACILITY_ID

NOW='2026-10-08T10:00:00Z'


def seed(raw):
    rt=RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
    entry=RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir,runtime=rt,activated_at_factory=lambda:NOW)
    _seed_target_facilities(rt)
    block=entry.supplier_shipments_block
    for nm,barcode in enumerate(['1111111111111','2222222222222','3333333333333'],101):
        block.create_nomenclature_item({'is_active':True,'nm_id':nm,'barcode':barcode,'nomenclature_name':'fixture',
            'product_type':'clean' if nm==101 else 'anti_spy','match_key':'clean|iphone_14_pro' if nm==101 else '', 'purchase_price_yuan':'1'})
    parsed=block.parse_upload(_build_invoice_fixture(),uploaded_filename='synthetic-source.xlsx')
    payload={'request_id':'supplier-create-fixture','upload_id':parsed['upload_id'],'shipment_date':'2026-10-10',
             'target_facility_id':TARGET_FACILITY_ID,'payload':parsed}
    return rt,entry,payload


def main():
    with TemporaryDirectory(prefix='operator-supplier-') as raw:
        rt,entry,payload=seed(raw)
        # Source HTTP application must not consume heavy native preparation or
        # invoke a migration/materialization detail getter after the final save.
        with patch.object(intents,'resume_supplier_preparation',side_effect=AssertionError('heavy after source save')), \
             patch.object(entry.supplier_shipments_block,'get_shipment',side_effect=AssertionError('heavy native detail')):
            saved=entry.handle_supplier_shipments_create_request(payload,actor='alice',request_scope='alice-key')
        op=saved['acceptance']['operation_id'];shipment=saved['shipment_id']
        assert saved['acceptance']['state']=='accepted' and op!=payload['request_id']
        assert saved['acceptance']['actor']=='alice'
        # A fresh application instance reopens the SAME immutable version.
        restarted=RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir,runtime=rt)
        with patch.object(restarted.supplier_shipments_block,'create_shipment',side_effect=AssertionError('repeat final source')):
            assert restarted.handle_supplier_shipments_create_request(payload,actor='alice',request_scope='alice-key')['acceptance']['operation_id']==op
        changed=deepcopy(payload);changed['shipment_date']='2026-10-11'
        try:entry.handle_supplier_shipments_create_request(changed,request_scope='alice-key')
        except ValueError:pass
        else:raise AssertionError('request identity collision accepted')
        assert receipts.read_request(rt.db_path,payload['request_id'],request_scope='other-actor')['status']=='unknown'
        before=rt.db_path.read_bytes()
        with patch.object(receipts,'ensure_schema',side_effect=AssertionError('GET bootstrap')):
            recovered=receipts.read_request(rt.db_path,payload['request_id'],request_scope='alice-key')
            assert recovered['shipment']['shipment_id']==shipment and recovered['acceptance']['operation_id']==op
            assert receipts.read_acceptance(rt.db_path,op,request_scope='alice-key')['operation_id']==op
        assert before==rt.db_path.read_bytes()
        price=entry.handle_supplier_shipments_price_check_request(shipment,{'request_id':'supplier-price-fixture'},actor='alice',request_scope='alice-key')
        assert price['acceptance']['processing']=={'kind':'source_only','complete':True}
        assert price['acceptance']['source_ref']['revision']==2
        edit=entry.handle_supplier_shipments_patch_request(shipment,{'request_id':'supplier-edit-fixture','shipment_date':'2026-10-11'},actor='alice',request_scope='alice-key')
        assert edit['acceptance']['source_ref']['revision']==3
        assert receipts.read_request(rt.db_path,payload['request_id'],request_scope='alice-key')['shipment']['shipment_date']=='2026-10-10'
        old=receipts.read_acceptance(rt.db_path,op,request_scope='alice-key')
        assert old['reason_code']=='source_superseded' and old['state']!='completed'
        # Delivered/coalesced/native queue flags never prove cost completion.
        with _connect(rt.db_path) as conn:
            conn.execute(f"UPDATE {intents.TABLE} SET status='delivered' WHERE shipment_id=?",(shipment,));conn.commit()
        current=receipts.read_acceptance(rt.db_path,edit['acceptance']['operation_id'],request_scope='alice-key')
        assert current['state']=='processing' and not current['processing']['complete']
        context,_=receipts.request_context(rt.db_path,request_id='supplier-cas-fixture',request_scope='alice-key',actor='alice',action='edit',payload={},shipment_id=shipment)
        source=rt.load_supplier_shipment(shipment)
        rt.save_supplier_shipment(header={**source['header'],'invoice_no':'concurrent-native'},lines=source['lines'])
        try:rt.save_supplier_shipment(header=source['header'],lines=source['lines'],operator_request=context)
        except ValueError:pass
        else:raise AssertionError('stale source overwrote concurrent commit')
        assert rt.load_supplier_shipment(shipment)['header']['invoice_no']=='concurrent-native'
        # A failed acknowledgement insert rolls back source AND native intent.
        with _connect(rt.db_path) as conn:
            conn.execute(f"CREATE TRIGGER fail_receipt BEFORE INSERT ON {receipts.TABLE} BEGIN SELECT RAISE(ABORT,'synthetic-stop'); END");conn.commit()
        try:entry.handle_supplier_shipments_patch_request(shipment,{'request_id':'supplier-failure-fixture','shipment_date':'2026-10-12'},request_scope='alice-key')
        except sqlite3.IntegrityError:pass
        else:raise AssertionError('source saved without acknowledgement')
        assert rt.load_supplier_shipment(shipment)['header']['shipment_date']=='2026-10-11'
        with _connect(rt.db_path) as conn:conn.execute('DROP TRIGGER fail_receipt');conn.commit()
        completeness=entry.handle_supplier_shipments_expenses_complete_patch_request(shipment,{'request_id':'supplier-complete-fixture','expenses_complete':True},actor='alice',request_scope='alice-key')
        assert completeness['acceptance']['state']=='accepted' # A claim is not certified cost.
        archived=entry.handle_supplier_shipments_delete_request(shipment,{'request_id':'supplier-archive-fixture'},actor='alice',request_scope='alice-key')
        assert archived['archived'] and archived['acceptance']['state']=='accepted'
        assert receipts.read_request(rt.db_path,'supplier-archive-fixture',request_scope='alice-key')['shipment']['archived']
        try:entry.handle_supplier_shipments_patch_request(shipment,{'request_id':'supplier-rejected-fixture','shipment_date':'2026-10-12'},request_scope='alice-key')
        except ValueError:pass
        else:raise AssertionError('archived native edit was accepted')
        rejected=receipts.read_request(rt.db_path,'supplier-rejected-fixture',request_scope='alice-key')
        assert rejected['status']=='rejected' and rejected['acceptance'] is None
        assert receipts.read_request(rt.db_path,'supplier-failure-fixture',request_scope='alice-key')['status']=='unknown'
        with closing(receipts.readonly(rt.db_path)) as conn:
            row=conn.execute(f'SELECT * FROM {intents.TABLE} WHERE shipment_id=?',(shipment,)).fetchone()
            assert json.loads(row['affected_nm_ids_json'])==[101,102,103]
            assert row['effective_date']=='2026-05-14'
            assert conn.execute(f'SELECT count(*) FROM {receipts.TABLE}').fetchone()[0]==5
        safe=receipts.read_request(rt.db_path,payload['request_id'],request_scope='alice-key',supplier_safe=True)
        assert 'affected_nm_ids' not in safe['acceptance']['processing']
        assert 'source_file_path' not in safe['shipment']
        assert 'internal_nm_id' not in safe['shipment']['lines'][0]
    with TemporaryDirectory(prefix='operator-supplier-guard-') as raw:
        rt,entry,payload=seed(raw)
        bad={**payload,'actual_ff_acceptance_date':'2026-10-08'}
        try:entry.handle_supplier_shipments_create_request(bad)
        except ValueError:pass
        else:raise AssertionError('new source bypassed actual FF date guard')
        rejected=receipts.read_request(rt.db_path,bad['request_id'],request_scope='local_operator')
        assert rejected['status']=='rejected' and rejected['payload_digest']==receipts.digest(receipts.source_payload(bad))
        safe_bad={'request_id':'supplier-sanitize-refusal','upload_id':payload['upload_id'],'shipment_date':'2026-10-10','forbidden_source_field':True}
        try:entry.handle_supplier_shipments_create_request(safe_bad,supplier_safe=True)
        except ValueError:pass
        else:raise AssertionError('supplier sanitizer guard removed')
        assert receipts.read_request(rt.db_path,safe_bad['request_id'],request_scope='local_operator')['status']=='rejected'
        # The verified wire hash is distinct from semantic/source CAS. Neither
        # forged operands nor alternate wire text may borrow an accepted alias.
        numeric={**payload,'request_id':'supplier-numeric-wire','approx_yuan_rate':0.00001}
        wire=json.dumps(receipts.source_payload(numeric),ensure_ascii=False,separators=(',',':'))
        numeric[receipts.WIRE_FIELD]=wire
        saved=entry.handle_supplier_shipments_create_request(numeric)
        read=receipts.read_request(rt.db_path,numeric['request_id'],request_scope='local_operator')
        assert read['wire_digest']==receipts.verified_wire_digest(numeric)
        assert read['payload_digest']==receipts.digest(receipts.source_payload(numeric))
        forged={**numeric,'request_id':'supplier-forged-wire','approx_yuan_rate':0.00002}
        try:entry.handle_supplier_shipments_create_request(forged)
        except ValueError:pass
        else:raise AssertionError('unbound wire certified different source operands')
        assert receipts.read_request(rt.db_path,forged['request_id'],request_scope='local_operator')['status']=='unknown'
        alternate={**numeric,receipts.WIRE_FIELD:json.dumps(receipts.source_payload(numeric),ensure_ascii=False,indent=2)}
        try:entry.handle_supplier_shipments_create_request(alternate)
        except ValueError:pass
        else:raise AssertionError('same alias accepted another wire identity')
        for identity,edit in [('supplier-edit-sanitize-refusal',{'forbidden_source_field':True}),
                              ('supplier-edit-date-refusal',{'actual_ff_acceptance_date':'2026-10-08'})]:
            try:entry.handle_supplier_shipments_patch_request(saved['shipment_id'],{'request_id':identity,**edit},supplier_safe=True)
            except ValueError:pass
            else:raise AssertionError('identified source edit bypassed validation guard')
            assert receipts.read_request(rt.db_path,identity,request_scope='local_operator')['status']=='rejected'
        with closing(receipts.readonly(rt.db_path)) as conn:
            assert conn.execute('SELECT count(*) FROM sheet_vitrina_v1_supplier_shipments').fetchone()[0]==1
            assert conn.execute(f'SELECT count(*) FROM {receipts.TABLE}').fetchone()[0]==1
            assert conn.execute(f'SELECT count(*) FROM {receipts.REJECTIONS}').fetchone()[0]==4
    print('native supplier source/receipt/intent atomic; immutable versions+same-ID restart; CAS; coalesced/delivered no false complete; source-only price; archive+safe scope; verified wire and retained create/edit validation refusals: OK')


if __name__=='__main__':main()
