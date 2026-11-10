"""Append-only source evidence for current FBO closure admission only.

The global WB revision and native dispatch receipts remain unchanged. A full
revision authenticates only its own inputs; physical fields outside that hash
are attested separately by a native close or an explicitly approved snapshot.
"""
import json

from packages.application import current_fbo_supply_close as owner

CONTRACT = 'current_fbo_closure_evidence_v1'
# Native warehouse-domain prefix also includes these additive tables in the
# existing T2 checkpoint/restore; a rollback must not leave an orphan baseline.
EVIDENCE = 'sheet_vitrina_v1_ff_pool_current_supply_close_evidence'
IMPORTS = 'sheet_vitrina_v1_ff_pool_current_supply_close_evidence_imports'
ALLOWED = frozenset(('readyForSaleQuantity', 'unloadingQuantity'))
PHYSICAL_FIELDS = ('supply_id', 'wb_supply_id', 'preorder_id', 'cache_key',
    'status_id', 'virtual_type_id', 'type_label', 'box_type_id', 'is_box_on_pallet',
    'source_created_at', 'supply_date', 'fact_date', 'updated_date', 'updatedDate')


def exists(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def migrate(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {EVIDENCE}(
        closure_operation_id TEXT NOT NULL, supply_id TEXT NOT NULL,
        payload_digest TEXT NOT NULL, payload_json TEXT NOT NULL,
        import_operation_id TEXT, PRIMARY KEY(closure_operation_id,supply_id))''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {IMPORTS}(
        operation_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
        candidate_digest TEXT NOT NULL, receipt_json TEXT NOT NULL)''')
    for table in (EVIDENCE, IMPORTS):
        for action in ('UPDATE', 'DELETE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_immutable
                BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable FBO closure evidence'); END''')


def lookup(conn, closure_operation_id, supply_id):
    if not exists(conn, EVIDENCE):
        return None
    row = conn.execute(f'SELECT * FROM {EVIDENCE} WHERE closure_operation_id=? AND supply_id=?',
        (closure_operation_id, supply_id)).fetchone()
    if row is None:
        return None
    value = json.loads(row['payload_json'])
    owner.require(owner.digest(value) == row['payload_digest'], 'closure_evidence_corrupt')
    return value


def ordinary(conn, record, *, supply_id, source_key):
    normalized = record['normalized']
    cache, identity, source = owner.ledger._wb_supply_debit_identity(record=record, normalized=normalized)
    owner.require(cache and identity == supply_id and source == source_key,
        'current_warehouse_close_source_identity_changed')
    owner.require(owner.ledger._optional_int(normalized.get('status_id')) in owner.ledger.WB_DEBIT_STATUS_IDS
        and owner.ledger._optional_int(normalized.get('virtual_type_id')) != owner.ledger.WB_SKIP_VIRTUAL_TYPE_ID
        and normalized.get('type_label') != owner.ledger.WB_SKIP_TYPE_LABEL,
        'current_warehouse_close_ordinary_physical_supply_required')
    from packages.application.wb_supply_box_correction import load_active_box_correction
    owner.require(load_active_box_correction(conn, supply_id) is None,
        'current_warehouse_close_box_correction_requires_review')
    goods = record.get('raw_goods')
    owner.require(isinstance(goods, list) and 0 < len(goods) <= 500 and all(isinstance(g, dict) for g in goods),
        'closure_goods_scope')
    identities = [owner.canonical({k: g[k] for k in ('nmID', 'nmId', 'nm_id', 'barcode', 'vendorCode', 'techSize') if k in g}) for g in goods]
    owner.require('{}' not in identities and len(set(identities)) == len(identities), 'closure_goods_duplicate_or_missing_identity')


def inputs(record):
    n = record['normalized']
    first = owner.ledger._first_present
    return dict(supply_id=first(n, 'supply_id', 'wb_supply_id', 'preorder_id'),
        status_id=n.get('status_id'), updated_date=first(n, 'updated_date', 'updatedDate'),
        raw_goods_hash=record.get('raw_goods_hash') or n.get('raw_goods_hash'),
        raw_goods=record.get('raw_goods') or n.get('raw_goods') or [])


def physical(record):
    # Presence is evidence too: absent and null must not silently interchange.
    return dict(record={k: record[k] for k in PHYSICAL_FIELDS if k in record},
        normalized={k: record['normalized'][k] for k in PHYSICAL_FIELDS if k in record['normalized']})


def payload(plan, effect, record, provenance):
    revision = owner.ledger._wb_supply_revision(record, record['normalized'])
    owner.require(revision == effect['revision'] and {l['source_revision'] for l in effect['lines']} == {revision},
        'closure_baseline_old_full_revision_mismatch')
    value = dict(contract=CONTRACT, closure_operation_id=plan['operation_id'], supply_id=effect['supply_id'],
        source_key=effect['source_key'], plan_digest=plan['plan_digest'], old_full_revision=revision,
        revision_inputs=inputs(record), physical=physical(record), provenance=provenance)
    owner.require(owner.digest(value['revision_inputs']) == revision, 'closure_revision_inputs_contract_changed')
    return value


def append(conn, value, import_operation_id=None):
    previous = lookup(conn, value['closure_operation_id'], value['supply_id'])
    owner.require(previous is None or previous == value, 'closure_evidence_conflict')
    if previous is not None:
        return False
    conn.execute(f'INSERT INTO {EVIDENCE} VALUES(?,?,?,?,?)',
        (value['closure_operation_id'], value['supply_id'], owner.digest(value), owner.canonical(value), import_operation_id))
    return True


def capture_native(conn, plan):
    migrate(conn)
    for effect in plan['effects']:
        row = source_row(conn, effect['supply_id'])
        record = owner._wb_supply_record_from_row(row)
        ordinary(conn, record, supply_id=effect['supply_id'], source_key=effect['source_key'])
        append(conn, payload(plan, effect, record, dict(kind='native_close_transaction',
            source_row_digest=owner.digest(dict(row)), authority=owner.CONTRACT)))


def source_row(conn, supply):
    found = list(conn.execute('SELECT * FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=? OR wb_supply_id=? OR cache_key=?',
        (supply, supply, 'supply:' + supply)))
    owner.require(len(found) == 1, 'current_warehouse_close_source_missing_or_ambiguous')
    return found[0]


def verify_closure(conn, *, record, plan, effect, baseline=None):
    ordinary(conn, record, supply_id=effect['supply_id'], source_key=effect['source_key'])
    revision = owner.ledger._wb_supply_revision(record, record['normalized'])
    baseline = baseline if baseline is not None else lookup(conn, plan['operation_id'], effect['supply_id'])
    if baseline is None:
        # Compatibility only: exact old hash says nothing about historical type
        # or other fields it never hashed. Current ordinary checks always run.
        owner.require(revision == effect['revision'], 'current_warehouse_close_unproven_baseline')
        return 'exact_full_revision_without_historical_physical_attestation'
    owner.require(baseline['contract'] == CONTRACT and baseline['source_key'] == effect['source_key']
        and baseline['plan_digest'] == plan['plan_digest'] and baseline['old_full_revision'] == effect['revision']
        and baseline['closure_operation_id'] == plan['operation_id'] and baseline['supply_id'] == effect['supply_id']
        and owner.digest(baseline['revision_inputs']) == effect['revision'], 'closure_evidence_binding')
    owner.require(physical(record) == baseline['physical'], 'current_warehouse_close_source_revision_changed:physical')
    old, current = baseline['revision_inputs'], inputs(record)
    def invariant(value):
        # This hash derives from raw goods; retain it in evidence but compare the
        # actual complete goods with the two explicitly permitted leaves erased.
        return {**{k: v for k, v in value.items() if k not in ('raw_goods_hash', 'raw_goods')},
            'raw_goods': [{k: v for k, v in g.items() if k not in ALLOWED} for g in value['raw_goods']]}
    owner.require(len(old['raw_goods']) == len(current['raw_goods'])
        and all(set(a) == set(b) for a, b in zip(old['raw_goods'], current['raw_goods']))
        and owner.canonical(invariant(old)) == owner.canonical(invariant(current)), 'current_warehouse_close_source_revision_changed')
    return 'closure_ready_unloading_only_v1'
