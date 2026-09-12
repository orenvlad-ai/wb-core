"""Atomic CNY account demand, resumed by the existing warehouse continuation.

The singleton is an account watermark, not a money ledger. It retains exact
per-document revisions and old/new/pending supplier scope until queue delivery.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from packages.application.supplier_preparation_intents import _fingerprint, _json, _now

TABLE = "sheet_vitrina_v1_cny_preparation_intents"
PREFIX = "sheet_vitrina_v1_"
SOURCE_ID = "cny_document:account_replay"
DOCUMENT_FIELDS = (
    "document_id", "document_type", "source", "source_order_id", "context_order_id",
    "linked_financial_document_id", "natural_key", "operation_date", "operation_datetime",
    "status", "document_number", "currency", "rub_amount", "cny_amount", "bank_rate",
    "parsed_payload_json",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
        account_id TEXT PRIMARY KEY CHECK(account_id='account'), revision INTEGER NOT NULL,
        source_fingerprint TEXT NOT NULL, source_versions_json TEXT NOT NULL,
        affected_shipment_ids_json TEXT NOT NULL, affected_nm_ids_json TEXT NOT NULL,
        effective_date TEXT NOT NULL, missing_links_json TEXT NOT NULL,
        dependency_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
        requested_at TEXT NOT NULL, updated_at TEXT NOT NULL, prepared_at TEXT,
        replay_json TEXT NOT NULL DEFAULT '{{}}', queue_json TEXT NOT NULL DEFAULT '{{}}', error TEXT
    )""")


def capture_source(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    documents = {}
    for row in conn.execute(f"SELECT {','.join(DOCUMENT_FIELDS)} FROM {PREFIX}cny_documents ORDER BY document_id"):
        document = dict(row)
        document['parsed_payload_json'] = json.loads(document['parsed_payload_json'] or '{}')
        documents[document['document_id']] = document
    return documents


def _versions(source: dict[str, Any]) -> dict[str, str]:
    return {key: _fingerprint(value) for key, value in source.items()}


def _dependencies(conn: sqlite3.Connection, shipment_ids: list[str]) -> dict[str, Any]:
    result = {}
    for shipment_id in shipment_ids:
        header = conn.execute(f"SELECT shipment_id,invoice_amount_total,actual_shipment_date,actual_ff_acceptance_date FROM {PREFIX}supplier_shipments WHERE shipment_id=?", (shipment_id,)).fetchone()
        lines = [dict(row) for row in conn.execute(f"SELECT line_id,line_type,internal_nm_id,qty,unit_price,amount,match_status FROM {PREFIX}supplier_shipment_lines WHERE shipment_id=? ORDER BY line_id", (shipment_id,))]
        result[shipment_id] = {'header': dict(header) if header else None, 'lines': lines}
    return result


def begin_source_change(conn: sqlite3.Connection) -> dict[str, Any]:
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    return capture_source(conn)


def finish_source_change(conn: sqlite3.Connection, before: dict[str, Any], *, force: bool = False) -> None:
    """Caller owns the source transaction; no separate connection or commit."""
    after = capture_source(conn)
    before_versions, versions = _versions(before), _versions(after)
    changed = {key for key in before_versions.keys() | versions.keys() if before_versions.get(key) != versions.get(key)}
    current = conn.execute(f"SELECT * FROM {TABLE} WHERE account_id='account'").fetchone()
    if not changed and not force:
        return
    current = dict(current) if current else None
    dates = [str(source[key].get('operation_date') or '')[:10] for source in (before, after) for key in changed if key in source]
    if force and not changed:
        dates = [str(doc.get('operation_date') or '')[:10] for doc in after.values() if doc['status'] != 'excluded']
    valid_dates = [day for day in dates if len(day) == 10]
    first_date = min(valid_dates) if valid_dates else ''
    shipment_ids: set[str] = set()
    missing: set[str] = set()
    for source in (before, after):
        for key, doc in source.items():
            if key not in changed and (doc['status'] == 'excluded' or (first_date and str(doc['operation_date'])[:10] < first_date)):
                continue
            shipment_id = str(doc['source_order_id'] or '').strip()
            if shipment_id:
                shipment_ids.add(shipment_id)
            elif source is after and doc['document_type'] in {'supplier_cny_payment', 'bank_fee'} and doc['status'] != 'excluded':
                missing.add('document:' + key)
    # Coalescing may replace an undelivered revision but never drops its scope.
    nm_ids: set[int] = set()
    queue_table_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (PREFIX+'warehouse_targeted_recalc_queue',)).fetchone() is not None
    queue_pending = queue_table_exists and conn.execute(f"SELECT 1 FROM {PREFIX}warehouse_targeted_recalc_queue WHERE stable_source_id=? AND status<>'complete' LIMIT 1", (SOURCE_ID,)).fetchone() is not None
    # Delivery only acknowledges queue insertion. Keep the old shipment/SKU
    # associations while any account revision still awaits the warehouse ack.
    if current and (current['status'] != 'delivered' or queue_pending):
        shipment_ids.update(json.loads(current['affected_shipment_ids_json']))
        nm_ids.update(json.loads(current['affected_nm_ids_json']))
        if current['effective_date']:
            first_date = min(filter(None, (first_date, current['effective_date'])))
    dependencies = _dependencies(conn, sorted(shipment_ids))
    for shipment_id, dependency in dependencies.items():
        if dependency['header'] is None:
            missing.add('shipment:' + shipment_id)
        for line in dependency['lines']:
            if line['line_type'] == 'product' and int(line['internal_nm_id'] or 0) > 0:
                nm_ids.add(int(line['internal_nm_id']))
    now = _now()
    conn.execute(f"""INSERT INTO {TABLE}(
        account_id,revision,source_fingerprint,source_versions_json,affected_shipment_ids_json,
        affected_nm_ids_json,effective_date,missing_links_json,dependency_fingerprint,
        status,requested_at,updated_at,prepared_at,replay_json,queue_json,error
    ) VALUES('account',?,?,?,?,?,?,?,?,'pending',?,?,NULL,'{{}}','{{}}',NULL)
    ON CONFLICT(account_id) DO UPDATE SET revision=excluded.revision,
        source_fingerprint=excluded.source_fingerprint,source_versions_json=excluded.source_versions_json,
        affected_shipment_ids_json=excluded.affected_shipment_ids_json,affected_nm_ids_json=excluded.affected_nm_ids_json,
        effective_date=excluded.effective_date,missing_links_json=excluded.missing_links_json,
        dependency_fingerprint=excluded.dependency_fingerprint,status='pending',updated_at=excluded.updated_at,
        prepared_at=NULL,replay_json='{{}}',queue_json='{{}}',error=NULL""", (
        int(current['revision']) + 1 if current else 1, _fingerprint(versions), _json(versions),
        _json(sorted(shipment_ids)), _json(sorted(nm_ids)), first_date, _json(sorted(missing)),
        _fingerprint(dependencies), now, now,
    ))


