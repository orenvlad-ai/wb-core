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
    # Dense source zeros for the other 55 products do not expand the trading pool.
    dense = deepcopy(p)
    for n in range(38, 93):
        for metric in ('orderSum', 'orderCount', 'ads_sum'):
            set_value(dense, f'SKU:{n}|{metric}', 0)
    assert coverage(project(dense))['pool_count'] == 37
    assert len(coverage(project(dense))['inactive_scope']) == 55
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
    # Multi-day page composition must preserve nested evidence and booleans.
    from dataclasses import replace
    from packages.application.sheet_vitrina_v1_web_vitrina import _merge_period_server_cell_presentation, _PeriodDateBinding
    from packages.application.daily_trading_pool import remembered_active
    saved = replace(fresh, metadata={'server_cell_presentation': r['metadata']['server_cell_presentation']})
    binding = _PeriodDateBinding(DAY, DAY, DAY)
    merged = _merge_period_server_cell_presentation(period_date_bindings=[binding],
        snapshots_by_as_of_date={DAY:saved}, template_rows=sheet['rows'])
    assert isinstance(merged['SKU:34|proxy_profit_4_rub'][DAY]['evidence'], dict)
    assert 'SKU:34' in remembered_active(merged, DAY)
    damaged = deepcopy(merged)
    damaged['SKU:34|proxy_profit_4_rub'][DAY]['evidence'] = 'old flattened metadata'
    assert 'SKU:34' in remembered_active(damaged, DAY)
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
    # Brand-new SKU has no legacy economic rows or manual configuration.
    from packages.application.vitrina_economics import METRICS
    from packages.application.web_vitrina_management_history import recalculate_current_rows
    from packages.contracts.web_vitrina_contract import WebVitrinaContractRow
    sparse = fixture()
    sparse['sheets'][0]['rows'] = [row for row in sparse['sheets'][0]['rows']
        if not (row[1].startswith('SKU:93|') and row[1].split('|')[1] in METRICS)]
    filled = project(sparse)
    assert values(filled)['SKU:93|proxy_profit_4_rub'] == 0
    view_rows = []
    for index, row in enumerate(sparse['sheets'][0]['rows']):
        scope, metric = row[1].split('|')
        view_rows.append(WebVitrinaContractRow(row[1],index,'SKU' if scope.startswith('SKU:') else 'TOTAL',
            scope,scope,metric,metric,'','Экономика',None,int(scope.split(':')[1]) if ':' in scope else None,
            'rub',dict(zip(sparse['date_columns'],row[2:])),{}))
    read_rows = recalculate_current_rows(view_rows,business_date=DAY,parameters=(P,P),original_presentation={},snapshot_id='new-sku')
    new_rows = {row.metric_key:row for row in read_rows if row.scope_key=='SKU:93'}
    assert all(metric in new_rows for metric in METRICS)
    assert new_rows['proxy_profit_4_rub'].values_by_date[DAY] == 0
    assert new_rows['proxy_profit_4_rub'].values_by_date['2026-09-07'] == ''
    # Persisted envelope must round-trip after automatic row creation.
    import json
    from dataclasses import asdict
    from packages.application.web_vitrina_management_history import recalculate_current_envelope
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
    from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot
    fresh = replace(fresh, temporal_slots=[SheetVitrinaV1TemporalSlot('yesterday_closed','Вчера','2026-09-07'), SheetVitrinaV1TemporalSlot('today_current','Сегодня',DAY)])
    raw_sheet = sparse['sheets'][0]
    envelope = replace(fresh, sheets=[replace(target, rows=raw_sheet['rows'], row_count=len(raw_sheet['rows'])),
        replace(target, sheet_name='STATUS', rows=[], row_count=0)])
    updated = recalculate_current_envelope(envelope, business_date=DAY, parameters=(P,P))
    assert updated.sheets[0].row_count == len(updated.sheets[0].rows)
    decoded = _deserialize_sheet_vitrina_plan(json.dumps(asdict(updated)))
    assert decoded.sheets[0].rows == updated.sheets[0].rows
    # Read the exact legacy defect without changing values or relaxing other errors.
    old_count = len(raw_sheet['rows'])
    legacy = asdict(updated)
    legacy['sheets'][0]['row_count'] = old_count
    decoded = _deserialize_sheet_vitrina_plan(json.dumps(legacy))
    assert decoded.sheets[0].rows == updated.sheets[0].rows
    legacy['sheets'][0]['rows'][-1][1] = 'SKU:93|orderSum'
    try:
        _deserialize_sheet_vitrina_plan(json.dumps(legacy))
    except ValueError as exc:
        assert 'row_count must match' in str(exc)
    else:
        raise AssertionError('unrelated row corruption was accepted')
    print('daily_pool: 92/37/33, zero sales, sellout, day rollover, stock provenance, ad-only and campaign dates: ok')


if __name__ == '__main__':
    main()
