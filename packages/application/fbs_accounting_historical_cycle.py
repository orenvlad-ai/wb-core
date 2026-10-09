"""Receipt revision phases inside the existing warehouse and owned cycle.

No scheduler, standalone executor or source collector. An uncertain publication
is recovered by its exact durable intent before another candidate is prepared.
History runs only after the warehouse owner exits. Finalization reacquires the
real domain owner and retains the posting/readback rules of the native service.
"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from contextvars import ContextVar
_FINALIZING = ContextVar("historical_receipt_finalizing",default=None)

from packages.application import fbs_accounting_runtime as accounting
from packages.application import ready_publication as ready
from packages.application import fbs_accounting_historical_publication as publication
from packages.application import fbs_accounting_historical_sources as sources
from packages.application import fbs_accounting_historical_revision_writer as staging
from packages.application.fbs_accounting_historical_revision import build_historical_revision_plan
from packages.application.fbs_accounting_historical_stages import require
from packages.application.fbs_accounting_historical_history import pending_receipt
from packages.application.fbs_snapshot_cost import canonical,fingerprint
from packages.application.warehouse_functional_lock import require_warehouse_job_owner,warehouse_functional_write_lock


def refresh(runtime, *, now=None):
    require_warehouse_job_owner(runtime.runtime_dir)
    now=now or datetime.now(timezone.utc)
    book,version=accounting.load(runtime.runtime_dir)
    if not book or not book['active']:return None
    with ready.readonly(runtime.db_path) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sheet_vitrina_v1_ready_publications'").fetchone():
            prepared=conn.execute("SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE kind=? AND state<>'complete' ORDER BY created_at,operation_id,attempt_id",(publication.CONTRACT,)).fetchall()
            require(len(prepared)<=1,'historical_multiple_prepared_publications')
            if prepared:return publication.publish(json.loads(prepared[0][0]),runtime_dir=runtime.runtime_dir)
        pending=pending_receipt(runtime)
        if pending:
            # Do not supersede an unacknowledged contour with another book.
            pending.validate_sources_readonly(now=now)
            return None  # Closed suffix stays pinned; current day uses ordinary refresh.
        cohort=sources.capture_confirmed_cohort(conn,db_path=runtime.db_path,book=book,now=now)
        if not cohort['receipt_document_ids']:return None
        cap=cohort['capture'];day=cap['business_date']
        if day>max(book['state']['periods']):
            from packages.application.shared_sku_cost_sources import capture_wb_component
            from packages.application.fbs_inventory_presentation import capture_retained_stages
            nm_ids=sorted({int(r['nm_id']) for r in cap['quantity_snapshot']['rows']})
            wb=capture_wb_component(runtime.db_path,day=day,nm_ids=nm_ids,connection=conn)
            retained=capture_retained_stages(runtime.db_path,day=day,wb_version_id=wb['version_id'],nm_ids=nm_ids,connection=conn)
            cap['current_dated_inputs']=dict(wb_capture=wb,retained_capture=retained)
        plan=build_historical_revision_plan(book,cap,receipt_document_ids=cohort['receipt_document_ids'],
            current_document_ids=cohort['current_document_ids'],auxiliary_document_ids=cohort['auxiliary_document_ids'])
        require(plan['status']=='ready','historical_revision_inputs_blocked',source=canonical(plan.get('blocker',{})))
    # Existing native ready builder supplies only today's current local sources.
    # Older dates must be present in their saved exact dated ready envelopes.
    from packages.business_time import default_business_as_of_date,current_business_date_iso
    current=runtime.load_current_state()
    expected=runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version,as_of_date=default_business_as_of_date(now))
    current_envelope=None
    if not expected.exists:
        from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock,bind_local_derive_publication
        from packages.application.sheet_vitrina_v1_own_product_capital import OWN_PRODUCT_CAPITAL_SOURCE_KEY
        envelope=SheetVitrinaV1LivePlanBlock(runtime,now_factory=lambda:now).build_plan(as_of_date=expected.as_of_date,source_keys=(OWN_PRODUCT_CAPITAL_SOURCE_KEY,))
        _,expected=bind_local_derive_publication(runtime,envelope,current,expected)
        envelope=accounting._retain_ready_history(envelope,runtime.load_sheet_vitrina_ready_snapshot(),business_date=current_business_date_iso(now))
        current_envelope=(envelope,expected)
    operation_id='historical-receipt:'+fingerprint([version,plan['plan_fingerprint']])[7:]
    with warehouse_functional_write_lock(runtime.runtime_dir,timeout_seconds=2):
        with closing(sqlite3.connect(Path(runtime.db_path).as_uri()+'?mode=rw',uri=True,timeout=0)) as schema_conn:
            from packages.application.sheet_vitrina_v1_inventory_history import ensure_inventory_history_schema
            ensure_inventory_history_schema(schema_conn)
            ready.ensure_publication_schema(schema_conn);schema_conn.commit()
            staging.ensure_staging_schema(schema_conn)
    manifest=publication.prepare_publication(plan,runtime_dir=runtime.runtime_dir,owner_generation='source-book:'+version,
        operation_id=operation_id,now=now,current_envelope=current_envelope)
    return publication.publish(manifest,runtime_dir=runtime.runtime_dir)


def completion_authorized(conn, request_id):
    """Ordinary operator reconcile may not bypass the historical obligation."""
    selected=_FINALIZING.get()
    if selected is None or request_id not in selected:return False
    rows=conn.execute("SELECT inputs_json,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE kind=? AND state='complete'",(publication.CONTRACT,)).fetchall()
    for row in rows:
        manifest=json.loads(row['inputs_json'])
        if request_id in manifest['operator_confirmation']:
            ack=json.loads(row['diagnostics_json']).get('historical_history_ack')
            return bool(ack and ack['manifest_digest']==manifest['manifest_digest'] and {d for d in manifest['plan']['scope']['dates'] if d < manifest['plan']['scope']['date_to']} <= set(ack['native']) <= set(manifest['plan']['scope']['dates']))
    return False


def finalize(runtime, *, config, now=None):
    """After the History child exits, under a newly acquired real job owner."""
    from packages.application.business_data_heavy_admission import require_heavy_owner
    require(require_heavy_owner(runtime.runtime_dir).operation=='cycle','historical_completion_cycle_owner_required')
    require_warehouse_job_owner(runtime.runtime_dir)
    receipt=pending_receipt(runtime)
    if receipt is None:return dict(status='not_needed')
    now=now or datetime.now(timezone.utc)
    receipt.freeze_scope(now)
    sources_before=receipt.validate_sources_readonly(now=now)
    manifest,diagnostics=receipt._read();ack=diagnostics.get('historical_history_ack')
    require(ack and ack['manifest_digest']==manifest['manifest_digest'] and set(ack['native'])==set(receipt.dates),'historical_completion_history_ack_missing')
    from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
    from packages.application.web_vitrina_history_store import HistoryStore
    from packages.application.web_vitrina_history_compiler import dates_between
    from datetime import timedelta
    from packages.business_time import current_business_date_iso
    today=current_business_date_iso(now)
    first=min(receipt.dates[0],(datetime.fromisoformat(today)-timedelta(days=13)).date().isoformat())
    adapter=LiveNativeAdapter(db_path=runtime.db_path,runtime_dir=runtime.runtime_dir,cache_dir=Path(config.candidate_root)/'proofs',now=now,
        date_from=first,date_to=today,formula_epoch=config.formula_epoch)
    native=receipt.validate_native_readonly(adapter=adapter,store=HistoryStore(Path(config.candidate_root)/'history'),now=now)
    require(all(native['native'][d]==ack['native'][d] for d in receipt.dates),'historical_completion_history_current_changed')
    # Finance's native dependency and target CAS run while the book owner is
    # held; no operation-level BEGIN spans its numerical preparation.
    from apps.warehouse_functional_runner import _recalculate_downstream_finance_cost
    from packages.application.operator_warehouse_documents import TABLE,_update,reconcile
    finance=_recalculate_downstream_finance_cost(runtime)
    require(finance.get('status') in {'applied','already_current'} and finance.get('non_target_preserved') is True
        and finance.get('post_verify_stale_week_count')==0 and finance.get('accounting_version_unchanged') is True
        and finance.get('source_advanced_after_apply') is not True,'historical_completion_finance_unproven')
    require(receipt.validate_sources_readonly(now=now)==sources_before,'historical_completion_source_changed')
    economics=accounting.current_publication_receipt(runtime,now=now)
    identities=list(manifest['operator_confirmation'])
    with ready.readonly(runtime.db_path) as conn:
        rows=[dict(conn.execute(f'SELECT * FROM {TABLE} WHERE request_id=?',(identity,)).fetchone()) for identity in identities]
    for row in rows:
        value=json.loads(row['receipt_json']);value['historical_publication']=dict(manifest_digest=manifest['manifest_digest'],operation_id=receipt.operation_id,dates=list(receipt.dates),history_ack=ack)
        if row['state']!='completed':_update(runtime.db_path,row['request_id'],'processing','historical_completion_pending',value)
    token=_FINALIZING.set(frozenset(identities))
    try:result=reconcile(runtime,request_ids=identities,finance_receipt=finance,economics_receipt=economics,now=now)
    finally:_FINALIZING.reset(token)
    with ready.readonly(runtime.db_path) as conn:
        require(all(conn.execute(f'SELECT state FROM {TABLE} WHERE request_id=?',(identity,)).fetchone()[0]=='completed' for identity in identities),'historical_completion_native_posted_readback_pending')
    with warehouse_functional_write_lock(runtime.runtime_dir,timeout_seconds=2):
        with closing(sqlite3.connect(Path(runtime.db_path).as_uri()+'?mode=rw',uri=True,timeout=0)) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(receipt.operation_id,receipt.attempt_id)).fetchone()
            actual=json.loads(row[0]);require(actual.get('historical_history_ack')==ack,'historical_completion_ack_changed')
            actual.update(historical_operator_complete=True,historical_finance=finance,historical_completed_requests=identities)
            require(conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=? AND diagnostics_json=?',(canonical(actual),receipt.operation_id,receipt.attempt_id,row[0])).rowcount==1,'historical_completion_cas_failed')
    return dict(status='complete',operation_id=receipt.operation_id,manifest_digest=manifest['manifest_digest'],request_count=len(identities),**{'native_reconcile':result})
