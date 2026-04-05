# Контракт Блока Sf Period

## Что Это За Блок

`sf_period_block` — bounded migration unit для текущего `sf_period` RAW/APPLY path из табличного legacy-контура.

Для этого блока в legacy не подтверждён отдельный consumer-facing HTTP contract.

Поэтому target block фиксируется относительно:
- official source `POST /api/analytics/v3/sales-funnel/products`;
- current RAW normalization semantics для snapshot на дату;
- current APPLY semantics в `DATA` для `localizationPercent` и `feedbackRating`.

## Границы Блока

В этот migration unit входит:
- request на period snapshot по набору `nmId`;
- semantics `snapshot_date`;
- item-level payload shape на уровне `nmId`;
- current downstream смысл `localizationPercent` и `feedbackRating`.

В этот migration unit не входит:
- `CONFIG/METRICS` migration;
- новая таблица;
- jobs/API/production hardening;
- search/sales funnel отчёты beyond текущего поля;
- cutover любых других migration units.

## Что Блок Должен Принимать

Блок должен принимать:
- `snapshot_type`;
- `snapshot_date`;
- `nm_ids`;
- `scenario` для controlled checks.

Минимально обязательные сущности:
- `snapshot_type`;
- `snapshot_date`;
- `nm_ids`;
- `scenario`.

## Что Блок Должен Отдавать

Блок должен отдавать target envelope поверх snapshot-а `sf_period`.

Для success:
- `snapshot_date`;
- `count`;
- `items`.

Внутри item:
- `nm_id`;
- `localization_percent`;
- `feedback_rating`.

Отдельный `empty/not_found` shape в checkpoint не фиксируется, пока для этого upstream не подтверждён честный domain-level empty-case.

## Минимальная Parity Surface

Legacy и target сравниваются по:
- `snapshot_date`;
- составу `nmId`;
- `localizationPercent`;
- `feedbackRating`.

Нельзя потерять:
- привязку snapshot-а к дате запроса;
- `nmId` как identity;
- `statistic.selected.localizationPercent`;
- `product.feedbackRating`;
- semantics latest `fetched_at` для пары `date|nmId` в legacy apply.

## Required Evidence

Чтобы считать блок корректным, нужны:
- legacy normal-case sample;
- target normal-case sample;
- parity comparison по normal-case;
- короткий evidence summary;
- artifact-backed smoke;
- real-source smoke в authoritative server-side среде.

## Что Не Делаем В Рамках Этого Блока

Не делаем:
- перенос table runtime;
- перенос `CONFIG/METRICS`;
- production jobs/API;
- test framework;
- deploy.
