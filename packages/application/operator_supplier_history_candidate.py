"""Independently rebuild bounded supplier-owned dated cost editions.

The supplier source is already saved. This module never edits its ledger,
receipt/debit documents, official quantities or archived accounting versions.
"""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import hashlib,json

from packages.application import fbs_accounting_runtime as accounting
from packages.application import fbs_accounting_historical_stages as stages
from packages.application import operator_supplier_history_sources as sources
from packages.application import operator_supplier_processing as processing
from packages.application import ready_publication as ready
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot

CONTRACT = "supplier_cost_dated_candidate_v1"
MAX_BYTES = 160*1024**2
MAX_COHORT = 32
MAX_ATTEMPTS = 32
require = sources.require


def code_authority():
    from packages.application import warehouse_functional, warehouse_historical_recovery, warehouse_business_projection
    from packages.application import historical_dated_inputs
    modules = (sources, warehouse_functional, warehouse_historical_recovery, warehouse_business_projection, historical_dated_inputs)
    paths = [Path(m.__file__) for m in modules] + [Path(__file__), Path(__file__).with_name("operator_supplier_history.py"), Path(stages.__file__), Path(processing.__file__)]
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def business_image(rows):
    """Physical and monetary output; source provenance has separate exact pins."""
    from packages.application.warehouse_functional import _decimal, _text
    numbers = {"quantity", "wac_rub", "capital_rub", "cost_covered_quantity", "wb_quantity", "wb_in_way_to_client", "wb_in_way_from_client"}
    return [{key: (_text(_decimal(row[key])) if key in numbers and row.get(key) is not None else row.get(key)) for key in ("warehouse_key", "nm_id", "quantity", "wac_rub",
        "capital_rub", "cost_covered_quantity", "quality", "certified", "wb_quantity",
        "wb_in_way_to_client", "wb_in_way_from_client")} for row in sorted(rows,key=lambda r:(r["warehouse_key"],r["nm_id"]))]


def _wb_rows(rows, *, daily, day, ids):
    """Only the actual correlated native daily cost can replace WB money."""
    result = deepcopy(rows)
    by_nm = {int(r["nm_id"]): r for r in daily if r["as_of_date"] == day}
    evidence = []
    for row in result:
        if row["warehouse_key"] != "wb" or row["nm_id"] not in ids:
            continue
        operand = by_nm.get(row["nm_id"])
        require(operand, "dated_wb_cost_operand_missing")
        quantity = Decimal(str(row["quantity"]))
        require(Decimal(operand["quantity"]) == quantity, "dated_wb_quantity_changed")
        if quantity == 0:
            require(Decimal(str(row["capital_rub"])) == 0, "dated_wb_zero_money_invalid")
            continue
        wac = Decimal(operand["wac_rub"])
        require(wac > 0 and quantity*wac == Decimal(operand["capital_rub"]), "dated_wb_cost_not_conserved")
        if Decimal(str(row["capital_rub"])) != Decimal(operand["capital_rub"]):
            row.update(capital_rub=operand["capital_rub"],wac_rub=operand["wac_rub"],
                       cost_covered_quantity=str(quantity),quality=operand["quality"])
            row["provenance"] = {**row["provenance"], "supplier_dated_cost_operand": deepcopy(operand)}
            evidence.append(deepcopy(operand))
    return result, evidence


