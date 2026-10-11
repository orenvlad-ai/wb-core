"""Exact saved native source capture for the owned receipt-history contour.

All functions consume a caller-owned query-only transaction. No runtime schema,
service, API collection or write occurs here. Ordinary capture/evaluation is not
modified. Native accepted confirmation and immutable postings remain authority.
"""
from __future__ import annotations
from copy import deepcopy
import json

from packages.application.fbs_accounting_historical_revision import _manifest_known, _time, MAX_DOCUMENTS, MAX_DAYS, MAX_CAPTURE_BYTES, MAX_LOCATIONS
from packages.application.fbs_accounting_historical_revision_writer import REQUEST_PROOF_FIELDS
from packages.application.fbs_snapshot_cost_sources import capture_current
from packages.application.ff_pool_documents import DOCUMENTS_TABLE, REQUESTS_TABLE
from packages.application.fbs_snapshot_cost import fingerprint


def archived_quantity_snapshot(conn, *, day, observed_at, generation_id=None):
    """Read one immutable official generation of the exact missing date.

    The archived catalog/mapping owns this observation. Today's catalog,
    mapping and stock are deliberately not substitutes for historical facts.
    """
    from datetime import date, datetime, time, timedelta, timezone
    from packages.business_time import CANONICAL_BUSINESS_TIMEZONE, current_business_date_iso
    from packages.application.wb_fbs_warehouse_registry import (
        REGISTRY_RUNS_TABLE, STOCK_RUNS_TABLE, STOCK_ROWS_TABLE, COMPLETE_CATALOG_OMISSION_ZERO_POLICY,
    )
    from packages.application.fbs_accounting_historical_stages import require
    require(conn.in_transaction and conn.execute('PRAGMA query_only').fetchone()[0] == 1,
            'historical_archive_query_only_required')
    start = datetime.combine(date.fromisoformat(day), time.min, CANONICAL_BUSINESS_TIMEZONE)
    bounds = (start.astimezone(timezone.utc).isoformat(), (start + timedelta(days=1)).astimezone(timezone.utc).isoformat())
    if generation_id is None:
        # Metadata only: never load every archived catalog/stock payload.
        selected = conn.execute(f'''SELECT g.run_id FROM {REGISTRY_RUNS_TABLE} g
            WHERE g.status='success' AND g.complete=1 AND g.policy_version=?
              AND julianday(g.completed_at)<=julianday(?)
              AND EXISTS(SELECT 1 FROM {STOCK_RUNS_TABLE} s WHERE s.registry_run_id=g.run_id)
              AND NOT EXISTS(SELECT 1 FROM {STOCK_RUNS_TABLE} s WHERE s.registry_run_id=g.run_id
                  AND (s.complete<>1 OR julianday(s.snapshot_at) IS NULL
                       OR julianday(s.snapshot_at)<julianday(?) OR julianday(s.snapshot_at)>=julianday(?)))
            ORDER BY g.run_sequence DESC LIMIT 1''',
            (COMPLETE_CATALOG_OMISSION_ZERO_POLICY, observed_at, *bounds)).fetchone()
        require(selected is not None, 'historical_archived_quantity_missing', day=day)
        generation_id = selected[0]
    size = conn.execute(f'SELECT length(CAST(catalog_scope_json AS BLOB))+length(CAST(warehouse_scope_json AS BLOB)) FROM {REGISTRY_RUNS_TABLE} WHERE run_id=?', (generation_id,)).fetchone()
    require(size and size[0] <= MAX_CAPTURE_BYTES, 'historical_archived_quantity_size_limit', day=day)
    run = conn.execute(f'SELECT * FROM {REGISTRY_RUNS_TABLE} WHERE run_id=?', (generation_id,)).fetchone()
    require(run and run['status'] == 'success' and run['complete'] == 1
            and run['policy_version'] == COMPLETE_CATALOG_OMISSION_ZERO_POLICY and run['generation_digest']
            and _time(run['completed_at']) <= _time(observed_at), 'historical_archived_quantity_generation_invalid', day=day)
    catalog, scope = json.loads(run['catalog_scope_json']), json.loads(run['warehouse_scope_json'])
    require(catalog.get('complete') is True and scope.get('complete') is True
            and 0 < catalog['requested_chrt_count'] <= 100000
            and 0 < scope['warehouse_count'] <= MAX_LOCATIONS
            and len(scope['warehouses']) == scope['warehouse_count'], 'historical_archived_quantity_scope_invalid', day=day)
    warehouses = scope['warehouses']
    require(len({w['facility_id'] for w in warehouses}) == len(warehouses)
            and len({w['seller_warehouse_id'] for w in warehouses}) == len(warehouses)
            and all(w['facility_id'] and w['mapping_id'] and type(w['seller_warehouse_id']) is int
                    and w['seller_warehouse_id'] > 0 for w in warehouses),
            'historical_archived_quantity_facility_ambiguous', day=day)
    count = conn.execute(f'SELECT count(*) FROM {STOCK_RUNS_TABLE} WHERE registry_run_id=?', (generation_id,)).fetchone()[0]
    require(count == len(warehouses), 'historical_archived_quantity_stock_scope_changed', day=day)
    # SELECT * below (and the original-source fence) also consumes native
    # identity_scope_json. Bound every persisted stock-run column before any
    # wide row is loaded; minimal legacy test schemas have fewer columns.
    columns = [str(r[1]).replace('"', '""') for r in conn.execute(f'PRAGMA table_info({STOCK_RUNS_TABLE})')]
    widths = '+'.join(f'coalesce(length(CAST("{column}" AS BLOB)),0)' for column in columns)
    size = conn.execute(f'SELECT coalesce(sum({widths}),0) FROM {STOCK_RUNS_TABLE} WHERE registry_run_id=?', (generation_id,)).fetchone()[0]
    require(size <= MAX_CAPTURE_BYTES, 'historical_archived_quantity_size_limit', day=day)
    count, size = conn.execute(f'SELECT count(*),coalesce(sum(length(CAST(r.provenance AS BLOB))),0) FROM {STOCK_ROWS_TABLE} r JOIN {STOCK_RUNS_TABLE} s ON r.run_id=s.run_id WHERE s.registry_run_id=?', (generation_id,)).fetchone()
    require(count <= 100000 and size <= MAX_CAPTURE_BYTES, 'historical_archived_quantity_size_limit', day=day)
    quantities, evidence, observations, identities = {}, {}, [], None
    for warehouse in warehouses:
        stock = conn.execute(f'SELECT * FROM {STOCK_RUNS_TABLE} WHERE registry_run_id=? AND seller_warehouse_id=?',
                             (generation_id, warehouse['seller_warehouse_id'])).fetchone()
        require(stock and stock['complete'] == 1 and stock['source_digest']
                and current_business_date_iso(_time(stock['snapshot_at'])) == day
                and _time(stock['snapshot_at']) <= _time(run['completed_at']),
                'historical_archived_quantity_date_or_completeness_invalid', day=day)
        rows = conn.execute(f'SELECT chrt_id,nm_id,amount,provenance FROM {STOCK_ROWS_TABLE} WHERE run_id=? ORDER BY chrt_id', (stock['run_id'],)).fetchall()
        keys = {(r['chrt_id'], r['nm_id']) for r in rows}
        require(len(rows) == len(keys) == len({r['chrt_id'] for r in rows}) == catalog['requested_chrt_count'] == stock['requested_chrt_count'] == stock['dense_row_count']
                and len({r['nm_id'] for r in rows}) == catalog['active_nm_id_count']
                and sum(r['provenance']=='explicit_wb_row' for r in rows) == stock['explicit_chrt_count']
                and sum(r['provenance']=='omitted_requested_zero' for r in rows) == stock['omitted_zero_count']
                and (identities is None or keys == identities), 'historical_archived_quantity_dense_identity_changed', day=day)
        identities = keys
        fid = str(warehouse['facility_id'])
        evidence[fid] = {**warehouse, 'captured_at': stock['snapshot_at'], 'stock_run_id': stock['run_id'], 'stock_digest': stock['source_digest']}
        observations.append(stock['snapshot_at'])
        for row in rows:
            require(type(row['chrt_id']) is int and row['chrt_id'] > 0
                    and type(row['nm_id']) is int and row['nm_id'] > 0 and type(row['amount']) is int and row['amount'] >= 0
                    and row['provenance'] in {'explicit_wb_row', 'omitted_requested_zero'}
                    and (row['provenance'] != 'omitted_requested_zero' or row['amount'] == 0),
                    'historical_archived_quantity_operand_invalid', day=day)
            key = (row['nm_id'], fid)
            quantities[key] = quantities.get(key, 0) + row['amount']
    require(0 < len(quantities) <= MAX_LOCATIONS, 'historical_archived_quantity_size_limit', day=day)
    return dict(date=day, captured_at=min(observations), id=generation_id, digest=run['generation_digest'], complete=True,
                rows=[dict(nm_id=nm, facility_id=fid, quantity=q) for (nm, fid), q in sorted(quantities.items())],
                source='official_fbs_stock_snapshot_v1', facility_evidence=evidence)


