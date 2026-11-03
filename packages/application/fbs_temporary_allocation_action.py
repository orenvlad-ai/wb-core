"""Registered temporary FBS allocation repair through native owned phases.

The private rehearsal must price the actual archived WB sources and produce a
healthy full book before the durable source recipe may admit monetary writes.
Readback never writes. A continuation owns the original recipe and never
reconstructs new effects from a partly applied current state.

Use the existing ``fbs_snapshot_accounting_v1`` adapter with action
``restore_temporary_fbs_allocation`` and the standard runtime SHA/storage,
held-maintenance identities plus evidence_manifest_path/sha256. The manifest
contains original_document_id, facility_id, official_path, terminal_path,
anchors_path, approval_reference and the exact hashed source_files list.
Recovery uses action ``recover_temporary_fbs_allocation``, a NEW launcher
operation ID, recovery_ref and original_recipe_digest. Finish this operation's
owned History/Finance cycle before preparing the separate historical-FBO
restoration: their exact after-book completion guards remain independent.
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

from apps.production_apply_contract import AmbiguousSubmit
from packages.application import fbs_accounting_runtime as accounting
from packages.application import fbs_accounting_historical_publication as publication
from packages.application import fbs_accounting_historical_revision_writer as staging
from packages.application import fbs_accounting_historical_stages as stages
from packages.application import fbs_accounting_historical_sqlite as owned_sqlite
from packages.application import fbs_historical_fbo_action as shared
from packages.application import ff_pool_fbs_historical_handoff as handoffs
from packages.application import ff_pool_fbs_lifecycle as lifecycle
from packages.application import ff_pool_documents as docs
from packages.application import ready_publication as ready
from packages.application.fbs_snapshot_cost import canonical, fingerprint, _last_closed
from packages.application.ff_pool_foundation import BALANCES_TABLE, LINES_TABLE, OPERATIONS_TABLE
from packages.application.storage_registry import StoreRegistry
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock, require_warehouse_job_owner
from packages.contracts.ff_pool_documents import DocumentIdentity

RESTORE = 'restore_temporary_fbs_allocation'
RECOVER = 'recover_temporary_fbs_allocation'
CONTRACT = 'fbs_temporary_allocation_publication_v1'
EFFECT_CONTRACT = 'fbs_temporary_allocation_effect_intent_v1'
# Retain the complete prior historical receipt: measured source operands exceed
# 512 MiB. This private-copy budget does not change publication/apply limits.
MAX_BYTES = 1024 * 1024**2
MAX_ROWS = 100000
MAX_PREFLIGHT_BUILD_SECONDS = 1800
MAX_COMPLETION_AGE_SECONDS = 600
COMPLETION_CONTRACT = 'temporary_fbs_verified_completion_v1'
P = 'sheet_vitrina_v1_'


def require(value, code):
    if not value: raise ValueError('temporary_fbs_allocation_' + code)


def _repair():
    from packages.application import fbs_temporary_allocation_repair
    return fbs_temporary_allocation_repair


def _code():
    names=('fbs_temporary_allocation_action.py','fbs_temporary_allocation_repair.py','fbs_temporary_allocation_foreign_sources.py','wb_historical_fbs_cost.py',
           'ff_pool_fbs_historical_handoff.py','ff_pool_fbs_lifecycle.py','ff_pool_documents.py','fbs_current_snapshot_policy.py')
    return {**shared._preflight_code(),**{name:'sha256:'+hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}}


def _directory(root, operation_id):
    # Reuse the existing registered private evidence storage authority.
    base=shared._directory(Path(root),operation_id)
    result=(base.parent.parent.parent/'temporary-fbs-allocation'/base.parent.name/operation_id if shared._hosted()
            else base.parent.parent/'temporary-fbs-allocation'/operation_id)
    require(not any(path.is_symlink() for path in (result,result.parent,result.parent.parent)),'private_directory_symlink')
    return result


def _inputs(request):
    require(not any(k in request for k in ('capture','candidate_book','native_plan','unit')), 'pasted_operands_forbidden')
    retained=request.get('retained_files',{})
    def path(value): return Path(retained.get(str(Path(value).resolve()),str(Path(value).resolve())))
    manifest=path(request['evidence_manifest_path']);data=manifest.read_bytes()
    require(len(data)<=2*1024**2 and not manifest.is_symlink()
            and hashlib.sha256(data).hexdigest()==request['evidence_manifest_sha256'], 'manifest_changed')
    value=json.loads(data)
    require(set(value)=={'original_document_id','facility_id','official_path','terminal_path','anchors_path','approval_reference','source_files'}, 'documentary_fields_invalid')
    evidence=handoffs.read_reviewed_handoff_evidence(official_path=path(value['official_path']),terminal_path=path(value['terminal_path']),
        anchors_path=path(value['anchors_path']),approval_reference=value['approval_reference'])
    expected={}
    for source in value['source_files']:
        file=path(source['path']);blob=file.read_bytes()
        require(not file.is_symlink() and len(blob)<=4*1024**2 and hashlib.sha256(blob).hexdigest()==source['sha256'], 'primary_file_changed')
        require(file.name not in expected, 'duplicate_primary_name')
        expected[file.name]='sha256:'+source['sha256']
    require(expected==evidence['source_files'], 'complete_primary_scope_required')
    return value,evidence


def _retain(directory,request,inputs):
    folder=directory/'reviewed-sources';folder.mkdir(mode=0o700)
    sources=[dict(path=request['evidence_manifest_path'],sha256=request['evidence_manifest_sha256']),*inputs[0]['source_files']]
    retained={}
    for item in sources:
        source=Path(item['path']);blob=source.read_bytes()
        require(hashlib.sha256(blob).hexdigest()==item['sha256'], 'retained_source_changed')
        target=folder/source.name;require(not target.exists(), 'retained_name_conflict')
        shared._admit(target,len(blob))
        with os.fdopen(os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb') as handle:
            handle.write(blob);handle.flush();os.fsync(handle.fileno())
        retained[str(source.resolve())]=str(target)
    return retained


def _snapshot(runtime,inputs,*,now):
    source,evidence=inputs;book,version=accounting.load(runtime.runtime_dir)
    require(book and book.get('active') is True and book.get('schema')==accounting.SCHEMA, 'active_book_required')
    last,_=_last_closed(book['state']);require(last=='2026-10-26','exact_closed_26_boundary_required')
    opened=book['state']['periods'].get('2026-10-27',{})
    require(opened.get('status')=='open' and opened.get('quality')=='incomplete'
            and book.get('publication_error',{}).get('reason')=='incomplete_period_cannot_close','exact_open_incomplete_27_required')
    handoffs._boundary(book,allocation_date='2026-10-27',handoff_dates=['2026-10-28','2026-10-29'])
    nms=sorted({row['order']['nm_id'] for row in evidence['orders']});facility=source['facility_id']
    with ready.readonly(runtime.db_path) as conn:
        from packages.application.fbs_current_snapshot_policy import resolve_current_snapshot_policy
        freshness_policy=asdict(resolve_current_snapshot_policy(connection=conn))
        original=handoffs._document(conn,source['original_document_id'])
        require(original['document']['document_kind']=='china_acceptance' and original['document']['business_date']=='2026-10-07', 'original_october_receipt_required')
        lines={row['nm_id']:row for row in original['lines'] if row['facility_id']==facility and row['pool']=='FBO' and row['nm_id'] in nms}
        require(set(lines)==set(nms) and sum(row['quantity'] for row in lines.values())==5400, 'original_5400_mass_required')
        for nm,line in lines.items():
            from decimal import Decimal
            fbo=conn.execute(f"SELECT * FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBO' AND nm_id=?",(facility,nm)).fetchone()
            fbs=conn.execute(f"SELECT * FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=?",(facility,nm)).fetchone()
            require(fbo and fbs and fbo['quantity']==line['quantity'] and Decimal(fbo['capital_rub'])==Decimal(line['capital_rub'])
                    and fbs['quantity']==0 and Decimal(fbs['capital_rub'])==0, 'unmodified_original_pool_boundary_required')
        guard=handoffs._capture_guard(conn,nms=[(facility,nm) for nm in nms],order_ids=[row['order']['order_id'] for row in evidence['orders']])
        from packages.business_time import default_business_as_of_date,current_business_date_iso
        active=conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()
        require(active,'active_ready_bundle_required')
        current_day=current_business_date_iso(now)
        target=conn.execute(f'''SELECT as_of_date FROM {ready.TABLE} WHERE bundle_version=?
            AND EXISTS(SELECT 1 FROM json_each(plan_json,'$.date_columns') WHERE value=?)
            ORDER BY (as_of_date=?) DESC,activated_at DESC,refreshed_at DESC,as_of_date DESC LIMIT 1''',
            (active[0],current_day,default_business_as_of_date(now))).fetchone()
        require(target is not None,'real_ready_current_column_required')
        expected=ready.capture_expected(conn,bundle_version=active[0],as_of_date=target[0])
        require(expected.exists,'real_current_ready_required')
        from packages.application.warehouse_functional import FUNCTIONAL_CUTOVER_ID
        source_versions={};retained_inputs={};initial_queries=[];day=datetime(2026,10,26,tzinfo=timezone.utc).date()
        from packages.application.fbs_accounting_historical_sources import archived_quantity_snapshot
        from packages.application.fbs_inventory_presentation import capture_retained_stages
        while day.isoformat()<=current_day:
            sql=f'''SELECT version_id FROM {P}warehouse_functional_versions
                WHERE status='good' AND cutover_id=? AND business_effective_date=?
                AND version_kind!='targeted_supplier_replay'
                ORDER BY effective_at DESC,created_at DESC,rowid DESC LIMIT 1'''
            args=(FUNCTIONAL_CUTOVER_ID,day.isoformat());initial_queries.append((sql,args))
            selected=conn.execute(sql,args).fetchone()
            require(selected is not None,'exact_pre_native_wb_day_missing')
            # A supplier correction's effective date marks its historical
            # impact; its copied current WB stock is not that day's source.
            # Authenticate the selected dated edition rather than silently
            # falling back from a corrupt ordinary snapshot.
            from packages.application.fbs_historical_fbo_stage_sources import _snapshot_quantity_authority
            dated_version=dict(conn.execute(f'SELECT * FROM {P}warehouse_functional_versions WHERE version_id=?',(selected[0],)).fetchone())
            _snapshot_quantity_authority(dated_version,
                dict(conn.execute(f'SELECT * FROM {P}warehouse_wb_snapshots WHERE version_id=?',(selected[0],)).fetchone()))
            if day.isoformat()=='2026-10-26':
                correction_since=json.loads(dated_version['source_watermarks_json'])['captured_at']
            source_versions[day.isoformat()]=selected[0]
            if day.isoformat()>='2026-10-26':
                frozen=(book['state']['periods'][day.isoformat()]['snapshot']
                    if day.isoformat()=='2026-10-26' and day.isoformat() in book.get('retained_days',{}) else None)
                stock=archived_quantity_snapshot(conn,day=day.isoformat(),observed_at=now.isoformat(),
                    **({'generation_id':frozen['id']} if frozen else {}))
                ids=sorted({int(row['nm_id']) for row in stock['rows']})
                retained_inputs[day.isoformat()]=capture_retained_stages(runtime.db_path,day=day.isoformat(),wb_version_id=selected[0],nm_ids=ids,connection=conn)
                if day.isoformat()=='2026-10-26' and day.isoformat() in book.get('retained_days',{}):
                    require(book['wb_days'][day.isoformat()]['version_id']==selected[0]
                        and book['state']['periods'][day.isoformat()]['snapshot']==stock,
                        'closed_receipt_day_frozen_source_changed')
                    retained_inputs[day.isoformat()]=deepcopy(book['retained_days'][day.isoformat()])
                initial_queries.append((f'SELECT * FROM {P}warehouse_business_projection_current_rows WHERE as_of_date=? ORDER BY nm_id',(day.isoformat(),)))
            day+=timedelta(days=1)
        sql=f'''SELECT version_id FROM {P}warehouse_functional_versions
            WHERE status='good' AND cutover_id=? AND version_kind='targeted_supplier_replay'
            AND julianday(created_at)>=julianday(?) AND business_effective_date<=?
            ORDER BY business_effective_date,effective_at,created_at,rowid'''
        args=(FUNCTIONAL_CUTOVER_ID,correction_since,current_day);initial_queries.append((sql,args))
        targeted_version_ids=[row[0] for row in conn.execute(sql,args)]
        require(len(targeted_version_ids)<=32,'targeted_correction_scope_limit')
        selector_fence=json.loads(canonical(ready.pin_queries(conn,initial_queries)))
    return dict(book_version=version,original=original,native_guard=guard,ready_expected=asdict(expected),freshness_policy=freshness_policy,
                documentary_digest=fingerprint(source),evidence_digest=evidence['evidence_digest'],facility_id=facility,
                nm_ids=nms,source_lines={str(k):v for k,v in lines.items()},source_versions=source_versions,
                retained_inputs=retained_inputs,targeted_version_ids=targeted_version_ids,
                source_capture_frontier=correction_since,selector_fence=selector_fence)


def _check_source_copy_budget(*,table,count,size,elapsed):
    exceeded=('rows' if count>MAX_ROWS else 'bytes' if size>MAX_BYTES else 'time' if elapsed>=120 else '')
    require(not exceeded,f'source_copy_budget:{exceeded}:table={table}:rows={count}:bytes={size}:seconds={elapsed:.3f}')


def _subset(conn,destination,*,book,now,snapshot):
    """Bounded coherent source copy; preserve rowids and global native sequences."""
    require(conn.in_transaction and conn.execute('PRAGMA query_only').fetchone()[0]==1,'query_only_copy_required')
    through=now.date().isoformat();dates=sorted(book['state']['periods'])
    versions={v['version_id'] for v in book.get('wb_days',{}).values()}
    versions.update(snapshot['source_versions'].values())
    versions.update(snapshot.get('targeted_version_ids',[]))
    versions.update(r[0] for r in conn.execute(f"SELECT version_id FROM {P}warehouse_functional_versions WHERE version_kind='functional_cutover'"))
    versions.update(r[0] for r in conn.execute(f'SELECT version_id FROM {P}warehouse_functional_active'))
    require(0<len(versions)<=1000,'source_version_budget')
    vm=','.join('?' for _ in versions);va=sorted(versions)
    # All current pools and all documents are necessary for the native global
    # FF parity and the accepted original book's complete document manifest.
    nms=snapshot['nm_ids'];marks=','.join('?' for _ in nms)
    selected_orders=f'SELECT order_id FROM {P}wb_supplies_fbs_order_observations WHERE nm_id IN ({marks})'
    # Inclusive Julian prefilter is conservative at sub-millisecond precision;
    # the foreign-source reader performs the exact aware timestamp comparison.
    late_sql=f'SELECT operation_id FROM {OPERATIONS_TABLE} WHERE (julianday(posted_at)>=julianday(?) OR julianday(posted_at) IS NULL) AND business_date<=? ORDER BY operation_id'
    late_args=(snapshot['source_capture_frontier'],max(snapshot['source_versions']))
    nativeops=f'SELECT operation_id FROM {LINES_TABLE} WHERE nm_id IN ({marks}) UNION SELECT operation_id FROM {docs.DOCUMENTS_TABLE} UNION SELECT operation_id FROM {OPERATIONS_TABLE} WHERE (julianday(posted_at)>=julianday(?) OR julianday(posted_at) IS NULL) AND business_date<=?'
    native_args=[*nms,*late_args]
    filters={LINES_TABLE:(f'operation_id IN ({nativeops})',native_args),OPERATIONS_TABLE:(f'operation_id IN ({nativeops})',native_args)}
    for table in ('wb_supplies_fbs_order_observations','wb_supplies_fbs_status_observations','wb_supplies_fbs_status_current','wb_supplies_fbs_status_transitions','wb_supplies_fbs_identity_evidence'):
        filters[P+table]=(f'order_id IN ({selected_orders})',nms)
    for table in ('ff_pool_fbs_lifecycle_current','ff_pool_fbs_lifecycle_events','ff_pool_fbs_applicability_events'):
        filters[P+table]=(f'nm_id IN ({marks})',nms)
    filters[lifecycle.RECONCILIATION_TABLE]=(f'event_id IN (SELECT event_id FROM {lifecycle.EVENTS_TABLE} WHERE nm_id IN ({marks}))',nms)
    for suffix in ('functional_versions','functional_balances','functional_read_models','wb_snapshots',
                   'functional_ff_reservations','supplier_cost_states','unmatched_doprinato'):
        filters[P+'warehouse_'+suffix]=(f'version_id IN ({vm})',va)
    targeted=snapshot.get('targeted_version_ids',[])
    if targeted:
        tm=','.join('?' for _ in targeted)
        publications=P+'warehouse_targeted_publications'
        undo=P+'warehouse_targeted_undo_manifests'
        filters[publications]=(f'version_id IN ({tm})',targeted)
        filters[undo]=(f'published_version_id IN ({tm})',targeted)
        shipments=f'SELECT shipment_id FROM {undo} WHERE published_version_id IN ({tm})'
        for suffix,key in (('supplier_shipments','shipment_id'),('supplier_shipment_lines','shipment_id'),
                           ('supplier_financial_documents','supplier_order_id'),
                           ('supplier_financial_expense_lines','supplier_order_id'),('cny_ledger_operations','source_order_id')):
            filters[P+suffix]=(f'{key} IN ({shipments})',targeted)
    # Only original selected WB generations by day; no current generation is
    # substituted for an older official observation during the rehearsal.
    gids={p['snapshot']['id'] for p in book['state']['periods'].values() if p.get('snapshot',{}).get('id')}
    if book['state']['baseline'].get('snapshot',{}).get('id'):gids.add(book['state']['baseline']['snapshot']['id'])
    from packages.application.fbs_accounting_historical_sources import archived_quantity_snapshot
    day=datetime(2026,10,27,tzinfo=timezone.utc).date()
    while day.isoformat()<=max(snapshot['source_versions']):
        stock=archived_quantity_snapshot(conn,day=day.isoformat(),observed_at=now.isoformat())
        gids.add(stock['id']);day+=timedelta(days=1)
    gm=','.join('?' for _ in gids);ga=sorted(gids)
    filters.update({P+'wb_fbs_warehouse_registry_runs':(f'run_id IN ({gm})',ga),P+'wb_fbs_warehouse_registry_rows':(f'run_id IN ({gm})',ga),
        P+'wb_fbs_stock_snapshot_runs':(f'registry_run_id IN ({gm})',ga),
        P+'wb_fbs_stock_snapshot_rows':(f'run_id IN (SELECT run_id FROM {P}wb_fbs_stock_snapshot_runs WHERE registry_run_id IN ({gm}))',ga)})
    extras=('ff_pool_operator_confirmations','ff_pool_feature_epochs','ff_pool_cutover_manifests','ff_pool_cutover_order_classifications','ff_pool_fbs_lifecycle_current',
        'ff_pool_fbs_lifecycle_events','ff_pool_fbs_reconciliation_lane','ff_pool_fbs_drain_state','ff_pool_fbs_late_evidence',
        'ff_pool_fbs_identity_pending','ff_pool_fbs_identity_pending_resolutions','ff_pool_fbs_mapping_extensions','ff_pool_fbs_mapping_extension_allocations',
        'ff_pool_fbs_forward_generations','ff_pool_fbs_forward_state','ff_pool_fbs_applicability_events','wb_supply_ff_origin_assignments',
        'wb_supplies_fbs_warehouse_facility_mappings','wb_supplies_fbs_identity_mappings','wb_supplies_fbs_identity_evidence',
        'wb_supplies_fbs_order_observations','wb_supplies_fbs_status_observations','wb_supplies_fbs_status_current','wb_supplies_fbs_status_transitions',
        'ff_stock_operations','ff_stock_operation_lines','ff_stock_reservation_operations','ff_stock_reservation_lines',
        'warehouse_opening_cost_map','warehouse_wb_sync_status','ready_publications','ready_input_revisions','ready_snapshots')
    from packages.application.fbs_accounting_rehearsal_sources import select_rehearsal_sources
    selected_sources=select_rehearsal_sources(conn,dates=sorted(snapshot['retained_inputs']),now=now,
        extra_ready_keys=[(snapshot['ready_expected']['bundle_version'],snapshot['ready_expected']['as_of_date'])])
    selected_sources['selection_queries'].append((late_sql,late_args))
    filters.update(selected_sources['filters'])
    allowed=set(staging.SOURCE_TABLES)|set(filters)|{P+x for x in extras}
    schema=list(conn.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"))
    require(not destination.exists() and shutil.disk_usage(destination.parent).free>=MAX_BYTES+shared.RESERVE,'copy_disk_budget')
    shared._admit(destination,MAX_BYTES)
    fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    queries=list(selected_sources['selection_queries']);copy_queries=[];counts={};size=0;started=time.monotonic()
    with closing(sqlite3.connect(destination)) as dst:
        for row in schema:
            if row['type']=='table':dst.execute(row['sql'])
        for row in schema:
            table=row['name']
            if row['type']!='table' or table not in allowed:continue
            where,args=filters.get(table,('1',()))
            sql=f'SELECT rowid __source_rowid__,* FROM "{table}" WHERE {where} ORDER BY rowid'
            columns=[r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            count=0
            for source in conn.execute(sql,args):
                count+=1;size+=sum(len(v) if isinstance(v,bytes) else len(canonical(v).encode()) for v in source)
                _check_source_copy_budget(table=table,count=count,size=size,elapsed=time.monotonic()-started)
                dst.execute(f'INSERT INTO "{table}"(rowid,{",".join(chr(34)+c+chr(34) for c in columns)}) VALUES({",".join("?" for _ in range(len(columns)+1))})',
                            [source['__source_rowid__'],*[source[c] for c in columns]])
            queries.append((sql,args));copy_queries.append((sql,args));counts[table]=count
        # AUTOINCREMENT is global even when a source table is scoped or empty.
        for sequence in conn.execute('SELECT name,seq FROM sqlite_sequence'):
            if sequence['name'] in allowed:
                dst.execute('DELETE FROM sqlite_sequence WHERE name=?',(sequence['name'],))
                dst.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)',tuple(sequence))
        for row in schema:
            if row['type'] in {'index','trigger','view'}:dst.execute(row['sql'])
        dst.commit();require(dst.execute('PRAGMA quick_check').fetchall()==[('ok',)],'source_copy_integrity')
    ready_where,ready_args=selected_sources['filters'][ready.TABLE]
    selection_queries=[*selected_sources['selection_queries'],
        (f'SELECT rowid __source_rowid__,* FROM "{ready.TABLE}" WHERE {ready_where} ORDER BY rowid',ready_args)]
    return dict(query_fence=json.loads(canonical(ready.pin_queries(conn,queries))),
        copy_fence=json.loads(canonical(ready.pin_queries(conn,copy_queries))),
        selection_fence=json.loads(canonical(ready.pin_queries(conn,selection_queries))),rows=counts,bytes=size)


def _ordinary_identity(*,operation_id,evidence,epoch,day,book_version):
    source=fingerprint(dict(operation_id=operation_id,evidence_digest=evidence['evidence_digest'],book_version=book_version,business_date=day))
    return DocumentIdentity(request_id='temporary-fbs:'+fingerprint([operation_id,day])[7:55],source_system='wb_core',
        source_type=EFFECT_CONTRACT,source_id=operation_id,source_revision=source,idempotency_epoch=epoch,actor='Кодекс',business_date=day)


def _accept_request(conn,*,identity,manifest,now):
    """Canonical native request admission on the caller's connection only."""
    docs._validate_identity(identity);docs._validate_manifest('pool_reallocation',manifest)
    key=docs._document_request_identity(identity,'pool_reallocation',manifest,'','')
    request_id='ffpdr_'+key[7:35];client=docs._client_request_id(identity.request_id)
    row=conn.execute(f'SELECT * FROM {docs.REQUESTS_TABLE} WHERE request_id=?',(request_id,)).fetchone()
    if row:
        require(row['request_identity']==key and json.loads(row['request_payload_json'])==manifest,'ordinary_request_conflict')
        return dict(row)
    conn.execute(f'''INSERT INTO {docs.REQUESTS_TABLE}(request_id,request_identity,client_request_id,document_kind,state,
        source_system,source_type,source_id,source_revision,idempotency_epoch,actor,business_date,request_payload_json,accepted_at,updated_at)
        VALUES(?,?,?,'pool_reallocation','accepted',?,?,?,?,?,?,?,?,?,?)''',
        (request_id,key,client,identity.source_system,identity.source_type,identity.source_id,identity.source_revision,
         identity.idempotency_epoch,identity.actor,identity.business_date,canonical(manifest),now,now))
    conn.execute(f'INSERT INTO {docs.ALIASES_TABLE}(client_request_id,request_id,request_identity,accepted_at) VALUES(?,?,?,?)',(client,request_id,key,now))
    return dict(conn.execute(f'SELECT * FROM {docs.REQUESTS_TABLE} WHERE request_id=?',(request_id,)).fetchone())


