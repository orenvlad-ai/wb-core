#!/usr/bin/env python3
"""Forward WB admission/replay/publication on synthetic sources, not a full-copy rehearsal."""
from pathlib import Path
import sys,sqlite3,tempfile,json
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.warehouse_wb_initial_valuation_smoke import seed,basis,payload,DAY,NOW
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.warehouse_functional import WarehouseFunctionalBlock,_line_from_payload
from packages.application import wb_forward_valuation as forward
from packages.application.ff_pool_documents import ensure_ff_pool_document_schema


def old_rows(db):
    with sqlite3.connect(db) as c:
        return c.execute('SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE nm_id=200 AND as_of_date<=? ORDER BY as_of_date,nm_id',(DAY,)).fetchall()


def main():
    with tempfile.TemporaryDirectory(prefix='wb-forward-') as tmp:
        runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        block=WarehouseFunctionalBlock(runtime=runtime,timestamp_factory=lambda:DAY+'T09:00:00Z')
        seed(block)
        block.timestamp_factory=lambda:NOW
        with patch('packages.application.wb_initial_fbs_cost_sources.capture',return_value=basis(block._active_version_id())):
            plan=block._build_plan(kind='hourly_wb_sync',wb_payload=payload())
            block.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'])
        with sqlite3.connect(runtime.db_path) as c:
            ensure_ff_pool_document_schema(c)
            values=dict(document_id='receipt',request_id='request',document_role='root',document_kind='china_acceptance',root_document_id='receipt',operation_id='operation',source_system='supplier_registry',source_type='china_acceptance_form',source_id='supplier',source_revision='1',idempotency_epoch=1,actor='fixture',business_date=DAY,posted_manifest_sha256='sha256:fixture',posted_manifest_json='{}',posted_at=NOW)
            c.execute('INSERT INTO sheet_vitrina_v1_ff_pool_documents('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
            c.execute("INSERT INTO sheet_vitrina_v1_ff_pool_document_lines VALUES('receipt',1,'receipt','accepted_pool_allocation',NULL,'FBS',200,2,'200','0','{}')")
        before=old_rows(runtime.db_path)
        day='2026-10-09';now=day+'T10:00:00Z';block.timestamp_factory=lambda:now
        request=dict(request_id='fixture-forward-1',day=day,shipment_id='supplier',receipt_id='receipt',nm_ids=[200])
        allocation=dict(blockers=[],financial_readiness={'ready':True},lines=[dict(nm_id=200,quantity='2',unit_cost_rub='120')])
        with patch('packages.application.operator_supplier_cost_proof.numerical_supplier_state',return_value=({'source_fingerprint':'sha256:source'},allocation)):
            plan=forward.prepare_plan(block,request=request,wb_payload=payload('1','0',now))
            line=next(r for r in plan['lines'] if r['nm_id']==200)
            assert Decimal(line['wac_rub'])==195 and Decimal(line['capital_rub'])==195
            assert not line['certified'] and line['quality']==forward.QUALITY
            with sqlite3.connect(runtime.db_path) as c:
                c.row_factory=sqlite3.Row
                changed=deepcopy(allocation);changed['lines'][0]['unit_cost_rub']='121'
                with patch('packages.application.operator_supplier_cost_proof.numerical_supplier_state',return_value=({'source_fingerprint':'sha256:changed'},changed)):
                    try:forward.revalidate_plan(c,plan)
                    except ValueError:pass
                    else:raise AssertionError('supplier drift admitted')
            result=block.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'])
            assert next(r for r in result['wb_valuation']['provisional'] if r['nm_id']==200)['quality']==forward.QUALITY
            assert block.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'])['idempotent']
        assert old_rows(runtime.db_path)==before
        original=next(r for r in result['balances'] if r['nm_id']==200)['provenance']['initial_valuation_anchor']
        for day,physical,transit in [('2026-10-09','2','1'),('2026-10-10','1','1')]:
            now=day+'T11:00:00Z';block.timestamp_factory=lambda:now
            repeat=block._build_plan(kind='hourly_wb_sync',wb_payload=payload(physical,transit,now))
            line=next(r for r in repeat['lines'] if r['nm_id']==200)
            assert Decimal(line['wac_rub'])==195
            assert line['quality']==forward.QUALITY and not line['certified']
            block.apply_plan(repeat,confirm_fingerprint=repeat['plan_fingerprint'])
            assert old_rows(runtime.db_path)==before
            assert line['provenance']['initial_valuation_anchor']==original
        with sqlite3.connect(runtime.db_path) as c:
            assert c.execute('SELECT count(*) FROM sheet_vitrina_v1_warehouse_functional_events WHERE event_type=?',(forward.EVENT,)).fetchone()[0]==1
        with sqlite3.connect(runtime.db_path) as c:
            c.row_factory=sqlite3.Row
            try:forward.revalidate_plan(c,plan)
            except ValueError as exc:assert 'boundary' in str(exc)
            else:raise AssertionError('same-day or stale admission accepted')
            altered=deepcopy(repeat['historical_wb_cost_projection'])
            for row in altered:
                if row['nm_id']==104 and row['as_of_date']<=DAY:row['capital_rub']='999'
            forward.protected_history_floor(c,altered,[])
            earlier=deepcopy(next(r for r in altered if r['nm_id']==200));earlier['as_of_date']='2026-06-30';altered.append(earlier)
            try:forward.protected_history_floor(c,altered,[])
            except ValueError as exc:assert 'keys' in str(exc)
            else:raise AssertionError('extra old key admitted')
        receipt=dict(event_id='concurrent-receipt',event_type='wb_final_acceptance',source_id='receipt-2',source_fingerprint='sha256:r2',business_date='2026-10-09',nm_id=200,quantity='2',capital_rub='300',provenance={})
        concurrent=block._build_post_cutover_daily_cost_projection(captured_at=repeat['captured_at'],candidate_lines=[_line_from_payload(r) for r in repeat['lines']],candidate_snapshot=repeat['wb_snapshot'],new_events=[receipt],opening_cost_map=[],cutover_mode=False,allow_partial=True)
        assert Decimal(next(r for r in concurrent if r['nm_id']==200 and r['as_of_date']=='2026-10-10')['wac_rub'])==Decimal('172.5')
        event=plan['new_events'][-1]
        for qty,wac in [('0','175'),('3','175'),('2','176')]:
            try:forward.validate_event(event,day=request['day'],nm_id=200,opening_quantity=qty,opening_wac=wac,anchor=original)
            except ValueError:pass
            else:raise AssertionError('changed opening accepted')
        broken=deepcopy(event);broken['capital_rub']='41'
        try:forward.check_identity(event,broken)
        except ValueError:pass
        else:raise AssertionError('conflicting identity accepted')
        negative=forward.make_event(request_id='negative',day='2026-10-11',nm_id=200,opening_quantity='3',opening_wac='195',old_supplier_unit='120',new_supplier_unit='110',source_evidence={'fixture':True},anchor=original)
        assert negative['capital_rub']=='-30.00'
        forward.validate_event(negative,day='2026-10-11',nm_id=200,opening_quantity='3.000',opening_wac='195.000',anchor=original)
    print('forward WB synthetic native publication, next day, idempotency, old rows: PASS')

if __name__=='__main__':main()