def capture_missing_dated_inputs(conn, *, db_path, book, capture, plan=None):
    """Fill only absent interior tail days; re-read pinned sources for a plan."""
    from datetime import date, timedelta
    from packages.application.fbs_accounting_historical_stages import require
    from packages.application.shared_sku_cost_sources import capture_wb_component
    from packages.application.fbs_inventory_presentation import capture_retained_stages
    from packages.application.fbs_snapshot_cost import canonical
    first, last = date.fromisoformat(max(book['state']['periods'])), date.fromisoformat(capture['business_date'])
    advance = (last - first).days
    require(0 <= advance <= MAX_DAYS, 'historical_date_scope_limit')
    result = {}
    for n in range(1, advance):
        day = (first + timedelta(days=n)).isoformat()
        pinned = plan['candidate_book'] if plan else None
        generation_id = pinned['state']['periods'][day]['snapshot']['id'] if pinned else None
        snapshot = archived_quantity_snapshot(conn, day=day, observed_at=capture['captured_at'], generation_id=generation_id)
        nm_ids = sorted({r['nm_id'] for r in snapshot['rows']})
        version = pinned['wb_days'][day]['version_id'] if pinned else None
        if version is None:
            found = conn.execute("SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_versions "
                "WHERE cutover_id='warehouse_functional_cutover_v1' AND status='good' AND business_effective_date=? "
                "ORDER BY COALESCE(NULLIF(published_at,''),created_at) DESC,created_at DESC,version_id DESC LIMIT 1", (day,)).fetchone()
            require(found is not None, 'historical_archived_wb_missing', day=day)
            version = found[0]
        # Bound payloads before the existing exact-date native readers parse them.
        for table, fields, clause, args in (
            ('warehouse_wb_snapshots', ('items_json','raw_rows_json','requested_nm_ids_json'), 'version_id=?', (version,)),
            ('warehouse_functional_balances', ('provenance_json','capital_rub','quantity'), 'version_id=?', (version,)),
            ('warehouse_functional_read_models', ('payload_json',), 'version_id=?', (version,)),
            ('warehouse_business_projection_current_rows', ('metrics_json','presentation_json','provenance_json'), 'as_of_date=?', (day,)),
        ):
            sizes = '+'.join(f'coalesce(length(CAST({field} AS BLOB)),0)' for field in fields)
            count, size = conn.execute(f'SELECT count(*),coalesce(sum({sizes}),0) FROM sheet_vitrina_v1_{table} WHERE {clause}', args).fetchone()
            require(count <= 100000 and size <= MAX_CAPTURE_BYTES, 'historical_archived_dated_source_size_limit', day=day)
        wb = capture_wb_component(db_path, day=day, nm_ids=nm_ids, version_id=version, connection=conn)
        require(wb['complete'] and wb.get('authority_complete') is True, 'historical_archived_wb_incomplete', day=day)
        retained = capture_retained_stages(db_path, day=day, wb_version_id=version, nm_ids=nm_ids, connection=conn)
        result[day] = dict(quantity_snapshot=snapshot, wb_capture=wb, retained_capture=retained)
        require(len(canonical(result).encode()) <= MAX_CAPTURE_BYTES, 'historical_archived_dated_source_size_limit', day=day)
    if result:
        capture['archived_dated_inputs'] = result
    return capture


