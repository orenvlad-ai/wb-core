# Контракт Блока Spp

## Что Это За Блок

`spp_block` — bounded migration unit для текущего `spp` RAW/APPLY path из табличного legacy-контура.

Для этого блока в legacy не подтверждён отдельный consumer-facing HTTP contract.

Поэтому target block фиксируется относительно:
- official source `GET /api/v1/supplier/sales?dateFrom=...`;
- current RAW normalization semantics из `40_raw_spp.js`;
- current APPLY semantics из `77_plugins_spp.js`.

## Границы Блока

В этот migration unit входит:
- request на yesterday-style snapshot `spp` по дате и набору `nmId`;
- semantics `snapshot_date`;
- item-level payload shape на уровне `nmId`;
- natural empty-case, когда за дату нет sales rows по запрошенным `nmId`.

В этот migration unit не входит:
- `CONFIG/METRICS` migration;
- новая таблица;
- jobs/API/production hardening;
- любые downstream use-cases beyond `spp`;
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

Блок должен отдавать target envelope поверх snapshot-а `spp`.

Для success:
- `snapshot_date`;
- `count`;
- `items`.

Внутри item:
- `nm_id`;
- `spp`.

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
- `spp`.

Нельзя потерять:
- yesterday semantics для `snapshot_date`;
- `nmId` как identity;
- нормализацию `spp` в долю;
- среднее `spp_avg` по sales rows в рамках `snapshot_date`.

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