def _ordinary_plan(conn,request):
    payload=json.loads(request['request_payload_json'])
    plan=docs._build_posting_plan(conn,request=request,manifest=payload,epoch=request['idempotency_epoch'])
    docs._validate_physical_plan(conn,plan=plan,epoch=request['idempotency_epoch'])
    before=docs._balance_digest(conn,plan['balance_keys'])
    proof=fingerprint(dict(request_identity=request['request_identity'],epoch=request['idempotency_epoch'],before_digest=before,plan=plan))
    return dict(request=request,plan=plan,before_digest=before,plan_fingerprint=proof,
                before_images=docs._before_images(conn,request=request,plan=plan))


def _register_recovery(runtime,prepared):
    from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry,RecoveryState
    registry=WarehouseRecoveryRegistry(runtime_dir=runtime.runtime_dir,db_path=runtime.db_path)
    request,plan=prepared['request'],prepared['plan']
    operation=registry.prepare_t1(mutation_kind='ff_pool_document_posting',closure_kind='document',plan_fingerprint=prepared['plan_fingerprint'],
        scope=dict(request_id=request['request_id'],document_kind='pool_reallocation',balance_keys=[list(k) for k in plan['balance_keys']]),
        before_images=prepared['before_images'],source_digest=request['request_identity'],non_target_digest=prepared['before_digest'])
    require(operation['lifecycle'] in {RecoveryState.VERIFIED.value,RecoveryState.MUTATION_RUNNING.value,RecoveryState.RETAINED.value},'ordinary_recovery_not_ready')
    if operation['lifecycle']==RecoveryState.VERIFIED.value:
        operation=registry.begin_mutation(operation['operation_id'],expected_source_digest=request['request_identity'])
    return operation['operation_id']


