"""Owned special publication of validated dated receipt editions.

Preparation rebuilds numerical and stage candidates before the short CAS.
The ordinary closed-day book writer is deliberately not used or weakened.
An actual ready target owns the recovery intent; staging intents never enter
this path. The runner must hold its real live warehouse job owner.
"""
from __future__ import annotations
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from packages.application import fbs_accounting_runtime as accounting
from packages.application import ready_publication as ready
from packages.application import fbs_accounting_historical_revision_writer as staging
from packages.application import fbs_accounting_historical_sources as sources
from packages.application import fbs_accounting_historical_stages as stages
from packages.application.fbs_accounting_historical_revision import validate_historical_revision_plan
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.warehouse_functional_lock import require_warehouse_job_owner,warehouse_functional_write_lock

CONTRACT="fbs_accounting_historical_receipt_publication_v1"
MAX_BYTES=160*1024**2
MAX_READY_HEADERS=10000
MAX_READY_HEADER_BYTES=8*1024**2
STORAGE_ENCODING='historical_publication_shared_operands_v1'
BOOK_DAY_FIELDS=('wb_days','retained_days','shared_days','presentations')
# The stored unique operands remain capped at 160 MiB. The legacy digest also
# visits repeated references; bound that expanded representation before any
# large reconstruction/serialization (the measured real contour is 235 MB).
MAX_EXPANDED_BYTES=320*1024**2


def _same_json(left, right):
    # Python equality aliases bool/int/float; the native JSON digest does not.
    if type(left) is not type(right):return False
    if isinstance(left,float):return canonical(left)==canonical(right)
    if isinstance(left,dict):return set(left)==set(right) and all(_same_json(v,right[k]) for k,v in left.items())
    if isinstance(left,(list,tuple)):return len(left)==len(right) and all(_same_json(a,b) for a,b in zip(left,right))
    return left==right


def _json_size(value, limit, code):
    cache={};active=set()
    def size(item):
        identity=id(item)
        if identity in cache:return cache[identity]
        stages.require(identity not in active,code)
        active.add(identity)
        if isinstance(item,dict):
            total=2+max(0,len(item)-1)
            for key,child in item.items():
                total+=size(key)+1+size(child);stages.require(total<=limit,code)
        elif isinstance(item,(list,tuple)):
            total=2+max(0,len(item)-1)
            for child in item:
                total+=size(child);stages.require(total<=limit,code)
        else:total=len(canonical(item).encode())
        stages.require(total<=limit,code);active.remove(identity);cache[identity]=total
        return total
    return size(value)


def compact_manifest(manifest):
    """Store repeated exact operands once; retain the expanded manifest digest.

    References have fixed destinations within this same intent: the numerical
    book and target before-images. No external lookup, recursive reference or
    compression is involved. Original unencoded intents remain readable.
    """
    stages.require('storage_encoding' not in manifest,'historical_manifest_already_encoded')
    base=manifest['plan']['candidate_book'];book=manifest['candidate']['candidate_book']
    stages.require(set(base)==set(book),'historical_manifest_book_shape_changed')
    for field in BOOK_DAY_FIELDS:
        stages.require(set(base[field])==set(book[field]),'historical_manifest_book_dates_changed')
    overlay=dict(fields={k:v for k,v in book.items() if k not in BOOK_DAY_FIELDS and not _same_json(v,base[k])},
        days={field:{day:value for day,value in book[field].items() if not _same_json(value,base[field][day])} for field in BOOK_DAY_FIELDS})
    candidate={k:v for k,v in manifest['candidate'].items() if k!='candidate_book'}
    candidate['candidate_book_overlay']=overlay
    targets=[]
    for i,original in enumerate(manifest['targets']):
        target=dict(original);donor=target.get('donor_expected')
        if donor:
            matches=[j for j,t in enumerate(manifest['targets']) if j!=i and _same_json(t['expected'],donor)]
            if len(matches)==1:
                target.pop('donor_expected');target['donor_expected_target']=matches[0]
        if target['before_envelope_json']==target['expected']['plan_json']:
            target.pop('before_envelope_json');target['before_envelope_source']='expected'
        elif donor and target['before_envelope_json']==_promoted_envelope(ready.ExpectedReady(**donor),target['expected']['as_of_date']):
            target.pop('before_envelope_json');target['before_envelope_source']='donor_promoted'
        targets.append(target)
    return {**manifest,'storage_encoding':STORAGE_ENCODING,'candidate':candidate,'targets':targets}


