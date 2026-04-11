# Source Basis Для Table Projection Bundle

## Почему Здесь Не Используется Обычный Legacy-Source

Для `table_projection_bundle_block` честный upstream находится не в legacy RAW-path напрямую.

Этот модуль строится поверх уже перенесённых и подтверждённых bounded outputs `wb-core`.

Поэтому в этом checkpoint используется термин:
- `reference source`

а не обычный:
- `legacy source`

## Что Считается Upstream Basis

Upstream/source basis фиксируется так:
- `migration/65_new_table_minimum_data_contract.md`
- `sku_display_bundle_block`
- `web_source_snapshot_block`
- `seller_funnel_snapshot_block`
- `prices_snapshot_block`
- `sf_period_block`
- `spp_block`
- `ads_bids_block`
- `stocks_block`
- `ads_compact_block`
- `fin_report_daily_block`
- `sales_funnel_history_block`

Именно эта связка уже даёт достаточный минимум для первой новой витрины.

## Что Берётся Из Upstream

Из upstream берутся:
- canonical SKU/display rows;
- snapshot-level поля и status/freshness;
- coverage requested `nmId`;
- минимальный history summary block.

## Что Сознательно Не Берётся

Сознательно не берётся:
- прямой legacy RAW-path;
- полный `CONFIG`;
- `METRICS`, `FORMULAS`, `DAILY RUN`;
- full inline history;
- новый registry/read-model framework.

## Почему Этого Достаточно

Этого достаточно, потому что первой новой витрине сейчас нужен не новый источник данных, а один честный server-side projection bundle, который склеивает уже перенесённые модульные результаты в table-facing контракт.
