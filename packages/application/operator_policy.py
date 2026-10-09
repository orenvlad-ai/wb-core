"""Immutable final policy commands; existing warehouse owner applies native sources.

Acceptance is distinct from a native version and exact derived publication.
"""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,re,sqlite3

TABLE='sheet_vitrina_v1_operator_policy_commands'
BINDINGS='sheet_vitrina_v1_operator_policy_bindings'
KINDS={'legacy_proxy':'Параметры Proxy','proxy_v4_tax':'Налог Proxy V4','wb_incident_policy':'Политика инцидентов WB'}
PATH='/v1/sheet-vitrina-v1/settings/policy-operations/'
NATIVE={'legacy_proxy':'sheet_vitrina_v1_calculation_parameter_versions','proxy_v4_tax':'sheet_vitrina_v1_proxy_v4_parameter_versions','wb_incident_policy':'sheet_vitrina_v1_wb_incident_policy_revisions'}

def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value):return 'sha256:'+hashlib.sha256(canonical(value).encode()).hexdigest()
def readonly(path):
    conn=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row;conn.execute('PRAGMA query_only=ON');return conn

def exists(conn,table=TABLE):return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())
def ensure_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE}(operation_id TEXT PRIMARY KEY,kind TEXT NOT NULL,actor TEXT NOT NULL,
        request_digest TEXT NOT NULL,accepted_at TEXT NOT NULL,effective_date TEXT NOT NULL,source_json TEXT NOT NULL,
        source_digest TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',reason_code TEXT NOT NULL DEFAULT '',progress_json TEXT NOT NULL DEFAULT '{{}}')''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {BINDINGS}(operation_id TEXT NOT NULL,attempt INTEGER NOT NULL,role TEXT NOT NULL,
        native_json TEXT NOT NULL,native_digest TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(operation_id,attempt,role))''')
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS operator_policy_source_immutable BEFORE UPDATE OF operation_id,kind,actor,
        request_digest,accepted_at,effective_date,source_json,source_digest ON {TABLE}
        BEGIN SELECT RAISE(ABORT,'operator policy source is immutable');END''')
    for table in (TABLE,BINDINGS):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT,'operator policy receipts are append-only');END''')
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS operator_policy_binding_immutable BEFORE UPDATE ON {BINDINGS}
        BEGIN SELECT RAISE(ABORT,'operator policy binding is immutable');END''')
    conn.execute(f'CREATE INDEX IF NOT EXISTS operator_policy_pending ON {TABLE}(state,accepted_at,operation_id)')
    conn.execute(f'CREATE INDEX IF NOT EXISTS operator_policy_actor_time ON {TABLE}(actor,accepted_at,operation_id)')

@dataclass(frozen=True)
class NativeCommand:
    operation_id:str
    attempt:int
    source:dict
    kind:str
    actor:str
    role:str="source"

def current_native(conn,kind,effective_date,*,seller_id=None):
    table=NATIVE[kind]
    if not exists(conn,table):return None
    if kind=='wb_incident_policy':
        row=conn.execute(f'SELECT * FROM {table} WHERE seller_id=? ORDER BY revision DESC LIMIT 1',(seller_id,)).fetchone()
    else:
        row=conn.execute(f'SELECT * FROM {table} WHERE effective_date<=? ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1',(effective_date,)).fetchone()
    return dict(row) if row else None

def legacy_configs(conn):
    table='sheet_vitrina_v1_user_configs'
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE config_key='wb_warehouse_exclusions' ORDER BY user_key")] if exists(conn,table) else []

def binding(conn,operation_id,role='source',attempt=None):
    if not exists(conn,BINDINGS):return None
    where='operation_id=? AND role=?';args=[operation_id,role]
    if attempt is not None:where+=' AND attempt=?';args.append(attempt)
    row=conn.execute(f'SELECT * FROM {BINDINGS} WHERE {where} ORDER BY attempt DESC LIMIT 1',args).fetchone()
    return json.loads(row['native_json']) if row else None

