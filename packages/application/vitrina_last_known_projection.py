"""Explicit historical estimate plus dated economics; no inventory mutation."""
from copy import deepcopy

from packages.application.vitrina_last_known_cost import project_costs
from packages.application.web_vitrina_management_history import data_sheet
from packages.application.vitrina_economics import METRICS, TOTALS, project_catalog_economics


def project_last_known(plan, *, dates, source, parameters, operation_id):
    result = project_costs(plan, dates=dates, source=source, operation_id=operation_id)
    working, changes, remaining = result['plan'], result['changes'], result['remaining']
    for day in dates:
        sheet = data_sheet(working)
        if day not in sheet['header']:
            continue
        index = sheet['header'].index(day)
        rows = {r[1]: r for r in sheet['rows']}
        cells = working.setdefault('metadata', {}).setdefault('server_cell_presentation', {})
        # The ordinary catalog evaluator owns the formula and partial TOTAL
        # rules. This explicit repair copies only existing economic cells.
        recalculated = project_catalog_economics(working, day=day, parameters=parameters[day])
        estimates = {
            key.split('|')[0]: by_day[day]
            for key, by_day in cells.items()
            if key.endswith('|our_wb_unit_cost_rub') and day in by_day
            and by_day[day].get('evidence', {}).get('selection_method') == 'latest_same_sku_closed_resolved_on_or_before_target'
        }
        for row in data_sheet(recalculated)['rows']:
            key = row[1]
            if key not in rows or key.split('|')[1] not in (*METRICS, *TOTALS):
                continue
            cell = deepcopy(recalculated['metadata']['server_cell_presentation'][key][day])
            scope = key.split('|')[0]
            included = cell.get('evidence', {}).get('eligible_scope', []) if scope == 'TOTAL' else [scope]
            used = {s: estimates[s] for s in included if s in estimates}
            cell['operation_id'] = operation_id
            if used:
                cell['evidence']['last_known_costs'] = used
                note = (' Себестоимость оценочная: последнее известное значение каждого товара '
                        'не позднее даты расчёта; фактические остатки не восстановлены.')
                cell['reason'] = str(cell.get('reason') or '') + note
                cell['quality_reason'] = str(cell.get('quality_reason') or '') + note
            previous = cells.get(key, {}).get(day, {})
            if rows[key][index] != row[index] or previous != cell:
                changes.append({'date': day, 'row_id': key, 'before': rows[key][index],
                                'after': row[index], 'provenance': cell})
                rows[key][index] = row[index]
                cells.setdefault(key, {})[day] = cell
            if row[index] in ('', None):
                remaining.append({'date': day, 'row_id': key, 'reason': cell['reason']})
    return {'plan': working, 'changes': changes, 'remaining': remaining}
