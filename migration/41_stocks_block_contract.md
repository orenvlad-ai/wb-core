# Контракт Блока Stocks

## Что Это За Блок

`stocks_block` — bounded migration unit для текущего `stocks` RAW/APPLY path из табличного legacy-контура.

Для этого блока в legacy не подтверждён отдельный consumer-facing HTTP contract.

Поэтому target block фиксируется относительно:
- official source `POST /api/v2/stocks-report/products/sizes`;
- current RAW normalization semantics из `31_raw_stocks.js`;
- current APPLY semantics из `72_plugins_stocks.js`.

## Границы Блока

В этот migration unit входит:
- request на stocks snapshot по дате и набору `nmId`;
- semantics `snapshot_date`;
- item-level payload shape на уровне `nmId`;
- `stock_total` и региональные `stock_*`;
- guard против публикации неполного snapshot.

В этот migration unit не входит:
- перенос Apps Script cursor/staging 1:1;
- `CONFIG/METRICS` migration;
- новая таблица;
- jobs/API/production hardening;
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

Блок должен отдавать target envelope поверх snapshot-а остатков.

Для success:
- `snapshot_date`;
- `count`;
- `items`.

Внутри item:
- `nm_id`;
- `stock_total`;
- `stock_ru_central`;
- `stock_ru_northwest`;
- `stock_ru_volga`;
- `stock_ru_ural`;
- `stock_ru_south_caucasus`;
- `stock_ru_far_siberia`.

Для partial/incomplete snapshot:
- отдельный `kind: "incomplete"`;
- `snapshot_date`;
- `requested_count`;
- `covered_count`;
- `missing_nm_ids`;
- `detail`.

## Минимальная Parity Surface

Legacy и target сравниваются по:
- `snapshot_date`;
- составу `nmId`;
- `stock_total`;
- всем региональным `stock_*`;
- semantics coverage guard.

Нельзя потерять:
- latest `snapshot_ts` per `(date,nmId)`;
- суммирование `stockCount` по всем offices для `stock_total`;
- суммирование `stockCount` по region mapping для RU districts;
- отказ от публикации при неполном coverage requested set.

## Required Evidence

Чтобы считать блок корректным, нужны:
- legacy normal-case sample;
- legacy partial-case sample;
- target samples для обоих режимов;
- parity comparison по normal-case и partial-case;
- короткий evidence summary;
- artifact-backed smoke;
- authoritative server-side smoke.

## Что Не Делаем В Рамках Этого Блока

Не делаем:
- перенос Script Properties cursor/staging буквально;
- production jobs/API;
- test framework;
- deploy.