def expand_manifest(stored):
    """Resolve only fixed, nonrecursive references, before normal verification."""
    if 'storage_encoding' not in stored:return stored
    require=stages.require
    require(stored['storage_encoding']==STORAGE_ENCODING,'historical_manifest_encoding_unknown')
    expanded_size=_json_size(stored,MAX_BYTES,'historical_publication_size_limit')
    base=stored['plan']['candidate_book'];candidate=dict(stored['candidate'])
    expanded_size+=_json_size(base,MAX_EXPANDED_BYTES,'historical_manifest_expanded_size_limit')
    require('candidate_book' not in candidate,'historical_manifest_duplicate_book')
    overlay=candidate.pop('candidate_book_overlay')
    require(set(overlay)=={'fields','days'} and set(overlay['days'])==set(BOOK_DAY_FIELDS)
        and set(overlay['fields'])<=set(base)-set(BOOK_DAY_FIELDS),'historical_manifest_book_overlay_invalid')
    book={**base,**overlay['fields']}
    for field in BOOK_DAY_FIELDS:
        require(set(overlay['days'][field])<=set(base[field]),'historical_manifest_book_dates_changed')
        book[field]={**base[field],**overlay['days'][field]}
    candidate['candidate_book']=book
    targets=[]
    for i,original in enumerate(stored['targets']):
        target=dict(original)
        if 'donor_expected_target' in target:
            index=target.pop('donor_expected_target')
            require(type(index) is int and 0<=index<len(stored['targets']) and index!=i
                and 'donor_expected' not in target,'historical_manifest_donor_reference_invalid')
            target['donor_expected']=stored['targets'][index]['expected']
            require(target['donor_expected']['plan_json'] is not None,'historical_manifest_donor_reference_invalid')
            expanded_size+=_json_size(target['donor_expected'],MAX_EXPANDED_BYTES,'historical_manifest_expanded_size_limit')
        if 'before_envelope_source' in target:
            source=target.pop('before_envelope_source')
            require('before_envelope_json' not in target,'historical_manifest_duplicate_before')
            if source=='expected':
                require(target['expected']['plan_json'] is not None,'historical_manifest_before_reference_invalid')
                expanded_size+=_json_size(target['expected']['plan_json'],MAX_EXPANDED_BYTES,'historical_manifest_expanded_size_limit')
                require(expanded_size<=MAX_EXPANDED_BYTES,'historical_manifest_expanded_size_limit')
                target['before_envelope_json']=target['expected']['plan_json']
            elif source=='donor_promoted':
                require(target.get('donor_expected') is not None,'historical_manifest_before_reference_invalid')
                expanded_size+=_json_size(target['donor_expected']['plan_json'],MAX_EXPANDED_BYTES,'historical_manifest_expanded_size_limit')+1024
                require(expanded_size<=MAX_EXPANDED_BYTES,'historical_manifest_expanded_size_limit')
                target['before_envelope_json']=_promoted_envelope(ready.ExpectedReady(**target['donor_expected']),target['expected']['as_of_date'])
            else:require(False,'historical_manifest_before_reference_invalid')
        targets.append(target)
    require(expanded_size<=MAX_EXPANDED_BYTES,'historical_manifest_expanded_size_limit')
    return {**{k:v for k,v in stored.items() if k!='storage_encoding'},'candidate':candidate,'targets':targets}


def _parameter_proof(conn, dates):
    """Exact native parameter fields survive the durable JSON recovery intent."""
    from packages.application.web_vitrina_management_history import dated_parameters
    def decimal_text(value):
        if isinstance(value, Decimal):return str(value)
        raise TypeError(f'Unsupported dated parameter value: {type(value).__name__}')
    result={}
    for day in dates:
        parameters=dated_parameters(conn,day)
        result[day]=None if parameters is None else json.loads(json.dumps(
            [asdict(parameter) for parameter in parameters],default=decimal_text))
    return result


def _promoted_envelope(donor, day):
    """Retain an already accepted column verbatim in a new exact-date target."""
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan,_serialize_sheet_vitrina_plan
    envelope=_deserialize_sheet_vitrina_plan(donor.plan_json)
    stages.require(day in envelope.date_columns,'historical_ready_donor_date_missing')
    return _serialize_sheet_vitrina_plan(replace(envelope,as_of_date=day))