def original_source_queries(capture):
    """Fixed query set for the exact original cohort, never caller SQL."""
    queries=[]
    from packages.application.fbs_accounting_historical_stages import require
    ids=sorted(d['document_id'] for d in capture['documents'])
    require(0<len(ids)<=MAX_DOCUMENTS,'historical_original_document_limit')
    # Chunk bound SQLite's variable limit and manifest allocation.
    for offset in range(0,len(ids),250):
        part=tuple(ids[offset:offset+250]);marks=','.join('?' for _ in part)
        queries.extend((f'SELECT * FROM sheet_vitrina_v1_{table} WHERE document_id IN ({marks}) ORDER BY '+order,part)
            for table,order in (('ff_pool_documents','document_id'),('ff_pool_document_lines','document_id,line_no'),('ff_pool_document_expense_lines','document_id,expense_line_no')))
        queries.append((f'SELECT * FROM sheet_vitrina_v1_ff_pool_document_relations WHERE child_document_id IN ({marks}) ORDER BY child_document_id,relation_type',part))
        queries.extend((f'SELECT m.* FROM sheet_vitrina_v1_{table} m JOIN {DOCUMENTS_TABLE} d USING(operation_id) WHERE d.document_id IN ({marks}) ORDER BY '+order,part)
            for table,order in (('ff_pool_movement_lines','m.operation_id,m.line_no'),('warehouse_business_operations','m.operation_id')))
    for identity in sorted(capture['native_requests_by_id']):
        queries.append((f"SELECT {','.join(REQUEST_PROOF_FIELDS)} FROM {REQUESTS_TABLE} WHERE request_id=?",(identity,)))
    from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE,STOCK_RUNS_TABLE,STOCK_ROWS_TABLE
    generations = {capture['quantity_snapshot']['id'], *(v['quantity_snapshot']['id']
        for v in capture.get('archived_dated_inputs', {}).values())}
    require(len(generations) <= MAX_DAYS, 'historical_date_scope_limit')
    for generation in sorted(generations):
        queries.extend(((f'SELECT * FROM {REGISTRY_RUNS_TABLE} WHERE run_id=?',(generation,)),
            (f'SELECT * FROM {STOCK_RUNS_TABLE} WHERE registry_run_id=? ORDER BY seller_warehouse_id',(generation,)),
            (f'SELECT r.* FROM {STOCK_ROWS_TABLE} r JOIN {STOCK_RUNS_TABLE} s ON s.run_id=r.run_id WHERE s.registry_run_id=? ORDER BY r.run_id,r.chrt_id',(generation,))))
    from packages.application.operator_warehouse_documents import TABLE
    for identity in sorted(capture['native_requests_by_id']):
        queries.append((f'SELECT request_id,source_json,source_digest,accepted_at,actor FROM {TABLE} WHERE request_id=?',(identity,)))
    queries.append(("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (name LIKE 'ff_pool_%no_update' OR name LIKE 'ff_pool_%no_delete' OR name LIKE 'wb_fbs_%no_update' OR name LIKE 'wb_fbs_%no_delete' OR name LIKE 'ff_operator_%immutable' OR name='ff_operator_confirmation_no_delete') ORDER BY name",()))
    return queries


