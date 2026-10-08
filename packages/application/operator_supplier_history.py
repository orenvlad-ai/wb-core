"""Supplier-owned cost-only dated publication and separate History authority.

No physical document, modern posted money or closed receipt is rewritten. The
existing native ready intent, book revisions and owned History worker provide
recovery and exact completion; this module adds no independent job queue.
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import json,sqlite3,time
from packages.application import ready_publication as ready, fbs_accounting_runtime as accounting
from packages.application import operator_supplier_history_candidate as candidate
from packages.application import operator_supplier_history_sources as sources
from packages.application import operator_supplier_processing as processing
from packages.application import fbs_accounting_historical_stages as stages
from packages.application.fbs_snapshot_cost import fingerprint,canonical

CONTRACT='supplier_cost_dated_publication_v1'
MAX_COHORT=32
require=sources.require
EXPECTED_NATIVE={
    'supplier_completion_source_changed','supplier_correlated_functional_proof_missing',
    'supplier_correlated_source_changed','supplier_functional_version_missing',
    'supplier_native_wb_source_changed','supplier_native_cost_projection_changed',
    'supplier_native_cost_operands_changed','supplier_native_cost_unavailable',
    'supplier_native_balance_publication_changed','supplier_wb_supply_authority_incomplete',
    'supplier_wb_supply_sku_scope_changed','supplier_wb_cost_projection_pending',
    'policy_history_dated_ready_inputs_missing','policy_history_dated_book_binding_changed',
    'historical_complete_stage_models_missing','historical_saved_functional_version_missing',
    'historical_saved_wb_authority_changed','historical_exact_snapshot_missing',
}


def _checked(runtime,identity,reason,now,*,state='processing'):
    with closing(sqlite3.connect(runtime.db_path)) as conn:
        conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
        actual=processing.operation(conn,identity)
        if processing.superseded(conn,actual):state,reason='needs_attention',processing.SUPERSEDED
        conn.execute(f'INSERT OR REPLACE INTO {processing.ATTEMPTS} VALUES(?,?,?,?)',(identity,state,reason,now.isoformat()));conn.commit()


def _verify(manifest):
    require(manifest.get('contract')==CONTRACT and manifest.get('manifest_digest')==fingerprint({k:v for k,v in manifest.items() if k!='manifest_digest'}),'publication_corrupt')
    require(manifest['operation_id']=='supplier-history:'+manifest['source_ref']['operation_id'],'publication_identity_changed')
    require(manifest['candidate']['code_authority']==candidate.code_authority(),'formula_changed')
    require(sorted({*manifest['candidate']['effect_dates'],manifest['candidate']['functional']['business_date']})==sorted({d for t in manifest['targets'] for d in t['dates']}) and manifest['candidate']['effect_dates'],'publication_scope_changed')
    require(manifest['after_book']==fingerprint(manifest['candidate']['candidate_book']),'book_candidate_corrupt')


def _targets(runtime, numerical, *, now):
    from packages.application.historical_dated_inputs import selected,dated_slice,book_lineage
    from packages.application.fbs_accounting_historical_publication import materialize_exact_dates
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan,_serialize_sheet_vitrina_plan
    from packages.application.inventory_quantity import resolve_plan_quantities,CONTRACT as QUANTITY_CONTRACT,EFFECTIVE_DATE as QUANTITY_EFFECTIVE_DATE
    from packages.application.sheet_vitrina_v1_inventory_history import FINALIZATIONS_TABLE
    from packages.application.web_vitrina_management_history import dated_parameters
    after_book=fingerprint(numerical['candidate_book']);targets=[]
    with ready.readonly(runtime.db_path) as conn:
        dates=sorted({*numerical['effect_dates'],numerical['functional']['business_date']})
        for row,days in selected(conn,dates,numerical['functional']['business_date']):
            target={'bundle_version':row['bundle_version'],'as_of_date':row['as_of_date']}
            plan=materialize_exact_dates(_deserialize_sheet_vitrina_plan(row['plan_json']),book=numerical['candidate_book'],version=after_book,runtime_dir=runtime.runtime_dir,target=target,dates=days,now=now,connection=conn)
            inventory=[]
            for day in days:
                if day<QUANTITY_EFFECTIVE_DATE:continue  # Native inventory authority does not exist before its effective date.
                operands=resolve_plan_quantities(plan,day=day,prepared_book=numerical['candidate_book'],ready_target=target,require_closed=day<numerical['functional']['business_date'])
                require(operands,'inventory_exact_dated_inputs_missing')
                predecessor=conn.execute(f'SELECT finalization_digest FROM {FINALIZATIONS_TABLE} WHERE business_date=? ORDER BY finalization_sequence DESC LIMIT 1',(day,)).fetchone()
                inventory.append(dict(business_date=day,capture_kind='accepted_refresh',formula_version=QUANTITY_CONTRACT,bundle_version=row['bundle_version'],ready_snapshot_id=plan.snapshot_id,ready_plan_version=plan.plan_version,generation_identity=row['bundle_version'],facility_roster=operands['facility_roster'],source_manifest=operands['source_manifest'],components=operands['components'],captured_at=now.isoformat(),expected_predecessor=predecessor[0] if predecessor else '',closed=day<numerical['functional']['business_date']))
            encoded=_serialize_sheet_vitrina_plan(plan)
            targets.append(dict(expected=asdict(ready.ExpectedReady(row['bundle_version'],row['as_of_date'],row['plan_json'])),after_json=encoded,dates=days,inventory=inventory,dated_outputs={day:fingerprint(dated_slice(encoded,day)) for day in days}))
        targets.sort(key=lambda target:(numerical['functional']['business_date'] in target['dates'],target['expected']['as_of_date']))
        parameters={day:dated_parameters(conn,day) for day in dates}
    return targets,parameters


def prepare(runtime,operation_id,*,now):
    numerical=candidate.prepare(runtime,operation_id,now=now)
    operation_id=numerical['operation_id']
    require(numerical['effect_dates'],'no_dated_effect')
    targets,parameters=_targets(runtime,numerical,now=now)
    result=dict(contract=CONTRACT,operation_id='supplier-history:'+operation_id,attempt_id=numerical['attempt_id'],source_ref=numerical['source_ref'],candidate=numerical,targets=targets,parameter_proof=parameters,expected_book=numerical['before_book'],after_book=fingerprint(numerical['candidate_book']),created_at=now.isoformat(),authority=ready.capture_authority(runtime.runtime_dir),book_operation_id='supplier-history-book:'+numerical['attempt_id'])
    require(len(canonical(result).encode())<=candidate.MAX_BYTES,'publication_size_limit')
    result=json.loads(canonical(result))
    result['manifest_digest']=fingerprint(result);_verify(result);return result


def _fence(conn,manifest,*,ready_before=True):
    _verify(manifest);ready.check_pinned_authority(conn,manifest['authority'])
    stages.check_query_fence(conn,manifest['candidate']['source_inputs'])
    stages.check_query_fence(conn,manifest['candidate']['query_fence'])
    for ref in manifest['candidate']['cohort_refs']:
        actual=processing.operation(conn,ref['operation_id'])
        require(processing.ref(actual)==ref and processing.current(conn,actual),'native_source_changed')
    from packages.application.web_vitrina_management_history import dated_parameters
    require({day:dated_parameters(conn,day) for day in manifest['parameter_proof']}==manifest['parameter_proof'],'dated_parameters_changed')
    if ready_before:
        for target in manifest['targets']:ready.check_expected(conn,ready.ExpectedReady(**target['expected']))
    check_obligations(conn,manifest,retired=not ready_before)
    for recovery in manifest['candidate']['recovery_inputs']:
        before,_=accounting.load(Path(manifest['authority']['runtime_dir']),version=manifest['expected_book'])
        original,_=accounting.load(Path(manifest['authority']['runtime_dir']),version=recovery['expected_book'])
        require(candidate.book_tuple(before,recovery['day'])==recovery['current_tuple'] and candidate.book_tuple(original,recovery['day'])==recovery['original_tuple'],'ghost_book_tuple_changed')
        with closing(sources.source.readonly(accounting.path(Path(manifest['authority']['runtime_dir'])))) as ledger:
            row=ledger.execute('SELECT version,payload,previous_version FROM accounting_revisions WHERE operation_id=?',(recovery['book_operation_id'],)).fetchone()
            prior=next(item for item in manifest['candidate']['obligations'] if item['operation_id']==recovery['prior_operation'] and item['attempt_id']==recovery['prior_attempt'])
            require(row and row[0]==recovery['after_book'] and row[2]==recovery['expected_book'] and accounting.unpack(ledger,row[1])==json.loads(prior['inputs_json'])['candidate']['candidate_book'],'ghost_book_revision_unproved')
        current,version=accounting.load(Path(manifest['authority']['runtime_dir']))
        require(version in (manifest['expected_book'],manifest['after_book']) and candidate.book_tuple(current,recovery['day'])==candidate.book_tuple(before if version==manifest['expected_book'] else manifest['candidate']['candidate_book'],recovery['day']),'ghost_current_tuple_changed')


def replacement_link(manifest):
    return dict(operation_id=manifest['operation_id'],attempt_id=manifest['attempt_id'],manifest_digest=manifest['manifest_digest'],reason='native_cohort_superseded')


def verified_retirement(conn,row):
    """A diagnostic alone cannot hide an owed source or fabricate completion."""
    diagnostics=json.loads(row['diagnostics_json'])
    if 'supplier_history_replaced_by' not in diagnostics:return False
    link=diagnostics['supplier_history_replaced_by']
    require(isinstance(link,dict) and set(link)=={'operation_id','attempt_id','manifest_digest','reason'} and link['reason']=='native_cohort_superseded','retirement_link_corrupt')
    new=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(link['operation_id'],link['attempt_id'])).fetchone()
    require(new and new['kind']==CONTRACT and new['state']=='complete','retirement_publication_missing')
    manifest=json.loads(new['inputs_json']);_verify(manifest)
    require(link==replacement_link(manifest) and new['book_version']==manifest['after_book'] and new['after_digest']==ready.digest(manifest['targets'][-1]['after_json']),'retirement_publication_changed')
    prior=json.loads(row['inputs_json']);_verify(prior)
    require(any(item['operation_id']==row['operation_id'] and item['attempt_id']==row['attempt_id'] and item['inputs_json']==row['inputs_json'] and item['manifest_digest']==prior['manifest_digest'] for item in manifest['candidate']['obligations']) and set(prior['candidate']['effect_dates'])<=set(manifest['candidate']['effect_dates']) and 'supplier_history_ack' not in diagnostics,'retirement_obligations_changed')
    return True


def check_obligations(conn,manifest,*,retired=False):
    for item in manifest['candidate']['obligations']:
        row=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(item['operation_id'],item['attempt_id'])).fetchone()
        require(row,'prior_attempt_missing');diagnostics=json.loads(row[0])
        require('supplier_history_ack' not in diagnostics,'prior_history_ack_changed')
        expected=json.loads(item['diagnostics_json'])
        if retired:expected['supplier_history_replaced_by']=replacement_link(manifest)
        require(diagnostics==expected,'prior_attempt_retirement_changed')


def retire_obligations(conn,manifest):
    check_obligations(conn,manifest)
    for item in manifest['candidate']['obligations']:
        diagnostics=json.loads(item['diagnostics_json']);diagnostics['supplier_history_replaced_by']=replacement_link(manifest)
        require(conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=? AND diagnostics_json=?',(canonical(diagnostics),item['operation_id'],item['attempt_id'],item['diagnostics_json'])).rowcount==1,'prior_retirement_cas_failed')


def publish(runtime,manifest,*,inject=lambda point:None):
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner,warehouse_functional_write_lock
    from packages.application.fbs_accounting_historical_publication import _append_and_activate
    require(require_heavy_owner(runtime.runtime_dir).operation=='cycle','cycle_owner_required');require_warehouse_job_owner(runtime.runtime_dir)
    _verify(manifest)
    with ready.readonly(runtime.db_path) as conn:
        prior=conn.execute('SELECT state,inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(manifest['operation_id'],manifest['attempt_id'])).fetchone()
        if prior:require(json.loads(prior['inputs_json'])==manifest,'retry_identity_conflict')
    if prior and prior['state']=='complete':return SupplierHistory(runtime,manifest['operation_id'],manifest['attempt_id'])
    # The immutable before-book is still independently rebuildable after a
    # crash that activated the own candidate but rolled back the registry TX.
    clock=datetime.fromisoformat(manifest['created_at']);candidate.validate(manifest['candidate'],runtime=runtime,now=clock)
    rebuilt,parameters=_targets(runtime,manifest['candidate'],now=clock)
    require(rebuilt==manifest['targets'] and parameters==manifest['parameter_proof'],'ready_independent_rebuild_changed')
    with warehouse_functional_write_lock(runtime.runtime_dir,timeout_seconds=5),accounting.writer_lock(runtime.runtime_dir),closing(sqlite3.connect(Path(runtime.db_path).as_uri()+'?mode=rw',uri=True,timeout=0)) as conn:
        conn.row_factory=sqlite3.Row
        with conn:
            conn.execute('BEGIN IMMEDIATE');_fence(conn,manifest)
            row=conn.execute('SELECT inputs_json,state FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(manifest['operation_id'],manifest['attempt_id'])).fetchone()
            require(not row or json.loads(row['inputs_json'])==manifest,'retry_identity_conflict')
            if not row:ready.record_intent(conn,operation_id=manifest['operation_id'],attempt_id=manifest['attempt_id'],kind=CONTRACT,expected=ready.ExpectedReady(**manifest['targets'][-1]['expected']),inputs=manifest,expected_book=manifest['expected_book'],book_required=True,ready_required=True,created_at=manifest['created_at'],book_operation_id=manifest['book_operation_id'])
        inject('after_intent')
        with conn:
            conn.execute('BEGIN IMMEDIATE');_fence(conn,manifest)
            _append_and_activate(runtime.runtime_dir,manifest);inject('after_book')
            stages.publish_dated_stages(conn,manifest=manifest['candidate']);inject('after_stages')
            from packages.application.sheet_vitrina_v1_inventory_history import append_inventory_history_capture,append_inventory_history_finalization
            for target in manifest['targets']:
                envelope=json.loads(target['after_json'])
                ready.replace_ready(conn,expected=ready.ExpectedReady(**target['expected']),plan_json=target['after_json'],activated_at=manifest['created_at'],snapshot_id=envelope['snapshot_id'],plan_version=envelope['plan_version'],refreshed_at=manifest['created_at'])
                for item in target['inventory']:
                    capture=append_inventory_history_capture(conn,**{k:v for k,v in item.items() if k not in {'expected_predecessor','closed'}})
                    if item['closed']:append_inventory_history_finalization(conn,business_date=item['business_date'],capture_id=capture['capture_id'],finalization_identity=manifest['operation_id']+':'+item['business_date'],finalized_at=manifest['created_at'],expected_predecessor=item['expected_predecessor'],provenance=dict(contract=CONTRACT,manifest_digest=manifest['manifest_digest'],book_version=manifest['after_book']))
            inject('after_ready')
            ready.complete_publication(conn,operation_id=manifest['operation_id'],attempt_id=manifest['attempt_id'],book_version=manifest['after_book'],after_digest=ready.digest(manifest['targets'][-1]['after_json']),finished_at=manifest['created_at'])
            inject('before_retirement')
            retire_obligations(conn,manifest)
    receipt=SupplierHistory(runtime,manifest['operation_id'],manifest['attempt_id']);receipt.validate_sources_readonly(now=clock);return receipt


class SupplierHistory:
    """This type, not FF or policy authority, enrolls the exact supplier dates."""
    def __init__(self,runtime,operation_id,attempt_id):
        self.runtime=runtime;self.operation_id=operation_id;self.attempt_id=attempt_id
        self.manifest=self._read()[0];self.dates=tuple(self.manifest['candidate']['effect_dates'])
    def _read(self):
        with ready.readonly(self.runtime.db_path) as conn:
            row=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(self.operation_id,self.attempt_id)).fetchone()
            require(row and row['kind']==CONTRACT and row['state']=='complete' and len(row['inputs_json'].encode())<=candidate.MAX_BYTES,'publication_not_complete')
            manifest=json.loads(row['inputs_json']);_verify(manifest)
            require('supplier_history_replaced_by' not in json.loads(row['diagnostics_json']),'publication_superseded')
            require(manifest['operation_id']==self.operation_id and manifest['attempt_id']==self.attempt_id and row['book_version']==manifest['after_book'] and row['after_digest']==ready.digest(manifest['targets'][-1]['after_json']),'publication_readback_changed')
            return manifest,json.loads(row['diagnostics_json'])
    def binding(self):
        require(self._read()[0]==self.manifest,'receipt_changed')
        return dict(operation_id=self.operation_id,attempt_id=self.attempt_id,digest=self.manifest['manifest_digest'],dates=list(self.dates))
    def publication_dates(self):return self.dates
    def _validate_sources_conn(self,conn):
        from packages.application.historical_dated_inputs import selected,dated_slice,book_lineage
        manifest=self.manifest;_fence(conn,manifest,ready_before=False)
        active,version=accounting.load(self.runtime.runtime_dir);expected=manifest['candidate']['candidate_book']
        require(all(active[field].get(day)==expected[field][day] for field in ('wb_days','retained_days','shared_days','presentations') for day in self.dates),'book_dates_changed')
        proof={}
        for row,days in selected(conn,list(self.dates),manifest['candidate']['functional']['business_date']):
            for day in days:
                target=next(t for t in manifest['targets'] if day in t['dates'])
                require(fingerprint(dated_slice(row['plan_json'],day))==target['dated_outputs'][day],'dated_ready_changed')
                lineage=book_lineage(self.runtime,row['plan_json'],day)
                require(lineage=={field:expected[field][day] for field in ('wb_days','retained_days','shared_days','presentations')},'dated_book_binding_changed')
                edition=manifest['candidate']['editions'][day]
                actual=conn.execute(f'SELECT * FROM {stages.P}functional_versions WHERE version_id=?',(edition['version_id'],)).fetchone()
                require(actual and dict(actual)==edition['version'] and stages._version_balances(conn,version_id=edition['version_id'])==edition['balances'],'dated_stage_readback_changed')
                proof[day]=dict(ready_key=[row['bundle_version'],row['as_of_date']],ready_digest=ready.digest(row['plan_json']),dated_digest=target['dated_outputs'][day],functional_version=edition['version_id'])
        require(set(proof)==set(self.dates),'dated_scope_missing')
        return dict(binding=self.binding(),dated=proof,active_book=version)
    def validate_sources_readonly(self,*,now):
        with ready.readonly(self.runtime.db_path) as conn:result=self._validate_sources_conn(conn)
        require(self._read()[0]==self.manifest,'receipt_changed');return result
    def validate_native_readonly(self,*,adapter,store,now):
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
        from packages.application.web_vitrina_history_store import HistoryStore
        from packages.application.owned_history_native_ack import _object
        require(type(adapter) is LiveNativeAdapter and type(store) is HistoryStore and adapter.db_path==Path(self.runtime.db_path).resolve() and adapter.runtime_dir==Path(self.runtime.runtime_dir).resolve(),'native_context_changed')
        before=self.validate_sources_readonly(now=now);vector=adapter.capture();fence=adapter.fence;pointer=store._current();require(pointer,'history_current_missing');edition=store.edition();proofs=store.day_proofs(edition);native={}
        for day in self.dates:
            require(day in edition['days'] and proofs.get(day)=={'epoch':vector['epoch'],'token':vector['dates'].get(day)},'native_date_pending')
            _object(store,day,edition['days'][day],store.day_catalogs(edition)[day],time.monotonic()+30)
            native[day]=dict(edition_id=pointer['current'],object_digest=edition['days'][day],**proofs[day],**before['dated'][day])
        require(self.validate_sources_readonly(now=now)==before and adapter.capture()==vector and adapter.fence==fence and store._current()==pointer,'changed_during_ack')
        return dict(receipt_digest=self.manifest['manifest_digest'],current=pointer,vector=vector,fence=fence,native=native)
    def _acknowledge_verified_native(self,proof):
        from packages.application.owned_history_native_ack import consume_supplier_history_ack
        native=consume_supplier_history_ack(proof,self.runtime.runtime_dir,self.manifest['manifest_digest'],self.dates)
        with closing(sqlite3.connect(Path(self.runtime.db_path).as_uri()+'?mode=rw',uri=True,timeout=0)) as conn,conn:
            conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
            current=self._validate_sources_conn(conn)['dated']
            require(set(current)==set(native) and all(all(native[day].get(k)==v for k,v in current[day].items()) for day in current),'ack_source_changed')
            row=conn.execute('SELECT inputs_json,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(self.operation_id,self.attempt_id)).fetchone();require(row and json.loads(row['inputs_json'])==self.manifest,'receipt_changed')
            diagnostics=json.loads(row['diagnostics_json']);diagnostics['supplier_history_ack']=dict(manifest_digest=self.manifest['manifest_digest'],native={day:dict(value) for day,value in native.items()})
            require(conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=? AND diagnostics_json=?',(canonical(diagnostics),self.operation_id,self.attempt_id,row['diagnostics_json'])).rowcount==1,'ack_cas_failed')


def publication_for(conn,source_ref):
    """Resolve ONLY the saved exact cohort membership, never all pending rows."""
    rows=list(conn.execute("SELECT p.* FROM sheet_vitrina_v1_ready_publications p WHERE p.kind=? AND EXISTS(SELECT 1 FROM json_each(p.inputs_json,'$.candidate.cohort_refs') m WHERE json_extract(m.value,'$.operation_id')=? AND json_extract(m.value,'$.source_digest')=?) ORDER BY p.created_at DESC,p.rowid DESC LIMIT ?",(CONTRACT,source_ref['operation_id'],source_ref['source_digest'],candidate.MAX_ATTEMPTS+1)))
    require(len(rows)<=candidate.MAX_ATTEMPTS,'attempt_scope_limit')
    return next((row for row in rows if not verified_retirement(conn,row)),None)


def completed_proof(conn,source_ref):
    """Cheap native receipt read; callers still hold source/book/ready CAS."""
    row=publication_for(conn,source_ref)
    if not row:return None
    value=completed_row(row)
    if value:require(source_ref in value['manifest']['candidate']['cohort_refs'],'native_source_changed')
    return value


def completed_row(row):
    """Validate one retained native ack; missing and corrupt are distinct."""
    if row['state']!='complete':return None
    manifest=json.loads(row['inputs_json']);_verify(manifest)
    diagnostics=json.loads(row['diagnostics_json'])
    if 'supplier_history_ack' not in diagnostics:return None
    ack=diagnostics['supplier_history_ack']
    require(isinstance(ack,dict) and ack,'history_ack_corrupt')
    require(ack.get('manifest_digest')==manifest['manifest_digest'] and set(ack.get('native',{}))==set(manifest['candidate']['effect_dates']) and all(v.get('object_digest') and v.get('edition_id') and v.get('functional_version')==manifest['candidate']['editions'][day]['version_id'] for day,v in ack['native'].items()),'history_ack_corrupt')
    return dict(manifest=manifest,ack=ack)


def pending(runtime,*,now):
    """Bounded oldest-check fairness using the existing supplier attempt rows."""
    with ready.readonly(runtime.db_path) as conn:
        if not sources.source._exists(conn,processing.FUNCTIONAL):return None
        identities=processing.captured_cohort(conn)
    for identity in identities:
        try:
            with ready.readonly(runtime.db_path) as conn:
                op=processing.operation(conn,identity)
                if not processing.current(conn,op):
                    _checked(runtime,identity,'supplier_history_native_source_pending',now);continue
                if op['action']=='factual_date':
                    _checked(runtime,identity,'supplier_history_native_factual_owner_pending',now);continue
                prior=publication_for(conn,processing.ref(op))
                if prior and completed_proof(conn,processing.ref(op)):
                    _checked(runtime,identity,'supplier_history_finance_pending',now);continue
            if prior:
                manifest=json.loads(prior['inputs_json']);_verify(manifest)
                try:
                    receipt=publish(runtime,manifest);receipt.validate_sources_readonly(now=now);return receipt
                except ValueError as exc:
                    if not isinstance(exc,(sources.SupplierHistoryError,ready.ReadyPublicationConflict)) and str(exc).split(':')[0] not in EXPECTED_NATIVE:raise
                    # An actual newer native source publication must prove
                    # supersession; candidate discovery preserves all owed
                    # dates and refuses unknown/code/Ready-only drift.
                    numerical=candidate.prepare(runtime,identity,now=now)
                    require(numerical['obligations'] and numerical['attempt_id']!=manifest['attempt_id'],'native_attempt_not_superseded')
                    return publish(runtime,prepare(runtime,identity,now=now))
            numerical=candidate.prepare(runtime,identity,now=now)
            if not numerical['effect_dates']:
                _checked(runtime,identity,'supplier_history_no_dated_effect',now);continue
            return publish(runtime,prepare(runtime,identity,now=now))
        except ValueError as exc:
            reason=str(exc)
            if not isinstance(exc,(sources.SupplierHistoryError,ready.ReadyPublicationConflict)) and reason.split(':')[0] not in EXPECTED_NATIVE:raise
            state='processing' if any(word in reason for word in ('missing','pending','unavailable')) else 'needs_attention'
            _checked(runtime,identity,reason,now,state=state)
    return None
