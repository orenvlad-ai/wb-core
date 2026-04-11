# Контракт Блока Sku Display Bundle

## Что Это За Блок

`sku_display_bundle_block` — bounded migration unit для тонкого table-facing bundle поверх legacy `CONFIG`.

Это не перенос полного `CONFIG` и не новый registry-слой.

Target block фиксируется относительно:
- текущего operator source `CONFIG`;
- минимального display-поднабора полей, которые реально нужны первой новой витрине;
- table-facing semantics активного и неактивного SKU без переноса `METRICS`, `FORMULAS` и `DAILY RUN`.

## Границы Блока

В этот migration unit входит:
- чтение минимального legacy shape из `CONFIG`;
- нормализация bundle на уровне одного SKU;
- display semantics для новой витрины;
- stable order для предсказуемого table-facing вывода.

В этот migration unit не входит:
- полный перенос `CONFIG` 1:1;
- registry redesign;
- `METRICS`, `FORMULAS`, `DAILY RUN`;
- jobs/API/production hardening;
- deploy;
- любые другие migration units.

## Что Блок Должен Принимать

Блок должен принимать:
- `bundle_type`;
- `scenario` для controlled checks.

Минимально обязательные сущности:
- `bundle_type`;
- `scenario`.

## Что Блок Должен Отдавать

Блок должен отдавать тонкий table-facing display bundle.

Для success:
- `count`;
- `items`.

Внутри item:
- `nm_id`;
- `display_name`;
- `group`;
- `enabled`;
- `display_order`.

Для natural empty-case:
- отдельный `kind: "empty"`;
- `count = 0`;
- `items = []`;
- `detail`.

## Минимальная Parity Surface

Legacy и target сравниваются по:
- `nmId`;
- `comment -> display_name`;
- `group`;
- `active -> enabled`;
- стабильному порядку строк.

Нельзя потерять:
- `nmId` как главный SKU key;
- `comment` как текущий human-readable display source;
- `group` как минимальную grouping semantics;
- boolean-like смысл `active`;
- детерминированный порядок bundle для витрины.

## Required Evidence

Чтобы считать блок корректным, нужны:
- legacy normal-case sample;
- legacy empty-case sample;
- target samples для обоих режимов;
- parity comparison по normal-case и empty-case;
- короткий evidence summary;
- artifact-backed smoke;
- safe CONFIG-fixture smoke без live spreadsheet/runtime.

## Что Не Делаем В Рамках Этого Блока

Не делаем:
- полный перенос `CONFIG`;
- перенос `METRICS`, `FORMULAS`, `DAILY RUN`;
- новую таблицу целиком;
- registry/framework redesign;
- jobs/API/deploy.
