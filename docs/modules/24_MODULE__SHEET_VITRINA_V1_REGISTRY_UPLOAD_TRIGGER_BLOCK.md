---
title: "Модуль: sheet_vitrina_v1_registry_upload_trigger_block"
doc_id: "WB-CORE-MODULE-24-SHEET-VITRINA-V1-REGISTRY-UPLOAD-TRIGGER-BLOCK"
doc_type: "module"
status: "archived"
purpose: "Зафиксировать archive/migration reference по bounded checkpoint блока `sheet_vitrina_v1_registry_upload_trigger_block`."
scope: "Archived operator-facing sheet-side trigger for former Google Sheets `CONFIG / METRICS / FORMULAS` and `COST_PRICE` flows. Apps Script menu/upload actions are not active runtime/update/write/load targets."
source_basis:
  - "migration/86_registry_upload_contract.md"
  - "migration/90_registry_upload_http_entrypoint.md"
  - "migration/91_sheet_vitrina_v1_registry_upload_trigger.md"
  - "artifacts/sheet_vitrina_v1_registry_upload_trigger/input/registry_upload_bundle__fixture.json"
  - "artifacts/sheet_vitrina_v1_registry_upload_trigger/evidence/initial__sheet-vitrina-v1-registry-upload-trigger__evidence.md"
related_modules:
  - "packages/contracts/cost_price_upload.py"
  - "packages/contracts/registry_upload_bundle_v1.py"
  - "packages/contracts/registry_upload_file_backed_service.py"
  - "packages/contracts/registry_upload_http_entrypoint.py"
  - "packages/application/cost_price_upload.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
related_tables:
  - "CONFIG"
  - "METRICS"
  - "FORMULAS"
  - "COST_PRICE"
related_endpoints:
  - "POST /v1/registry-upload/bundle"
  - "POST /v1/cost-price/upload"
related_runners:
  - "apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py"
  - "apps/sheet_vitrina_v1_cost_price_upload_smoke.py"
  - "apps/registry_upload_http_entrypoint_live.py"
related_docs:
  - "migration/90_registry_upload_http_entrypoint.md"
  - "migration/91_sheet_vitrina_v1_registry_upload_trigger.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/18_MODULE__SHEET_VITRINA_V1_WRITE_BRIDGE_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Архивирован: former sheet-side registry/COST_PRICE upload trigger remains only as migration evidence; server HTTP upload contracts remain active outside Google Sheets."
---

# 1. Идентификатор и статус

- `module_id`: `sheet_vitrina_v1_registry_upload_trigger_block`
- `family`: `sheet-side`
- `status_transfer`: operator-facing sheet trigger перенесён в `wb-core`
- `status_verification`: bundle-to-runtime smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`
- `status_current`: `ARCHIVED / DO NOT USE`

Current norm:
- `gas/sheet_vitrina_v1/RegistryUploadTrigger.gs` menu now exposes only archive status.
- `prepareRegistryUploadOperatorSheets`, `uploadRegistryUploadBundle`, `prepareCostPriceSheet`, `uploadCostPriceSheet` and `loadSheetVitrinaTable` are blocked by `ArchiveGuard.gs`.
- Server endpoints `POST /v1/registry-upload/bundle` and `POST /v1/cost-price/upload` remain active server contracts when called by current non-Google-Sheets flows.
- This module must not be used as a Codex completion or verification target.

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `sheet_vitrina_v1_write_bridge_block`
  - `registry_upload_bundle_v1_block`
  - `registry_upload_http_entrypoint_block`
  - `migration/91_sheet_vitrina_v1_registry_upload_trigger.md`
- Семантика блока: не менять server-side runtime и не строить второй UI, а дать bound таблице operator-visible trigger line для двух sibling upload contour'ов:
  - existing compact registry bundle из `CONFIG / METRICS / FORMULAS`;
  - separate `COST_PRICE` dataset с тем же thin HTTP entrypoint boundary.

# 3. Target contract и смысл результата

- Канонический operator input:
  - лист `CONFIG`
  - лист `METRICS`
  - лист `FORMULAS`
- Канонический sibling operator input:
  - лист `COST_PRICE`
  - header = `group | cost_price_rub | effective_from`
- Канонический sheet-side bundle:
  - `bundle_version`
  - `uploaded_at`
  - `config_v2`
  - `metrics_v2`
  - `formulas_v2`
- Канонический upload result с server-side entrypoint:
  - `status`
  - `bundle_version`
  - `accepted_counts`
  - `validation_errors`
  - `activated_at`
- Канонический sibling cost-price payload:
  - `dataset_version`
  - `uploaded_at`
  - `cost_price_rows`
- Канонический sibling cost-price upload result:
  - `status`
  - `dataset_version`
  - `accepted_counts.cost_price_rows`
  - `validation_errors`
  - `activated_at`

## 3.1 Control block bounded шага

- `CONFIG!I2` хранит `endpoint_url`.
- `CONFIG!I3:I7` фиксирует:
  - `last_bundle_version`
  - `last_status`
  - `last_activated_at`
  - `last_http_status`
  - `last_validation_errors`
- Control block не входит в upload bundle и не меняет server-side contract.
- `COST_PRICE!F2` хранит sibling `endpoint_url`.
- `COST_PRICE!F3:F7` фиксирует:
  - `last_dataset_version`
  - `last_status`
  - `last_activated_at`
  - `last_http_status`
  - `last_validation_errors`
- COST_PRICE control block не входит в server payload и не меняет authoritative contract.

## 3.2 Допущение bounded шага

- Автоматический smoke использует локальный harness Apps Script логики и реальный live HTTP entrypoint внутри repo.
- Этот шаг не утверждает, что cloud Apps Script уже может ходить в недеплоенный локальный `localhost`.
- Как только в `CONFIG!I2` указывается внешне достижимый URL materialized entrypoint и Apps Script pushed в bound spreadsheet, тот же trigger становится live operator path без изменения bundle/result contracts.
- Для `COST_PRICE` действует тот же принцип: sheet-side contour остаётся thin shell и может использовать либо свой explicit endpoint URL, либо derived origin от `CONFIG!I2`, но heavy validation и current-state logic остаются server-side.

## 3.3 Current authoritative operator package

- Текущий trigger не режет sheet-side bundle до MVP-subset.
- При current uploaded compact package из листов уходит полный набор:
  - `config_v2 = 33`
  - `metrics_v2 = 102`
  - `formulas_v2 = 7`
- Duplicate rejection не должен сдвигать current truth и только обновляет status block как rejected attempt.
- `COST_PRICE` не подмешивается в current uploaded compact package.
- Separate `Подготовить лист COST_PRICE` и `Отправить себестоимости` не должны менять поведение existing действий:
  - `Подготовить листы CONFIG / METRICS / FORMULAS`
  - `Отправить реестры на сервер`
  - `POST /v1/sheet-vitrina-v1/refresh`
  - `Загрузить таблицу`
- Duplicate `(group, effective_from)` внутри одного COST_PRICE dataset отвергаются server-side как explicit validation error.

# 4. Артефакты и wiring по модулю

- input artifact:
  - `artifacts/sheet_vitrina_v1_registry_upload_trigger/input/registry_upload_bundle__fixture.json`
- target artifacts:
  - `artifacts/sheet_vitrina_v1_registry_upload_trigger/target/bundle_from_sheets__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_upload_trigger/target/upload_response__accepted__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_upload_trigger/target/upload_response__duplicate_bundle_version__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_upload_trigger/target/current_state__fixture.json`
- parity:
  - `artifacts/sheet_vitrina_v1_registry_upload_trigger/parity/sheets-vs-bundle__comparison.md`
- evidence:
  - `artifacts/sheet_vitrina_v1_registry_upload_trigger/evidence/initial__sheet-vitrina-v1-registry-upload-trigger__evidence.md`

# 5. Кодовые части

- bound Apps Script:
  - `gas/sheet_vitrina_v1/RegistryUploadTrigger.gs`
  - `gas/sheet_vitrina_v1/appsscript.json`
- reused runtime path:
  - `packages/application/registry_upload_http_entrypoint.py`
  - `packages/application/registry_upload_db_backed_runtime.py`
- local harness:
  - `apps/sheet_vitrina_v1_registry_upload_trigger_harness.js`
- smoke:
  - `apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py`
  - `apps/sheet_vitrina_v1_cost_price_upload_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный bundle-to-runtime smoke через `apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py`.