def _append_ordinary(conn,prepared,*,recovery_id,now):
    request=conn.execute(f'SELECT * FROM {docs.REQUESTS_TABLE} WHERE request_id=?',(prepared['request']['request_id'],)).fetchone()
    require(request and request['state'] in {'accepted','ready'},'ordinary_request_not_unposted')
    fresh=_ordinary_plan(conn,dict(request))
    # Workflow state is not a monetary operand; the reviewed exact business
    # plan and its native pool before-image remain independently compared.
    require(canonical(fresh['plan'])==canonical(prepared['plan']) and fresh['before_digest']==prepared['before_digest'],'ordinary_plan_changed')
    conn.execute(f"UPDATE {docs.REQUESTS_TABLE} SET state='ready',preview_manifest_json=request_payload_json,ready_at=?,updated_at=? WHERE request_id=?",(now,now,request['request_id']))
    request=conn.execute(f'SELECT * FROM {docs.REQUESTS_TABLE} WHERE request_id=?',(request['request_id'],)).fetchone()
    docs._apply_plan(conn,request=request,plan=prepared['plan'],epoch=request['idempotency_epoch'],posted_at=now)
    conn.execute(f"UPDATE {docs.REQUESTS_TABLE} SET state='posted',posted_document_id=?,posted_manifest_sha256=?,recovery_operation_id=?,posted_at=?,updated_at=? WHERE request_id=? AND state='ready'",
        (prepared['plan']['primary_document_id'],fingerprint(prepared['plan']['posted_manifest']),recovery_id,now,now,request['request_id']))
    return prepared['plan']['primary_document_id']


