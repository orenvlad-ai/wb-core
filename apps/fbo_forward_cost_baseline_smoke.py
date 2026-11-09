#!/usr/bin/env python3
"""Read-only activation planning and exact SKU aggregation across WB barcodes."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from decimal import Decimal, localcontext
from io import BytesIO
from zipfile import ZipFile
import json
import sqlite3
from tempfile import TemporaryDirectory

from packages.application import fbo_forward_costs as policy
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _connect
from packages.application.fulfillment_services import FulfillmentServicesBlock
from apps.sheet_vitrina_v1_fulfillment_services_smoke import _wb_supply_row, _build_workbook, _valid_row

NOW = '2026-11-10T08:00:00Z'


def supply(identity, goods, *, doprinato=True):
    row = _wb_supply_row(identity, accepted_quantity=0, quantity_added=0, cost_total=0)
    row.update(raw_goods=goods, status_id=3,
               type_label='Допринято' if doprinato else 'Короб')
    return row


def source_fixture(raw):
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
    runtime.save_wb_supply_rows(rows=[
        supply('example1', [
            {'nmID':101, 'barcode':'a', 'quantity':130, 'acceptedQuantity':13},
            {'nmID':101, 'barcode':'b', 'quantity':100, 'acceptedQuantity':10},
            {'nmID':101, 'barcode':'c', 'quantity':80, 'acceptedQuantity':8},
            {'nmID':102, 'barcode':'d', 'quantity':7, 'acceptedQuantity':0},
            {'nmID':102, 'barcode':'e', 'quantity':2},
        ]),
        supply('example2', [{'nmID':101, 'quantity':5, 'acceptedQuantity':4}]),
        supply('1001', [{'nmID':1, 'quantity':10, 'acceptedQuantity':0}], doprinato=False),
    ], warehouses=[], synced_at=NOW)
    return runtime


def doprinato_quantities(plan):
    return {row['identity']:json.loads(row['payload_json'])['quantity']
            for row in plan['rows'] if row['kind']=='doprinato'}


def test_readonly_plan_and_activation():
    with TemporaryDirectory() as raw:
        runtime = source_fixture(raw)
        block = FulfillmentServicesBlock(runtime=runtime, timestamp_factory=lambda:NOW)
        payload = _build_workbook([_valid_row('1001')])
        uploads = []
        for index in range(6):
            distinct_file = BytesIO(payload)
            with ZipFile(distinct_file, 'a') as archive:
                archive.comment = f'legacy invoice {index}'.encode()
            result = block.upload_xlsx(distinct_file.getvalue())
            assert result['validation_status']=='ok'
            uploads.append(result['upload']['upload_id'])
        with _connect(runtime.db_path) as conn:
            originals = {upload:policy._lines(conn,upload) for upload in uploads}
        with sqlite3.connect(Path(runtime.db_path).as_uri()+'?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            before_schema = [tuple(row) for row in conn.execute('SELECT * FROM sqlite_master ORDER BY type,name')]
            conn.execute('BEGIN')
            plan = policy.baseline_preview(conn)
            assert policy.baseline_preview(conn)==plan
            assert conn.total_changes==0
            assert [tuple(row) for row in conn.execute('SELECT * FROM sqlite_master ORDER BY type,name')]==before_schema
            assert not policy.exists(conn,policy.CONFIG) and not policy.exists(conn,policy.BASELINES)
        assert plan['counts']=={'upload':6,'invoice_semantic':6,'supply_packed':3,'doprinato':3},plan['counts']
        assert doprinato_quantities(plan)=={'example1:101':'31','example1:102':'9','example2:101':'4'}
        saved_uploads = {row['identity']:json.loads(row['payload_json']) for row in plan['rows'] if row['kind']=='upload'}
        assert saved_uploads==originals,'all six original invoice baselines retained exactly'
        for row in plan['rows']:
            if row['kind']=='invoice_semantic':
                payload = json.loads(row['payload_json'])
                assert payload['economic_digest']==policy._economic_identity(originals[payload['upload_id']])
        with _connect(runtime.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            result = policy.activate(conn,NOW,expected_plan=plan)
            assert result['baseline_plan_digest']==plan['plan_digest']
            assert result['baseline_counts']==plan['counts']
            actual = [dict(row) for row in conn.execute(f'SELECT * FROM {policy.BASELINES} ORDER BY kind,identity')]
            assert actual==plan['rows']
            total_changes = conn.total_changes
            assert policy.activate(conn,NOW,expected_plan=plan)['status']=='already_active'
            assert conn.total_changes==total_changes and conn.execute(f'SELECT COUNT(*) FROM {policy.EFFECTS}').fetchone()[0]==0
            # A later matched total starts above the full31-unit historical floor,
            # never above one of the old barcode variants (13,10,8).
            from packages.application.warehouse_functional import reconcile_discrepancies
            audit = []
            reconcile_discrepancies(discrepancies=[{'nm_id':101,'quantity':'36','capital':'360'}],
                doprinato=[{'source_id':'example1:101','nm_id':101,'quantity':str(qty)} for qty in (18,10,8)],audit=audit)
            assert len(audit)==3
            events = policy.doprinato_events(audit,{'baselines':actual},posted_at=NOW,business_date=NOW[:10])
            assert len(events)==1 and Decimal(events[0]['quantity'])==5 and Decimal(events[0]['capital_rub'])==50
            assert policy.doprinato_events(audit,{'baselines':actual,'doprinato_events':events},posted_at=NOW,business_date=NOW[:10])==[]



def producer_audit(discrepancies, supplies):
    """The warehouse producer preserves each barcode under one supply/SKU key."""
    from packages.application.warehouse_stocks import _normalized_wb_record, _validated_wb_goods
    from packages.application.warehouse_functional import reconcile_discrepancies
    doprinato = []
    for identity, goods in supplies:
        record = _normalized_wb_record({'supply_id':identity,'status_id':5,
            'normalized':{'type_label':'Допринято'},'raw_goods':goods})
        for good in _validated_wb_goods(record):
            accepted = policy.number(good.get('accepted_quantity'))
            quantity = accepted if accepted>0 else policy.number(good.get('quantity'))
            doprinato.append({'source_id':f"{identity}:{good['nm_id']}", 'nm_id':good['nm_id'],
                'quantity':str(quantity), 'source_fingerprint':policy.digest(good),
                'business_date':NOW[:10], 'provenance':{'supply_id':identity,'barcode':good['barcode']}})
    audit=[]
    balances,unmatched = reconcile_discrepancies(discrepancies=discrepancies,doprinato=doprinato,audit=audit)
    return audit,balances,unmatched


def barcode_goods(nm, quantities):
    return [{'nmID':nm,'barcode':f'barcode-{index}','quantity':quantity,'acceptedQuantity':quantity}
            for index,quantity in enumerate(quantities)]


def forward_events(audit, state, *, accepted=()):
    return policy.doprinato_events(audit,state,posted_at=NOW,business_date=NOW[:10],accepted_events=accepted)


def test_real_multibarcode_producer():
    baseline={'baselines':[{'kind':'doprinato','identity':'example1:101','payload_json':'{"quantity":"31"}'}]}
    original,_,_=producer_audit([{'nm_id':101,'quantity':'31','capital':'310'}],
        [('example1',barcode_goods(101,[13,10,8]))])
    assert len(original)==3 and forward_events(original,baseline)==[]
    audit,balances,unmatched=producer_audit([{'nm_id':101,'quantity':'36','capital':'360'},
        {'nm_id':102,'quantity':'4','capital':'80'}],
        [('example1',barcode_goods(101,[18,10,8])+barcode_goods(102,[3,1]))])
    assert len(audit)==5 and balances==[] and unmatched==[]
    events=forward_events(audit,baseline)
    by_nm={row['nm_id']:row for row in events}
    assert (Decimal(by_nm[101]['quantity']),Decimal(by_nm[101]['capital_rub']))==(5,50)
    assert (Decimal(by_nm[102]['quantity']),Decimal(by_nm[102]['capital_rub']))==(4,80)
    evidence=by_nm[101]['provenance']['doprinato_barcode_matches']
    assert {row['source_fingerprint'] for row in evidence}=={row['source_fingerprint'] for row in audit if row['nm_id']==101}
    assert forward_events(list(reversed(audit)),baseline)==events,'aggregation and provenance order deterministic'
    posted={**baseline,'doprinato_events':events}
    assert forward_events(audit,posted)==[]
    other,_,_=producer_audit([{'nm_id':101,'quantity':'2','capital':'60'}],
        [('example2',barcode_goods(101,[1,1]))])
    separate=forward_events(other,posted)
    assert len(separate)==1 and separate[0]['source_id']=='example2:101' and Decimal(separate[0]['capital_rub'])==60
    # The original category absorbs all five already posted supplemental units.
    folded,_,_=producer_audit([{'nm_id':101,'quantity':'31','capital':'310'}],
        [('example1',barcode_goods(101,[18,10,8]))])
    assert forward_events(folded,posted)==[],'unexplained shrinkage cannot authorize a reversal'
    offsets=forward_events(folded,posted,accepted=[{'nm_id':101,'quantity':'5','capital_rub':'50'}])
    assert len(offsets)==1 and Decimal(offsets[0]['quantity'])==-5 and Decimal(offsets[0]['capital_rub'])==-50
    assert forward_events(folded,{**posted,'doprinato_events':events+offsets},accepted=[{'nm_id':101,'quantity':'5'}])==[]


def test_partial_unknown_and_exact_aggregation():
    baseline={'baselines':[{'kind':'doprinato','identity':'example1:101','payload_json':'{"quantity":"31"}'}]}
    audit,balances,unmatched=producer_audit([{'nm_id':101,'quantity':'34','capital':'340'}],
        [('example1',barcode_goods(101,[18,10,8]))])
    assert balances==[] and Decimal(unmatched[0]['quantity'])==2
    events=forward_events(audit,baseline)
    assert len(events)==1 and Decimal(events[0]['quantity'])==3 and Decimal(events[0]['capital_rub'])==30
    # One fully unmatched barcode has wac=None but no matched cost operand.
    audit,_,unmatched=producer_audit([{'nm_id':101,'quantity':'5','capital':'50'}],
        [('example1',barcode_goods(101,[5,3]))])
    assert audit[1]['matched_wac_rub'] is None and Decimal(audit[1]['matched_quantity'])==0
    assert Decimal(forward_events(audit,{'config':{}})[0]['capital_rub'])==50
    # Any positively matched unknown portion blocks the entire new SKU delta.
    unknown,_,_=producer_audit([{'nm_id':101,'quantity':'3','capital':'0','cost_covered_quantity':'0'}],
        [('example1',barcode_goods(101,[3]))])
    mixed=[audit[0]]+unknown
    assert forward_events(mixed,{'config':{}})==[]
    partial,_,_=producer_audit([{'nm_id':101,'quantity':'36','capital':'180','cost_covered_quantity':'18'}],
        [('example1',barcode_goods(101,[18,10,8]))])
    assert forward_events(partial,baseline)==[]
    # Summation preserves exact source amounts rather than trusting rounded WAC.
    exact=[{'source_id':'example3:103','nm_id':103,'matched_quantity':'1',
        'matched_wac_rub':'10','matched_capital_rub':value} for value in ('10.000000001','20.000000002')]
    with localcontext() as context:
        context.prec=3
        aggregated=policy._aggregate_doprinato_audit(exact)[0]
    assert Decimal(aggregated['matched_quantity'])==2
    assert Decimal(aggregated['matched_capital_rub'])==Decimal('30.000000003')


def test_drift_and_invalid_sources_before_mutation():
    for bad in ('changed','NaN','-1'):
        with TemporaryDirectory() as raw:
            runtime = source_fixture(raw)
            with _connect(runtime.db_path) as conn:
                expected = policy.baseline_preview(conn)
                row = conn.execute("SELECT raw_goods_json FROM sheet_vitrina_v1_wb_supplies WHERE supply_id='example1'").fetchone()
                goods = json.loads(row[0])
                goods[0]['acceptedQuantity'] = '14' if bad=='changed' else bad
                conn.execute("UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=? WHERE supply_id='example1'",(json.dumps(goods),))
                conn.commit()
                before = list(conn.iterdump())
                changes = conn.total_changes
                if bad!='changed':
                    try:
                        policy.baseline_preview(conn)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError('invalid quantity passed the preclaim preview')
                try:
                    policy.activate(conn,NOW,expected_plan=expected)
                except ValueError as exc:
                    if bad=='changed':
                        assert str(exc)=='fbo_forward_cost_baseline_source_changed'
                else:
                    raise AssertionError('changed source was activated')
                assert conn.total_changes==changes
                assert list(conn.iterdump())==before,'source-drift rejection must precede even schema writes'
    with localcontext() as context:
        context.prec=3
        assert policy._sum_baseline_quantities([Decimal('123456789.123456789'),Decimal('0.000000001')])==Decimal('123456789.123456790')


def test_plan_tampering_before_mutation():
    with TemporaryDirectory() as raw:
        runtime = source_fixture(raw)
        with _connect(runtime.db_path) as conn:
            plan = policy.baseline_preview(conn)
            plan['rows'][0]['payload_json']='{}'
            before=list(conn.iterdump())
            try:
                policy.activate(conn,NOW,expected_plan=plan)
            except ValueError as exc:
                assert str(exc)=='invalid_fbo_forward_cost_baseline_plan'
            else:
                raise AssertionError('modified plan accepted')
            assert list(conn.iterdump())==before


if __name__=='__main__':
    test_readonly_plan_and_activation()
    test_real_multibarcode_producer()
    test_partial_unknown_and_exact_aggregation()
    test_drift_and_invalid_sources_before_mutation()
    test_plan_tampering_before_mutation()
    print('FBO baseline: 13+10+8=31; source choice and keys isolated; six invoice baselines exact; read-only preview; source drift rejected before writes; future delta5/50; OK')
