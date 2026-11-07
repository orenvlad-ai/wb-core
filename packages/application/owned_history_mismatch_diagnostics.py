"""Bounded hash-only explanations; never an input to History acceptance guards."""
from datetime import date
from itertools import islice
import hashlib
import json
import re

CONTRACT = 'history_anchor_diff_v1'
MAX_ENTRIES = 24
GROUPS = ('dates', 'components', 'epoch', 'revisions', 'fence', 'ready_dates')
NAMED_KEYS = frozenset({
    'sheet_vitrina_v1_ready_snapshots', 'sheet_vitrina_v1_ready_revisions',
    'ready', 'publications', 'components', 'clock', 'current_sources', 'current_evidence',
    'supplier_certification', 'temporal', 'finance_daily_coverage', 'archival_cost',
    'breakglass', 'book', 'lifecycle_quality', 'formula', 'static', 'policies', 'labels',
    'facilities', 'identities', 'scopes', 'legacy', 'alarms', 'authority',
    'sheet_vitrina_v1_warehouse_business_projection_current_rows',
    'sheet_vitrina_v1_inventory_history_captures',
    'sheet_vitrina_v1_inventory_history_finalizations',
    'sheet_vitrina_v1_warehouse_wb_daily_cost',
    'sheet_vitrina_v1_canonical_cost_daily_state', 'sheet_vitrina_v1_wb_cost_daily_state',
    'sheet_vitrina_v1_calculation_parameter_versions', 'sheet_vitrina_v1_proxy_v4_parameter_versions',
}) | frozenset({
    'cost_price_current_state',
    'cost_price_upload_rows',
    'registry_upload_config_v2',
    'registry_upload_current_state',
    'registry_upload_formulas_v2',
    'registry_upload_metrics_v2',
    'sheet_vitrina_v1_calculation_parameter_versions',
    'sheet_vitrina_v1_ff_facilities',
    'sheet_vitrina_v1_ff_facility_profiles',
    'sheet_vitrina_v1_ff_pool_balances',
    'sheet_vitrina_v1_ff_pool_cutover_manifests',
    'sheet_vitrina_v1_ff_pool_document_expense_lines',
    'sheet_vitrina_v1_ff_pool_document_lines',
    'sheet_vitrina_v1_ff_pool_document_relations',
    'sheet_vitrina_v1_ff_pool_documents',
    'sheet_vitrina_v1_ff_pool_fbs_applicability_events',
    'sheet_vitrina_v1_ff_pool_fbs_drain_state',
    'sheet_vitrina_v1_ff_pool_fbs_forward_generations',
    'sheet_vitrina_v1_ff_pool_fbs_forward_state',
    'sheet_vitrina_v1_ff_pool_fbs_identity_pending',
    'sheet_vitrina_v1_ff_pool_fbs_identity_pending_resolutions',
    'sheet_vitrina_v1_ff_pool_fbs_lifecycle_current',
    'sheet_vitrina_v1_ff_pool_fbs_mapping_extension_allocations',
    'sheet_vitrina_v1_ff_pool_fbs_mapping_extensions',
    'sheet_vitrina_v1_ff_pool_feature_epochs',
    'sheet_vitrina_v1_ff_pool_movement_lines',
    'sheet_vitrina_v1_inventory_history_captures',
    'sheet_vitrina_v1_inventory_history_components',
    'sheet_vitrina_v1_inventory_history_finalizations',
    'sheet_vitrina_v1_nomenclature_items',
    'sheet_vitrina_v1_proxy_v4_parameter_versions',
    'sheet_vitrina_v1_sku_action_events',
    'sheet_vitrina_v1_sku_groups',
    'sheet_vitrina_v1_warehouse_archival_estimate_active',
    'sheet_vitrina_v1_warehouse_archival_estimate_rows',
    'sheet_vitrina_v1_warehouse_archival_estimate_versions',
    'sheet_vitrina_v1_warehouse_business_operations',
    'sheet_vitrina_v1_warehouse_business_projection_current_rows',
    'sheet_vitrina_v1_warehouse_functional_active',
    'sheet_vitrina_v1_warehouse_functional_balances',
    'sheet_vitrina_v1_warehouse_functional_cutovers',
    'sheet_vitrina_v1_warehouse_functional_events',
    'sheet_vitrina_v1_warehouse_functional_read_models',
    'sheet_vitrina_v1_warehouse_functional_versions',
    'sheet_vitrina_v1_warehouse_wb_daily_cost',
    'sheet_vitrina_v1_warehouse_wb_snapshots',
    'sheet_vitrina_v1_wb_fbs_stock_snapshot_rows',
    'sheet_vitrina_v1_wb_fbs_stock_snapshot_runs',
    'sheet_vitrina_v1_wb_fbs_warehouse_registry_rows',
    'sheet_vitrina_v1_wb_fbs_warehouse_registry_runs',
    'sheet_vitrina_v1_wb_supplies_fbs_identity_evidence',
    'sheet_vitrina_v1_wb_supplies_fbs_identity_mappings',
    'sheet_vitrina_v1_wb_supplies_fbs_order_observations',
    'sheet_vitrina_v1_wb_supplies_fbs_status_observations',
    'sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings',
})
HASH = re.compile(r'[0-9a-f]{64}\Z')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _date(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _key(value):
    return isinstance(value, str) and (value in NAMED_KEYS or _date(value) or bool(re.fullmatch(r'key_sha256:[0-9a-f]{64}', value)))


def _map(value):
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in islice(value.items(), 512)
            if _key(key) and isinstance(item, str) and HASH.fullmatch(item)}