def _unit_inputs(snapshot,evidence,*,operation_id,now):
    epoch=snapshot['original']['document']['idempotency_epoch'];facility=snapshot['facility_id']
    sent={nm:sum(row['order']['nm_id']==nm and 'dispatch' in row for row in evidence['orders']) for nm in snapshot['nm_ids']}
    result=[]
    for day,source,destination in (('2026-10-27','FBO','FBS'),('2026-10-29','FBS','FBO')):
        quantities={int(nm):line['quantity']-(sent[int(nm)] if source=='FBS' else 0) for nm,line in snapshot['source_lines'].items()}
        manifest=dict(facility_id=facility,source_pool=source,destination_pool=destination,
            items=[dict(nm_id=nm,quantity=q) for nm,q in sorted(quantities.items())],expenses=[])
        identity=_ordinary_identity(operation_id=operation_id,evidence=evidence,epoch=epoch,day=day,book_version=snapshot['book_version'])
        result.append(dict(identity=asdict(identity),manifest=manifest))
    return result


def _admit_requests(runtime,unit_inputs,*,now):
    # This reversible durable admission precedes native money. The existing
    # Ready parent is already saved on production; simulation owns its clone.
    with owned_sqlite.connection(runtime.db_path,phase_name='temporary_requests_connection') as conn:
        conn.row_factory=sqlite3.Row
        with owned_sqlite.transaction(conn,db_path=runtime.db_path,phase_name='temporary_requests'):
            return [_accept_request(conn,identity=DocumentIdentity(**value['identity']),manifest=value['manifest'],now=now) for value in unit_inputs]


def _verify_unit(conn,unit):
    existing=[conn.execute(f'SELECT * FROM {docs.DOCUMENTS_TABLE} WHERE document_id=?',(p['plan']['primary_document_id'],)).fetchone() for p in unit['ordinary']]
    if not any(existing):return False
    require(all(existing) and handoffs.verify_existing_handoffs(conn,unit['native_plan']),'partial_native_unit')
    for prepared,row in zip(unit['ordinary'],existing):
        expected=prepared['plan']['documents'][0]
        actual=handoffs._document(conn,row['document_id'])
        require(row['business_date']==prepared['request']['business_date'] and row['request_id']==prepared['request']['request_id']
                and row['operation_id']==expected['document_id'],'ordinary_document_identity_conflict')
        from packages.application.fbs_snapshot_cost_sources import _verified_manifest,_verify_typed_lines
        _verify_typed_lines(dict(row),_verified_manifest(dict(row)),actual['lines'],actual['movements'])
        require(json.loads(row['posted_manifest_json'])['domain']==prepared['plan']['domain_manifest'],'ordinary_document_domain_conflict')
    return True


def _build_candidate(runtime,*,book,version,native_plan,evidence,now,source_versions,retained_inputs,targeted_version_ids=()):
    with ready.readonly(runtime.db_path) as conn:
        captured=_repair().capture_temporary_allocation_repair(conn,runtime_dir=runtime.runtime_dir,book=book,book_version=version,
            native_plan=native_plan,evidence=evidence,now=now,source_versions=source_versions,retained_inputs=retained_inputs,
            targeted_version_ids=targeted_version_ids)
        candidate=_repair().build_temporary_allocation_repair(book,capture=captured,native_plan=native_plan)
    require(candidate['after_book']==fingerprint(candidate['candidate_book']) and candidate['candidate_book'].get('active') is True
        and not candidate['candidate_book'].get('publication_error'),'healthy_full_candidate_required')
    require({'2026-10-28','2026-10-29'}<=set(candidate['editions']) and candidate.get('quality_proof') and candidate.get('initial_anchors'),
            'actual_wb_28_29_replay_required')
    return captured,candidate


def _reconcile_sources(conn,native_plan,*,now):
    # Bounded native recovery of exactly the actual captured statuses. It owns
    # no global cursor and retains the cancelled-after-handoff return warning.
    return lifecycle.drain_post_checkpoint_fbs_lifecycle(conn,manifest=native_plan['manifest'],occurred_at=now,
        pinned_status_observation_sequences=tuple(row['status_sequence'] for row in native_plan['orders']),limit=33)


def _simulate_unit(runtime,*,snapshot,evidence,operation_id,now,owner_token):
    stamp=now.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    inputs=_unit_inputs(snapshot,evidence,operation_id=operation_id,now=stamp)
    requests=_admit_requests(runtime,inputs,now=stamp)
    # Plan every sequential native boundary on the private clone. The normal
    # document recovery records retain these exact stage-specific before-images.
    with warehouse_functional_write_lock(runtime.runtime_dir),accounting.writer_lock(runtime.runtime_dir):
        with owned_sqlite.connection(runtime.db_path,phase_name='temporary_simulation_connection') as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('BEGIN IMMEDIATE')
            try:
                first=_ordinary_plan(conn,requests[0]);doc27=_append_ordinary(conn,first,recovery_id='private-preflight-only',now=stamp)
                native_plan=handoffs.prepare_historical_handoffs(conn,runtime_dir=runtime.runtime_dir,evidence=evidence,
                    allocation_document_id=doc27,original_document_id=snapshot['original']['document']['document_id'],posted_at=stamp,
                    approval_reference=evidence['approval_reference'])
                handoffs.apply_historical_handoffs(conn,native_plan,runtime=runtime,owner_token=owner_token)
                second=_ordinary_plan(conn,requests[1]);_append_ordinary(conn,second,recovery_id='private-preflight-only',now=stamp)
                reconciliation=_reconcile_sources(conn,native_plan,now=stamp)
            finally:
                # Discover the two exact sequential T1 before-images first;
                # the subsequent real rehearsal registers native journals and
                # applies this same unit, never these planning placeholders.
                conn.rollback()
    return json.loads(canonical(dict(inputs=inputs,ordinary=[first,second],native_plan=native_plan,reconciliation=reconciliation)))


def _read_recipe(conn,binding):
    row=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
        (binding['operation_id'],binding['attempt_id'])).fetchone()
    require(row and row['kind']==EFFECT_CONTRACT and row['state'] in {'prepared','complete'},'durable_effect_recipe_required')
    recipe=json.loads(row['inputs_json'])
    require(recipe['recipe_digest']==binding['recipe_digest']==fingerprint({k:v for k,v in recipe.items() if k!='recipe_digest'})
        and recipe['expected_book']==row['expected_book'],'effect_recipe_conflict')
    expected=ready.ExpectedReady(**recipe['expected'])
    require(recipe['operation_id']==binding['operation_id']==row['operation_id']
        and recipe['attempt_id']==binding['attempt_id']==row['attempt_id']
        and row['bundle_version']==expected.bundle_version and row['as_of_date']==expected.as_of_date
        and row['expected_digest']==expected.fingerprint,'effect_recipe_identity_conflict')
    ready.check_pinned_authority(conn,recipe['authority'])
    return recipe


