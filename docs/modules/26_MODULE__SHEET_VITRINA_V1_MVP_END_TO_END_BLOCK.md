---
title: "Модуль: sheet_vitrina_v1_mvp_end_to_end_block"
doc_id: "WB-CORE-MODULE-26-SHEET-VITRINA-V1-MVP-END-TO-END-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sheet_vitrina_v1_mvp_end_to_end_block`."
scope: "Первый bounded end-to-end alignment для `sheet_vitrina_v1`: uploaded compact bootstrap `CONFIG / METRICS / FORMULAS`, сохранённый upload trigger, `Загрузить таблицу` и controlled reverse-load живых server-side данных в `DATA_VITRINA` по current truth."
source_basis:
  - "migration/90_registry_upload_http_entrypoint.md"
  - "migration/91_sheet_vitrina_v1_registry_upload_trigger.md"
  - "migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md"
  - "migration/93_sheet_vitrina_v1_mvp_end_to_end.md"
  - "artifacts/sheet_vitrina_v1_mvp_end_to_end/target/mvp_summary__fixture.json"
  - "artifacts/sheet_vitrina_v1_mvp_end_to_end/evidence/initial__sheet-vitrina-v1-mvp-end-to-end__evidence.md"
related_modules:
  - "gas/sheet_vitrina_v1/RegistryUploadSeedV3.gs"
  - "gas/sheet_vitrina_v1/RegistryUploadTrigger.gs"
  - "gas/sheet_vitrina_v1/PresentationPass.gs"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "packages/application/sheet_vitrina_v1.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/web_source_snapshot_block.py"
  - "packages/adapters/seller_funnel_snapshot_block.py"
related_tables:
  - "CONFIG"
  - "METRICS"
  - "FORMULAS"
  - "DATA_VITRINA"
  - "STATUS"
related_endpoints:
  - "POST /v1/registry-upload/bundle"
  - "GET /v1/sheet-vitrina-v1/plan"
related_runners:
  - "apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py"
  - "apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py"
  - "apps/registry_upload_http_entrypoint_live.py"
related_docs:
  - "migration/90_registry_upload_http_entrypoint.md"
  - "migration/91_sheet_vitrina_v1_registry_upload_trigger.md"
  - "migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md"
  - "migration/93_sheet_vitrina_v1_mvp_end_to_end.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён после возврата operator-facing date-matrix presentation: `prepare -> upload -> load` по-прежнему работает на `33 / 102 / 7`, server-side plan остаётся flat `95` metric keys / `1631` source rows, а `DATA_VITRINA` renders тот же current-truth set как `34` block date-matrix / `1698` rendered rows при одном дне."
---

# 1. Идентификатор и статус

- `module_id`: `sheet_vitrina_v1_mvp_end_to_end_block`
- `family`: `sheet-side`
- `status_transfer`: первый bounded end-to-end MVP перенесён в `wb-core`
- `status_verification`: prepare-to-upload-to-load smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `registry_upload_http_entrypoint_block`
  - `sheet_vitrina_v1_registry_upload_trigger_block`
  - `sheet_vitrina_v1_registry_seed_v3_bootstrap_block`
  - `migration/90_registry_upload_http_entrypoint.md`
  - `migration/91_sheet_vitrina_v1_registry_upload_trigger.md`
  - `migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md`
  - `migration/93_sheet_vitrina_v1_mvp_end_to_end.md`
- Семантика блока: не строить новый parallel server contour и не возвращать full legacy 1:1, а замкнуть практический `prepare -> upload -> load` сценарий на uploaded compact package и уже существующих bounded server-side модулях.

# 3. Target contract и смысл результата

- Канонический operator flow:
  - `Подготовить листы CONFIG / METRICS / FORMULAS`
  - `Отправить реестры на сервер`
  - `Загрузить таблицу`
- Канонический prepare output:
  - `CONFIG` с uploaded compact rows
  - `METRICS` с uploaded compact rows
  - `FORMULAS` с uploaded compact rows
- Канонический upload path:
  - `POST /v1/registry-upload/bundle`
  - request body = existing upload bundle V1
  - response body = canonical `RegistryUploadResult`
- Канонический load path:
  - `GET /v1/sheet-vitrina-v1/plan`
  - response body = existing `SheetVitrinaV1Envelope`-совместимый write plan для `DATA_VITRINA` и `STATUS`

## 3.1 Expanded operator seed bounded шага

- `config_v2 = 33`
- `metrics_v2 = 102`
- `formulas_v2 = 7`
- `enabled + show_in_data = 95`
- server-side plan materialize-ит:
  - `95` enabled+show_in_data metric rows
  - `1631` flat data rows (`47 TOTAL` + `48 * 33 SKU`)
- operator-facing `DATA_VITRINA` materialize-ит:
  - тот же incoming current-truth row set как thin presentation-only `date_matrix`
  - `95` unique metric keys
  - `34` block headers (`1 TOTAL` + `33 SKU`)
  - `33` separator rows
  - `1698` rendered data rows при одном дне
  - header `дата | key | <as_of_date>`

