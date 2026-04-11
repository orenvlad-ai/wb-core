# Минимальная целевая схема `CONFIG_V2 / METRICS_V2 / FORMULAS_V2`

## 1. Назначение документа

Этот документ фиксирует минимальную будущую табличную схему для первой новой витрины:
- `CONFIG_V2` как тонкий SKU/display registry;
- `METRICS_V2` как display-registry витрины;
- `FORMULAS_V2` как короткий реестр формул.

Текущие legacy-регистры не нужно переносить `1:1`, потому что:
- `CONFIG` в legacy живёт как sheet-specific operator input и несёт лишнюю spreadsheet-semantics;
- `METRICS` сейчас смешивает display, tabular runtime и часть server/runtime semantics;
- `FORMULAS` в V2 нужен только как минимальный словарь формульных ссылок, а не как перенос всего табличного движка.

## 2. `CONFIG_V2`

### Минимальная схема

| Поле | Тип | Обязательно | Описание | Пример |
| --- | --- | --- | --- | --- |
| `nm_id` | `int` | да | Канонический SKU-ключ витрины | `210183919` |
| `enabled` | `bool` | да | Показывать SKU в витрине или нет | `true` |
| `display_name` | `string` | да | Человеческое имя SKU для таблицы | `Защитное стекло iPhone 14` |
| `group` | `string` | нет | Группа SKU для display/grouping | `iphone_glass` |
| `display_order` | `int` | да | Стабильный порядок показа в витрине | `120` |

### Что сознательно не тащим из legacy `CONFIG`

Не тащим:
- sheet-specific заголовки вроде `sku(nmId)` и их варианты;
- boolean-like spreadsheet значения вместо нормального `bool`;
- скрытую row-order semantics без явного `display_order`;
- любые дополнительные operator-поля, не нужные первой витрине.

## 3. `METRICS_V2`

### Минимальная схема

| Поле | Тип | Обязательно | Описание | Пример |
| --- | --- | --- | --- | --- |
| `metric_key` | `string` | да | Канонический ключ метрики в витрине | `price_seller_discounted` |
| `enabled` | `bool` | да | Показывать метрику в витрине или нет | `true` |
| `scope` | `enum[string]` | да | Где живёт метрика: `SKU`, `GROUP`, `TOTAL` | `SKU` |
| `label_ru` | `string` | да | Русский display-label | `Цена со скидкой` |
| `calc_type` | `enum[string]` | да | Как витрина получает значение: `projection`, `formula` | `projection` |
| `calc_ref` | `string` | да | Ссылка на projection field или `formula_id` | `official_api.prices.price_seller_discounted` |
| `show_in_data` | `bool` | да | Показывать ли строку в основной витрине | `true` |
| `format` | `enum[string]` | да | Display-формат ячейки | `rub` |
| `display_order` | `int` | да | Порядок показа внутри секции | `210` |
| `section` | `string` | да | Раздел витрины | `Цены` |

### Почему это display-registry, а не runtime-registry

`METRICS_V2` отвечает только за:
- что показывать;
- как подписывать;
- как форматировать;
- в каком порядке выводить;
- откуда брать готовое значение для display.

`METRICS_V2` не должна хранить тяжёлую runtime-semantics:
- как сервер типизирует метрику;
- как сервер обрабатывает missing values;
- как сервер считает ratio/aggregation;
- как сервер хранит aliases и units.

### Что сознательно не входит в `METRICS_V2`

Из старой `METRICS` и server registry сознательно не входят:
- `source`;
- `formula` как отдельное legacy-поле, оно заменяется связкой `calc_type + calc_ref`;
- `type` как табличная runtime-semantics;
- `ui_parent`;
- `ui_collapse`;
- `agg_scope`;
- server-side поля:
  - `metric_kind`
  - `value_unit`
  - `value_scale`
  - `missing_policy`
  - `period_agg`
  - `ratio_num_key`
  - `ratio_den_key`

Все эти поля должны жить вне табличной витрины, в server-side runtime semantics.

## 4. `FORMULAS_V2`

### Минимальная схема

| Поле | Тип | Обязательно | Описание | Пример |
| --- | --- | --- | --- | --- |
| `formula_id` | `string` | да | Канонический идентификатор формулы | `margin_net_v1` |
| `expression` | `string` | да | Текст формульного выражения | `(fin_buyout_rub - fin_commission - fin_storage_fee)` |
| `description` | `string` | да | Короткое описание для человека | `Чистая выручка после комиссии и хранения` |

## 5. Разделение ответственности

- `CONFIG_V2` отвечает за SKU/display registry и стабильный состав строк витрины.
- `METRICS_V2` отвечает за display-registry метрик: label, section, format, order и display binding.
- `FORMULAS_V2` отвечает только за словарь формульных ссылок, используемых через `METRICS_V2.calc_ref`.
- В server-side runtime registry должны оставаться:
  - metric typing;
  - units/scales;
  - missing policy;
  - ratio semantics;
  - aggregation semantics;
  - aliases и другие поля вычислительного runtime.

## 6. Практический вывод

- Почти напрямую эволюционно можно взять:
  - `CONFIG -> CONFIG_V2`, но с явным `display_order` и нормализованными типами;
  - `FORMULAS -> FORMULAS_V2` как короткий реестр `formula_id -> expression`.
- Осознанно перепроектировать нужно:
  - `METRICS -> METRICS_V2`, потому что V2 должна стать display-registry, а не смесью display и runtime semantics.
- Следующий шаг:
  - отдельно зафиксировать минимальную server-side registry semantics для метрик, которая будет обслуживать `METRICS_V2.calc_ref`, но не попадёт в табличную V2-схему.
