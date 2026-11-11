"""Canonical dispatch money, separate from immutable compatibility pool money.

One append-only valuation accompanies a new live dispatch. It never rewrites
the physical ledger, historical closeout, or an already frozen valuation.
"""
from copy import deepcopy
from decimal import Decimal
from datetime import datetime
import json

from packages.application.current_fbo_supply_close import allocate_capital
from packages.application.fbs_snapshot_cost import canonical, fingerprint

CONTRACT = 'fbo_dispatch_accounting_valuation_v1'
TABLE = 'sheet_vitrina_v1_ff_pool_dispatch_valuations'


def ensure_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE}(
        operation_id TEXT PRIMARY KEY, valuation_digest TEXT NOT NULL,
        valuation_json TEXT NOT NULL)''')
    for action in ('UPDATE', 'DELETE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {TABLE}_no_{action.lower()}
            BEFORE {action} ON {TABLE} BEGIN
            SELECT RAISE(ABORT, 'immutable_fbo_dispatch_valuation'); END''')


def native_identity(conn, operation_id):
    from packages.application import fbo_supply_lifecycle as owner
    header = conn.execute(f'SELECT * FROM {owner.OPERATIONS_TABLE} WHERE operation_id=?', (operation_id,)).fetchone()
    if header is None:
        raise ValueError('fbo_dispatch_valuation_native_missing')
    native = [dict(r) for r in conn.execute(f'SELECT * FROM {owner.LINES_TABLE} WHERE operation_id=? ORDER BY line_no', (operation_id,))]
    compat = [dict(r) for r in conn.execute(f'SELECT * FROM {owner.AGGREGATE_LINES} WHERE operation_id=? ORDER BY line_no', (operation_id,))]
    return owner.digest(dict(header=dict(header), lines=native, compat=compat))


def prepare(book, version, documents, *, business_date, lines):
    """Pure candidate; exact document coverage also detects money-only changes."""
    from packages.business_time import business_date_from_timestamp
    if (not book or not book.get('active') or not version or version != fingerprint(book) or book.get('publication_error')
            or business_date_from_timestamp(book.get('prepared_at') or '') != business_date):
        raise ValueError('fbo_dispatch_accounting_book_unavailable')
    state = book['state']
    period = state.get('periods', {}).get(business_date)
    if (not period or period.get('status') != 'open' or period.get('quality') != 'preliminary'
            or state.get('pending_documents')):
        raise ValueError('fbo_dispatch_accounting_period_unavailable')
    manifest = {doc['document_id']: doc['fingerprint'] for doc in documents}
    observed = {identity: item['fingerprint'] for identity, item in state['observed_documents'].items()}
    if len(manifest) != len(documents) or manifest != observed:
        raise ValueError('fbo_dispatch_accounting_documents_changed')
    rows = period['document_cost_state']['fbo_rows']
    result = []
    seen = set()
    for line in lines:
        key = f"{line['facility_id']}:FBO:{line['nm_id']}"
        row = rows.get(key)
        if (key in seen or line['pool'] != 'FBO' or line['quantity_delta'] >= 0
                or row is None or Decimal(str(row['quantity'])) != line['before_quantity']):
            raise ValueError('fbo_dispatch_accounting_quantity_changed')
        seen.add(key)
        allocation = allocate_capital(line['before_quantity'], row['capital_rub'], -line['quantity_delta'])
        result.append(dict(nm_id=line['nm_id'], facility_id=line['facility_id'], pool='FBO',
                           quantity_delta=line['quantity_delta'], **allocation))
    if not result:
        raise ValueError('fbo_dispatch_valuation_empty')
    return dict(contract=CONTRACT, book_version=version, business_date=business_date,
                document_manifest_digest=fingerprint(manifest), lines=result)


def capture(conn, runtime_dir, *, business_date, lines):
    from packages.application.fbs_accounting_runtime import load, path, readonly_book_connection
    from packages.application.fbs_snapshot_cost_sources import _documents
    # A GET/window-bound book can predate this owned operational transaction.
    # Borrow no window state here; read the latest committed immutable revision.
    if path(runtime_dir).exists():
        with readonly_book_connection(runtime_dir) as book_conn:
            book_conn.execute('BEGIN')
            book, version = load(runtime_dir, connection=book_conn)
    else:
        book, version = load(runtime_dir)
    if not book or not book.get('active'):
        raise ValueError('fbo_dispatch_accounting_book_unavailable')
    return prepare(book, version, _documents(conn), business_date=business_date, lines=lines)


def append(conn, operation_id, candidate):
    if not conn.in_transaction:
        raise ValueError('fbo_dispatch_valuation_owned_transaction_required')
    ensure_schema(conn)
    native_digest = native_identity(conn, operation_id)
    if candidate.get('expected_native_digest', native_digest) != native_digest:
        raise ValueError('fbo_dispatch_valuation_native_changed')
    value = dict(candidate, operation_id=operation_id, native_digest=native_digest)
    conn.execute(f'INSERT INTO {TABLE} VALUES(?,?,?)', (operation_id, fingerprint(value), canonical(value)))
    return value