def before_native(conn,command):
    row=conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=?',(command.operation_id,)).fetchone()
    if not row or row['source_digest']!=digest(command.source) or row['kind']!=command.kind or row['actor']!=command.actor:
        raise ValueError('operator_policy_command_changed')
    if binding(conn,command.operation_id,command.role,command.attempt):raise ValueError('operator_policy_native_already_bound')
    expected=command.source['before']
    if command.role=='recovery':
        applied=binding(conn,command.operation_id,attempt=command.attempt)
        if not applied:raise ValueError('operator_policy_recovery_source_missing')
        expected=applied['row']
    if command.attempt>1 and command.role=='source':
        recovery=binding(conn,command.operation_id,'recovery',command.attempt-1)
        if not recovery:raise ValueError('operator_policy_own_recovery_missing')
        expected=recovery['row']
    actual=current_native(conn,command.kind,command.source['effective_date'],seller_id=command.source.get('seller_id'))
    if digest(actual)!=digest(expected) or (command.kind=='wb_incident_policy' and expected is None and digest(legacy_configs(conn))!=digest(command.source['legacy_before'])):raise ValueError('operator_policy_source_version_changed')

def bind_native(conn,command,*,identity,effective_date,queue_id='',recovery_operation_id=''):
    if not conn.in_transaction:raise ValueError('operator_policy_native_transaction_required')
    table=NATIVE[command.kind]
    if command.kind=='wb_incident_policy':
        native=conn.execute(f'SELECT * FROM {table} WHERE seller_id=? AND revision=?',(command.source['seller_id'],int(identity))).fetchone()
    else:native=conn.execute(f'SELECT * FROM {table} WHERE version_id=?',(identity,)).fetchone()
    if native is None:raise ValueError('operator_policy_native_source_missing')
    record={'identity':identity,'row':dict(native),'effective_date':effective_date,'queue_id':queue_id,'recovery_operation_id':recovery_operation_id}
    add_binding(conn,command,command.role,record)
    state='restored_pending' if command.role=='recovery' else 'native_applied'
    conn.execute(f'UPDATE {TABLE} SET state=?,reason_code=? WHERE operation_id=?',(state,'native_last_good_restored' if command.role=='recovery' else '',command.operation_id))

def add_binding(conn,command,role,record):
    encoded=canonical(record)
    conn.execute(f'INSERT INTO {BINDINGS} VALUES(?,?,?,?,?,?)',(command.operation_id,command.attempt,role,encoded,digest(record),datetime.now(timezone.utc).isoformat()))

def read(path,operation_id,*,actor):
    if not Path(path).is_file() or not re.fullmatch(r'oppolicy_[a-f0-9]{32}',str(operation_id or '')):return None
    with closing(readonly(path)) as conn:
        row=conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=? AND actor=?',(operation_id,actor)).fetchone() if exists(conn) else None
        return public(conn,row) if row else None

def source_not_saved(path,identity):
    """Private error evidence only: an existing identity must never be discarded."""
    if not Path(path).is_file():return True
    with closing(readonly(path)) as conn:
        return not exists(conn) or conn.execute(f'SELECT 1 FROM {TABLE} WHERE operation_id=?',(identity,)).fetchone() is None

def proof_valid(conn,row,native,proof):
    if not native or not proof or proof.get('readback_verified') is not True or str(proof.get('exact_version'))!=str(native['identity']):return False
    table=NATIVE[row['kind']]
    if proof.get('consumer')=='operator_policy_dated_publication_v1':
        from packages.application.operator_policy_history import publication_proof_valid
        return publication_proof_valid(conn,native,proof,operation_id=row['operation_id'] if 'operation_id' in row.keys() else None)
    if row['kind']=='wb_incident_policy':
        current=conn.execute(f'SELECT * FROM {table} WHERE seller_id=? AND revision=?',(native['row']['seller_id'],int(native['identity']))).fetchone()
        if proof.get('consumer')!='native_vitrina_incident_rematerialization' or proof.get('non_target_invariant')!='unchanged' or type(proof.get('readback_changed_cells')) is not int or proof['readback_changed_cells']!=0 or not proof.get('snapshots'):return False
        for snapshot in proof['snapshots']:
            audit=conn.execute('SELECT * FROM sheet_vitrina_v1_incident_rematerialization_audit WHERE operation_id=? AND bundle_version=? AND as_of_date=?',
                (proof.get('native_operation_id'),snapshot['bundle_version'],snapshot['as_of_date'])).fetchone()
            if not audit or audit['plan_fingerprint']!=proof.get('plan_fingerprint') or audit['after_plan_digest']!=snapshot['after_plan_digest'] or audit['non_target_digest']!=snapshot['non_target_digest']:return False
            if any(snapshot['projection_evidence'].get(day,{}).get('policy_revision')!=int(native['identity']) for day in snapshot['target_dates']):return False
    else:
        current=conn.execute(f'SELECT * FROM {table} WHERE version_id=?',(native['identity'],)).fetchone()
        version_key='proxy3_version' if row['kind']=='legacy_proxy' else 'proxy4_version'
        fp_key='proxy3_fingerprint' if row['kind']=='legacy_proxy' else 'proxy4_fingerprint'
        if proof.get('consumer')!='native_functional_economics' or not proof.get('dates') or any(not str(proof.get(key,'')).startswith('sha256:') for key in ('source_fingerprint','readback_plan_fingerprint','non_target_digest','ready_manifest_digest')):return False
        if any(proof.get('parameter_dependencies',{}).get(day,{}).get(version_key)!=native['identity'] or proof.get('parameter_dependencies',{}).get(day,{}).get(fp_key)!=native['row']['fingerprint'] for day in proof['dates']):return False
    return bool(current and digest(dict(current))==digest(native['row']))

