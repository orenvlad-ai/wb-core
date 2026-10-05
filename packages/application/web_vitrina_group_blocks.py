"""Group the final dated SKU operands with the native TOTAL semantics.

No provider calls, no proportionate allocation of account-only values. The
caller pins the native read context; membership is today's reporting registry.
"""
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
import math

from packages.application.sheet_vitrina_v1_live_plan import _MetricEvaluator, TemporalLiveSources
from packages.application.sheet_vitrina_v1_buyout_percent import (
    BUYOUT_PERCENT_METRIC_KEY, aggregate_buyout_percent, load_buyout_percent_snapshot_metrics,
    buyout_snapshot_is_mature,
)
from packages.application.sheet_vitrina_v1_authenticated_buyer import AVG_EFFECTIVE_DISCOUNT_METRIC_KEY, EFFECTIVE_DISCOUNT_METRIC_KEY
from packages.application.sheet_vitrina_v1_card_rating import TOTAL_METRIC_KEY as RATING_TOTAL, SKU_METRIC_KEY as RATING_SKU
from packages.application.canonical_wb_cost_resolver import load_canonical_wb_cost_lookup
from packages.application.own_product_capital import OwnProductCapitalBlock, OWN_PRODUCT_CAPITAL_STAGES, own_stage_metric_key
from packages.application.inventory_cost_blend import build_inventory_cost_blend_lookup
from packages.application.web_vitrina_window_read_context import borrowed_operational_connection

UNALLOCATED_METRICS = frozenset({'fin_storage_fee_total'})


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


