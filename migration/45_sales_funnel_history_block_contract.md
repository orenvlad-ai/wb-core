# Контракт Блока Sales Funnel History

## Что Это За Блок

`sales_funnel_history_block` — bounded migration unit для текущего historical sales-funnel RAW/APPLY path из табличного legacy-контура.

Для этого блока в legacy не подтверждён отдельный consumer-facing HTTP contract.

Поэтому target block фиксируется относительно:
- official source `POST /api/analytics/v3/sales-funnel/products/history`;
- current RAW normalization semantics из `30_raw_sales_funnel.js`;
- current APPLY semantics из `71_plugins_sales_funnel.js`.

## Границы Блока

В этот migration unit входит:
- request historical sales-funnel по набору `nmId` и периоду;
- semantics `date + nmId + metric`;
- latest `fetched_at` per `(date,nmId,metric)`;
- apply-level normalization percent metrics.

В этот migration unit не входит:
- `CONFIG/METRICS/FORMULAS` migration;
- новая таблица;
- jobs/API/production hardening;
- cutover любых других migration units.

## Что Блок Должен Принимать

Блок должен принимать:
- `snapshot_type`;
- `date_from`;
- `date_to`;
- `nm_ids`;
- `scenario` для controlled checks.

## Что Блок Должен Отдавать

Блок должен отдавать target envelope поверх history rows.

Для success:
- `date_from`;
- `date_to`;
- `count`;
- `items`.

Внутри item:
- `date`;
- `nm_id`;
- `metric`;
- `value`.

Для natural empty-case:
- отдельный `kind: "empty"`;
- `date_from`;
- `date_to`;
- `count = 0`;
- `items = []`;
- `detail`.

## Минимальная Parity Surface

Legacy и target сравниваются по:
- датам;
- составу `nmId`;
- составу metric keys;
- `value` after latest `fetched_at`;
- normalization percent metrics:
  - `addToCartConversion`
  - `cartToOrderConversion`
  - `buyoutPercent`.

## Required Evidence

Чтобы считать блок корректным, нужны:
- legacy normal-case sample;
- legacy empty-case sample;
- target samples для обоих режимов;
- parity comparison по normal-case и empty-case;
- короткий evidence summary;
- artifact-backed smoke;
- authoritative server-side smoke.
