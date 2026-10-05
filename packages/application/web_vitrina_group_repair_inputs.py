"""Pure GROUP-only repair from the accepted sixteen-field dated observations.

No evaluator/source replay and no re-labeling of the native day epoch. Auxiliary
inputs are explicit; unresolved arithmetic leaves that original GROUP cell.
"""
from copy import deepcopy
from types import SimpleNamespace
import math
from decimal import Decimal, ROUND_HALF_UP

from packages.application.web_vitrina_group_blocks import (
    AcceptedGroupEvaluator, PROXY_TOTALS, numeric, accepted_number, input_incomplete,
)
from packages.application.web_vitrina_history_compiler import digest
from packages.application.sheet_vitrina_v1_live_plan import TemporalLiveSources
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow

SCHEMA = 'accepted_group_cell_repair_inputs_v1'
BASE_METRICS = frozenset({'ctr', 'total_our_wb_unit_cost_rub',
    'total_our_wb_cost_confirmed_share_pct', *PROXY_TOTALS})
COUNTERS = frozenset({'total_orderCount', 'total_orderSum', 'total_views_current',
    'total_open_card_count', 'total_view_count', 'total_stock_total', 'total_stock_wb_total'})


def supported_metric(key):
    return key in BASE_METRICS or key in COUNTERS or key.startswith('total_inventory_')


def unpack_rows(day, catalog, cells):
    rows = []
    for position, rid in enumerate(catalog['order']):
        structural = catalog['rows'][rid]
        packed = cells.get(rid)
        if packed is None:
            continue
        if not isinstance(packed, list) or len(packed) != 16:
            raise ValueError('group_repair_invalid_cell_width')
        def static(key, default=''):
            return structural.get('values', {}).get(key, [default])[0]
        presentation = {key: packed[i] for key, i in (
            ('state', 5), ('tone', 6), ('reason', 7), ('quality_state', 8),
            ('quality_label', 9), ('quality_reason', 10), ('completeness_state', 11)) if packed[i] != ''}
        # None is also the absent wire default. It represents unknown scope
        # only when the preserved completeness actually says so.
        if packed[12] is not None or packed[11] == 'unknown_scope':
            presentation['missing_sku_count'] = packed[12]
        group = structural.get('group_id')
        group = group[6:] if isinstance(group, str) and group.startswith('group:') else None
        rows.append(WebVitrinaContractRow(rid, position, static('scope_kind'),
            static('scope_key'), static('scope_label'), static('metric_key'),
            static('metric_label'), '', static('section'), group, static('nm_id', None),
            packed[2], {day: packed[0]}, {day: presentation}))
    return rows


def _parameters_from_saved(rows, day, version, members):
    """Infer one rate only if all eligible preserved SKU ratios corroborate it."""
    by = {(r.nm_id, r.metric_key): r for r in rows if r.scope_kind == 'SKU'}
    def value(nm, metric): return accepted_number(by.get((nm, metric)), day)
    observations, rates = [], []
    for item in members:
        nm = item.nm_id
        profit, revenue, qty, ads = (value(nm, key) for key in
            (f'proxy_profit_{version}_rub', 'orderSum', 'orderCount', 'ads_sum'))
        if None in (profit, revenue, qty, ads) or min(revenue, qty, ads) < 0 or (qty == 0 and revenue != 0):
            continue
        margin = value(nm, f'proxy_margin_{version}_pct')
        perunit = value(nm, 'proxy_margin_per_unit_rub') if version == 4 else None
        if (revenue > 0 and margin is None) or (version == 4 and qty > 0 and perunit is None):
            return None, 'saved_proxy_ratio_missing'
        if profit != 0:
            if revenue <= 0 or margin in (None, 0):
                return None, 'saved_proxy_ratio_undefined'
            rates.append(profit / (revenue * margin))
            if version == 4:
                if qty <= 0 or perunit in (None, 0):
                    return None, 'saved_proxy_perunit_undefined'
                rates.append(profit / (qty * perunit))
        observations.append((profit, revenue, qty, margin, perunit))
    if not rates:
        return None, 'saved_proxy_rate_ambiguous'
    rate = rates[0]
    if not 0 < rate <= 1 or not all(math.isclose(x, rate, rel_tol=1e-9, abs_tol=1e-12) for x in rates):
        return None, 'saved_proxy_rate_disagreement'
    for profit, revenue, qty, margin, perunit in observations:
        if revenue > 0 and not math.isclose(profit / (revenue * rate), margin, rel_tol=1e-9, abs_tol=1e-12):
            return None, 'saved_proxy_margin_mismatch'
        if version == 4 and qty > 0 and not math.isclose(profit / (qty * rate), perunit, rel_tol=1e-9, abs_tol=1e-12):
            return None, 'saved_proxy_perunit_mismatch'
    return SimpleNamespace(buyout_rate=rate, version_id='corroborated_saved_SKU_ratios'), None