def _record_recipe(runtime,recipe,*,owner_token):
    require_warehouse_job_owner(runtime.runtime_dir,owner_token)
    binding={k:recipe[k] for k in ('operation_id','attempt_id','recipe_digest')}
    if recipe.get('simulation_only'):
        root=Path(runtime.runtime_dir)
        require(root.name=='simulation' and root.parent.name==recipe['operation_id']
                and 'temporary-fbs-allocation' in root.parent.parts,'simulation_recipe_cannot_admit_production')
    else:
        numeric=recipe.get('numeric') or {};candidate=numeric.get('candidate',{})
        require(recipe['snapshot'].get('source_versions') and recipe['snapshot'].get('retained_inputs')
                and recipe['snapshot'].get('selector_fence'),'pre_native_source_fences_required')
        require(candidate.get('candidate_book',{}).get('active') is True
                and not candidate['candidate_book'].get('publication_error') and candidate.get('initial_anchors')
                and {'2026-10-28','2026-10-29'}<=set(candidate.get('editions',{})), 'full_healthy_preflight_required_before_effects')
    with warehouse_functional_write_lock(runtime.runtime_dir),accounting.writer_lock(runtime.runtime_dir):
        with owned_sqlite.connection(runtime.db_path,phase_name='temporary_intent_connection') as conn:
            conn.row_factory=sqlite3.Row;ready.ensure_publication_schema(conn);conn.commit()
            with owned_sqlite.transaction(conn,db_path=runtime.db_path,phase_name='temporary_intent'):
                existing=conn.execute('SELECT 1 FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                    (recipe['operation_id'],recipe['attempt_id'])).fetchone()
                if not recipe.get('simulation_only') and not _verify_unit(conn,recipe['unit']):
                    require(bool(recipe.get('source_selection_fence')),'ready_selection_fence_required')
                    stages.check_query_fence(conn,recipe['source_selection_fence'])
                    require('foreign_source_fence' in candidate,'foreign_source_fence_required')
                    stages.check_query_fence(conn,candidate['foreign_source_fence'])
                    _validate_completion(runtime,authority=recipe.get('completion_authority',{}),snapshot=recipe['snapshot'],
                        numeric=recipe['numeric'],file_authority=recipe['file_authority'],code_authority=recipe['adapter_code'],recipe=recipe,conn=conn)
                if existing:
                    require(_read_recipe(conn,binding)==recipe,'saved_recipe_changed')
                    if not _verify_unit(conn,recipe['unit']):stages.check_query_fence(conn,recipe['snapshot'].get('selector_fence',[]))
                    return binding
                require(staging._file_authority(runtime.runtime_dir,runtime.db_path)==recipe['file_authority'],'intent_file_authority_changed')
                require(accounting.load(runtime.runtime_dir)[1]==recipe['expected_book'],'intent_book_changed')
                stages.check_query_fence(conn,recipe['snapshot'].get('selector_fence',[]))
                ready.check_expected(conn,ready.ExpectedReady(**recipe['expected']))
                guard=handoffs._capture_guard(conn,nms=[(recipe['snapshot']['facility_id'],nm) for nm in recipe['snapshot']['nm_ids']],
                    order_ids=[row['order']['order_id'] for row in recipe['evidence']['orders']])
                require(guard==recipe['snapshot']['native_guard'],'intent_native_source_changed')
                ready.record_intent(conn,operation_id=recipe['operation_id'],attempt_id=recipe['attempt_id'],kind=EFFECT_CONTRACT,
                    expected=ready.ExpectedReady(**recipe['expected']),inputs=recipe,expected_book=recipe['expected_book'],book_required=True,
                    ready_required=True,created_at=recipe['created_at'],book_operation_id='temporary-fbs-effects:'+recipe['recipe_digest'][7:])
    return binding


def _apply_native_unit(runtime,recipe,*,binding,owner_token,fault=None):
    require_warehouse_job_owner(runtime.runtime_dir,owner_token)
    unit=recipe['unit'];stamp=recipe['created_at']
    with ready.readonly(runtime.db_path) as conn:
        require(_read_recipe(conn,binding)==recipe,'native_recipe_changed')
        if _verify_unit(conn,unit):return False
    _admit_requests(runtime,unit['inputs'],now=stamp)
    recovery_ids=[_register_recovery(runtime,p) for p in unit['ordinary']]
    with warehouse_functional_write_lock(runtime.runtime_dir),accounting.writer_lock(runtime.runtime_dir):
        with owned_sqlite.connection(runtime.db_path,phase_name='temporary_native_connection') as conn:
            conn.row_factory=sqlite3.Row
            with owned_sqlite.transaction(conn,db_path=runtime.db_path,phase_name='temporary_native_unit'):
                require(_read_recipe(conn,binding)==recipe and _code()==recipe['adapter_code'],'native_recipe_or_code_changed')
                require(staging._file_authority(runtime.runtime_dir,runtime.db_path)==recipe['file_authority'],'native_file_authority_changed')
                if _verify_unit(conn,unit):return False
                if not recipe.get('simulation_only'):
                    require(bool(recipe.get('source_selection_fence')),'ready_selection_fence_required')
                    stages.check_query_fence(conn,recipe['source_selection_fence'])
                    candidate=recipe['numeric']['candidate']
                    require('foreign_source_fence' in candidate,'foreign_source_fence_required')
                    stages.check_query_fence(conn,candidate['foreign_source_fence'])
                require(accounting.load(runtime.runtime_dir)[1]==recipe['expected_book'],'native_book_changed')
                stages.check_query_fence(conn,recipe['snapshot'].get('selector_fence',[]))
                ready.check_expected(conn,ready.ExpectedReady(**recipe['expected']))
                snap=recipe['snapshot']
                guard=handoffs._capture_guard(conn,nms=[(snap['facility_id'],nm) for nm in snap['nm_ids']],
                    order_ids=[row['order']['order_id'] for row in recipe['evidence']['orders']])
                require(guard==snap['native_guard'],'native_prestate_changed')
                if not recipe.get('simulation_only'):
                    _validate_completion(runtime,authority=recipe.get('completion_authority',{}),snapshot=recipe['snapshot'],
                        numeric=recipe['numeric'],file_authority=recipe['file_authority'],code_authority=recipe['adapter_code'],recipe=recipe,conn=conn)
                _append_ordinary(conn,unit['ordinary'][0],recovery_id=recovery_ids[0],now=stamp)
                if fault:fault('after_allocation')
                fresh=handoffs.prepare_historical_handoffs(conn,runtime_dir=runtime.runtime_dir,evidence=recipe['evidence'],
                    allocation_document_id=unit['ordinary'][0]['plan']['primary_document_id'],
                    original_document_id=snap['original']['document']['document_id'],posted_at=stamp,
                    approval_reference=recipe['evidence']['approval_reference'])
                require(fresh==unit['native_plan'],'approved_handoff_plan_changed')
                handoffs.apply_historical_handoffs(conn,unit['native_plan'],runtime=runtime,owner_token=owner_token)
                if fault:fault('after_handoffs')
                _append_ordinary(conn,unit['ordinary'][1],recovery_id=recovery_ids[1],now=stamp)
                if fault:fault('after_return')
                require(_reconcile_sources(conn,unit['native_plan'],now=stamp)==unit['reconciliation'],'reconciliation_preflight_changed')
                require(_verify_unit(conn,unit),'native_whole_unit_readback_failed')
    _finalize_native_documents(runtime,unit)
    return True


def _finalize_native_documents(runtime,unit):
    # These are the native document service's own recovery retention and
    # document readback phases. The parent remains pending for publication.
    service=docs.FfPoolDocumentService(db_path=runtime.db_path,runtime_dir=runtime.runtime_dir,bootstrap=False,resume=False)
    for prepared in unit['ordinary']:
        value=service._finalize_posted(prepared['request']['request_id'])
        require(value['state']=='complete','ordinary_native_document_incomplete')


def prepare_publication(runtime,*,recipe,binding,now):
    book,version=accounting.load(runtime.runtime_dir)
    require(version==recipe['expected_book'],'publication_original_book_changed')
    authority=ready.capture_authority(runtime.runtime_dir);db=Path(authority['path'])
    with ready.readonly(db) as conn:
        require(_read_recipe(conn,binding)==recipe and _verify_unit(conn,recipe['unit']),'publication_native_unit_unproven')
    captured,candidate=_build_candidate(runtime,book=book,version=version,native_plan=recipe['unit']['native_plan'],evidence=recipe['evidence'],now=now,
        source_versions=recipe['snapshot']['source_versions'],retained_inputs=recipe['snapshot']['retained_inputs'],
        targeted_version_ids=recipe['snapshot'].get('targeted_version_ids',[]))
    dates=sorted(candidate['editions'])
    closed=candidate['quality_proof'].get('closed_prefix_repair')
    first='2026-10-26' if closed else '2026-10-27'
    require(dates and dates[0]==first,'complete_receipt_and_allocation_suffix_required')
    if closed:
        from packages.application.fbs_accounting_historical_revision import _prefix
        require(closed.get('contract')=='temporary_fbs_closed_receipts_20261026_v1'
            and closed.get('date')==first and len(closed.get('receipt_document_ids',[]))==2
            and len(set(closed['receipt_document_ids']))==2
            and closed.get('source_digest')==fingerprint({k:v for k,v in closed.items() if k!='source_digest'})
            and closed.get('immutable_prefix_digest')==fingerprint(_prefix(book,first))
            ==fingerprint(_prefix(candidate['candidate_book'],first)), 'closed_receipt_prefix_proof_changed')
    plan=dict(contract=CONTRACT,status='ready',before_book_digest=version,candidate_book=candidate['candidate_book'],
        scope=dict(dates=dates,date_from=dates[0],date_to=dates[-1]),observation=dict(captured_at=recipe['created_at']),
        effect_binding=binding,native_plan=recipe['unit']['native_plan'],quality_proof=candidate['quality_proof'])
    with ready.readonly(db) as conn:
        targets=publication.prepare_ready_targets(conn,plan=plan,candidate=candidate,runtime_dir=runtime.runtime_dir,authority=authority,now=now)
        material=ready.capture_material(conn,tables=staging.SOURCE_TABLES)
        confirmations={identity:dict(fingerprint=value) for identity,value in candidate.get('source_document_manifest',{}).items()}
        require(all(p['plan']['primary_document_id'] in confirmations for p in recipe['unit']['ordinary']),'ordinary_document_absorption_proof_required')
        parameters=publication._parameter_proof(conn,dates)
    result=dict(contract=CONTRACT,operation_id=recipe['publication_operation_id'],attempt_id=recipe['attempt_id'],owner_generation=recipe['owner_generation'],
        authority=authority,file_authority=staging._file_authority(runtime.runtime_dir,db),code_authority=publication._code(),adapter_code_authority=_code(),
        expected_book=version,after_book=candidate['after_book'],candidate=candidate,targets=targets,source_revisions=material,
        source_confirmation=confirmations,operator_confirmation={},plan=plan,original_capture=captured,
        original_source_fence=candidate['query_fence'],parameter_proof=parameters,
        book_operation_id='temporary-fbs-publish:'+fingerprint([recipe['operation_id'],recipe['attempt_id']])[7:],created_at=recipe['created_at'])
    publication._json_size(result,publication.publication_limits(CONTRACT)[1],'temporary_allocation_publication_size_limit')
    result['manifest_digest']=fingerprint(result)
    return result


def independent_rebuild(manifest,*,runtime_dir,recovering=False):
    require(manifest['adapter_code_authority']==_code(),'publication_adapter_code_changed')
    plan=manifest['plan'];book,version=accounting.load(runtime_dir,version=manifest['expected_book'])
    with ready.readonly(Path(manifest['authority']['path'])) as conn:
        recipe=_read_recipe(conn,plan['effect_binding'])
        require(version==recipe['expected_book'] and _verify_unit(conn,recipe['unit']),'publication_source_unit_changed')
        stages.check_query_fence(conn,manifest['original_source_fence'])
    now=datetime.fromisoformat(manifest['created_at'].replace('Z','+00:00'))
    if recovering:
        captured=manifest['original_capture']
        candidate=_repair().build_temporary_allocation_repair(book,capture=captured,native_plan=recipe['unit']['native_plan'])
    else:
        runtime=shared._runtime(Path(runtime_dir))
        captured,candidate=_build_candidate(runtime,book=book,version=version,native_plan=recipe['unit']['native_plan'],evidence=recipe['evidence'],now=now,
            source_versions=recipe['snapshot']['source_versions'],retained_inputs=recipe['snapshot']['retained_inputs'],
            targeted_version_ids=recipe['snapshot'].get('targeted_version_ids',[]))
    require(captured==manifest['original_capture'] and candidate==manifest['candidate'],'publication_independent_candidate_changed')
    with ready.readonly(Path(manifest['authority']['path'])) as conn:
        publication.verify_ready_targets(conn,manifest=manifest,candidate=candidate,runtime_dir=runtime_dir,now=now)


def publish_stages(conn,*,candidate):
    return _repair().publish_temporary_allocation_stages(conn,manifest=candidate)


def verify_stage_readback(conn,*,manifest):
    return _repair().verify_temporary_allocation_stage_readback(conn,candidate=manifest['candidate'])


def validate_source_receipt(conn,*,manifest):
    recipe=_read_recipe(conn,manifest['plan']['effect_binding'])
    require(_verify_unit(conn,recipe['unit']),'publication_native_source_receipt_changed')
    return dict(contract=CONTRACT,recipe_digest=recipe['recipe_digest'],native_plan_digest=recipe['unit']['native_plan']['plan_digest'])


def _history_ack_dates(scope,*,now):
    from packages.business_time import current_business_date_iso
    return {day for day in scope['dates'] if day<scope['date_to'] or current_business_date_iso(now)>scope['date_to']}


def finalize_scope(runtime,*,manifest,history_ack,finance,economics,now):
    from packages.application.business_data_heavy_admission import require_heavy_owner
    require(require_heavy_owner(runtime.runtime_dir).operation=='cycle','owned_cycle_required')
    require_warehouse_job_owner(runtime.runtime_dir)
    publication._verify(manifest)
    book,version=accounting.load(runtime.runtime_dir)
    require(version==manifest['after_book'] and book==manifest['candidate']['candidate_book'] and not book.get('publication_error'),'healthy_published_book_required')
    # Match HistoricalReceipt.freeze_scope without admitting a later book or
    # changed Ready. Successful completion after either changes is not implied.
    closed_dates=_history_ack_dates(manifest['plan']['scope'],now=now)
    require(history_ack and history_ack.get('manifest_digest')==manifest['manifest_digest']
        and set(history_ack.get('native',{}))==closed_dates,'exact_history_ack_required')
    require(finance.get('status') in {'applied','already_current'} and finance.get('non_target_preserved') is True
        and finance.get('post_verify_stale_week_count')==0 and finance.get('accounting_version_unchanged') is True,'finance_completion_unproven')
    require(economics and economics==accounting.current_publication_receipt(runtime,now=now)
            and economics.get('accounting_version')==version,'economics_completion_unproven')
    require(book.get('historical_revision',{}).get('quality_proof')==manifest['candidate']['quality_proof'],'published_quality_proof_changed')
    publication.publication_receipt(manifest,runtime_dir=runtime.runtime_dir)
    with warehouse_functional_write_lock(runtime.runtime_dir),accounting.writer_lock(runtime.runtime_dir):
        with owned_sqlite.connection(runtime.db_path,phase_name='temporary_completion_connection') as conn:
            conn.row_factory=sqlite3.Row
            with owned_sqlite.transaction(conn,db_path=runtime.db_path,phase_name='temporary_completion'):
                proof=validate_source_receipt(conn,manifest=manifest)
                row=conn.execute('SELECT kind,inputs_json,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=? AND state=\'complete\'',
                    (manifest['operation_id'],manifest['attempt_id'])).fetchone()
                require(row and row['kind']==CONTRACT
                    and publication.decode_manifest(row['inputs_json'],kind=row['kind'])==manifest,'completed_publication_changed')
                diagnostics=json.loads(row['diagnostics_json']);require(diagnostics.get('historical_history_ack')==history_ack,'stored_history_ack_changed')
                diagnostics.update(temporary_allocation_scope_complete=True,temporary_allocation_finance=finance,
                    temporary_allocation_economics=economics,temporary_allocation_quality_proof=manifest['candidate']['quality_proof'])
                conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=?',
                    (canonical(diagnostics),manifest['operation_id'],manifest['attempt_id']))
                binding=manifest['plan']['effect_binding'];recipe=_read_recipe(conn,binding)
                completion=dict(child_operation_id=manifest['operation_id'],child_attempt_id=manifest['attempt_id'],
                    manifest_digest=manifest['manifest_digest'],history_ack=history_ack,finance=finance,economics=economics,
                    quality_proof=manifest['candidate']['quality_proof'],**proof)
                ready.complete_publication(conn,operation_id=binding['operation_id'],attempt_id=binding['attempt_id'],book_version=version,
                    after_digest=ready.digest(manifest['targets'][-1]['after_json']),finished_at=now.isoformat())
                conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=?',
                    (canonical(dict(temporary_allocation_completion=completion)),binding['operation_id'],binding['attempt_id']))
    return dict(status='scope_complete',operation_id=manifest['operation_id'],book_version=version,
                quality_proof=manifest['candidate']['quality_proof'],operator_receipts_completed=False)


