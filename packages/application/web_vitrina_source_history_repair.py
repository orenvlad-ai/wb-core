"""Exact dated Seller Portal cell repair of retained HistoryStore objects.

This is an observed-source correction, not a native recomputation. Catalogs,
membership, native day proofs and every non-target compressed cell are retained.
The caller supplies validated, durably published observations and holds writer
admission. A fresh native watermark is never manufactured here.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
from types import SimpleNamespace
import uuid

from packages.application.web_vitrina_history_compiler import digest, dates_between
from packages.application.web_vitrina_history_store import HistoryUnavailable, _atomic, _json, _directory_fd
from packages.application.web_vitrina_history_group_repair import (
    _catalog, _unit, _patched_object, _copy, _same_file, _separate, _deadline,
)
from packages.application.web_vitrina_compact_table import CELL_FIELDS
from packages.application.web_vitrina_view_model import _build_cell, _FORMATTER_LIBRARY
from packages.application.web_vitrina_gravity_table_adapter import _renderer_id

CONTRACT = 'dated_observed_web_source_history_repair_v1'
SKU = {'seller_funnel_snapshot': ('view_count', 'open_card_count', 'ctr'),
       'web_source_snapshot': ('views_current', 'ctr_current', 'orders_current', 'position_avg')}
TOTAL = {'seller_funnel_snapshot': ('total_view_count', 'total_open_card_count', 'ctr'),
         'web_source_snapshot': ('total_views_current', 'avg_ctr_current', 'total_orders_current', 'avg_position_avg')}
PERCENT = frozenset({'ctr', 'ctr_current', 'avg_ctr_current'})


def roster(unit):
    return sorted({int(r.split('|')[0][4:]) for r in unit['members'] if r.startswith('SKU:')})


def _cell(day, metric, value, missing, *, undefined):
    """Use the actual native view-model and renderer wire construction."""
    absent = value is None or value == ''
    value = '' if absent else value
    reason = 'Обновлено по дневному отчёту WB; отсутствующие данные не заменены нулями.'
    if undefined:
        reason += ' Нулевой знаменатель: отношение не определено.'
    presentation = {'state': 'unavailable' if absent else 'unconfirmed' if missing else 'available',
        'tone': 'neutral' if absent or not missing else 'warning',
        'reason': reason, 'quality_reason': reason,
        'quality_state': 'undefined' if undefined else 'missing' if absent else 'partial' if missing else 'exact',
        'completeness_state': 'partial' if missing else 'complete', 'missing_sku_count': len(missing)}
    native = asdict(_build_cell(SimpleNamespace(id='date:'+day, value_type='decimal'),
        {'format': 'percent' if metric in PERCENT else 'number',
         'values_by_date': {day: value}, 'presentation_by_date': {day: presentation}}))
    native['renderer_id'] = _renderer_id(cell_kind=native['cell_kind'], formatter_id=native['formatter_id'])
    return [native[key] for key in CELL_FIELDS]


def _totals(observation, ids):
    """Reuse the accepted source publication's sums/weighted CTR/mean recipe."""
    from apps.web_vitrina_web_source_publication import project, TOTAL_METRICS
    source, day = observation['source_key'], observation['snapshot_date']
    items = {i['nm_id']: i for i in observation['items'] if i['nm_id'] in ids}
    missing = sorted(set(ids)-set(items))
    if not items:
        return {k: None for k in TOTAL[source]}, missing, set()
    plan = {'sheets': [{'sheet_name': 'DATA_VITRINA', 'header': ['label','row_id',day],
        'rows': [[k, 'TOTAL|'+k, ''] for k in TOTAL_METRICS[source]]}], 'temporal_slots': []}
    projected, _ = project(plan, [observation], ids, 'history-recipe')
    values = {r[1].split('|')[1]: r[2] for r in projected['sheets'][0]['rows']}
    undefined = set()
    if source == 'seller_funnel_snapshot':
        # Native visible funnel CTR is a rounded ratio of paired operands,
        # never an average of the provider's rounded SKU percentages.
        from packages.application.sheet_vitrina_v1_web_vitrina import _funnel_ctr_value
        denominator = values['total_view_count']
        values['ctr'] = _funnel_ctr_value(numerator=values['total_open_card_count'], denominator=denominator)
        if not denominator:
            undefined.add('ctr')
    elif values['avg_ctr_current'] in ('', None):
        undefined.add('avg_ctr_current')
    return values, missing, undefined


