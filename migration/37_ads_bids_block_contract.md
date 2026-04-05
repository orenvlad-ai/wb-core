# Контракт Блока Ads Bids

## Что Это За Блок

`ads_bids_block` — bounded migration unit для текущего `ads_bids` RAW/APPLY path из табличного legacy-контура.

Для этого блока в legacy не подтверждён отдельный consumer-facing HTTP contract.

Поэтому target block фиксируется относительно:
- official source chain `GET /adv/v1/promotion/count` + `GET /api/advert/v2/adverts`;
- current RAW normalization semantics из `34_raw_ads_bids.js`;
- current APPLY semantics из `74_plugins_ads_bids.js`.

## Границы Блока

В этот migration unit входит:
- request на bids snapshot по дате и набору `nmId`;
- semantics `snapshot_date`;
- item-level payload shape на уровне `nmId`;
- natural empty-case, когда active campaigns не содержат запрошенные `nmId`.

В этот migration unit не входит:
- `CONFIG/METRICS` migration;
- новая таблица;
- jobs/API/production hardening;
- изменения рекламных кампаний;
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

Блок должен отдавать target envelope поверх snapshot-а bids.

Для success:
- `snapshot_date`;
- `count`;
- `items`.

Внутри item:
- `nm_id`;
- `ads_bid_search`;
- `ads_bid_recommendations`.

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
- `ads_bid_search`;
- `ads_bid_recommendations`.

Нельзя потерять:
- latest `fetched_at` semantics;
- `nmId` как identity;
- placement split `search` / `recommendations`;
- `max bid` per `(date,nmId,placement)`;
- перевод из копеек в рубли.

## Required Evidence

Чтобы считать блок корректным, нужны:
- legacy normal-case sample;
- legacy empty-case sample;
- target samples для обоих режимов;
- parity comparison по normal-case и empty-case;
- короткий evidence summary;
- artifact-backed smoke;
- authoritative server-side smoke.

## Что Не Делаем В Рамках Этого Блока

Не делаем:
- перенос table runtime;
- перенос `CONFIG/METRICS`;
- production jobs/API;
- test framework;
- deploy.