def capture_archived_cohort(conn, *, plan, original_capture):
    """Rebuild a durable intent from its exact immutable original sources.

    Recovery does not select today's newest generation or absorb later posted
    documents. The saved quantity operand is checked against its actual native
    registry/stock rows; every normalized document is independently reread.
    """
    from packages.application.fbs_snapshot_cost_sources import _documents
    from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE,STOCK_RUNS_TABLE,STOCK_ROWS_TABLE
    from packages.application.fbs_accounting_historical_stages import require
    require(conn.in_transaction and conn.execute('PRAGMA query_only').fetchone()[0]==1,'historical_archive_query_only_required')
    saved=original_capture['quantity_snapshot'];day=plan['scope']['date_to']
    run=conn.execute(f'SELECT * FROM {REGISTRY_RUNS_TABLE} WHERE run_id=?',(saved['id'],)).fetchone()
    require(run and run['complete'] and run['generation_digest']==saved['digest'],'historical_original_quantity_generation_missing')
    scope=json.loads(run['warehouse_scope_json']);catalog=json.loads(run['catalog_scope_json'])
    require(scope.get('complete') and catalog.get('complete'),'historical_original_quantity_scope_incomplete')
    quantities={};observations=[];identities=None
    require(len(scope['warehouses'])==scope['warehouse_count'],'historical_original_quantity_facility_ambiguous')
    for warehouse in scope['warehouses']:
        fid=str(warehouse['facility_id']);evidence=saved['facility_evidence'].get(fid)
        stock=conn.execute(f'SELECT * FROM {STOCK_RUNS_TABLE} WHERE registry_run_id=? AND seller_warehouse_id=?',(saved['id'],warehouse['seller_warehouse_id'])).fetchone()
        require(stock and stock['complete'] and evidence and stock['run_id']==evidence['stock_run_id'] and stock['source_digest']==evidence['stock_digest']
            and stock['snapshot_at']==evidence['captured_at'] and all(evidence.get(k)==v for k,v in warehouse.items()),'historical_original_quantity_stock_changed')
        from packages.business_time import current_business_date_iso
        from datetime import datetime
        require(current_business_date_iso(datetime.fromisoformat(stock['snapshot_at'].replace('Z','+00:00')))==day,'historical_original_quantity_date_mismatch')
        count=conn.execute(f'SELECT count(*) FROM {STOCK_ROWS_TABLE} WHERE run_id=?',(stock['run_id'],)).fetchone()[0]
        require(count<=100000,'historical_original_quantity_size_limit')
        rows=conn.execute(f'SELECT chrt_id,nm_id,amount,provenance FROM {STOCK_ROWS_TABLE} WHERE run_id=?',(stock['run_id'],)).fetchall()
        keys={(r['chrt_id'],r['nm_id']) for r in rows}
        require(len(keys)==catalog['requested_chrt_count'] and (identities is None or keys==identities),'historical_original_quantity_dense_identity_changed')
        identities=keys;observations.append(stock['snapshot_at'])
        for r in rows:
            require(isinstance(r['amount'],int) and r['amount']>=0 and r['provenance'] in {'explicit_wb_row','omitted_requested_zero'}
                and (r['provenance']!='omitted_requested_zero' or r['amount']==0),'historical_original_quantity_operand_invalid')
            key=(int(r['nm_id']),fid);quantities[key]=quantities.get(key,0)+r['amount']
    rebuilt=[dict(nm_id=nm,facility_id=fid,quantity=q) for (nm,fid),q in sorted(quantities.items())]
    require(rebuilt==saved['rows'] and min(observations)==saved['captured_at'],'historical_original_quantity_operand_changed')
    documents=_documents(conn);wanted=plan['source_proof']['document_manifest']
    by_id={d['document_id']:d for d in documents}
    require(all(i in by_id and by_id[i]['fingerprint']==digest for i,digest in wanted.items()),'historical_original_document_changed')
    capture=deepcopy(original_capture);capture['documents']=[by_id[i] for i in sorted(wanted)]
    # Dated WB/retained operands are independently verified against their
    # immutable native versions by capture_dated_stages during every rebuild.
    # Quantity generations need their own reconstruction, including recovery
    # after today's active generation and projection pointers have advanced.
    for archived_day, extra in capture.get('archived_dated_inputs', {}).items():
        rebuilt = archived_quantity_snapshot(conn, day=archived_day, observed_at=capture['captured_at'],
                                             generation_id=extra['quantity_snapshot']['id'])
        require(rebuilt == extra['quantity_snapshot'], 'historical_original_archived_quantity_changed', day=archived_day)
    capture['posted_manifest_json_by_id']={i:conn.execute(f'SELECT posted_manifest_json FROM {DOCUMENTS_TABLE} WHERE document_id=?',(i,)).fetchone()[0] for i in sorted(wanted)}
    selected=[*plan['receipt_document_ids'],*plan.get('current_document_ids',[]),*plan.get('auxiliary_document_ids',[])]
    capture=augment_native_requests(conn,capture,document_ids=selected)
    # The archived cohort retains exactly the authority accepted before intent:
    # native posted request/actor/expense operands for current facts, plus human
    # confirmation for late receipt requests. Lifecycle state may progress.
    require({i:{k:v for k,v in r.items() if k!='state'} for i,r in capture['native_requests_by_id'].items()}
        == {i:{k:v for k,v in r.items() if k!='state'} for i,r in original_capture['native_requests_by_id'].items()},
        'historical_original_native_request_changed')
    verify_cohort_confirmations(conn,plan=plan,capture=capture)
    capture['source_digest']=fingerprint({k:capture[k] for k in ('contract','business_date','quantity_snapshot','documents_complete','documents','documents_reason')})
    return capture