def _selected_ready_rows(conn, *, plan, authority, current_envelope, now):
    """Choose native date bindings from bounded headers before loading bodies.

    The archive is not an operand of every receipt. Keep its original selection
    order and the exact/default/recovery rules, then budget only consumed bodies.
    The current builder replaces a header, but retains its persisted before CAS.
    """
    from packages.application.registry_upload_db_backed_runtime import _serialize_sheet_vitrina_plan
    from packages.application.web_vitrina_window_v3 import _ReadyHeader,_select_bindings,has_complete_recovery
    from packages.application.sheet_vitrina_v1 import _require_date_columns_or_infer
    from packages.business_time import default_business_as_of_date
    require=stages.require
    active=conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()
    require(active is not None,'historical_active_ready_bundle_missing')
    bundle=active[0]
    query=f'''SELECT bundle_version,as_of_date,activated_at,refreshed_at,
        json_extract(plan_json,'$.date_columns') AS dates_json,
        (SELECT json_extract(value,'$.header') FROM json_each(plan_json,'$.sheets')
         WHERE json_extract(value,'$.sheet_name')='DATA_VITRINA' LIMIT 1) AS header_json,
        json_extract(plan_json,'$.snapshot_id') AS snapshot_id
        FROM {ready.TABLE} WHERE bundle_version=?'''
    count,size=conn.execute(f'''SELECT count(*),coalesce(sum(
        length(CAST(coalesce(dates_json,'') AS BLOB))+
        length(CAST(coalesce(header_json,'') AS BLOB))+
        length(CAST(coalesce(snapshot_id,'') AS BLOB))+
        length(CAST(bundle_version||as_of_date||activated_at||refreshed_at AS BLOB))),0)
        FROM ({query})''',(bundle,)).fetchone()
    require(count<=MAX_READY_HEADERS and size<=MAX_READY_HEADER_BYTES,'historical_ready_header_size_limit')
    raw=[dict(r) for r in conn.execute(query+' ORDER BY activated_at DESC,refreshed_at DESC,as_of_date DESC',(bundle,))]
    prior_raw=list(raw)
    overlay=None
    if current_envelope is not None:
        envelope,expected=current_envelope
        require(expected.authority==authority and envelope.as_of_date==expected.as_of_date
                and expected.bundle_version==bundle,'historical_current_envelope_authority_changed')
        ready.check_expected(conn,expected)
        ready.check_build_inputs(conn,envelope.metadata['publication_inputs'])
        encoded=_serialize_sheet_vitrina_plan(envelope)
        require(len(encoded.encode())<=MAX_BYTES,'historical_ready_source_size_limit')
        overlay=dict(bundle_version=bundle,as_of_date=expected.as_of_date,plan_json=encoded,
                     _expected=asdict(expected),_build_inputs=envelope.metadata['publication_inputs'])
        raw=[r for r in raw if r['as_of_date']!=expected.as_of_date]
        raw.append(dict(bundle_version=bundle,as_of_date=expected.as_of_date,
                        snapshot_id=envelope.snapshot_id,dates_json=canonical(envelope.date_columns),
                        header_json=canonical(next(s.header for s in envelope.sheets if s.sheet_name=='DATA_VITRINA')),
                        activated_at='',refreshed_at=''))
    headers=[]
    header_bytes=0
    for row in sorted(raw,key=lambda r:(r['activated_at'],r['refreshed_at'],r['as_of_date']),reverse=True):
        header_bytes+=len(canonical(row).encode())
        require(header_bytes<=MAX_READY_HEADER_BYTES and len(headers)<MAX_READY_HEADERS,'historical_ready_header_size_limit')
        header=json.loads(row['header_json'] or '[]')
        require(isinstance(header,list),'historical_ready_header_invalid')
        dates=_require_date_columns_or_infer({'date_columns':json.loads(row['dates_json']) if row['dates_json'] is not None else None},header)
        headers.append(_ReadyHeader(bundle,row['as_of_date'],row['snapshot_id'],row['activated_at'],row['refreshed_at'],tuple(dates),'{}',0))
    recovery_bytes=0
    recovery_cache={}
    def bounded_recovery(connection,header,day):
        nonlocal recovery_bytes
        key=header.key
        if key not in recovery_cache:
            size=connection.execute(f"SELECT length(CAST(json_extract(plan_json,'$.metadata.server_cell_presentation') AS BLOB)) FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?",header.key).fetchone()
            recovery_bytes+=(size[0] or 0) if size else 0
            require(recovery_bytes<=MAX_BYTES,'historical_ready_recovery_size_limit')
            row=connection.execute(f"SELECT json_extract(plan_json,'$.metadata.server_cell_presentation') FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?",key).fetchone()
            try:recovery_cache[key]=json.loads(row[0] or '{}') if row else {}
            except json.JSONDecodeError:recovery_cache[key]={}
        return has_complete_recovery({'server_cell_presentation':recovery_cache[key]},day)
    bindings,_=_select_bindings(conn,headers,plan['scope']['dates'],bundle,default_business_as_of_date(now),recovery_check=bounded_recovery)
    selected={b.date:b.source_key for b in bindings}
    promoted={}
    missing=[day for day,key in selected.items() if key is None]
    if missing and current_envelope is not None:
        # The old default may have served its next-day column legitimately.
        # Advancing the default must not discard that accepted column. Promote
        # only a prior native binding; absent historical facts remain absent.
        prior_headers=[]
        for row in prior_raw:
            dates=_require_date_columns_or_infer({'date_columns':json.loads(row['dates_json']) if row['dates_json'] is not None else None},json.loads(row['header_json'] or '[]'))
            prior_headers.append(_ReadyHeader(bundle,row['as_of_date'],row['snapshot_id'],row['activated_at'],row['refreshed_at'],tuple(dates),'{}',0))
        prior_bindings,_=_select_bindings(conn,prior_headers,missing,bundle,default_business_as_of_date(now),recovery_check=bounded_recovery)
        for binding in prior_bindings:
            day=binding.date
            if binding.source_key is not None and day<current_envelope[0].as_of_date:
                key=(bundle,day)
                require(conn.execute(f'SELECT 1 FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?',key).fetchone() is None,
                        'historical_ready_promotion_target_exists')
                promoted[key]=binding.source_key;selected[day]=key
    require(set(selected)==set(plan['scope']['dates']) and all(selected.values()),'historical_exact_ready_dates_missing')
    keys=sorted(set(selected.values()))
    total=0
    for key in keys:
        if overlay is not None and key==(bundle,overlay['as_of_date']):
            total+=len(overlay['plan_json'].encode())+len((overlay['_expected']['plan_json'] or '').encode())
        else:
            row=conn.execute(f'SELECT length(CAST(plan_json AS BLOB)) FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?',promoted.get(key,key)).fetchone()
            require(row is not None,'historical_exact_ready_dates_missing')
            total+=row[0]*(2 if key in promoted else 1)
        require(total<=MAX_BYTES,'historical_ready_source_size_limit')
    rows=[]
    for key in keys:
        if overlay is not None and key==(bundle,overlay['as_of_date']):rows.append(overlay)
        elif key in promoted:
            donor=ready.capture_expected(conn,bundle_version=promoted[key][0],as_of_date=promoted[key][1],authority=authority)
            rows.append(dict(bundle_version=bundle,as_of_date=key[1],plan_json=_promoted_envelope(donor,key[1]),
                             _expected=asdict(ready.ExpectedReady(bundle,key[1],None,authority)),_donor_expected=asdict(donor)))
        else:rows.append(dict(conn.execute(f'SELECT * FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?',key).fetchone()))
    return rows,selected


