"""Exact dated native stage editions for an admitted receipt revision.

Old functional versions and original snapshots are immutable. This adapter
consumes complete saved stage payloads, never a current supplier calculator.
Preparation is query-only; the publication seam requires its caller's owned
transaction and an exact prepared source fence.
"""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal
import json
import sqlite3
from contextlib import closing

from packages.application.fbs_snapshot_cost import fingerprint, canonical, candidate_period_view
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot, RETAINED_STAGES
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application.shared_sku_cost_sources import capture_wb_component
from packages.application.sheet_vitrina_v1_own_product_capital import own_stage_metric_key
from packages.application.warehouse_business_projection import _version_balances, _metric_rows
from packages.application import ready_publication as ready

CONTRACT = "fbs_accounting_historical_native_stages_v1"
P = "sheet_vitrina_v1_warehouse_"
STAGES = {"PRODUCTION":"production", "PRODUCTION_TO_FF":"china_to_ff", "FF":"ff",
          "FF_TO_WB":"ff_to_wb", "WB":"wb", "WB_ACCEPTANCE_DISCREPANCY":"wb_acceptance_discrepancy"}
MAX_ROWS = 100000
MAX_BYTES = 64 * 1024**2


def require(value, code, *, day="", source=""):
    if not value:
        raise ValueError(code + (":" + day if day else "") + (":" + source if source else ""))


def decimal(value):
    require(value is not None and value != "" and not isinstance(value, bool), "historical_stage_operand_missing")
    result = Decimal(str(value))
    require(result.is_finite() and result >= 0, "historical_stage_operand_invalid")
    return result


def _native_debit(conn, *, record, nm_id, day, queries):
    supply_ids=sorted({str(record.get(k) or '') for k in ('supply_id','wb_supply_id')}-{''})
    require(supply_ids,'historical_dispatch_source_identity_missing',day=day)
    args=tuple([*supply_ids,nm_id])
    sql=("SELECT o.*,l.* FROM sheet_vitrina_v1_ff_stock_operations o JOIN sheet_vitrina_v1_ff_stock_operation_lines l ON l.operation_id=o.operation_id "
        f"WHERE o.source_object_id IN ({','.join('?' for _ in supply_ids)}) AND l.nm_id=? AND CAST(l.quantity_delta AS NUMERIC)<0 ORDER BY o.created_at,o.operation_id,l.line_no")
    rows=[dict(r) for r in conn.execute(sql,args)]
    require(len(rows)==1,'historical_dispatch_exact_debit_missing_or_ambiguous',day=day,source=supply_ids[0])
    from packages.application.warehouse_functional import _ff_ledger_line_cost_snapshot
    frozen=_ff_ledger_line_cost_snapshot(rows[0])
    if frozen:require(decimal(record['ff_wac_at_ledger_debit_rub'])==frozen['unit_cost_rub'],'historical_dispatch_frozen_basis_mismatch',day=day)
    queries.append((sql,args))
    return dict(kind='frozen_native_money' if frozen else 'proportional_native_aggregate',line=rows[0],
        frozen={k:format(v,'f') if isinstance(v,Decimal) else v for k,v in (frozen or {}).items()})


def _applicability(proof, *, first, day, source):
    debit_day=str(proof['line'].get('business_effective_date') or proof['line'].get('business_date') or proof['line']['created_at'])[:10]
    require(debit_day<first or proof['kind']=='frozen_native_money','historical_dispatch_exact_debit_basis_required',day=day,source=source)
    return 'pre_revision_dispatch' if debit_day<first else 'exact_frozen_native_money'