def public(conn,row):
    source=json.loads(row['source_json']);native=binding(conn,row['operation_id']);proof=binding(conn,row['operation_id'],'publication')
    complete=row['state']=='completed' and proof_valid(conn,row,native,proof)
    state='completed' if complete else 'needs_attention' if row['state'] in {'needs_attention','completed'} else 'processing'
    return {'operation_id':row['operation_id'],'domain':row['kind'],'document_kind':row['kind'],'title_ru':KINDS[row['kind']],
        'actor':row['actor'],'accepted_at':row['accepted_at'],'status':'accepted','accepted':True,'durable_saved':True,
        'primary_effect':'source_saved','physical_applied':False,'state':state,'reason_code':('native_publication_proof_missing' if row['state']=='completed' and not complete else row['reason_code']) or ('' if complete else 'native_publication_pending' if native else 'native_apply_pending'),
        'message':'Документ сохранён.','fields':[{'label':'Действует с','value':row['effective_date']}],
        'source_ref':{'domain':row['kind'],'operation_id':row['operation_id'],'source_digest':row['source_digest'],'native_identity':native.get('identity') if native else None},
        'processing_receipt':{'native':native,'publication':proof,'progress':json.loads(row['progress_json'])},
        'detail_path':PATH+row['kind']+'/'+row['operation_id'],
        'journal_path':'/sheet-vitrina-v1/operations?operation_id='+row['operation_id'],
        'source_path':('/sheet-vitrina-v1' if row['kind']=='wb_incident_policy' else '/sheet-vitrina-v1/settings')+'?policy_operation_id='+row['operation_id']+'&policy_kind='+row['kind']}

def accept(entry,kind,payload,*,actor,operation_id):
    if kind not in KINDS or not re.fullmatch(r'oppolicy_[a-f0-9]{32}',str(operation_id or '')) or not actor:raise ValueError('operator_policy_identity_invalid')
    clean={key:value for key,value in payload.items() if key!='_operator_request_id'}
    request_digest=digest(clean)
    with closing(readonly(entry.runtime.db_path)) as conn:
        old=conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=?',(operation_id,)).fetchone() if exists(conn) else None
        if old:
            if old['kind']!=kind or old['actor']!=actor or old['request_digest']!=request_digest:raise ValueError('operator_policy_identity_conflict')
            return {'status':'ok','acceptance':public(conn,old)}
    source=prepare(entry,kind,clean)
    with closing(sqlite3.connect(entry.runtime.db_path)) as conn:
        conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE');ensure_schema(conn)
        # Reconcile a concurrent retry first, before any new command is saved.
        old=conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=?',(operation_id,)).fetchone()
        if old:
            if old['kind']!=kind or old['actor']!=actor or old['request_digest']!=request_digest:raise ValueError('operator_policy_identity_conflict')
            return {'status':'ok','acceptance':public(conn,old)}
        current=current_native(conn,kind,source['effective_date'],seller_id=source.get('seller_id'))
        if digest(current)!=digest(source['before']) or (kind=='wb_incident_policy' and current is None and digest(legacy_configs(conn))!=digest(source['legacy_before'])):raise ValueError('operator_policy_source_version_changed')
        competing=conn.execute(f"SELECT 1 FROM {TABLE} WHERE kind=? AND state IN ('pending','native_applied','restored_pending') LIMIT 1",(kind,)).fetchone()
        if competing:raise ValueError('operator_policy_pending_command_conflict')
        conn.execute(f'INSERT INTO {TABLE}(operation_id,kind,actor,request_digest,accepted_at,effective_date,source_json,source_digest) VALUES(?,?,?,?,?,?,?,?)',
            (operation_id,kind,actor,request_digest,entry.activated_at_factory(),source['effective_date'],canonical(source),digest(source)))
        conn.commit()
    receipt=read(entry.runtime.db_path,operation_id,actor=actor)
    if not receipt:raise ValueError('operator_policy_saved_readback_missing')
    return {'status':'ok','acceptance':receipt}