def _proof_map(values):
    # Unknown source-table names are opaque identities, never arbitrary text.
    return {key if _key(key) else 'key_sha256:' + _hash(key): _hash(value)
            for key, value in sorted(values.items())[:512]}


def capture_diagnostics(*, per_day, epoch_parts, alarms, ready, authority):
    names = sorted({name for value in per_day.values() for name in value})
    ready_dates = {day: [] for day in per_day}
    for row in ready:
        if row[1] in ready_dates:
            ready_dates[row[1]].append(row)
    return {
        'components': _proof_map({name: [[day, values.get(name)] for day, values in sorted(per_day.items())]
                                  for name in names}),
        'epoch': _proof_map(epoch_parts), 'revisions': _proof_map(alarms),
        'fence': _proof_map({'alarms': alarms, 'ready': ready, 'authority': authority}),
        'ready_dates': _proof_map(ready_dates),
    }


def anchor_mismatch(anchor, vector, fence, diagnostic=None):
    """Explain existing inequality without changing or repeating its capture."""
    before = anchor.get('capture_diagnostics') or {}
    before = before if isinstance(before, dict) else {}
    after = diagnostic if isinstance(diagnostic, dict) else {}
    groups = {}
    for group in GROUPS:
        old_source = anchor['vector'].get('dates') if group == 'dates' else before.get(group)
        new_source = vector.get('dates') if group == 'dates' else after.get(group)
        old_available, new_available = isinstance(old_source, dict), isinstance(new_source, dict)
        old, new = _map(old_source), _map(new_source)
        keys = sorted(key for key in old.keys() | new.keys() if old.get(key) != new.get(key)) if old_available and new_available else []
        groups[group] = {'expected_available': old_available, 'observed_available': new_available,
                         'changed_count': len(keys) if old_available and new_available else None, 'truncated': len(keys) > MAX_ENTRIES,
                         'changes': [{'key': key, 'expected': old.get(key), 'observed': new.get(key)}
                                     for key in keys[:MAX_ENTRIES]]}
    return safe_mismatch({'contract': CONTRACT,
        'vector_changed': vector != anchor['vector'], 'fence_changed': fence != anchor['fence'],
        'expected_vector': _hash(anchor['vector']), 'observed_vector': _hash(vector),
        'expected_fence': _hash(anchor['fence']), 'observed_fence': _hash(fence), 'groups': groups})


def safe_mismatch(value):
    """Revalidate exact schema at every deserialized/mutable durable boundary."""
    if not isinstance(value, dict) or value.get('contract') != CONTRACT:
        return None
    if any(type(value.get(key)) is not bool for key in ('vector_changed', 'fence_changed')):
        return None
    fields = ('expected_vector', 'observed_vector', 'expected_fence', 'observed_fence')
    if any(not isinstance(value.get(key), str) or not HASH.fullmatch(value[key]) for key in fields):
        return None
    result = {key: value[key] for key in ('contract', 'vector_changed', 'fence_changed', *fields)}
    result['groups'] = {}
    groups = value.get('groups') if isinstance(value.get('groups'), dict) else {}
    for group in GROUPS:
        source = groups.get(group)
        if not isinstance(source, dict):
            continue
        changes = source.get('changes')
        changes = changes if isinstance(changes, list) else []
        clean = []
        for item in changes[:MAX_ENTRIES]:
            if (not isinstance(item, dict) or not _key(item.get('key')) or
                    any(item.get(key) is not None and (not isinstance(item[key], str) or not HASH.fullmatch(item[key]))
                        for key in ('expected', 'observed'))):
                continue
            clean.append({key: item.get(key) for key in ('key', 'expected', 'observed')})
        old_available, new_available = source.get('expected_available') is True, source.get('observed_available') is True
        count = source.get('changed_count')
        count = count if type(count) is int and 0 <= count <= 1024 else len(clean)
        if not (old_available and new_available):
            count, clean = None, []
        result['groups'][group] = {'expected_available': old_available, 'observed_available': new_available, 'changed_count': count, 'truncated': source.get('truncated') is True or len(changes) > MAX_ENTRIES,
                                   'changes': clean}
    return result
