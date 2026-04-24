# Контракт Блока Promo By Price

## Что Это За Блок

`promo_by_price_block` — bounded migration unit для current tabular apply path `77_plugins_promo_by_price.js`.

Для этого блока не подтверждён отдельный consumer-facing HTTP contract.

Target block фиксируется относительно:
- ручного rule-source `RAW_PROMO_RULES`;
- текущей `DATA`-семантики строки `price_seller_discounted`;
- current APPLY semantics из `77_plugins_promo_by_price.js`.

## Границы Блока

В этот migration unit входит:
- историческая semantics уровня `date + nmId`;
- вычисление:
  - `promo_count_by_price`
  - `promo_entry_price_best`
  - `promo_participation`;
- natural empty-case, когда для requested `nmId` в заданном окне нет применимых promo rows.

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

Блок должен отдавать target envelope поверх historical promo apply snapshot.

Для success:
- `date_from`;
- `date_to`;
- `count`;
- `items`.

Внутри item:
- `date`;
- `nm_id`;
- `promo_count_by_price`;
- `promo_entry_price_best`;
- `promo_participation`.

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
- `promo_count_by_price`;
- `promo_entry_price_best`;
- `promo_participation`.

Нельзя потерять:
- semantics уровня `date + nmId`;
- зависимость `promo_count_by_price` от текущего `price_seller_discounted`;
 - общий eligible set для `promo_entry_price_best` / `promo_count_by_price` / `promo_participation`;
 - `promo_entry_price_best = max(plan_price)` только по eligible акциям на дату, иначе truthful empty `0`;
 - `promo_participation = 1`, если `promo_count_by_price > 0`, иначе `0`.

## Required Evidence

Чтобы считать блок корректным, нужны:
- legacy normal-case sample;
- legacy empty/no-rule sample;
- target samples для обоих режимов;
- parity comparison по normal-case и empty-case;
- короткий evidence summary;
- artifact-backed smoke;
- rule-source smoke поверх безопасного fixture-backed path.
