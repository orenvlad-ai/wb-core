"""Automatic per-day reporting eligibility, independent of calculation coverage."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation


def numeric(value):
    if value in ('', None) or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def remembered_active(presentation, day):
    """Only evidence from this exact day may latch membership through a sellout."""
    active = set()
    for key, by_date in presentation.items():
        if not key.startswith('SKU:') or '|proxy_profit_' not in key:
            continue
        cell = by_date.get(day, {})
        if cell.get('source_as_of_date') != day:
            continue
        operands = cell.get('evidence', {}).get('operands', {})
        if cell.get('daily_pool_state') == 'active' or any(
                numeric(operands.get(k)) is not None and numeric(operands[k]) > 0
                for k in ('order_sum', 'order_count', 'ads_sum')):
            active.add(key.split('|')[0])
    return active


def classify(*, scope, day, rows, header, cells, remembered):
    def value(metric, target_day=day):
        row = rows.get(scope + '|' + metric)
        if row is None or target_day not in header:
            return None
        index = header.index(target_day)
        if len(row) <= index:
            return None
        cell = cells.get(scope + '|' + metric, {}).get(target_day, {})
        if cell.get('state') == 'unavailable' or cell.get('candidate_only') is True:
            return None
        if cell.get('source_as_of_date') not in (None, '', target_day):
            return None
        return numeric(row[index])

    activity = [value(k) for k in ('orderSum', 'orderCount', 'ads_sum')]
    stock = value('stock_total')  # WB physical plus published FBS; not all owned capital.
    previous_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    opening_stock = value('stock_total', previous_day)
    if any(v is not None and v > 0 for v in activity):
        return 'active', 'За дату есть заказы или рекламные расходы.'
    if scope in remembered:
        return 'active', 'Товар уже участвовал в продаже в течение этой даты.'
    if stock is not None and stock > 0:
        return 'active', 'Есть доступный остаток WB/FBS.'
    if opening_stock is not None and opening_stock > 0:
        return 'active', 'День начался с доступного остатка WB/FBS.'
    if all(v == 0 for v in activity):
        return 'zero_activity', 'За дату подтверждены нулевые заказы и рекламные расходы.'
    if stock == 0:
        return 'inactive', 'Нет доступного остатка WB/FBS и признаков участия в продаже за дату.'
    return 'unknown', 'Не удалось определить доступность товара к продаже за дату.'
