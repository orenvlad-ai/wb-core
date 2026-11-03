"""Typed FBO owner of the existing append-only book/Ready publication phases.

Native dispatches are verified foundation sources, never receipt confirmations.
Only actual saved quantity dates enter the Ready and History target set. Missing
quantity days and provisional WB cost remain explicitly unavailable/provisional.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import json
import time

from packages.application import fbs_accounting_historical_publication as publication
from packages.application import fbs_accounting_runtime as accounting
from packages.application import fbs_accounting_historical_stages as stages
from packages.application import fbs_accounting_historical_sources as receipt_sources
from packages.application import fbs_accounting_historical_revision_writer as staging
from packages.application import ready_publication as ready
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.fbs_historical_fbo_gap import build_historical_fbo_state_revision
from packages.application.fbs_historical_fbo_native import _verify_existing, operation_id
from packages.application.fbs_snapshot_cost_sources import capture_current, _documents

CONTRACT = publication.FBO_CONTRACT


def _stage_adapter():
    from packages.application import fbs_historical_fbo_stages
    return fbs_historical_fbo_stages


def _adapter_code():
    from packages.application import (fbs_historical_fbo_native, fbs_historical_fbo_gap, fbs_historical_fbo_sources,
        fbs_historical_fbo_dispatch, fbs_historical_fbo_stage_sources, our_wb_costs, fulfillment_services,
        operator_fulfillment_services, fulfillment_recalc_intents, warehouse_functional,
        warehouse_archival_estimate, wb_initial_fbs_cost)
    paths = [Path(module.__file__) for module in (_stage_adapter(),fbs_historical_fbo_native,
        fbs_historical_fbo_gap,fbs_historical_fbo_sources,fbs_historical_fbo_dispatch,
        fbs_historical_fbo_stage_sources,our_wb_costs,fulfillment_services,
        operator_fulfillment_services,fulfillment_recalc_intents,warehouse_functional,
        warehouse_archival_estimate,wb_initial_fbs_cost)]
    return {p.name:'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def _healthy(book):
    stages.require(isinstance(book,dict) and book.get('schema') == accounting.SCHEMA
                   and book.get('active') is True and not book.get('publication_error'),
                   'historical_fbo_publication_healthy_book_required')


def _source_queries(capture,native_plan):
    # The receipt helper pins ordinary documents and exact official generations;
    # typed foundation and canonical aggregate rows need their own honest fence.
    ordinary = {**capture,'native_requests_by_id':{}}
    queries = receipt_sources.original_source_queries(ordinary)
    ids = tuple(operation_id(native_plan,i) for i in range(4));marks=','.join('?' for _ in ids)
    for table,order in (('warehouse_business_operations','operation_id'),('ff_pool_movement_lines','operation_id,line_no'),
                        ('ff_stock_operations','operation_id'),('ff_stock_operation_lines','operation_id,line_no')):
        queries.append((f'SELECT * FROM sheet_vitrina_v1_{table} WHERE operation_id IN ({marks}) ORDER BY {order}',ids))
    queries.append(("SELECT operation_id FROM sheet_vitrina_v1_warehouse_business_operations WHERE source_system='historical_fbo_dispatch' ORDER BY operation_id",()))
    return queries


def _capture(conn, *, db, now, native_plan, original=None):
    stages.require(_verify_existing(conn,native_plan),'historical_fbo_publication_native_cohort_required')
    if original is None:
        capture = capture_current(db,now=now,include_baseline=False,connection=conn)
    else:
        # Recovery uses the same immutable generation and selected documents,
        # rather than taking over a later ordinary refresh's new-day sources.
        capture = deepcopy(original)
        saved = original['quantity_snapshot']
        rebuilt = receipt_sources.archived_quantity_snapshot(conn,day=original['business_date'],
            observed_at=original['captured_at'],generation_id=saved['id'])
        stages.require(rebuilt==saved,'historical_fbo_original_quantity_changed')
        current={d['document_id']:d for d in _documents(conn)}
        wanted={d['document_id']:d['fingerprint'] for d in original['documents']}
        stages.require(all(identity in current and current[identity]['fingerprint']==value for identity,value in wanted.items()),
                       'historical_fbo_original_document_changed')
        capture['documents']=[current[d['document_id']] for d in original['documents']]
        capture['source_digest']=fingerprint({k:capture[k] for k in
            ('contract','business_date','quantity_snapshot','documents_complete','documents','documents_reason')})
    stages.require(capture['documents_complete'] is True and capture['quantity_snapshot']['complete'] is True,
                   'historical_fbo_publication_complete_sources_required')
    return capture


def _build(conn, *, book, native_plan, accounting_capture, fee_scope):
    state_revision=build_historical_fbo_state_revision(book,accounting_capture)
    adapter=_stage_adapter()
    stage_capture=adapter.capture_historical_fbo_stages(conn,book=book,native_plan=native_plan,
        accounting_capture=accounting_capture,fee_scope=fee_scope)
    candidate=adapter.build_historical_fbo_stage_revision(state_revision,capture=stage_capture)
    dates=state_revision['derived_dates_to_rebuild']
    stages.require(set(candidate['editions'])==set(dates)
        and candidate['after_book']==fingerprint(candidate['candidate_book'])
        and set(candidate['candidate_book']['state']['periods'])==set(book['state']['periods']),
        'historical_fbo_stage_candidate_scope_changed')
    stages.require(candidate['manifest_digest']==fingerprint({k:v for k,v in candidate.items() if k!='manifest_digest'}),
                   'historical_fbo_stage_candidate_digest_invalid')
    return state_revision,stage_capture,candidate


def prepare_publication(native_plan, *, fee_scope, effect_binding, runtime_dir, owner_generation, operation_id,
                        attempt_id='1',now=None,current_envelope=None):
    """Independently capture real committed sources and build reviewed targets.

    This is publication preparation after native/fee source readback. The
    orchestrator must durably admit its effect recipe before those sources are
    written; this function alone does not claim that earlier obligation.
    """
    started=time.monotonic();now=now or datetime.now(timezone.utc)
    for identity in (owner_generation,operation_id,attempt_id):
        stages.require(isinstance(identity,str) and identity.strip()==identity and 0<len(identity)<=240,
                       'historical_fbo_publication_identity_invalid')
    authority=ready.capture_authority(runtime_dir);db=Path(authority['path'])
    book,expected=accounting.load(runtime_dir);_healthy(book)
    stages.require(expected==native_plan['source_proof']['book_version'],'historical_fbo_publication_book_changed')
    with ready.readonly(db) as conn:
        staging._schema_ready(conn)
        recipe=_bound_effect_recipe(conn,binding=effect_binding,native_plan=native_plan,expected_book=expected,fee_scope=fee_scope)
        stages.require(operation_id==recipe['publication_operation_id'] and attempt_id==recipe['publication_attempt_id'],
                       'historical_fbo_publication_child_identity_changed')
        capture=_capture(conn,db=db,now=now,native_plan=native_plan)
        state_revision,stage_capture,candidate=_build(conn,book=book,native_plan=native_plan,
            accounting_capture=capture,fee_scope=fee_scope)
        dates=state_revision['derived_dates_to_rebuild']
        stages.require(dates and capture['business_date']==dates[-1], 'historical_fbo_publication_current_period_missing')
        plan=dict(contract=CONTRACT,status='ready',before_book_digest=expected,candidate_book=candidate['candidate_book'],
            scope=dict(dates=dates,date_from=dates[0],date_to=dates[-1]),
            observation=dict(captured_at=capture['captured_at']),native_plan=deepcopy(native_plan),fee_scope=deepcopy(fee_scope),
            effect_binding=deepcopy(effect_binding),state_revision_digest=fingerprint(state_revision),source_proof=dict(document_manifest={d['document_id']:d['fingerprint'] for d in capture['documents']}))
        targets=publication.prepare_ready_targets(conn,plan=plan,candidate=candidate,runtime_dir=runtime_dir,
            authority=authority,now=now,current_envelope=current_envelope)
        material=ready.capture_material(conn,tables=staging.SOURCE_TABLES)
        fence=json.loads(canonical(ready.pin_queries(conn,_source_queries(capture,native_plan))))
        confirmation={d['document_id']:{'fingerprint':d['fingerprint']} for d in capture['documents']
                      if d.get('historical_fbo_native')}
        parameter_proof=publication._parameter_proof(conn,dates)
    result=dict(contract=CONTRACT,operation_id=operation_id,attempt_id=attempt_id,owner_generation=owner_generation,
        authority=authority,file_authority=staging._file_authority(runtime_dir,db),code_authority=publication._code(),
        adapter_code_authority=_adapter_code(),expected_book=expected,after_book=candidate['after_book'],candidate=candidate,
        targets=targets,source_revisions=material,source_confirmation=confirmation,operator_confirmation={},plan=plan,
        original_capture=capture,original_stage_capture=stage_capture,original_source_fence=fence,parameter_proof=parameter_proof,
        book_operation_id='historical-fbo-publish:'+fingerprint([operation_id,attempt_id])[7:],created_at=capture['captured_at'])
    publication._json_size(result,publication.MAX_EXPANDED_BYTES,'historical_manifest_expanded_size_limit')
    stages.require(publication._same_json(publication.expand_manifest(publication.compact_manifest(result)),result),
                   'historical_manifest_roundtrip_changed')
    result['manifest_digest']=fingerprint(result);result['prepare_seconds']=time.monotonic()-started
    return result


def independent_rebuild(manifest, *, runtime_dir, recovering=False):
    stages.require(manifest['adapter_code_authority']==_adapter_code(),'historical_fbo_publication_adapter_code_changed')
    plan=manifest['plan'];book,version=accounting.load(runtime_dir,version=manifest['expected_book']);_healthy(book)
    stages.require(version==plan['before_book_digest']==plan['native_plan']['source_proof']['book_version'],
                   'historical_fbo_original_book_changed')
    now=datetime.fromisoformat(manifest['created_at'].replace('Z','+00:00'))
    with ready.readonly(Path(manifest['authority']['path'])) as conn:
        stages.check_query_fence(conn,manifest['original_source_fence'])
        _bound_effect_recipe(conn,binding=plan['effect_binding'],native_plan=plan['native_plan'],expected_book=version,fee_scope=plan['fee_scope'])
        capture=_capture(conn,db=Path(manifest['authority']['path']),now=now,native_plan=plan['native_plan'],
                         original=manifest['original_capture'] if recovering else None)
        stages.require(capture==manifest['original_capture'],'historical_fbo_original_capture_changed')
        state_revision,stage_capture,candidate=_build(conn,book=book,native_plan=plan['native_plan'],
            accounting_capture=capture,fee_scope=plan['fee_scope'])
        stages.require(fingerprint(state_revision)==plan['state_revision_digest']
            and stage_capture==manifest['original_stage_capture'] and candidate==manifest['candidate'],
            'historical_fbo_numeric_or_stage_rebuild_mismatch')
        publication.verify_ready_targets(conn,manifest=manifest,candidate=candidate,runtime_dir=runtime_dir,now=now)


def publish_stages(conn, *, candidate):
    return _stage_adapter().publish_historical_fbo_stages(conn,manifest=candidate)


EFFECT_CONTRACT='fbs_historical_fbo_effect_intent_v1'


def _fee_requests(native_plan, requests):
    from decimal import Decimal
    from packages.application.operator_fulfillment_services import request_identity
    supplies={e['source_key'].rsplit(':',1)[1] for e in native_plan['effects'] if e['kind']=='historical_wb_dispatch'}
    stages.require(type(requests) is list and len(requests)==3,'historical_fbo_three_fee_requests_required')
    identities=set();seen=set();total=Decimal(0)
    fields={'request_scope','request_id','file_sha256','wb_supply_id','amount_with_vat_rub'}
    for item in requests:
        stages.require(set(item)==fields,'historical_fbo_fee_request_fields_invalid')
        identity,scope=request_identity(item['request_id'],item['request_scope'])
        stages.require(identity==item['request_id'] and scope==item['request_scope'] and identity and scope and (scope,identity) not in identities,'historical_fbo_fee_request_identity_invalid')
        identities.add((scope,identity));seen.add(item['wb_supply_id'])
        sha=item['file_sha256']
        stages.require(type(sha) is str and len(sha)==64 and all(c in '0123456789abcdef' for c in sha),
                       'historical_fbo_fee_file_hash_invalid')
        value=Decimal(item['amount_with_vat_rub'])
        stages.require(type(item['amount_with_vat_rub']) is str and value.is_finite() and value>0
                       and value==value.quantize(Decimal('.01')),'historical_fbo_fee_amount_invalid')
        total+=value
    stages.require(seen==supplies and len(seen)==3 and total==Decimal('234122.05'),
                   'historical_fbo_fee_scope_or_total_invalid')
    return deepcopy(requests)


def prepare_effect_intent(native_plan, *, capture, scope, expected_native_guard, fee_requests,
                          runtime_dir, owner_generation, operation_id, ready_target, attempt_id='1',now=None,actor='Кодекс'):
    """Freeze an executable source recipe in the existing real Ready intent log.

    This performs no writes. Random upload IDs are deliberately absent: the
    native fulfillment request identities own readback after uncertain upload.
    """
    from packages.application.fbs_historical_fbo_dispatch import validate_historical_fbo_dispatch_plan
    from packages.application.fbs_historical_fbo_native import capture_native_guard
    validate_historical_fbo_dispatch_plan(native_plan,capture=capture,scope=scope)
    now=now or datetime.now(timezone.utc)
    for identity in (owner_generation,operation_id,attempt_id):
        stages.require(type(identity) is str and identity.strip()==identity and 0<len(identity)<=200,
                       'historical_fbo_effect_identity_invalid')
    stages.require(type(actor) is str and actor.strip()==actor and 0<len(actor)<=160,'historical_fbo_effect_actor_invalid')
    fees=_fee_requests(native_plan,fee_requests)
    authority=ready.capture_authority(runtime_dir);db=Path(authority['path'])
    book,version=accounting.load(runtime_dir);_healthy(book)
    stages.require(version==native_plan['source_proof']['book_version'],'historical_fbo_effect_book_changed')
    with ready.readonly(db) as conn:
        expected=ready.capture_expected(conn,authority=authority,**ready_target)
        stages.require(expected.exists,'historical_fbo_effect_real_ready_target_required')
        stages.require(capture_native_guard(conn,native_plan)==expected_native_guard,'historical_fbo_effect_native_guard_changed')
        stages.require(not _verify_existing(conn,native_plan),'historical_fbo_effect_intent_must_precede_native')
        _assert_fee_effects_not_started(conn,fees)
    from dataclasses import asdict
    recipe=dict(contract=EFFECT_CONTRACT,operation_id=operation_id,attempt_id=attempt_id,owner_generation=owner_generation,
        authority=authority,file_authority=staging._file_authority(runtime_dir,db),expected=asdict(expected),
        expected_book=version,native_plan=deepcopy(native_plan),native_capture=deepcopy(capture),native_scope=deepcopy(scope),
        native_guard=deepcopy(expected_native_guard),fee_requests=fees,created_at=now.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
        publication_operation_id=operation_id+':publication',publication_attempt_id=attempt_id,
        native_actor=actor,native_posted_at=now.astimezone(timezone.utc).isoformat().replace('+00:00','Z'))
    recipe['recipe_digest']=fingerprint(recipe)
    publication._json_size(recipe,publication.MAX_BYTES,'historical_fbo_effect_recipe_size_limit')
    return recipe


def _verify_recipe(recipe):
    from packages.application.fbs_historical_fbo_dispatch import validate_historical_fbo_dispatch_plan
    stages.require(recipe['contract']==EFFECT_CONTRACT and recipe['recipe_digest']==fingerprint({k:v for k,v in recipe.items() if k!='recipe_digest'}),
                   'historical_fbo_effect_recipe_corrupt')
    validate_historical_fbo_dispatch_plan(recipe['native_plan'],capture=recipe['native_capture'],scope=recipe['native_scope'])
    _fee_requests(recipe['native_plan'],recipe['fee_requests'])


def read_effect_intent(conn, *, binding):
    import json
    row=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                     (binding['operation_id'],binding['attempt_id'])).fetchone()
    stages.require(row and row['kind']==EFFECT_CONTRACT and row['state'] in {'prepared','complete'},
                   'historical_fbo_effect_intent_not_pending')
    recipe=json.loads(row['inputs_json']);_verify_recipe(recipe)
    if row['state']=='complete':
        proof=json.loads(row['diagnostics_json']).get('historical_fbo_completion',{})
        stages.require(proof.get('child_operation_id')==recipe['publication_operation_id']
            and proof.get('child_attempt_id')==recipe['publication_attempt_id']
            and proof.get('book_version')==row['book_version'] and proof.get('history_ack') and proof.get('finance'),
            'historical_fbo_effect_completion_proof_missing')
    expected=ready.ExpectedReady(**recipe['expected'])
    stages.require(recipe['recipe_digest']==binding['recipe_digest'] and row['expected_book']==recipe['expected_book']
        and recipe['operation_id']==binding['operation_id'] and recipe['attempt_id']==binding['attempt_id']
        and row['bundle_version']==expected.bundle_version and row['as_of_date']==expected.as_of_date
        and row['expected_digest']==expected.fingerprint,
        'historical_fbo_effect_intent_identity_conflict')
    ready.check_pinned_authority(conn,recipe['authority'])
    return recipe


def record_effect_intent(recipe, *, runtime, recapture, owner_token):
    """Persist before effects; same identity reads its recipe without resubmission."""
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner,warehouse_functional_write_lock
    from packages.application.fbs_historical_fbo_native import capture_native_guard
    from packages.application.fbs_historical_fbo_dispatch import validate_historical_fbo_dispatch_plan
    from packages.application import fbs_accounting_historical_sqlite as owned_sqlite
    _verify_recipe(recipe);require_warehouse_job_owner(runtime.runtime_dir,owner_token)
    binding={k:recipe[k] for k in ('operation_id','attempt_id','recipe_digest')}
    with warehouse_functional_write_lock(runtime.runtime_dir),accounting.writer_lock(runtime.runtime_dir):
        with owned_sqlite.connection(runtime.db_path,phase_name='fbo_effect_intent_connection') as conn:
            import sqlite3
            conn.row_factory=sqlite3.Row
            # Bootstrap is additive and deliberately outside the owned write txn.
            ready.ensure_publication_schema(conn);conn.commit()
            with owned_sqlite.transaction(conn,db_path=runtime.db_path,phase_name='fbo_effect_intent'):
                existing=conn.execute('SELECT state FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                    (recipe['operation_id'],recipe['attempt_id'])).fetchone()
                if existing:
                    stages.require(read_effect_intent(conn,binding=binding)==recipe,'historical_fbo_effect_intent_identity_conflict')
                    return dict(contract=EFFECT_CONTRACT,status='scope_complete' if existing['state']=='complete' else 'pending',binding=binding,idempotent=True,monetary_effects_status='not_checked')
                ready.check_pinned_authority(conn,recipe['authority'])
                stages.require(staging._file_authority(runtime.runtime_dir,runtime.db_path)==recipe['file_authority'],
                               'historical_fbo_effect_file_changed')
                book,version=accounting.load(runtime.runtime_dir);_healthy(book)
                stages.require(version==recipe['expected_book'],'historical_fbo_effect_book_changed')
                ready.check_expected(conn,ready.ExpectedReady(**recipe['expected']))
                stages.require(capture_native_guard(conn,recipe['native_plan'])==recipe['native_guard'],'historical_fbo_effect_native_guard_changed')
                validate_historical_fbo_dispatch_plan(recipe['native_plan'],capture=recapture(conn),scope=recipe['native_scope'])
                stages.require(not _verify_existing(conn,recipe['native_plan']),'historical_fbo_effect_intent_must_precede_native')
                _assert_fee_effects_not_started(conn,recipe['fee_requests'])
                ready.record_intent(conn,operation_id=recipe['operation_id'],attempt_id=recipe['attempt_id'],kind=EFFECT_CONTRACT,
                    expected=ready.ExpectedReady(**recipe['expected']),inputs=recipe,expected_book=version,book_required=True,
                    ready_required=True,created_at=recipe['created_at'],book_operation_id='historical-fbo-effects:'+recipe['recipe_digest'][7:])
    return dict(contract=EFFECT_CONTRACT,status='pending',binding=binding,idempotent=False,monetary_effects_started=False)


def capture_fee_scope(conn, *, native_plan, fee_requests):
    """Resolve exact accepted requests to native uploaded sources, never resend."""
    import json
    from decimal import Decimal
    from packages.application import operator_fulfillment_services as receipts
    from packages.application import fulfillment_recalc_intents as intents
    requests=_fee_requests(native_plan,fee_requests);uploads=[];queries=[]
    for request in requests:
        args=(request['request_scope'],request['request_id'])
        sql=f'SELECT * FROM {receipts.REQUESTS} WHERE request_scope=? AND request_id=?'
        bound=conn.execute(sql,args).fetchone();queries.append((sql,args))
        stages.require(bound and bound['action']=='upload' and bound['payload_digest']==request['file_sha256']
                       and bound['operation_id'],'historical_fbo_fee_request_not_accepted')
        upload_id=bound['upload_id'];operation_id=bound['operation_id']
        upload=conn.execute(f'SELECT * FROM {intents.UPLOADS} WHERE upload_id=?',(upload_id,)).fetchone()
        operation=conn.execute(f'SELECT * FROM {receipts.TABLE} WHERE operation_id=?',(operation_id,)).fetchone()
        lines=list(conn.execute(f'SELECT * FROM {intents.LINES} WHERE upload_id=? ORDER BY row_index,id',(upload_id,)))
        value=Decimal(request['amount_with_vat_rub'])
        stages.require(upload and upload['validation_status']=='ok' and not upload['deleted_at']
            and upload['file_sha256']==request['file_sha256'] and Decimal(str(upload['amount_with_vat_total']))==value,
            'historical_fbo_fee_upload_changed')
        stages.require(lines and all(line['match_status']=='ok' and not line['is_storage_line']
            and str(line['matched_wb_supply_id'])==request['wb_supply_id']
            and str(line['matched_wb_cache_key'])=='supply:'+request['wb_supply_id'] for line in lines)
            and sum((Decimal(str(line['amount_with_vat'])) for line in lines),Decimal(0))==value,
            'historical_fbo_fee_exact_supply_lines_required')
        source={**receipts._source(conn,upload_id),'action':'upload','source_revision':intents.source_revision(upload)}
        stages.require(operation and operation['upload_id']==upload_id and operation['action']=='upload'
            and operation['source_revision']==request['file_sha256'] and json.loads(operation['source_json'])==source
            and operation['source_digest']==receipts.digest(source),'historical_fbo_fee_source_receipt_changed')
        demand=conn.execute(f'SELECT * FROM {intents.TABLE} WHERE upload_id=? AND source_revision=?',(upload_id,request['file_sha256'])).fetchone()
        stages.require(demand is not None,'historical_fbo_fee_durable_recalc_intent_required')
        targets=json.loads(demand['scope_json'])
        stages.require(targets and all(str(row.get('supply_id',''))==request['wb_supply_id'] for row in targets.values()),
                       'historical_fbo_fee_recalc_scope_changed')
        uploads.append({**request,'upload_id':upload_id,'operator_operation_id':operation_id,
                        'source_revision':operation['source_revision'],'source_digest':operation['source_digest']})
        # Mutable delivery/queue state is not source authority. Pin its immutable
        # upload/revision/supply scope; ordinary delivery may continue afterward.
        queries.extend([(f'SELECT * FROM {intents.UPLOADS} WHERE upload_id=?',(upload_id,)),
            (f'SELECT * FROM {intents.LINES} WHERE upload_id=? ORDER BY row_index,id',(upload_id,)),
            (f'SELECT * FROM {receipts.TABLE} WHERE operation_id=?',(operation_id,)),
            (f'SELECT upload_id,source_revision,scope_json FROM {intents.TABLE} WHERE upload_id=? AND source_revision=?',(upload_id,request['file_sha256']))])
    return dict(contract='historical_fbo_exact_fulfillment_fee_scope_v1',requests=requests,uploads=uploads,
                total_with_vat_rub='234122.05',query_fence=json.loads(canonical(ready.pin_queries(conn,queries))))


def _bound_effect_recipe(conn, *, binding, native_plan, expected_book, fee_scope):
    recipe=read_effect_intent(conn,binding=binding)
    stages.require(recipe['expected_book']==expected_book and recipe['native_plan']==native_plan,
                   'historical_fbo_publication_effect_recipe_changed')
    actual=capture_fee_scope(conn,native_plan=native_plan,fee_requests=recipe['fee_requests'])
    stages.require(actual==fee_scope,'historical_fbo_publication_fee_scope_changed')
    return recipe


def _assert_fee_effects_not_started(conn,requests):
    from packages.application import operator_fulfillment_services as receipts
    from packages.application import fulfillment_recalc_intents as intents
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for item in requests:
        if receipts.REQUESTS in tables:
            row=conn.execute(f'SELECT 1 FROM {receipts.REQUESTS} WHERE request_scope=? AND request_id=?',
                             (item['request_scope'],item['request_id'])).fetchone()
            stages.require(row is None,'historical_fbo_effect_intent_must_precede_fee_request')
        if intents.UPLOADS in tables:
            row=conn.execute(f"SELECT 1 FROM {intents.UPLOADS} WHERE file_sha256=? AND validation_status='ok' AND deleted_at IS NULL",
                             (item['file_sha256'],)).fetchone()
            stages.require(row is None,'historical_fbo_effect_intent_must_precede_fee_source')


def validate_source_receipt(conn, *, manifest):
    plan=manifest['plan']
    _bound_effect_recipe(conn,binding=plan['effect_binding'],native_plan=plan['native_plan'],
                         expected_book=manifest['expected_book'],fee_scope=plan['fee_scope'])
    stages.require(_verify_existing(conn,plan['native_plan']),'historical_fbo_published_native_source_changed')


def finalize_scope(runtime, *, manifest, history_ack, finance, economics, now):
    """Finalize this scope after the existing cycle's real History/Finance proof.

    No warehouse receipt is fabricated and no fulfillment receipt is marked
    complete here. Its ordinary native proof owner retains that responsibility.
    """
    from packages.application import fbs_accounting_historical_sqlite as owned_sqlite
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner,warehouse_functional_write_lock
    from packages.application.business_data_heavy_admission import require_heavy_owner
    require_warehouse_job_owner(runtime.runtime_dir)
    stages.require(require_heavy_owner(runtime.runtime_dir).operation=='cycle','historical_fbo_completion_cycle_owner_required')
    publication._verify(manifest)
    book,version=accounting.load(runtime.runtime_dir);_healthy(book)
    stages.require(version==manifest['after_book'] and book==manifest['candidate']['candidate_book'],
                   'historical_fbo_completion_book_changed')
    quality=manifest['candidate'].get('quality_proof')
    stages.require(isinstance(quality,dict) and quality
        and book.get('historical_revision',{}).get('quality_proof')==quality,
        'historical_fbo_completion_quality_proof_required')
    stages.require(economics and economics==accounting.current_publication_receipt(runtime,now=now),
                   'historical_fbo_completion_economics_unproven')
    stages.require(history_ack and history_ack['manifest_digest']==manifest['manifest_digest']
        and finance.get('status') in {'applied','already_current'} and finance.get('non_target_preserved') is True
        and finance.get('post_verify_stale_week_count')==0 and finance.get('accounting_version_unchanged') is True
        and finance.get('source_advanced_after_apply') is not True,'historical_fbo_completion_native_proof_required')
    # The cycle has independently checked the live History supervisor and the
    # current economics publication. Retain their actual receipts verbatim.
    binding=manifest['plan']['effect_binding']
    with warehouse_functional_write_lock(runtime.runtime_dir,timeout_seconds=2):
        with owned_sqlite.connection(runtime.db_path,phase_name='fbo_completion_connection') as conn, owned_sqlite.transaction(conn,db_path=runtime.db_path,phase_name='fbo_completion'):
            import sqlite3
            conn.row_factory=sqlite3.Row
            validate_source_receipt(conn,manifest=manifest)
            row=conn.execute('SELECT kind,inputs_json,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=? AND state=\'complete\'',
                             (manifest['operation_id'],manifest['attempt_id'])).fetchone()
            stages.require(row and row['kind']==CONTRACT
                           and publication.decode_manifest(row['inputs_json'],kind=row['kind'])==manifest,
                           'historical_fbo_completion_publication_changed')
            diagnostics=json.loads(row['diagnostics_json'])
            stages.require(diagnostics.get('historical_history_ack')==history_ack,'historical_fbo_completion_ack_changed')
            proof=dict(child_operation_id=manifest['operation_id'],child_attempt_id=manifest['attempt_id'],
                manifest_digest=manifest['manifest_digest'],book_version=manifest['after_book'],history_ack=history_ack,
                finance=finance,economics=economics,quality_proof=deepcopy(manifest['candidate'].get('quality_proof',{})),
                operator_receipts_completed=False)
            diagnostics.update(historical_fbo_scope_complete=True,historical_finance=finance,historical_fbo_completion=proof)
            stages.require(conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=? AND diagnostics_json=?',
                (canonical(diagnostics),manifest['operation_id'],manifest['attempt_id'],row['diagnostics_json'])).rowcount==1,
                'historical_fbo_completion_cas_failed')
            parent=conn.execute('SELECT state,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                                (binding['operation_id'],binding['attempt_id'])).fetchone()
            if parent['state']=='prepared':
                ready.complete_publication(conn,operation_id=binding['operation_id'],attempt_id=binding['attempt_id'],
                    book_version=manifest['after_book'],after_digest=ready.digest(manifest['targets'][-1]['after_json']),
                    finished_at=now.astimezone(timezone.utc).isoformat().replace('+00:00','Z'))
                conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=?',
                    (canonical({'historical_fbo_completion':proof}),binding['operation_id'],binding['attempt_id']))
            else:
                stages.require(json.loads(parent['diagnostics_json']).get('historical_fbo_completion')==proof,
                               'historical_fbo_parent_completion_changed')
    return dict(status='scope_complete',operation_id=manifest['operation_id'],manifest_digest=manifest['manifest_digest'],
                native_and_fee_sources_complete=True,history_complete=True,finance_complete=True,
                operator_receipts_completed=False,quality_proof=proof['quality_proof'])


def apply_native_from_effect_intent(binding, *, runtime, recapture, owner_token, fault=None):
    """The orchestration seam: the exact recipe is required inside native CAS."""
    from packages.application.fbs_historical_fbo_native import apply_historical_fbo_native
    with ready.readonly(runtime.db_path) as conn:
        recipe=read_effect_intent(conn,binding=binding)
    def admit(conn):
        stages.require(read_effect_intent(conn,binding=binding)==recipe,'historical_fbo_native_effect_recipe_changed')
    return apply_historical_fbo_native(runtime,plan=recipe['native_plan'],capture=recipe['native_capture'],
        scope=recipe['native_scope'],expected_native_guard=recipe['native_guard'],recapture=recapture,
        owner_token=owner_token,posted_at=recipe['native_posted_at'],actor=recipe['native_actor'],fault=fault,
        pre_write_guard=admit)


def _fee_attempt(runtime, *, binding, request, state, reason=''):
    from packages.application import fbs_accounting_historical_sqlite as owned_sqlite
    from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
    import sqlite3
    with warehouse_functional_write_lock(runtime.runtime_dir):
        with owned_sqlite.connection(runtime.db_path,phase_name='fbo_fee_attempt_connection') as conn, owned_sqlite.transaction(conn,db_path=runtime.db_path,phase_name='fbo_fee_attempt'):
            conn.row_factory=sqlite3.Row
            read_effect_intent(conn,binding=binding)
            row=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                             (binding['operation_id'],binding['attempt_id'])).fetchone()
            diagnostics=json.loads(row['diagnostics_json']);attempts=diagnostics.setdefault('historical_fbo_fee_attempts',{})
            key=fingerprint([request['request_scope'],request['request_id']])
            previous=attempts.get(key)
            if previous and previous['state']=='accepted':
                stages.require(state=='accepted','historical_fbo_fee_accepted_attempt_cannot_regress')
            attempts[key]=dict(request_scope=request['request_scope'],request_id=request['request_id'],
                file_sha256=request['file_sha256'],state=state,reason=reason[:300])
            stages.require(conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=? AND diagnostics_json=?',
                (canonical(diagnostics),binding['operation_id'],binding['attempt_id'],row['diagnostics_json'])).rowcount==1,
                'historical_fbo_fee_attempt_cas_failed')


def apply_fees_from_effect_intent(binding, *, runtime, workbooks, owner_token):
    """One native upload attempt per frozen request; uncertain state only reads.

    `workbooks` maps (request_scope, request_id) to reviewed XLSX bytes. No file
    or amount is inferred. A durable ``submitting`` marker precedes each call;
    a crash or absent acknowledgment therefore cannot trigger another upload.
    """
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner
    from packages.application import operator_fulfillment_services as receipts
    from packages.application.fulfillment_services import FulfillmentServicesBlock
    require_warehouse_job_owner(runtime.runtime_dir,owner_token)
    with ready.readonly(runtime.db_path) as conn:
        recipe=read_effect_intent(conn,binding=binding)
        stages.require(_verify_existing(conn,recipe['native_plan']),'historical_fbo_fee_native_effects_required')
    # This constructor performs no database or source writes. Its ordinary
    # upload owner retains source+receipt+recalc-intent transaction semantics.
    service=FulfillmentServicesBlock(runtime=runtime)
    readbacks=[]
    for request in recipe['fee_requests']:
        with ready.readonly(runtime.db_path) as conn:
            stages.require(read_effect_intent(conn,binding=binding)==recipe,'historical_fbo_fee_effect_recipe_changed')
            row=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                             (binding['operation_id'],binding['attempt_id'])).fetchone()
            attempts=json.loads(row['diagnostics_json']).get('historical_fbo_fee_attempts',{})
            previous=attempts.get(fingerprint([request['request_scope'],request['request_id']]))
        saved=receipts.read_request(runtime.db_path,request['request_id'],request_scope=request['request_scope'])
        if saved['status']=='accepted':
            stages.require(saved['payload_digest']==request['file_sha256'] and saved['action']=='upload',
                           'historical_fbo_fee_saved_request_conflict')
            _fee_attempt(runtime,binding=binding,request=request,state='accepted')
            readbacks.append(saved);continue
        stages.require(saved['status']=='not_found' and previous is None,
                       'historical_fbo_fee_uncertain_no_resend')
        data=workbooks[(request['request_scope'],request['request_id'])]
        stages.require(type(data) is bytes and hashlib.sha256(data).hexdigest()==request['file_sha256'],
                       'historical_fbo_fee_reviewed_workbook_changed')
        book,version=accounting.load(runtime.runtime_dir);_healthy(book)
        stages.require(version==recipe['expected_book'],'historical_fbo_fee_book_changed_before_submit')
        _fee_attempt(runtime,binding=binding,request=request,state='submitting')
        error=None
        try:
            service.upload_xlsx(data,uploaded_filename=request['wb_supply_id']+'-fulfillment.xlsx',
                request_id=request['request_id'],request_scope=request['request_scope'],actor=recipe['native_actor'])
        except Exception as exc:
            error=exc
        # Always read the native request, including on a lost response. There is
        # no second submit in this call or a later continuation of this identity.
        saved=receipts.read_request(runtime.db_path,request['request_id'],request_scope=request['request_scope'])
        if saved['status']!='accepted':
            _fee_attempt(runtime,binding=binding,request=request,state='needs_review',reason=str(error or saved['status']))
            raise ValueError('historical_fbo_fee_uncertain_no_resend') from error
        stages.require(saved['payload_digest']==request['file_sha256'] and saved['action']=='upload',
                       'historical_fbo_fee_saved_request_conflict')
        _fee_attempt(runtime,binding=binding,request=request,state='accepted');readbacks.append(saved)
    with ready.readonly(runtime.db_path) as conn:
        scope=capture_fee_scope(conn,native_plan=recipe['native_plan'],fee_requests=recipe['fee_requests'])
    return dict(status='sources_accepted_pending_publication',effect_binding=binding,fee_scope=scope,readbacks=readbacks,
                publication_complete=False)


COST_LAYER_CONTRACT='historical_fbo_frozen_native_cost_layers_v1'


def prepare_cost_layer_materialization(binding, *, runtime, now, ordinary_refresh=False, target_supply_ids=None):
    """Prepare selected compatibility layers using frozen native FF money only."""
    from decimal import Decimal
    from packages.application import our_wb_costs as costs
    from packages.application.fulfillment_services import FulfillmentServicesBlock
    from packages.application import fulfillment_recalc_intents as intents
    block=costs.OurWbCostBlock(runtime=runtime,timestamp_factory=lambda:now)
    with ready.readonly(runtime.db_path) as conn:
        recipe=read_effect_intent(conn,binding=binding)
        stages.require(_verify_existing(conn,recipe['native_plan']),'historical_fbo_cost_native_required')
        book,version=accounting.load(runtime.runtime_dir);_healthy(book)
        if not ordinary_refresh:
            stages.require(version==recipe['expected_book'],'historical_fbo_cost_book_changed')
        effects={e['source_key'].rsplit(':',1)[1]:e for e in recipe['native_plan']['effects'] if e['kind']=='historical_wb_dispatch'}
        if target_supply_ids is not None:
            stages.require(set(target_supply_ids)<=set(effects) and target_supply_ids,'historical_fbo_cost_selected_supplies_changed')
            effects={sid:effects[sid] for sid in sorted(target_supply_ids)}
        fee_scope=(capture_ordinary_fee_scope(conn,supply_ids=sorted(effects)) if ordinary_refresh else
            capture_fee_scope(conn,native_plan=recipe['native_plan'],fee_requests=recipe['fee_requests']))
        overlays=FulfillmentServicesBlock.approved_overlay_in_connection(conn)
        payloads=[];queries=[];native_ids=tuple(operation_id(recipe['native_plan'],i) for i in range(4))
        native_marks=','.join('?' for _ in native_ids)
        for table,order in (('warehouse_business_operations','operation_id'),('ff_pool_movement_lines','operation_id,line_no'),('ff_stock_operations','operation_id'),('ff_stock_operation_lines','operation_id,line_no')):
            queries.append((f'SELECT * FROM sheet_vitrina_v1_{table} WHERE operation_id IN ({native_marks}) ORDER BY {order}',native_ids))
        for supply_id,effect in effects.items():
            row=intents.load_supply_in_connection(conn,supply_id)
            stages.require(row and str(row['supply_id'])==supply_id and str(row['wb_supply_id'])==supply_id
                           and row['cache_key']=='supply:'+supply_id,'historical_fbo_cost_supply_alias_changed')
            supply=costs._wb_supply_row_to_dict(row)
            goods=costs._parse_wb_goods(supply.get('raw_goods_json'))
            native={l['nm_id']:l for l in effect['aggregate_movements']}
            observed={}
            for good in goods:
                nm=costs._optional_int(good.get('nmID') or good.get('nmId') or good.get('nm_id'))
                q=costs._wb_good_packed_quantity(good).qty
                stages.require(nm in native and nm not in observed and q==-native[nm]['quantity_delta'],
                               'historical_fbo_cost_packed_source_changed')
                observed[nm]=good
            stages.require(set(observed)==set(native),'historical_fbo_cost_native_sku_missing')
            denominator=sum(-l['quantity_delta'] for l in native.values())
            overlay=overlays.get(supply_id)
            stages.require(ordinary_refresh or overlay is not None,'historical_fbo_cost_fee_overlay_missing')
            overlay=overlay or {}
            enrichment=conn.execute('SELECT * FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment WHERE supply_id=?',(supply_id,)).fetchone()
            if enrichment and costs._canonical_seller_portal_transit_enrichment(dict(enrichment)):
                supply.update(seller_portal_transit_cost=enrichment['amount'],effective_transit_cost_total=enrichment['amount'],
                    effective_transit_cost_source=enrichment['source']+':'+enrichment['evidence_type'])
            transit=costs.classify_wb_supply_transit(costs._normalized_wb_row(supply),denominator=denominator)
            acceptance=costs.wb_supply_acceptance_cost(costs._normalized_wb_row(supply))
            accepted_denominator=costs._sum_positive(costs._number_or_zero(g.get('acceptedQuantity') if g.get('acceptedQuantity') is not None else g.get('accepted_quantity')) for g in goods)
            acceptance_per=None if acceptance is None else acceptance/accepted_denominator if accepted_denominator>0 else 0.0
            for nm,good in observed.items():
                frozen=native[nm]
                # No supplier-current reader is invoked, including for missing
                # native money. The exact Decimal stays in native provenance;
                # the existing REAL compatibility columns retain their policy.
                unit=Decimal(frozen['wac_rub'])
                stages.require(unit.is_finite() and unit>0,'historical_fbo_cost_frozen_ff_missing')
                reference=dict(contract=COST_LAYER_CONTRACT,plan_digest=recipe['native_plan']['plan_digest'],
                    operation_id=operation_id(recipe['native_plan'],recipe['native_plan']['effects'].index(effect)),
                    source_book=recipe['expected_book'],business_date=effect['business_date'],nm_id=nm,
                    unit_cost_rub=frozen['wac_rub'],quantity_delta=frozen['quantity_delta'],capital_delta_rub=frozen['capital_delta_rub'])
                accepted=costs._wb_good_accepted_quantity(good)
                payload=block._build_wb_supply_cost_layer_payload(supply=supply,nm_id=nm,accepted_qty=accepted.qty,
                    quantity_source=accepted.source,quantity_is_final_accepted=costs._wb_good_quantity_is_final_accepted(supply=supply,quantity=accepted),
                    denominator=denominator,ff_line=dict(sku_ff_unit_cost_rub=frozen['wac_rub'],layer_status='historical_frozen_native'),
                    transit=transit,services_total=costs._number_or_zero(overlay.get('service_amount_with_vat_without_storage_total')),
                    services_per_unit=costs._number_or_zero(overlay.get('service_amount_with_vat_without_storage_total'))/denominator,
                    storage_total=costs._number_or_zero(overlay.get('storage_allocated_amount_with_vat_total')),
                    storage_per_unit=costs._number_or_zero(overlay.get('storage_allocated_amount_with_vat_total'))/denominator,
                    acceptance_total=acceptance,acceptance_per_accepted_unit=acceptance_per,overlay=overlay)
                payload['component_status']['historical_fbo_native_source']=reference
                payload['component_status']['supplier_ff_cost']='exact_historical_native_debit'
                payload['input']['historical_fbo_native_source']=reference
                payload['input']['payload']['component_status']=deepcopy(payload['component_status'])
                payloads.append(dict(payload=payload,inputs_hash=costs._stable_hash(payload['input'])))
            queries.extend([('SELECT * FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=?',(supply_id,)),
                ('SELECT * FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment WHERE supply_id=?',(supply_id,)),
                ('SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE wb_supply_id=? ORDER BY nm_id,version',(supply_id,)),
                (f'SELECT u.*,l.* FROM {intents.UPLOADS} u JOIN {intents.LINES} l USING(upload_id) WHERE l.matched_wb_supply_id=? OR l.matched_wb_cache_key=? ORDER BY u.upload_id,l.row_index,l.id',(supply_id,'supply:'+supply_id))])
        source_fence=json.loads(canonical(ready.pin_queries(conn,queries)))
        result=dict(contract=COST_LAYER_CONTRACT,effect_binding=deepcopy(binding),expected_book=version,
            fee_scope=fee_scope,source_fence=source_fence,payloads=payloads,supply_ids=sorted(effects),created_at=now,ordinary_refresh=ordinary_refresh)
        result['manifest_digest']=fingerprint(result)
        return result


def materialize_cost_layers(cost_block, *, binding, expected=None, supply_ids=None, ordinary_refresh=False):
    """Existing layer owner, bounded frozen source/CAS; no daily publication."""
    from packages.application import our_wb_costs as costs
    from packages.application import fbs_accounting_historical_sqlite as owned_sqlite
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner,warehouse_functional_write_lock
    import sqlite3
    runtime=cost_block.runtime;require_warehouse_job_owner(runtime.runtime_dir)
    manifest=expected or prepare_cost_layer_materialization(binding,runtime=runtime,now=cost_block.timestamp_factory(),
        ordinary_refresh=ordinary_refresh,target_supply_ids=supply_ids if ordinary_refresh else None)
    stages.require(manifest['contract']==COST_LAYER_CONTRACT and manifest['effect_binding']==binding
        and manifest['manifest_digest']==fingerprint({k:v for k,v in manifest.items() if k!='manifest_digest'}),
        'historical_fbo_cost_manifest_corrupt')
    stages.require(not supply_ids or set(map(str,supply_ids))==set(manifest['supply_ids']),
                   'historical_fbo_cost_selected_supplies_changed')
    rebuilt=prepare_cost_layer_materialization(binding,runtime=runtime,now=manifest['created_at'],
        ordinary_refresh=manifest['ordinary_refresh'],target_supply_ids=manifest['supply_ids'])
    if manifest!=rebuilt:
        # A lost response may leave precisely these layers already committed.
        # Rebuild every payload and recheck every other source, then read back
        # complete native row fields instead of treating an old fence as retry.
        stable=lambda value:{k:v for k,v in value.items() if k not in {'source_fence','manifest_digest'}}
        nonlayers=lambda value:[entry for entry in value['source_fence'] if not entry[0].startswith('SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers ')]
        stages.require(stable(manifest)==stable(rebuilt) and nonlayers(manifest)==nonlayers(rebuilt),
                       'historical_fbo_cost_numeric_rebuild_changed')
        with ready.readonly(runtime.db_path) as conn:
            complete=True
            for item in manifest['payloads']:
                payload=item['payload']
                row=conn.execute('SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE wb_supply_id=? AND nm_id=? AND is_current=1',
                    (payload['wb_supply_id'],payload['nm_id'])).fetchone()
                complete=complete and bool(row and row['inputs_hash']==item['inputs_hash']
                    and all(row[key]==value for key,value in payload.items() if key not in {'input','component_status'})
                    and json.loads(row['component_status_json'])==payload['component_status'])
            stages.require(complete,'historical_fbo_cost_partial_or_conflicting_retry')
        return 0
    stages.require(manifest==rebuilt,'historical_fbo_cost_numeric_rebuild_changed')
    with warehouse_functional_write_lock(runtime.runtime_dir),accounting.writer_lock(runtime.runtime_dir):
        with owned_sqlite.connection(runtime.db_path,phase_name='fbo_cost_layers_connection') as conn, owned_sqlite.transaction(conn,db_path=runtime.db_path,phase_name='fbo_cost_layers'):
            conn.row_factory=sqlite3.Row
            ready.check_queries(conn,[(sql,tuple(params),digest) for sql,params,digest in manifest['source_fence']])
            recipe=read_effect_intent(conn,binding=binding)
            stages.require(_verify_existing(conn,recipe['native_plan']),'historical_fbo_cost_native_required')
            actual_fees=(capture_ordinary_fee_scope(conn,supply_ids=manifest['supply_ids']) if manifest['ordinary_refresh'] else
                capture_fee_scope(conn,native_plan=recipe['native_plan'],fee_requests=recipe['fee_requests']))
            stages.require(actual_fees==manifest['fee_scope'],'historical_fbo_cost_fee_source_changed')
            book,version=accounting.load(runtime.runtime_dir);_healthy(book)
            stages.require(version==manifest['expected_book'],'historical_fbo_cost_book_changed')
            count=0
            for item in manifest['payloads']:
                count+=bool(cost_block._upsert_wb_supply_cost_layer(conn=conn,now=manifest['created_at'],
                    payload=item['payload'],inputs_hash=item['inputs_hash']))
            # Prove only this selected contour, retaining unrelated queued
            # fulfillment demands for their ordinary owner.
            for item in manifest['payloads']:
                payload=item['payload']
                row=conn.execute('SELECT inputs_hash,ff_upload_id,ff_services_amount_total,ff_storage_amount_total FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE wb_supply_id=? AND nm_id=? AND is_current=1',
                                 (payload['wb_supply_id'],payload['nm_id'])).fetchone()
                stages.require(row and row['inputs_hash']==item['inputs_hash']
                    and row['ff_upload_id']==payload['ff_upload_id']
                    and row['ff_services_amount_total']==payload['ff_services_amount_total']
                    and row['ff_storage_amount_total']==payload['ff_storage_amount_total'],
                    'historical_fbo_cost_materialization_readback_changed')
    return count


def verify_stage_readback(conn, *, manifest):
    """Read native acceptance events and daily cost rows owned by this child."""
    candidate=manifest['candidate'];first=candidate['editions'][min(candidate['editions'])]
    for event in candidate['new_acceptance_events']:
        expected={k:v for k,v in event.items() if k!='provenance'}
        expected.update(version_id=first['version_id'],created_at=first['version']['created_at'],provenance_json=canonical(event['provenance']))
        actual=conn.execute(f'SELECT * FROM {stages.P}functional_events WHERE event_id=?',(event['event_id'],)).fetchone()
        stages.require(actual and dict(actual)==expected,'historical_fbo_native_acceptance_readback_changed')
    for item in candidate['daily_wb_costs']:
        expected=dict(cutover_id=_stage_adapter().wf.FUNCTIONAL_CUTOVER_ID,as_of_date=item['as_of_date'],nm_id=item['nm_id'],
            quantity=item['quantity'],wac_rub=item['wac_rub'],capital_rub=item['capital_rub'],quality=item['quality'],
            provenance_json=canonical(item['provenance']),fingerprint=item['fingerprint'],created_at=first['version']['created_at'])
        actual=conn.execute(f'SELECT * FROM {stages.P}wb_daily_cost WHERE cutover_id=? AND as_of_date=? AND nm_id=?',
                           (expected['cutover_id'],expected['as_of_date'],expected['nm_id'])).fetchone()
        stages.require(actual and dict(actual)==expected,'historical_fbo_native_daily_cost_readback_changed')


def capture_ordinary_fee_scope(conn, *, supply_ids):
    """Current accepted fee authority; immutable historical FF is independent."""
    from packages.application.fulfillment_services import FulfillmentServicesBlock
    from packages.application import fulfillment_recalc_intents as intents
    overlays=FulfillmentServicesBlock.approved_overlay_in_connection(conn)
    queries=[]
    for supply in sorted(supply_ids):
        queries.append((f'SELECT u.*,l.* FROM {intents.UPLOADS} u JOIN {intents.LINES} l USING(upload_id) WHERE l.matched_wb_supply_id=? OR l.matched_wb_cache_key=? ORDER BY u.upload_id,l.row_index,l.id',
                        (supply,'supply:'+supply)))
    return dict(contract='historical_fbo_current_fee_overlay_v1',supply_ids=sorted(supply_ids),
        overlays={supply:deepcopy(overlays.get(supply,{})) for supply in sorted(supply_ids)},
        query_fence=json.loads(canonical(ready.pin_queries(conn,queries))))


def refresh_ordinary_protected_cost_layers(cost_block, *, supply_ids=None, opening_date):
    """Keep the immutable FF primary during every subsequent normal refresh."""
    from packages.application import operator_fulfillment_services as receipts
    from packages.application.fbs_historical_fbo_native import operation_metadata
    target={str(sid) for sid in supply_ids or []};groups={};protected=set()
    with ready.readonly(cost_block.runtime.db_path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_wb_supply_cost_layers'").fetchone():
            return dict(count=0,supply_ids=[])
        eligible={str(row[0]) for row in conn.execute("SELECT supply_id FROM sheet_vitrina_v1_wb_supplies WHERE COALESCE(supply_date,fact_date,updated_date,'')>=?",(opening_date,))}
        rows=conn.execute("SELECT wb_supply_id,nm_id,component_status_json FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE is_current=1").fetchall()
        for row in rows:
            sid=str(row['wb_supply_id'])
            if sid not in eligible or (target and sid not in target):continue
            ref=json.loads(row['component_status_json'] or '{}').get('historical_fbo_native_source')
            if ref is None:continue
            stages.require(ref.get('contract')==COST_LAYER_CONTRACT,'historical_fbo_protected_ff_contract_changed')
            intents=conn.execute("SELECT operation_id,attempt_id,inputs_json FROM sheet_vitrina_v1_ready_publications WHERE kind=? AND json_extract(inputs_json,'$.native_plan.plan_digest')=?",
                                 (EFFECT_CONTRACT,ref.get('plan_digest'))).fetchall()
            stages.require(len(intents)==1,'historical_fbo_protected_ff_recipe_missing_or_ambiguous')
            stored=json.loads(intents[0]['inputs_json'])
            binding=dict(operation_id=intents[0]['operation_id'],attempt_id=intents[0]['attempt_id'],recipe_digest=stored['recipe_digest'])
            recipe=read_effect_intent(conn,binding=binding)
            stages.require(_verify_existing(conn,recipe['native_plan']),'historical_fbo_protected_native_changed')
            matches=[(index,effect,line) for index,effect in enumerate(recipe['native_plan']['effects'])
                if effect.get('source_key')=='wb_supply_debit:supply:'+sid
                for line in effect['aggregate_movements'] if line['nm_id']==row['nm_id']]
            stages.require(len(matches)==1,'historical_fbo_protected_native_scope_changed')
            index,effect,line=matches[0]
            expected=dict(contract=COST_LAYER_CONTRACT,plan_digest=recipe['native_plan']['plan_digest'],
                operation_id=operation_id(recipe['native_plan'],index),source_book=recipe['expected_book'],
                business_date=effect['business_date'],nm_id=line['nm_id'],unit_cost_rub=line['wac_rub'],
                quantity_delta=line['quantity_delta'],capital_delta_rub=line['capital_delta_rub'])
            stages.require(ref==expected,'historical_fbo_protected_frozen_ff_changed')
            key=(binding['operation_id'],binding['attempt_id'],binding['recipe_digest'])
            group=groups.setdefault(key,dict(binding=binding,ids=set()))
            group['ids'].add(sid);protected.add(sid)
    changed=0
    for group in groups.values():
        changed+=materialize_cost_layers(cost_block,binding=group['binding'],supply_ids=sorted(group['ids']),ordinary_refresh=True)
    return dict(count=changed,supply_ids=sorted(protected))
