# Контракт широкой wide-by-date витрины

## 1. Назначение wide matrix

`wide data matrix` — будущая основная таблица-витрина проекта.

Она должна быть:
- wide-by-date;
- визуально похожей на текущий `DATA`;
- read-side витриной поверх `wb-core`, а не old RAW/APPLY workbook.

Эта матрица не является:
- переносом старых `RAW_*` и `APPLY` листов;
- новым вычислительным центром внутри таблицы;
- местом, где снова смешиваются display-слой и server-side runtime semantics.

## 2. Базовая форма матрицы

Каноническая форма матрицы:
- колонка `A` = `label`;
- колонка `B` = `key`;
- колонки `C..` = даты;
- матрица растёт вправо по датам.

Базовый смысл колонок:
- `label` — человекочитаемое имя строки;
- `key` — стабильный machine-readable идентификатор строки;
- каждая дата справа — отдельный срез значения по этой строке.

Канонические key-паттерны для первой версии:
- `TOTAL|<metric_key>`;
- `GROUP:<group>|<metric_key>`;
- `SKU:<nm_id>|<metric_key>`.

## 3. Блоки матрицы

Матрица должна содержать три блока:
- `TOTAL`
- `GROUP`
- `SKU`

### 3.1 `TOTAL`

Зачем нужен:
- показывать верхнеуровневый срез по всем активным SKU;
- давать оператору короткий итог без раскрытия каждой карточки.

Что реально должно попадать в V1:
- ограниченный набор агрегируемых строк из `METRICS_V2` со `scope = TOTAL`;
- метрики, которые можно честно собрать без возврата к старой table-semantics.

Что пока не обещаем:
- полный набор legacy total-строк;
- сложные supply/report totals;
- любую логику, завязанную на старые ручные скрипты.

### 3.2 `GROUP`

Зачем нужен:
- дать промежуточный уровень чтения между общим итогом и конкретным SKU;
- сгруппировать строки по `CONFIG_V2.group`.

Что реально должно попадать в V1:
- ограниченный набор строк из `METRICS_V2` со `scope = GROUP`;
- только группы, которые уже однозначно заданы в `sku_display_bundle_block` / `CONFIG_V2`.

Что пока не обещаем:
- полный legacy group-behavior;
- сложные derived/group rules из старого табличного контура;
- любые group-level supply/report вычисления.

### 3.3 `SKU`

Зачем нужен:
- быть главным рабочим блоком первой витрины;
- показывать динамику по каждому SKU в форме, близкой к старому `DATA`, но на новом read-side контракте.

Что реально должно попадать в V1:
- строки из `METRICS_V2` со `scope = SKU`;
- SKU из `sku_display_bundle_block` / `CONFIG_V2` в canonical `display_order`;
- значения по датам, собранные server-side из уже перенесённых источников и projection-слоя.

Что пока не обещаем:
- полный legacy `DATA` parity по всем историческим строкам;
- все ручные rule-driven строки;
- перенос spreadsheet-formulas внутрь листа.

## 4. Роль `METRICS_V2`

`METRICS_V2` определяет строки wide matrix.

Именно `METRICS_V2` управляет display-слоем:
- какие строки есть;
- в каком порядке они идут;
- в каком разделе они находятся;
- в каком `scope` (`TOTAL`, `GROUP`, `SKU`) живут.

`METRICS_V2` не хранит тяжёлую runtime-semantics.

Тяжёлая вычислительная семантика живёт отдельно:
- в server-side runtime registry;
- в `FORMULAS_V2` как словаре формул;
- в server-side logic, которая резолвит `calc_ref`, а не в самой матрице.

## 5. Роль upstream данных

Wide matrix должна питаться от уже существующих слоёв:
- `sku_display_bundle_block`
- `table_projection_bundle_block`
- `registry/pilot_bundle`

### 5.1 Что приходит из `sku_display_bundle_block`

Используется для:
- canonical SKU-set;
- `display_name`;
- `group`;
- `enabled`;
- `display_order`.

Это задаёт:
- состав `SKU`-блока;
- группировку `GROUP`-блока;
- порядок строк внутри витрины.

### 5.2 Что приходит из `table_projection_bundle_block`

Используется для:
- canonical field surface по уже перенесённым источникам;
- snapshot/value names для первого server-side чтения;
- status/freshness/coverage источников.

Это должно идти:
- в server-side подготовку значений для wide matrix;
- в отдельный status/freshness layer;
- в техническую диагностику покрытия по SKU.

### 5.3 Что приходит из `registry/pilot_bundle`

Используется для:
- `CONFIG_V2` как стабилизированного display-SKU registry;
- `METRICS_V2` как реестра строк wide matrix;
- `FORMULAS_V2` как словаря formula references;
- runtime registry как server-side semantics layer.

Это должно определять:
- row inventory;
- row order;
- scope;
- display format;
- server-side resolution path для метрик.

### 5.4 Что пока не тянется в wide matrix

Пока не тянутся:
- supply/report слои;
- старые `RAW_*` и `APPLY` листы;
- full inline legacy operator logic;
- тяжёлая табличная runtime-semantics;
- полная историческая реконструкция всех старых строк `DATA`.

## 6. Минимальная первая версия

В первой версии реально можно наполнить прежде всего `SKU`-блок:
- по canonical SKU из `sku_display_bundle_block`;
- по строкам `METRICS_V2` со `scope = SKU`;
- по значениям, которые server-side слой уже умеет честно собрать из перенесённых модулей и projection-слоя.

`TOTAL` и `GROUP` в первом подэтапе могут быть:
- неполными;
- ограниченными по набору строк;
- собранными только для safe aggregation subset.

Ключевое правило:
- форма wide matrix должна быть сразу правильной;
- даже если наполнение `TOTAL` и `GROUP` будет достраиваться позже.

## 7. Status layer

Рядом с основной матрицей должен жить отдельный status/freshness слой.

Он может быть оформлен как:
- отдельный лист;
- или отдельный технический sidecar-layer.

В него относятся:
- `source_key`;
- `kind/status`;
- `as_of_date` / `snapshot_date` / `date_from` / `date_to`;
- freshness;
- requested/covered counts;
- missing entities;
- технические notes по coverage.

Его не нужно смешивать с основной wide matrix, потому что:
- status-данные служат для диагностики и контроля свежести;
- wide matrix должна оставаться операторской read-side витриной, а не operational log.

## 8. Что сознательно не входит

В этот контракт пока не входят:
- supply/report скрипты;
- factory order;
- полный legacy `CONFIG/METRICS/FORMULAS`;
- orchestration;
- старые `RAW/APPLY` листы;
- тяжёлая вычислительная логика внутри таблицы.

## 9. Следующий практический шаг

Следующий bounded implementation step:
- зафиксировать маленький artifact-backed `wide_data_matrix_v1` fixture на 3-5 дат, ограниченном наборе `SKU`-строк и минимальном subset `TOTAL/GROUP`, а затем добавить один smoke, который проверяет форму `A=label`, `B=key`, `C..=dates`, порядок строк по `METRICS_V2` и корректность key-patterns.