def capture_dated_stages(conn, *, db_path, book, dates):
    require(conn.in_transaction and conn.execute("PRAGMA query_only").fetchone()[0] == 1,
            "historical_stage_query_only_required")
    result, queries = {}, []
    for day in dates:
        wb = book["wb_days"][day]
        version_id = wb["version_id"]
        version = conn.execute(f"SELECT * FROM {P}functional_versions WHERE version_id=?", (version_id,)).fetchone()
        require(version and version["status"] == "good" and version["business_effective_date"] == day,
                "historical_saved_functional_version_missing", day=day, source=version_id)
        nm_ids = sorted(int(nm) for nm in book["retained_days"][day]["rows"])
        exact = capture_wb_component(db_path, day=day, nm_ids=nm_ids, version_id=version_id, connection=conn)
        require(exact == wb and exact["complete"], "historical_saved_wb_authority_changed", day=day, source=version_id)
        count,size=conn.execute(f'SELECT count(*),coalesce(sum(length(provenance_json)+length(capital_rub)+length(quantity)),0) FROM {P}functional_balances WHERE version_id=?',(version_id,)).fetchone()
        require(count<=MAX_ROWS and size<=MAX_BYTES,'historical_saved_stage_size_limit',day=day)
        count,size=conn.execute(f'SELECT count(*),coalesce(sum(length(payload_json)),0) FROM {P}functional_read_models WHERE version_id=?',(version_id,)).fetchone()
        require(count==6 and size<=MAX_BYTES,'historical_complete_stage_models_missing',day=day)
        balances = _version_balances(conn, version_id=version_id)
        require(len(balances) <= MAX_ROWS, "historical_stage_row_limit", day=day)
        require(len({(r["warehouse_key"],r["nm_id"]) for r in balances}) == len(balances), "historical_stage_duplicate_row", day=day)
        models = [dict(row) for row in conn.execute(f"SELECT * FROM {P}functional_read_models WHERE version_id=? ORDER BY warehouse_key", (version_id,))]
        require({r["warehouse_key"] for r in models} == set(STAGES.values()), "historical_complete_stage_models_missing", day=day)
        for model in models:
            payload = json.loads(model["payload_json"])
            expected = {int(r["nm_id"]):(decimal(r["quantity"]),decimal(r["capital_rub"]))
                        for r in balances if r["warehouse_key"] == model["warehouse_key"]}
            observed = {int(r["nm_id"]):(decimal(r["quantity"]),decimal(r["capital_rub"])) for r in payload.get("balances",[])}
            require(payload.get("status") == "ready" and observed == expected and len(observed) == len(payload.get("balances",[]))
                    and decimal(payload["warehouse"]["total_quantity"]) == sum((v[0] for v in expected.values()),Decimal(0))
                    and decimal(payload["warehouse"]["total_capital_rub"]) == sum((v[1] for v in expected.values()),Decimal(0)),
                    "historical_stage_complete_payload_mismatch", day=day, source=model["warehouse_key"])
        metrics = _metric_rows(balances, affected_nm_ids=nm_ids)
        retained = book["retained_days"][day]
        for nm in nm_ids:
            require(str(nm) in retained["rows"], "historical_stage_catalog_mismatch", day=day)
            for stage in RETAINED_STAGES:
                item = retained["rows"][str(nm)]["stages"][stage]
                for key, field in (("quantity","qty"),("capital_rub","capital_rub")):
                    raw = metrics[nm]["metrics"][own_stage_metric_key(stage,field)]
                    observed = decimal(item[key])
                    if raw is None and not any(int(r["nm_id"])==nm for r in balances):
                        explicit_wb=next(r for r in wb["rows"] if int(r["nm_id"])==nm)
                        require(explicit_wb["status"]=="available" and decimal(explicit_wb["quantity"])==0 and decimal(explicit_wb["capital_rub"])==0,
                                "historical_catalog_zero_source_unproven",day=day,source=str(nm))
                        # All six exact closed-world stage payloads were proven
                        # above; official WB also publishes this SKU's zero.
                        raw=0
                    require(raw is not None and observed == Decimal(str(raw)), "historical_retained_native_operand_mismatch", day=day, source=stage)
        snapshots = [dict(r) for r in conn.execute(f"SELECT * FROM {P}wb_snapshots WHERE version_id=?", (version_id,))]
        require(len(snapshots) == 1, "historical_exact_snapshot_missing", day=day)
        companions={}
        for table,order in (('functional_ff_reservations','supply_id,nm_id'),('supplier_cost_states','shipment_id'),('unmatched_doprinato','unmatched_id')):
            name=P+table
            sql=f'SELECT * FROM {name} WHERE version_id=? ORDER BY {order}'
            require(conn.execute(f'SELECT count(*) FROM {name} WHERE version_id=?',(version_id,)).fetchone()[0]<=MAX_ROWS,'historical_stage_companion_row_limit',day=day)
            companions[table]=[dict(r) for r in conn.execute(sql,(version_id,))]
            require(len(companions[table])<=MAX_ROWS,'historical_stage_companion_row_limit',day=day)
            queries.append((sql,(version_id,)))
        debit_proofs = {}
        for balance in balances:
            if balance["warehouse_key"] != "ff_to_wb": continue
            for record in balance["provenance"].get("source_records") or []:
                debit_proofs[fingerprint([balance['nm_id'],record])]=_native_debit(conn,record=record,nm_id=balance['nm_id'],day=day,queries=queries)
        # The WB pool may already contain post-revision FF dispatches that no
        # longer appear in open transit. Their exact saved acceptance events
        # must have the same immutable debit authority as retained transit.
        wb_events=[]
        for nm in nm_ids:
            sql=f"SELECT * FROM {P}functional_events WHERE event_type='wb_final_acceptance' AND nm_id=? AND business_date>=? AND business_date<=? AND created_at<=? ORDER BY business_date,event_id"
            args=(nm,min(dates),day,version['published_at'] or version['created_at'])
            count=conn.execute(f'SELECT count(*) FROM ({sql})',args).fetchone()[0]
            require(count<=MAX_ROWS,'historical_wb_acceptance_source_limit',day=day)
            events=[dict(r) for r in conn.execute(sql,args)];queries.append((sql,args))
            for event in events:
                record=json.loads(event['provenance_json'])
                proof=_native_debit(conn,record=record,nm_id=nm,day=day,queries=queries)
                wb_events.append(dict(event=event,native_debit=proof))
        result[day] = {"version":dict(version),"snapshot":snapshots[0],"balances":balances,"read_models":models,"debit_proofs":debit_proofs,"companions":companions,'wb_acceptance_proofs':wb_events}
        require(len(canonical(result).encode()) <= MAX_BYTES, "historical_dated_stage_byte_limit")
        queries.extend((f"SELECT * FROM {P}{table} WHERE version_id=? ORDER BY {order}", (version_id,))
                       for table,order in (("functional_versions","version_id"),("wb_snapshots","snapshot_id"),
                                           ("functional_balances","warehouse_key,nm_id"),("functional_read_models","warehouse_key")))
    return {"dates":result,"query_fence":json.loads(canonical(ready.pin_queries(conn,queries))),"source_digest":fingerprint(result)}


