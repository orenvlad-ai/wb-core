---
title: "Модуль: sheet_vitrina_v1_registry_upload_trigger_block"
doc_id: "WB-CORE-MODULE-24-SHEET-VITRINA-V1-REGISTRY-UPLOAD-TRIGGER-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sheet_vitrina_v1_registry_upload_trigger_block`."
scope: "Первый operator-facing sheet-side trigger для отправки V2-реестров: листы `CONFIG / METRICS / FORMULAS`, Apps Script menu, bundle assembly, thin HTTP POST в существующий entrypoint и минимальный feedback для оператора."
source_basis:
  - "migration/86_registry_upload_contract.md"
  - "migration/90_registry_upload_http_entrypoint.md"
  - "migration/91_sheet_vitrina_v1_registry_upload_trigger.md"
  - "artifacts/sheet_vitrina_v1_registry_upload_trigger/input/registry_upload_bundle__fixture.json"
  - "artifacts/sheet_vitrina_v1_registry_upload_trigger/evidence/initial__sheet-vitrina-v1-registry-upload-trigger__evidence.md"
related_modules:
  - "packages/contracts/registry_upload_bundle_v1.py"
  - "packages/contracts/registry_upload_file_backed_service.py"
  - "packages/contracts/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
related_tables:
  - "CONFIG"
  - "METRICS"
  - "FORMULAS"
related_endpoints:
  - "POST /v1/registry-upload/bundle"
related_runners:
  - "apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py"
  - "apps/registry_upload_http_entrypoint_live.py"
related_docs:
  - "migration/90_registry_upload_http_entrypoint.md"
  - "migration/91_sheet_vitrina_v1_registry_upload_trigger.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/18_MODULE__SHEET_VITRINA_V1_WRITE_BRIDGE_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ для первого sheet-side operator trigger загрузки реестров."
---

# 1. Идентификатор и статус

- `module_id`: `sheet_vitrina_v1_registry_upload_trigger_block`
- `family`: `sheet-side`
- `status_transfer`: operator-facing sheet trigger перенесён в `wb-core`
- `status_verification`: bundle-to-runtime smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль находится в PR и предназначен для merge в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `sheet_vitrina_v1_write_bridge_block`
  - `registry_upload_bundle_v1_block`
  - `registry_upload_http_entrypoint_block`
  - `migration/91_sheet_vitrina_v1_registry_upload_trigger.md`
- Семантика блока: не менять server-side runtime и не строить второй UI, а дать bound таблице первый operator-visible trigger, который собирает bundle из `CONFIG / METRICS / FORMULAS` и шлёт его в уже существующий HTTP entrypoint.

# 3. Target contract и смысл результата

- Канонический operator input:
  - лист `CONFIG`
  - лист `METRICS`
  - лист `FORMULAS`
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

## 3.1 Control block bounded шага

- `CONFIG!I2` хранит `endpoint_url`.
- `CONFIG!I3:I7` фиксирует:
  - `last_bundle_version`
  - `last_status`
  - `last_activated_at`
  - `last_http_status`
  - `last_validation_errors`
- Control block не входит в upload bundle и не меняет server-side contract.

## 3.2 Допущение bounded шага

- Автоматический smoke использует локальный harness Apps Script логики и реальный live HTTP entrypoint внутри repo.
- Этот шаг не утверждает, что cloud Apps Script уже может ходить в недеплоенный локальный `localhost`.
- Как только в `CONFIG!I2` указывается внешне достижимый URL materialized entrypoint и Apps Script pushed в bound spreadsheet, тот же trigger становится live operator path без изменения bundle/result contracts.

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

# 6. Какой smoke подтверждён

- Подтверждён локальный bundle-to-runtime smoke через `apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py`.
- Smoke проверяет:
  - что operator sheets `CONFIG / METRICS / FORMULAS` materialize-ят канонический bundle;
  - что именно Apps Script upload function делает thin POST в существующий HTTP entrypoint;
  - что accepted response возвращается в канонической форме;
  - что duplicate `bundle_version` отвергается и фиксируется в operator status block;
  - что current truth обновляется через уже существующий runtime DB.

# 7. Что уже доказано по модулю

- В `wb-core` появился первый operator-facing trigger для загрузки реестров из новой таблицы.
- Trigger не дублирует server-side validation/runtime логику, а пишет в уже materialized HTTP entrypoint.
- В таблице появились отдельные service-листы `CONFIG`, `METRICS`, `FORMULAS`.
- Оператор получает минимальный persisted feedback по последней загрузке в control block `CONFIG!I2:I7`.

# 8. Что пока не является частью финальной production-сборки

- загрузка server-side current truth обратно в таблицу;
- отдельная кнопка `обновить витрину`;
- deploy и внешняя доступность entrypoint;
- auth-hardening;
- orchestration и daily operator workflow;
- большой UI/UX redesign таблицы.