class AcceptedGroupEvaluator(_MetricEvaluator):
    """Resolve existing SKU observations, never recalculate an unobserved SKU."""
    def __init__(self, *, rows, **kwargs):
        super().__init__(**kwargs)
        self.accepted = {(r.nm_id, r.metric_key): r for r in rows if r.scope_kind == 'SKU'}
        self.operand_proofs = {}
        self.visited_operands = set()

    def resolve_sku(self, key, nm_id, slot):
        if (key != 'ads_sum' and key in self.ads_dependent_metrics
                and self._partial_ads_slot(slot)
                and self.resolve_sku('ads_sum', nm_id, slot) is None):
            return None
        row = self.accepted.get((nm_id, key))
        if row is not None:
            self.visited_operands.add((nm_id, key))
            return numeric(row.values_by_date.get(slot))
        metric = self.metrics_by_key.get(key)
        # TOTAL ratio/formula dependencies refer to TOTAL aliases. Resolve their
        # underlying SKU operand, not a sum of already calculated percentages.
        if metric and metric.calc_type == 'metric' and metric.calc_ref != key:
            return self.resolve_sku(metric.calc_ref, nm_id, slot)
        return None

    def resolve_total(self, key, slot):
        cached = self.operand_proofs.get((slot, key))
        if cached is not None:
            self.visited_operands.update(cached)
            return super().resolve_total(key, slot)
        before = self.visited_operands
        self.visited_operands = set()
        try:
            result = super().resolve_total(key, slot)
            visited = set(self.visited_operands)
            self.operand_proofs[(slot, key)] = visited
        finally:
            before.update(self.visited_operands)
            self.visited_operands = before
        return result

    def _complete_share_inputs(self, stages, slot, field):
        lookup = self._slot_lookups(slot).own_product_capital_lookup
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
                       runtime, today, parameters3, parameters4, quality_resolver=None, source_statuses=()):
    totals = [row for row in rows if row.scope_kind == 'TOTAL']
    # Replace any pre-existing legacy GROUP projections with this single path.
    result = [row for row in rows if row.scope_kind != 'GROUP']
    members = {g['group_key']: [x for x in config if x.group == g['group_key']] for g in groups}
    conn = borrowed_operational_connection(runtime.db_path)
    if conn is None:
        raise ValueError('group_blocks_requires_pinned_context')
    cost = {day: load_canonical_wb_cost_lookup(conn, as_of_date=date.fromisoformat(day)) for day in dates}
    # Native group shares weight the final dated stage quantities. The same
    # native aggregate receives the final SKU confirmed/coverage observations;
    # no source replays or lazy write-capable legacy capital reader are invoked.
    own = {day: {} for day in dates}
    for row in rows:
        if row.scope_kind != 'SKU':
            continue
        for day in dates:
            own[day].setdefault(row.nm_id, {})[row.metric_key] = numeric(row.values_by_date.get(day))
    for day in dates:
        for values in own[day].values():
            for stage in OWN_PRODUCT_CAPITAL_STAGES:
                qty = values.get(own_stage_metric_key(stage, 'qty'))
                for share, count in [('confirmed_share_pct', 'confirmed_qty'), ('cost_coverage_pct', 'cost_covered_qty')]:
                    ratio = values.get(own_stage_metric_key(stage, share))
                    values[own_stage_metric_key(stage, count)] = qty * ratio if qty is not None and ratio is not None else None
    # Post-cutover WAC must use the very same native functional/facility
    # evidence as TOTAL. Never substitute scalar WB-only cost for WB+FF.
    capital = OwnProductCapitalBlock(runtime=runtime)
    for day in dates:
        arguments = {"requested_nm_ids": [x.nm_id for x in config],
                     "revalidate_current_sources": True}
        if quality_resolver is not None:
            arguments["lifecycle_quality_resolver"] = quality_resolver
        functional = capital._load_functional_daily_metric_lookup(day, **arguments)
        if functional is not None:
            own[day] = capital.load_daily_metric_lookup(day, **arguments)
        elif day >= "2026-07-01":
            canonical = capital._load_canonical_daily_metric_lookup(day)
            if canonical:
                own[day] = canonical
        cost[day] = build_inventory_cost_blend_lookup(as_of_date=day,
            wb_compat_lookup=cost[day], product_capital_lookup=own[day])
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
        lookups = {day: SimpleNamespace(column_date=day, our_wb_cost_lookup=cost[day],
            own_product_capital_lookup=own[day], fin_storage_fee_total=None,
            order_price_lookup={x.nm_id: {'orderSum': numeric(next((r.values_by_date.get(day) for r in group_rows if r.nm_id == x.nm_id and r.metric_key == 'orderSum'), None)),
                'orderCount': numeric(next((r.values_by_date.get(day) for r in group_rows if r.nm_id == x.nm_id and r.metric_key == 'orderCount'), None))} for x in selected}) for day in dates}
        evaluator = AcceptedGroupEvaluator(rows=group_rows, enabled_config=selected,
            metrics_by_key=metrics, formulas_by_id=formulas,
            live_sources=TemporalLiveSources([], [SimpleNamespace(**s) for s in source_statuses], lookups, {}),
            proxy_parameters_resolver=parameters3, proxy_v4_parameters_resolver=parameters4)
        for total in totals:
            metric = total.metric_key
            values, presentation = {}, {}
            for day in dates:
                operand_key = metric.removeprefix('total_')
                operands = [r for r in group_rows if r.metric_key == operand_key]
                numeric_operands = [numeric(r.values_by_date.get(day)) for r in operands]
                if metric in UNALLOCATED_METRICS:
                    value, reason = None, 'Нет подтверждённого распределения по группам.'
                elif metric == BUYOUT_PERCENT_METRIC_KEY:
                    pairs = [buyout.get(x.nm_id, {}).get(day) for x in selected]
                    value = aggregate_buyout_percent((p.buyout_percent, p.order_count) if p else (None, None) for p in pairs).value if day in mature else None
                    reason = 'Выкуп: взвешенный итог наблюдений группы; незрелая дата не подтверждена.'
                elif metric in {AVG_EFFECTIVE_DISCOUNT_METRIC_KEY, RATING_TOTAL}:
                    source = EFFECTIVE_DISCOUNT_METRIC_KEY if metric == AVG_EFFECTIVE_DISCOUNT_METRIC_KEY else RATING_SKU
                    operands = [r for r in group_rows if r.metric_key == source]
                    available = [v for r in operands if (v := numeric(r.values_by_date.get(day))) is not None]
                    value = sum(available) / len(available) if available else None
                    reason = f'Среднее доступных наблюдений группы: {len(available)} из {len(selected)} SKU.'
                elif operand_key.startswith('inventory_') or operand_key in {'stock_total', 'stock_wb_total'}:
                    # The final inventory overlay has already applied typed,
                    # finalized/current semantics to every SKU/facility cell.
                    value = sum(numeric_operands) if len(operands) == len(selected) and all(v is not None for v in numeric_operands) else None
                    reason = 'Сумма окончательных дневных складских наблюдений членов группы; неполные входы не равны нулю.'
                elif metric in metrics:
                    evaluator.visited_operands.clear()
                    value = evaluator.resolve_total(metric, day)
                    operands = [evaluator.accepted[k] for k in evaluator.visited_operands]
                    reason = 'Нативный итог по текущему составу группы и окончательным дневным SKU-операндам.'
                else:
                    value, reason = None, 'Для этой метрики нет подтверждённого распределения по группам.'
                values[day] = '' if value is None else float(value)
                affected = [r.presentation_by_date.get(day, {}) for r in operands]
                partial_ads = metric in evaluator.ads_dependent_metrics and evaluator._partial_ads_slot(day)
                uncertain = partial_ads or any(p.get('state') in {'unavailable', 'unconfirmed'} or p.get('quality_state') in {'missing', 'partial', 'stale', 'historical_repair_required'} for p in affected)
                if metric in metrics and any(token in metric for token in ('own_', 'proxy_', 'wb_unit_cost')):
                    uncertain = uncertain or any(own[day].get(x.nm_id, {}).get('presentation_state') in {'unconfirmed', 'unavailable'}
                        or 'provisional' in str(cost[day].get(x.nm_id, {}).get('source_status', '')) for x in selected)
                presentation[day] = {'state': 'unavailable' if value is None else 'unconfirmed' if uncertain else 'available',
                    'tone': 'neutral' if value is None else 'warning' if uncertain else 'neutral',
                    'quality_state': 'unallocated' if metric in UNALLOCATED_METRICS else 'missing' if value is None else 'partial' if uncertain else 'exact',
                    'source': 'native_reporting_group_v1', 'source_as_of_date': day,
                    'reason': reason, 'quality_reason': reason,
                    'group_key': key, 'calculation_scope': [f'SKU:{x.nm_id}' for x in selected]}
            result.append(replace(total, row_id=f'GROUP:{key}|{metric}', scope_kind='GROUP',
                scope_key=f'GROUP:{key}', scope_label=group['label'], group=key, nm_id=None,
                row_order=len(result)+1, values_by_date=values, presentation_by_date=presentation))
    return result