def check_query_fence(conn,expected):
    # Recovery reads JSON lists; normalize only representation, never SQL,
    # parameters or a pinned digest supplied by the capture.
    ready.check_queries(conn,[(sql,tuple(params),digest) for sql,params,digest in expected])


def _supplier_revision(balances, *, day, receipts):
    """Remove only a newly accepted shipment; retain every foreign source."""
    result, evidence = deepcopy(balances), []
    for receipt in receipts:
        if receipt["kind"] != "china_acceptance" or receipt["business_date"] > day or receipt["source_type"] not in {"china_acceptance_workbook","china_acceptance_form"}:
            continue
        shipment = receipt["source_id"]
        selected = [r for r in result if r["warehouse_key"] in {"production","china_to_ff"}]
        before, removed = {}, {}
        for row in selected:
            sources = row["provenance"].get("source_records")
            require(isinstance(sources,list), "historical_supplier_source_records_missing", day=day, source=shipment)
            q = sum((decimal(s["flow_quantity"]) for s in sources),Decimal(0))
            c = sum((decimal(s["flow_capital_rub"]) for s in sources),Decimal(0))
            require(q == decimal(row["quantity"]) and c == decimal(row["capital_rub"]), "historical_supplier_source_conservation_failed", day=day, source=shipment)
            kept, taken = [s for s in sources if s.get("shipment_id") != shipment], [s for s in sources if s.get("shipment_id") == shipment]
            for source in taken:
                identity = (row["nm_id"],source.get("supplier_flow_id"),source.get("source_fingerprint"))
                require(identity not in removed, "historical_supplier_source_duplicate", day=day, source=shipment)
                removed[identity] = {"stage":row["warehouse_key"],"source":deepcopy(source)}
            before[(row["warehouse_key"],row["nm_id"])] = fingerprint(sources)
            row["provenance"]["source_records"] = kept
            quantity = sum((decimal(s["flow_quantity"]) for s in kept),Decimal(0))
            capital = sum((decimal(s["flow_capital_rub"]) for s in kept),Decimal(0))
            row.update(quantity=format(quantity,"f"),capital_rub=format(capital,"f"),
                       wac_rub=format(capital/quantity,"f") if quantity else None,cost_covered_quantity=format(quantity,"f"))
        # Zero records are valid only because all six saved closed-world stage
        # payloads were verified. An already accepted shipment has no transit.
        evidence.append({"shipment_id":shipment,"receipt_id":receipt["document_id"],"date":day,
            "before_source_digests":[[list(k),v] for k,v in sorted(before.items())],
            "removed_sources":[v for _,v in sorted(removed.items(),key=lambda item:str(item[0]))],
            "removed_quantity":format(sum((decimal(v["source"]["flow_quantity"]) for v in removed.values()),Decimal(0)),"f"),
            "removed_capital_rub":format(sum((decimal(v["source"]["flow_capital_rub"]) for v in removed.values()),Decimal(0)),"f"),
            "new_receipt_quantity":format(sum((decimal(v["quantity"]) for v in receipt["cost_document"]["lines"] if v["line_role"]=="accepted_pool_allocation"),Decimal(0)),"f"),
            "rule":"shipment_departure_removes_whole_flow_accepted_capital_is_native_receipt_authority"})
    return [r for r in result if r["warehouse_key"] not in {"production","china_to_ff"} or decimal(r["quantity"]) > 0], evidence


