# wbcore_registry_seed_v3_compact

Этот пакет пересобран уже под **новые компактные заголовки**, присланные скриншотами.

## 1. Формы листов

### FORMULAS
- formula_id
- expression
- description

### CONFIG
Основной блок:
- nm_id
- enabled
- display_name
- group
- display_order

Сервисный блок:
- key
- value

Технически между основным и сервисным блоком оставлены пустые колонки F:G для визуального разделения, как на скриншоте.

### METRICS
- metric_key
- enabled
- scope
- label_ru
- calc_type
- calc_ref
- show_in_data
- format
- display_order
- section

## 2. Что было сделано

- исходные legacy-реестры перечитаны из трёх Excel-файлов
- заголовки приведены **ровно** к новой компактной форме
- пустые хвосты строк/колонок убраны
- `avg_ocalizationPercent` исправлен на `avg_localizationPercent`

## 3. Принятые mapping-правила

### CONFIG
- sku -> nm_id
- active -> enabled
- comment -> display_name
- group -> group
- row order -> display_order

### FORMULAS
- comment -> description

### METRICS
- metric -> metric_key
- enabled -> enabled
- scope -> scope
- label_ru -> label_ru
- source/formula/ratio_* схлопнуты в:
  - calc_type
  - calc_ref

Использован минимальный компактный mapping:
- FORMULA -> calc_type = formula, calc_ref = formula_id
- ratio rows -> calc_type = ratio, calc_ref = "num/den"
- все остальные -> calc_type = metric, calc_ref = source или metric_key

### format
Нормализован в компактные значения:
- rub
- percent
- integer

### section
Разложен эвристически по смысловым блокам:
- Воронка
- Поиск
- Цены
- Реклама
- Запасы
- Финансы
- Акции
- Экономика
- Отзывы
- Разное

## 4. Что ещё стоит вручную проверить перед встраиванием в live sheet

- alias-зону `openCount` / `open_card_count`
- нужны ли все legacy TOTAL_SUM / TOTAL_AVG строки в новом compact METRICS без дополнительной фильтрации
- нужен ли другой словарь `section`
- нужно ли в CONFIG сохранять пустые значения `endpoint_url / last_bundle_version / last_status` или подхватывать живые значения из таблицы без перезаписи
