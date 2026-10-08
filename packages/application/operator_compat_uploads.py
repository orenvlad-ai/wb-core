"""Receipts for explicit compatibility API uploads; native versions stay authoritative."""
from contextlib import closing
from dataclasses import asdict
import hashlib
import json

from packages.application.operator_ff_overhead import readonly

TABLE = 'operator_compat_upload_receipts'
DOMAINS = frozenset({'registry_bundle_upload', 'cost_price_upload'})


def ensure_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE}(
        operation_id TEXT PRIMARY KEY, domain TEXT NOT NULL, source_version TEXT NOT NULL,
        actor TEXT NOT NULL, accepted_at TEXT NOT NULL, source_digest TEXT NOT NULL,
        source_json TEXT NOT NULL, UNIQUE(domain,source_version),
        CHECK(domain IN ('registry_bundle_upload','cost_price_upload'))
    )''')
    conn.execute(f'CREATE INDEX IF NOT EXISTS operator_compat_upload_time ON {TABLE}(domain,accepted_at DESC,operation_id)')
    for action in ('UPDATE', 'DELETE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS operator_compat_upload_no_{action.lower()}
            BEFORE {action} ON {TABLE}
            BEGIN SELECT RAISE(ABORT,'compatibility upload receipt is immutable'); END''')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def identity(domain, version):
    if domain not in DOMAINS or not isinstance(version, str) or not version:
        raise ValueError('compatibility_upload_identity_required')
    return 'ops_compat_' + hashlib.sha256(canonical([domain, version]).encode()).hexdigest()


def record(conn, *, domain, source, result, actor):
    if not conn.in_transaction:
        raise RuntimeError('compatibility_receipt_requires_source_transaction')
    version = result.bundle_version if domain == 'registry_bundle_upload' else result.dataset_version
    actor = str(actor or '').strip()
    if not actor or result.status != 'accepted':
        raise ValueError('compatibility_receipt_requires_accepted_source_and_actor')
    metadata = {'source_version': version, 'counts': asdict(result.accepted_counts)}
    source_digest = 'sha256:' + hashlib.sha256(canonical(asdict(source)).encode()).hexdigest()
    conn.execute(f'''INSERT INTO {TABLE}(operation_id,domain,source_version,actor,accepted_at,
        source_digest,source_json) VALUES(?,?,?,?,?,?,?)''',
        (identity(domain, version), domain, version, actor, result.activated_at, source_digest, canonical(metadata)))


def exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def public(conn, row):
    metadata = json.loads(row['source_json'])
    return {'contract_name': 'operator_operations_v1', 'operation_id': row['operation_id'],
        'domain': row['domain'], 'actor': row['actor'], 'accepted_at': row['accepted_at'],
        'durable_saved': True, 'primary_effect': 'source_saved', 'state': 'completed',
        'native_state': 'source_saved', 'calculation_completed': False,
        'consumer_status': 'not_tracked', 'reason_code': 'source_saved',
        'reason_ru': 'Данные сохранены. Использование этой версии в расчётах проверяется отдельно.',
        'title_ru': 'Загрузка справочников через API' if row['domain'] == 'registry_bundle_upload' else 'Загрузка себестоимости через API',
        'source_ref': {'domain': row['domain'], 'entity_id': row['source_version'],
            'revision': row['source_version'], 'fingerprint': row['source_digest'], 'action': 'upload'},
        'fields': [{'label': 'Версия', 'value': row['source_version']},
            {'label': 'Строк сохранено', 'value': str(sum(metadata['counts'].values()))}],
        'journal_path': '/sheet-vitrina-v1/operations?operation_id=' + row['operation_id'],
        'detail_path': '/v1/sheet-vitrina-v1/operations/' + row['operation_id']}


def read_version(db_path, domain, version):
    operation_id = identity(domain, version)
    with closing(readonly(db_path)) as conn:
        if not exists(conn):
            return None
        row = conn.execute(f'SELECT * FROM {TABLE} WHERE operation_id=? AND domain=? AND source_version=?',
            (operation_id, domain, version)).fetchone()
        return public(conn, row) if row is not None else None