def _clone_rows(saved, *, version_id, day, published_at, balances, source_digest):
    version = deepcopy(saved["version"])
    version.update(version_id=version_id,version_kind="historical_receipt_revision",published_at=published_at,
                   created_at=published_at,plan_fingerprint=fingerprint([day,version_id,balances]),local_source_digest=source_digest)
    snapshot = deepcopy(saved["snapshot"])
    snapshot.update(version_id=version_id,snapshot_id="historical-snapshot:"+fingerprint([version_id,snapshot["snapshot_id"]])[7:],created_at=published_at)
    return version,snapshot


def _physical_ff_revision(saved, *, day, documents):
    """Native full physical locations, independent from official availability.

    Native local captured_at records the posted-source frontier. Documents
    already present at that frontier are never applied twice. Their dated
    immutable movement money is used exactly, not an available-stock price.
    """
    watermark=json.loads(saved["version"]["source_watermarks_json"])
    frontier=watermark.get("captured_at")
    require(frontier,"historical_physical_ff_capture_frontier_missing",day=day)
    def timestamp(value):
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z","+00:00"))
    locations={};before_rows={}
    for row in saved["balances"]:
        if row["warehouse_key"]!="ff":continue
        records=row["provenance"].get("source_records") or []
        raw=[loc for record in records for loc in record.get("locations",[])]
        require(raw,"historical_physical_ff_exact_locations_missing",day=day,source=str(row["nm_id"]))
        require(sum((decimal(v["quantity"]) for v in raw),Decimal(0))==decimal(row["quantity"])
                and sum((decimal(v["capital_rub"]) for v in raw),Decimal(0))==decimal(row["capital_rub"]),"historical_physical_ff_location_conservation_failed",day=day)
        before_rows[int(row["nm_id"])]=row
        for loc in raw:
            key=(loc["facility_id"],loc["pool"],int(row["nm_id"]))
            require(key not in locations,"historical_physical_ff_duplicate_location",day=day)
            locations[key]=deepcopy(loc)
    proof=[]
    for doc in sorted(documents,key=lambda d:(d["business_date"],d["posted_at"],d["document_id"])):
        if doc["business_date"]>day:continue
        included=timestamp(doc["posted_at"])<=timestamp(frontier)
        movements=doc["cost_document"]["movements"]
        proof.append({"document_id":doc["document_id"],"posted_at":doc["posted_at"],"capture_frontier":frontier,"already_in_native_frontier":included,"movements":deepcopy(movements)})
        if included:continue
        for movement in movements:
            key=(movement["facility_id"],movement["pool"],int(movement["nm_id"]))
            loc=locations.setdefault(key,{"facility_id":key[0],"pool":key[1],"nm_id":key[2],"quantity":"0","capital_rub":"0"})
            q=decimal(loc["quantity"])+Decimal(str(movement["quantity_delta"]))
            c=decimal(loc["capital_rub"])+Decimal(str(movement["capital_delta_rub"]))
            require(q>=0 and c>=0 and (q==0)==(c==0),"historical_physical_ff_native_movement_not_priceable",day=day,source=doc["document_id"])
            loc.update(quantity=format(q,"f"),capital_rub=format(c,"f"),source_watermark=doc["document_id"])
    result=[]
    nms=sorted({key[2] for key in locations})
    for nm in nms:
        detail=[v for key,v in sorted(locations.items()) if key[2]==nm]
        q=sum((decimal(v["quantity"]) for v in detail),Decimal(0));c=sum((decimal(v["capital_rub"]) for v in detail),Decimal(0))
        if not q:continue
        prior=deepcopy(before_rows.get(nm) or {"warehouse_key":"ff","nm_id":nm,"wb_quantity":"0","wb_in_way_to_client":"0","wb_in_way_from_client":"0"})
        prior.update(quantity=format(q,"f"),capital_rub=format(c,"f"),wac_rub=format(c/q,"f"),cost_covered_quantity=format(q,"f"),quality="exact_dated_native_physical_movements",certified=0,
            provenance={"source_records":[{"source":"historical_native_full_physical_locations","business_date":day,"locations":detail,"native_movement_proof":proof}]})
        result.append(prior)
    return result,proof


