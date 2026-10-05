"""Group the final dated SKU operands with the native TOTAL semantics.

No provider calls, no proportionate allocation of account-only values. The
caller pins the native read context; membership is today's reporting registry.
"""
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
import math
import json
import zlib

from packages.application.sheet_vitrina_v1_live_plan import _MetricEvaluator, TemporalLiveSources
from packages.application.sheet_vitrina_v1_buyout_percent import (
    BUYOUT_PERCENT_METRIC_KEY, aggregate_buyout_percent, load_buyout_percent_snapshot_metrics,
    buyout_snapshot_is_mature,
)
from packages.application.sheet_vitrina_v1_authenticated_buyer import AVG_EFFECTIVE_DISCOUNT_METRIC_KEY, EFFECTIVE_DISCOUNT_METRIC_KEY
from packages.application.sheet_vitrina_v1_card_rating import TOTAL_METRIC_KEY as RATING_TOTAL, SKU_METRIC_KEY as RATING_SKU
from packages.application.own_product_capital import OWN_PRODUCT_CAPITAL_STAGES, own_stage_metric_key
from packages.application.web_vitrina_window_read_context import borrowed_operational_connection

UNALLOCATED_METRICS = frozenset({'fin_storage_fee_total'})
PROXY_TOTALS = {
    'total_proxy_profit_3_rub': (3, 'profit'),
    'proxy_margin_3_pct_total': (3, 'margin'),
    'total_proxy_profit_4_rub': (4, 'profit'),
    'proxy_margin_4_pct_total': (4, 'margin'),
    'proxy_margin_per_unit_rub_total': (4, 'per_unit'),
}
CTR_LABEL = 'Открытия / показы выдачи, %'
CTR_REASON = ('Отношение открытий карточки к показам в выдаче по одному составу SKU. '
              'Открытия по прямым ссылкам и артикулам не входят в показы выдачи, '
              'поэтому значение может превышать 100%.')


def accepted_source_statuses(snapshot, *, column_date, requested_date=None):
    """Carry the accepted STATUS slot onto the requested dated evaluator slot."""
    slot_keys = {slot.slot_key for slot in snapshot.temporal_slots
                 if slot.column_date == column_date}
    result = []
    for sheet in snapshot.sheets:
        if sheet.sheet_name != 'STATUS':
            continue
        for row in sheet.rows:
            values = dict(zip(sheet.header, row))
            source, separator, slot = str(values.get('source_key') or '').partition('[')
            if not separator or slot.removesuffix(']') not in slot_keys:
                continue
            result.append({'source_key': source, 'kind': str(values.get('kind') or ''),
                           'temporal_slot': requested_date or column_date})
    return result


def numeric(value):
    if isinstance(value, bool) or value in ('', None):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def accepted_number(row, day):
    if row is None or row.presentation_by_date.get(day, {}).get('state') == 'unavailable':
        return None
    return numeric(row.values_by_date.get(day))


def input_incomplete(presentation):
    return (presentation.get('state') == 'unavailable'
            or presentation.get('quality_state') in {'missing', 'partial', 'stale', 'historical_repair_required'}
            or presentation.get('completeness_state') in {'partial', 'unknown_scope'}
            or (presentation.get('missing_sku_count') is None and 'missing_sku_count' in presentation)
            or bool(presentation.get('missing_sku_count')))


