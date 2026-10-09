"""Immutable business source versions in the native user-config transaction.

Personal table preferences/authentication are not operator documents. This table
is source evidence only; it neither schedules nor executes calculation jobs.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import quote

TABLE='sheet_vitrina_v1_business_settings_versions'
DOMAIN='business_settings'
FIELDS={'sku_management':'forecast','sku_inventory_balance':'calculation'}
LABELS={'sku_management':'Настройки прогноза SKU','sku_inventory_balance':'Настройки расчёта Balance'}
NATIVE_PATHS={'sku_management':'/v1/sheet-vitrina-v1/sku-management/settings',
    'sku_inventory_balance':'/v1/sheet-vitrina-v1/sku-management/inventory-balance/settings'}


@dataclass(frozen=True)
class SettingsScope:
    actor: str
    user_key: str
    seller_id: str
    configs: frozenset[str]


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def ensure(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS '+TABLE+' (operation_id TEXT PRIMARY KEY,user_key TEXT NOT NULL,config_key TEXT NOT NULL,revision INTEGER NOT NULL,actor TEXT NOT NULL,seller_id TEXT NOT NULL,request_digest TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,accepted_at TEXT NOT NULL,schema_version INTEGER NOT NULL,UNIQUE(user_key,config_key,revision))')
    for action in ('UPDATE','DELETE'):
        conn.execute('CREATE TRIGGER IF NOT EXISTS '+TABLE+'_'+action.lower()+' BEFORE '+action+' ON '+TABLE+" BEGIN SELECT RAISE(ABORT,'business_settings_version_immutable'); END")


def command(user_key,config_key,payload,expected_revision,identity,actor,seller_id,schema_version):
    if config_key not in FIELDS or not isinstance(identity,str) or not re.fullmatch(r'business-settings:[A-Za-z0-9_-]{8,120}',identity):
        raise ValueError('business_settings_identity_invalid')
    if not actor or not seller_id or expected_revision is None:
        raise ValueError('business_settings_scope_and_CAS_required')
    digest=hashlib.sha256(canonical(dict(user_key=user_key,config_key=config_key,payload=payload,
        expected_revision=expected_revision,actor=actor,seller_id=seller_id,schema_version=schema_version)).encode()).hexdigest()
    return dict(operation_id=identity,user_key=user_key,config_key=config_key,actor=actor,seller_id=seller_id,request_digest=digest)


def previous(conn,command):
    row=conn.execute('SELECT * FROM '+TABLE+' WHERE operation_id=?',(command['operation_id'],)).fetchone()
    if row and any(row[field]!=command[field] for field in command):raise ValueError('business_settings_request_identity_conflict')
    return row


def save(conn,command,*,revision,before,after,accepted_at,schema_version):
    field=FIELDS[command['config_key']]
    conn.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?,?)',(
        command['operation_id'],command['user_key'],command['config_key'],revision,
        command['actor'],command['seller_id'],command['request_digest'],
        canonical(before.get(field)),canonical(after[field]),accepted_at,schema_version))
    return public(conn.execute('SELECT * FROM '+TABLE+' WHERE operation_id=?',(command['operation_id'],)).fetchone())


def public(row):
    identity=row['operation_id']
    return dict(contract_name='operator_operations_v1',domain=DOMAIN,operation_id=identity,
        accepted_at=row['accepted_at'],actor=row['actor'],title_ru=LABELS[row['config_key']],
        state='completed',durable_saved=True,primary_effect='source_saved',calculation_completed=False,
        reason_ru='Бизнес-настройки сохранены. Расчёт выполняется своим обычным процессом.',
        source_ref=dict(domain=DOMAIN,entity_id=identity,config_key=row['config_key'],revision=row['revision'],
            seller_id=row['seller_id'],request_digest=row['request_digest'],schema_version=row['schema_version'],
            after=json.loads(row['after_json'])),
        fields=[dict(label='Настройки',value=LABELS[row['config_key']]),dict(label='Версия',value=row['revision'])],
        native_state='source_saved',resubmit_allowed=False,
        journal_path='/sheet-vitrina-v1/operations?operation_id='+quote(identity,safe=''),
        detail_path='/v1/sheet-vitrina-v1/operations/'+quote(identity,safe=''))


def source(conn,*,selected,scope):
    if DOMAIN not in selected or not isinstance(scope,SettingsScope) or not scope.actor or not scope.user_key or not scope.seller_id:return None
    configs=tuple(sorted(set(scope.configs).intersection(FIELDS)))
    if not configs or not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():return None
    return (TABLE,'operation_id','*','actor=? AND user_key=? AND seller_id=? AND config_key IN ('+','.join('?' for _ in configs)+')',
        (scope.actor,scope.user_key,scope.seller_id,*configs),lambda connection,row:public(row))


def source_not_saved(db_path,identity):
    """Bound negative proof only after an exact read of this native identity."""
    from pathlib import Path
    import sqlite3
    from contextlib import closing
    if not isinstance(identity,str) or not re.fullmatch(r'business-settings:[A-Za-z0-9_-]{8,120}',identity):return False
    path=Path(db_path)
    if not path.is_file():return True
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            conn.execute('PRAGMA query_only=ON')
            exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone()
            return not exists or conn.execute('SELECT 1 FROM '+TABLE+' WHERE operation_id=?',(identity,)).fetchone() is None
    except sqlite3.Error:return False