def _cloned_wb_capture(version, snapshot, balances, *, day, nm_ids):
    """Run the real saved-WB reader on only the proposed immutable rows.

    This private in-memory DB is a numeric preparation scratch space. No schema
    initializer or runtime constructor executes and no operational file opens.
    """
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        for table,rows in (("functional_versions",[version]),("wb_snapshots",[snapshot]),("functional_balances",balances)):
            keys = list(rows[0]) if rows else []
            if table=="functional_balances":
                keys = ["version_id","warehouse_key","nm_id","quantity","wac_rub","capital_rub","cost_covered_quantity","quality","certified","wb_quantity","wb_in_way_to_client","wb_in_way_from_client","provenance_json"]
                rows = [{**{k:r.get(k) for k in keys},"version_id":version["version_id"],"provenance_json":canonical(r["provenance"])} for r in rows]
            conn.execute(f"CREATE TABLE {P}{table}({','.join(keys)})")
            conn.executemany(f"INSERT INTO {P}{table} VALUES({','.join('?' for _ in keys)})",[[r[k] for k in keys] for r in rows])
        conn.commit();conn.execute("PRAGMA query_only=ON");conn.execute("BEGIN")
        result = capture_wb_component(None,day=day,nm_ids=nm_ids,version_id=version["version_id"],connection=conn)
        require(result["complete"],"historical_cloned_wb_invalid",day=day,source=result["reason"])
        return result