def accepted_cost_basis(runtime, metadata, dates):
    """Read only exact READY-bound immutable presentation blobs, never latest."""
    from packages.application.fbs_accounting_runtime import path, SOURCE
    from packages.application.fbs_snapshot_cost import fingerprint
    from packages.application.web_vitrina_window_read_context import active_window_read_context
    context = active_window_read_context()
    if context is None:
        raise ValueError('group_cost_basis_requires_pinned_context')
    bindings = metadata.get('fbs_accounting_bindings', {})
    needed = [day for day in dates if day in bindings]
    if not needed:
        return {}
    conn = context.borrow_book(path(runtime.runtime_dir))
    if conn is None:
        raise ValueError('group_cost_basis_bound_book_missing')
    result = {}
    for day in needed:
        binding = bindings[day]
        target = metadata.get('fbs_accounting_targets', {}).get(day, metadata.get('ready_publication_target'))
        if (not target or binding.get('ready_target') != target or binding.get('date') != day
                or binding.get('source') != SOURCE):
            raise ValueError('group_cost_basis_ready_binding_mismatch')
        row = conn.execute('SELECT payload FROM accounting_revisions WHERE version=?',
                           (binding['book_version'],)).fetchone()
        if row is None:
            raise ValueError('group_cost_basis_bound_revision_missing')
        index = json.loads(row[0])
        blob_digest = index.get('presentations', {}).get(day)
        blob = conn.execute('SELECT payload FROM accounting_blobs WHERE digest=?', (blob_digest,)).fetchone()
        if blob is None:
            raise ValueError('group_cost_basis_bound_presentation_missing')
        payload = json.loads(zlib.decompress(blob[0]))
        if (fingerprint(payload) != blob_digest or payload.get('date') != day
                or payload.get('version_id') != binding.get('presentation_version')
                or payload.get('quality') != binding.get('quality')
                or index.get('effective_date') != binding.get('effective_date')):
            raise ValueError('group_cost_basis_bound_presentation_mismatch')
        # Native shared-cost books name the admitted capital `capital_rub`.
        # The evaluator/repair internal basis uses `capital`; normalize once,
        # without substituting displayed WB/FF overlays or inferred weights.
        result[day] = {int(nm): {**value.get('shared_cost', {}),
            'capital': value.get('shared_cost', {}).get('capital_rub'),
            'presentation_version': payload['version_id'], 'presentation_digest': blob_digest,
            'book_version': binding['book_version']} for nm, value in payload.get('rows', {}).items()}
    return result