Bounded допущение:
- seed deliberately не равен full legacy dump;
- `METRICS` materialize-ит полный uploaded compact dictionary для sheet/upload/runtime;
- server-side current truth и `STATUS` не режутся до legacy subset;
- `DATA_VITRINA` не режет incoming server plan и делает только presentation-side reshape в data-driven `date_matrix`;
- unsupported live-source tail продолжает фиксироваться в `STATUS`, а не переносится в Apps Script как local truth path.

## 3.2 Явно принятые решения bounded шага

- `openCount` и `open_card_count` сохраняются как разные метрики из разных live sources.
- Все uploaded `total_*` и `avg_*` rows сохраняются:
  - `total_*` = сумма по enabled SKU rows;
  - `avg_*` = arithmetic mean по доступным enabled SKU values.
- Uploaded `section` dictionary считается authoritative и не remap-ится локально.
- `CONFIG!H:I` service/status block сохраняется при `prepare`, `upload`, `load`.

## 3.3 Явный live blocker

- `promo_by_price` и `cogs_by_group` не имеют live HTTP adapters в текущем contour.
- Поэтому full current truth / `STATUS` остаются шире чисто sheet-side presentation pass.
- Это сознательно лучше, чем тихо подменять server contour локальным fixture/rule path или возвращать heavy aggregation logic в Apps Script.

## 3.4 Service block bounded шага

- `CONFIG!H:I` остаётся служебной зоной.
- `CONFIG!I2:I7` сохраняет:
  - `endpoint_url`
  - `last_bundle_version`
  - `last_status`
  - `last_activated_at`
  - `last_http_status`
  - `last_validation_errors`
- Ни `prepare`, ни `load` не должны очищать этот блок.

# 4. Артефакты и wiring по модулю

- target artifacts:
  - `artifacts/sheet_vitrina_v1_mvp_end_to_end/target/mvp_summary__fixture.json`
- parity:
  - `artifacts/sheet_vitrina_v1_mvp_end_to_end/parity/seed-and-runtime-vs-data-vitrina__comparison.md`
- evidence:
  - `artifacts/sheet_vitrina_v1_mvp_end_to_end/evidence/initial__sheet-vitrina-v1-mvp-end-to-end__evidence.md`

# 5. Кодовые части

- bound Apps Script:
  - `gas/sheet_vitrina_v1/RegistryUploadSeedV3.gs`
  - `gas/sheet_vitrina_v1/RegistryUploadTrigger.gs`
  - `gas/sheet_vitrina_v1/PresentationPass.gs`
- application:
  - `packages/application/sheet_vitrina_v1_live_plan.py`
  - `packages/application/sheet_vitrina_v1.py`
  - `packages/application/registry_upload_http_entrypoint.py`
- adapters:
  - `packages/adapters/registry_upload_http_entrypoint.py`
  - `packages/adapters/web_source_snapshot_block.py`
  - `packages/adapters/seller_funnel_snapshot_block.py`
- local harness:
  - `apps/sheet_vitrina_v1_registry_upload_trigger_harness.js`
- smoke:
  - `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`
  - `apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный end-to-end smoke через `apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py`.
- Подтверждён targeted server-driven smoke через `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`.
- Smoke проверяет:
  - что `prepare` поднимает operator seed `33 / 102 / 7`;
  - что upload из sheet-side trigger сохраняет current truth в existing runtime без усечения `metrics_v2`;
  - что `load` ходит в живой HTTP plan endpoint, а не в локальный stub;
  - что `DATA_VITRINA` materialize-ит полный server-driven metric set как `date_matrix` и не режется до `7` metric keys;
  - что repeated load обновляет текущую date-column и добавляет новый день вправо без локального history/truth path;
  - что `STATUS` фиксирует live sources и blocked sources `promo_by_price` / `cogs_by_group`;
  - что service/status block `CONFIG!H:I` сохраняется и не перезаписывается при load.

# 7. Что уже доказано по модулю

- В `wb-core` появился первый bounded end-to-end MVP для `VB-Core Витрина V1`.
- Sheet-side upload registry больше не обрезает `METRICS` до subset: current truth хранит полный uploaded compact dictionary `102` rows.
- Таблица больше не заканчивается на upload-only flow: из уже существующего server-side contour появился controlled reverse-load обратно в `DATA_VITRINA`.
- Readback строится на текущем registry current truth и уже materialized live public source blocks, а не на фейковом локальном fixture.
- `DATA_VITRINA` теперь materialize-ит полный incoming current-truth row set `95` metric keys / `1631` source rows как operator-facing `date_matrix` (`34` blocks / `1698` rendered rows при одном дне) и не теряет `show_in_data` metrics на sheet-side bridge.
- Existing upload contour не ломается: bundle/result contracts и control block сохраняются.

# 8. Что пока не является частью финальной production-сборки

- full legacy parity 1:1 по всем metric sections и registry rows;
- numeric live fill для promo/cogs-backed metrics до появления `promo_by_price` и `cogs_by_group` HTTP adapters;
- full operator-facing legacy parity beyond current server-driven date-matrix scaffold;
- official-api-backed coverage всех historical metrics beyond current uploaded package;
- stable hosted runtime URL и production-bound operator runtime;
- deploy/auth-hardening;
- daily orchestration;
- большой UI/UX redesign таблицы.
