"""GET-only projection of existing append-only seller writer operations.

The Change Registry owns submission/readback. This adapter never submits,
reconciles, initializes storage or infers external success from a prepared job.
"""
from pathlib import Path
import sqlite3
from packages.application.change_registry import ChangeRegistryRepository, OPERATIONS_TABLE
from packages.application.change_registry_observer import ChangeRegistryReadSurface

# Each domain retains its actual native source grant, not a supply-wide grant.
SURFACES = {
    'wb_prices': ('prices_upload', 'Цены WB', '/v1/sheet-vitrina-v1/prices/status'),
    'wb_ads': ('ads_bid_change', 'Рекламные ставки WB', '/v1/sheet-vitrina-v1/ads/status'),
    'sku_prices': ('sku_management_price', 'Цены SKU', '/v1/sheet-vitrina-v1/sku-management'),
    'sku_ads': ('sku_management_bid', 'Рекламные ставки SKU', '/v1/sheet-vitrina-v1/sku-management'),
    'inventory_balance': ('sku_inventory_balance', 'Баланс запасов: внешние действия', '/v1/sheet-vitrina-v1/sku-management/inventory-balance'),
    'spp_test': ('spp_tester', 'SPP: изменение и восстановление цены', '/v1/sheet-vitrina-v1/prices/spp-test/status'),
    'keyword_cleaner': ('search_cluster_cleaner', 'Чистка поисковых запросов', '/v1/sheet-vitrina-v1/ads/keyword-cleaner/history'),
}
DOMAIN_LABELS = {d: v[1] for d, v in SURFACES.items()}
BY_SURFACE = {v[0]: d for d, v in SURFACES.items()}
TABLE = OPERATIONS_TABLE
SEARCH_COLUMNS = "operation_id || ' ' || source_surface || ' ' || actor_principal || ' ' || native_idempotency_key || ' ' || correlation_id"


def source(conn, *, selected, scope, db_path):
    """Exact account and allowed source surfaces precede all common reads/counts."""
    surfaces=tuple(sorted(SURFACES[d][0] for d in set(selected).intersection(SURFACES)))
    if not surfaces or not isinstance(scope, ChangeRegistryReadSurface):
        return None
    if Path(scope.store_registry.resolve('operational')).resolve() != Path(db_path).resolve():
        raise ValueError('operator_external_store_binding_mismatch')
    if not scope.seller_id or scope.account_scope != 'seller-portal-primary':
        raise ValueError('operator_external_account_binding_invalid')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    where='seller_id=? AND account_scope=? AND source_surface IN ('+','.join('?' for _ in surfaces)+')'
    values=(scope.seller_id,scope.account_scope,*surfaces)
    repository=ChangeRegistryRepository(scope.runtime_dir)
    return (TABLE,'operation_id','*',where,values,
        lambda connection,row: public(repository.read_operation(row['operation_id']),scope=scope,surfaces=surfaces))