def _formatted_display(value, formatter_id):
    from packages.application.web_vitrina_view_model import _FORMATTER_LIBRARY
    formatter = _FORMATTER_LIBRARY.get(formatter_id)
    if formatter is None:
        raise ValueError('group_repair_formatter_unknown')
    if value is None:
        return formatter.null_display
    if formatter.decimals is None:
        return str(value)
    number = Decimal(str(value)) * Decimal(str(formatter.value_multiplier if formatter.value_multiplier is not None else 1))
    number = number.quantize(Decimal(1).scaleb(-formatter.decimals), rounding=ROUND_HALF_UP)
    text = format(number, ',' + '.' + str(formatter.decimals) + 'f' if formatter.thousands_separator else '.' + str(formatter.decimals) + 'f')
    return text.replace(',', '\u00a0').replace('.', ',') + (formatter.suffix or '')


def _cell(original, value, operands, day, missing_members, *, undefined=False):
    cell = list(original)
    known = [r.presentation_by_date.get(day, {}) for r in operands]
    missing_members = set(missing_members)
    missing_members.update(r.nm_id for r in operands if accepted_number(r, day) is None
                           or input_incomplete(r.presentation_by_date.get(day, {})))
    unknown = any(p.get('completeness_state') == 'unknown_scope' for p in known)
    partial = bool(missing_members) or unknown
    preliminary = any(p.get('state') == 'unconfirmed' or p.get('quality_state') in
                      {'preliminary', 'management_estimate'} for p in known)
    reason = ('Исправленный итог по сохранённым дневным SKU-операндам. '
              'Неизвестные вклады не равны нулю.' if partial else
              'Исправленный итог по сохранённым дневным SKU-операндам.')
    inherited = list(dict.fromkeys(p.get('quality_reason') or p.get('reason') for p in known if input_incomplete(p)))
    reason += ' ' + ' '.join(str(x) for x in inherited[:3] if x)
    if undefined:
        reason += ' Нулевой знаменатель: отношение не определено.'
    cell[0:2] = [value, _formatted_display(value, original[3])]
    cell[5:13] = ['unavailable' if value is None else 'unconfirmed' if partial or preliminary else 'available',
        'neutral' if value is None else 'warning' if partial or preliminary else 'neutral', reason.strip(),
        'undefined' if undefined else 'missing' if value is None else 'partial' if partial else 'preliminary' if preliminary else 'exact',
        '', reason.strip(), 'unknown_scope' if unknown else 'partial' if partial else 'complete',
        None if unknown else len(missing_members)]
    return cell


