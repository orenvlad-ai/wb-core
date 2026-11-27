"""Shared daily aggregates for observed SKU reports in READY and history."""
from packages.application.web_source_snapshot_block import observed_search_ctr


def observed_source_totals(source, items):
    """Aggregate a nonempty mapping of SKU to a row from one dated report."""
    rows = items.values()
    if source == 'seller_funnel_snapshot':
        return {
            'total_view_count': sum(row['view_count'] for row in rows),
            'total_open_card_count': sum(row['open_card_count'] for row in rows),
        }
    if source != 'web_source_snapshot':
        raise ValueError('unknown observed report source')
    return {
        'total_views_current': sum(row['views_current'] for row in rows),
        'avg_ctr_current': observed_search_ctr((row['ctr_current'], row['views_current']) for row in rows),
        'total_orders_current': sum(row['orders_current'] for row in rows),
        'avg_position_avg': round(sum(row['position_avg'] for row in rows) / len(items), 6),
    }