def native_cohort(conn, operation_id, *, pinned=None):
    """Exact current publisher membership, closed under overlapping SKU sets."""
    from packages.application.operator_supplier_cost_proof import read_correlated_native
    root=read_correlated_native(conn,operation_id);version=root['functional']['version_id']
    ids=set(root['functional']['queue_ref']['affected_nm_ids']);members={operation_id:root}
    if pinned is not None:
        require(0<len(pinned)<=MAX_COHORT and len({r['operation_id'] for r in pinned})==len(pinned),'cohort_size_limit')
        members={r['operation_id']:read_correlated_native(conn,r['operation_id']) for r in pinned}
        require(all(processing.ref(members[r['operation_id']]['operation'])==r for r in pinned),'cohort_source_changed')
    else:
        while True:
            sql=(f'SELECT DISTINCT p.operation_id FROM {processing.FUNCTIONAL} p LEFT JOIN {processing.COMPLETIONS} c USING(operation_id) '
                 f'LEFT JOIN {processing.ATTEMPTS} a USING(operation_id) WHERE p.version_id=? AND c.operation_id IS NULL AND COALESCE(a.reason,\'\')<>? AND EXISTS(SELECT 1 FROM json_each(p.proof_json,\'$.queue_ref.affected_nm_ids\') WHERE value IN ('+','.join('?' for _ in ids)+')) ORDER BY p.operation_id LIMIT ?')
            rows=list(conn.execute(sql,(version,processing.SUPERSEDED,*sorted(ids),MAX_COHORT+1)))
            require(len(rows)<=MAX_COHORT,'cohort_size_limit')
            prior=set(members)
            for row in rows:
                op=processing.operation(conn,row[0])
                if not processing.current(conn,op):continue
                native=read_correlated_native(conn,row[0]);members[row[0]]=native
                ids.update(native['functional']['queue_ref']['affected_nm_ids'])
            if set(members)==prior:break
    require(all(m['functional']['version_id']==version for m in members.values()),'cohort_native_publication_pending')
    require(len(members)<=MAX_COHORT,'cohort_size_limit')
    return [members[k] for k in sorted(members)]


def attempt_identity(natives):
    return fingerprint([CONTRACT,[(processing.ref(m['operation']),m['functional']['version_id'],m['functional']['queue_ref']) for m in natives]])[7:]


