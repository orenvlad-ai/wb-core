# Минимальная Runtime-Схема Реестра Метрик

## 1. Назначение Документа

`metric runtime registry` нужен как отдельный server-side слой, который хранит вычислительный смысл метрик:
- откуда берётся метрика;
- как интерпретировать её значение;
- как вести себя при missing source;
- как агрегировать период;
- как связать прямую, формульную и ratio-метрику.

Этот слой отличается от `METRICS_V2` тем, что:
- `METRICS_V2` остаётся лёгким display-реестром витрины;
- runtime registry хранит server-side semantics и правила вычисления;
- эти правила нельзя держать в лёгкой табличной витрине, потому что они относятся к runtime-поведению, а не к display-конфигурации.

## 2. Граница Ответственности

В `METRICS_V2` остаётся:
- `metric_key`;
- `enabled`;
- `scope`;
- `label_ru`;
- `calc_type`;
- `calc_ref`;
- `show_in_data`;
- `format`;
- `display_order`;
- `section`.

В server-side runtime registry должно жить:
- тип runtime-метрики;
- семантическая единица значения;
- числовая шкала значения;
- missing policy;
- периодная агрегация;
- связь с формулой;
- связь числителя и знаменателя для ratio;
- source family и source module;
- runtime enable flag.

В `FORMULAS_V2` остаётся:
- `formula_id`;
- `expression`;
- `description`.

`FORMULAS_V2` не хранит source family, aggregation, missing behavior и другие runtime-правила.

## 3. Минимальная Server-Side Схема

| Поле | Тип | Обязательно | Описание | Пример |
| --- | --- | --- | --- | --- |
| `metric_key` | `string` | да | Стабильный runtime-ключ метрики. Совпадает с display-ключом, который видит витрина. | `stock_total` |
| `metric_kind` | `string` | да | Тип runtime-семантики. Минимум: `direct`, `formula`, `ratio`. | `ratio` |
| `value_unit` | `string` | да | Семантическая единица значения, не display-формат. | `rub` |
| `value_scale` | `string` | да | Каноническая шкала числового значения на runtime-слое. | `ratio_0_1` |
| `missing_policy` | `string` | да | Правило поведения при missing source или неполных входах. | `null_if_denominator_missing_or_zero` |
| `period_agg` | `string` | да | Канонический способ агрегировать значение по окну/периоду. | `last_snapshot` |
| `formula_id` | `string \| null` | нет | Ссылка на `FORMULAS_V2`, если `metric_kind = formula`. | `proxy_profit_rub` |
| `ratio_num_key` | `string \| null` | нет | Ключ числителя, если `metric_kind = ratio`. | `ads_clicks` |
| `ratio_den_key` | `string \| null` | нет | Ключ знаменателя, если `metric_kind = ratio`. | `ads_impressions` |
| `source_family` | `string` | да | Семейство upstream-источника или derived-слоя. | `official_api` |
| `source_module` | `string` | да | Канонический модуль `wb-core`, который даёт upstream-value или primary input. | `stocks_block` |
| `is_runtime_enabled` | `boolean` | да | Разрешена ли runtime-обработка этой метрики в server-side слое. | `true` |

### Минимальные Допущения По Значениям

- `metric_kind`: `direct`, `formula`, `ratio`
- `value_unit`: `items`, `rub`, `percent`, `ratio`, `count`
- `value_scale`: `integer`, `decimal_2`, `ratio_0_1`, `percent_0_100`
- `missing_policy`: `null`, `zero`, `propagate_missing`, `null_if_denominator_missing_or_zero`
- `period_agg`: `last_snapshot`, `sum_window`, `avg_window`, `ratio_window`, `formula_window`

## 4. Три Типа Метрик Как Образцы

| Тип | `metric_key` | Как работает | Минимальная runtime-конфигурация |
| --- | --- | --- | --- |
| Прямая | `stock_total` | Берётся как прямой snapshot-value из upstream-модуля. | `metric_kind=direct`, `value_unit=items`, `value_scale=integer`, `missing_policy=zero`, `period_agg=last_snapshot`, `source_family=official_api`, `source_module=stocks_block` |
| Формульная | `proxy_profit_rub` | Считается по формуле, а не приходит как готовое поле из одного source-модуля. | `metric_kind=formula`, `value_unit=rub`, `value_scale=decimal_2`, `missing_policy=propagate_missing`, `period_agg=formula_window`, `formula_id=proxy_profit_rub`, `source_family=derived`, `source_module=metric_runtime_registry` |
| Ratio | `ads_ctr` | Считается как отношение двух runtime-метрик. | `metric_kind=ratio`, `value_unit=ratio`, `value_scale=ratio_0_1`, `missing_policy=null_if_denominator_missing_or_zero`, `period_agg=ratio_window`, `ratio_num_key=ads_clicks`, `ratio_den_key=ads_impressions`, `source_family=official_api`, `source_module=ads_compact_block` |

## 5. Связь С `METRICS_V2`

Связка должна быть минимальной и однозначной:
- `METRICS_V2.metric_key` совпадает с `metric runtime registry.metric_key`;
- если `METRICS_V2.calc_type = metric`, то `METRICS_V2.calc_ref` указывает на `metric_key` в runtime registry;
- если `METRICS_V2.calc_type = formula`, то `METRICS_V2.calc_ref` указывает на `FORMULAS_V2.formula_id`, а runtime registry для этого `metric_key` хранит `metric_kind = formula` и тот же `formula_id`;
- если `METRICS_V2.calc_type = ratio`, то `METRICS_V2.calc_ref` указывает на `metric_key` runtime-метрики, а детализация числителя и знаменателя живёт только в runtime registry.

Таблица отвечает только за display-слой:
- включена ли строка в витрину;
- как она называется;
- в каком разделе и порядке её показывать;
- какой display-format применить.

Server-side runtime registry отвечает за:
- runtime-смысл метрики;
- source provenance;
- вычислительную семантику;
- missing behavior;
- периодную агрегацию;
- formula/ratio wiring.

## 6. Что Сознательно Не Входит В Этот Слой

В этот слой не нужно смешивать:
- display-поля витрины;
- UI-порядок;
- `section`, `label_ru`, `show_in_data`, `format`;
- полную админку;
- физическую БД-реализацию;
- orchestration;
- deploy/runtime wiring;
- API contract финальной витрины.

## 7. Практический Вывод

Почти напрямую можно зафиксировать:
- `metric_key` как стабильный ключ связи;
- базовые runtime-типы `direct`, `formula`, `ratio`;
- минимальный набор unit/scale/missing/aggregation правил.

Осознанно перепроектировать нужно:
- отделение display-layer от runtime semantics;
- единый runtime-словарь источников и derived-метрик;
- связь `METRICS_V2.calc_ref` с `FORMULAS_V2` и runtime registry без возврата к legacy-смешению.

Следующий практический шаг:
- отдельным bounded шагом зафиксировать pilot runtime-registry fixture для 8-12 ключевых метрик и проверить на нём связку `METRICS_V2.calc_ref -> runtime registry -> FORMULAS_V2`, не переходя пока к полной server-side реализации.