def _make_recipe(runtime,*,snapshot,unit,evidence,operation_id,created_at,numeric,source_selection_fence=(),completion_authority=None):
    recipe=dict(contract=EFFECT_CONTRACT,operation_id=operation_id,attempt_id='1',owner_generation='temporary-fbs:'+operation_id,
        authority=ready.capture_authority(runtime.runtime_dir),file_authority=staging._file_authority(runtime.runtime_dir,runtime.db_path),
        adapter_code=_code(),expected_book=snapshot['book_version'],expected=snapshot['ready_expected'],snapshot=snapshot,
        unit=unit,evidence=evidence,created_at=created_at,publication_operation_id=operation_id+':publication',numeric=numeric,
        simulation_only=numeric is None,source_selection_fence=source_selection_fence)
    if completion_authority is not None:recipe['completion_authority']=completion_authority
    recipe=json.loads(canonical(recipe));recipe['recipe_digest']=fingerprint(recipe)
    publication._json_size(recipe,publication.MAX_BYTES,'temporary_allocation_effect_recipe_size_limit')
    return recipe


def _numeric(manifest):
    # Runtime filesystem identities belong to CAS, not numerical comparison.
    return dict(after_book=manifest['after_book'],candidate=manifest['candidate'],
        source_confirmation=manifest['source_confirmation'],ready_targets=[dict(dates=t['dates'],after_digest=ready.digest(t['after_json'])) for t in manifest['targets']])


def _pipeline(runtime,*,recipe,binding,owner_token,now,fault=None):
    _apply_native_unit(runtime,recipe,binding=binding,owner_token=owner_token,fault=fault)
    _finalize_native_documents(runtime,recipe['unit'])
    # A crash after book activation resumes the immutable stored child; it
    # never re-plans from a current pool that already contains all effects.
    with ready.readonly(runtime.db_path) as conn:
        child=conn.execute('SELECT kind,inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
            (recipe['publication_operation_id'],recipe['attempt_id'])).fetchone()
    require(not child or child['kind']==CONTRACT,'publication_kind_changed')
    manifest=publication.decode_manifest(child['inputs_json'],kind=child['kind']) if child else prepare_publication(runtime,recipe=recipe,binding=binding,now=now)
    require(_numeric(manifest)==recipe['numeric'],'approved_full_preflight_changed')
    publication.publish(manifest,runtime_dir=runtime.runtime_dir)
    return manifest


def _utc_now():
    return datetime.now(timezone.utc)


def _completion_deadline(authority, *, now):
    require(authority.get('contract')==COMPLETION_CONTRACT,'completion_authority_required')
    observed=datetime.fromisoformat(authority['observed_at'])
    completed=datetime.fromisoformat(authority['completed_at'])
    require(observed.utcoffset() is not None and completed.utcoffset() is not None
        and 0<=(completed-observed).total_seconds()<=MAX_PREFLIGHT_BUILD_SECONDS,'preflight_build_expired')
    require(0<=(now-completed).total_seconds()<=MAX_COMPLETION_AGE_SECONDS,'preflight_expired')
    from packages.business_time import current_business_date_iso
    require(current_business_date_iso(now)==current_business_date_iso(observed),'preflight_business_day_changed')