def retained_obligations(conn, natives, *, runtime, pinned=None):
    """Native persisted attempts are obligations, never borrowed History ack."""
    from packages.application import operator_supplier_history as history
    refs=[processing.ref(m['operation']) for m in natives];attempt=attempt_identity(natives)
    if pinned is None:
        rows={}
        for ref in refs:
            found=list(conn.execute("SELECT * FROM sheet_vitrina_v1_ready_publications p WHERE kind=? AND attempt_id<>? AND EXISTS(SELECT 1 FROM json_each(p.inputs_json,'$.candidate.cohort_refs') m WHERE json_extract(m.value,'$.operation_id')=? AND json_extract(m.value,'$.source_digest')=?) ORDER BY created_at,operation_id,attempt_id LIMIT ?",(history.CONTRACT,attempt,ref['operation_id'],ref['source_digest'],MAX_ATTEMPTS+1)))
            require(len(found)<=MAX_ATTEMPTS,'attempt_scope_limit')
            rows.update({(r['operation_id'],r['attempt_id']):dict(r) for r in found})
        require(len(rows)<=MAX_ATTEMPTS,'attempt_scope_limit')
    else:
        require(len(pinned)<=MAX_ATTEMPTS,'attempt_scope_limit');rows={}
        for saved in pinned:
            row=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(saved['operation_id'],saved['attempt_id'])).fetchone()
            require(row,'prior_attempt_missing');rows[(row['operation_id'],row['attempt_id'])]=dict(row)
    result=[]
    for key,row in sorted(rows.items()):
        if pinned is None and history.verified_retirement(conn,row):continue
        if pinned is None and 'supplier_history_ack' in json.loads(row['diagnostics_json']):
            require(history.completed_row(row),'prior_history_ack_corrupt')
            continue
        manifest=json.loads(row['inputs_json']);history._verify(manifest)
        require(row['kind']==history.CONTRACT and row['state'] in ('prepared','complete'),'prior_attempt_state_unknown')
        require(manifest['source_ref'] in manifest['candidate']['cohort_refs'] and (row['operation_id'],row['attempt_id'],row['expected_book'],row['book_operation_id'])==(manifest['operation_id'],manifest['attempt_id'],manifest['expected_book'],manifest['book_operation_id']),'prior_attempt_identity_changed')
        for member in manifest['candidate']['members']:
            functional=member['functional'];proof=conn.execute(f'SELECT proof_json,proof_digest FROM {processing.FUNCTIONAL} WHERE operation_id=? AND version_id=?',(member['source_ref']['operation_id'],functional['version_id'])).fetchone()
            require(proof and json.loads(proof[0])==functional and proof[1]==sources.source.digest(functional),'prior_native_proof_changed')
        first=min(m['functional']['queue_ref']['effective_date'] for m in manifest['candidate']['members'])
        require(set(manifest['candidate']['effect_dates'])<=set(sources.dates_between(first,manifest['candidate']['functional']['business_date'])[:-1]),'prior_date_scope_changed')
        from contextlib import closing
        with closing(sources.source.readonly(accounting.path(runtime.runtime_dir))) as ledger:
            revision=ledger.execute('SELECT version,payload,previous_version FROM accounting_revisions WHERE operation_id=?',(manifest['book_operation_id'],)).fetchone()
            require(revision is not None or row['state']=='prepared','prior_book_revision_missing')
            if revision:require(revision[0]==manifest['after_book'] and revision[2]==manifest['expected_book'] and accounting.unpack(ledger,revision[1])==manifest['candidate']['candidate_book'],'prior_book_revision_changed')
        if row['state']=='complete':require(row['book_version']==manifest['after_book'] and row['after_digest']==ready.digest(manifest['targets'][-1]['after_json']),'prior_native_publication_changed')
        for old in manifest['candidate']['cohort_refs']:
            actual=processing.operation(conn,old['operation_id']);require(processing.ref(actual)==old,'prior_source_identity_changed')
            if processing.current(conn,actual):
                require(old in refs,'prior_current_member_missing')
            else:require(processing.superseded(conn,actual),'prior_source_supersession_unproved')
        # A new clock/error/Ready target cannot grant a fresh attempt. The
        # actual native source publisher must have issued a newer version.
        current=natives[0]['version'];old_version=conn.execute(f'SELECT * FROM {stages.P}functional_versions WHERE version_id=?',(manifest['candidate']['functional']['version_id'],)).fetchone()
        require(old_version and current['version_id']!=old_version['version_id'] and (current['published_at'] or current['created_at'])>(old_version['published_at'] or old_version['created_at']),'native_attempt_not_superseded')
        item={k:row[k] for k in ('operation_id','attempt_id','kind','state','inputs_json','expected_book','book_operation_id','book_version','after_digest')}
        item.update(manifest_digest=manifest['manifest_digest'],dates=manifest['candidate']['effect_dates'],diagnostics_json=row['diagnostics_json'])
        if pinned is not None:
            saved=next(v for v in pinned if (v['operation_id'],v['attempt_id'])==key)
            # Retirement is our own final commit. Its mutable diagnostic is
            # checked separately; all retained native publication facts pin.
            item['diagnostics_json']=saved['diagnostics_json']
            require(item==saved,'prior_attempt_changed')
        result.append(item)
    return result


def book_tuple(book,day):
    return {key:book[key].get(day) for key in ('wb_days','retained_days','shared_days','presentations')}


