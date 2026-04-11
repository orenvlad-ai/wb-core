# Контракт Блока Cogs By Group

## Что Это За Блок

`cogs_by_group_block` — bounded migration unit для current tabular apply path `77_plugins_cogs_by_group.js`.

Для этого блока не подтверждён отдельный consumer-facing HTTP contract.

Target block фиксируется относительно:
- ручного rule-source `RAW_COGS_RULES`;
- текущего active SKU linkage из `CONFIG.group`;
- current APPLY semantics из `77_plugins_cogs_by_group.js`.

## Границы Блока

В этот migration unit входит:
- historical semantics уровня `date + nmId`;
- вычисление `cost_price_rub` на основе `group`-правил;
- natural empty-case, когда для requested `nmId` в заданном окне нет применимой historical COGS row.

В этот migration unit не входит:
- перенос `CONFIG/METRICS/FORMULAS`;
- новая таблица;
- jobs/API/production hardening;
- deploy;
- любые другие migration units.

## Что Блок Должен Принимать

Блок должен принимать:
- `snapshot_type`;
- `date_from`;
- `date_to`;
- `nm_ids`;
- `scenario` для controlled checks.

## Что Блок Должен Отдавать

Блок должен отдавать target envelope поверх historical COGS apply snapshot.

Для success:
- `date_from`;
- `date_to`;
- `count`;
- `items`.

Внутри item:
- `date`;
- `nm_id`;
- `cost_price_rub`.

Для natural empty-case:
- отдельный `kind: "empty"`;
- `date_from`;
- `date_to`;
- `count = 0`;
- `items = []`;
- `detail`.

## Минимальная Parity Surface

Legacy и target сравниваются по:
- `date`;
- `nmId`;
- `cost_price_rub`.

Нельзя потерять:
- semantics уровня `date + nmId`;
- group linkage из active SKU set;
- historical rule selection как latest `effective_from <= date`;
- strict rule validation policy текущего apply path.

## Required Evidence

Чтобы считать блок корректным, нужны:
- legacy normal-case sample;
- legacy empty/no-row sample;
- target samples для обоих режимов;
- parity comparison по normal-case и empty-case;
- короткий evidence summary;
- artifact-backed smoke;
- rule-source smoke поверх безопасного fixture-backed path.