def read_bound(path,operation_id,*,actor,kind,payload):
    if not Path(path).is_file():return None
    clean={key:value for key,value in payload.items() if key!='_operator_request_id'}
    with closing(readonly(path)) as conn:
        row=conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=? AND actor=? AND kind=? AND request_digest=?',(operation_id,actor,kind,digest(clean))).fetchone() if exists(conn) else None
        return public(conn,row) if row else None

def prepare(entry,kind,payload):
    from packages.business_time import current_business_date_iso
    today=current_business_date_iso(entry.now_factory())
    if kind=='legacy_proxy':
        from packages.application.calculation_parameters import _parameters_from_payload
        before_date=_parameters_from_payload(payload).effective_date
    else:before_date=today
    seller=None
    if kind=='wb_incident_policy':
        from packages.application.wb_incident_policy import canonical_seller_id
        seller=canonical_seller_id()
    with closing(readonly(entry.runtime.db_path)) as observer:
        before=current_native(observer,kind,before_date,seller_id=seller)
        legacy=legacy_configs(observer) if kind=='wb_incident_policy' and before is None else []
    source={'payload':payload,'confirmation_date':today,'before':before,'legacy_before':legacy}
    if kind=='legacy_proxy':
        from packages.application.calculation_parameters import INITIAL_VERSION_ID
        preview=entry.calculation_parameters_block.preview_version(payload)
        if preview['preview_fingerprint']!=str(payload.get('preview_fingerprint') or ''):raise ValueError('calculation parameters changed after preview')
        effective=preview['parameters']['effective_date'];source['preview']=preview
        with closing(readonly(entry.runtime.db_path)) as conn:
            if not conn.execute(f"SELECT 1 FROM {NATIVE[kind]} WHERE version_id=?",(INITIAL_VERSION_ID,)).fetchone():raise ValueError('calculation parameters initial version required')
    elif kind=='proxy_v4_tax':
        preview=entry.proxy_v4_parameters_block.preview_tax_version(payload)
        if preview['preview_fingerprint']!=str(payload.get('preview_fingerprint') or ''):raise ValueError('Proxy V4 tax or current version changed after preview')
        effective=preview['effective_date'];source['preview']=preview
    else:
        from packages.application.wb_incident_policy import canonical_seller_id,save_policy_revision
        source['seller_id']=canonical_seller_id()
        normalized=dict(payload)
        if 'active' not in normalized and 'effective_from' not in normalized:
            ids=list(normalized.get('excluded_wb_warehouse_ids') or []);normalized.update(active=bool(ids),reason='Миграция совместимой настройки исключения складов',effective_from=today,effective_to='',status='active' if ids else 'disabled')
        normalized.setdefault('change_effective_from',today)
        # Native validator checks exact options from the accepted source; no WB call.
        options=incident_options(entry.runtime,today)
        previous=entry.runtime.load_latest_wb_incident_policy(seller_id=source['seller_id'])
        from packages.application.wb_incident_policy import get_latest_policy_state
        source['previous_policy']=get_latest_policy_state(entry.runtime,snapshot_date=today,seller_id=source['seller_id'])
        if options is None:
            # Validate intrinsic fields through the native planner. These temporary
            # option labels are never persisted as canonical identities or effects.
            requested=normalized.get('warehouse_entries')
            ids=[int(item.get('warehouse_id') or 0) for item in requested] if isinstance(requested,list) else list(normalized.get('excluded_wb_warehouse_ids') or [])
            names={int(item['warehouse_id']):item['warehouse_name'] for item in previous.get('warehouse_identities',[])}
            temporary=[{'warehouse_id':int(identity),'warehouse_name':names.get(int(identity),'pending identity '+str(identity))} for identity in ids]
            intrinsic=save_policy_revision(entry.runtime,payload=normalized,actor='validation',warehouse_options=temporary,timestamp=entry.activated_at_factory(),dry_run=True)
            source.update(payload=normalized,policy_plan=None,warehouse_options=[],options_pending=True)
            effective=intrinsic.get('changed_from') or normalized['change_effective_from']
        else:
            plan=save_policy_revision(entry.runtime,payload=normalized,actor='validation',warehouse_options=options,timestamp=entry.activated_at_factory(),dry_run=True)
            source.update(payload=normalized,policy_plan=plan,warehouse_options=options,options_pending=False)
            effective=plan.get('changed_from') or normalized['change_effective_from']
    source['effective_date']=effective
    return source

