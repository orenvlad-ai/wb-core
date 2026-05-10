# Контракт Блока Table Projection Bundle

## Что Это За Блок

`table_projection_bundle_block` — bounded migration unit для первого server-side table-facing projection слоя новой витрины.

Это не новая таблица.
Это не новый источник данных.
Это композиционный projection-блок поверх уже перенесённых модульных результатов `wb-core`.

Target block фиксируется относительно:
- минимального data contract для новой витрины из `migration/65_new_table_minimum_data_contract.md`;
- `sku_display_bundle_block`;
- уже перенесённых web-source и official API модулей;
- history-модуля `sales_funnel_history_block` в виде linked/status representation, а не full inline history.

## Границы Блока

В этот migration unit входит:
- чтение уже существующих module outputs;
- композиция одного table-facing projection bundle;
- status/freshness/coverage слой для первой витрины;
- linked history summary вместо полного inline historical payload.

В этот migration unit не входит:
- новая таблица;
- новые fetch/API источники;
- перепроектирование `CONFIG`, `METRICS`, `FORMULAS`, `DAILY RUN`;
- перенос supply/report скриптов;
- deploy;
- новый registry/read-model framework.

## Что Блок Должен Принимать

Блок должен принимать:
- `bundle_type`;
- `scenario` для controlled checks.

Минимально обязательные сущности:
- `bundle_type`;
- `scenario`.

## Что Блок Должен Отдавать

Блок должен отдавать минимальный table-facing projection bundle.

Для success:
- `as_of_date`;
- `count`;
- `items`;
- `source_statuses`.

Внутри item:
- базовый SKU/display bundle:
  - `nm_id`
  - `display_name`
  - `group`
  - `enabled`
  - `display_order`
- `web_source` summary:
  - `search_analytics`
  - `seller_funnel_daily`
- `official_api` summary:
  - `prices`
  - `sf_period`
  - `spp`
  - `ads_bids`
  - `stocks`
  - `ads_compact`
  - `fin_report_daily`
- `history_summary`

Для natural empty/minimal-case:
- отдельный `kind: "empty"`;
- `count = 0`;
- `items = []`;
- `source_statuses`;
- `detail`.

## Минимальная Parity Surface

Reference inputs и target сравниваются по:
- составу SKU из `sku_display_bundle_block`;
- row-level display semantics;
- статусам и freshness каждого upstream-блока;
- coverage requested `nmId` set;
- linked history summary без inline full history.

Нельзя потерять:
- canonical row order из `sku_display_bundle_block`;
- distinction между `present` и `missing` на уровне одного SKU;
- distinction между source-level `kind/status` и row-level presence;
- минимальную честную history representation без архитектурного перераздувания.

## Required Evidence

Чтобы считать блок корректным, нужны:
- reference/input normal-case sample;
- minimal-case sample;
- target samples для обоих режимов;
- parity comparison по normal-case и minimal-case;
- короткий evidence summary;
- artifact-backed smoke;
- bundle-composition smoke поверх уже существующих module fixtures.

## Что Не Делаем В Рамках Этого Блока

Не делаем:
- идеальную финальную модель всей витрины;
- перенос full history inline;
- новый live-source path;
- server-side deploy;
- новый registry/read-model layer.