def prepare_group_repair_day(day, catalog, cells, *, cost_basis=None):
    cost_basis = {int(k):v for k,v in (cost_basis or {}).items()}
    rows = unpack_rows(day, catalog, cells)
    by = {r.row_id: r for r in rows}
    members = {}
    for r in rows:
        if r.scope_kind == 'SKU' and r.group is not None:
            members.setdefault(r.group, {})[r.nm_id] = ConfigV2Item(r.nm_id, True, r.scope_label, r.group, r.row_order)
    patch, unresolved = {}, {}
    parameters = {}
    for group, items in members.items():
        config = list(items.values())
        group_rows = [r for r in rows if r.scope_kind == 'SKU' and r.nm_id in items]
        resolved = {v: _parameters_from_saved(group_rows, day, v, config) for v in (3, 4)}
        parameters[group] = {str(v): {'rate': p.buyout_rate if p else None, 'reason': why}
                             for v, (p, why) in resolved.items()}
        evaluator = AcceptedGroupEvaluator(rows=group_rows, cost_basis={day: cost_basis or {}},
            enabled_config=config, metrics_by_key={}, formulas_by_id={},
            live_sources=TemporalLiveSources([], [], {day:SimpleNamespace(column_date=day)}, {}),
            proxy_parameters_resolver=lambda d: resolved[3][0],
            proxy_v4_parameters_resolver=lambda d: resolved[4][0])
        for rid, r in by.items():
            if r.scope_kind != 'GROUP' or r.group != group or not supported_metric(r.metric_key):
                continue
            key = r.metric_key
            if key in {'total_our_wb_unit_cost_rub','total_our_wb_cost_confirmed_share_pct'} and not cost_basis:
                patch[rid] = list(cells[rid]);unresolved[rid] = 'accepted_cost_basis_unproven'
                continue
            if key in PROXY_TOTALS and resolved[PROXY_TOTALS[key][0]][0] is None:
                patch[rid] = list(cells[rid]);unresolved[rid] = resolved[PROXY_TOTALS[key][0]][1]
                continue
            evaluator.visited_operands.clear()
            if key in BASE_METRICS:
                value = evaluator.resolve_total(key, day)
                evidence_key = PROXY_TOTALS[key][0] if key in PROXY_TOTALS else 'cost' if key == 'total_our_wb_unit_cost_rub' else 'confirmed_cost' if key == 'total_our_wb_cost_confirmed_share_pct' else 'ctr'
                evidence = evaluator.aggregation_evidence.get((day, evidence_key), {})
                operands = [evaluator.accepted[k] for k in evaluator.visited_operands if k in evaluator.accepted]
                absent = set(evidence.get('missing_nm_ids', []))
                undefined = value is None and evidence.get('zero_denominator', False)
            else:
                operand = key.removeprefix('total_')
                operands = [r for r in group_rows if r.metric_key == operand]
                if not operands:
                    patch[rid] = list(cells[rid]);unresolved[rid] = 'saved_counter_alias_absent'
                    continue
                values = [v for r in operands if (v := accepted_number(r, day)) is not None]
                value = sum(values) if values else None
                absent = set(items) - {r.nm_id for r in operands if accepted_number(r, day) is not None}
                undefined = False
            patch[rid] = _cell(cells[rid], value, operands, day, absent, undefined=undefined)
    aux = {'stored_day_cells': digest(cells), 'dated_catalog': digest(catalog),
           'accepted_cost_basis': digest({str(k):v for k,v in (cost_basis or {}).items()}), 'saved_proxy_rate_parity': digest(parameters)}
    return {'schema': SCHEMA, 'day': day, 'patch': patch, 'auxiliary_digests': aux,
            'unresolved': unresolved, 'parameters': parameters}


class GroupRepairTransform:
    def __init__(self, prepared_by_day):
        self.prepared = deepcopy(prepared_by_day)

    def __call__(self, day, catalog, cells):
        prepared = self.prepared[day]
        if (prepared.get('schema') != SCHEMA or prepared.get('day') != day
                or prepared['auxiliary_digests']['dated_catalog'] != digest(catalog)
                or prepared['auxiliary_digests']['stored_day_cells'] != digest(cells)):
            raise ValueError('group_repair_prepared_input_changed')
        return deepcopy({'patch': prepared['patch'], 'auxiliary_digests': prepared['auxiliary_digests']})