def _official_completion_stock(conn, *, snapshot, numeric, now):
    from packages.application.fbs_current_snapshot_policy import resolve_current_snapshot_policy
    from packages.application.official_fbs_stock_read import read_complete_official_fbs_stock
    from packages.business_time import current_business_date_iso
    day=max(snapshot['source_versions'])
    require(current_business_date_iso(now)==day,'preflight_business_day_changed')
    require(asdict(resolve_current_snapshot_policy(connection=conn))==snapshot['freshness_policy'],
        'completion_freshness_policy_changed')
    stock=read_complete_official_fbs_stock(conn,universe=None,day=day,now=now)
    actual=dict(date=stock['date'],captured_at=stock['captured_at'],id=stock['generation_id'],
        digest=stock['generation_digest'],complete=True,
        rows=[dict(nm_id=int(nm),facility_id=str(facility),quantity=int(quantity))
            for nm,sku in sorted(stock['skus'].items()) for facility,quantity in sorted(sku['facilities'].items())],
        source=stock['source'],facility_evidence=stock['facility_evidence'])
    expected=numeric['candidate']['candidate_book']['state']['periods'][day]['snapshot']
    require(actual==expected,'completion_official_generation_changed')
    return stock


def _completion_stock_clock(stock, *, now):
    from packages.application.wb_fbs_warehouse_registry import _freshness
    from packages.business_time import current_business_date_iso
    require(current_business_date_iso(now)==stock['date'],'preflight_business_day_changed')
    require(all(_freshness(value['captured_at'],now.isoformat(),
        max_age_seconds=stock['freshness_max_age_seconds'])=='fresh'
        for value in stock['facility_evidence'].values()),'completion_official_source_expired')


def _normalized_completion_fence(conn, *, authority, recipe=None):
    """Undo only verified own intent/request INSERTs in the source comparison."""
    expected=authority['full_query_fence'];normal={}
    for table in ('ready_publications','ff_pool_document_requests','ready_input_revisions'):
        normal[P+table]=f'SELECT rowid __source_rowid__,* FROM "{P+table}" WHERE 1 ORDER BY rowid'
    # Prove the precise revision increment instead of exempting revision rows.
    trigger=conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='historical_stage_request_insert'").fetchone()
    require(trigger is not None and trigger[0]==authority['request_insert_trigger'],'completion_request_trigger_changed')
    requests={p['request']['request_id']:p['request'] for p in (recipe['unit']['ordinary'] if recipe else [])}
    inserted=0
    for identity,wanted in requests.items():
        row=conn.execute(f'SELECT * FROM {docs.REQUESTS_TABLE} WHERE request_id=?',(identity,)).fetchone()
        if row is not None:
            require(dict(row)==wanted,'completion_owned_request_changed');inserted+=1
    require(inserted in (0,2),'completion_owned_request_partial')
    own_parent=False
    if recipe:
        row=conn.execute(f'SELECT * FROM {P}ready_publications WHERE operation_id=? AND attempt_id=?',
            (recipe['operation_id'],recipe['attempt_id'])).fetchone()
        if row:
            expected_ready=ready.ExpectedReady(**recipe['expected'])
            exact=dict(operation_id=recipe['operation_id'],attempt_id=recipe['attempt_id'],kind=EFFECT_CONTRACT,
                bundle_version=expected_ready.bundle_version,as_of_date=expected_ready.as_of_date,
                expected_digest=expected_ready.fingerprint,inputs_json=canonical(recipe),expected_book=recipe['expected_book'],
                book_operation_id='temporary-fbs-effects:'+recipe['recipe_digest'][7:],book_required=1,ready_required=1,
                state='prepared',book_version=None,after_digest=None,created_at=recipe['created_at'],finished_at=None,diagnostics_json='{}')
            require(dict(row)==exact,'completion_owned_recipe_changed')
            require(_read_recipe(conn,{k:recipe[k] for k in ('operation_id','attempt_id','recipe_digest')})==recipe,
                'completion_owned_recipe_changed')
            own_parent=True
    for sql,args,digest in expected:
        if sql not in normal.values():
            stages.check_query_fence(conn,[(sql,args,digest)]);continue
        hashed=hashlib.sha256()
        for raw in conn.execute(sql,args):
            row=dict(raw)
            if sql==normal[P+'ready_publications'] and own_parent and row['operation_id']==recipe['operation_id'] and row['attempt_id']==recipe['attempt_id']:
                continue
            if sql==normal[docs.REQUESTS_TABLE] and row['request_id'] in requests:
                continue
            if sql==normal[ready.REVISIONS] and row['source_table']==docs.REQUESTS_TABLE:
                row['revision']-=inserted
            hashed.update(canonical(list(row.values())).encode());hashed.update(b'\n')
        require('sha256:'+hashed.hexdigest()==digest,'completion_full_source_changed')


def _completion_evidence(authority, *, snapshot):
    source,evidence=_inputs(authority['documentary_request'])
    require(fingerprint(source)==snapshot['documentary_digest'] and evidence['evidence_digest']==snapshot['evidence_digest'],
        'completion_documentary_source_changed')


def _validate_completion(runtime, *, authority, snapshot, numeric, file_authority, code_authority, recipe=None, conn=None):
    _completion_deadline(authority,now=_utc_now())
    require(code_authority==_code(),'preflight_code_changed')
    _completion_evidence(authority,snapshot=snapshot)
    require(staging._file_authority(runtime.runtime_dir,runtime.db_path)==file_authority,'preflight_file_replaced')
    require(accounting.load(runtime.runtime_dir)[1]==snapshot['book_version'],'completion_book_changed')
    if conn is None:
        with ready.readonly(runtime.db_path) as source:
            _normalized_completion_fence(source,authority=authority,recipe=recipe)
            stock=_official_completion_stock(source,snapshot=snapshot,numeric=numeric,now=_utc_now())
    else:
        _normalized_completion_fence(conn,authority=authority,recipe=recipe)
        # A new proven RO connection supplies canonical opened-file authority;
        # the writer connection itself must never widen freshness admission.
        with ready.readonly(runtime.db_path) as source:
            stock=_official_completion_stock(source,snapshot=snapshot,numeric=numeric,now=_utc_now())
    checked=_utc_now()
    _completion_stock_clock(stock,now=checked)
    _completion_deadline(authority,now=checked)


def _complete_preflight(runtime, *, result, inputs, operation_id, request):
    observed=datetime.fromisoformat(result['observed_at'])
    started=_utc_now()
    require(0<=(started-observed).total_seconds()<=MAX_PREFLIGHT_BUILD_SECONDS,'preflight_build_expired')
    require(_snapshot(runtime,inputs,now=observed)==result['snapshot'],'preflight_source_changed')
    require(result['code_authority']==_code(),'preflight_code_changed')
    require(staging._file_authority(runtime.runtime_dir,runtime.db_path)==result['file_authority'],'preflight_file_replaced')
    with ready.readonly(runtime.db_path) as source:
        stages.check_query_fence(source,result['subset']['query_fence'])
        ids=[value['request']['request_id'] for value in result['unit']['ordinary']]
        require(len(set(ids))==2 and not any(source.execute(f'SELECT 1 FROM {docs.REQUESTS_TABLE} WHERE request_id=?',(identity,)).fetchone() for identity in ids),
            'completion_owned_request_preexists')
        require(source.execute(f'SELECT 1 FROM {P}ready_publications WHERE operation_id=?',(operation_id,)).fetchone() is None,
            'completion_owned_recipe_preexists')
        staging._schema_ready(source)
        trigger=source.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='historical_stage_request_insert'").fetchone()[0]
        stock=_official_completion_stock(source,snapshot=result['snapshot'],numeric=result['numeric'],now=_utc_now())
    completed=_utc_now()
    _completion_stock_clock(stock,now=completed)
    authority=dict(contract=COMPLETION_CONTRACT,observed_at=result['observed_at'],completed_at=completed.isoformat(),
        full_query_fence=result['subset']['query_fence'],request_insert_trigger=trigger,
        documentary_request={**{key:request[key] for key in ('evidence_manifest_path','evidence_manifest_sha256')},
            'retained_files':result['retained_files']})
    _completion_evidence(authority,snapshot=result['snapshot'])
    # Evidence readback belongs to completion, rather than consuming its age.
    completed=_utc_now();authority['completed_at']=completed.isoformat()
    _completion_stock_clock(stock,now=completed)
    _completion_deadline(authority,now=completed)
    return authority