def project_day(day, catalog, unit, observations, ids, operation_id, *, presentation_catalog=None):
    if roster(unit) != ids:
        raise HistoryUnavailable('source_history_roster_drift')
    cells, patch = unit['cells'], {}
    groups = {}
    for rid in unit['members']:
        if rid.startswith('SKU:'):
            nm = int(rid.split('|')[0][4:])
            group = catalog['rows'][rid].get('group_id', '')
            if group.startswith('group:'):
                groups.setdefault(group[6:], set()).add(nm)
    for observation in observations:
        source = observation['source_key']
        items = {i['nm_id']: i for i in observation['items'] if i['nm_id'] in ids}
        for nm in ids:
            for metric in SKU[source]:
                rid = f'SKU:{nm}|{metric}'
                if rid not in cells or rid not in unit['members']:
                    raise HistoryUnavailable('source_history_required_sku_cell_absent')
                value = items.get(nm, {}).get(metric)
                undefined = False
                if source == 'seller_funnel_snapshot' and metric == 'ctr' and nm in items:
                    from packages.application.sheet_vitrina_v1_web_vitrina import _funnel_ctr_value
                    value = _funnel_ctr_value(numerator=items[nm]['open_card_count'], denominator=items[nm]['view_count'])
                    undefined = items[nm]['view_count'] == 0
                elif value is not None and metric in PERCENT:
                    value /= 100
                patch[rid] = _cell(day, metric, value, [nm] if nm not in items else [],
                    undefined=undefined)
        for scope, scope_ids in [('TOTAL', ids), *[(f'GROUP:{g}', sorted(n)) for g,n in sorted(groups.items())]]:
            values, missing, undefined = _totals(observation, scope_ids)
            for metric in TOTAL[source]:
                rid = scope+'|'+metric
                if rid not in cells:
                    if scope == 'TOTAL':
                        raise HistoryUnavailable('source_history_required_total_cell_absent')
                    continue  # No new GROUP identities or membership are invented.
                patch[rid] = _cell(day, metric, values[metric], missing,
                    undefined=metric in undefined)
    # HistoryStore/read_history_page serves assets from edition.catalog. Dated
    # catalogs bind row membership/context but may lack an asset already in the
    # unchanged serving union (e.g. an old day had no numeric percent cells).
    presentation = (catalog if presentation_catalog is None else presentation_catalog).get('presentation', {})
    renderers = {r['renderer_id']: r.get('formatter_id') for r in presentation.get('renderers', [])}
    formatters = {f['formatter_id'] for f in presentation.get('formatters', [])}
    # read_history_page expands built-in formatters referenced by declared
    # renderers. Older saved catalogs legitimately store only custom `rating`.
    # Require the exact existing renderer binding; never append catalog assets.
    if any(renderers.get(c[4]) != c[3] or c[3] not in formatters | _FORMATTER_LIBRARY.keys()
           for c in patch.values()):
        raise HistoryUnavailable('source_history_native_presentation_asset_missing')
    return patch


def plan_repair(source, *, expected_base, observations, rosters, operation_id, authority, deadline=None):
    """Read-only complete preview; no directory, object, pointer or cache writes."""
    days = sorted(rosters)
    if not 1 <= len(days) <= 7 or {o['snapshot_date'] for o in observations} != set(days):
        raise HistoryUnavailable('source_history_dates_invalid')
    for day in days:
        dates_between(day, day)
        if rosters[day] != sorted(set(rosters[day])) or not rosters[day]:
            raise HistoryUnavailable('source_history_roster_invalid')
    pointer = (source.root/'CURRENT.json').read_bytes()
    if (source._current() or {}).get('current') != expected_base:
        raise HistoryUnavailable('source_history_superseded')
    old = source.edition(expected_base)
    if not set(days) <= set(old['days']):
        raise HistoryUnavailable('source_history_unpublished_date')
    cache, patches, changed, base_objects = {}, {}, 0, {}
    presentation_catalog = _catalog(source, old['catalog'], deadline, cache)
    for day in days:
        _deadline(deadline)
        catalog = _catalog(source, source.day_catalogs(old)[day], deadline, cache)
        ref = old['days'][day]
        unit, _, _ = _unit(source, source.root/'objects'/(ref+'.sqlite3'), ref, day, catalog, deadline)
        patch = project_day(day, catalog, unit, [o for o in observations if o['snapshot_date']==day], rosters[day], operation_id,
                            presentation_catalog=presentation_catalog)
        changed += sum(unit['cells'][r] != c for r,c in patch.items())
        patches[day] = patch
        base_objects[day] = ref
    claim = {'contract': CONTRACT, 'operation_id': operation_id, 'base_edition_id': expected_base,
        'base_pointer_sha256': hashlib.sha256(pointer).hexdigest(), 'base_objects': base_objects,
        'source_observations_sha256': digest(observations), 'audited_nm_ids_by_date': deepcopy(rosters),
        'authority': deepcopy(authority), 'patches_sha256': digest(patches),
        'presentation_catalog_id': old['catalog'],
        'allowlisted_row_ids': {d: sorted(patches[d]) for d in days},
        'native_proofs_retained': True, 'fresh_native_watermark_claimed': False}
    if (source.root/'CURRENT.json').read_bytes() != pointer:
        raise HistoryUnavailable('source_history_superseded')
    return {'claim': claim, 'patches': patches, 'changed_cells': changed}


