"""Prove and journal unchanged accepted inventory across a scoped READY edit.

The caller owns the source transition, transaction, backup and CAS. This module
never captures quantities, searches for latest inventory, or changes READY/book.
"""
from __future__ import annotations

import json

from packages.application.inventory_quantity import CONTRACT, resolve_plan_quantities
from packages.application.management_inventory_history import verify_capture_content
from packages.application.ready_publication import (
    ExpectedReady, canonical, complete_publication, digest, record_intent,
)
from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
from packages.application.sheet_vitrina_v1_inventory_history import CAPTURES_TABLE, preview_inventory_history_capture


class InventoryRetentionError(ValueError):
    pass


def inventory_slice(plan, day):
    """All source and aggregate inventory quantities, including their quality."""
    keys = {str(row[1]) for sheet in plan.sheets if sheet.sheet_name == 'DATA_VITRINA'
            for row in sheet.rows if len(row) > 1 and
            (str(row[1]).split('|')[-1].removeprefix('total_') == 'stock_total'
             or str(row[1]).split('|')[-1].removeprefix('total_').startswith('inventory_'))}
    index = 2 + plan.date_columns.index(day)
    values = {str(row[1]): row[index] for sheet in plan.sheets if sheet.sheet_name == 'DATA_VITRINA'
              for row in sheet.rows if len(row) > 1 and str(row[1]) in keys}
    cells = {key: plan.metadata.get('server_cell_presentation', {}).get(key, {}).get(day) for key in keys}
    return values, cells


def _certifies(row, item):
    if row['kind'] != 'inventory_retention':
        return True
    try:
        pointer = json.loads(row['inputs_json'])
        return pointer.get('contract') == 'accepted_inventory_retention_v1' and all(
            pointer.get('dates', {}).get(item['business_date'], {}).get(key) == value
            for key, value in item.items() if key in {'binding', 'capture_id', 'source_digest',
                'publication_operation_id', 'publication_content_digest'})
    except (ValueError, TypeError, AttributeError):
        return False