def _matches(conn: sqlite3.Connection, request: dict[str, Any]) -> bool:
    return (_fingerprint(_versions(capture_source(conn))) == request['source_fingerprint']
            and _fingerprint(_dependencies(conn, json.loads(request['affected_shipment_ids_json']))) == request['dependency_fingerprint'])


def ensure_account_request(runtime: Any) -> None:
    """Explicit replay/reconcile may admit the currently named account once."""
    from packages.application.registry_upload_db_backed_runtime import _connect, _ensure_schema
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        before = begin_source_change(conn)
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE account_id='account'").fetchone()
        if row is None or not _matches(conn, dict(row)):
            finish_source_change(conn, before, force=True)
        conn.commit()


def read_account_request(runtime: Any) -> dict[str, Any] | None:
    # Status never initializes schema or changes the account.
    with sqlite3.connect(f'file:{runtime.db_path}?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
            return None
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE account_id='account'").fetchone()
        if row is None:
            return None
        result = dict(row)
        queue = json.loads(result['queue_json'])
        if queue.get('queue_id'):
            queued = conn.execute(f"SELECT * FROM {PREFIX}warehouse_targeted_recalc_queue WHERE queue_id=? AND stable_source_id=? AND source_revision=?", (queue['queue_id'], SOURCE_ID, queue['source_revision'])).fetchone()
            if queued is not None:
                result['queue_json'] = _json(dict(queued))
        return result


def _identity(request: dict[str, Any]) -> dict[str, Any]:
    return {'stable_source_id': SOURCE_ID, 'source_revision': 'cny-preparation:' + str(request['revision']) + ':' + request['source_fingerprint'],
            'account_revision': request['revision'], 'affected_shipment_ids': json.loads(request['affected_shipment_ids_json']),
            'affected_nm_ids': json.loads(request['affected_nm_ids_json']), 'effective_date': request['effective_date']}


def _outcome(request: dict[str, Any]) -> dict[str, Any]:
    queue = json.loads(request['queue_json'])
    replay = json.loads(request['replay_json'])
    identity = _identity(request)
    if request['status'] != 'delivered':
        queue = {**identity, 'status': 'replay_error', 'error': request['error'] or 'CNY account replay pending',
                 'diagnostic_code': 'cny_replay_shipment_scope_missing' if json.loads(request['missing_links_json']) else 'cny_account_preparation_pending'}
    return {**replay, 'status': 'pending' if request['status'] != 'delivered' else replay.get('status', 'ok'),
            'operation_applied': True, 'durable_saved': True,
            'readback_confirmed': bool(replay.get('readback_confirmed')),
            'durable_retry_identity': identity, 'warehouse_targeted_recalculation': {**queue, **identity}}


def drain_cny_preparation_intents(runtime: Any, *, block: Any = None, reason: str = 'saved_cny_source', inject_failure: Any = None) -> dict[str, Any]:
    from packages.application.cny_ledger import CnyLedgerBlock
    from packages.application.registry_upload_db_backed_runtime import _connect
    from packages.application.own_product_capital import OwnProductCapitalBlock
    from packages.application.warehouse_functional import ensure_warehouse_functional_schema, enqueue_source_replay_in_connection
    from packages.application.warehouse_functional_lock import warehouse_functional_write_lock

    request = read_account_request(runtime)
    if request is None:
        return {'status': 'no_op', 'requests': []}
    if request['status'] == 'delivered':
        return _outcome(request)
    preparation_complete = False
    try:
        with warehouse_functional_write_lock(runtime.runtime_dir, timeout_seconds=45):
            # Re-read after owning the existing writer lock: two consumers do
            # not repeat the same preparation. New source changes remain live.
            with _connect(runtime.db_path) as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute(f"SELECT * FROM {TABLE} WHERE account_id='account'").fetchone()
                request = dict(row)
                if request['status'] == 'delivered':
                    return _outcome(request)
                if not _matches(conn, request):
                    finish_source_change(conn, capture_source(conn), force=True)
                    request = dict(conn.execute(f"SELECT * FROM {TABLE} WHERE account_id='account'").fetchone())
                conn.commit()
            if not request['prepared_at']:
                if inject_failure:
                    inject_failure('before_preparation', request)
                ledger = block or CnyLedgerBlock(runtime=runtime)
                replay = ledger._replay_ledger(reason=reason)
                request['replay_json'] = _json(replay)
                if replay.get('status') == 'pending':
                    raise ValueError(replay.get('derived_replay_error') or 'CNY derived preparation pending')
                if json.loads(request['affected_nm_ids_json']) and (replay.get('replay') or {}).get('own_product_capital_diagnostics'):
                    raise ValueError('CNY payment capital allocation requires attention')
                if json.loads(request['missing_links_json']):
                    raise ValueError('CNY required shipment link missing')
                shipment_ids = json.loads(request['affected_shipment_ids_json'])
                if shipment_ids:
                    dependencies = _dependencies_for_runtime(runtime, shipment_ids)
                    if any(line['line_type'] == 'product' and int(line['internal_nm_id'] or 0) <= 0 for dependency in dependencies.values() for line in dependency['lines']):
                        raise ValueError('CNY required shipment SKU scope missing')
                    OwnProductCapitalBlock(runtime=runtime).set_expenses_certifications(
                        shipment_ids=shipment_ids, expenses_complete=False, recalculate=False,
                        update_shipment_headers=True, preserve_unchanged=True,
                    )
                if shipment_ids and not json.loads(request['affected_nm_ids_json']):
                    from packages.application.warehouse_business_projection import terminalize_supplier_certification_noop
                    terminalize_supplier_certification_noop(runtime, shipment_ids=shipment_ids)
                preparation_complete = True
                if inject_failure:
                    inject_failure('after_preparation', request)
            with _connect(runtime.db_path) as conn:
                ensure_warehouse_functional_schema(conn)
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute(f"SELECT * FROM {TABLE} WHERE account_id='account'").fetchone()
                if current is None or current['revision'] != request['revision'] or not _matches(conn, request):
                    raise ValueError('new CNY source revision awaits preparation')
                identity = _identity(request)
                if identity['affected_nm_ids']:
                    if not request['effective_date']:
                        raise ValueError('CNY required effective date missing')
                    queue = enqueue_source_replay_in_connection(conn, stable_source_id=SOURCE_ID,
                        source_revision=identity['source_revision'], effective_date=request['effective_date'],
                        affected_nm_ids_json=request['affected_nm_ids_json'], requested_at=request['requested_at'])
                else:
                    queue = {**identity, 'status': 'no_op', 'terminal_no_op': True,
                             'diagnostic_code': 'cny_replay_shipment_scope_unbound',
                             'warehouse_mutation_count': 0, 'functional_queue_count': 0}
                if inject_failure:
                    inject_failure('before_ack', request)
                conn.execute(f"UPDATE {TABLE} SET status='delivered',prepared_at=?,replay_json=?,queue_json=?,error=NULL WHERE account_id='account' AND revision=? AND source_fingerprint=?", (_now(), request['replay_json'], _json(queue), request['revision'], request['source_fingerprint']))
                conn.commit()
    except Exception as exc:
        with _connect(runtime.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            prepared_at = _now() if preparation_complete and _matches(conn, request) else request['prepared_at']
            conn.execute(f"UPDATE {TABLE} SET status='error',error=?,replay_json=?,prepared_at=? WHERE account_id='account' AND revision=? AND source_fingerprint=? AND status<>'delivered'", (str(exc).replace('\n', ' ')[:500], request['replay_json'], prepared_at, request['revision'], request['source_fingerprint']))
            conn.commit()
    return _outcome(read_account_request(runtime))


def _dependencies_for_runtime(runtime: Any, shipment_ids: list[str]) -> dict[str, Any]:
    from packages.application.registry_upload_db_backed_runtime import _connect
    with _connect(runtime.db_path) as conn:
        return _dependencies(conn, shipment_ids)
