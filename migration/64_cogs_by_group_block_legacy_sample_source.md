# Legacy Source Для Cogs By Group

## Что Считается Legacy-Source

Для `cogs_by_group_block` legacy-source фиксируется так:
- ручной rule-source `RAW_COGS_RULES`;
- active SKU linkage из `CONFIG.group`;
- current APPLY semantics из `77_plugins_cogs_by_group.js`.

Именно эта связка сейчас задаёт downstream смысл `cost_price_rub` в tabular legacy-контуре.

## Почему Выбран Именно Он

Он выбран потому, что:
- модуль относится к rule/apply-слою, а не к live API snapshot;
- current legacy code однозначно фиксирует required columns, strict validation и historical selection rule;
- downstream значение считается на уровне `date + nmId`, а не через отдельный HTTP response.

## Какая Semantics Зафиксирована

Зафиксировано:
- `RAW_COGS_RULES` должен содержать колонки:
  - `group`
  - `cost_price_rub`
  - `effective_from`;
- `nmId` и `group` берутся из active SKU set;
- `cost_price_rub` на дату выбирается как последнее валидное правило, где `effective_from <= date`;
- текущий apply path делает strict validation:
  - duplicate `(group, effective_from)` -> abort
  - invalid `effective_from` -> abort
  - non-numeric `cost_price_rub` -> abort
  - unknown group в rules -> abort
  - missing group rules для active SKU -> abort.

## Откуда Берётся Bootstrap `nmId` И Group Linkage

В `wb-core` нет live `CONFIG`, поэтому checkpoint использует bootstrap sample set из уже известных проекту SKU:
- `210183919 -> hoodie`
- `210184534 -> tshirt`

`nmId` остаются безопасно привязаны к уже подтверждённому repository sample set:
- `artifacts/prices_snapshot_block/legacy/normal__template__legacy__fixture.json`;
- `artifacts/web_source_snapshot_block/legacy/normal__template__legacy__fixture.json`.

## Какие Первые Два Sample Нужны

Первые два sample:
- normal-case sample на bootstrap `nmId + group` linkage;
- empty/no-row sample, где requested `nmId` не получает ни одной historical COGS row в выбранном окне.
