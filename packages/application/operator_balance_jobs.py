"""Retained Balance client requests on the native apply job, query-only recovery.

This is a projection, not an executor. Native items and Change Registry attempts
own external effects. No owner constructor, schema initialization or replay on GET.
"""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from packages.application.operator_ff_overhead import readonly
from packages.application.change_registry import OPERATIONS_TABLE, ITEMS_TABLE, ATTEMPT_EVENTS_TABLE

DOMAIN = 'inventory_balance_jobs'
LABEL = 'Баланс запасов: задания'
PATH = '/v1/sheet-vitrina-v1/sku-management/inventory-balance/apply-jobs'
TABLE = 'sheet_vitrina_v1_inventory_balance_apply_jobs'
ITEM_TABLE = 'sheet_vitrina_v1_inventory_balance_apply_items'
IDENTITY = re.compile(r'balance-apply:[A-Za-z0-9_-]{16,80}\Z')
MAX_TARGETS = 1000
MAX_PROOF_BYTES = 2 * 1024 * 1024


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class Rejected(ValueError):
    """Proven before queue commit. Only these codes may clear a browser fence."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def command(payload, *, native_actor, actor, seller_id, account_scope):
    fields = {'request_id', 'apply_source_revision', 'calculation_id', 'nm_ids', 'target_keys', 'state_actions', 'mode', 'confirmed'}
    if set(payload) - fields or not IDENTITY.fullmatch(str(payload.get('request_id') or '')):
        raise Rejected('balance_apply_command_invalid')
    if not native_actor or not actor or not seller_id or account_scope != 'seller-portal-primary':
        raise Rejected('balance_apply_scope_required')
    if not re.fullmatch('[a-f0-9]{64}', str(payload.get('apply_source_revision') or '')):
        raise Rejected('balance_apply_revision_required')
    if payload.get('confirmed') is not True or payload.get('mode') not in ('live_wb', 'dry_run'):
        raise Rejected('balance_apply_command_invalid')
    if not isinstance(payload.get('calculation_id'), str) or not 1 <= len(payload['calculation_id']) <= 160:
        raise Rejected('balance_apply_command_invalid')
    nm, keys, actions = (payload.get(k, []) for k in ('nm_ids', 'target_keys', 'state_actions'))
    if any(not isinstance(v, list) or len(v) > MAX_TARGETS for v in (nm, keys, actions)):
        raise Rejected('balance_apply_command_invalid')
    if any(type(n) is not int or n <= 0 for n in nm) or any(not isinstance(k, str) or not 1 <= len(k) <= 240 for k in keys):
        raise Rejected('balance_apply_command_invalid')
    if any(not isinstance(a, dict) or set(a) != {'nm_id', 'advert_id', 'action'} or
           type(a['nm_id']) is not int or a['nm_id'] <= 0 or type(a['advert_id']) is not int or a['advert_id'] <= 0 or
           a['action'] not in ('start', 'pause') for a in actions):
        raise Rejected('balance_apply_command_invalid')
    if not (nm or keys or actions) or len(nm) != len(set(nm)) or len(keys) != len(set(keys)) or len(actions) != len({(a['nm_id'], a['advert_id']) for a in actions}):
        raise Rejected('balance_apply_command_invalid')
    request = dict(calculation_id=payload['calculation_id'], apply_source_revision=payload['apply_source_revision'],
                   nm_ids=sorted(nm), target_keys=sorted(keys), state_actions=sorted(actions, key=lambda a:(a['nm_id'], a['advert_id'])),
                   mode=payload['mode'], confirmed=True)
    if len(encoded(request)) > 512 * 1024:
        raise Rejected('balance_apply_command_invalid')
    return dict(request_id=payload['request_id'], native_actor=native_actor, actor=actor,
                seller_id=seller_id, account_scope=account_scope, request=request, request_digest=digest(request))


def revision(calculation):
    # Include immutable calculation plus every native override generation and
    # observed business target. Presentation/capability timestamps are excluded.
    keys = ('target_key','nm_id','advert_id','placement','payment_type','identity_valid','can_apply',
            'current_bid_minor','calculated_target_bid_rub','manual_target_bid_rub','final_target_bid_minor',
            'override_updated_at','override_updated_by','override_generation','campaign_status','campaign_state',
            'state_action','state_action_available','recommendation_item_id','campaign_state_recommendation_item_id',
            'current_bid_evidence','current_state_evidence')
    targets = [{k:t.get(k) for k in keys} for r in calculation.get('rows', []) for t in r.get('campaign_recommendations', [])]
    return digest(dict(calculation_id=calculation['calculation_id'], targets=sorted(targets,key=lambda t:t['target_key']),
                       owner_confirmation_policy=(calculation.get('apply_capability') or {}).get('owner_confirmation_policy')))


def proof(cmd, *, job_id, created_at, manifest, selection, targets):
    if not targets or len(targets) > MAX_TARGETS:
        raise Rejected('balance_apply_capacity_exceeded')
    value = dict(command=cmd, job_id=job_id, created_at=created_at, manifest=manifest, selection=selection,
                 targets=sorted(targets, key=lambda t:t['target_key']))
    if len(encoded(value)) > MAX_PROOF_BYTES:
        raise Rejected('balance_apply_capacity_exceeded')
    return value


@dataclass(frozen=True)
class BalanceScope:
    db_path: Path
    native_actor: str
    actor: str
    seller_id: str
    account_scope: str

    @classmethod
    def from_entrypoint(cls, app, *, native_actor, actor):
        owner, surface = app.sku_inventory_balance_block, app.change_registry_read_surface
        if not owner.seller_id or surface is None or not surface.seller_id:
            return None  # Unconfigured owner is not authority for this source.
        if (Path(owner.runtime.db_path).resolve() != Path(app.runtime.db_path).resolve() or
            Path(surface.store_registry.resolve('operational')).resolve() != Path(app.runtime.db_path).resolve() or
            owner.seller_id != surface.seller_id or owner.account_scope != surface.account_scope):
            raise ValueError('balance_apply_native_binding_invalid')
        return cls(Path(app.runtime.db_path).resolve(), native_actor, actor, owner.seller_id, owner.account_scope)


def source(conn, *, selected, scope, db_path):
    if DOMAIN not in selected or not isinstance(scope, BalanceScope) or not scope.actor or not scope.native_actor or not scope.seller_id:
        return None
    if Path(db_path).resolve() != scope.db_path.resolve() or scope.account_scope != 'seller-portal-primary':
        raise ValueError('balance_apply_native_binding_invalid')
    columns = {r[1] for r in conn.execute(f'PRAGMA table_info({TABLE})')}
    if 'client_request_id' not in columns:
        return None  # Legacy rows cannot prove operator acceptance.
    where = 'client_request_id IS NOT NULL AND created_by=? AND operator_actor=? AND operator_seller_id=? AND operator_account_scope=?'
    return (TABLE, 'client_request_id', '*', where, (scope.native_actor,scope.actor,scope.seller_id,scope.account_scope), public)


def verify(conn, row):
    try:
        return _verify(conn,row)
    except Rejected as exc:
        # A malformed ALREADY SAVED proof is never a negative acknowledgment
        # of source commit. Unknown/proof failures must retain the browser ID.
        raise ValueError('balance_apply_retained_proof_invalid') from exc


def _verify(conn, row):
    value = json.loads(row['operator_proof_json'])
    if len(encoded(value)) > MAX_PROOF_BYTES or digest(value) != row['operator_proof_digest']:
        raise ValueError('balance_apply_retained_proof_invalid')
    cmd = value['command']
    rebuilt = command(dict(cmd['request'],request_id=cmd['request_id']), native_actor=row['created_by'], actor=row['operator_actor'],
                      seller_id=row['operator_seller_id'], account_scope=row['operator_account_scope'])
    items = conn.execute(f'SELECT * FROM {ITEM_TABLE} WHERE job_id=? ORDER BY target_key',(row['job_id'],)).fetchall()
    targets = [json.loads(i['target_json']) for i in items]
    if cmd != rebuilt or cmd['request_id'] != row['client_request_id'] or value != proof(cmd,job_id=row['job_id'],created_at=row['created_at'],
            manifest=json.loads(row['apply_manifest_json']),selection=json.loads(row['selection_json']),targets=targets):
        raise ValueError('balance_apply_retained_proof_invalid')
    if row['apply_manifest_digest'] != 'sha256:'+digest(value['manifest']) or row['idempotency_key'] != row['apply_manifest_digest'] or cmd['request']['calculation_id'] != row['calculation_id'] or cmd['request']['mode'] != row['mode']:
        raise ValueError('balance_apply_retained_proof_invalid')
    return value, items


def external_child(conn, row, target, item):
    identity = str(item['registry_operation_id'] or '')
    result = dict(target_key=target['target_key'], registry_operation_id=identity, external_confirmed=False, native_state=item['state'])
    if not identity:
        return result
    op = conn.execute(f'SELECT * FROM {OPERATIONS_TABLE} WHERE operation_id=?',(identity,)).fetchone()
    expected = dict(seller_id=row['operator_seller_id'],account_scope=row['operator_account_scope'],source_surface='sku_inventory_balance',
                    actor_principal=row['created_by'],native_idempotency_key=row['job_id']+':'+target['target_key'],correlation_id=row['job_id'],
                    calculation_id=row['calculation_id'],apply_operation_id=row['job_id'])
    if not op or any(op[k] != v for k,v in expected.items()):
        return result | dict(reason_code='balance_apply_registry_binding_invalid')
    children = conn.execute(f'SELECT * FROM {ITEMS_TABLE} WHERE operation_id=?',(identity,)).fetchall()
    if len(children) != 1:
        return result | dict(reason_code='balance_apply_registry_binding_invalid')
    child = children[0]
    campaign = target.get('action_type') == 'campaign_state'
    expected_item = dict(nm_id=int(target['nm_id']),advert_id=int(target['advert_id']),placement='' if campaign else target['placement'],
                         parameter_field='campaign_state' if campaign else 'bid_minor',recommendation_item_id=target['recommendation_item_id'])
    expected_item.update(before_value_kind='text' if campaign else 'integer',requested_value_kind='text' if campaign else 'integer')
    expected_item.update({'before_value_text':target['current_campaign_state'],'requested_value_text':target['requested_campaign_state']} if campaign else
                         {'before_value_integer':target['current_bid_minor'],'requested_value_integer':target['final_target_bid_minor']})
    if any(child[k] != v for k,v in expected_item.items()):
        return result | dict(reason_code='balance_apply_registry_binding_invalid')
    event = conn.execute(f'SELECT * FROM {ATTEMPT_EVENTS_TABLE} WHERE change_item_id=? ORDER BY sequence_no DESC LIMIT 1',(child['change_item_id'],)).fetchone()
    reference = 'inventory-balance:'+row['job_id']+':'+target['target_key']
    confirmed = bool(event and (event['state']=='confirmed' or event['state']=='resolved' and event['resolution_state']=='confirmed') and
                     event['readback_proof_kind']=='wb_readback' and event['readback_digest'] and event['receipt_reference']==reference and
                     item['registry_receipt_reference']==reference)
    return result | dict(external_confirmed=confirmed,attempt_id=event['attempt_id'] if event else '',
                         readback_digest=event['readback_digest'] if event else '',receipt_reference=reference,
                         receipt_digest=event['receipt_digest'] if event else '',occurred_at=event['occurred_at'] if event else '')


def public(conn, row):
    value, items = verify(conn,row)
    children = [external_child(conn,row,target,item) for target,item in zip(value['targets'],items)]
    confirmed = sum(c['external_confirmed'] for c in children)
    complete = row['mode']=='live_wb' and confirmed==len(children) and bool(children)
    if complete:
        state, reason = 'completed','Все выбранные изменения подтверждены нативной проверкой WB.'
    elif row['state'] in ('completed','failed','stalled') or any(i['state'] in ('ambiguous','failed','skipped') for i in items):
        state, reason = 'needs_attention','Есть неподтверждённые строки. Читаем это же задание; повторная отправка не выполняется.'
    elif row['state'] in ('running','delayed'):
        state, reason = 'processing','Задание сохранено; фактическое изменение WB ещё проверяется.'
    else:
        state, reason = 'accepted','Задание и точные цели сохранены. Изменение WB ещё не подтверждено.'
    cmd = value['command']
    return dict(contract_name='operator_operations_v1',operation_id=cmd['request_id'],domain=DOMAIN,title_ru=LABEL,
        durable_saved=True,primary_effect='external_job',state=state,native_state=row['state'],accepted_at=row['created_at'],actor=cmd['actor'],
        source_ref=dict(domain=DOMAIN,entity_id=row['job_id'],revision=row['operator_proof_digest'],calculation_id=row['calculation_id'],
            request_digest=cmd['request_digest'],apply_source_revision=cmd['request']['apply_source_revision'],selection=value['selection'],
            apply_manifest_digest=row['apply_manifest_digest'],seller_id=cmd['seller_id'],account_scope=cmd['account_scope']),
        reason_ru=reason,external_confirmed=complete,partial=0<confirmed<len(children),confirmed_count=confirmed,target_count=len(children),children=children,
        fields=[dict(label='Задание',value=row['job_id']),dict(label='Подтверждено WB',value=f'{confirmed} из {len(children)}')],
        resubmit_allowed=False,retry_owner='native_source')


def read(db_path, *, scope, request_id='', job_id='', expected_command=None, builder=None):
    with closing(readonly(db_path)) as conn:
        spec = source(conn,selected={DOMAIN},scope=scope,db_path=db_path)
        if not spec:
            return None
        _, _, _, where, params, _ = spec
        row = conn.execute(f'SELECT * FROM {TABLE} WHERE {"client_request_id" if request_id else "job_id"}=? AND ({where})',
                           (request_id or job_id,*params)).fetchone()
        if not row:
            return None
        value, items = verify(conn,row)
        if expected_command is not None and value['command'] != expected_command:
            raise Rejected('balance_apply_identity_conflict')
        receipt = public(conn,row)
        receipt.update(journal_path='/sheet-vitrina-v1/operations?operation_id='+receipt['operation_id'],
                       detail_path='/v1/sheet-vitrina-v1/operations/'+receipt['operation_id'])
        result = builder(row,items) if builder else {}
        if builder:
            confirmed = {c['target_key'] for c in receipt['children'] if c['external_confirmed']}
            for item in result['items']:
                if item['target_key'] in confirmed:
                    item['result'] = {**item['result'], 'readback_status':'matching',
                        'confirmed_campaign_state':item.get('requested_campaign_state'),
                        'confirmed_bid_minor':item.get('final_target_bid_minor')}
        return result | dict(acceptance=receipt,client_request_id=row['client_request_id'],request=value['command']['request'],
                             request_digest=value['command']['request_digest'])
