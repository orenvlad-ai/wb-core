#!/usr/bin/env python3
"""LOCAL native posted receipt + persisted six-stage historical editions.

All data are synthetic. Posting, WB source reader, native compact read models
and business projection publisher are real; no runtime or production opens.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.fbs_accounting_historical_revision_smoke import native_writer_fixture, END
from apps.fbs_accounting_historical_cohort_smoke import posted_requests
from packages.application import fbs_accounting_historical_stages as stages
from packages.application import fbs_accounting_historical_revision as revision
from packages.application import ready_publication as ready
from packages.application.fbs_snapshot_cost import canonical,fingerprint
from packages.application.shared_sku_cost_sources import capture_wb_component, _digest
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot,RETAINED_STAGES
from packages.application.sheet_vitrina_v1_own_product_capital import own_stage_metric_key
from packages.application.warehouse_functional import ensure_warehouse_functional_schema, _materialize_compact_warehouse_read_models
from packages.application.warehouse_business_projection import ensure_warehouse_business_projection_schema,ensure_functional_version_business_time_schema,_metric_rows
from packages.application.ff_pool_documents import DOCUMENTS_TABLE
from packages.application.fbs_snapshot_cost_sources import capture_current
from packages.application.fbs_accounting_historical_sources import augment_native_requests


def native_stages_fixture(db, *, end=END,service_receipt=False):
    book,cap,receipt,_=native_writer_fixture(db,end=end,receipt_source_type=None if service_receipt else "china_acceptance_form",receipt_source_id="selected-shipment",service_receipt=service_receipt)
    saved_dates=sorted(book["state"]["periods"])
    days=[*saved_dates,end]
    with closing(sqlite3.connect(db)) as conn,conn:
        conn.row_factory=sqlite3.Row
        # The stock-reader fixture uses deliberately minimal functional tables;
        # replace only those LOCAL tables with the actual native schema.
        for name in ("functional_versions","functional_balances","functional_active","business_projection_current_rows"):
            conn.execute("DROP TABLE IF EXISTS sheet_vitrina_v1_warehouse_"+name)
        ensure_warehouse_functional_schema(conn);ensure_warehouse_business_projection_schema(conn);ensure_functional_version_business_time_schema(conn)
        posted_requests(conn)
        for day in days:
            version="native-saved:"+day
            stamp=day+"T12:00:00Z"
            raw=[dict(nmId=1,warehouseId=2,stockCount=500,inWayToClient=0,inWayFromClient=0)]
            items=[dict(nm_id=1,quantity="500",in_way_to_client="0",in_way_from_client="0",wb_contour_quantity="500"),
                   dict(nm_id=2,quantity="0",in_way_to_client="0",in_way_from_client="0",wb_contour_quantity="0")]
            watermark=dict(snapshot_id="source:"+day,digest=_digest(raw),fetched_at=stamp,pagination_complete=True,raw_row_count=1,requested_count=2)
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (version,"warehouse_functional_cutover_v1","hourly_wb_sync",stamp,"good",fingerprint(version),fingerprint(raw),canonical(dict(wb_snapshot=watermark,captured_at=day+"T11:00:00Z")),stamp,day,stamp))
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("snapshot:"+day,version,stamp,day,"[1,2]",1,1,"[0]",1,_digest(raw),canonical(raw),canonical(items),stamp))
            lines=[]
            def add(stage,q,c,sources,wb="0",nm=1):
                from decimal import Decimal
                row=dict(warehouse_key=stage,nm_id=nm,quantity=q,capital_rub=c,wac_rub=str(Decimal(c)/Decimal(q)) if Decimal(q) else None,cost_covered_quantity=q,
                         quality="saved_native_exact",certified=0,wb_quantity=wb,wb_in_way_to_client="0",wb_in_way_from_client="0",provenance=dict(source_records=sources))
                lines.append(row)
                conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (version,stage,nm,q,row["wac_rub"],c,q,row["quality"],0,wb,"0","0",canonical(row["provenance"])))
            add("wb","500","100000",[dict(source="official_wb_snapshot",snapshot_id=watermark["snapshot_id"],snapshot_date=day,fetched_at=stamp)],wb="500")
            add("wb","0","0",[dict(source="official_wb_snapshot",snapshot_id=watermark["snapshot_id"],snapshot_date=day,fetched_at=stamp)],nm=2)
            add("ff","2000","300000",[dict(source="current_facility_pool_exact_projection",business_date=day,
                locations=[dict(facility_id="A",pool="FBS",nm_id=1,quantity="1000",capital_rub="100000"),dict(facility_id="B",pool="FBS",nm_id=1,quantity="1000",capital_rub="200000")])])
            add("china_to_ff","1010","200500",[dict(shipment_id="selected-shipment",supplier_flow_id="selected-flow",source_fingerprint="sha256:local-selected",flow_quantity="1000",flow_capital_rub="200000"),
                                                       dict(shipment_id="foreign-shipment",supplier_flow_id="foreign-flow",source_fingerprint="sha256:local-foreign",flow_quantity="10",flow_capital_rub="500")])
            _materialize_compact_warehouse_read_models(conn,version_id=version,plan=dict(lines=lines),created_at=stamp,effective_at=stamp,business_effective_date=day)
            from packages.application.warehouse_business_projection import publish_functional_version_business_projection
            publish_functional_version_business_projection(conn,published_version_id=version,business_effective_date=day,published_at=stamp,source_revision=fingerprint(['LOCAL original native edition',day]))
        conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,?,?)",("native-saved:"+end,end+"T12:00:00Z"))
    # Explicit LOCAL archived complete official generations. The real stock
    # reader constructs each dated quantity operand; no missing source becomes
    # zero and the pure planner fixture's simplified snapshots are not reused.
    quantities={}
    with closing(sqlite3.connect(db)) as stock_conn:
        stock_conn.row_factory=sqlite3.Row
        generation=dict(stock_conn.execute('SELECT * FROM sheet_vitrina_v1_wb_fbs_warehouse_registry_runs LIMIT 1').fetchone())
        runs=[dict(r) for r in stock_conn.execute('SELECT * FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_runs')]
        stock_rows=[dict(r) for r in stock_conn.execute('SELECT * FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_rows')]
        for sequence,day in enumerate(days,2):
            g={**generation,'run_id':'history-generation:'+day,'run_sequence':sequence,'generation_digest':fingerprint([day,generation]),'started_at':day+'T13:55:00Z','completed_at':day+'T13:56:00Z'}
            stock_conn.execute('INSERT INTO sheet_vitrina_v1_wb_fbs_warehouse_registry_runs('+','.join(g)+') VALUES('+','.join('?' for _ in g)+')',tuple(g.values()))
            for old in runs:
                row={**old,'run_id':'history-stock:'+day+':'+old['run_id'],'registry_run_id':g['run_id'],'snapshot_at':day+'T13:55:00Z','source_digest':fingerprint([day,old])}
                stock_conn.execute('INSERT INTO sheet_vitrina_v1_wb_fbs_stock_snapshot_runs('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
                for item in stock_rows:
                    if item['run_id']!=old['run_id']:continue
                    line={**item,'run_id':row['run_id']}
                    stock_conn.execute('INSERT INTO sheet_vitrina_v1_wb_fbs_stock_snapshot_rows('+','.join(line)+') VALUES('+','.join('?' for _ in line)+')',tuple(line.values()))
            stock_conn.commit();stock_conn.execute('PRAGMA query_only=ON');stock_conn.execute('BEGIN')
            quantities[day]=capture_current(db,now=datetime.fromisoformat(day+'T14:00:00+00:00'),include_baseline=False,connection=stock_conn)['quantity_snapshot']
            stock_conn.rollback();stock_conn.execute('PRAGMA query_only=OFF')
            assert quantities[day]['complete']
    for day in saved_dates:book['state']['periods'][day]['snapshot']=quantities[day]
    with ready.readonly(db) as conn:
        for day in days:
            wb=capture_wb_component(db,day=day,nm_ids=[1,2],version_id="native-saved:"+day,connection=conn)
            assert wb["complete"],wb
            balances=stages._version_balances(conn,version_id="native-saved:"+day)
            metrics=_metric_rows(balances,affected_nm_ids=[1,2])
            # Native projection closes all stage zeros for catalog-only SKU2.
            rows={}
            for nm in [1,2]:
                row=metrics.get(nm) or _metric_rows([dict(warehouse_key="ff",nm_id=2,quantity="0",capital_rub="0",wac_rub=None,cost_covered_quantity="0",quality="empty",certified=0,provenance={})],affected_nm_ids=[2])[2]
                rows[str(nm)]=dict(stages={s:dict(quantity=str(row["metrics"][own_stage_metric_key(s,"qty")]),capital_rub=str(row["metrics"][own_stage_metric_key(s,"capital_rub")]),wac_rub=row["metrics"][own_stage_metric_key(s,"unit_cost_rub")]) for s in RETAINED_STAGES},reason="",identity={})
            retained=dict(date=day,wb_version_id=wb["version_id"],rows=rows);retained["source_digest"]=fingerprint(retained)
            if day==end:cap["current_dated_inputs"]=dict(wb_capture=wb,retained_capture=retained)
            else:
                book["wb_days"][day]=wb;book["retained_days"][day]=retained
                book["shared_days"][day]=build_shared_cost_day(book["state"],wb,day)
                book["presentations"][day]=FbsInventorySnapshot(fbs_state=book["state"],wb_capture=wb,retained=retained,day=day).payload()
        fresh=capture_current(db,now=datetime.fromisoformat(end+"T14:00:00+00:00"),include_baseline=False,connection=conn)
        fresh["posted_manifest_json_by_id"]={r[0]:r[1] for r in conn.execute(f"SELECT document_id,posted_manifest_json FROM {DOCUMENTS_TABLE}")}
        fresh["current_dated_inputs"]=cap["current_dated_inputs"]
        fresh=augment_native_requests(conn,fresh,document_ids=[receipt])
    plan=revision.build_historical_revision_plan(book,fresh,receipt_document_ids=[receipt])
    assert plan["status"]=="ready",plan
    with ready.readonly(db) as conn:
        dated=stages.capture_dated_stages(conn,db_path=db,book=plan["candidate_book"],dates=plan["scope"]["dates"])
    return book,fresh,plan,dated


class NativeHistoricalStages(unittest.TestCase):
    def fixture(self,**kwargs):
        temp=TemporaryDirectory(prefix="historical-native-stages-");self.addCleanup(temp.cleanup)
        self.db=Path(temp.name)/"local.sqlite3"
        return native_stages_fixture(self.db,**kwargs)

    def test_exact_shipment_removed_foreign_source_and_old_versions_unchanged(self):
        book,cap,plan,dated=self.fixture()
        before=deepcopy((book,cap,plan,dated))
        result=stages.build_dated_stage_revision(plan,capture=cap,dated=dated)
        day=plan["scope"]["date_from"]
        transit=result["candidate_book"]["presentations"][day]["rows"]["1"]["stages"]["PRODUCTION_TO_FF"]
        self.assertEqual(transit["quantity"],"10");self.assertEqual(transit["capital_rub"],"500")
        proof=result["editions"][day]["supplier_revision"][0]
        self.assertEqual(proof["removed_quantity"],"1000");self.assertEqual(proof["removed_capital_rub"],"200000")
        self.assertEqual((book,cap,plan,dated),before)
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.row_factory=sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            original=list(conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances ORDER BY version_id,warehouse_key,nm_id"))
            active=conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_active").fetchall()
            published=stages.publish_dated_stages(conn,manifest=result)
            self.assertEqual(set(published),set(plan["scope"]["dates"]))
            self.assertEqual(conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_active").fetchall(),active)
            for row in original:self.assertEqual(conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? AND warehouse_key=? AND nm_id=?",tuple(row[:3])).fetchone(),row)
        with ready.readonly(self.db) as conn:
            for day in plan["scope"]["dates"]:
                wb=capture_wb_component(self.db,day=day,nm_ids=[1,2],version_id=result["editions"][day]["version_id"],connection=conn)
                self.assertEqual(wb,result["candidate_book"]["wb_days"][day])

    def test_missing_saved_payload_fail_closed(self):
        book,cap,plan,dated=self.fixture()
        with closing(sqlite3.connect(self.db)) as conn,conn:conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_read_models WHERE warehouse_key='production'")
        with ready.readonly(self.db) as conn:
            with self.assertRaisesRegex(ValueError,"historical_complete_stage_models_missing"):
                stages.capture_dated_stages(conn,db_path=self.db,book=plan["candidate_book"],dates=plan["scope"]["dates"])

    def test_precise_source_conservation_block(self):
        book,cap,plan,dated=self.fixture()
        forged=deepcopy(dated)
        forged["dates"][plan["scope"]["date_from"]]["balances"][0]["capital_rub"]="200501"
        forged["source_digest"]=fingerprint(forged["dates"])
        with self.assertRaisesRegex(ValueError,"historical_supplier_source_conservation_failed"):
            stages.build_dated_stage_revision(plan,capture=cap,dated=forged)

    def test_thirty_one_native_editions_not_closed_backlog(self):
        book,cap,plan,dated=self.fixture(end="2026-10-08")
        result=stages.build_dated_stage_revision(plan,capture=cap,dated=dated)
        self.assertEqual(len(result["editions"]),31)
        self.assertEqual(result["candidate_book"]["presentations"]["2026-09-08"]["rows"]["1"]["stages"]["PRODUCTION_TO_FF"]["capital_rub"],"500")

    def test_actual_native_frozen_dispatch_and_wb_acceptance_applicability(self):
        book,cap,plan,dated=self.fixture()
        # LOCAL real native append API: monetary snapshot is serialized on its
        # immutable debit line, not borrowed from today's book WAC.
        from apps.fbs_accounting_historical_history_smoke import complete_local_metadata
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        complete_local_metadata(self.db)
        runtime=RegistryUploadDbBackedRuntime(self.db.parent)
        # StoreRegistry's native default filename is used by its append API.
        native_path=runtime.db_path
        if native_path!=self.db:
            self.db.rename(native_path);self.db=native_path
        runtime.create_ff_stock_operation(operation_id='LOCAL frozen dispatch',operation_type='writeoff',source_type='wb_supply',source_key='LOCAL immutable debit',source_object_id='LOCAL supply',
            created_at='2026-09-09T10:00:00Z',business_effective_date='2026-09-09',lines=[dict(nm_id=1,quantity_delta=-5,
                raw=dict(cost_snapshot=dict(unit_cost_rub='100',capital_delta_rub='-500',quality='native exact',provenance={'fixture':'explicit frozen native money'})))])
        record=dict(supply_id='LOCAL supply',wb_supply_id='',ff_wac_at_ledger_debit_rub='100',flow_quantity='5',flow_capital_rub='550',downstream_pre_acceptance_addon_rub='10')
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.row_factory=sqlite3.Row
            for day in ('2026-09-09','2026-09-10'):
                v='native-saved:'+day
                conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,cost_covered_quantity,quality,certified,wb_quantity,wb_in_way_to_client,wb_in_way_from_client,provenance_json) VALUES(?,'ff_to_wb',1,'5','110','550','5','native exact',0,'0','0','0',?)",(v,canonical(dict(source_records=[record]))))
                balances=stages._version_balances(conn,version_id=v)
                _materialize_compact_warehouse_read_models(conn,version_id=v,plan=dict(lines=balances),created_at=day+'T12:00:00Z',effective_at=day+'T12:00:00Z',business_effective_date=day)
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_events VALUES('LOCAL acceptance','native-saved:2026-09-10','wb_final_acceptance','LOCAL supply:1','sha256:LOCAL event','2026-09-10',1,'1','110',?,'2026-09-10T10:00:00Z')",(canonical(record),))
        with ready.readonly(self.db) as conn:
            for day in plan['scope']['dates']:
                wb=capture_wb_component(self.db,day=day,nm_ids=[1,2],version_id='native-saved:'+day,connection=conn)
                retained=deepcopy(book['retained_days'].get(day,cap['current_dated_inputs']['retained_capture']))
                if day>='2026-09-09':retained['rows']['1']['stages']['FF_TO_WB'].update(quantity='5',capital_rub='550',wac_rub='110')
                retained['source_digest']=fingerprint({k:v for k,v in retained.items() if k!='source_digest'})
                if day==plan['scope']['date_to']:cap['current_dated_inputs']=dict(wb_capture=wb,retained_capture=retained)
                else:
                    book['wb_days'][day]=wb;book['retained_days'][day]=retained
                    book['shared_days'][day]=build_shared_cost_day(book['state'],wb,day)
                    book['presentations'][day]=FbsInventorySnapshot(fbs_state=book['state'],wb_capture=wb,retained=retained,day=day).payload()
            plan=revision.build_historical_revision_plan(book,cap,receipt_document_ids=plan['receipt_document_ids'])
            self.assertEqual(plan['status'],'ready')
            dated=stages.capture_dated_stages(conn,db_path=self.db,book=plan['candidate_book'],dates=plan['scope']['dates'])
        result=stages.build_dated_stage_revision(plan,capture=cap,dated=dated)
        self.assertEqual(result['editions']['2026-09-09']['dispatch_applicability'][0]['applicability'],'exact_frozen_native_money')
        self.assertEqual(result['editions']['2026-09-10']['wb_acceptance_applicability'][0]['applicability'],'exact_frozen_native_money')
        self.assertEqual(result['candidate_book']['retained_days']['2026-09-09']['rows']['1']['stages']['FF_TO_WB']['capital_rub'],'550')
        forged=deepcopy(dated);proof=next(iter(forged['dates']['2026-09-09']['debit_proofs'].values()));proof.update(kind='proportional_native_aggregate',frozen={});forged['source_digest']=fingerprint(forged['dates'])
        with self.assertRaisesRegex(ValueError,'historical_dispatch_exact_debit_basis_required'):
            stages.build_dated_stage_revision(plan,capture=cap,dated=forged)


if __name__=="__main__":unittest.main()