def _expected(row, authority):
    return ready.ExpectedReady(row["bundle_version"],row["as_of_date"],row["plan_json"],authority)


def _code():
    result=staging._code_authority()
    for path in (Path(__file__),Path(stages.__file__),Path(sources.__file__)):
        result[path.name]="sha256:"+hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def materialize_exact_dates(plan, *, book, version, runtime_dir, target, dates, now, connection=None):
    """Native cells, bindings and economics; immutable untouched date prefix."""
    from packages.application.registry_upload_db_backed_runtime import _serialize_sheet_vitrina_plan,_deserialize_sheet_vitrina_plan
    prior=json.loads(_serialize_sheet_vitrina_plan(plan))
    changed=set(plan.date_columns)&set(dates)
    require=stages.require
    require(changed,"historical_ready_target_has_no_revised_date")
    result=accounting.materialize(plan,runtime_dir=runtime_dir,book=book,book_version=version,ready_target=target,now=now)
    payload=json.loads(_serialize_sheet_vitrina_plan(result))
    # Native materialize regenerates all book bindings. Restore every other
    # binding and cell byte-for-byte, including non-book source presentations.
    oldmeta=prior.get("metadata",{});meta=payload.setdefault("metadata",{})
    for key in ("server_cell_presentation","fbs_accounting_bindings","fbs_accounting_targets"):
        old=oldmeta.get(key,{})
        new=meta.setdefault(key,{})
        if key=="server_cell_presentation":
            for identity in set(old)|set(new):
                out=new.setdefault(identity,{})
                for day in set(old.get(identity,{}))|set(out):
                    if day not in changed:
                        if day in old.get(identity,{}):out[day]=deepcopy(old[identity][day])
                        else:out.pop(day,None)
        else:
            for day in set(old)|set(new):
                if day not in changed:
                    if day in old:new[day]=deepcopy(old[day])
                    else:new.pop(day,None)
    meta["ready_publication_target"]=deepcopy(target)
    for day in changed:meta.setdefault("fbs_accounting_targets",{})[day]=deepcopy(target)
    for sheet in payload["sheets"]:
        if sheet["sheet_name"]!="DATA_VITRINA":continue
        oldsheet=next(s for s in prior["sheets"] if s["sheet_name"]=="DATA_VITRINA")
        oldrows={r[1]:r for r in oldsheet["rows"]}
        for row in sheet["rows"]:
            for day in set(plan.date_columns)-changed:
                index=sheet["header"].index(day)
                row[index]=deepcopy(oldrows[row[1]][oldsheet["header"].index(day)]) if row[1] in oldrows else ""
    from packages.application.web_vitrina_management_history import dated_parameters,recalculate_dated_proxy
    require(connection is not None,'historical_dated_parameters_connection_required')
    for day in sorted(changed):
        payload=recalculate_dated_proxy(payload,day=day,parameters=dated_parameters(connection,day),
            operation_id='historical-receipt:'+version)['plan']
    return _deserialize_sheet_vitrina_plan(canonical(payload))


