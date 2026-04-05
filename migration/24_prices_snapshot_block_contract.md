# Контракт Блока Prices Snapshot

## Что Это За Блок

`prices_snapshot_block` — bounded migration unit для текущего prices RAW/APPLY path из табличного legacy-контура.

Для этого блока в legacy нет отдельного consumer-facing HTTP contract.

Поэтому target block фиксируется относительно:
- official source `POST /api/v2/list/goods/filter`;
- current RAW normalization semantics;
- current APPLY semantics в `DATA` для `price_seller` и `price_seller_discounted`.

## Границы Блока

В этот migration unit входит:
- request на snapshot цен по набору `nmId`;
- semantics `snapshot_date`;
- item-level payload shape на уровне `nmId`;
- natural empty-case, когда upstream не вернул ни одного товара.

В этот migration unit не входит:
- `CONFIG/METRICS` migration;
- новая таблица;
- jobs/API/production hardening;
- downstream use-cases поверх цен;
- cutover любых других migration units.

## Что Блок Должен Принимать

Блок должен принимать:
- `snapshot_type`;
- `snapshot_date`;
- `nm_ids`;
- `scenario` для controlled checks.

Минимально обязательные сущности:
- `snapshot_type`
- `snapshot_date`
- `nm_ids`
- `scenario`

## Что Блок Должен Отдавать

Блок должен отдавать target envelope поверх snapshot-а цен.

Для success:
- `snapshot_date`
- `count`
- `items`

Внутри item:
- `nm_id`
- `price_seller`
- `price_seller_discounted`

Для natural empty-case:
- отдельный `kind: "empty"`;
- `snapshot_date`;
- `count = 0`;
- `items = []`;
- `detail`

## Минимальная Parity Surface

Legacy и target сравниваются по:
- `snapshot_date`;
- составу `nmId`;
- `price_seller`;
- `price_seller_discounted`;
- semantics empty-case.

Нельзя потерять:
- агрегацию по latest `snapshot_date`;
- `nmId` как identity;
- `min(price)` across sizes;
- `min(discountedPrice)` across sizes.

## Required Evidence

Чтобы считать блок корректным, нужны:
- legacy normal-case sample;
- legacy empty-case sample;
- target samples для обоих режимов;
- parity comparison по normal-case и empty-case;
- короткий evidence summary.

## Что Не Делаем В Рамках Этого Блока

Не делаем:
- перенос table runtime;
- перенос `CONFIG/METRICS`;
- production jobs/API;
- test framework;
- deploy.
