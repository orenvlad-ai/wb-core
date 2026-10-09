#!/usr/bin/env python3
"""Exact WB source selection, zero evidence and read-only boundaries."""
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_UP, localcontext
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.shared_sku_cost_sources import capture_wb_component, _digest, _json
from packages.application.warehouse_functional import WarehouseLine, _line_payload

P = "sheet_vitrina_v1_"
DAY = "2026-09-07"


def fixture(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        CREATE TABLE {P}warehouse_functional_versions(
            version_id,cutover_id,status,business_effective_date,published_at,created_at,
            effective_at,plan_fingerprint,source_watermarks_json);
        CREATE TABLE {P}warehouse_wb_snapshots(
            snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,pagination_complete,
            page_count,page_offsets_json,raw_row_count,raw_rows_digest,raw_rows_json,items_json,created_at);
        CREATE TABLE {P}warehouse_functional_balances(
            version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,cost_covered_quantity,
            quality,certified,wb_quantity,wb_in_way_to_client,wb_in_way_from_client,provenance_json);
    """)
    add_version(conn, "v1", DAY)
    conn.commit()
    return conn


def add_version(conn, version_id, day, capital="1200", published_at=None):
    fetched = day + "T10:00:00Z"
    published_at = published_at or day + "T10:01:00Z"
    raw = [{"nmId": 1, "warehouseId": 2, "stockCount": 7, "inWayToClient": 3, "inWayFromClient": 2}]
    items = [{"nm_id": 1, "quantity": "7", "in_way_to_client": "3",
              "in_way_from_client": "2", "wb_contour_quantity": "12"},
             {"nm_id": 2, "quantity": "0", "in_way_to_client": "0",
              "in_way_from_client": "0", "wb_contour_quantity": "0"}]
    watermark = {"snapshot_id": "source-" + version_id, "digest": _digest(raw),
                 "fetched_at": fetched, "pagination_complete": True, "raw_row_count": 1, "requested_count": 2}
    conn.execute(f"INSERT INTO {P}warehouse_functional_versions VALUES(?,?,?,?,?,?,?,?,?)",
                 (version_id, "warehouse_functional_cutover_v1", "good", day, published_at,
                  published_at, fetched, _digest(version_id), _json({"wb_snapshot": watermark})))
    conn.execute(f"INSERT INTO {P}warehouse_wb_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("stored-" + version_id, version_id, fetched, day, "[1,2]", 1, 1, "[0]", 1,
                  _digest(raw), _json(raw), _json(items), published_at))
    provenance = {"source_records": [{"source": "official_wb_snapshot", "snapshot_date": day,
                                      "snapshot_id": "source-" + version_id, "fetched_at": fetched}]}
    conn.execute(f"INSERT INTO {P}warehouse_functional_balances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (version_id, "wb", 1, "12", str(int(capital) // 12), capital, "12",
                  "periodic_snapshot_wac_provisional", 0, "7", "3", "2", _json(provenance)))
    # These other stages must neither affect the result nor be prerequisites.
    conn.execute(f"INSERT INTO {P}warehouse_functional_balances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (version_id, "ff_to_wb", 1, "999", "999", "998001", "999", "exact", 1, "0", "0", "0", "{}"))
    conn.execute(f"INSERT INTO {P}warehouse_functional_balances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (version_id, "ff", 1, "987654321", "666", "657777777786", "987654321", "legacy", 1, "0", "0", "0", "{}"))


def check_exact_date_and_zero(directory):
    path = Path(directory) / "authority.sqlite3"
    conn = fixture(path)
    before = path.read_bytes()
    result = capture_wb_component(path, day=DAY, nm_ids=[1, 2])
    assert result["complete"] and result["authority_complete"], result
    assert result["rows"][0]["quantity"] == 12
    assert result["rows"][0]["capital_rub"] == "1200"
    assert result["rows"][0]["components"] == {"physical": 7, "to_customer": 3, "from_customer": 2}
    zero = result["rows"][1]
    assert zero["status"] == "available" and zero["quantity"] == 0 and zero["capital_rub"] == "0"
    assert zero["source"]["basis"] == "explicit_dense_zero_in_linked_complete_wb_snapshot"
    assert path.read_bytes() == before
    assert not Path(str(path) + "-wal").exists() and not Path(str(path) + "-journal").exists()
    assert capture_wb_component(path, day=DAY, nm_ids=[2, 1])["source_digest"] == result["source_digest"]
    new_sku = capture_wb_component(path, day=DAY, nm_ids=[1, 2, 3])
    assert new_sku["authority_complete"] and not new_sku["complete"]
    assert new_sku["rows"][2]["quantity"] is None
    assert new_sku["rows"][2]["reason"] == "sku_outside_exact_wb_snapshot"
    absent_day = capture_wb_component(path, day="2026-09-08", nm_ids=[1, 2])
    assert not absent_day["complete"] and absent_day["rows"][0]["quantity"] is None
    add_version(conn, "v2", DAY, capital="2400", published_at=DAY + "T11:00:00Z")
    add_version(conn, "tomorrow", "2026-09-08", capital="3600")
    conn.commit()
    assert capture_wb_component(path, day=DAY, nm_ids=[1, 2])["version_id"] == "v2"
    pinned = capture_wb_component(path, day=DAY, nm_ids=[1, 2], version_id="v1")
    assert pinned["source_digest"] == result["source_digest"]
    assert not capture_wb_component(path, day=DAY, nm_ids=[1], version_id="tomorrow")["complete"]
    conn.close()


def check_invalid_authority(directory):
    mutations = {
        "foreign_snapshot": f"UPDATE {P}warehouse_wb_snapshots SET version_id='foreign'",
        "partial_snapshot": f"UPDATE {P}warehouse_wb_snapshots SET pagination_complete=0",
        "foreign_date": f"UPDATE {P}warehouse_wb_snapshots SET snapshot_date='2026-09-06'",
        "raw_digest_drift": f"UPDATE {P}warehouse_wb_snapshots SET raw_rows_json='[]'",
        "missing_zero_item": f"UPDATE {P}warehouse_wb_snapshots SET items_json=json_remove(items_json,'$[1]')",
        "watermark_drift": f"UPDATE {P}warehouse_functional_versions SET source_watermarks_json='{{}}'",
    }
    for name, sql in mutations.items():
        path = Path(directory) / (name + ".sqlite3")
        conn = fixture(path)
        conn.execute(sql)
        conn.commit()
        before = path.read_bytes()
        result = capture_wb_component(path, day=DAY, nm_ids=[1, 2])
        assert not result["authority_complete"] and not result["complete"], (name, result)
        assert all(row["quantity"] is None for row in result["rows"]), (name, result)
        assert path.read_bytes() == before
        conn.close()


def check_invalid_sku_cost(directory):
    mutations = {
        "uncovered": "cost_covered_quantity='11'",
        "missing_cost": "wac_rub=NULL",
        "capital_drift": "capital_rub='1201'",
        "quantity_drift": "quantity='13'",
        "component_drift": "wb_quantity='6'",
        "foreign_evidence": "provenance_json='{}'",
        "negative_cost": "capital_rub='-1'",
        "fallback": "quality='fallback'",
        "fallback_average": "quality='fallback_average'",
        "missing_cost_basis": "quality='zero_quantity_without_cost_basis'",
        "mixed_fallback": "quality='mixed:periodic_snapshot_wac_provisional,fallback_average'",
    }
    for name, sql in mutations.items():
        path = Path(directory) / (name + ".sqlite3")
        conn = fixture(path)
        conn.execute(f"UPDATE {P}warehouse_functional_balances SET {sql} WHERE warehouse_key='wb'")
        conn.commit()
        result = capture_wb_component(path, day=DAY, nm_ids=[1, 2])
        assert result["authority_complete"] and not result["complete"], (name, result)
        assert result["rows"][0]["quantity"] == 12, (name, result)
        assert result["rows"][0]["capital_rub"] is None, (name, result)
        assert result["rows"][0]["components"] == {"physical": 7, "to_customer": 3, "from_customer": 2}
        assert result["rows"][1]["status"] == "available", (name, result)
        conn.close()


def check_existing_wb_quality_and_no_legacy_reads(directory):
    path = Path(directory) / "published-wb.sqlite3"
    conn = fixture(path)
    conn.execute(f"UPDATE {P}warehouse_functional_balances SET quality='business_approved_archival_estimate' WHERE warehouse_key='wb'")
    conn.commit()
    result = capture_wb_component(path, day=DAY, nm_ids=[1, 2])
    assert result["complete"] and result["rows"][0]["quality"] == "business_approved_archival_estimate"
    # No inventory history, mutable WB daily cache, FBS lifecycle, pool balance,
    # supplier, legacy FF ledger or archival fallback table even exists here.
    conn.execute(f"DELETE FROM {P}warehouse_functional_balances WHERE warehouse_key='wb'")
    conn.commit()
    result = capture_wb_component(path, day=DAY, nm_ids=[1, 2])
    assert not result["complete"]
    assert result["rows"][0]["reason"] == "positive_wb_snapshot_without_valuation"
    assert result["rows"][1]["status"] == "available"
    conn.close()


def check_empty_complete_snapshot(directory):
    path = Path(directory) / "empty-complete.sqlite3"
    conn = fixture(path)
    items = [{"nm_id": nm_id, "quantity": "0", "in_way_to_client": "0",
              "in_way_from_client": "0", "wb_contour_quantity": "0"} for nm_id in (1, 2)]
    conn.execute(f"UPDATE {P}warehouse_wb_snapshots SET raw_rows_json='[]',raw_row_count=0,"
                 "raw_rows_digest=?,items_json=?", (_digest([]), _json(items)))
    watermark = json.loads(conn.execute(
        f"SELECT source_watermarks_json FROM {P}warehouse_functional_versions").fetchone()[0])
    watermark["wb_snapshot"].update(raw_row_count=0, digest=_digest([]))
    conn.execute(f"UPDATE {P}warehouse_functional_versions SET source_watermarks_json=?", (_json(watermark),))
    conn.execute(f"DELETE FROM {P}warehouse_functional_balances WHERE warehouse_key='wb'")
    conn.commit()
    result = capture_wb_component(path, day=DAY, nm_ids=[1, 2])
    assert result["complete"], result
    assert all(row["quantity"] == 0 and row["capital_rub"] == "0" for row in result["rows"])
    conn.close()


# Reduced arithmetic counterexamples only: no production identities or payloads.
# Each exact publisher division is canonical, but its inverse multiplication
# loses a fractional tail (positive and negative differences are represented).
DECIMAL_CASES = (
    (4, "107.9027227678571428571428572", "431.6108910714285714285714286"),
    (8, "111.7389567718141870684243566", "893.9116541745134965473948525"),
    (15, "115.5751882337526205450733753", "1733.627823506289308176100629"),
    (7, "127.0839014574898785425101215", "889.5873102024291497975708502"),
    (4, "130.9201257075471698113207547", "523.6805028301886792452830189"),
    (4, "130.9201228429546865301055245", "523.6804913718187461204220981"),
    (5, "134.756367287399130974549969", "673.7818364369956548727498448"),
    (7, "124.5264008709717556302102775", "871.6848060968022894114719423"),
    (6, "132.1988674123788217747949292", "793.1932044742729306487695749"),
)


def decimal_fixture(path):
    conn = fixture(path)
    raw, items = [], []
    conn.execute(f"DELETE FROM {P}warehouse_functional_balances WHERE warehouse_key='wb'")
    for nm, (q, expected_wac, capital) in enumerate(DECIMAL_CASES, 1):
        provenance = {"source_records": [{"source":"official_wb_snapshot", "snapshot_id":"source-v1",
            "snapshot_date":DAY, "fetched_at":DAY+'T10:00:00Z'}]}
        line=WarehouseLine('wb',nm,Decimal(q),Decimal(capital),Decimal(q),
            'snapshot_fbs_wb_initial_provisional',provenance,wb_quantity=Decimal(q))
        with localcontext() as ctx:
            ctx.prec=28;ctx.rounding=ROUND_HALF_EVEN
            saved=_line_payload(line)  # Real publisher serialization, not hand-written operands.
            assert saved['wac_rub']==expected_wac and saved['capital_rub']==capital
            assert q*Decimal(saved['wac_rub'])!=Decimal(capital)  # Old guard rejects every case.
        raw.append({'nmId':nm,'warehouseId':2,'stockCount':q,'inWayToClient':0,'inWayFromClient':0})
        items.append({'nm_id':nm,'quantity':str(q),'in_way_to_client':'0','in_way_from_client':'0',
            'wb_contour_quantity':str(q)})
        conn.execute(f"INSERT INTO {P}warehouse_functional_balances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ('v1','wb',nm,saved['quantity'],saved['wac_rub'],saved['capital_rub'],saved['cost_covered_quantity'],
             saved['quality'],saved['certified'],saved['wb_quantity'],saved['wb_in_way_to_client'],
             saved['wb_in_way_from_client'],_json(saved['provenance'])))
    watermark={'snapshot_id':'source-v1','digest':_digest(sorted(raw,key=_json)),'fetched_at':DAY+'T10:00:00Z',
        'pagination_complete':True,'raw_row_count':len(raw),'requested_count':len(raw)}
    conn.execute(f"UPDATE {P}warehouse_functional_versions SET source_watermarks_json=?",(_json({'wb_snapshot':watermark}),))
    conn.execute(f"UPDATE {P}warehouse_wb_snapshots SET requested_nm_ids_json=?,raw_row_count=?,raw_rows_digest=?,raw_rows_json=?,items_json=?",
        (_json(list(range(1,10))),len(raw),watermark['digest'],_json(raw),_json(items)))
    conn.commit()
    return conn


def check_decimal_roundtrip_and_totals(directory):
    from apps.fbs_snapshot_cost_smoke import capture
    from packages.application.fbs_snapshot_cost import fingerprint, initialize_candidate
    from packages.application.shared_sku_cost import build_shared_cost_day
    from packages.application.fbs_inventory_presentation import FbsInventorySnapshot, COST
    path=Path(directory)/'decimal-roundtrip.sqlite3'
    conn=decimal_fixture(path);before=path.read_bytes()
    source=capture_wb_component(path,day=DAY,nm_ids=list(range(1,10)))
    assert source['complete'] and source['authority_complete'],source
    assert [r['capital_rub'] for r in source['rows']]==[case[2] for case in DECIMAL_CASES]
    # Reader invariance under caller precision AND rounding, with no source writes.
    for prec, rounding in ((6,ROUND_UP),(50,ROUND_UP),(160,ROUND_HALF_EVEN)):
        with localcontext() as ctx:
            ctx.prec=prec;ctx.rounding=rounding
            assert capture_wb_component(path,day=DAY,nm_ids=list(range(1,10)))==source
    assert path.read_bytes()==before
    initial=capture(quantity='0',extra=[{'nm_id':nm,'facility_id':'ff-1','quantity':'0'} for nm in range(2,10)])
    initial['baseline_costs']['rows']=[{'nm_id':nm,'facility_id':'ff-1','wac_rub':'100','source':{'version_id':'synthetic-opening'}} for nm in range(1,10)]
    initial['source_digest']=fingerprint(initial)
    state=initialize_candidate(initial)
    retained={'date':DAY,'wb_version_id':'v1','rows':{}}
    for row in source['rows']:
        stages={s:{'quantity':'0','capital_rub':'0','wac_rub':None} for s in
            ('PRODUCTION','PRODUCTION_TO_FF','FF_TO_WB','WB_ACCEPTANCE_DISCREPANCY')}
        stages['WB']={'quantity':row['quantity'],'capital_rub':row['capital_rub'],'wac_rub':DECIMAL_CASES[row['nm_id']-1][1]}
        retained['rows'][str(row['nm_id'])]={'stages':stages,'identity':{}}
    retained['source_digest']=fingerprint(retained)
    period=build_shared_cost_day(state,source,DAY)
    assert period['quality']=='complete' and all(r['status']=='resolved' for r in period['rows'].values())
    snapshot=FbsInventorySnapshot(fbs_state=state,wb_capture=source,retained=retained,day=DAY)
    with localcontext() as ctx:
        ctx.prec=50
        capital=sum((Decimal(case[2]) for case in DECIMAL_CASES),Decimal(0))
        quantity=sum(case[0] for case in DECIMAL_CASES)
        totals=snapshot.payload()['totals']
        assert Decimal(totals['shared_cost']['capital_rub'])==capital
        assert Decimal(totals['total']['capital_rub'])==capital
        assert Decimal(snapshot.metrics()['total_'+COST])==capital/quantity
    # A material invalid WB valuation still keeps TOTAL money null, with facts retained.
    conn.execute(f"UPDATE {P}warehouse_functional_balances SET capital_rub='432' WHERE warehouse_key='wb' AND nm_id=1")
    conn.commit()
    invalid=capture_wb_component(path,day=DAY,nm_ids=list(range(1,10)))
    assert not invalid['complete'] and invalid['rows'][0]['reason']=='wb_published_wac_capital_mismatch'
    partial=FbsInventorySnapshot(fbs_state=state,wb_capture=invalid,retained=retained,day=DAY)
    assert partial.payload()['totals']['total']['capital_rub'] is None
    assert partial.metrics()['total_'+COST] is None
    assert partial.payload()['rows']['2']['shared_cost']['status']=='resolved'
    assert partial.payload()['totals']['total']['quantity']==str(quantity)
    conn.close()


def check_decimal_negative_guards(directory):
    mutations={
        'capital_tamper':("capital_rub='431.6208910714285714285714286'",'wb_published_wac_capital_mismatch'),
        'wac_last_digit_tamper':("wac_rub='107.9027227678571428571428573'",'wb_published_wac_capital_mismatch'),
        'quantity_tamper':("quantity='5'",'wb_balance_snapshot_quantity_mismatch'),
        'component_tamper':("wb_quantity='3'",'wb_balance_snapshot_quantity_mismatch'),
        'coverage_tamper':("cost_covered_quantity='3'",'wb_cost_coverage_incomplete'),
        'provenance_tamper':("provenance_json='{}'",'wb_balance_snapshot_provenance_mismatch'),
    }
    for name,(sql,reason) in mutations.items():
        path=Path(directory)/(name+'.sqlite3');conn=decimal_fixture(path)
        conn.execute(f"UPDATE {P}warehouse_functional_balances SET {sql} WHERE warehouse_key='wb' AND nm_id=1")
        conn.commit();before=path.read_bytes()
        source=capture_wb_component(path,day=DAY,nm_ids=list(range(1,10)))
        assert source['authority_complete'] and not source['complete'],(name,source)
        bad=source['rows'][0]
        assert bad['reason']==reason and bad['capital_rub'] is None and bad['quantity']==4,(name,bad)
        assert all(r['status']=='available' for r in source['rows'][1:]),name
        assert path.read_bytes()==before
        conn.close()

def main():
    with TemporaryDirectory(prefix="shared-sku-wb-source-") as directory:
        check_exact_date_and_zero(directory)
        check_invalid_authority(directory)
        check_invalid_sku_cost(directory)
        check_existing_wb_quality_and_no_legacy_reads(directory)
        check_empty_complete_snapshot(directory)
        check_decimal_roundtrip_and_totals(directory)
        check_decimal_negative_guards(directory)
    print("shared SKU WB saved-source smoke: ok")


if __name__ == "__main__":
    main()