def incident_options(runtime,day):
    from packages.application.stocks_block import build_wb_warehouse_exclusion
    from packages.application.registry_upload_db_backed_runtime import _deserialize_temporal_source_payload
    with closing(readonly(runtime.db_path)) as conn:
        row=conn.execute("SELECT payload_json FROM temporal_source_snapshots WHERE source_key='stocks' AND snapshot_date=?",(day,)).fetchone() if exists(conn,'temporal_source_snapshots') else None
    stock=_deserialize_temporal_source_payload(row['payload_json']) if row else None
    if stock is None or getattr(stock,'kind','')!='success':return None
    return build_wb_warehouse_exclusion(items=list(stock.items),warehouse_rows=list(stock.warehouse_rows),excluded_warehouse_ids=(),snapshot_date=day,
        fetched_at=str(stock.fetched_at),pagination_complete=bool(stock.pagination_complete and getattr(stock,'warehouse_granularity_complete',True)),raw_rows_digest=str(stock.raw_rows_digest),require_complete=True)['options']


def progress(runtime,operation_id,*,state,reason='',data=None):
    with closing(sqlite3.connect(runtime.db_path)) as conn:
        conn.execute(f'UPDATE {TABLE} SET state=?,reason_code=?,progress_json=? WHERE operation_id=?',(state,reason,canonical(data or {}),operation_id));conn.commit()

def journal_sources():
    return [{'table':TABLE,'identity_column':'operation_id','domain':kind,'kind_filter':kind,'kind_column':'kind','actor_column':'actor','accepted_at_column':'accepted_at',
        'permission_guard':'_ensure_supply_operator_role' if kind=='wb_incident_policy' else '_ensure_operator_role','reader':public,'detail_path':PATH+kind+'/'} for kind in KINDS]

def command_from_row(row,attempt=None,role='source'):
    return NativeCommand(row['operation_id'],attempt or 1,json.loads(row['source_json']),row['kind'],row['actor'],role)

def actual_bound(conn,command):
    bound=binding(conn,command.operation_id,attempt=command.attempt)
    if not bound:raise ValueError('operator_policy_native_binding_missing')
    current=current_native(conn,command.kind,command.source['effective_date'],seller_id=command.source.get('seller_id'))
    if digest(current)!=digest(bound['row']):raise ValueError('operator_policy_source_version_changed')
    return bound

def _bind_no_change(entry,command):
    with closing(sqlite3.connect(entry.runtime.db_path)) as conn:
        conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE');before_native(conn,command)
        native=current_native(conn,command.kind,command.source['effective_date'],seller_id=command.source.get('seller_id'))
        if native is None:raise ValueError('operator_policy_no_change_native_source_missing')
        identity=str(native['revision']) if command.kind=='wb_incident_policy' else native['version_id']
        bind_native(conn,command,identity=identity,effective_date=command.source['effective_date']);conn.commit()

