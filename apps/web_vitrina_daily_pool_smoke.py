"""Daily scope, stock evidence, sellout continuity and dated advertising roster."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copy import deepcopy
from datetime import date, timedelta
from apps.web_vitrina_catalog_economics_smoke import DAY, P, fixture, values, set_value
from packages.application.vitrina_economics import project_catalog_economics
from packages.adapters.ads_compact_block import HttpBackedAdsCompactSource


def project(p):
    return project_catalog_economics(p, day=DAY, parameters=(P, P))


def coverage(p):
    return p['metadata']['catalog_economics_coverage'][DAY]['4']


def main():
    p = fixture()
    p['sheets'][0]['rows'] = [r for r in p['sheets'][0]['rows'] if not r[1].startswith('SKU:93|')]
    for n in range(1, 93):
        p['sheets'][0]['rows'].append([str(n), f'SKU:{n}|stock_total', 0, 10 if n <= 37 else 0])
        if n > 33:
            for metric in ('orderSum', 'orderCount', 'ads_sum'):
                set_value(p, f'SKU:{n}|{metric}', '')
    r = project(p)
    assert len(coverage(r)['missing_scope']) == 4
    assert len(coverage(r)['inactive_scope']) == 55
    assert coverage(r)['pool_count'] == 37
    # Four stocked products with confirmed zero activity require no cost.
    for n in range(34, 38):
        for metric in ('orderSum', 'orderCount', 'ads_sum'):
            set_value(p, f'SKU:{n}|{metric}', 0)
    r = project(p)
    assert not coverage(r)['missing_scope'] and coverage(r)['included_count'] == 37
    assert values(r)['TOTAL|total_proxy_profit_4_rub'] == 1188
    assert values(r)['TOTAL|proxy_margin_per_unit_rub_total'] == 22.5
    # Sellout stays in scope; a later missing source cannot hide it.
    set_value(r, 'SKU:34|stock_total', 0)
    set_value(r, 'SKU:34|orderSum', '')
    r = project(r)
    assert 'SKU:34' in coverage(r)['missing_scope']
    # Persisted membership survives a rebuilt plan, before recalculation.
    from packages.application.web_vitrina_management_history import carry_forward
    from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaWriteTarget
    sheet = p['sheets'][0]
    target = SheetVitrinaWriteTarget('DATA_VITRINA','A1','A1:D999','A:D','replace',False,
                                   sheet['header'],sheet['rows'],len(sheet['rows']),4)
    fresh = SheetVitrinaV1Envelope('v1','bundle',DAY,p['date_columns'],[],{},[target])
    carried = carry_forward(fresh, presentation=r['metadata']['server_cell_presentation'], business_date=DAY)
    assert 'SKU:34' in carried.metadata['daily_trading_pool'][DAY]
    # Membership is dated and does not leak into the next day.
    next_day = (date.fromisoformat(DAY) + timedelta(days=1)).isoformat()
    for row in r['sheets'][0]['rows']:
        row.append(0 if row[1].endswith('|stock_total') else '')
    r['sheets'][0]['header'].append(next_day)
    r = project_catalog_economics(r, day=next_day, parameters=(P, P))
    assert 'SKU:34' in r['metadata']['catalog_economics_coverage'][next_day]['4']['inactive_scope']
    # Yesterday's sellable stock keeps today's unexplained gap visible.
    p2 = deepcopy(p)
    next(row for row in p2['sheets'][0]['rows'] if row[1] == 'SKU:38|stock_total')[2] = 1
    assert 'SKU:38' in coverage(project(p2))['missing_scope']
    # Internal stock and transit do not constitute available-to-sell stock.
    p['sheets'][0]['rows'].append(['38', 'SKU:38|own_capital_FF_qty', 100, 100])
    assert 'SKU:38' in coverage(project(p))['inactive_scope']
    # Unknown or rejected stock cannot silently exclude a product.
    set_value(p, 'SKU:38|stock_total', '')
    assert 'SKU:38' in coverage(project(p))['missing_scope']
    set_value(p, 'SKU:38|stock_total', 0)
    for cell in ({'state': 'unavailable'}, {'candidate_only': True}, {'source_as_of_date': '2026-09-07'}):
        p['metadata']['server_cell_presentation'] = {'SKU:38|stock_total': {DAY: cell}}
        assert 'SKU:38' in coverage(project(p))['missing_scope']
    # Orders or spend qualify even with zero current stock.
    p['metadata'] = {}
    for metric in ('orderSum', 'orderCount'): set_value(p, 'SKU:38|' + metric, 0)
    set_value(p, 'SKU:38|ads_sum', 8)
    assert values(project(p))['SKU:38|proxy_profit_4_rub'] == -8
    # Campaign status alone is not evidence of absence during the selected day.
    ads = HttpBackedAdsCompactSource(complete_catalog=True)
    roster = {'all': 5, 'adverts': [
        {'status': status, 'count': 1, 'advert_list': [{'advertId': i, 'changeTime': changed}]}
        for i, status, changed in [(1,7,'2026-09-07T23:59:00+03:00'),
          (2,11,'2026-09-08T00:00:00+03:00'), (3,9,'2025-01-01T00:00:00+03:00'),
          (4,7,'bad'), (5,11,'2026-09-07T23:00:00Z')]]}
    assert ads._extract_non_archived_advert_ids(roster, snapshot_date=DAY) == [2,3,4,5]
    assert ads._extract_non_archived_advert_ids(roster, snapshot_date='2026-09-07') == [1,2,3,4,5]
    print('daily_pool: 92/37/33, zero sales, sellout, day rollover, stock provenance, ad-only and campaign dates: ok')


if __name__ == '__main__':
    main()