def prepare_publication(plan, *, runtime_dir, owner_generation, operation_id, attempt_id="1", now=None, current_envelope=None):
    """Independent native rebuild outside BEGIN IMMEDIATE, with exact source CAS."""
    started=time.monotonic();now=now or datetime.now(timezone.utc)
    for value in (owner_generation,operation_id,attempt_id):
        stages.require(isinstance(value,str) and value.strip()==value and 0<len(value)<=240,"historical_publication_identity_invalid")
    authority=ready.capture_authority(runtime_dir);db=Path(authority["path"])
    book,expected=accounting.load(runtime_dir)
    stages.require(expected==plan["before_book_digest"],"historical_publication_book_changed")
    with ready.readonly(db) as conn:
        staging._schema_ready(conn)
        capture=staging._capture(conn,db,plan,now,book)
        current=plan.get("current_document_ids",[]);aux=plan.get("auxiliary_document_ids",[])
        capture=sources.augment_native_requests(conn,capture,document_ids=[*plan["receipt_document_ids"],*current,*aux])
        validate_historical_revision_plan(plan,book=book,capture=capture)
        dated=stages.capture_dated_stages(conn,db_path=db,book=plan["candidate_book"],dates=plan["scope"]["dates"])
        candidate=stages.build_dated_stage_revision(plan,capture=capture,dated=dated)
        # Select only the active bundle's actual native ready rows. An absent
        # dated target is an input gap, not permission to invent a ready source.
        from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan,_serialize_sheet_vitrina_plan
        rows,selected=_selected_ready_rows(conn,plan=plan,authority=authority,
            current_envelope=current_envelope,now=now)
        targets=[];covered=set()
        for row in rows:
            envelope=_deserialize_sheet_vitrina_plan(row["plan_json"])
            dates=sorted(d for d,key in selected.items() if key==(row['bundle_version'],row['as_of_date']))
            if not dates:continue
            expected_ready=ready.ExpectedReady(**row["_expected"]) if "_expected" in row else _expected(row,authority)
            target={"bundle_version":row["bundle_version"],"as_of_date":row["as_of_date"]}
            after=materialize_exact_dates(envelope,book=candidate["candidate_book"],version=candidate["after_book"],runtime_dir=runtime_dir,target=target,dates=dates,now=now,connection=conn)
            from packages.application.inventory_quantity import resolve_plan_quantities,CONTRACT as QUANTITY_CONTRACT
            from packages.application.sheet_vitrina_v1_inventory_history import FINALIZATIONS_TABLE
            inventory=[]
            for day in dates:
                operands=resolve_plan_quantities(after,day=day,prepared_book=candidate['candidate_book'],ready_target=target,require_closed=day<plan['scope']['date_to'])
                stages.require(operands is not None,'historical_inventory_exact_quantity_source_missing',day=day)
                prior=conn.execute(f'SELECT finalization_digest FROM {FINALIZATIONS_TABLE} WHERE business_date=? ORDER BY finalization_sequence DESC LIMIT 1',(day,)).fetchone()
                inventory.append(dict(business_date=day,capture_kind='accepted_refresh',formula_version=QUANTITY_CONTRACT,
                    bundle_version=row['bundle_version'],ready_snapshot_id=after.snapshot_id,ready_plan_version=after.plan_version,generation_identity=row['bundle_version'],
                    facility_roster=operands['facility_roster'],source_manifest=operands['source_manifest'],components=operands['components'],captured_at=plan['observation']['captured_at'],
                    expected_predecessor=prior[0] if prior else '',closed=day<plan['scope']['date_to']))
            targets.append({"inventory":inventory,"expected":asdict(expected_ready),"after_json":_serialize_sheet_vitrina_plan(after),"dates":dates,"before_envelope_json":row["plan_json"],"build_inputs":row.get("_build_inputs"),"donor_expected":row.get("_donor_expected")})
            covered.update(dates)
        # Each business date owns exactly one quantity finalization. The real
        # current-date ready target owns the durable publication intent even
        # when many historical envelopes overlap the same suffix.
        targets.sort(key=lambda t:(plan['scope']['date_to'] in t['dates'],t['expected']['as_of_date']))
        stages.require(covered==set(plan["scope"]["dates"]),"historical_exact_ready_dates_missing",source=",".join(sorted(set(plan["scope"]["dates"])-covered)))
        material=ready.capture_material(conn,tables=staging.SOURCE_TABLES)
        original_source_fence=json.loads(canonical(ready.pin_queries(conn,sources.original_source_queries(capture))))
        parameter_proof=_parameter_proof(conn,plan['scope']['dates'])
        source_confirmation={identity:{"fingerprint":next(d["fingerprint"] for d in capture["documents"] if d["document_id"]==identity),"posted_manifest_json":capture["posted_manifest_json_by_id"][identity]} for identity in plan["receipt_document_ids"]}
        operator_confirmation=sources.verify_cohort_confirmations(conn,plan=plan,capture=capture)
    result={"contract":CONTRACT,"operation_id":operation_id,"attempt_id":attempt_id,"owner_generation":owner_generation,
        "authority":authority,"file_authority":staging._file_authority(runtime_dir,db),"code_authority":_code(),
        "expected_book":expected,"after_book":candidate["after_book"],"candidate":candidate,"targets":targets,
        "source_revisions":material,"source_confirmation":source_confirmation,"operator_confirmation":operator_confirmation,"plan":plan,
        "original_capture":capture,"original_source_fence":original_source_fence,"parameter_proof":parameter_proof,
        "book_operation_id":"historical-publish:"+fingerprint([operation_id,attempt_id])[7:],"created_at":plan["observation"]["captured_at"]}
    _json_size(result,MAX_EXPANDED_BYTES,'historical_manifest_expanded_size_limit')
    stages.require(_same_json(expand_manifest(compact_manifest(result)),result),'historical_manifest_roundtrip_changed')
    result["manifest_digest"]=fingerprint(result)
    result["prepare_seconds"]=time.monotonic()-started
    stages.require(_same_json(expand_manifest(compact_manifest(result)),result),'historical_manifest_roundtrip_changed')
    return result


