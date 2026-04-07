# Legacy Source Для Ads Compact

## Что Считается Legacy-Source

Для `ads_compact_block` legacy-source фиксируется так:
- current official-source chain `GET /adv/v1/promotion/count` + `GET /adv/v3/fullstats?ids=...&beginDate=...&endDate=...`;
- current RAW normalization semantics из `33_raw_ads_stats_compact.js`;
- current APPLY semantics из `73_plugins_ads_compact_apply.js`.

Именно эта связка сейчас задаёт downstream смысл `ads_compact` в табличном legacy-контуре.

## Почему Выбран Именно Он

Он выбран потому, что:
- отдельный consumer-facing HTTP contract для `ads_compact` сейчас не подтверждён;
- raw path уже строит canonical `RAW_ADS_STATS_COMPACT`;
- apply path однозначно фиксирует итоговые метрики на уровне `snapshot_date + nmId`.

## Какая Semantics Зафиксирована

Зафиксировано:
- из `promotion/count` извлекаются non-archived advert IDs со статусами `4`, `9`, `11`;
- затем вызывается `fullstats` по батчам advert IDs и выбранному периоду;
- raw path агрегирует nested `days -> apps -> nms` в строки схемы:
  - `snapshot_date`
  - `nmId`
  - `ads_views`
  - `ads_clicks`
  - `ads_atbs`
  - `ads_orders`
  - `ads_sum`
  - `ads_sum_price`
  - `fetched_at`;
- aggregation в raw идёт суммированием по ключу `(snapshot_date, nmId)`;
- apply повторно суммирует значения по `date|nmId` из текущего содержимого `RAW_ADS_STATS_COMPACT`;
- apply-level derivation считает:
  - `ads_cpc = ads_sum / ads_clicks`, иначе `0`;
  - `ads_ctr = ads_clicks / ads_views`, иначе `0`;
  - `ads_cr = ads_orders / ads_clicks`, иначе `0`.

## Откуда Берётся Bootstrap `nmId`

В `wb-core` нет live `CONFIG`, поэтому checkpoint использует bootstrap sample set из уже известных проекту SKU:
- `210183919`
- `210184534`

Этот набор безопасно подтверждён repository evidence:
- `artifacts/prices_snapshot_block/legacy/normal__template__legacy__fixture.json`;
- `artifacts/web_source_snapshot_block/legacy/normal__template__legacy__fixture.json`.

## Какие Первые Два Sample Нужны

Первые два sample:
- normal-case sample на bootstrap `nmId` set;
- empty-case sample, где filtered compact rows не содержат запрошенный `nmId` на выбранную дату.