class AcceptedGroupEvaluator(_MetricEvaluator):
    """Resolve existing SKU observations, never recalculate an unobserved SKU."""
    def __init__(self, *, rows, cost_basis=None, **kwargs):
        self.cost_basis = cost_basis or {}
        super().__init__(**kwargs)
        self.accepted = {(r.nm_id, r.metric_key): r for r in rows if r.scope_kind == 'SKU'}
        self.operand_proofs = {}
        self.visited_operands = set()
        self.aggregation_evidence = {}

    def resolve_sku(self, key, nm_id, slot):
        if (key != 'ads_sum' and key in self.ads_dependent_metrics
                and self._partial_ads_slot(slot)
                and self.resolve_sku('ads_sum', nm_id, slot) is None):
            return None
        row = self.accepted.get((nm_id, key))
        if row is not None:
            self.visited_operands.add((nm_id, key))
            return accepted_number(row, slot)
        metric = self.metrics_by_key.get(key)
        # TOTAL ratio/formula dependencies refer to TOTAL aliases. Resolve their
        # underlying SKU operand, not a sum of already calculated percentages.
        if metric and metric.calc_type == 'metric' and metric.calc_ref != key:
            return self.resolve_sku(metric.calc_ref, nm_id, slot)
        self.visited_operands.add((nm_id, key))
        return None

    def resolve_total(self, key, slot):
        cached = self.operand_proofs.get((slot, key))
        if (slot, key) in self.total_cache:
            self.visited_operands.update(cached or ())
            return self.total_cache[(slot, key)]
        before = self.visited_operands
        self.visited_operands = set()
        try:
            if key == 'weighted_price_seller_discounted':
                for item in self.enabled_config:
                    for operand in ('orderSum', 'orderCount'):
                        self.resolve_sku(operand, item.nm_id, slot)
            if key in PROXY_TOTALS:
                version, output = PROXY_TOTALS[key]
                result = self._accepted_proxy(version, slot)[output]
            elif key == 'total_our_wb_unit_cost_rub':
                result = self._accepted_cost(slot)
            elif key == 'total_our_wb_cost_confirmed_share_pct':
                result = self._accepted_cost(slot, confirmed_share=True)
            elif key == 'ctr':
                result = self._accepted_ctr(slot)
            else:
                definition = self.metrics_by_key.get(key)
                # A direct SKU scalar has no implicit TOTAL sum semantics.
                # In particular, source percentages must never be added.
                if (definition and definition.scope == 'SKU' and definition.calc_type == 'metric'
                        and definition.calc_ref == key
                        and (definition.format in {'percent', 'rating', 'rub_per_unit'}
                             or 'unit_cost' in key or 'price' in key)):
                    result = None
                else:
                    result = super().resolve_total(key, slot)
            self.total_cache[(slot, key)] = result
            visited = set(self.visited_operands)
            self.operand_proofs[(slot, key)] = visited
        finally:
            before.update(self.visited_operands)
            self.visited_operands = before
        return result

    def _accepted_proxy(self, version, slot):
        # The final SKU profit is already calculated by the dated native/managed
        # projection. Requiring a fresh private WAC here changes its cohort.
        parameters = self._proxy_parameters(slot) if version == 3 else self._proxy_v4_parameters(slot)
        rate = numeric(getattr(parameters, 'buyout_rate', None))
        eligible, missing = [], []
        for item in self.enabled_config:
            nm = item.nm_id
            profit = self.resolve_sku(f'proxy_profit_{version}_rub', nm, slot)
            revenue = self.resolve_sku('orderSum', nm, slot)
            quantity = self.resolve_sku('orderCount', nm, slot)
            ads = self.resolve_sku('ads_sum', nm, slot)
            if (profit is None or revenue is None or quantity is None or ads is None
                    or min(revenue, quantity, ads) < 0 or (quantity == 0 and revenue != 0)):
                missing.append(nm)
                continue
            eligible.append((nm, profit, revenue, quantity))
        self.aggregation_evidence[(slot, version)] = {
            'eligible_nm_ids': [x[0] for x in eligible], 'missing_nm_ids': missing,
            'parameter_version': getattr(parameters, 'version_id', None), 'buyout_rate': rate}
        profit = sum(x[1] for x in eligible) if eligible else None
        revenue = sum(x[2] for x in eligible) * rate if rate is not None and rate > 0 else None
        quantity = sum(x[3] for x in eligible) * rate if rate is not None and rate > 0 else None
        return {'profit': profit, 'margin': profit / revenue if profit is not None and revenue else None,
                'per_unit': profit / quantity if profit is not None and quantity else None}

    def _accepted_cost(self, slot, *, confirmed_share=False):
        # The accounting presentation retains the actual WB/FF_FBS/FF_FBO
        # basis of this accepted WAC. Public stage cells may be a later overlay.
        capital = quantity = confirmed = 0.0
        included, missing = [], []
        for item in self.enabled_config:
            nm = item.nm_id
            cost = self.resolve_sku('our_wb_unit_cost_rub', nm, slot)
            basis = self.cost_basis.get(slot, {}).get(nm, {})
            q, c = numeric(basis.get('quantity')), numeric(basis.get('capital'))
            row = self.accepted.get((nm, 'our_wb_unit_cost_rub'))
            cell_version = row.presentation_by_date.get(slot, {}).get('source_version_id') if row else None
            if (q is None or c is None or q < 0 or c < 0 or (q == 0 and c != 0)
                    or not basis.get('source_digest') or not basis.get('presentation_digest')
                    or (cell_version is not None and cell_version != basis.get('presentation_version'))):
                missing.append(nm)
                continue
            if q == 0:
                continue
            if cost is None or not math.isclose(cost, c / q, rel_tol=1e-9, abs_tol=1e-9):
                missing.append(nm)
                continue
            share = self.resolve_sku('our_wb_cost_confirmed_share_pct', nm, slot) if confirmed_share else None
            if confirmed_share and (share is None or not 0 <= share <= 1):
                missing.append(nm)
                continue
            if confirmed_share:
                confirmed += q * share
            included.append(nm)
            quantity += q
            capital += c
        self.aggregation_evidence[(slot, 'confirmed_cost' if confirmed_share else 'cost')] = {
            'eligible_nm_ids': included, 'missing_nm_ids': missing,
            'basis': 'accepted_bound_shared_cost_capital_over_quantity',
            'input_digests': sorted({self.cost_basis[slot][nm]['presentation_digest'] for nm in included})}
        return (confirmed if confirmed_share else capital) / quantity if quantity > 0 else None

    def _accepted_ctr(self, slot):
        pairs, missing = [], []
        for item in self.enabled_config:
            opens = self.resolve_sku('open_card_count', item.nm_id, slot)
            views = self.resolve_sku('view_count', item.nm_id, slot)
            if opens is None or views is None or opens < 0 or views < 0:
                missing.append(item.nm_id)
            else:
                pairs.append((item.nm_id, opens, views))
        self.aggregation_evidence[(slot, 'ctr')] = {
            'eligible_nm_ids': [x[0] for x in pairs], 'missing_nm_ids': missing,
            'zero_denominator': bool(pairs) and sum(x[2] for x in pairs) == 0}
        denominator = sum(x[2] for x in pairs)
        return sum(x[1] for x in pairs) / denominator if denominator > 0 else None

    def _complete_share_inputs(self, stages, slot, field):
        lookup = self._slot_lookups(slot).own_product_capital_lookup
        for item in self.enabled_config:
            for stage in stages:
                self.resolve_sku(own_stage_metric_key(stage, 'qty'), item.nm_id, slot)
                self.resolve_sku(own_stage_metric_key(stage, 'confirmed_share_pct' if field == 'confirmed_qty' else 'cost_coverage_pct'), item.nm_id, slot)
        return all(numeric(lookup.get(item.nm_id, {}).get(own_stage_metric_key(stage, "qty"))) is not None
                   and (numeric(lookup[item.nm_id][own_stage_metric_key(stage, "qty")]) <= 0
                        or numeric(lookup[item.nm_id].get(own_stage_metric_key(stage, field))) is not None)
                   for item in self.enabled_config for stage in stages)

    def _aggregate_own_stage_confirmed_share(self, stage, slot):
        return super()._aggregate_own_stage_confirmed_share(stage, slot) if self._complete_share_inputs([stage], slot, "confirmed_qty") else None

    def _aggregate_own_stage_cost_coverage(self, stage, slot):
        return super()._aggregate_own_stage_cost_coverage(stage, slot) if self._complete_share_inputs([stage], slot, "cost_covered_qty") else None

    def _aggregate_own_product_capital_confirmed_share(self, slot):
        return super()._aggregate_own_product_capital_confirmed_share(slot) if self._complete_share_inputs(OWN_PRODUCT_CAPITAL_STAGES, slot, "confirmed_qty") else None