def prove_inventory_retention(conn, *, runtime_dir, before, after):
    """Return only exact previously accepted captures; uncertified absence stays absent.

    Before can be a full row from an attested owner backup. Immutable operands,
    original receipt, and certification of that exact before digest are read
    from the canonical live connection. Chained retention keeps the original
    publisher identity, never the time of the later unrelated edit.
    """
    identity = ('bundle_version', 'as_of_date', 'snapshot_id', 'plan_version', 'refreshed_at', 'activated_at')
    if any(before[key] != after[key] for key in identity):
        raise InventoryRetentionError('inventory-retention-ready-identity-changed')
    old, new = (_deserialize_sheet_vitrina_plan(row['plan_json']) for row in (before, after))
    target = {'bundle_version': before['bundle_version'], 'as_of_date': before['as_of_date']}
    if old.date_columns != new.date_columns:
        raise InventoryRetentionError('inventory-retention-date-layout-changed')
    for key in ('ready_publication_target', 'fbs_accounting_bindings', 'fbs_accounting_targets'):
        if old.metadata.get(key) != new.metadata.get(key):
            raise InventoryRetentionError('inventory-retention-binding-changed')
    for day in old.date_columns:
        if inventory_slice(old, day) != inventory_slice(new, day):
            raise InventoryRetentionError('inventory-retention-quantity-cells-changed')
    pointer = {'contract': 'accepted_inventory_retention_v1', 'ready_target': target,
               'before_content_digest': digest(before['plan_json']),
               'after_content_digest': digest(after['plan_json']), 'dates': {}}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if CAPTURES_TABLE not in tables or 'sheet_vitrina_v1_ready_publications' not in tables:
        return pointer
    for day in old.date_columns:
        binding = old.metadata.get('fbs_accounting_bindings', {}).get(day)
        if not binding:
            continue
        if binding.get('ready_target') != target:
            # This READY only references another canonical owner's binding.
            # Its unchanged pointer remains that owner's responsibility.
            continue
        if old.metadata.get('ready_publication_target') != target:
            raise InventoryRetentionError('inventory-retention-binding-target-invalid')
        # Resolve only the exact retained binding. No latest/day-wide capture lookup.
        operands = resolve_plan_quantities(old, day=day, runtime_dir=runtime_dir,
                                          ready_target=target, require_closed=False)
        if operands is None:
            continue
        preview = preview_inventory_history_capture(business_date=day, capture_kind='accepted_refresh',
            formula_version=CONTRACT, facility_roster=operands['facility_roster'],
            source_manifest=operands['source_manifest'], components=operands['components'],
            captured_at=before['refreshed_at'])
        capture = conn.execute(f'''SELECT * FROM {CAPTURES_TABLE} WHERE capture_id=? AND business_date=?
            AND bundle_version=? AND ready_snapshot_id=? AND ready_plan_version=?''',
            (preview['capture_id'], day, before['bundle_version'],
             before['snapshot_id'], before['plan_version'])).fetchone()
        if capture is None:
            continue
        original = conn.execute('''SELECT * FROM sheet_vitrina_v1_ready_publications WHERE
            bundle_version=? AND as_of_date=? AND state='complete' AND ready_required=1
            AND book_version=? AND finished_at=? ORDER BY operation_id,attempt_id LIMIT 1''',
            (target['bundle_version'], target['as_of_date'], binding['book_version'], capture['captured_at'])).fetchone()
        if (original is None or not original['after_digest'] or capture['source_digest'] != preview['source_digest']
                or not verify_capture_content(conn, capture)):
            raise InventoryRetentionError('inventory-retention-original-acceptance-missing')
        item = {'binding': binding, 'capture_id': capture['capture_id'], 'source_digest': capture['source_digest'],
                'publication_operation_id': original['operation_id'], 'publication_content_digest': original['after_digest'],
                'publication_attempt_id': original['attempt_id'],
                'publication_receipt_digest': digest(canonical(dict(original))),
                'capture_record_digest': digest(canonical(dict(capture)))}
        current = conn.execute('''SELECT kind,inputs_json FROM sheet_vitrina_v1_ready_publications WHERE
            bundle_version=? AND as_of_date=? AND state='complete' AND ready_required=1 AND after_digest=?''',
            (target['bundle_version'], target['as_of_date'], pointer['before_content_digest'])).fetchall()
        if not any(_certifies(row, {**item, 'business_date': day}) for row in current):
            raise InventoryRetentionError('inventory-retention-before-certification-missing')
        pointer['dates'][day] = item
    return pointer


def retention_operation_id(operation_id, as_of_date):
    return operation_id + ':inventory_retention:' + as_of_date


def publish_inventory_retention(conn, *, operation_id, pointer, now):
    """Append one complete retention receipt inside the owner's existing transaction."""
    if not conn.in_transaction:
        raise InventoryRetentionError('inventory-retention-transaction-required')
    if not pointer['dates']:
        return None
    target = pointer['ready_target']
    row = conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?',
                       (target['bundle_version'], target['as_of_date'])).fetchone()
    if row is None or digest(row[0]) != pointer['after_content_digest']:
        raise InventoryRetentionError('inventory-retention-after-ready-drift')
    version = next(iter(pointer['dates'].values()))['binding']['book_version']
    identifier = retention_operation_id(operation_id, target['as_of_date'])
    record_intent(conn, operation_id=identifier, attempt_id='1', kind='inventory_retention',
        expected=ExpectedReady(target['bundle_version'], target['as_of_date'], row[0]), inputs=pointer,
        expected_book=version, book_required=False, ready_required=True, created_at=now)
    complete_publication(conn, operation_id=identifier, attempt_id='1', book_version=version,
                         after_digest=pointer['after_content_digest'], finished_at=now)
    return dict(conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                            (identifier, '1')).fetchone())


def retention_receipts_match(conn, receipts):
    for expected in receipts:
        actual = conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                             (expected['operation_id'], expected['attempt_id'])).fetchone()
        if actual is None or canonical(dict(actual)) != canonical(expected):
            return False
    return True