def _source_apply(entry,command):
    payload=dict(command.source['payload'])
    if command.kind=='legacy_proxy':
        result=entry.calculation_parameters_block.create_version(payload,preview_fingerprint=payload['preview_fingerprint'],created_by=command.actor,operator_command=command)
    elif command.kind=='proxy_v4_tax':
        result=entry.proxy_v4_parameters_block.create_tax_version(payload,preview_fingerprint=payload['preview_fingerprint'],created_by=command.actor,operator_command=command)
    else:
        from packages.application.wb_incident_policy import save_policy_revision
        from packages.application.stocks_block import build_wb_warehouse_exclusion
        from packages.contracts.stocks_block import StocksRequest
        from packages.business_time import current_business_date_iso
        today=current_business_date_iso(entry.now_factory())
        options=incident_options(entry.runtime,today)
        if options is None:
            stocks=entry.sku_management_block.stocks_block
            if stocks is None:raise RuntimeError('native_incident_options_pending')
            stock=stocks.execute(StocksRequest(snapshot_type='stocks',snapshot_date=today,nm_ids=[int(item['nm_id']) for item in entry.sku_management_block._active_skus()])).result
            if getattr(stock,'kind','')!='success':raise RuntimeError('native_incident_options_pending')
            options=build_wb_warehouse_exclusion(items=list(stock.items),warehouse_rows=list(stock.warehouse_rows),excluded_warehouse_ids=(),snapshot_date=today,
                fetched_at=str(stock.fetched_at),pagination_complete=bool(stock.pagination_complete and getattr(stock,'warehouse_granularity_complete',True)),raw_rows_digest=str(stock.raw_rows_digest),require_complete=True)['options']
        if command.attempt>1:
            with closing(readonly(entry.runtime.db_path)) as conn:recovery=binding(conn,command.operation_id,'recovery',command.attempt-1)
            payload['base_revision']=int(recovery['row']['revision'])
        native_plan=save_policy_revision(entry.runtime,payload=payload,actor=command.actor,warehouse_options=options,timestamp=entry.activated_at_factory(),dry_run=True)
        captured=command.source.get('policy_plan')
        def authorized_effect(plan):
            return {k:v for k,v in plan.get('native_arguments',{}).items() if k not in {'actor','created_at','expected_revision'}}
        if captured and authorized_effect(captured)!=authorized_effect(native_plan):raise ValueError('operator_policy_authorized_effect_changed')
        result=save_policy_revision(entry.runtime,payload=payload,actor=command.actor,warehouse_options=options,timestamp=entry.activated_at_factory(),operator_command=command)
    with closing(readonly(entry.runtime.db_path)) as conn:saved=binding(conn,command.operation_id,attempt=command.attempt)
    if not saved:_bind_no_change(entry,command)
    return result

def _economics_unresolved(runtime,dates):
    relevant=set(dates);issues=[]
    with closing(readonly(runtime.db_path)) as conn:
        for row in conn.execute('SELECT bundle_version,as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots'):
            meta=json.loads(row['plan_json']).get('metadata',{})
            registry=meta.get('functional_economics_historical_repair_required',{}).get('dates',{})
            for day,evidence in registry.items():
                if day in relevant:issues.append({'date':day,'snapshot_date':row['as_of_date'],'evidence':evidence})
    return issues

def _publish_economics(entry,command):
    from packages.business_time import current_business_date_iso
    if command.source['effective_date']<current_business_date_iso(entry.now_factory()):
        progress(entry.runtime,command.operation_id,state='needs_attention',reason='policy_historical_authority_required',data={'effective_date':command.source['effective_date'],'uncovered_dates':[command.source['effective_date']]});return
    result=entry.calculation_parameters_block.publish_current_functional_economics(
        verified_backup=entry.calculation_parameters_block.prepare_functional_economics_backup(),include_history=True)
    with closing(readonly(entry.runtime.db_path)) as conn:native=actual_bound(conn,command)
    key='proxy3_version' if command.kind=='legacy_proxy' else 'proxy4_version'
    dates=[day for day,params in result.get('parameter_dependencies',{}).items() if params.get(key)==native['identity']]
    if not dates:
        progress(entry.runtime,command.operation_id,state='native_applied',reason='native_ready_dates_pending',data={'effective_date':command.source['effective_date']});return
    unresolved=_economics_unresolved(entry.runtime,dates)
    if unresolved:
        progress(entry.runtime,command.operation_id,state='needs_attention',reason='policy_historical_authority_required',data={'uncovered_dates':sorted({item['date'] for item in unresolved}),'issues':unresolved});return
    if result.get('status')!='applied' or result.get('updates') or not str(result.get('source_fingerprint','')).startswith('sha256:'):
        raise RuntimeError('native_economics_exact_readback_pending')
    proof={'consumer':'native_functional_economics','exact_version':native['identity'],'readback_verified':True,
        'dates':dates,'parameter_dependencies':{day:result['parameter_dependencies'][day] for day in dates},
        'source_fingerprint':result['source_fingerprint'],'readback_plan_fingerprint':result['plan_fingerprint'],
        'applied_plan_fingerprint':result.get('applied_plan_fingerprint'),'recovery_policy':result.get('recovery_policy'),
        'non_target_digest':result['non_target_digest'],'ready_manifest_digest':result['ready_snapshot_manifest_digest']}
    finish_publication(entry,command,proof)

