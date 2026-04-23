# Legacy Source Для Promo By Price

## Что Считается Legacy-Source

Для `promo_by_price_block` legacy-source фиксируется так:
- ручной rule-source `RAW_PROMO_RULES`;
- текущая metric-row semantics `price_seller_discounted` в `DATA`;
- current APPLY semantics из `77_plugins_promo_by_price.js`.

Именно эта связка сейчас задаёт downstream смысл `promo_count_by_price`, `promo_entry_price_best` и `promo_participation` в tabular legacy-контуре.

## Почему Выбран Именно Он

Он выбран потому, что:
- модуль относится к rule/apply-слою, а не к live API snapshot;
- current legacy code однозначно фиксирует required columns и формулы сравнения;
- downstream значение считается на уровне `date + nmId`, а не на уровне отдельного HTTP response.

## Какая Semantics Зафиксирована

Зафиксировано:
- `RAW_PROMO_RULES` должен содержать колонки:
  - `Артикул WB`
  - `Плановая цена для акции`
  - `start_date`
  - `end_date`;
- `nmId` берётся из active SKU set и rule rows по `Артикул WB`;
- для каждой даты внутри окна выбираются только active rules, где `start_date <= date <= end_date`;
- eligible rule set строится из active rules, где `price_seller_discounted < plan_price`;
- `promo_entry_price_best` считается как `max(plan_price)` по eligible rules на дату, иначе truthful empty `0`;
- `promo_count_by_price` считается как число eligible rules;
- `promo_participation` считается как `1`, если `promo_count_by_price > 0`, иначе `0`;
- если валидных rules для requested `nmId` нет, возникает natural empty/no-rule case.

## Откуда Берётся Bootstrap `nmId`

В `wb-core` нет live `CONFIG`, поэтому checkpoint использует bootstrap sample set из уже известных проекту SKU:
- `210183919`
- `210184534`

Этот набор безопасно подтверждён repository evidence:
- `artifacts/prices_snapshot_block/legacy/normal__template__legacy__fixture.json`;
- `artifacts/web_source_snapshot_block/legacy/normal__template__legacy__fixture.json`.

## Какие Первые Два Sample Нужны

Первые два sample:
- normal-case sample на bootstrap `nmId` set;
- empty/no-rule sample, где requested `nmId` не получает ни одной promo row в выбранном окне.