def _verify(manifest):
    stages.require(manifest["contract"]==CONTRACT and manifest["manifest_digest"]==fingerprint({k:v for k,v in manifest.items() if k not in {"manifest_digest","prepare_seconds"}}),"historical_publication_manifest_corrupt")
    stages.require(_code()==manifest["code_authority"],"historical_publication_code_changed")
    stages.require(fingerprint(manifest["candidate"]["candidate_book"])==manifest["after_book"],"historical_publication_candidate_corrupt")


def _fence(conn,manifest,runtime_dir,*,recovering=False):
    ready.check_pinned_authority(conn,manifest["authority"])
    stages.require(staging._file_authority(runtime_dir,manifest["authority"]["path"])==manifest["file_authority"],"historical_publication_file_replaced")
    if not recovering:ready.check_material(conn,manifest["source_revisions"])
    stages.check_query_fence(conn,manifest["candidate"]["query_fence"])
    stages.check_query_fence(conn,manifest['original_source_fence'])
    from packages.application.operator_warehouse_documents import TABLE
    for identity,expected in manifest["operator_confirmation"].items():
        actual=conn.execute(f"SELECT source_json,source_digest,accepted_at,actor FROM {TABLE} WHERE request_id=?",(identity,)).fetchone()
        stages.require(actual and dict(actual)==expected,"historical_operator_confirmation_changed")
    for target in manifest['targets']:
        ready.check_expected(conn,ready.ExpectedReady(**target['expected']))
        if target.get('donor_expected'):ready.check_expected(conn,ready.ExpectedReady(**target['donor_expected']))
        if not recovering:ready.check_build_inputs(conn,target.get('build_inputs'))
    if recovering:
        # Exact immutable inputs replace a current-generation epoch only once
        # this intent already owns the activated book. Unrelated new-day facts
        # remain outside this source set and are handled by ordinary refresh.
        stages.require(_parameter_proof(conn,manifest['plan']['scope']['dates'])==manifest['parameter_proof'],'historical_original_dated_parameters_changed')


def _independent_rebuild(manifest,runtime_dir,*,recovering=False):
    """Do not treat caller-provided hashes as numerical authorization."""
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan,_serialize_sheet_vitrina_plan
    now=datetime.fromisoformat(manifest["created_at"].replace("Z","+00:00"))
    book,version=accounting.load(runtime_dir,version=manifest["expected_book"])
    stages.require(version==manifest["plan"]["before_book_digest"],"historical_before_book_missing")
    with ready.readonly(Path(manifest["authority"]["path"])) as conn:
        if recovering:
            capture=sources.capture_archived_cohort(conn,plan=manifest['plan'],original_capture=manifest['original_capture'])
        else:
            capture=staging._capture(conn,Path(manifest["authority"]["path"]),manifest["plan"],now,book)
            capture=sources.augment_native_requests(conn,capture,document_ids=[*manifest["plan"]["receipt_document_ids"],
                *manifest["plan"].get("current_document_ids",[]),*manifest["plan"].get("auxiliary_document_ids",[])])
        validate_historical_revision_plan(manifest["plan"],book=book,capture=capture)
        dated=stages.capture_dated_stages(conn,db_path=Path(manifest["authority"]["path"]),book=manifest["plan"]["candidate_book"],dates=manifest["plan"]["scope"]["dates"])
        candidate=stages.build_dated_stage_revision(manifest["plan"],capture=capture,dated=dated)
        stages.require(candidate==manifest["candidate"],"historical_numeric_or_stage_rebuild_mismatch")
        for target in manifest["targets"]:
            expected=ready.ExpectedReady(**target["expected"])
            if target.get('donor_expected'):
                donor=ready.ExpectedReady(**target['donor_expected'])
                ready.check_expected(conn,donor)
                stages.require(expected.plan_json is None and expected.bundle_version==donor.bundle_version
                    and _promoted_envelope(donor,expected.as_of_date)==target['before_envelope_json'],
                    'historical_ready_donor_changed')
            envelope=_deserialize_sheet_vitrina_plan(target["before_envelope_json"])
            rebuilt=materialize_exact_dates(envelope,book=candidate["candidate_book"],version=candidate["after_book"],runtime_dir=runtime_dir,
                target={"bundle_version":expected.bundle_version,"as_of_date":expected.as_of_date},dates=target["dates"],now=now,connection=conn)
            stages.require(_serialize_sheet_vitrina_plan(rebuilt)==target['after_json'],'historical_ready_numeric_rebuild_mismatch')
            from packages.application.inventory_quantity import resolve_plan_quantities
            for item in target['inventory']:
                operands=resolve_plan_quantities(rebuilt,day=item['business_date'],prepared_book=candidate['candidate_book'],ready_target={'bundle_version':expected.bundle_version,'as_of_date':expected.as_of_date},require_closed=item['closed'])
                stages.require(all(operands[k]==item[k] for k in ('facility_roster','source_manifest','components')),'historical_inventory_numeric_rebuild_mismatch')


