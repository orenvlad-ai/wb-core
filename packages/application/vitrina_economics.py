"""Dated catalog economics with explicit coverage and one eligible set per total."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from packages.application.calculation_parameters import calculate_proxy_3
from packages.application.calculation_parameters_v4 import calculate_proxy_4
from packages.application.daily_trading_pool import classify, remembered_active

EFFECTIVE_DATE = '2026-09-08'
SOURCE = 'web_vitrina_management_history_v1'
COST = 'our_wb_unit_cost_rub'
METRICS = ('proxy_profit_3_rub', 'proxy_margin_3_pct', 'proxy_profit_4_rub',
           'proxy_margin_4_pct', 'proxy_margin_per_unit_rub')
TOTALS = ('total_proxy_profit_3_rub', 'proxy_margin_3_pct_total',
          'total_proxy_profit_4_rub', 'proxy_margin_4_pct_total', 'proxy_margin_per_unit_rub_total')
LABELS = {'order_sum': 'заказы в рублях', 'order_count': 'количество заказов',
          'ads_sum': 'расходы на рекламу', 'cost': 'себестоимость'}


def number(value):
    if value in ('', None) or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def calculate(operands, parameters, *, version, day):
    """A zero quantity needs no unit cost; missing activity is never zero."""
    values = {key: number(value) for key, value in operands.items()}
    missing = [key for key in ('order_sum', 'order_count', 'ads_sum') if values[key] is None]
    if values['order_count'] is not None and values['order_count'] > 0 and values['cost'] is None:
        missing.append('cost')
    invalid = any(values[k] is not None and values[k] < 0 for k in values)
    inconsistent = values['order_count'] == 0 and values['order_sum'] not in (None, Decimal(0))
    if missing or invalid or inconsistent or parameters is None:
        return {'available': False, 'reason': ('Нет данных: ' + ', '.join(LABELS[k] for k in missing)
                if missing else 'Несогласованные входы' if invalid or inconsistent else 'Нет параметров расчёта')}
    kwargs = dict(order_sum=values['order_sum'], order_count=values['order_count'],
                  ads_sum=values['ads_sum'], parameters=parameters,
                  canonical_wb_wac=values['cost'] if values['order_count'] else Decimal(0))
    result = calculate_proxy_3(**kwargs) if version == 3 else calculate_proxy_4(**kwargs, business_date=day)
    return {'available': True, 'profit': result[f'proxy_profit_{version}'],
            'revenue': result['expected_buyout_revenue'], 'quantity': result['expected_buyout_qty'],
            'state': 'no_activity' if values['order_count'] == 0 and values['ads_sum'] == 0 else 'calculated'}


def project_catalog_economics(plan, *, day, parameters):
    working = deepcopy(plan)
    sheet = next(s for s in working['sheets'] if s['sheet_name'] == 'DATA_VITRINA')
    if day not in sheet['header']:
        return working
    index = sheet['header'].index(day)
    rows = {r[1]: r for r in sheet['rows']}
    scopes = sorted({k.split('|')[0] for k in rows if k.startswith('SKU:') and
                     k.split('|')[1] in (COST, 'orderSum', 'proxy_profit_4_rub')})
    # Every discovered SKU owns the same economic rows, including new catalog
    # entries that have never belonged to the manual management configuration.
    for scope in scopes:
        anchor = next(r for key, r in rows.items() if key.startswith(scope + '|'))
        for metric in METRICS:
            key = scope + '|' + metric
            if key not in rows:
                row = [anchor[0], key, *['' for _ in sheet['header'][2:]]]
                sheet['rows'].append(row)
                rows[key] = row
    if 'row_count' in sheet:
        sheet['row_count'] = len(sheet['rows'])
    cells = working.setdefault('metadata', {}).setdefault('server_cell_presentation', {})
    p3, p4 = parameters if parameters else (None, None)
    results = {3: {}, 4: {}}
    remembered = remembered_active(cells, day) | set(
        working['metadata'].get('daily_trading_pool', {}).get(day, []))
    pool = {scope: classify(scope=scope, day=day, rows=rows, header=sheet['header'],
                            cells=cells, remembered=remembered) for scope in scopes}
    working['metadata'].setdefault('daily_trading_pool', {})[day] = sorted(
        scope for scope, (state, _) in pool.items() if state == 'active')

    def put(key, value, evidence):
        if key not in rows:
            return
        row = rows[key]
        while len(row) <= index:
            row.append('')
        row[index] = float(value) if value is not None else ''
        cell = {**evidence, 'source': SOURCE, 'source_as_of_date': day, 'target_date': day,
                'calculation_contract': 'catalog_economics_v1',
                'management_value': str(value) if value is not None else '', 'tone': 'warning'}
        cells.setdefault(key, {})[day] = cell

    def value(scope, metric):
        row = rows.get(scope + '|' + metric)
        cell = cells.get(scope + '|' + metric, {}).get(day, {})
        if cell.get('state') == 'unavailable' or cell.get('candidate_only') is True or cell.get('source_as_of_date') not in (None, '', day):
            return None
        return row[index] if row and len(row) > index else None

    for scope in scopes:
        cost = value(scope, COST)
        cell = cells.get(scope + '|' + COST, {}).get(day, {})
        if cell.get('source') in (SOURCE, 'official_fbs_management_inventory_v1', 'fbs_snapshot_inventory_presentation_v1'):
            cost = cell.get('management_value')
        operands = dict(order_sum=value(scope, 'orderSum'), order_count=value(scope, 'orderCount'),
                        ads_sum=value(scope, 'ads_sum'), cost=cost)
        for version, parameter in ((3, p3), (4, p4)):
            pool_state, pool_reason = pool[scope]
            result = (calculate(operands, parameter, version=version, day=day)
                      if pool_state != 'inactive' else {'available': False, 'reason': pool_reason})
            # Known zero activity can establish the result without an inventory
            # signal; otherwise an unknown pool member remains an explicit gap.
            if pool_state == 'unknown' and not result['available']:
                result['reason'] = pool_reason + ' ' + result['reason']
            results[version][scope] = result
            profit = result.get('profit')
            revenue, qty = result.get('revenue'), result.get('quantity')
            evidence = {'state': 'unconfirmed' if result['available'] else 'unavailable',
                        'daily_pool_state': pool_state, 'daily_pool_reason': pool_reason,
                        'quality_state': 'management_estimate' if result['available'] else 'unavailable',
                        'quality_label': 'Управленческая оценка' if result['available'] else 'Вне пула продаж' if pool_state == 'inactive' else 'Нет данных',
                        'reason': 'Нет заказов и рекламных расходов за дату.' if result.get('state') == 'no_activity'
                                  else 'Расчёт по заказам, рекламе и себестоимости указанной даты.' if result['available'] else result['reason'],
                        'evidence': {'operand_date': day, 'operands': operands,
                                     'parameter_version': parameter.version_id if parameter else None}}
            put(scope + f'|proxy_profit_{version}_rub', profit, evidence)
            put(scope + f'|proxy_margin_{version}_pct', profit / revenue if revenue else None, evidence)
            if version == 4:
                put(scope + '|proxy_margin_per_unit_rub', profit / qty if qty else None, evidence)

    coverage = {}
    for version in (3, 4):
        eligible = {s: r for s, r in results[version].items() if r['available']}
        missing = {s: r['reason'] for s, r in results[version].items() if not r['available'] and pool[s][0] != 'inactive'}
        inactive = [s for s in scopes if pool[s][0] == 'inactive']
        profit = sum((r['profit'] for r in eligible.values()), Decimal(0)) if eligible or (scopes and not missing) else None
        revenue = sum((r['revenue'] for r in eligible.values()), Decimal(0)) if eligible else None
        qty = sum((r['quantity'] for r in eligible.values()), Decimal(0)) if eligible else None
        reason = ('Неполный итог. Не учтены: ' + '; '.join(s.removeprefix('SKU:') + ' — ' + why for s, why in missing.items())
                  if missing else 'Полный итог по дневному пулу продаж. Товары вне продажи полноту не ухудшают.')
        evidence = {'state': 'unconfirmed' if profit is not None else 'unavailable',
                    'quality_state': 'partial' if missing else 'management_estimate',
                    'quality_label': 'Неполный итог' if missing else 'Управленческая оценка',
                    'reason': reason, 'quality_reason': reason,
                    'evidence': {'eligible_scope': list(eligible), 'missing_scope': missing, 'inactive_scope': inactive,
                                 'pool_count': len(scopes) - len(inactive),
                                 'catalog_count': len(scopes), 'included_count': len(eligible), 'operand_date': day}}
        put(f'TOTAL|total_proxy_profit_{version}_rub', profit, evidence)
        put(f'TOTAL|proxy_margin_{version}_pct_total', profit / revenue if revenue else None, evidence)
        if version == 4:
            put('TOTAL|proxy_margin_per_unit_rub_total', profit / qty if qty else None, evidence)
        coverage[str(version)] = evidence['evidence']
    working['metadata'].setdefault('catalog_economics_coverage', {})[day] = coverage
    return working
