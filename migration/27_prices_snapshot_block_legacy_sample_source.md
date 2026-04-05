# Legacy Source Для Prices Snapshot

## Что Считается Legacy-Source

Для `prices_snapshot_block` legacy-source фиксируется так:
- current official-source path `POST /api/v2/list/goods/filter`;
- current RAW normalization semantics из `38_raw_prices.gs`;
- current APPLY semantics из `76_plugins_prices.gs`.

Именно эта связка сейчас задаёт downstream смысл цен в табличном legacy-контуре.

## Почему Выбран Именно Он

Он выбран потому, что:
- отдельный consumer-facing HTTP contract для цен сейчас не подтверждён;
- табличный контур уже фиксирует canonical semantics на уровне `DATA`;
- raw path и apply path вместе однозначно задают смысл результата на `nmId`.

## Какая Semantics Зафиксирована

Зафиксировано:
- список `nmId` берётся из активных SKU в `CONFIG` через `wbReadActiveSkus_`;
- upstream вызов идёт в `POST /api/v2/list/goods/filter` с телом `{ "nmList": [...] }`;
- raw snapshot получает `snapshot_date` по дню запуска;
- apply выбирает latest `snapshot_date`;
- на уровне `nmId` пишет:
  - `price_seller = min(price)`
  - `price_seller_discounted = min(discountedPrice)`

## Откуда Берётся Bootstrap `nmId`

В `wb-core` нет live `CONFIG`, поэтому checkpoint использует bootstrap sample set из уже известных проекту SKU:
- `210183919`
- `210184534`

Этот набор безопасно подтверждён repository evidence:
- `artifacts/web_source_snapshot_block/legacy/normal__template__legacy__fixture.json`
- `wb-gas-mvp` codebase references

## Какие Первые Два Sample Нужны

Первые два sample:
- normal-case sample с несколькими sizes на `nmId`;
- empty-case sample, где upstream возвращает `listGoods = []`.
