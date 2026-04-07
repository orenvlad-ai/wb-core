# Контракт Блока Ads Compact

## Что Это За Блок

`ads_compact_block` — bounded migration unit для текущего `ads_compact` RAW/APPLY path из табличного legacy-контура.

Для этого блока в legacy не подтверждён отдельный consumer-facing HTTP contract.

Поэтому target block фиксируется относительно:
- official source chain `GET /adv/v1/promotion/count` + `GET /adv/v3/fullstats`;
- current RAW normalization semantics из `33_raw_ads_stats_compact.js`;
- current APPLY semantics из `73_plugins_ads_compact_apply.js`.

## Границы Блока

В этот migration unit входит:
- request compact ads snapshot по дате и набору `nmId`;
- semantics `snapshot_date + nmId`;
- базовые рекламные поля:
  - `ads_views`
  - `ads_clicks`
  - `ads_atbs`
  - `ads_orders`
  - `ads_sum`
  - `ads_sum_price`;
- apply-level производные метрики:
  - `ads_cpc`
  - `ads_ctr`
  - `ads_cr`;
- natural empty-case, когда filtered compact rows по запрошенным `nmId` и дате отсутствуют.

В этот migration unit не входит:
- `CONFIG/METRICS/FORMULAS` migration;
- новая таблица;
- jobs/API/production hardening;
- deploy;
- cutover любых других migration units.

## Что Блок Должен Принимать

Блок должен принимать:
- `snapshot_type`;
- `snapshot_date`;
- `nm_ids`;
- `scenario` для controlled checks.

## Что Блок Должен Отдавать

Блок должен отдавать target envelope поверх compact ads snapshot.

Для success:
- `snapshot_date`;
- `count`;
- `items`.

Внутри item:
- `nm_id`;
- `ads_views`;
- `ads_clicks`;
- `ads_atbs`;
- `ads_orders`;
- `ads_sum`;
- `ads_sum_price`;
- `ads_cpc`;
- `ads_ctr`;
- `ads_cr`.

Для natural empty-case:
- отдельный `kind: "empty"`;
- `snapshot_date`;
- `count = 0`;
- `items = []`;
- `detail`.

## Минимальная Parity Surface

Legacy и target сравниваются по:
- `snapshot_date`;
- составу `nmId`;
- `ads_views`;
- `ads_clicks`;
- `ads_atbs`;
- `ads_orders`;
- `ads_sum`;
- `ads_sum_price`;
- `ads_cpc`;
- `ads_ctr`;
- `ads_cr`.

Нельзя потерять:
- semantics уровня `snapshot_date + nmId`;
- current RAW aggregation из nested `days -> apps -> nms`;
- sum semantics для базовых полей;
- apply-level derivation для `ads_cpc`, `ads_ctr`, `ads_cr`.

## Required Evidence

Чтобы считать блок корректным, нужны:
- legacy normal-case sample;
- legacy empty-case sample;
- target samples для обоих режимов;
- parity comparison по normal-case и empty-case;
- короткий evidence summary;
- artifact-backed smoke;
- authoritative server-side smoke.
