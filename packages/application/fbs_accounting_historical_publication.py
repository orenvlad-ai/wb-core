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
        count,size=conn.execute(f'SELECT count(*),coalesce(sum(length(ready.plan_json)),0) FROM {ready.TABLE} ready JOIN registry_upload_current_state current ON current.bundle_version=ready.bundle_version AND current.slot=1').fetchone()
        stages.require(count<=10000 and size<=MAX_BYTES,'historical_ready_source_size_limit')
        rows=[dict(r) for r in conn.execute(f"SELECT ready.* FROM {ready.TABLE} ready JOIN registry_upload_current_state current ON current.bundle_version=ready.bundle_version AND current.slot=1 ORDER BY ready.as_of_date")]
        if current_envelope is not None:
            envelope,expected_ready=current_envelope
            stages.require(expected_ready.authority==authority and envelope.as_of_date==expected_ready.as_of_date
                and expected_ready.bundle_version==conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()[0], 'historical_current_envelope_authority_changed')
            ready.check_expected(conn,expected_ready)
            ready.check_build_inputs(conn,envelope.metadata['publication_inputs'])
            rows=[r for r in rows if (r['bundle_version'],r['as_of_date'])!=(expected_ready.bundle_version,expected_ready.as_of_date)]
            rows.append(dict(bundle_version=expected_ready.bundle_version,as_of_date=expected_ready.as_of_date,
                plan_json=_serialize_sheet_vitrina_plan(envelope),_expected=asdict(expected_ready),_build_inputs=envelope.metadata['publication_inputs']))
        from packages.application.web_vitrina_window_v3 import _ReadyHeader,_select_bindings
        from packages.business_time import default_business_as_of_date
        headers=[]
        for row in sorted(rows,key=lambda r:(r.get('activated_at',''),r.get('refreshed_at',''),r['as_of_date']),reverse=True):
            env=_deserialize_sheet_vitrina_plan(row['plan_json'])
            headers.append(_ReadyHeader(row['bundle_version'],row['as_of_date'],env.snapshot_id,row.get('activated_at',plan['observation']['captured_at']),row.get('refreshed_at',plan['observation']['captured_at']),tuple(env.date_columns),canonical(env.metadata.get('fbs_accounting_bindings',{})),0))
        active_bundle=conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()[0]
        bindings,_=_select_bindings(conn,headers,plan['scope']['dates'],active_bundle,default_business_as_of_date(now))
        selected={binding.date:binding.source_key for binding in bindings}
        stages.require(all(selected.values()),'historical_exact_ready_dates_missing')
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
            targets.append({"inventory":inventory,"expected":asdict(expected_ready),"after_json":_serialize_sheet_vitrina_plan(after),"dates":dates,"before_envelope_json":row["plan_json"],"build_inputs":row.get("_build_inputs")})
            covered.update(dates)
        # Each business date owns exactly one quantity finalization. The real
        # current-date ready target owns the durable publication intent even
        # when many historical envelopes overlap the same suffix.
        targets.sort(key=lambda t:(plan['scope']['date_to'] in t['dates'],t['expected']['as_of_date']))
        stages.require(covered==set(plan["scope"]["dates"]),"historical_exact_ready_dates_missing",source=",".join(sorted(set(plan["scope"]["dates"])-covered)))
        material=ready.capture_material(conn,tables=staging.SOURCE_TABLES)
        original_source_fence=json.loads(canonical(ready.pin_queries(conn,sources.original_source_queries(capture))))
        from packages.application.web_vitrina_management_history import dated_parameters
        parameter_proof={day:dated_parameters(conn,day) for day in plan['scope']['dates']}
        source_confirmation={identity:{"fingerprint":next(d["fingerprint"] for d in capture["documents"] if d["document_id"]==identity),"posted_manifest_json":capture["posted_manifest_json_by_id"][identity]} for identity in plan["receipt_document_ids"]}
        operator_confirmation=sources.verify_cohort_confirmations(conn,plan=plan,capture=capture)
    result={"contract":CONTRACT,"operation_id":operation_id,"attempt_id":attempt_id,"owner_generation":owner_generation,
        "authority":authority,"file_authority":staging._file_authority(runtime_dir,db),"code_authority":_code(),
        "expected_book":expected,"after_book":candidate["after_book"],"candidate":candidate,"targets":targets,
        "source_revisions":material,"source_confirmation":source_confirmation,"operator_confirmation":operator_confirmation,"plan":plan,
        "original_capture":capture,"original_source_fence":original_source_fence,"parameter_proof":parameter_proof,
        "book_operation_id":"historical-publish:"+fingerprint([operation_id,attempt_id])[7:],"created_at":plan["observation"]["captured_at"]}
    stages.require(len(canonical(result).encode())<=MAX_BYTES,"historical_publication_size_limit")
    result["manifest_digest"]=fingerprint(result)
    result["prepare_seconds"]=time.monotonic()-started
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
        if not recovering:ready.check_build_inputs(conn,target.get('build_inputs'))
    if recovering:
        # Exact immutable inputs replace a current-generation epoch only once
        # this intent already owns the activated book. Unrelated new-day facts
        # remain outside this source set and are handled by ordinary refresh.
        from packages.application.web_vitrina_management_history import dated_parameters
        stages.require({d:dated_parameters(conn,d) for d in manifest['plan']['scope']['dates']}==manifest['parameter_proof'],'historical_original_dated_parameters_changed')


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
    _verify(manifest);require_warehouse_job_owner(Path(runtime_dir))
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
                        expected=ready.ExpectedReady(**manifest["targets"][-1]["expected"]),inputs=manifest,expected_book=manifest["expected_book"],book_required=True,ready_required=True,created_at=manifest["created_at"],book_operation_id=manifest["book_operation_id"])
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
