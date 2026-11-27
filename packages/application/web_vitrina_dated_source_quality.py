"""Verify native historical numbers before attaching dated source coverage.

This changes presentation only. An immutable dated object's membership/catalog
defines the scope; the accepted exact-day WB payload supplies the observations.
Neither current nomenclature nor a previous numeric cell is an operand.
"""
from contextlib import closing
from datetime import datetime
import json
import math
import hashlib

from packages.application.web_vitrina_compact_table import CELL_FIELDS

PRESENTATION = tuple(CELL_FIELDS.index(key) for key in (
    'presentation_state', 'presentation_tone', 'presentation_reason',
    'quality_state', 'quality_label', 'quality_reason', 'completeness_state', 'missing_sku_count'))


def _scope_digest(scope):
    return hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def retained_scope_reader(store, edition):
    """Read only the exact day catalog and membership, never retained numbers."""
    cache = {}
    def read(day):
        if not edition or day not in edition['days']:
            return None
        catalog_id = store.day_catalogs(edition)[day]
        if catalog_id not in cache:
            cache[catalog_id] = json.loads((store.root / 'catalogs' / (catalog_id + '.json')).read_bytes())
        ref = edition['days'][day]
        with closing(store._open_day(store.root / 'objects' / (ref + '.sqlite3'))) as conn:
            rows = conn.execute('SELECT payload FROM metadata').fetchall()
        if len(rows) != 1:
            raise ValueError('dated_source_scope_metadata_invalid')
        metadata = json.loads(rows[0][0])
        if metadata.get('date') != day or not isinstance(metadata.get('members'), list):
            raise ValueError('dated_source_scope_date_invalid')
        previous = metadata.get('source_metric_quality') or {}
        scope = previous.get('dated_scope')
        if previous.get('contract') == 'native_dated_source_quality_v1' and scope is not None:
            if previous.get('date') != day or _scope_digest(scope) != previous.get('scope_sha256'):
                raise ValueError('dated_source_scope_proof_invalid')
            ids, groups = scope['nm_ids'], scope['groups']
            if (not ids or ids != sorted(set(ids)) or any(type(nm) is not int or nm <= 0 for nm in ids)
                    or not isinstance(groups, dict) or any(not key.startswith('group:') for key in groups)
                    or any(nm not in ids for values in groups.values() for nm in values)
                    or len([nm for values in groups.values() for nm in values]) != len({nm for values in groups.values() for nm in values})):
                raise ValueError('dated_source_scope_proof_invalid')
            from packages.application.web_vitrina_source_history_repair import SKU
            keys = sorted({key for family in SKU.values() for key in family})
            memberships = {nm: group for group, values in groups.items() for nm in values}
            structural = {f'SKU:{nm}|{key}': {'group_id': memberships.get(nm, '')} for nm in ids for key in keys}
            # Keep only explicit typed historical scope, not this generation's
            # current catalog grouping. No retained numbers are loaded.
            return {'catalog': {'rows': structural}, 'members': sorted(structural), 'dated_scope': scope}
        return {'catalog': cache[catalog_id], 'members': metadata['members'],
                'catalog_id': catalog_id, 'object_id': ref}
    return read


def _observation(payload, source, day):
    from packages.application.registry_upload_db_backed_runtime import _to_jsonable
    if payload is None:
        return None
    raw = _to_jsonable(payload)
    if not isinstance(raw, dict):
        return None
    if (raw.get('kind') != 'success' or not raw.get('source_fetched_at')
            or (raw.get('date') != day if source == 'seller_funnel_snapshot' else
                raw.get('date_from') != day or raw.get('date_to') != day)):
        return None
    try:
        if datetime.fromisoformat(str(raw['source_fetched_at'])).tzinfo is None:
            return None
    except ValueError:
        return None
    items = raw.get('items')
    if not isinstance(items, list) or type(raw.get('count')) is not int or raw['count'] != len(items):
        return None
    keys = ('view_count', 'open_card_count', 'ctr') if source == 'seller_funnel_snapshot' else (
        'views_current', 'ctr_current', 'orders_current', 'position_avg')
    ids = []
    for item in items:
        if (not isinstance(item, dict) or type(item.get('nm_id')) is not int or item['nm_id'] <= 0
                or any(type(item.get(key)) not in (int, float) or not math.isfinite(item[key]) or item[key] < 0
                       for key in keys)):
            return None
        ids.append(item['nm_id'])
    if len(ids) != len(set(ids)):
        return None
    return {'source_key': source, 'snapshot_date': day, 'source_fetched_at': raw['source_fetched_at'],
            'completeness': 'accepted_exact_day_payload', 'items': items}