def _append_and_activate(runtime_dir,manifest):
    with closing(sqlite3.connect(accounting.path(runtime_dir).as_uri()+"?mode=rw",uri=True,timeout=0)) as conn,conn:
        conn.execute("BEGIN IMMEDIATE");accounting.admit(conn)
        current=conn.execute("SELECT version FROM accounting_current WHERE singleton=1").fetchone()[0]
        prior=conn.execute("SELECT version,payload,previous_version FROM accounting_revisions WHERE operation_id=?",(manifest["book_operation_id"],)).fetchone()
        stages.require(current in {manifest["expected_book"],manifest["after_book"]},"historical_book_current_cas_failed")
        if prior:
            stages.require(prior[0]==manifest["after_book"] and prior[2]==manifest["expected_book"] and accounting.unpack(conn,prior[1])==manifest["candidate"]["candidate_book"],"historical_book_retry_identity_conflict")
        else:
            stages.require(current==manifest["expected_book"],"historical_book_revision_missing")
            conn.execute("INSERT INTO accounting_revisions VALUES(?,?,?,?)",(manifest["after_book"],manifest["book_operation_id"],accounting.pack(conn,manifest["candidate"]["candidate_book"]),manifest["expected_book"]))
        if current!=manifest["after_book"]:
            stages.require(conn.execute("UPDATE accounting_current SET version=? WHERE singleton=1 AND version=?",(manifest["after_book"],manifest["expected_book"])).rowcount==1,"historical_book_current_cas_failed")


def publish(manifest, *, runtime_dir, fault_injector=None):
    """Exact identity recovery; actual ready target, live owner, short source CAS."""
    manifest=expand_manifest(manifest)
    _verify(manifest);require_warehouse_job_owner(Path(runtime_dir))
    stored_manifest=compact_manifest(manifest)
    stages.require(_same_json(expand_manifest(stored_manifest),manifest),'historical_manifest_roundtrip_changed')
    from packages.application.web_vitrina_window_read_context import active_window_read_context
    stages.require(active_window_read_context() is None,'historical_publication_window_reader_active')
    stages.require(Path(runtime_dir).resolve()==Path(manifest["authority"]["runtime_dir"]),"historical_runtime_changed")
    inject=fault_injector or (lambda _boundary:None)
    db=Path(manifest["authority"]["path"]);started=time.monotonic()
    status=ready.publication_status(db,operation_id=manifest["operation_id"],attempt_id=manifest["attempt_id"])
    _,current_book=accounting.load(runtime_dir)
    recovering=bool(status and status['state']!='complete' and current_book in {manifest['expected_book'],manifest['after_book']})
    if not status or status["state"]!="complete":_independent_rebuild(manifest,runtime_dir,recovering=recovering)
    with warehouse_functional_write_lock(Path(runtime_dir),timeout_seconds=5),accounting.writer_lock(runtime_dir):
        with closing(sqlite3.connect(db.as_uri()+"?mode=rw",uri=True,timeout=0)) as conn:
            conn.row_factory=sqlite3.Row
            existing=conn.execute("SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?",(manifest["operation_id"],manifest["attempt_id"])).fetchone()
            if existing:
                stored=json.loads(existing["inputs_json"])
                stages.require(stored["manifest_digest"]==manifest["manifest_digest"],"historical_publication_retry_identity_conflict")
                if existing["state"]=="complete":return publication_receipt(manifest,runtime_dir=runtime_dir)
            with conn:
                conn.execute("BEGIN IMMEDIATE");_fence(conn,manifest,runtime_dir,recovering=recovering)
                if not existing:
                    ready.record_intent(conn,operation_id=manifest["operation_id"],attempt_id=manifest["attempt_id"],kind=CONTRACT,
                        expected=ready.ExpectedReady(**manifest["targets"][-1]["expected"]),inputs=stored_manifest,expected_book=manifest["expected_book"],book_required=True,ready_required=True,created_at=manifest["created_at"],book_operation_id=manifest["book_operation_id"])
            inject("after_intent")
            with conn:
                conn.execute("BEGIN IMMEDIATE");_fence(conn,manifest,runtime_dir,recovering=recovering)
                _append_and_activate(runtime_dir,manifest);inject("after_book")
                stages.publish_dated_stages(conn,manifest=manifest["candidate"]);inject("after_stages")
                for target in manifest["targets"]:
                    envelope=json.loads(target['after_json'])
                    ready.replace_ready(conn,expected=ready.ExpectedReady(**target['expected']),plan_json=target['after_json'],
                        activated_at=manifest['created_at'],snapshot_id=envelope['snapshot_id'],plan_version=envelope['plan_version'],refreshed_at=manifest['created_at'])
                    from packages.application.sheet_vitrina_v1_inventory_history import append_inventory_history_capture,append_inventory_history_finalization
                    for item in target['inventory']:
                        value={k:v for k,v in item.items() if k not in {'expected_predecessor','closed'}}
                        capture=append_inventory_history_capture(conn,**value)
                        if item['closed']:
                            append_inventory_history_finalization(conn,business_date=item['business_date'],capture_id=capture['capture_id'],
                                finalization_identity=manifest['operation_id']+':'+item['business_date'],finalized_at=manifest['created_at'],
                                expected_predecessor=item['expected_predecessor'],provenance=dict(contract=CONTRACT,manifest_digest=manifest['manifest_digest'],book_version=manifest['after_book']))
                inject("after_ready")
                ready.complete_publication(conn,operation_id=manifest["operation_id"],attempt_id=manifest["attempt_id"],book_version=manifest["after_book"],after_digest=ready.digest(manifest["targets"][-1]["after_json"]),finished_at=manifest["created_at"])
    result=publication_receipt(manifest,runtime_dir=runtime_dir);result["writer_seconds"]=time.monotonic()-started
    return result