def load_repair_cost_basis(runtime, day, catalog, cells, *,
                           accepted_binding, authority, deadline):
    """Read exact accounting inputs under native or retained component authority.

    The caller captures with the old formula identity on the same source pin.
    Whole-token equality or explicit retained original component provenance is
    required; neither path silently binds a historical day to current inputs.
    """
    import time
    from packages.application.web_vitrina_group_blocks import accepted_cost_basis
    proof_claim = {'old_dependency_epoch': authority.get('old_epoch'),
        'captured_dependency_epoch': authority.get('fresh_epoch'),
        'old_day_proof': authority.get('old_token'), 'captured_day_proof': authority.get('fresh_token')}
    if time.monotonic() >= deadline:
        raise ValueError('group_repair_inputs_deadline')
    retained = authority.get('retained_cost_binding')
    if (not proof_claim.get('old_dependency_epoch')
            or proof_claim.get('old_dependency_epoch') != proof_claim.get('captured_dependency_epoch')
            or not proof_claim.get('old_day_proof')
            or (not retained and proof_claim.get('old_day_proof') != proof_claim.get('captured_day_proof'))):
        return {}, {'status':'unresolved_source_authority','proof_claim':deepcopy(proof_claim)}
    if not accepted_binding:
        return {}, {'status':'accepted_cost_binding_absent','proof_claim':deepcopy(proof_claim)}
    if retained:
        if (retained.get('authority_mode') != 'retained_original_cost_binding'
                or retained.get('day') != day
                or retained.get('old_day_proof') != proof_claim['old_day_proof']
                or retained.get('binding_digest') != digest(accepted_binding)):
            raise ValueError('group_repair_retained_binding_claim_invalid')
        from packages.application.web_vitrina_window_read_context import active_window_read_context
        from packages.application.fbs_accounting_runtime import path
        import json
        context = active_window_read_context()
        conn = context.borrow_book(path(runtime.runtime_dir)) if context else None
        if conn is None:
            raise ValueError('group_repair_retained_book_pin_required')
        row = conn.execute('SELECT payload FROM accounting_revisions WHERE version=?',
                           (accepted_binding['book_version'],)).fetchone()
        index = json.loads(row[0]) if row else {}
        projection = {field: dict(index.get(field, {})) for field in
                      ('shared_days', 'wb_days', 'retained_days', 'presentations')}
        if (digest(projection) != retained['book_index_digest']
                or index.get('presentations', {}).get(day) != retained['presentation_blob']):
            raise ValueError('group_repair_retained_book_changed')
    metadata={'fbs_accounting_bindings':{day:accepted_binding},
              'fbs_accounting_targets':{day:accepted_binding.get('ready_target')}}
    basis=accepted_cost_basis(runtime,metadata,[day]).get(day,{})
    if retained and (not basis or any(b.get('presentation_digest') != retained['presentation_blob']
                                     for b in basis.values())):
        raise ValueError('group_repair_retained_blob_changed')
    rows=unpack_rows(day,catalog,cells)
    costs=[r for r in rows if r.scope_kind=='SKU' and r.metric_key=='our_wb_unit_cost_rub'
           and accepted_number(r,day) is not None]
    mismatches=[]
    for r in costs:
        b=basis.get(r.nm_id,{})
        q,c=numeric(b.get('quantity')),numeric(b.get('capital'))
        observed=accepted_number(r,day)
        if (q is None or c is None or q<0 or c<0 or (q==0 and (c!=0 or observed!=0))
                or (q>0 and not math.isclose(c/q,observed,rel_tol=1e-9,abs_tol=1e-9))):
            mismatches.append(r.nm_id)
    if mismatches:
        return {}, {'status':'saved_cost_basis_mismatch','nm_ids':sorted(mismatches),
                    'proof_claim':deepcopy(proof_claim),'binding_digest':digest(accepted_binding)}
    if retained and not costs:
        return {}, {'status':'saved_cost_observations_absent','proof_claim':deepcopy(proof_claim)}
    if time.monotonic() >= deadline:
        raise ValueError('group_repair_inputs_deadline')
    return basis, {'status':'accepted_bound_basis_verified','checked_saved_costs':len(costs),
        'binding_digest':digest(accepted_binding),'basis_digest':digest({str(k):v for k,v in basis.items()}),
        'presentation_digests':sorted({b['presentation_digest'] for b in basis.values()}),
        'proof_claim':deepcopy(proof_claim),
        'authority_mode':'retained_original_cost_binding' if retained else 'native_day_token_match',
        'retained_cost_binding':deepcopy(retained)}