def include_group_rows(rows, *, groups, config, metrics, formulas, dates,
                       runtime, today, parameters3, parameters4, quality_resolver=None, source_statuses=(), cost_basis=None):
    totals = [row for row in rows if row.scope_kind == 'TOTAL']
    # Replace any pre-existing legacy GROUP projections with this single path.
    result = [row for row in rows if row.scope_kind != 'GROUP']
    members = {g['group_key']: [x for x in config if x.group == g['group_key']] for g in groups}
    conn = borrowed_operational_connection(runtime.db_path)
    if conn is None:
        raise ValueError('group_blocks_requires_pinned_context')
    # Native group shares weight the final dated stage quantities. The same
    # native aggregate receives the final SKU confirmed/coverage observations;
    # no source replays or lazy write-capable legacy capital reader are invoked.
    own = {day: {} for day in dates}
    for row in rows:
        if row.scope_kind != 'SKU':
            continue
        for day in dates:
            value = accepted_number(row, day)
            own[day].setdefault(row.nm_id, {})[row.metric_key] = value
    for day in dates:
        for values in own[day].values():
            for stage in OWN_PRODUCT_CAPITAL_STAGES:
                qty = values.get(own_stage_metric_key(stage, 'qty'))
                for share, count in [('confirmed_share_pct', 'confirmed_qty'), ('cost_coverage_pct', 'cost_covered_qty')]:
                    ratio = values.get(own_stage_metric_key(stage, share))
                    values[own_stage_metric_key(stage, count)] = qty * ratio if qty is not None and ratio is not None else None
    mature = [day for day in dates if buyout_snapshot_is_mature(snapshot_date=day, today=date.fromisoformat(today))]
    buyout = load_buyout_percent_snapshot_metrics(runtime=runtime, snapshot_dates=mature,
        nm_ids=[x.nm_id for x in config], require_mature_capture=True)
    for group in groups:
        key = group['group_key']
        selected = members[key]
        if not selected:
            continue  # Empty registry groups are declared in metadata, not zero rows.
        selected_ids = {x.nm_id for x in selected}
        group_rows = [r for r in rows if r.scope_kind == 'SKU' and r.nm_id in selected_ids]
        lookups = {day: SimpleNamespace(column_date=day, our_wb_cost_lookup={},
            own_product_capital_lookup=own[day], fin_storage_fee_total=None,
            order_price_lookup={x.nm_id: {'orderSum': accepted_number(next((r for r in group_rows if r.nm_id == x.nm_id and r.metric_key == 'orderSum'), None), day),
                'orderCount': accepted_number(next((r for r in group_rows if r.nm_id == x.nm_id and r.metric_key == 'orderCount'), None), day)} for x in selected}) for day in dates}
        evaluator = AcceptedGroupEvaluator(rows=group_rows, cost_basis=cost_basis, enabled_config=selected,
            metrics_by_key=metrics, formulas_by_id=formulas,
            live_sources=TemporalLiveSources([], [SimpleNamespace(**s) for s in source_statuses], lookups, {}),
            proxy_parameters_resolver=parameters3, proxy_v4_parameters_resolver=parameters4)
        for total in totals:
            metric = total.metric_key
            values, presentation = {}, {}
            for day in dates:
                evaluator.visited_operands.clear()
                operand_key = metric.removeprefix('total_')
                operands = [r for r in group_rows if r.metric_key == operand_key]
                numeric_operands = [accepted_number(r, day) for r in operands]
                if metric in UNALLOCATED_METRICS:
                    value, reason = None, 'Нет подтверждённого распределения по группам.'
                elif metric == BUYOUT_PERCENT_METRIC_KEY:
                    pairs = [buyout.get(x.nm_id, {}).get(day) for x in selected]
                    value = aggregate_buyout_percent((p.buyout_percent, p.order_count) if p else (None, None) for p in pairs).value if day in mature else None
                    reason = 'Выкуп: взвешенный итог наблюдений группы; незрелая дата не подтверждена.'
                elif metric in {AVG_EFFECTIVE_DISCOUNT_METRIC_KEY, RATING_TOTAL}:
                    source = EFFECTIVE_DISCOUNT_METRIC_KEY if metric == AVG_EFFECTIVE_DISCOUNT_METRIC_KEY else RATING_SKU
                    operands = [r for r in group_rows if r.metric_key == source]
                    available = [v for r in operands if (v := accepted_number(r, day)) is not None]
                    value = sum(available) / len(available) if available else None
                    evaluator.aggregation_evidence[(day, metric)] = {'missing_nm_ids': sorted(selected_ids - {r.nm_id for r in operands if accepted_number(r, day) is not None})}
                    reason = f'Среднее доступных наблюдений группы: {len(available)} из {len(selected)} SKU.'
                elif operand_key.startswith('inventory_') or operand_key in {'stock_total', 'stock_wb_total'}:
                    # The final inventory overlay has already applied typed,
                    # finalized/current semantics to every SKU/facility cell.
                    available = [v for v in numeric_operands if v is not None]
                    value = sum(available) if available else None
                    missing_inventory = {x.nm_id for x in selected} - {r.nm_id for r in operands if accepted_number(r, day) is not None}
                    evaluator.aggregation_evidence[(day, metric)] = {'missing_nm_ids': sorted(missing_inventory),
                        'eligible_nm_ids': sorted({r.nm_id for r in operands if accepted_number(r, day) is not None})}
                    reason = 'Сумма доступных окончательных дневных складских наблюдений членов группы; неполные входы не равны нулю.'
                elif metric in metrics:
                    evaluator.visited_operands.clear()
                    value = evaluator.resolve_total(metric, day)
                    operands = [evaluator.accepted[k] for k in evaluator.visited_operands if k in evaluator.accepted]
                    reason = CTR_REASON if metric == 'ctr' else 'Нативный итог по текущему составу группы и окончательным дневным SKU-операндам.'
                    evidence_key = PROXY_TOTALS[metric][0] if metric in PROXY_TOTALS else 'cost' if metric == 'total_our_wb_unit_cost_rub' else 'confirmed_cost' if metric == 'total_our_wb_cost_confirmed_share_pct' else 'ctr' if metric == 'ctr' else None
                    evidence = evaluator.aggregation_evidence.get((day, evidence_key), {})
                    if evidence.get('missing_nm_ids'):
                        reason += ' Итог по доступным согласованным входам; неизвестные вклады не равны нулю.'
                    if metric == 'total_our_wb_unit_cost_rub' and value is None:
                        reason += ' Нет сохранённого согласованного количества и капитала для веса себестоимости.'
                else:
                    value, reason = None, 'Для этой метрики нет подтверждённого распределения по группам.'
                values[day] = '' if value is None else float(value)
                affected = [r.presentation_by_date.get(day, {}) for r in operands]
                aggregation = evaluator.aggregation_evidence.get((day, PROXY_TOTALS[metric][0] if metric in PROXY_TOTALS else 'cost' if metric == 'total_our_wb_unit_cost_rub' else 'confirmed_cost' if metric == 'total_our_wb_cost_confirmed_share_pct' else 'ctr' if metric == 'ctr' else metric), {})
                partial_ads = metric in evaluator.ads_dependent_metrics and evaluator._partial_ads_slot(day)
                missing_members = set(aggregation.get('missing_nm_ids', []))
                if metric in metrics and metric not in UNALLOCATED_METRICS:
                    missing_members.update(nm for nm, key in evaluator.visited_operands if (nm, key) not in evaluator.accepted)
                missing_members.update(r.nm_id for r in operands if accepted_number(r, day) is None)
                missing_members.update(r.nm_id for r in operands if input_incomplete(r.presentation_by_date.get(day, {})))
                unknown_scope = any(p.get('completeness_state') == 'unknown_scope'
                    or ('missing_sku_count' in p and p['missing_sku_count'] is None) for p in affected)
                partial = bool(missing_members) or partial_ads or unknown_scope
                preliminary = any(p.get('state') == 'unconfirmed' or p.get('quality_state') in {'preliminary', 'management_estimate'} for p in affected)
                uncertain = partial or preliminary
                inherited_reasons = list(dict.fromkeys(str(p.get('quality_reason') or p.get('reason') or '')
                    for p in affected if input_incomplete(p)))
                reason = ' '.join([reason, *[r for r in inherited_reasons if r][:3]])
                undefined = value is None and aggregation.get('zero_denominator', False)
                if undefined:
                    reason += ' Нулевой знаменатель: отношение не определено; это не отсутствие числителя.'
                presentation[day] = {'state': 'unavailable' if value is None else 'unconfirmed' if uncertain else 'available',
                    'tone': 'neutral' if value is None else 'warning' if uncertain else 'neutral',
                    'quality_state': 'unallocated' if metric in UNALLOCATED_METRICS else 'undefined' if undefined else 'missing' if value is None else 'partial' if partial else 'preliminary' if preliminary else 'exact',
                    'source': 'native_reporting_group_v1', 'source_as_of_date': day,
                    'completeness_state': 'unknown_scope' if unknown_scope else 'partial' if partial else 'complete',
                    'missing_sku_count': None if unknown_scope else len(missing_members),
                    'reason': reason, 'quality_reason': reason,
                    'group_key': key, 'aggregation_evidence': aggregation, 'calculation_scope': [f'SKU:{x.nm_id}' for x in selected]}
            result.append(replace(total, row_id=f'GROUP:{key}|{metric}', scope_kind='GROUP',
                scope_key=f'GROUP:{key}', scope_label=group['label'], group=key, nm_id=None,
                row_order=len(result)+1, metric_label=CTR_LABEL if metric == 'ctr' else total.metric_label,
                values_by_date=values, presentation_by_date=presentation))
    return result