def _same_number(actual, expected, metric):
    if actual in ('', None) or expected in ('', None):
        return actual in ('', None) and expected in ('', None)
    if type(actual) not in (int, float) or type(expected) not in (int, float):
        return False
    tolerance = 1e-6 if metric in {'ctr', 'ctr_current', 'avg_ctr_current', 'avg_position_avg'} else 0
    return math.isfinite(actual) and math.isfinite(expected) and abs(actual - expected) <= tolerance


def verified_source_quality(runtime, day, cells, reference):
    """Return unchanged native values with independently matched quality fields."""
    from packages.application.web_vitrina_source_history_repair import project_day, roster, SKU
    from packages.application.web_vitrina_history_compiler import digest
    if reference is None:
        return cells, None
    unit = {'members': reference['members'], 'cells': cells}
    ids = roster(unit)
    result = dict(cells)
    membership = {str(nm): sorted({reference['catalog']['rows'][rid].get('group_id', '')
              for rid in reference['members'] if rid.startswith(f'SKU:{nm}|')}) for nm in ids}
    groups = {}
    for nm, candidates in membership.items():
        if len(candidates) == 1 and candidates[0].startswith('group:'):
            groups.setdefault(candidates[0], []).append(int(nm))
    scope = reference.get('dated_scope') or {'nm_ids': ids, 'groups': {g: sorted(n) for g, n in sorted(groups.items())}}
    grouped = {nm for values in scope['groups'].values() for nm in values}
    proof = {'contract': 'native_dated_source_quality_v1', 'date': day,
             'dated_scope': scope, 'scope_sha256': _scope_digest(scope), 'sources': {}}
    for source in ('seller_funnel_snapshot', 'web_source_snapshot'):
        payload, _ = runtime.load_temporal_source_slot_snapshot(source_key=source, snapshot_date=day,
            snapshot_role='accepted_closed_day_snapshot')
        observation = _observation(payload, source, day)
        if observation is None:
            continue
        required = {f'SKU:{nm}|{key}' for nm in ids for key in SKU[source]}
        if not required <= set(unit['members']) or not required <= reference['catalog']['rows'].keys():
            proof['sources'][source] = {'state': 'dated_family_rows_unproven',
                'payload_sha256': digest(observation)}
            continue
        # A changed native row roster is not an error for unrelated dates or
        # metrics. Projection scaffolding carries no numbers and is never
        # returned: it merely lets the independent source recipe count the
        # entire proven dated cohort before matching the still-existing cells.
        absent = required - cells.keys()
        projection_unit = {**unit, 'cells': {**cells, **{rid: [None] * len(CELL_FIELDS) for rid in absent}}}
        expected = project_day(day, reference['catalog'], projection_unit, [observation], ids,
            'native-dated-source-quality', metadata_only=True)
        matched, mismatched = [], []
        for row_id, wanted in expected.items():
            if row_id not in cells:
                continue
            actual = result[row_id]
            cell = list(actual)
            group_unproven = row_id.startswith('GROUP:') and grouped != set(ids)
            if not group_unproven and _same_number(actual[0], wanted[0], row_id.split('|')[-1]):
                for index in PRESENTATION:
                    cell[index] = wanted[index]
                matched.append(row_id)
            else:
                # A new report/value wins over any historical number. Until
                # paired, its exact dated coverage is not proven by this report.
                cell[CELL_FIELDS.index('quality_state')] = 'partial' if actual[0] not in ('', None) else 'missing'
                cell[CELL_FIELDS.index('completeness_state')] = 'unknown_scope'
                cell[CELL_FIELDS.index('missing_sku_count')] = None
                reason = ('Датированная принадлежность части SKU к группе не подтверждена.' if group_unproven else
                          'Число из READY не совпало с принятым дневным отчётом WB; полнота не подтверждена.')
                cell[CELL_FIELDS.index('quality_reason')] = reason
                cell[CELL_FIELDS.index('presentation_reason')] = reason
                cell[CELL_FIELDS.index('presentation_state')] = 'unconfirmed' if actual[0] not in ('', None) else 'unavailable'
                cell[CELL_FIELDS.index('presentation_tone')] = 'warning' if actual[0] not in ('', None) else 'neutral'
                mismatched.append(row_id)
            result[row_id] = cell
        proof['sources'][source] = {'source_fetched_at': observation['source_fetched_at'],
            'payload_sha256': digest(observation), 'matched_count': len(matched),
            'matched_row_ids_sha256': digest(matched), 'mismatched_count': len(mismatched),
            'mismatched_row_ids_sha256': digest(mismatched), 'absent_native_count': len(absent)}
    return result, proof if proof['sources'] else None