def public(native, *, scope, surfaces):
    op=native['operation']
    if op['seller_id']!=scope.seller_id or op['account_scope']!=scope.account_scope or op['source_surface'] not in surfaces:
        raise ValueError('operator_external_source_scope_changed')
    domain=BY_SURFACE[op['source_surface']]
    latest={r['change_item_id']:r for r in native['latest_attempts']}
    children=[]
    for item in native['items']:
        event=latest.get(item['change_item_id'],{})
        state=event.get('resolution_state') if event.get('state')=='resolved' else event.get('state','missing')
        proven=state=='confirmed' and event.get('readback_proof_kind')=='wb_readback' and bool(event.get('readback_digest'))
        children.append({k:item.get(k) for k in ('change_item_id','target_kind','nm_id','advert_id','placement','query_hash','parameter_field',
            'before_value_kind','before_value_integer','before_value_text','requested_value_kind','requested_value_integer','requested_value_text')} | {
            'native_state':event.get('state','missing'),'resolution_state':event.get('resolution_state',''),
            'outcome':state,'external_confirmed':proven,'attempt_id':event.get('attempt_id',''),
            'receipt_reference':event.get('receipt_reference',''),'readback_digest':event.get('readback_digest','')})
    outcomes=[c['outcome'] for c in children]
    confirmed=sum(c['external_confirmed'] for c in children)
    if children and confirmed==len(children):
        state,reason='completed','Изменение подтверждено точной проверкой WB.'
    elif not children or any(c=='missing' for c in outcomes) or any(c in {'ambiguous','rejected','failed','cancelled','confirmed'} and not child['external_confirmed'] for c,child in zip(outcomes,children)):
        state,reason='needs_attention','Есть неподтверждённые или завершившиеся с ошибкой строки. Повторная отправка не выполняется.'
    elif any(c=='submitted' for c in outcomes):
        state,reason='processing','Запрос отправлен. Ожидается точное подтверждение WB.'
    else:
        state,reason='accepted','Команда сохранена перед отправкой. Фактическое изменение WB ещё не подтверждено.'
    return dict(contract_name='operator_operations_v1',operation_id=op['operation_id'],domain=domain,
        title_ru=DOMAIN_LABELS[domain],durable_saved=True,primary_effect='external_command',state=state,
        native_state=','.join(sorted(set(c['native_state'] for c in children))),accepted_at=op['created_at'],actor=op['actor_principal'],
        source_ref=dict(domain=domain,entity_id=op['operation_id'],revision=op['provenance_digest'],
            native_idempotency_key=op['native_idempotency_key'],source_surface=op['source_surface'],
            seller_id=op['seller_id'],account_scope=op['account_scope'],action='external_change'),
        reason_ru=reason,external_confirmed=bool(children and confirmed==len(children)),
        partial=0<confirmed<len(children),confirmed_count=confirmed,target_count=len(children),children=children,
        fields=[dict(label='Автор',value=op['actor_principal']),dict(label='Подтверждено WB',value=f'{confirmed} из {len(children)}')],
        retry_owner='native_source',resubmit_allowed=False)


def read_native(db_path, *, domain, native_id, allowed_domains, scope):
    """Lost command response: read the exact native idempotency identity only."""
    from contextlib import closing
    from packages.application.operator_ff_overhead import readonly
    if domain not in SURFACES or not isinstance(native_id,str) or not native_id or len(native_id)>256:
        raise ValueError('operator_external_native_identity_invalid')
    if domain not in set(allowed_domains):return None
    with closing(readonly(db_path)) as conn:
        spec=source(conn,selected={domain},scope=scope,db_path=db_path)
        if not spec:return None
        table,key,columns,where,values,reader=spec
        rows=conn.execute(f'SELECT {columns} FROM {table} WHERE native_idempotency_key=? AND ({where})',(native_id,*values)).fetchall()
        if not rows:return None
        if len(rows)!=1:raise ValueError('operator_external_native_identity_ambiguous')
        from packages.application.operator_operations import _common
        return _common(reader(conn,rows[0]))


def decorate(result, *, db_path, scope, domain):
    """Attach only a native existing receipt; source success survives read failure."""
    result=dict(result)
    identity=str(result.get('registry_operation_id') or '')
    native_id=str(result.get('operation_id') or (result.get('event') or {}).get('correlation_id') or '')
    try:
        from packages.application.operator_operations import read_acceptance
        receipt=(read_acceptance(db_path,identity,allowed_domains={domain},external_scope=scope)
            if identity else read_native(db_path,domain=domain,native_id=native_id,allowed_domains={domain},scope=scope) if native_id else None)
    except (ValueError,OSError,sqlite3.Error):receipt=None
    if receipt:result['acceptance']=receipt
    else:result['operator_projection']={'status':'not_tracked','reason_code':'native_receipt_unavailable',
        'reason_ru':'Результат проверяется в исходном задании. Подтверждение внешнего изменения ещё не получено.'}
    return result