- Подтверждён локальный COST_PRICE prepare-to-upload smoke через `apps/sheet_vitrina_v1_cost_price_upload_smoke.py`.
- Smoke проверяет:
  - что operator sheets `CONFIG / METRICS / FORMULAS` materialize-ят канонический bundle;
  - что именно Apps Script upload function делает thin POST в существующий HTTP entrypoint;
  - что accepted response возвращается в канонической форме;
  - что duplicate `bundle_version` отвергается и фиксируется в operator status block;
  - что current truth обновляется через уже существующий runtime DB;
  - что sheet-built bundle сохраняет все rows uploaded compact package, а не только pilot/MVP-subset.
- COST_PRICE smoke дополнительно проверяет:
  - что `Подготовить лист COST_PRICE` materialize-ит точные headers `group / cost_price_rub / effective_from`;
  - что sheet-side payload для себестоимостей уходит отдельно от `config_v2 / metrics_v2 / formulas_v2`;
  - что accepted COST_PRICE upload обновляет separate runtime current state;
  - что duplicate `dataset_version` фиксируется в COST_PRICE status block как rejected attempt.

# 7. Что уже доказано по модулю

- В `wb-core` появился первый operator-facing trigger для загрузки реестров из новой таблицы.
- В том же Apps Script contour появился отдельный bounded trigger для `COST_PRICE`, но он не смешивается с compact registry bundle.
- Trigger не дублирует server-side validation/runtime логику, а пишет в уже materialized HTTP entrypoint.
- В таблице появились отдельные service-листы `CONFIG`, `METRICS`, `FORMULAS` и отдельный input/service sheet `COST_PRICE`.
- Оператор получает минимальный persisted feedback по последней загрузке в control block `CONFIG!I2:I7`.
- Для себестоимостей оператор получает аналогичный persisted feedback в `COST_PRICE!F2:F7`.
- Sheet-side upload теперь отправляет current authoritative registry lists, уже согласованные с uploaded compact package.
- COST_PRICE contour по-прежнему остаётся upload-only на sheet-side, но его authoritative dataset теперь используется server-side refresh/read contour без новой Apps Script truth logic.

# 8. Что пока не является частью финальной production-сборки

- загрузка server-side current truth обратно в таблицу;
- server-side integration logic `COST_PRICE` в `DATA_VITRINA` / `STATUS` beyond emitting the upload itself;
- отдельная кнопка `обновить витрину`;
- deploy и внешняя доступность entrypoint;
- auth-hardening;
- orchestration и daily operator workflow;
- большой UI/UX redesign таблицы.