def recovery_stage_book(conn,runtime,book,obligations,dates):
    """Only an exact own after-book ghost can reuse ORIGINAL saved stages."""
    capture=deepcopy(book);recovery=[];queries=[]
    for day in dates:
        version=book['wb_days'][day]['version_id']
        if conn.execute(f'SELECT 1 FROM {stages.P}functional_versions WHERE version_id=?',(version,)).fetchone():continue
        matches=[]
        for item in obligations:
            prior=json.loads(item['inputs_json'])
            if day in prior['candidate']['effect_dates'] and prior['candidate']['editions'][day]['version_id']==version and book_tuple(book,day)==book_tuple(prior['candidate']['candidate_book'],day):matches.append((item,prior))
        require(len(matches)==1,'ghost_stage_owner_unproved');item,prior=matches[0]
        from contextlib import closing
        with closing(sources.source.readonly(accounting.path(runtime.runtime_dir))) as ledger:
            row=ledger.execute('SELECT version,payload,previous_version FROM accounting_revisions WHERE operation_id=?',(prior['book_operation_id'],)).fetchone()
            require(row and row[0]==prior['after_book'] and row[2]==prior['expected_book'] and accounting.unpack(ledger,row[1])==prior['candidate']['candidate_book'],'ghost_book_revision_unproved')
        original,_=accounting.load(runtime.runtime_dir,version=prior['expected_book'])
        saved=stages.capture_dated_stages(conn,db_path=runtime.db_path,book=original,dates=[day])
        require(saved['dates'][day]==prior['candidate']['editions'][day]['saved'],'ghost_original_stage_changed')
        ghost=prior['candidate']['editions'][day]['balances'];facts=saved['dates'][day]['balances']
        require([(r['warehouse_key'],r['nm_id'],Decimal(str(r['quantity']))) for r in ghost]==[(r['warehouse_key'],r['nm_id'],Decimal(str(r['quantity']))) for r in facts],'ghost_quantity_changed')
        # The old after-image proves ownership only. Physical quantities and
        # frozen modern money come exclusively from the original saved facts.
        for key,value in book_tuple(original,day).items():capture[key][day]=deepcopy(value)
        recovery.append(dict(day=day,prior_operation=item['operation_id'],prior_attempt=item['attempt_id'],ghost_version=version,expected_book=prior['expected_book'],after_book=prior['after_book'],book_operation_id=prior['book_operation_id'],current_tuple=book_tuple(book,day),original_tuple=book_tuple(original,day),saved_digest=fingerprint(saved['dates'][day])))
        for table,order in (('functional_versions','version_id'),('functional_balances','warehouse_key,nm_id'),('functional_read_models','warehouse_key')):
            sql=f'SELECT * FROM {stages.P}{table} WHERE version_id=? ORDER BY {order}'
            require(not list(conn.execute(sql,(version,))),'ghost_partial_stage_unknown');queries.append((sql,(version,)))
    return capture,recovery,queries