def publication_receipt(manifest, *, runtime_dir):
    manifest=expand_manifest(manifest)
    _verify(manifest)
    book,version=accounting.load(runtime_dir)
    stages.require(version==manifest["after_book"] and book==manifest["candidate"]["candidate_book"],"historical_publication_book_readback_changed")
    with ready.readonly(Path(manifest["authority"]["path"])) as conn:
        ready.check_pinned_authority(conn,manifest["authority"])
        row=conn.execute("SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?",(manifest["operation_id"],manifest["attempt_id"])).fetchone()
        stages.require(row and row["state"]=="complete" and row["book_version"]==version,"historical_publication_intent_not_complete")
        for target in manifest["targets"]:
            expected=ready.ExpectedReady(**target["expected"])
            actual=ready.capture_expected(conn,bundle_version=expected.bundle_version,as_of_date=expected.as_of_date)
            stages.require(actual.plan_json==target["after_json"],"historical_ready_readback_changed")
        for day,edition in manifest["candidate"]["editions"].items():
            actual=dict(conn.execute('SELECT * FROM '+stages.P+'functional_versions WHERE version_id=?',(edition['version_id'],)).fetchone() or {})
            stages.require(actual==edition['version'],'historical_stage_readback_changed')
            balances=stages._version_balances(conn,version_id=edition['version_id'])
            stages.require(balances==edition['balances'],'historical_stage_balances_readback_changed')
            models=list(conn.execute('SELECT warehouse_key,payload_json FROM '+stages.P+'functional_read_models WHERE version_id=?',(edition['version_id'],)))
            stages.require({r[0] for r in models}==set(stages.STAGES.values()),'historical_stage_models_readback_missing')
            for key,serialized in models:
                value=json.loads(serialized);expected=[r for r in balances if r['warehouse_key']==key]
                stages.require(value.get('status')=='ready' and [(r['nm_id'],r['quantity'],r['capital_rub']) for r in value.get('balances',[])]==[(r['nm_id'],r['quantity'],r['capital_rub']) for r in expected]
                    and stages.decimal(value['warehouse']['total_quantity'])==sum((stages.decimal(r['quantity']) for r in expected),stages.Decimal(0))
                    and stages.decimal(value['warehouse']['total_capital_rub'])==sum((stages.decimal(r['capital_rub']) for r in expected),stages.Decimal(0)),'historical_stage_models_readback_changed')
            for table,old_rows in edition['companions'].items():
                expected=[]
                for old in old_rows:
                    value={**old,'version_id':edition['version_id']}
                    if table=='unmatched_doprinato':value['unmatched_id']='historical-unmatched:'+fingerprint([edition['version_id'],old['unmatched_id']])[7:]
                    expected.append(value)
                actual=[dict(r) for r in conn.execute(f'SELECT * FROM {stages.P}{table} WHERE version_id=?',(edition['version_id'],))]
                stages.require(sorted(actual,key=canonical)==sorted(expected,key=canonical),'historical_stage_companion_readback_changed')
    return {"contract":CONTRACT,"status":"published","operation_id":manifest["operation_id"],"attempt_id":manifest["attempt_id"],"book_version":version,
        "manifest_digest":manifest["manifest_digest"],"dates":manifest["plan"]["scope"]["dates"],"native_dated_stages":True,"ready":True,
        "history_complete":False,"finance_complete":False,"operator_complete":False,
        "version":version,"date":max(book["shared_days"]),"ready_obligation":"complete"}