def finish_publication(entry,command,proof):
    with closing(sqlite3.connect(entry.runtime.db_path)) as conn:
        conn.row_factory=sqlite3.Row;conn.execute('BEGIN IMMEDIATE');native=actual_bound(conn,command)
        if str(proof.get('exact_version'))!=str(native['identity']) or proof.get('readback_verified') is not True:raise ValueError('operator_policy_publication_proof_incomplete')
        if proof.get('consumer')=='native_functional_economics':
            from packages.application.warehouse_functional_economics_backfill import _snapshot_manifest_digest
            snapshots=[dict(row) for row in conn.execute('SELECT bundle_version,as_of_date,plan_json,refreshed_at FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date')]
            if _snapshot_manifest_digest(snapshots)!=proof.get('ready_manifest_digest'):raise RuntimeError('native_ready_changed_during_policy_ack')
        if proof.get('consumer')=='native_vitrina_incident_rematerialization':
            from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan,_sheet_vitrina_plan_digest
            for snapshot in proof.get('snapshots',[]):
                ready=conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',(snapshot['bundle_version'],snapshot['as_of_date'])).fetchone()
                if not ready or _sheet_vitrina_plan_digest(_deserialize_sheet_vitrina_plan(ready['plan_json']))!=snapshot['after_plan_digest']:raise RuntimeError('native_ready_changed_during_policy_ack')
        if not proof_valid(conn,{'kind':command.kind},native,proof):raise ValueError('operator_policy_publication_proof_incomplete')
        old=binding(conn,command.operation_id,'publication',command.attempt)
        if old and old!=proof:raise ValueError('operator_policy_publication_proof_conflict')
        if not old:add_binding(conn,command,'publication',proof)
        if native.get('queue_id'):
            # Completion and the exact existing native queue acknowledgement
            # commit together; a lost response cannot leave a second proof attempt.
            matched=conn.execute("UPDATE sheet_vitrina_v1_proxy_targeted_recalc_queue SET status='complete',completed_at=?,error=NULL WHERE request_id=? AND settings_version_id=?",
                (entry.activated_at_factory(),native['queue_id'],native['identity'])).rowcount
            if matched!=1:raise ValueError('operator_policy_exact_native_queue_missing')
        conn.execute(f"UPDATE {TABLE} SET state='completed',reason_code='',progress_json='{{}}' WHERE operation_id=?",(command.operation_id,));conn.commit()

def incident_audit_proves(runtime,plan):
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan,_sheet_vitrina_plan_digest
    with closing(readonly(runtime.db_path)) as conn:
        for snapshot in plan['snapshots']:
            audit=conn.execute('SELECT * FROM sheet_vitrina_v1_incident_rematerialization_audit WHERE operation_id=? AND bundle_version=? AND as_of_date=?',
                (plan['operation_id'],snapshot['bundle_version'],snapshot['as_of_date'])).fetchone()
            ready=conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',(snapshot['bundle_version'],snapshot['as_of_date'])).fetchone()
            if not audit or audit['plan_fingerprint']!=plan['fingerprint'] or audit['after_plan_digest']!=snapshot['after_plan_digest'] or not ready or _sheet_vitrina_plan_digest(_deserialize_sheet_vitrina_plan(ready['plan_json']))!=snapshot['after_plan_digest']:return False
    return bool(plan['snapshots'])

