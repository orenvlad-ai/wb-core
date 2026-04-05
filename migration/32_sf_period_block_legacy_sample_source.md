# Legacy Source Для Sf Period

## Что Считается Legacy-Source

Для `sf_period_block` legacy-source фиксируется так:
- current official-source path `POST /api/analytics/v3/sales-funnel/products`;
- current RAW normalization semantics из `39_raw_sf_period.js`;
- current APPLY semantics из `75_plugins_sf_period.js`.

Именно эта связка сейчас задаёт downstream смысл `sf_period` в табличном legacy-контуре.

## Почему Выбран Именно Он

Он выбран потому, что:
- отдельный consumer-facing HTTP contract для `sf_period` сейчас не подтверждён;
- downstream смысл модуля уже задан полями, которые пишутся в `DATA`;
- official source path и apply semantics вместе достаточно строго задают результат на уровне `nmId`.

## Какая Semantics Зафиксирована

Зафиксировано:
- upstream вызов идёт в `POST /api/analytics/v3/sales-funnel/products`;
- запрос строится на `snapshot_date` и наборе `nmId`;
- legacy apply берёт latest `fetched_at` для пары `date|nmId`;
- в `DATA` пишутся:
  - `localizationPercent` из `statistic.selected.localizationPercent`;
  - `feedbackRating` из `product.feedbackRating`.

## Откуда Берётся Bootstrap `nmId`

В `wb-core` нет live `CONFIG`, поэтому checkpoint использует bootstrap sample set из уже известных проекту SKU:
- `210183919`
- `210184534`

Этот набор уже подтверждён repository evidence:
- `artifacts/prices_snapshot_block/legacy/normal__template__legacy__fixture.json`;
- `artifacts/web_source_snapshot_block/legacy/normal__template__legacy__fixture.json`.

## Какие Первые Sample Нужны

Для первого checkpoint обязателен:
- normal-case sample на bootstrap `nmId` set.

Synthetic `empty/not_found` sample не фиксируется как обязательный, пока upstream не даёт безопасный domain-level empty ответ вместо transport/gateway failure.
