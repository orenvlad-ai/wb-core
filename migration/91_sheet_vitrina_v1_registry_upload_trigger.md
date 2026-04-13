# Sheet Vitrina V1 Registry Upload Trigger

## 1. Что Это Такое

`sheet_vitrina_v1_registry_upload_trigger_block` — следующий bounded implementation step после `registry_upload_http_entrypoint_block`.

Этот шаг добавляет первый operator-facing sheet-side trigger для отправки `CONFIG / METRICS / FORMULAS` в уже materialized registry upload HTTP entrypoint.

## 2. Что Именно Покрывает Блок

Блок покрывает минимальный operator flow:
- подготовить листы `CONFIG`, `METRICS`, `FORMULAS`;
- дать оператору понятную tabular-структуру для ручного ввода;
- собрать из этих листов канонический registry upload bundle;
- отправить bundle в `POST /v1/registry-upload/bundle`;
- показать оператору последний upload result без загрузки витрины обратно.

## 3. Табличная Семантика Блока

Для bounded шага фиксируется минимальная структура:
- `CONFIG`:
  - `nm_id`
  - `enabled`
  - `display_name`
  - `group`
  - `display_order`
- `METRICS`:
  - `metric_key`
  - `enabled`
  - `scope`
  - `label_ru`
  - `calc_type`
  - `calc_ref`
  - `show_in_data`
  - `format`
  - `display_order`
  - `section`
- `FORMULAS`:
  - `formula_id`
  - `expression`
  - `description`

Control block bounded шага:
- `CONFIG!I2` хранит `endpoint_url`;
- `CONFIG!I3:I7` фиксирует последний `bundle_version`, `status`, `activated_at`, `http_status` и `validation_errors`.

Bundle meta для bounded шага генерируется на sheet-side:
- `uploaded_at`
- `bundle_version`

## 4. Чем Это Отличается От HTTP Entrypoint

`registry_upload_http_entrypoint_block` доказывал:
- первый live HTTP boundary;
- thin request -> runtime -> response wiring;
- server-side `activated_at`.

`sheet_vitrina_v1_registry_upload_trigger_block` добавляет:
- operator-facing tabular input;
- Apps Script menu trigger;
- sheet-side bundle assembly;
- Apps Script `UrlFetchApp` wiring к уже существующему HTTP entrypoint;
- operator-visible result block в таблице.

## 5. Допущение Bounded Шага

- Для автоматического smoke внутри repo используется локальный harness bound Apps Script логики + существующий live HTTP entrypoint.
- Это не заменяет manual/live прогон в реально bound spreadsheet через `clasp push`.
- Как только entrypoint становится внешне достижимым и Apps Script код pushed в bound spreadsheet, тот же trigger работает без изменения контракта.

## 6. Что Остаётся Следующим Шагом

Следующий bounded step после этого блока:
- загрузить server-side current truth / витрину обратно в таблицу отдельным controlled flow;
- не смешивать это с deploy, auth-hardening и production orchestration.