def verify_cohort_confirmations(conn, *, plan, capture):
    """Same receipt/current authority at initial prepare and exact recovery.

    Ordinary accepted-and-posted native current facts need no additional human
    confirmation. Their exact immutable request, posting and monetary operands
    remain independently validated by the planner and original source fence.
    If a current confirmation exists, verify it with the same native contract.
    Only late receipt requests own historical operator completion obligations.
    """
    from packages.application.operator_warehouse_documents import TABLE,assert_source
    from packages.application.fbs_accounting_historical_stages import require
    require(conn.in_transaction and conn.execute('PRAGMA query_only').fetchone()[0]==1,
        'historical_confirmation_query_only_required')
    required={json.loads(capture['posted_manifest_json_by_id'][i])['request_id'] for i in plan['receipt_document_ids']}
    documents={d['document_id']:d for d in capture['documents']}
    confirmations={}
    for request_id in sorted(capture['native_requests_by_id']):
        request=conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(request_id,)).fetchone()
        accepted=conn.execute(f'SELECT source_json,source_digest,accepted_at,actor FROM {TABLE} WHERE request_id=?',(request_id,)).fetchone()
        if accepted is None:
            require(request_id not in required,'historical_operator_confirmation_authority_mismatch')
            continue
        source=assert_source(conn,request)
        primary=documents.get(request['posted_document_id'])
        require(primary is not None,'historical_confirmation_primary_document_missing')
        # The saved request/physical actor owns the preview and posting; the
        # immutable confirmation separately owns the final human decision.
        require(type(accepted['actor']) is str and bool(accepted['actor'].strip())
            and source.get('preview_actor')==request['actor']
            and _time(request['accepted_at']) <= _time(accepted['accepted_at'])
            <= _time(primary['posted_at'])
            <= _time(request['posted_at']) <= _time(capture['captured_at'])
            and source['effect']['primary_document_id']==request['posted_document_id'],
            'historical_operator_confirmation_authority_mismatch')
        if request_id in required:confirmations[request_id]=dict(accepted)
    require(set(confirmations)==required,'historical_operator_confirmation_authority_mismatch')
    return confirmations