def verify_candidate(source, candidate, plan, edition, *, deadline=None):
    """Verify exact expected patches plus byte-identical non-target BLOBs."""
    claim = plan['claim']; old = source.edition(claim['base_edition_id'])
    if (claim['presentation_catalog_id'] != old['catalog'] or digest(plan['patches']) != claim['patches_sha256'] or
            {d: sorted(p) for d,p in plan['patches'].items()} != claim['allowlisted_row_ids']):
        raise HistoryUnavailable('source_history_patch_proof_changed')
    expected = repaired_edition(old, edition['days'], claim)
    if edition != expected or set(edition['days']) != set(old['days']):
        raise HistoryUnavailable('source_history_native_metadata_changed')
    cache = {}
    for day, old_id in old['days'].items():
        _deadline(deadline)
        if day not in plan['patches']:
            if edition['days'][day] != old_id:
                raise HistoryUnavailable('source_history_outside_scope')
            continue
        catalog = _catalog(source, source.day_catalogs(old)[day], deadline, cache)
        before, old_blobs, old_metadata = _unit(source, source.root/'objects'/(old_id+'.sqlite3'), old_id, day, catalog, deadline)
        new_id = edition['days'][day]
        owner = source if new_id == old_id else candidate
        after, new_blobs, new_metadata = _unit(owner, owner.root/'objects'/(new_id+'.sqlite3'), new_id, day, catalog, deadline)
        if old_metadata != new_metadata or set(old_blobs) != set(new_blobs):
            raise HistoryUnavailable('source_history_native_metadata_changed')
        patch = plan['patches'][day]
        if after['cells'] != {**before['cells'], **patch} or any(old_blobs[r] != new_blobs[r] for r in old_blobs if r not in patch):
            raise HistoryUnavailable('source_history_non_target_cell_changed')


def retained_claim(claim, day, object_id):
    """One bounded dated proof; never repeat a whole seven-day claim per day."""
    return {'contract': CONTRACT, 'operation_id': claim['operation_id'],
        'claim_sha256': digest(claim), 'object_id': object_id,
        'base_edition_id': claim['base_edition_id'], 'base_object': claim['base_objects'][day],
        'source_observations_sha256': claim['source_observations_sha256'],
        'audited_nm_ids': claim['audited_nm_ids_by_date'][day],
        'row_ids_sha256': digest(claim['allowlisted_row_ids'][day]),
        'presentation_catalog_id': claim['presentation_catalog_id'],
        'authority': deepcopy(claim['authority']), 'native_proofs_retained': True,
        'fresh_native_watermark_claimed': False}


def pin_base(source, claim):
    """Durable explicit rollback retention, independent of reader pin TTL."""
    from packages.application.web_vitrina_history_store import source_repair_pins
    path = source.root/'SOURCE_REPAIR_PINS.json'
    existing = source_repair_pins(source.root)
    entry = {'base_edition_id': claim['base_edition_id'], 'claim_sha256': digest(claim)}
    operation = claim['operation_id']
    if operation in existing and existing[operation] != entry:
        raise HistoryUnavailable('source_history_pin_conflict')
    existing[operation] = entry
    if len(existing) > 366:
        raise HistoryUnavailable('source_history_pin_limit')
    value = {'contract': 'explicit_source_repair_base_pins_v1', 'operations': existing}
    source._reserve(len(_json(value))+4096)
    _atomic(path, value)
    if source_repair_pins(source.root).get(operation) != entry:
        raise HistoryUnavailable('source_history_pin_unproven')