def prepare_repair(conn, book, version, *, operation_id, approval_reference):
    """Explicit, reviewable candidate for one old dispatch; never writes."""
    from packages.application.fbs_snapshot_cost_sources import _documents
    if not approval_reference or not str(approval_reference).strip():
        raise ValueError('fbo_dispatch_repair_reference_required')
    documents = _documents(conn)
    targets = [d for d in documents if d['document_id'] == operation_id]
    if len(targets) != 1 or targets[0].get('dispatch_valuation'):
        raise ValueError('fbo_dispatch_repair_target_unavailable')
    target = targets[0]
    from packages.application.fbo_dispatch_cost_source import KIND
    if target['kind'] != KIND:
        raise ValueError('fbo_dispatch_repair_target_invalid')
    # The donor must precede the shipment, not be today's invalidated book or
    # a later valuation that has already consumed the wrong native debit.
    prepared = datetime.fromisoformat(book['prepared_at'].replace('Z', '+00:00'))
    posted = datetime.fromisoformat(target['posted_at'].replace('Z', '+00:00'))
    if prepared > posted:
        raise ValueError('fbo_dispatch_repair_book_after_dispatch')
    candidate = prepare(book, version, [d for d in documents if d['document_id'] != operation_id],
                        business_date=target['business_date'], lines=target['cost_document']['lines'])
    candidate.update(expected_native_digest=native_identity(conn, operation_id),
                     repair=dict(approval_reference=str(approval_reference),
                                 original_document_fingerprint=target['fingerprint']))
    return candidate


def resolve(conn, operation_id, lines, *, native_digest=None):
    """Verify the single stored valuation, returning a normalized copy only."""
    from packages.application.fbo_supply_lifecycle import _tables
    if TABLE not in _tables(conn):
        return None
    row = conn.execute(f'SELECT valuation_digest,valuation_json FROM {TABLE} WHERE operation_id=?', (operation_id,)).fetchone()
    if row is None:
        return None
    value = json.loads(row['valuation_json'])
    if (fingerprint(value) != row['valuation_digest'] or value.get('contract') != CONTRACT
            or value.get('operation_id') != operation_id or not value.get('book_version')
            or value.get('native_digest') != (native_digest or native_identity(conn, operation_id))
            or len(lines) != len(value['lines'])):
        raise ValueError('fbo_dispatch_valuation_proof_changed')
    result = []
    for native, cost in zip(lines, value['lines']):
        if any(native[key] != cost[key] for key in ('nm_id', 'facility_id', 'pool', 'quantity_delta', 'before_quantity', 'after_quantity')):
            raise ValueError('fbo_dispatch_valuation_quantity_changed')
        exact = allocate_capital(cost['before_quantity'], cost['before_capital_rub'], -cost['quantity_delta'])
        if any(cost[key] != amount for key, amount in exact.items()):
            raise ValueError('fbo_dispatch_valuation_conservation')
        normalized = deepcopy(cost)
        normalized['raw'] = dict(cost_snapshot=dict(unit_cost_rub=cost['wac_rub'],
            capital_delta_rub=cost['capital_delta_rub'], quality='exact_accounting_fbo_dispatch',
            source_version_id=value['book_version'], source_plan_fingerprint=row['valuation_digest'],
            source_business_date=value['business_date'],
            provenance=dict(contract=CONTRACT, book_version=value['book_version'],
                            valuation_digest=row['valuation_digest'], allocation=exact)))
        result.append(normalized)
    return dict(lines=result, valuation_digest=row['valuation_digest'], book_version=value['book_version'])


def normalize_warehouse_lines(conn, rows):
    """Both accounting and WB read the same verified valuation and fixed debit."""
    from packages.application.fbo_supply_lifecycle import _tables
    if TABLE not in _tables(conn):
        return rows
    if conn.execute(f'SELECT operation_id FROM {TABLE} LIMIT 1').fetchone() is None:
        return rows
    from packages.application.fbo_dispatch_cost_source import capture_documents
    costs = {}
    for doc in capture_documents(conn):
        if doc.get('dispatch_valuation'):
            for line in doc['cost_document']['lines']:
                costs[(doc['operation_id'], line['nm_id'])] = line['raw']['cost_snapshot']
    result = []
    for row in rows:
        snapshot = costs.get((row['operation_id'], row['nm_id']))
        if snapshot is not None:
            row = deepcopy(row)
            raw = json.loads(row['raw_json'])
            raw['cost_snapshot'] = snapshot
            row['raw_json'] = canonical(raw)
        result.append(row)
    return result
