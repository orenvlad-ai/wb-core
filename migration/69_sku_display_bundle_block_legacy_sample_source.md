# Legacy Source Для Sku Display Bundle

## Что Считается Legacy-Source

Для `sku_display_bundle_block` legacy-source фиксируется так:
- лист `CONFIG`;
- минимальный column subset:
  - `sku(nmId)`
  - `active`
  - `comment`
  - `group`

Именно эта связка сейчас достаточна, чтобы собрать первый display bundle для новой витрины.

## Почему Выбран Именно Он

Он выбран потому, что:
- первая новая витрина пока не требует полного переноса `CONFIG`;
- `nmId`, `comment`, `group` и `active` уже подтверждены как current operator-visible semantics;
- `CONFIG.comment` фактически работает как human-readable SKU name;
- `group` уже используется downstream rule/apply модулями;
- без переноса остальных sheet/runtime смыслов можно честно собрать тонкий display bundle.

## Какие Поля Реально Берём

Берём только:
- `sku(nmId)` -> `nm_id`;
- `comment` -> `display_name`;
- `group` -> `group`;
- `active` -> `enabled`;
- row order внутри sample -> `display_order`.

## Какие Поля Сознательно Не Берём

Сознательно не берём:
- любые дополнительные legacy-колонки `CONFIG`, если они появятся вне этого минимального набора;
- полную registry semantics;
- `METRICS`, `FORMULAS`, `DAILY RUN`;
- applied/report поля;
- любые server-only enrichments.

## Откуда Берётся Bootstrap Sample

В `wb-core` нет live `CONFIG`, поэтому checkpoint использует bootstrap sample set из минимального captured CONFIG-like fixture.

Bundle deliberately фиксируется на уровне:
- двух active SKU;
- одного inactive SKU;
- одного empty-case без строк.

## Какие Первые Два Sample Нужны

Первые два sample:
- normal-case sample, где bundle содержит active и inactive SKU в стабильном порядке;
- empty-case sample, где в минимальном captured CONFIG subset нет ни одной строки.
