# Sheet Vitrina V1 Registry Seed V3 Bootstrap

## 1. Что Это Такое

`sheet_vitrina_v1_registry_seed_v3_bootstrap_block` — bounded implementation step поверх уже смёрженного `sheet_vitrina_v1_registry_upload_trigger_block`.

Этот шаг меняет семантику подготовки листов `CONFIG / METRICS / FORMULAS`:
- вместо пустых операторских листов;
- и вместо legacy-формата;
- таблица получает сразу materialized compact v3 seed из uploaded package, совместимый с текущей upload/runtime линией.

## 2. Что Именно Покрывает Блок

Блок покрывает:
- импорт compact v3 seed-pack из локального внешнего источника в канонические repo fixtures;
- Apps Script bootstrap `CONFIG / METRICS / FORMULAS` уже заполненными compact v3 registry rows;
- сохранение service/status зоны в `CONFIG`, включая `endpoint_url` и последние upload-поля;
- доказательство, что новый bootstrap не ломает уже живой upload trigger.

## 3. Канонический Compact V3 Формат

### 3.1 FORMULAS

- `formula_id`
- `expression`
- `description`

### 3.2 CONFIG

- `nm_id`
- `enabled`
- `display_name`
- `group`
- `display_order`

Service/control block остаётся отдельно:
- `CONFIG!H:I`
- `CONFIG!I2:I7` для `endpoint_url`, `last_bundle_version`, `last_status`, `last_activated_at`, `last_http_status`, `last_validation_errors`

### 3.3 METRICS

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

## 4. Bounded Допущение По Seed-Pack

Внешний seed-pack v3 materialize-ится в repo как repo-owned uploaded package и как canonical sheet bootstrap.

Для текущего bounded checkpoint в repo materialize-ится current authoritative package:
- `config_v2 = 33`
- `metrics_v2 = 102`
- `formulas_v2 = 7`

Минимальные нормализации bounded шага:
- raw uploaded files сохраняются отдельно как source artifact;
- sheet headers и bundle contract остаются каноническими `config_v2 / metrics_v2 / formulas_v2`;
- `CONFIG!H:I` service block сохраняется и не смешивается с uploaded rows.

Это не трактуется как финальная authoritative schema всего legacy-миррора, но закрывает repo drift относительно uploaded package пользователя.

## 5. Где Это Materialize-ится В Repo

- input fixtures:
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/input/config_v3_seed__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/input/metrics_v3_seed__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/input/formulas_v3_seed__fixture.json`
- GAS seed source:
  - `gas/sheet_vitrina_v1/RegistryUploadSeedV3.gs`
- updated trigger:
  - `gas/sheet_vitrina_v1/RegistryUploadTrigger.gs`
- harness:
  - `apps/sheet_vitrina_v1_registry_upload_trigger_harness.js`
- smoke:
  - `apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py`

## 6. Что Проверяет Smoke

`apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py` проверяет:
- что `prepareRegistryUploadOperatorSheets()` создаёт `CONFIG / METRICS / FORMULAS`;
- что листы получают именно compact v3 headers и compact v3 seed rows;
- что повторная подготовка не теряет service/status block;
- что собранный из листов bundle проходит через уже существующий HTTP upload path;
- что runtime current truth обновляется без поломки existing upload semantics.

## 7. Что Не Входит В Этот Шаг

- reverse-load server-side current truth обратно в таблицу;
- новая кнопка `обновить витрину`;
- server-side redesign;
- deploy/auth-hardening;
- большой UI redesign таблицы;
- полный legacy-normalization pass по всем историческим registry rows.