def _publish_incident(entry,command):
    from packages.application.registry_upload_db_backed_runtime import _wb_incident_policy_row_to_dict
    from packages.application.vitrina_incident_rematerialization import plan_vitrina_incident_rematerialization,apply_vitrina_incident_rematerialization
    from packages.business_time import current_business_date_iso
    from dataclasses import replace
    source=command.source;today=current_business_date_iso(entry.now_factory())
    with closing(readonly(entry.runtime.db_path)) as conn:native=actual_bound(conn,command)
    effective=native['effective_date']
    if effective>today:
        progress(entry.runtime,command.operation_id,state='native_applied',reason='native_effective_date_pending',data={'effective_date':effective});return
    if effective<today:
        progress(entry.runtime,command.operation_id,state='needs_attention',reason='policy_historical_authority_required',data={'date_from_requested':effective,'date_to':today});return
    plan=None
    try:
        plan,_=plan_vitrina_incident_rematerialization(entry.runtime,date_from=effective,date_to=today,generated_at=entry.activated_at_factory())
        # The original native bound does not certify omitted historical dates.
        if plan['date_from_effective']!=effective:
            progress(entry.runtime,command.operation_id,state='needs_attention',reason='policy_historical_authority_required',data={'date_from_requested':effective,'date_from_supported':plan['date_from_effective'],'date_to':today});return
        if not plan.get('snapshots'):
            progress(entry.runtime,command.operation_id,state='native_applied',reason='native_ready_dates_pending');return
        missing=[]
        for snapshot in plan['snapshots']:
            for day in snapshot['target_dates']:
                evidence=snapshot['projection_evidence'].get(day,{})
                if evidence.get('status')!='ready' or evidence.get('policy_revision')!=int(native['identity']):missing.append({'date':day,'reason':evidence.get('reason','native_policy_projection_pending')})
        if missing:
            progress(entry.runtime,command.operation_id,state='native_applied',reason='native_policy_projection_pending',data={'dates':missing});return
        # Native fingerprints/CAS, compact before-images and transactional audit own publication.
        result=apply_vitrina_incident_rematerialization(entry.runtime,reviewed_plan=plan,fingerprint=plan['fingerprint'],approval_reference='operator-policy:'+command.operation_id,actor=command.actor,applied_at=entry.activated_at_factory())
        if result.get('readback_status')!='ok' or type(result.get('readback_changed_cells')) is not int or result['readback_changed_cells']!=0 or result.get('non_target_invariant')!='unchanged':raise RuntimeError('native_incident_readback_incomplete')
    except Exception as exc:
        if plan and incident_audit_proves(entry.runtime,plan):
            # Source/publisher commit succeeded; recover its exact native audit,
            # never append last-good solely because response/ack was lost.
            pass
        else:
            # Never recover over a later/foreign revision; native recovery repeats CAS.
            with closing(readonly(entry.runtime.db_path)) as conn:actual_bound(conn,command)
            before=source['before']
            prior=_wb_incident_policy_row_to_dict(before,seller_id=source['seller_id']) if before else {'status':'missing'}
            restored=entry.sku_management_block._restore_incident_last_good(previous_record=prior,previous_policy=source['previous_policy'],warehouse_options=source['warehouse_options'],
                changed_from=effective,snapshot_date=today,saved_policy={'revision':int(native['identity'])},operator_command=replace(command,role='recovery'))
            progress(entry.runtime,command.operation_id,state='restored_pending',reason='native_last_good_restored',data={'error':str(exc),'recovery':restored})
            return
    proof={'consumer':'native_vitrina_incident_rematerialization','exact_version':native['identity'],'readback_verified':True,
        'native_operation_id':plan['operation_id'],'plan_fingerprint':plan['fingerprint'],'date_from':effective,'date_to':today,
        'snapshots':plan['snapshots'],'non_target_invariant':'unchanged','readback_changed_cells':0}
    finish_publication(entry,command,proof)

def drain(entry,*,limit=10):
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.warehouse_sync_lock import warehouse_sync_lock
    require_heavy_owner(entry.runtime.runtime_dir)
    with closing(readonly(entry.runtime.db_path)) as conn:
        rows=conn.execute(f"SELECT * FROM {TABLE} WHERE state IN ('pending','native_applied','restored_pending') ORDER BY accepted_at,operation_id LIMIT ?",(limit,)).fetchall() if exists(conn) else []
    results=[]
    for row in rows:
        try:
            with warehouse_sync_lock(entry.runtime.runtime_dir,blocking=False):
                with closing(readonly(entry.runtime.db_path)) as conn:
                    latest=conn.execute(f"SELECT MAX(attempt) FROM {BINDINGS} WHERE operation_id=? AND role='source'",(row['operation_id'],)).fetchone()[0]
                    retry=bool(latest and binding(conn,row['operation_id'],'recovery',latest))
                    applied=binding(conn,row['operation_id'],attempt=latest) if latest and not retry else None
                command=command_from_row(row,(int(latest)+1 if retry else int(latest)) if latest else 1)
                if not applied:_source_apply(entry,command)
                if command.kind=='wb_incident_policy':_publish_incident(entry,command)
                else:_publish_economics(entry,command)
        except Exception as exc:
            message=str(exc)
            permanent=any(code in message for code in ('source_version_changed','authorized_effect_changed','revision conflict','has no exact identity','native_already_bound'))
            progress(entry.runtime,row['operation_id'],state='needs_attention' if permanent else row['state'],reason='native_source_drift' if permanent else 'native_apply_retry_pending',data={'error':message})
        results.append(read(entry.runtime.db_path,row['operation_id'],actor=row['actor']))
    return results
