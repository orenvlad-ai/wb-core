"""Presentation of dated metric coverage; it never fills a missing value."""
import re
from dataclasses import replace

ADS_FIELDS = {'ads_views', 'ads_clicks', 'ads_atbs', 'ads_orders', 'ads_sum',
              'ads_sum_price', 'ads_cpc', 'ads_ctr', 'ads_cr'}
ADS_ECONOMICS = {'proxy_profit_3_rub', 'proxy_margin_3_pct', 'proxy_profit_4_rub',
                 'proxy_margin_4_pct', 'proxy_margin_per_unit_rub',
                 'total_proxy_profit_3_rub', 'proxy_margin_3_pct_total',
                 'total_proxy_profit_4_rub', 'proxy_margin_4_pct_total',
                 'proxy_margin_per_unit_rub_total'}


def ads_dependencies(metrics, formulas):
    affected = set(ADS_FIELDS | ADS_ECONOMICS)
    while True:
        before = set(affected)
        for key, metric in metrics.items():
            if metric.calc_type == 'formula':
                formula = formulas.get(metric.calc_ref)
                refs = set(re.findall(r'\{([^}]+)\}', formula.expression)) if formula else set()
            else:
                refs = set(metric.calc_ref.split('/'))
            if refs & affected:
                affected.add(key)
        if before == affected:
            return affected


def ads_partial_presentation(*, rows, slots, statuses, metrics, formulas):
    affected = ads_dependencies(metrics, formulas)
    result = {}
    for status in statuses:
        d = status.diagnostics or {}
        if (status.source_key != 'ads_compact' or status.kind != 'incomplete'
                or d.get('partial_observation_contract') != 'ads_partial_observed_v1'):
            continue
        day = status.column_date
        observed = d.get('source_observed_at', '')
        unresolved = d.get('unresolved_campaign_ids', [])
        reason = ('Реклама за ' + day + ': наблюдаемый вклад кампаний; полный вклад неизвестен. '
                  'Неизвестные кампании: ' + ', '.join(map(str, unresolved)) + '. '
                  'Датированный состав кампаний и затронутых SKU не доказан; точное число SKU неизвестно. '
                  'Наблюдение: ' + observed + '.')
        for row in rows:
            if row[1].split('|', 1)[1] not in affected:
                continue
            result.setdefault(row[1], {})[day] = {
                'source': 'ads_partial_observed_v1', 'source_as_of_date': day,
                'source_observed_at': observed, 'quality_state': 'partial',
                'completeness_state': 'unknown_scope', 'missing_sku_count': None,
                'quality_reason': reason, 'reason': reason,
                'evidence': {'unknown_campaign_ids': unresolved,
                             'dated_roster_state': d.get('dated_roster_state'),
                             'observed_campaign_ids': d.get('observed_campaign_ids', []),
                             'zero_fill_applied': False}}
    return result


def aggregate_counters(rows, *, dates, metrics=None):
    """Count only an explicit dated metric scope, intersected with the row scope."""
    metrics = metrics or {}
    sku_rows = {(row.scope_key, row.metric_key): row for row in rows if row.scope_kind == 'SKU'}
    result = []
    for row in rows:
        if row.scope_kind not in ('TOTAL', 'GROUP'):
            result.append(row)
            continue
        cells = dict(row.presentation_by_date)
        for day in dates:
            cell = dict(cells.get(day, {}))
            if cell.get('completeness_state') == 'unknown_scope':
                continue
            evidence = cell.get('evidence') or {}
            if not isinstance(evidence.get('applicable_scope'), list):
                evidence = cell.get('metric_scope_evidence') or evidence
            applicable = evidence.get('applicable_scope')
            dated = evidence.get('operand_date') == day
            if dated and isinstance(applicable, list):
                scope = {str(s) for s in applicable if str(s).startswith('SKU:')}
                # Group identity must be part of this same dated evidence.
                if row.scope_kind == 'GROUP':
                    groups = evidence.get('group_scopes', {})
                    if row.scope_key not in groups:
                        cell.update(completeness_state='unknown_scope', missing_sku_count=None)
                        cells[day] = cell
                        continue
                    scope &= set(groups[row.scope_key])
                scope -= set(evidence.get('inactive_scope', [])) | set(evidence.get('inapplicable_scope', []))
                missing = (set(evidence.get('missing_scope', [])) | set(evidence.get('partial_scope', []))) & scope
                keys = evidence.get('sku_metric_keys', [])
                unknown = False
                for member in scope:
                    for key in keys:
                        source = sku_rows.get((member, key))
                        detail = source.presentation_by_date.get(day, {}) if source else {}
                        if detail.get('completeness_state') == 'unknown_scope':
                            unknown = True
                        if source is None or source.values_by_date.get(day) in (None, '') or detail.get('quality_state') in ('partial', 'inventory_history_partial'):
                            missing.add(member)
                cell.update(completeness_state='unknown_scope' if unknown else 'partial' if missing else 'complete',
                            missing_sku_count=None if unknown else len(missing))
                if missing:
                    cell['quality_reason'] = (str(cell.get('quality_reason') or cell.get('reason') or '') +
                        ' SKU с отсутствующей или неполной метрикой: ' + ', '.join(sorted(missing)) + '.').strip()
            elif cell.get('quality_state') in ('partial', 'inventory_history_partial') or row.values_by_date.get(day) in (None, ''):
                cell.update(completeness_state='unknown_scope', missing_sku_count=None)
                cell.setdefault('quality_reason', 'Точный датированный состав отсутствующих SKU не подтверждён.')
            if cell:
                cells[day] = cell
        result.append(replace(row, presentation_by_date=cells))
    return result


def evaluator_scope_presentation(*, rows, slots, evaluator, current_date):
    """Freeze the current reporting metric scope; never backdate today's catalog."""
    def dependencies(key, visited=frozenset()):
        if key in visited:
            return []
        metric = evaluator.metrics_by_key.get(key)
        if metric is None:
            return []
        if metric.scope == 'SKU':
            return [key]
        if metric.calc_type == 'formula':
            formula = evaluator.formulas_by_id.get(metric.calc_ref)
            refs = re.findall(r'\{([^}]+)\}', formula.expression) if formula else []
        else:
            refs = metric.calc_ref.split('/')
        return sorted({dep for ref in refs for dep in dependencies(ref, visited | {key})})
    result = {}
    for slot in slots:
        if slot.column_date != current_date:
            continue
        for row in rows:
            scope, key = row[1].split('|', 1)
            if not (scope == 'TOTAL' or scope.startswith('GROUP:')):
                continue
            keys = dependencies(key)
            if not keys:
                continue
            members = evaluator.enabled_config if scope == 'TOTAL' else evaluator.grouped_config.get(scope[6:], [])
            applicable = ['SKU:' + str(item.nm_id) for item in members]
            missing = ['SKU:' + str(item.nm_id) for item in members
                       if any(evaluator.resolve_sku(k, item.nm_id, slot.slot_key) is None for k in keys)]
            result.setdefault(row[1], {})[slot.column_date] = {
                'metric_scope_evidence': {'operand_date': slot.column_date, 'applicable_scope': applicable,
                    'sku_metric_keys': keys, 'missing_scope': missing,
                    'group_scopes': {scope: applicable} if scope.startswith('GROUP:') else {}},
                'completeness_state': 'partial' if missing else 'complete',
                'missing_sku_count': len(set(missing))}
    return result