def prepare(runtime, operation_id, *, now, before_book=None, cohort_refs=None, obligations=None):
    from packages.application.operator_supplier_cost_proof import read_correlated_native
    from packages.business_time import current_business_date_iso
    from packages.application.fbs_snapshot_cost import canonical
    book, before_version = accounting.load(runtime.runtime_dir,version=before_book)
    require(book and book["active"], "accounting_not_active")
    with ready.readonly(runtime.db_path) as conn:
        natives=native_cohort(conn,operation_id,pinned=cohort_refs)
        native=natives[0];operation_id=native['operation']['operation_id']
        op, functional = native["operation"], native["functional"]
        require(functional["business_date"] == current_business_date_iso(now), "current_publication_pending")
        shipment = sources.component_manifest(conn, shipment_id=op["shipment_id"], allocation=native["allocation"])
        dependency = sources.receipt_dependency(conn, op["shipment_id"])
        members=[dict(source_ref=processing.ref(m['operation']),functional=m['functional'],
                      shipment=sources.component_manifest(conn,shipment_id=m['operation']['shipment_id'],allocation=m['allocation']),
                      receipt_dependency=sources.receipt_dependency(conn,m['operation']['shipment_id'])) for m in natives]
        obligations=retained_obligations(conn,natives,runtime=runtime,pinned=obligations)
        owed=sorted({day for item in obligations for day in item['dates']})
        first=min(m['functional']['queue_ref']['effective_date'] for m in members)
        all_dates = sources.dates_between(first, functional["business_date"])
        dates = [d for d in all_dates if d < functional["business_date"]]
        no_effect=[]
        for member in members:
            # Read the immutable original version row, never the native
            # current certification accessor with later replay overrides.
            raw_before=conn.execute(f'SELECT * FROM {sources.P}warehouse_supplier_cost_states WHERE version_id=? AND shipment_id=?',(member['functional']['origin_before_version_id'],member['source_ref']['shipment_id'])).fetchone()
            before={key:raw_before[key] for key in ('shipment_id','source_fingerprint','calculation_fingerprint','expenses_complete','calculation_available')} if raw_before else None
            after=member['functional']['cost_state']
            if before and after and all(before[key]==after[key] for key in ('source_fingerprint','calculation_fingerprint','calculation_available')):
                no_effect.append(dict(source_ref=member['source_ref'],before_state=before,after_state=after))
        # Exact identical native cost-input fingerprints prove this operation
        # has no authority to alter past money or stage dates. Do not demand
        # unrelated missing book editions for a nonexistent source effect.
        structural_no_effect=dict(kind='native_cost_source_operands_unchanged',source_dates=dates,members=no_effect) if len(no_effect)==len(members) else None
        if structural_no_effect and not owed:dates=[]
        dates=sorted(set(dates)|set(owed))
        require(len(dates)<=sources.MAX_DATES,'date_scope_unavailable')
        require(all(d in book["wb_days"] and d in book["retained_days"] for d in dates), "dated_book_inputs_missing")
        stage_book,recovery,recovery_queries=recovery_stage_book(conn,runtime,book,obligations,dates)
        dated = stages.capture_dated_stages(conn,db_path=runtime.db_path,book=stage_book,dates=dates) if dates else {
            "dates": {}, "query_fence": [], "source_digest": fingerprint({})}
        daily_index={}
        for m in natives:
            actual=processing.daily_rows(conn,m['queue'],functional['business_date'])
            require(actual==m['functional']['daily_cost_rows'],'dated_cost_publication_changed')
            for row in actual:
                key=(row['as_of_date'],row['nm_id'])
                require(key not in daily_index or daily_index[key]==row,'cohort_daily_operand_changed')
                daily_index[key]=row
        daily=[daily_index[key] for key in sorted(daily_index)]
        queries=[query for m in natives for query in sources.source_queries(m['operation'])]
        queries.extend(recovery_queries)
        for item in obligations:
            queries.append(('SELECT operation_id,attempt_id,kind,state,inputs_json,expected_book,book_operation_id,book_version,after_digest FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(item['operation_id'],item['attempt_id'])))
        from packages.application.warehouse_functional import _DOWNSTREAM_COST_ROWS_SQL
        queries.extend([(_DOWNSTREAM_COST_ROWS_SQL,()),(f'SELECT * FROM {sources.P}wb_supplies ORDER BY supply_id',())])
        for m in natives:
            proof=m['functional'];identity=m['operation']['operation_id']
            queries.extend([(f'SELECT * FROM {processing.FUNCTIONAL} WHERE operation_id=? AND version_id=?',(identity,proof['version_id'])),
                (f'SELECT * FROM {sources.P}warehouse_targeted_recalc_queue WHERE queue_id=?',(m['queue']['queue_id'],))])
            for version in {proof['version_id'],proof['origin_before_version_id']}:
                queries.extend([(f'SELECT * FROM {sources.P}warehouse_functional_versions WHERE version_id=?',(version,)),
                    (f'SELECT * FROM {sources.P}warehouse_functional_balances WHERE version_id=? ORDER BY warehouse_key,nm_id',(version,)),
                    (f'SELECT * FROM {sources.P}warehouse_supplier_cost_states WHERE version_id=? AND shipment_id=?',(version,m['operation']['shipment_id']))])
        for member in members:
            sid=member['source_ref']['shipment_id'];dep=member['receipt_dependency']
            queries.append((f"SELECT * FROM {sources.P}ff_stock_operations WHERE source_type IN ('supplier_shipment','supplier_shipment_acceptance') AND source_object_id=? ORDER BY created_at,operation_id",(sid,)))
            queries.append((f"SELECT l.* FROM {sources.P}ff_stock_operation_lines l JOIN {sources.P}ff_stock_operations o USING(operation_id) WHERE o.source_type IN ('supplier_shipment','supplier_shipment_acceptance') AND o.source_object_id=? ORDER BY l.operation_id,l.line_no",(sid,)))
            if dep['kind']=='immutable_posted_supplier_receipt':
                from packages.application.ff_pool_documents import GUIDED_REPLAYS_TABLE,DOCUMENTS_TABLE,DOCUMENT_LINES_TABLE,EXPENSE_LINES_TABLE,DOCUMENT_RELATIONS_TABLE
                queries.append((f"SELECT * FROM {GUIDED_REPLAYS_TABLE} WHERE shipment_id=? ORDER BY document_id",(sid,)))
                for document in dep['documents']:
                    for table,order in ((DOCUMENTS_TABLE,'document_id'),(DOCUMENT_LINES_TABLE,'line_no'),(EXPENSE_LINES_TABLE,'expense_line_no')):
                        queries.append((f"SELECT * FROM {table} WHERE document_id=? ORDER BY {order}",(document['document_id'],)))
                    queries.append((f"SELECT * FROM {DOCUMENT_RELATIONS_TABLE} WHERE parent_document_id=? OR child_document_id=? OR root_document_id=? ORDER BY parent_document_id,child_document_id,relation_type",(document['document_id'],document['document_id'],document['document_id'])))
        inputs = ready.pin_queries(conn, queries)
        original_version = functional.get("origin_before_version_id")
        require(original_version, "native_before_version_missing")
        current_before = processing.balance_rows(conn,original_version,functional["queue_ref"]["affected_nm_ids"])
        current_after = processing.balance_rows(conn,functional["version_id"],functional["queue_ref"]["affected_nm_ids"])
        current_evaluation = {"before_version":original_version,"after_version":functional["version_id"],
            "before_digest":fingerprint(business_image(current_before)),"after_digest":fingerprint(business_image(current_after))}
        member_evaluation={}
        for member in members:
            own=member['source_ref'];proof=member['functional'];own_ids=proof['queue_ref']['affected_nm_ids']
            before=[r for r in stages._version_balances(conn,version_id=proof['origin_before_version_id']) if r['nm_id'] in own_ids]
            after=[r for r in stages._version_balances(conn,version_id=proof['version_id']) if r['nm_id'] in own_ids]
            def contributions(rows):
                return sorted([(r['warehouse_key'],r['nm_id'],str(s['flow_quantity']),str(s['flow_capital_rub'])) for r in rows for s in r['provenance'].get('source_records',[]) if s.get('shipment_id')==own['shipment_id']])
            prior,actual=contributions(before),contributions(after)
            independent=bool(prior or actual)
            no_change=(prior==actual) if independent else (len(members)==1 and business_image(before)==business_image(after))
            member_evaluation[own['operation_id']]=dict(current_before=fingerprint(prior),current_after=fingerprint(actual),independent_supplier_contributions=independent,current_no_change=no_change,dated={})
        ff_revisions={}
        for day in dates:
            after,proof,fence=sources.dated_ff_rows(conn,dated['dates'][day],shipment=shipment,dependency=dependency,day=day,cohort=members)
            ff_revisions[day]=(after,proof)
            dated['query_fence'].extend(fence)
    candidate, editions, effects, evaluated = deepcopy(book), {}, [], {}
    ids = sorted({nm for m in members for nm in m['functional']['queue_ref']['affected_nm_ids']})
    for day in dates:
        saved = dated["dates"][day]
        rows=ff_revisions[day][0];supplier_proof=[]
        for member in members:
            before=fingerprint(business_image(rows))
            rows,proof=sources.dated_supplier_rows(rows,shipment=member['shipment'],day=day)
            supplier_proof.append(dict(source_ref=member['source_ref'],proof=proof))
            member_evaluation[member['source_ref']['operation_id']]['dated'][day]=dict(before=before,after=fingerprint(business_image(rows)))
        rows, wb_proof = _wb_rows(rows,daily=daily,day=day,ids=ids)
        evaluated[day] = {"before": fingerprint(business_image(saved["balances"])),
                          "after": fingerprint(business_image(rows)), "supplier": supplier_proof,
                          "ff": ff_revisions[day][1], "wb": wb_proof, "saved_version": saved["version"]["version_id"]}
        if evaluated[day]["before"] == evaluated[day]["after"] and day not in owed:
            continue
        effects.append(day)
        version_id = "supplier-cost:" + fingerprint([CONTRACT,processing.ref(op),day,rows,shipment])[7:]
        for row in rows:row["version_id"] = version_id
        version, snapshot = stages._clone_rows(saved,version_id=version_id,day=day,
            published_at=now.isoformat(),balances=rows,source_digest=dated["source_digest"])
        version["version_kind"] = "supplier_cost_historical_revision"
        wb = stages._cloned_wb_capture(version,snapshot,rows,day=day,nm_ids=book["wb_days"][day]["requested_nm_ids"])
        require(wb["complete"], "revised_wb_authority_incomplete")
        retained = deepcopy(book["retained_days"][day]);retained["wb_version_id"] = version_id
        by_key = {(r["warehouse_key"],str(r["nm_id"])):r for r in rows}
        for nm, row in retained["rows"].items():
            for stage in stages.RETAINED_STAGES:
                operand = by_key.get((stages.STAGES[stage],nm))
                row["stages"][stage].update(quantity=operand["quantity"] if operand else "0",
                    capital_rub=operand["capital_rub"] if operand else "0",wac_rub=operand["wac_rub"] if operand else None,
                    revision_proof={"contract":CONTRACT,"saved_version":saved["version"]["version_id"],"revised_version":version_id})
        retained["source_digest"] = fingerprint({k:v for k,v in retained.items() if k!="source_digest"})
        candidate["retained_days"][day] = retained;candidate["wb_days"][day] = wb
        candidate["shared_days"][day] = build_shared_cost_day(candidate["state"],wb,day)
        candidate["presentations"][day] = FbsInventorySnapshot(fbs_state=candidate["state"],wb_capture=wb,retained=retained,day=day).payload()
        companions=deepcopy(saved['companions'])
        for member in members:
            state=member['functional']['cost_state'];sid=member['source_ref']['shipment_id']
            prior=next((r for r in companions['supplier_cost_states'] if r['shipment_id']==sid),None)
            if prior is not None:prior.update(state)
        editions[day] = {"version_id":version_id,"version":version,"snapshot":snapshot,"saved":saved,
                        "balances":rows,"companions":companions,"source_dependency":evaluated[day]}
    if effects:
        candidate["supplier_cost_revision"] = {"contract":CONTRACT,"source_ref":processing.ref(op),
            "dates":effects,"native_stage_editions":{d:editions[d]["version_id"] for d in effects},
            "before_book":before_version,"evaluation_digest":fingerprint(evaluated)}
        candidate["prepared_at"] = now.isoformat()
    result = {"contract":CONTRACT,"operation_id":operation_id,"source_ref":processing.ref(op),
        "functional":functional,"certification":native["certification"],"queue_ref":functional["queue_ref"],
        "current_evaluation":current_evaluation,
        "shipment":shipment,"receipt_dependency":dependency,"dates":dates,"effect_dates":effects,
        "evaluation":evaluated,"source_inputs":inputs,"query_fence":dated["query_fence"],
        "before_book":before_version,"candidate_book":candidate,"editions":editions,
        "cohort_refs":[m['source_ref'] for m in members],"members":members,"member_evaluation":member_evaluation,
        "structural_no_effect":structural_no_effect,
        "attempt_id":attempt_identity(natives),"obligations":obligations,"owed_dates":owed,"recovery_inputs":recovery,
        "code_authority":code_authority(),"created_at":now.isoformat()}
    require(len(canonical(result).encode()) <= MAX_BYTES, "candidate_size_limit")
    result=json.loads(canonical(result))
    result["manifest_digest"] = fingerprint(result)
    return result


def validate(candidate, *, runtime, now):
    require(candidate.get("contract") == CONTRACT and candidate.get("manifest_digest") == fingerprint(
        {k:v for k,v in candidate.items() if k!="manifest_digest"}), "candidate_corrupt")
    require(candidate["code_authority"] == code_authority(), "formula_changed")
    require(prepare(runtime,candidate["operation_id"],now=now,before_book=candidate["before_book"],cohort_refs=candidate['cohort_refs'],obligations=candidate['obligations']) == candidate, "independent_rebuild_changed")
    return deepcopy(candidate)
