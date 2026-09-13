"""Management-only read of the exact accepted, still preliminary inventory day.

Canonical ready membership, its completed publisher and the immutable capture
are checked together. A caller-supplied accounting binding is never acceptance.
No latest book/capture or archive search is used, and no reader writes a pointer.
"""
from __future__ import annotations

import json

from packages.application.inventory_quantity import CONTRACT, resolve_plan_quantities
from packages.application.ready_publication import digest, readonly
from packages.application.sheet_vitrina_v1_inventory_history import (
    CAPTURES_TABLE, COMPONENTS_TABLE, _stored_component, _materialize_captures, preview_inventory_history_capture,
    read_inventory_history_window,
)


def verify_capture_content(conn, capture):
    """Verify stored components as well as the indexed immutable identity."""
    columns = 'scope_kind,scope_key,nm_id,component_kind,component_id,component_label,state,quantity,source_revision,source_digest,source_watermark,provenance_json'
    components = [_stored_component(r) for r in conn.execute(f'SELECT {columns} FROM {COMPONENTS_TABLE} WHERE capture_id=?', (capture['capture_id'],))]
    preview = preview_inventory_history_capture(business_date=capture['business_date'],
        capture_kind=capture['capture_kind'], formula_version=capture['formula_version'],
        facility_roster=json.loads(capture['facility_roster_json']), source_manifest=json.loads(capture['source_manifest_json']),
        components=components, captured_at=capture['captured_at'])
    return preview['capture_id'] == capture['capture_id'] and preview['source_digest'] == capture['source_digest']


def read_management_inventory_history(db_path, *, runtime_dir, plan, current_date,
                                      lifecycle_quality_resolver=None):
    """Keep accepted dated quantity evidence without relaxing strict history reads."""
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan

    with readonly(db_path) as conn:
        result = read_inventory_history_window(db_path, dates=plan.date_columns,
            current_date='', connection=conn,
            lifecycle_quality_resolver=lifecycle_quality_resolver)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if CAPTURES_TABLE not in tables or 'sheet_vitrina_v1_ready_publications' not in tables:
            return result
        metadata = dict(plan.metadata or {})
        accepted = {}
        evidence = {}
        canonical = {}
        for day in plan.date_columns:
            # A selected finalization, including a diagnosed bad version, wins.
            if result['dates'].get(day, {}).get('finalization_id'):
                continue
            binding = metadata.get('fbs_accounting_bindings', {}).get(day)
            target = (metadata.get('fbs_accounting_targets', {}).get(day)
                      or metadata.get('ready_publication_target'))
            if not binding or not target or binding.get('ready_target') != target:
                continue
            key = (target.get('bundle_version'), target.get('as_of_date'))
            if key not in canonical:
                canonical[key] = conn.execute(
                    'SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?', key).fetchone()
            row = canonical[key]
            if row is None:
                continue
            stored = _deserialize_sheet_vitrina_plan(row['plan_json'])
            if (day not in stored.date_columns or stored.as_of_date != target['as_of_date']
                    or stored.metadata.get('ready_publication_target') != target
                    or stored.metadata.get('fbs_accounting_bindings', {}).get(day) != binding):
                continue
            try:
                operands = resolve_plan_quantities(stored, day=day, runtime_dir=runtime_dir,
                    ready_target=target, require_closed=False)
                if operands is None:
                    continue
                preview = preview_inventory_history_capture(business_date=day,
                    capture_kind='accepted_refresh', formula_version=CONTRACT,
                    facility_roster=operands['facility_roster'], source_manifest=operands['source_manifest'],
                    components=operands['components'], captured_at=row['refreshed_at'])
            except (ValueError, KeyError, TypeError):
                continue
            capture = conn.execute(f"""SELECT *, '' AS finalization_id,
                    '' AS finalization_digest, '' AS finalized_at FROM {CAPTURES_TABLE}
                WHERE capture_id=? AND business_date=? AND source_digest=?
                    AND bundle_version=? AND ready_snapshot_id=? AND ready_plan_version=?""",
                (preview['capture_id'], day, preview['source_digest'], row['bundle_version'],
                 row['snapshot_id'], row['plan_version'])).fetchone()
            if capture is None:
                continue
            if not verify_capture_content(conn, capture):
                continue
            # Capture and complete intent were committed by the same ready owner.
            # Later scoped recoveries can change unrelated ready cells: they do
            # not change this exact retained inventory slice or its source digest.
            receipt = conn.execute("""SELECT operation_id,attempt_id,after_digest FROM
                sheet_vitrina_v1_ready_publications WHERE bundle_version=? AND as_of_date=?
                AND state='complete' AND ready_required=1 AND book_version=?
                AND finished_at=? ORDER BY operation_id,attempt_id LIMIT 1""",
                (*key, binding['book_version'], capture['captured_at'])).fetchone()
            if receipt is None or not receipt['after_digest']:
                continue
            current_digest = digest(row['plan_json'])
            current_receipts = conn.execute("""SELECT kind,inputs_json FROM
                sheet_vitrina_v1_ready_publications WHERE bundle_version=? AND as_of_date=?
                AND state='complete' AND ready_required=1 AND after_digest=?""", (*key, current_digest)).fetchall()
            certified = False
            for current_receipt in current_receipts:
                if current_receipt['kind'] != 'inventory_retention':
                    certified = True
                    break
                pointer = json.loads(current_receipt['inputs_json'])
                item = pointer.get('dates', {}).get(day, {})
                if (pointer.get('contract') == 'accepted_inventory_retention_v1'
                        and item.get('binding') == binding
                        and item.get('capture_id') == capture['capture_id']
                        and item.get('source_digest') == capture['source_digest']
                        and item.get('publication_operation_id') == receipt['operation_id']
                        and item.get('publication_content_digest') == receipt['after_digest']):
                    certified = True
                    break
            if not certified:
                continue
            accepted[day] = capture
            evidence[day] = {'ready_target': target, 'ready_content_digest': current_digest,
                'publication_operation_id': receipt['operation_id'], 'publication_attempt_id': receipt['attempt_id'],
                'publication_content_digest': receipt['after_digest'], 'binding': binding}
        retained = _materialize_captures(conn, accepted, lifecycle_quality_resolver=lifecycle_quality_resolver)
        for day, dated in retained['dates'].items():
            dated['accepted_publication'] = evidence[day]
            for scope in dated['scopes'].values():
                scope['accepted_preliminary'] = True
                scope['captured_at'] = dated['captured_at']
                scope['accepted_publication'] = evidence[day]
            result['dates'][day] = dated
        facilities = {v['facility_id']: v for v in [*result['facilities'], *retained['facilities']]}
        result['facilities'] = sorted(facilities.values(), key=lambda v: (v.get('display_order', 0), v['facility_id']))
        return result