def _preflight(runtime,*,request,operation_id,directory,snapshot,inputs,now):
    code_authority=_code()
    require(not directory.exists(),'preflight_exists')
    directory.mkdir(parents=True,mode=0o700);retained=_retain(directory,request,inputs)
    simroot=directory/'simulation';simroot.mkdir(mode=0o700)
    original_book=shared._backup(accounting.path(runtime.runtime_dir),directory/'accounting.before.sqlite3',budget=128*1024**2)
    shared._backup(Path(original_book['path']),accounting.path(simroot),budget=128*1024**2)
    book,_=accounting.load(runtime.runtime_dir)
    with ready.readonly(runtime.db_path) as conn:
        subset=_subset(conn,directory/'sources.before.sqlite3',book=book,now=now,snapshot=snapshot)
    shared._backup(directory/'sources.before.sqlite3',simroot/'registry_upload_runtime.sqlite3',budget=MAX_BYTES)
    sim=shared._runtime(simroot)
    from packages.application.fbs_current_snapshot_policy import rehearsal_snapshot_policy
    with ready.readonly(runtime.db_path) as source, rehearsal_snapshot_policy(source,
            runtime_dir=simroot,db_path=sim.db_path,source_fence=subset['query_fence'],copy_fence=subset['copy_fence']):
        require(_snapshot(sim,inputs,now=now)==snapshot,'private_source_copy_changed')
        with warehouse_functional_job_lock(simroot) as owner:
            unit=_simulate_unit(sim,snapshot=snapshot,evidence=inputs[1],operation_id=operation_id,now=now,owner_token=owner['owner_token'])
            stamp=now.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
            # First build the complete real archived-WB candidate after the actual
            # native unit. A provisional recipe is not admitted to production.
            recipe=_make_recipe(sim,snapshot=snapshot,unit=unit,evidence=inputs[1],operation_id=operation_id,created_at=stamp,numeric=None)
            binding=_record_recipe(sim,recipe,owner_token=owner['owner_token'])
            _apply_native_unit(sim,recipe,binding=binding,owner_token=owner['owner_token'])
            manifest=prepare_publication(sim,recipe=recipe,binding=binding,now=now)
            publication.publish(manifest,runtime_dir=simroot)
    require(code_authority==_code(),'preflight_code_changed')
    result=dict(code_authority=code_authority,snapshot=snapshot,unit=unit,numeric=_numeric(manifest),subset=subset,observed_at=now.isoformat(),retained_files=retained,
        file_authority=staging._file_authority(runtime.runtime_dir,runtime.db_path),
        backup_book=original_book,backup_sources=dict(path=str(directory/'sources.before.sqlite3'),scope='bounded exact native/book/Ready operands; not a full database restore'),
        documentary_digest=fingerprint(inputs[0]))
    result['completion_authority']=_complete_preflight(runtime,result=result,inputs=inputs,operation_id=operation_id,request=request)
    result['digest']=fingerprint(result);shared._private(directory/'preflight.json',result)
    return result


class TemporaryAllocationAction:
    def preview(self,request,operation_id,*,root):
        root=Path(root);runtime=shared._runtime(root)
        if request['action']==RECOVER:
            require(operation_id!=request['recovery_ref'],'new_continuation_identity_required')
            saved=shared._read_json(_directory(root,request['recovery_ref'])/'attempt.json')
            require(saved['recipe']['recipe_digest']==request['original_recipe_digest'],'recovery_recipe_changed')
            cached=shared._read_json(_directory(root,request['recovery_ref'])/'preflight.json')
            require(cached.get('code_authority')==_code(),'preflight_code_changed')
            status=self.readback(saved['request'],request['recovery_ref'],root=root)
            prepared=dict(original=request['recovery_ref'],recipe_digest=request['original_recipe_digest'],status=status)
            return dict(operation_id=operation_id,target=str(root),scope=dict(action=RECOVER,original=request['recovery_ref']),
                prestate_sha256=fingerprint(status),candidate_sha256=fingerprint(prepared),prepared=prepared,
                recovery=dict(kind='same_immutable_native_recipe',original=request['recovery_ref']))
        require(request['action']==RESTORE,'action_invalid')
        inputs=_inputs(request);directory=_directory(root,operation_id)
        with shared._held(root,request) as held:
            if (directory/'preflight.json').exists():
                preflight=shared._read_json(directory/'preflight.json');now=datetime.fromisoformat(preflight['observed_at'])
                require(preflight['digest']==fingerprint({k:v for k,v in preflight.items() if k!='digest'}),'preflight_corrupt')
                require(preflight.get('code_authority')==_code(),'preflight_code_changed')
            else:
                now=datetime.now(timezone.utc)
                snapshot=_snapshot(runtime,inputs,now=now)
                preflight=_preflight(runtime,request=request,operation_id=operation_id,directory=directory,snapshot=snapshot,inputs=inputs,now=now)
            _validate_completion(runtime,authority=preflight['completion_authority'],snapshot=preflight['snapshot'],
                numeric=preflight['numeric'],file_authority=preflight['file_authority'],code_authority=preflight['code_authority'])
            require(_snapshot(runtime,inputs,now=now)==preflight['snapshot'],'preflight_source_changed')
            with ready.readonly(runtime.db_path) as conn:stages.check_query_fence(conn,preflight['subset']['query_fence'])
        return dict(operation_id=operation_id,target=str(runtime.db_path),scope=dict(action=RESTORE,physical_handoffs=32,
            temporary_allocation=5400,returned_to_fbo=5368,full_wb_28_29_preflight=True),prestate_sha256=fingerprint(preflight['snapshot']),
            candidate_sha256=preflight['digest'],prepared=preflight,recovery=dict(kind='native_T1_and_existing_Ready_intent',path=str(directory),held=held))

    def apply(self,request,operation_id,preview,*,root):
        root=Path(root);runtime=shared._runtime(root);continuation=request['action']==RECOVER
        fresh=self.preview(request,operation_id,root=root)
        require(all(fresh[k]==preview[k] for k in ('prestate_sha256','candidate_sha256')),'preview_compare_failed')
        original=request['recovery_ref'] if continuation else operation_id;directory=_directory(root,original)
        with shared._held(root,request),warehouse_functional_job_lock(root) as owner:
            cached=shared._read_json(directory/'preflight.json')
            require(cached.get('code_authority')==_code(),'preflight_code_changed')
            if continuation:
                saved=shared._read_json(directory/'attempt.json');recipe=saved['recipe']
                require(recipe['recipe_digest']==request['original_recipe_digest'],'continuation_recipe_changed')
                invocation=_directory(root,operation_id);invocation.mkdir(mode=0o700,parents=True,exist_ok=False)
                shared._private(invocation/'continuation.json',dict(original=original,recipe_digest=recipe['recipe_digest']))
            else:
                preflight=shared._read_json(directory/'preflight.json');now=datetime.fromisoformat(preflight['observed_at'])
                inputs=_inputs(request)
                require(preflight['digest']==preview['candidate_sha256'] and _snapshot(runtime,inputs,now=now)==preflight['snapshot'],'approved_prestate_changed')
                with ready.readonly(runtime.db_path) as conn:stages.check_query_fence(conn,preflight['subset']['query_fence'])
                recipe=_make_recipe(runtime,snapshot=preflight['snapshot'],unit=preflight['unit'],evidence=inputs[1],operation_id=operation_id,
                    created_at=preflight['unit']['native_plan']['posted_at'],numeric=preflight['numeric'],
                    source_selection_fence=preflight['subset']['selection_fence'],completion_authority=preflight['completion_authority'])
                saved=dict(request={**deepcopy(request),'retained_files':preflight['retained_files']},recipe=recipe,
                    binding={k:recipe[k] for k in ('operation_id','attempt_id','recipe_digest')},backup=dict(book=preflight['backup_book'],sources=preflight['backup_sources']))
                shared._private(directory/'attempt.json',saved)
            try:
                binding=_record_recipe(runtime,recipe,owner_token=owner['owner_token'])
                _pipeline(runtime,recipe=recipe,binding=binding,owner_token=owner['owner_token'],now=datetime.fromisoformat(recipe['created_at'].replace('Z','+00:00')))
            except Exception:
                raise AmbiguousSubmit('temporary_allocation_same_recipe_readback_required') from None
        raise AmbiguousSubmit('temporary_allocation_Ready_published_pending_owned_History_Finance_cycle')

    def readback(self,request,operation_id,*,root):
        root=Path(root);continuation=request['action']==RECOVER
        if continuation:
            linkpath=_directory(root,operation_id)/'continuation.json'
            if not linkpath.exists():return dict(operation_id=operation_id,state='not_submitted')
            require(shared._read_json(linkpath)==dict(original=request['recovery_ref'],recipe_digest=request['original_recipe_digest']),'continuation_identity_conflict')
        original=request['recovery_ref'] if continuation else operation_id
        directory=_directory(root,original)
        if not (directory/'attempt.json').exists():return dict(operation_id=operation_id,state='not_submitted')
        saved=shared._read_json(directory/'attempt.json');recipe=saved['recipe']
        try:
            db=StoreRegistry(root).resolve('operational')
            with ready.readonly(db) as conn:
                durable=_read_recipe(conn,saved['binding']);require(durable==recipe,'readback_recipe_changed')
                native=_verify_unit(conn,recipe['unit'])
                parent=conn.execute('SELECT state,book_version,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                    (recipe['operation_id'],recipe['attempt_id'])).fetchone()
                child=conn.execute('SELECT state FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                    (recipe['publication_operation_id'],recipe['attempt_id'])).fetchone()
                complete=parent['state']=='complete'
                if complete:
                    proof=json.loads(parent['diagnostics_json']).get('temporary_allocation_completion',{})
                    require(native and child and child['state']=='complete' and proof.get('history_ack') and proof.get('finance') and proof.get('economics'),
                            'final_completion_proof_missing')
            return dict(operation_id=operation_id,state='applied' if complete else 'ambiguous',original_operation_id=original,
                native_complete=native,ready_complete=bool(child and child['state']=='complete'),scope_complete=complete,
                recipe_digest=recipe['recipe_digest'],book_version=parent['book_version'],recovery_ref=original,
                next_step='none' if complete else 'owned_cycle_or_explicit_same_recipe_recovery')
        except (ValueError,sqlite3.Error,KeyError):
            return dict(operation_id=operation_id,state='ambiguous',original_operation_id=original,scope_complete=False,
                recipe_digest=recipe['recipe_digest'],next_step='same_recipe_readonly_diagnosis_no_new_effect_identity')