def verify_retained(source, plan, current, *, deadline=None):
    """Read back this operation after disjoint repairs/rolling publications."""
    from packages.application.web_vitrina_history_store import source_repair_pins
    claim = plan['claim']
    if source_repair_pins(source.root).get(claim['operation_id']) != {
            'base_edition_id': claim['base_edition_id'], 'claim_sha256': digest(claim)}:
        raise HistoryUnavailable('source_history_pin_unproven')
    old = source.edition(claim['base_edition_id']); refs = dict(old['days'])
    for day in plan['patches']:
        ref = current['days'].get(day)
        if (current.get('source_repair', {}).get(day) != retained_claim(claim, day, ref)
                or source.day_catalogs(current).get(day) != source.day_catalogs(old)[day]):
            raise HistoryUnavailable('source_history_retained_claim_changed')
        refs[day] = ref
    # Reconstruct only A's expected postimage, not the later whole edition B.
    # Its own objects still undergo the exact cells/BLOB comparison to pinned A.
    verify_candidate(source, source, plan, repaired_edition(old, refs, claim), deadline=deadline)


def repaired_edition(old, refs, claim):
    claims = deepcopy(old.get('source_repair', {}))
    for day in claim['base_objects']:
        claims[day] = retained_claim(claim, day, refs[day])
    edition = {**deepcopy(old), 'days': refs, 'source_repair': claims}
    if 'group_repair' in old:
        # The prior GROUP claim attested entire immutable objects. An overlay
        # preserves non-target GROUP cells but cannot retain that whole-object
        # claim for a replaced reference. Match ordinary rolling retention.
        retained = old.get('group_repair_retained_days')
        if retained is None:
            retained = {day: old['days'][day] for day, base_ref in old['group_repair']['base_objects'].items()
                        if day in old['days'] and old['days'][day] != base_ref}
        retained = {day: ref for day, ref in retained.items() if refs.get(day) == ref}
        edition.pop('group_repair', None)
        edition.pop('group_repair_retained_days', None)
        if retained:
            edition['group_repair'] = deepcopy(old['group_repair'])
            edition['group_repair_retained_days'] = retained
    return edition


def publish_repair(source, candidate, plan, *, revalidate, deadline=None):
    """Caller journals before entry; a second submit is never an implicit retry."""
    _separate(source, candidate)
    claim = plan['claim']; base = claim['base_edition_id']
    with candidate._writer(), source._writer():
        if hashlib.sha256((source.root/'CURRENT.json').read_bytes()).hexdigest() != claim['base_pointer_sha256'] or not revalidate():
            raise HistoryUnavailable('source_history_superseded')
        old = source.edition(base); refs = dict(old['days']); cache = {}
        for day, patch in plan['patches'].items():
            catalog = _catalog(source, source.day_catalogs(old)[day], deadline, cache)
            unit, _, _ = _unit(source, source.root/'objects'/(refs[day]+'.sqlite3'), refs[day], day, catalog, deadline)
            refs[day] = _patched_object(source, candidate, refs[day], unit, patch, deadline)
        edition = repaired_edition(old, refs, claim)
        verify_candidate(source, candidate, plan, edition, deadline=deadline)
        pin_base(source, claim)
        edition_id = digest(edition)
        candidate._reserve(len(_json(edition))+4096)
        _atomic(candidate.root/'editions'/(edition_id+'.json'), edition)
        paths = [('objects', ref+'.sqlite3') for day,ref in refs.items() if ref != old['days'][day]]
        paths.append(('editions', edition_id+'.json'))
        for folder, name in dict.fromkeys(paths):
            _deadline(deadline)
            src, dst = candidate.root/folder/name, source.root/folder/name
            if dst.exists():
                if not _same_file(src, dst, deadline):
                    raise HistoryUnavailable('source_history_object_conflict')
                continue
            temporary = dst.with_name('.building-'+uuid.uuid4().hex)
            try:
                _copy(src, temporary, source, deadline)
                temporary.replace(dst)
                import os
                with _directory_fd(dst.parent) as fd:
                    os.fsync(fd)
            finally:
                temporary.unlink(missing_ok=True)
        _deadline(deadline)
        if not revalidate() or hashlib.sha256((source.root/'CURRENT.json').read_bytes()).hexdigest() != claim['base_pointer_sha256']:
            raise HistoryUnavailable('source_history_superseded')
        _deadline(deadline)
        source._reserve(4096)
        _atomic(source.root/'CURRENT.json', {'current': edition_id, 'previous': base})
        return edition_id
