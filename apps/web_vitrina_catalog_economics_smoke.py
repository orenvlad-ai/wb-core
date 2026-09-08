"""Catalog growth, dated coverage, zero activity and consistent partial totals."""
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.vitrina_economics import project_catalog_economics, METRICS, TOTALS
from packages.application.web_vitrina_management_history import recalculate_current
from packages.application.vitrina_catalog import reporting_config
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item
from packages.adapters.ads_compact_block import HttpBackedAdsCompactSource

DAY = '2026-09-08'
P = SimpleNamespace(buyout_rate=Decimal('.8'), retained_share=Decimal('.7'), included_expense_rate=Decimal('.3'), version_id='v1')


def fixture():
    rows = [['total', 'TOTAL|' + m, 123, ''] for m in TOTALS]
    for n in range(1, 94):
        data = {'orderSum': 100 if n <= 33 else 0, 'orderCount': 2 if n <= 33 else 0,
                'ads_sum': 4 if n <= 33 else 0, 'our_wb_unit_cost_rub': 10 if n <= 33 else ''}
        rows.extend([[str(n), f'SKU:{n}|' + k, 999, v] for k, v in data.items()])
        rows.extend([[str(n), f'SKU:{n}|' + m, 888, ''] for m in METRICS])
    return {'date_columns': ['2026-09-07', DAY], 'metadata': {},
            'sheets': [{'sheet_name': 'DATA_VITRINA', 'header': ['label', 'key', '2026-09-07', DAY], 'rows': rows}]}


def values(plan):
    return {r[1]: r[3] for r in plan['sheets'][0]['rows']}


def set_value(plan, key, value):
    next(r for r in plan['sheets'][0]['rows'] if r[1] == key)[3] = value


def main():
    original = fixture()
    plan = recalculate_current(original, business_date=DAY, parameters=(P, P))
    v = values(plan)
    assert v['TOTAL|total_proxy_profit_4_rub'] == 33 * 36
    assert v['TOTAL|proxy_margin_4_pct_total'] == .45
    assert v['TOTAL|proxy_margin_per_unit_rub_total'] == 22.5
    assert v['SKU:93|proxy_profit_4_rub'] == 0 and v['SKU:93|proxy_margin_4_pct'] == ''
    assert all(r[2] == original['sheets'][0]['rows'][i][2] for i, r in enumerate(plan['sheets'][0]['rows']))
    assert values(original)['TOTAL|total_proxy_profit_4_rub'] == ''
    # A new SKU becomes active without any reporting activation or fixed roster.
    set_value(original, 'SKU:93|orderSum', 100)
    set_value(original, 'SKU:93|orderCount', 2)
    plan = project_catalog_economics(original, day=DAY, parameters=(P, P))
    total = plan['metadata']['server_cell_presentation']['TOTAL|total_proxy_profit_4_rub'][DAY]
    assert total['quality_label'] == 'Неполный итог' and '93 — Нет данных: себестоимость' in total['reason']
    assert values(plan)['TOTAL|total_proxy_profit_4_rub'] == 1188
    set_value(original, 'SKU:93|our_wb_unit_cost_rub', 10)
    plan = project_catalog_economics(original, day=DAY, parameters=(P, P))
    assert values(plan)['TOTAL|total_proxy_profit_4_rub'] == 1228
    # Advertising-only losses are included in profit and in unit-margin numerator.
    set_value(original, 'SKU:92|ads_sum', 8)
    plan = project_catalog_economics(original, day=DAY, parameters=(P, P))
    assert values(plan)['SKU:92|proxy_profit_4_rub'] == -8
    assert abs(values(plan)['TOTAL|proxy_margin_per_unit_rub_total'] - 1220 / (34 * 1.6)) < 1e-9
    set_value(original, 'SKU:91|orderSum', '')
    plan = project_catalog_economics(original, day=DAY, parameters=(P, P))
    assert values(plan)['SKU:91|proxy_profit_4_rub'] == ''
    assert plan['metadata']['catalog_economics_coverage'][DAY]['4']['missing_scope']['SKU:91']
    assert project_catalog_economics(plan, day=DAY, parameters=(P, P)) == plan
    for row in original['sheets'][0]['rows']:
        if row[1].endswith('|orderSum'): row[3] = ''
    assert values(project_catalog_economics(original, day=DAY, parameters=(P, P)))['TOTAL|total_proxy_profit_4_rub'] == ''
    from apps.sheet_vitrina_v1_stock_catalog_scope_smoke import seed
    from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
    with TemporaryDirectory() as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        seed(runtime, [(101, 1, 0), (202, 1, 0)])
        config = [ConfigV2Item(101, True, 'old', 'manual', 1)]
        assert {r.nm_id for r in reporting_config(runtime.db_path, config)[0]} == {101, 202}
        seed(runtime, [(303, 0, 1)])
        assert {r.nm_id for r in reporting_config(runtime.db_path, config)[0]} == {101, 202, 303}
        assert len(config) == 1
        # Exercise the real refresh entry point: its collector requests and
        # materialized economic rows must use the same automatic catalog.
        from apps.sheet_vitrina_v1_business_time_smoke import _build_live_plan, INPUT_BUNDLE_FIXTURE
        runtime.ingest_bundle(json.loads(INPUT_BUNDLE_FIXTURE.read_text()), activated_at='2026-09-08T00:00:00Z')
        block = _build_live_plan(runtime)
        block.now_factory = lambda: datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
        requested = []
        original_execute = block.sales_funnel_history_block.execute
        def record(request):
            requested.append(request.nm_ids)
            return original_execute(request)
        block.sales_funnel_history_block.execute = record
        materialized = block.build_plan(source_keys=['sales_funnel_history'], metric_keys=['orderSum'])
        assert requested and all({101, 202, 303}.issubset(ids) for ids in requested)
        data = next(s for s in materialized.sheets if s.sheet_name == 'DATA_VITRINA')
        assert {f'SKU:{n}|orderSum' for n in (101, 202, 303)}.issubset({r[1] for r in data.rows})
    ads = HttpBackedAdsCompactSource(complete_catalog=True, batch_sleep_seconds=0)
    assert ads._extract_non_archived_advert_ids({'all': 1, 'adverts': [{'status': 7, 'count': 1, 'advert_list': [{'advertId': 5}]}]}) == [5]
    ads._get_json = lambda **kw: [{'advertId': 5, 'days': []}]
    kwargs = dict(base_url='unused', token='unused', advert_ids=[5], snapshot_date=DAY, nm_ids=[101, 202], timeout_seconds=1)
    assert [r['ads_sum'] for r in ads._fetch_compact_rows(**kwargs)] == [0, 0]
    ads._get_json = lambda **kw: []
    try:
        ads._fetch_compact_rows(**kwargs)
    except ValueError as exc:
        assert str(exc) == 'ads_catalog_statistics_incomplete'
    else:
        raise AssertionError('partial response became zero')
    print('catalog_economics: growth, 93/33, no activity, ad-only loss, partial coverage, recovery, dated history: ok')


if __name__ == '__main__':
    main()