def build_dated_stage_revision(plan, *, capture, dated):
    require(plan["status"] == "ready" and dated["source_digest"] == fingerprint(dated["dates"]), "historical_stage_plan_authority_missing")
    candidate, editions = deepcopy(plan["candidate_book"]), {}
    receipts = [d for d in capture["documents"] if d["document_id"] in plan["receipt_document_ids"]]
    for day in plan["scope"]["dates"]:
        saved = dated["dates"][day]
        balances,evidence = _supplier_revision(saved["balances"],day=day,receipts=receipts)
        # Every non-supplier retained contribution must carry actual source
        # records. Its monetary debit stays frozen only when native evidence
        # explicitly says so; a digest alone does not prove applicability.
        dependency_proof = []
        for row in balances:
            if row["warehouse_key"] != "ff_to_wb": continue
            records = row["provenance"].get("source_records")
            require(isinstance(records,list) and records, "historical_dispatch_source_missing", day=day, source=str(row["nm_id"]))
            require(sum((decimal(r['flow_quantity']) for r in records),Decimal(0))==decimal(row['quantity'])
                and sum((decimal(r['flow_capital_rub']) for r in records),Decimal(0))==decimal(row['capital_rub']),'historical_dispatch_source_conservation_failed',day=day,source=str(row['nm_id']))
            for record in records:
                proof = saved["debit_proofs"].get(fingerprint([row["nm_id"],record]))
                require(proof,"historical_dispatch_exact_debit_basis_required",day=day,source=record.get("supply_id",""))
                # Native transit records do not contain a business_date. The
                # append-only debit's own date is authority for applicability.
                dependency_proof.append({"source":deepcopy(record),"native_debit":deepcopy(proof),"applicability":_applicability(proof,first=plan['scope']['date_from'],day=day,source=record['supply_id'])})
        wb_applicability=[{**deepcopy(p),'applicability':_applicability(p['native_debit'],first=plan['scope']['date_from'],day=day,source=p['event']['source_id'])} for p in saved['wb_acceptance_proofs']]
        selected_ids=set(plan["receipt_document_ids"])|set(plan.get("current_document_ids",[]))|set(plan.get("auxiliary_document_ids",[]))
        native_ff,physical_proof=_physical_ff_revision(saved,day=day,documents=[d for d in capture["documents"] if d["document_id"] in selected_ids])
        balances = [r for r in balances if r["warehouse_key"] != "ff"]
        balances.extend(native_ff)
        balances.sort(key=lambda r:(r["warehouse_key"],r["nm_id"]))
        version_id = "historical-receipt:" + fingerprint([CONTRACT,plan["source_proof"],day,saved,balances])[7:]
        for balance in balances:balance["version_id"]=version_id
        version,snapshot = _clone_rows(saved,version_id=version_id,day=day,published_at=plan["observation"]["captured_at"],balances=balances,source_digest=dated["source_digest"])
        wb = _cloned_wb_capture(version,snapshot,balances,day=day,nm_ids=candidate["wb_days"][day]["requested_nm_ids"])
        retained = deepcopy(candidate["retained_days"][day])
        retained["wb_version_id"] = version_id
        by_key = {(r["warehouse_key"],str(r["nm_id"])):r for r in balances}
        for nm,row in retained["rows"].items():
            for stage in RETAINED_STAGES:
                r = by_key.get((STAGES[stage],nm))
                row["stages"][stage].update(quantity=r["quantity"] if r else "0",capital_rub=r["capital_rub"] if r else "0",wac_rub=r["wac_rub"] if r else None,
                    revision_proof={"contract":CONTRACT,"saved_version":saved["version"]["version_id"],"revised_version":version_id,"source_set_digest":plan["derived_stage_applicability"]["source_set_digest"]})
        retained["source_digest"] = fingerprint({k:v for k,v in retained.items() if k!="source_digest"})
        candidate["retained_days"][day] = retained
        candidate["wb_days"][day] = wb
        candidate["shared_days"][day] = build_shared_cost_day(candidate["state"],wb,day)
        candidate["presentations"][day] = FbsInventorySnapshot(fbs_state=candidate["state"],wb_capture=wb,retained=retained,day=day).payload()
        editions[day] = {"version_id":version_id,"version":version,"snapshot":snapshot,"saved":saved,"balances":balances,"companions":deepcopy(saved["companions"]),"supplier_revision":evidence,"dispatch_applicability":dependency_proof,'wb_acceptance_applicability':wb_applicability,
            "full_physical_ff_proof":physical_proof,"management_ff_basis":"official_fbs_available_plus_document_fbo_book_separate_from_native_full_physical"}
    candidate["historical_revision"]["native_stage_editions"] = {d:v["version_id"] for d,v in editions.items()}
    result = {"contract":CONTRACT,"candidate_book":candidate,"after_book":fingerprint(candidate),"editions":editions,"query_fence":dated["query_fence"],"dated_source_digest":dated["source_digest"]}
    result["manifest_digest"] = fingerprint(result)
    return result