def retained_repair_cost_bindings(adapter, cache, *, cache_sha256,
                                  original_edition_id, original, served):
    """Select only original producer headers, never a newly available READY.

    The CLI pins the known migration-root cache and original candidate edition.
    This is explicit operational component provenance, NOT whole-token equality.
    Unchanged immutable day object/catalog/proof links it to the served snapshot.
    """
    import json
    from datetime import date, timedelta
    from packages.application.web_vitrina_window_read_context import borrowed_operational_connection
    from packages.application.web_vitrina_window_v3 import _ReadyHeader, _select_bindings
    from packages.application.web_vitrina_history_store import HistoryStore
    conn = borrowed_operational_connection(adapter.db_path)
    if conn is None or not adapter.context or cache.get('source') != adapter._quality_cache.get('source'):
        raise ValueError('group_repair_retained_source_pin_invalid')
    if digest(original) != original_edition_id or len(cache_sha256) != 64:
        raise ValueError('group_repair_retained_origin_invalid')
    identities = conn.execute('''SELECT s.bundle_version,s.as_of_date,s.snapshot_id,s.activated_at,s.refreshed_at,r.revision
        FROM sheet_vitrina_v1_ready_snapshots s LEFT JOIN sheet_vitrina_v1_ready_revisions r USING(bundle_version,as_of_date)
        ORDER BY s.activated_at DESC,s.refreshed_at DESC,s.as_of_date DESC,s.bundle_version DESC''').fetchall()
    headers, nodes, matched = [], {}, set()
    for item in identities:
        key = digest(list(item))
        node = cache['headers'].get(key)
        if node is None:
            continue
        matched.add(key)
        h = _ReadyHeader(*item[:5], tuple(node['dates']), json.dumps(node['book'], sort_keys=True), item[5])
        headers.append(h); nodes[h.key] = (node, list(item))
    # Missing, updated or re-dated old READY cannot quietly switch bindings.
    if matched != set(cache['headers']):
        raise ValueError('group_repair_retained_ready_changed')
    current = conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()[0]
    original_today = max(original['days'])
    default_date = (date.fromisoformat(original_today) - timedelta(days=1)).isoformat()
    bindings, _ = _select_bindings(conn, headers, adapter.days, current, default_date)
    original_proofs, served_proofs = HistoryStore.day_proofs(original), HistoryStore.day_proofs(served)
    original_catalogs, served_catalogs = HistoryStore.day_catalogs(original), HistoryStore.day_catalogs(served)
    result = {}
    for selected in bindings:
        day = selected.date
        # The original current day could have used accounting_current instead
        # of its READY binding. No retained component claim is made for it.
        if (day == original_today or original['days'].get(day) != served['days'].get(day)
                or original_catalogs.get(day) != served_catalogs.get(day)
                or original_proofs.get(day) != served_proofs.get(day)):
            continue
        node, identity = nodes.get(selected.source_key, ({}, []))
        binding = node.get('book', {}).get(day)
        if not binding:
            continue
        record = cache['book'].get(binding.get('book_version'), {})
        ref = record.get('index', {}).get('presentations', {}).get(day)
        target = dict(zip(('bundle_version', 'as_of_date'), identity[:2]))
        if (binding.get('ready_target') != target or not ref or ref not in record.get('verified', [])):
            raise ValueError('group_repair_retained_book_proof_missing')
        result[day] = (deepcopy(binding), {'authority_mode':'retained_original_cost_binding',
            'day':day, 'original_cache_sha256':cache_sha256,
            'original_candidate_edition':original_edition_id,
            'original_day_object':original['days'][day], 'dated_catalog':original_catalogs[day],
            'old_day_proof':original_proofs[day]['token'],
            'ready_identity_revision':identity, 'binding_digest':digest(binding),
            'book_index_digest':digest(record['index']), 'presentation_blob':ref})
    return result


def captured_repair_cost_bindings(adapter):
    """Use the native binding selector and freshly validated compact headers."""
    import json
    from packages.application.web_vitrina_window_read_context import borrowed_operational_connection
    from packages.application.web_vitrina_window_v3 import _ReadyHeader, _select_bindings
    from packages.application.sheet_vitrina_v1_web_vitrina import default_business_as_of_date
    conn=borrowed_operational_connection(adapter.db_path)
    if conn is None or not adapter._quality_cache or not adapter.context:
        raise ValueError('group_repair_fresh_pinned_capture_required')
    current=conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()
    identities=conn.execute('''SELECT s.bundle_version,s.as_of_date,s.snapshot_id,s.activated_at,s.refreshed_at,r.revision
        FROM sheet_vitrina_v1_ready_snapshots s LEFT JOIN sheet_vitrina_v1_ready_revisions r USING(bundle_version,as_of_date)
        ORDER BY s.activated_at DESC,s.refreshed_at DESC,s.as_of_date DESC,s.bundle_version DESC''').fetchall()
    headers=[];nodes={}
    for item in identities:
        node=adapter._quality_cache['headers'].get(digest(list(item)))
        if node is None:
            raise ValueError('group_repair_captured_header_changed')
        header=_ReadyHeader(*item[:5],tuple(node['dates']),json.dumps(node['book'],sort_keys=True),item[5])
        headers.append(header);nodes[header.key]=node
    bindings,_=_select_bindings(conn,headers,adapter.days,current[0],default_business_as_of_date(adapter.now))
    return {b.date:deepcopy(nodes[b.source_key]['book'].get(b.date)) if b.source_key else None for b in bindings}
