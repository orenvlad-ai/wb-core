---
title: "Модуль: registry_upload_db_backed_runtime_block"
doc_id: "WB-CORE-MODULE-22-REGISTRY-UPLOAD-DB-BACKED-RUNTIME-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `registry_upload_db_backed_runtime_block`."
scope: "Локальный SQLite-backed runtime ingest для V2-реестров: persistent current state, version history, upload result в DB и smoke полного runtime flow без Apps Script UI и внешнего API."
source_basis:
  - "migration/86_registry_upload_contract.md"
  - "migration/88_registry_upload_file_backed_service.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "artifacts/registry_upload_db_backed_runtime/input/registry_upload_bundle__fixture.json"
  - "artifacts/registry_upload_db_backed_runtime/evidence/initial__registry-upload-db-backed-runtime__evidence.md"
related_modules:
  - "packages/contracts/registry_upload_bundle_v1.py"
  - "packages/application/registry_upload_bundle_v1.py"
  - "packages/contracts/registry_upload_file_backed_service.py"
  - "packages/application/registry_upload_file_backed_service.py"
  - "packages/contracts/registry_upload_db_backed_runtime.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
related_tables:
  - "CONFIG_V2"
  - "METRICS_V2"
  - "FORMULAS_V2"
related_endpoints: []
related_runners:
  - "apps/registry_upload_bundle_v1_smoke.py"
  - "apps/registry_upload_file_backed_service_smoke.py"
  - "apps/registry_upload_db_backed_runtime_smoke.py"
related_docs:
  - "migration/86_registry_upload_contract.md"
  - "migration/88_registry_upload_file_backed_service.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "docs/modules/21_MODULE__REGISTRY_UPLOAD_FILE_BACKED_SERVICE_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ для первого DB-backed runtime слоя registry upload."
---

# 1. Идентификатор и статус

- `module_id`: `registry_upload_db_backed_runtime_block`
- `family`: `registry`
- `status_transfer`: DB-backed runtime ingest перенесён в `wb-core`
- `status_verification`: full runtime smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `registry_upload_bundle_v1_block`
  - `registry_upload_file_backed_service_block`
  - `migration/86_registry_upload_contract.md`
  - `migration/89_registry_upload_db_backed_runtime.md`
- Семантика блока: принять уже собранный bundle, переиспользовать текущий validator и materialize-ить current server-side truth в DB-backed runtime storage до любого live API entrypoint.

# 3. Target contract и смысл результата

- Канонический input:
  - `bundle_version`
  - `uploaded_at`
  - `config_v2`
  - `metrics_v2`
  - `formulas_v2`
- Канонический output result:
  - `status`
  - `bundle_version`
  - `accepted_counts`
  - `validation_errors`
  - `activated_at`
- Runtime DB materialize-ит:
  - version history для принятых bundle;
  - persisted upload result;
  - current-state pointer;
  - versioned rows `config_v2`, `metrics_v2`, `formulas_v2`.

## 3.1 Допущение bounded шага

- Внутри `wb-core` этот шаг использует локальный SQLite-файл как минимальный DB-backed analog server-side runtime.
- Это не является решением, что production target storage обязан быть SQLite.
- Финальная Postgres/storage model остаётся отдельным архитектурным вопросом вне scope этого блока.

# 4. Артефакты и wiring по модулю

- input artifact:
  - `artifacts/registry_upload_db_backed_runtime/input/registry_upload_bundle__fixture.json`
- target artifacts:
  - `artifacts/registry_upload_db_backed_runtime/target/upload_result__accepted__fixture.json`
  - `artifacts/registry_upload_db_backed_runtime/target/upload_result__duplicate_bundle_version__fixture.json`
  - `artifacts/registry_upload_db_backed_runtime/target/current_state__fixture.json`
  - `artifacts/registry_upload_db_backed_runtime/target/version_index__fixture.json`
- parity:
  - `artifacts/registry_upload_db_backed_runtime/parity/input-vs-runtime__comparison.md`
- evidence:
  - `artifacts/registry_upload_db_backed_runtime/evidence/initial__registry-upload-db-backed-runtime__evidence.md`

# 5. Кодовые части

- contracts:
  - `packages/contracts/registry_upload_db_backed_runtime.py`
- application:
  - `packages/application/registry_upload_db_backed_runtime.py`
- reused validator:
  - `packages/application/registry_upload_bundle_v1.py`
- reused result contract:
  - `packages/contracts/registry_upload_file_backed_service.py`
- smoke:
  - `apps/registry_upload_db_backed_runtime_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный full runtime smoke через `apps/registry_upload_db_backed_runtime_smoke.py`.
- Smoke проверяет:
  - что bundle ingest-ится в runtime DB;
  - что accepted upload result persist-ится и читается обратно из DB;
  - что current server-side truth реконструируется из DB в канонической форме;
  - что version index materialize-ится в DB-backed storage;
  - что duplicate `bundle_version` отвергается и не двигает current state.

# 7. Что уже доказано по модулю

- upload line больше не заканчивается на file-backed simulation: есть локальный DB-backed runtime слой.
- Current truth уже materialize-ится как server-side runtime state, а не только как JSON-marker.
- Новый слой является прямой технической базой под будущий тонкий API/entrypoint для загрузки реестров из `VB-Core Витрина V1`.

# 8. Что пока не является частью финальной production-сборки

- Apps Script upload button;
- Google Sheets UI;
- live operator-facing API endpoint;
- deploy и orchestration;
- production Postgres schema и внешняя инфраструктура.