def publish_dated_stages(conn, *, manifest):
    """Append editions in an already-owned transaction after the source CAS."""
    from packages.application.warehouse_functional import _materialize_compact_warehouse_read_models
    from packages.application.warehouse_business_projection import publish_functional_version_business_projection
    require(conn.in_transaction,"historical_stage_publication_transaction_required")
    require(manifest["manifest_digest"]==fingerprint({k:v for k,v in manifest.items() if k!="manifest_digest"}),"historical_stage_manifest_corrupt")
    check_query_fence(conn,manifest["query_fence"])
    receipts={}
    for day,edition in manifest["editions"].items():
        require(not conn.execute(f"SELECT 1 FROM {P}functional_versions WHERE version_id=?",(edition["version_id"],)).fetchone(),"historical_stage_edition_already_exists",day=day)
        for table,row in (("functional_versions",edition["version"]),("wb_snapshots",edition["snapshot"])):
            conn.execute(f"INSERT INTO {P}{table}({','.join(row)}) VALUES({','.join('?' for _ in row)})",tuple(row.values()))
        for balance in edition["balances"]:
            row={**balance,"version_id":edition["version_id"],"provenance_json":canonical(balance["provenance"])}
            row.pop("provenance"); row.pop("line_id",None)
            conn.execute(f"INSERT INTO {P}functional_balances({','.join(row)}) VALUES({','.join('?' for _ in row)})",tuple(row.values()))
        for table,rows in edition['companions'].items():
            for old in rows:
                row={**old,'version_id':edition['version_id']}
                if table=='unmatched_doprinato':row['unmatched_id']='historical-unmatched:'+fingerprint([edition['version_id'],old['unmatched_id']])[7:]
                conn.execute(f'INSERT INTO {P}{table}({",".join(row)}) VALUES({",".join("?" for _ in row)})',tuple(row.values()))
        reservations=[{k:v for k,v in row.items() if k!='version_id'} for row in edition['companions']['functional_ff_reservations']]
        unmatched=[{**row,'provenance':json.loads(row['provenance_json'])} for row in edition['companions']['unmatched_doprinato']]
        _materialize_compact_warehouse_read_models(conn,version_id=edition["version_id"],plan={"plan_kind":edition["version"]["version_kind"],"plan_fingerprint":edition["version"]["plan_fingerprint"],"lines":edition["balances"],"ff_reservations":reservations,"unmatched_doprinato":unmatched},created_at=edition["version"]["created_at"],effective_at=edition["version"]["effective_at"],business_effective_date=day)
        receipts[day]=publish_functional_version_business_projection(conn,published_version_id=edition["version_id"],business_effective_date=day,published_at=edition["version"]["published_at"],source_revision=manifest["manifest_digest"])
    return receipts
