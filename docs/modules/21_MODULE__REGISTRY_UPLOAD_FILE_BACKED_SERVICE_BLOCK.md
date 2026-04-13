---
title: "Модуль: registry_upload_file_backed_service_block"
doc_id: "WB-CORE-MODULE-21-REGISTRY-UPLOAD-FILE-BACKED-SERVICE-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `registry_upload_file_backed_service_block`."
scope: "Локальный file-backed upload service для V2-реестров: accepted version artifact, current marker, канонический upload result и smoke полного flow без API/БД/GAS UI."
source_basis:
  - "migration/86_registry_upload_contract.md"
  - "migration/87_registry_upload_bundle_v1.md"
  - "migration/88_registry_upload_file_backed_service.md"
  - "artifacts/registry_upload_file_backed_service/input/registry_upload_bundle__fixture.json"
  - "artifacts/registry_upload_file_backed_service/evidence/initial__registry-upload-file-backed-service__evidence.md"
related_modules:
  - "packages/contracts/registry_upload_bundle_v1.py"
  - "packages/application/registry_upload_bundle_v1.py"
  - "packages/contracts/registry_upload_file_backed_service.py"
  - "packages/application/registry_upload_file_backed_service.py"
related_tables:
  - "CONFIG_V2"
  - "METRICS_V2"
  - "FORMULAS_V2"
related_endpoints: []
related_runners:
  - "apps/registry_upload_bundle_v1_smoke.py"
  - "apps/registry_upload_file_backed_service_smoke.py"
related_docs:
  - "migration/86_registry_upload_contract.md"
  - "migration/87_registry_upload_bundle_v1.md"
  - "migration/88_registry_upload_file_backed_service.md"
  - "docs/modules/20_MODULE__REGISTRY_UPLOAD_BUNDLE_V1_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ для первого file-backed accept/store/activate слоя registry upload."
---

# 1. Идентификатор и статус

- `module_id`: `registry_upload_file_backed_service_block`
- `family`: `registry`
- `status_transfer`: file-backed upload service перенесён в `wb-core`
- `status_verification`: full upload-flow smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `registry_upload_bundle_v1_block`
  - `migration/86_registry_upload_contract.md`
  - `migration/88_registry_upload_file_backed_service.md`
  - `registry/pilot_bundle/metric_runtime_registry.json`
- Семантика блока: принять уже собранный bundle, переиспользовать текущий validator и materialize-ить локальный analog `accept/store/activate/result` до любого live endpoint или DB runtime.

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
- File-backed store materialize-ит:
  - accepted version artifact;
  - upload result artifact;
  - current marker.

## 3.1 Допущение bounded шага

- Для имени файла `bundle_version` нормализуется заменой `:` на `-`.
- Duplicate `bundle_version` считается hard rejection.
- Duplicate rejection не перезаписывает accepted/current state; если versioned result file уже существует, service возвращает rejection in-memory.

# 4. Артефакты и wiring по модулю

- input artifact:
  - `artifacts/registry_upload_file_backed_service/input/registry_upload_bundle__fixture.json`
- target artifacts:
  - `artifacts/registry_upload_file_backed_service/target/accepted_bundle__fixture.json`
  - `artifacts/registry_upload_file_backed_service/target/upload_result__accepted__fixture.json`
  - `artifacts/registry_upload_file_backed_service/target/upload_result__duplicate_bundle_version__fixture.json`
  - `artifacts/registry_upload_file_backed_service/target/current_marker__fixture.json`
- parity:
  - `artifacts/registry_upload_file_backed_service/parity/input-vs-storage__comparison.md`
- evidence:
  - `artifacts/registry_upload_file_backed_service/evidence/initial__registry-upload-file-backed-service__evidence.md`

# 5. Кодовые части

- contracts:
  - `packages/contracts/registry_upload_file_backed_service.py`
- application:
  - `packages/application/registry_upload_file_backed_service.py`
- reused validator:
  - `packages/application/registry_upload_bundle_v1.py`
- smoke:
  - `apps/registry_upload_file_backed_service_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный full-flow smoke через `apps/registry_upload_file_backed_service_smoke.py`.
- Smoke проверяет:
  - что bundle принимается из artifact-backed input;
  - что existing validator пропускает bundle без переписывания contract rules;
  - что accepted bundle materialize-ится как versioned file;
  - что upload result materialize-ится в канонической форме;
  - что current marker указывает на активную version/result пару;
  - что duplicate `bundle_version` отвергается и не двигает current marker.

# 7. Что уже доказано по модулю

- upload line больше не обрывается на bundle+validator: появился локальный file-backed receiver.
- В repo есть прямой технический bridge под будущую кнопку отправки реестров из `VB-Core Витрина V1`.
- Accepted/current/result semantics доказаны локально без API, БД и Apps Script UI.

# 8. Что пока не является частью финальной production-сборки

- live upload endpoint;
- Apps Script upload button;
- live DB/Postgres storage;
- production activation runtime;
- operator trigger/orchestration;
- deploy и jobs.
