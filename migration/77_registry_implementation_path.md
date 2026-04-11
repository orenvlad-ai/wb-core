# Практический путь реализации registry layer для новой витрины

## 1. Назначение слоя

Этот документ фиксирует практический путь, который закрывает разрыв между:
- табличными V2-реестрами для витрины;
- server-side runtime semantics для метрик;
- первым рабочим server-side consumption path новой витрины.

Нужен не ещё один монолитный лист, а понятная переходная модель:
- что можно оставить в лёгких V2-реестрах;
- что уже должно жить как server-side truth;
- как пережить этап до появления полноценной admin/UI-формы.

## 2. Какие реестры должны существовать

Минимально должны существовать четыре отдельных слоя:
- `CONFIG_V2` как SKU/display registry;
- `METRICS_V2` как display registry метрик;
- `FORMULAS_V2` как короткий словарь формул;
- `metric runtime registry` как server-side registry вычислительной семантики.

## 3. Где живёт каноника

| Слой | Source of truth | Временный bridge | Что не должно быть каноникой |
| --- | --- | --- | --- |
| `CONFIG_V2` | Отдельный V2-реестр display-SKU | Нормализованный экспорт subset из legacy `CONFIG` до появления отдельной формы ведения | legacy `CONFIG` целиком, ad-hoc fixtures, row-order без явного `display_order` |
| `METRICS_V2` | Отдельный V2 display-registry | Нормализованный экспорт subset из legacy `METRICS` без runtime-полей | legacy `METRICS` как смешанный монолит, runtime-поля внутри табличной схемы |
| `FORMULAS_V2` | Отдельный V2-реестр формул | Нормализованный экспорт актуального formula-subset из legacy-таблицы | формулы, размазанные по `METRICS`, code constants, ручные листовые ссылки |
| `metric runtime registry` | Версионируемый server-side registry в Git | Pilot fixture / versioned file до появления постоянного server-side storage | legacy `METRICS`, табличные V2-листы, runtime-логика в display-реестре |

## 4. Переходная модель

До появления полноценной admin/UI-формы можно жить так:
- `CONFIG_V2`, `METRICS_V2`, `FORMULAS_V2` временно поддерживаются как тонкие табличные V2-реестры или их нормализованные экспорты;
- server-side runtime registry с первого шага считается не табличным, а серверным truth и хранится в Git как versioned artifact;
- bridge между таблицей и сервером выглядит как контролируемый экспорт V2-реестров в нормализованный bundle, который сервер уже может читать без legacy-листа целиком.

Временно через таблицу допустимо править:
- SKU display-поля;
- display-поля метрик;
- словарь формул.

Уже должно считаться server-side truth:
- `metric_kind`;
- `value_unit`;
- `value_scale`;
- `missing_policy`;
- `period_agg`;
- `ratio_num_key`;
- `ratio_den_key`;
- `source_family`;
- `source_module`;
- `is_runtime_enabled`.

## 5. Граница между display и runtime

Ради витрины редактируется:
- состав SKU;
- display name и group;
- display-порядок;
- label, section, format, `show_in_data`;
- привязка display-строки к `calc_ref`.

Ради вычислительной семантики существует:
- тип метрики;
- origin/source provenance;
- правила агрегации;
- missing behavior;
- ratio/formula wiring;
- runtime enable/disable.

Их нельзя снова смешивать в один монолитный `METRICS`-лист, потому что:
- display-изменения и runtime-изменения имеют разный owner и разный риск;
- table-facing registry должен быть лёгким и операторским;
- server-side semantics должна быть versioned, reviewable и не зависеть от скрытой spreadsheet-логики.

## 6. Минимальная последовательность реализации

1. Зафиксировать pilot bundle из четырёх реестров для ограниченного набора SKU и 8-12 ключевых метрик.
2. Зафиксировать формат нормализованного bridge-export для `CONFIG_V2`, `METRICS_V2`, `FORMULAS_V2`, который сервер сможет читать без legacy-листа целиком.
3. Зафиксировать file-based runtime registry fixture и явную связку `METRICS_V2.calc_ref -> runtime registry -> FORMULAS_V2`.
4. Подключить один bounded server-side consumer к этому bundle и проверить, что первая витрина может читать display-слой отдельно от runtime semantics.

## 7. Что сознательно не входит

В этот шаг не входят:
- полный UI/админка;
- полный `CONFIG/METRICS` migration;
- orchestration;
- новая таблица целиком;
- полная БД-реализация;
- deploy/runtime wiring;
- перенос legacy-листов `1:1`.

## 8. Следующий практический шаг

Следующий шаг: отдельно зафиксировать pilot registry bundle для 5-10 SKU и 8-12 метрик, где одновременно будут присутствовать:
- `CONFIG_V2`;
- `METRICS_V2`;
- `FORMULAS_V2`;
- `metric runtime registry`;
- и один нормализованный bridge-export, на котором уже можно будет делать bounded implementation первого registry-layer.