def augment_native_requests(conn, capture, *, document_ids):
    if not conn.in_transaction or conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ValueError("historical_native_source_pinned_query_only_required")
    wanted = set(document_ids)
    if len(wanted) > MAX_DOCUMENTS:
        raise ValueError("historical_native_source_document_limit")
    capture = deepcopy(capture)
    capture["native_requests_by_id"], capture["posted_actors_by_id"] = {}, {}
    for identity in sorted(wanted):
        doc = conn.execute(f"SELECT request_id,actor FROM {DOCUMENTS_TABLE} WHERE document_id=?", (identity,)).fetchone()
        if doc is None:
            raise ValueError("historical_native_document_missing:" + identity)
        capture["posted_actors_by_id"][identity] = doc[1]
        if doc[0] not in capture["native_requests_by_id"]:
            row = conn.execute(f"SELECT {','.join(REQUEST_PROOF_FIELDS)},state FROM {REQUESTS_TABLE} WHERE request_id=?", (doc[0],)).fetchone()
            if row is None:
                raise ValueError("historical_native_request_missing:" + doc[0])
            capture["native_requests_by_id"][doc[0]] = dict(row)
    return capture


def capture_confirmed_cohort(conn, *, db_path, book, now):
    """Select confirmed NEW late receipts and an explicit complete current cohort.

    An unselected late source is intentionally left in capture; the pure planner
    fails exact scope rather than silently absorbing it. Each native request's
    immutable full cohort is selected, including China discrepancy companions.
    """
    from packages.application.operator_warehouse_documents import TABLE, exists, assert_source
    if not conn.in_transaction or conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ValueError("historical_native_source_pinned_query_only_required")
    closed_days=[d for d,p in book['state']['periods'].items() if p['status']=='closed']
    if not closed_days:
        return dict(capture=None,receipt_document_ids=[],auxiliary_document_ids=[],current_document_ids=[],confirmed_requests={})
    count,size=conn.execute(f'SELECT count(*),coalesce(sum(length(posted_manifest_json)),0) FROM {DOCUMENTS_TABLE}').fetchone()
    if count>MAX_DOCUMENTS or size>32*1024**2:raise ValueError('historical_native_source_size_limit')
    capture = capture_current(db_path, now=now, include_baseline=False, connection=conn)
    capture["posted_manifest_json_by_id"] = {r[0]: r[1] for r in conn.execute(
        f"SELECT document_id,posted_manifest_json FROM {DOCUMENTS_TABLE} ORDER BY document_id")}
    known = _manifest_known(book["state"])
    closed = max(closed_days)
    new = {d["document_id"]: d for d in capture["documents"] if d["document_id"] not in known}
    receipts, auxiliary, confirmations = [], [], {}
    for identity, doc in sorted(new.items()):
        if doc["kind"] not in {"china_acceptance", "transfer_receipt"} or doc["business_date"] > closed:
            continue
        posted = json.loads(capture["posted_manifest_json_by_id"][identity])
        request_id = posted["request_id"]
        if not exists(conn):
            continue
        confirmed = conn.execute(f"SELECT source_json,source_digest,actor,accepted_at FROM {TABLE} WHERE request_id=?", (request_id,)).fetchone()
        if confirmed is None:
            continue
        request = conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?", (request_id,)).fetchone()
        source = assert_source(conn, request)
        if fingerprint(source) != confirmed[1]:
            raise ValueError("historical_operator_source_digest_mismatch")
        receipts.append(identity)
        auxiliary.extend(v["document_id"] for v in posted["documents"] if v["document_id"] != identity)
        confirmations[request_id] = dict(source_json=confirmed[0], source_digest=confirmed[1], actor=confirmed[2], accepted_at=confirmed[3])
    current = sorted(i for i, d in new.items() if d["business_date"] == capture["business_date"])
    capture = augment_native_requests(conn, capture, document_ids=[*receipts, *auxiliary, *current])
    return {"capture": capture, "receipt_document_ids": receipts, "auxiliary_document_ids": sorted(auxiliary),
            "current_document_ids": current, "confirmed_requests": confirmations}
