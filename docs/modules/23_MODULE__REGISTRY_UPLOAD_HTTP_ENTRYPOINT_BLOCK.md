---
title: "Модуль: registry_upload_http_entrypoint_block"
doc_id: "WB-CORE-MODULE-23-REGISTRY-UPLOAD-HTTP-ENTRYPOINT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `registry_upload_http_entrypoint_block`."
scope: "Первый live inbound HTTP entrypoint для V2-реестров: canonical bundle request, thin request -> runtime -> response wiring, server-side `activated_at`, integration smoke без Apps Script UI и deploy."
source_basis:
  - "migration/86_registry_upload_contract.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "migration/90_registry_upload_http_entrypoint.md"
  - "artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json"
  - "artifacts/registry_upload_http_entrypoint/evidence/initial__registry-upload-http-entrypoint__evidence.md"
related_modules:
  - "packages/contracts/registry_upload_bundle_v1.py"
  - "packages/contracts/registry_upload_file_backed_service.py"
  - "packages/contracts/registry_upload_db_backed_runtime.py"
  - "packages/contracts/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
related_tables:
  - "CONFIG_V2"
  - "METRICS_V2"
  - "FORMULAS_V2"
related_endpoints:
  - "POST /v1/registry-upload/bundle"
related_runners:
  - "apps/registry_upload_http_entrypoint_live.py"
  - "apps/registry_upload_http_entrypoint_smoke.py"
  - "apps/registry_upload_db_backed_runtime_smoke.py"
related_docs:
  - "migration/86_registry_upload_contract.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "migration/90_registry_upload_http_entrypoint.md"
  - "docs/modules/22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ для первого live HTTP entrypoint слоя registry upload."
---

# 1. Идентификатор и статус

- `module_id`: `registry_upload_http_entrypoint_block`
- `family`: `registry`
- `status_transfer`: live HTTP entrypoint перенесён в `wb-core`
- `status_verification`: integration smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `registry_upload_bundle_v1_block`
  - `registry_upload_file_backed_service_block`
  - `registry_upload_db_backed_runtime_block`
  - `migration/86_registry_upload_contract.md`
  - `migration/90_registry_upload_http_entrypoint.md`
- Семантика блока: принять bundle по HTTP, делегировать ingest в существующий DB-backed runtime и вернуть наружу канонический result без Apps Script UI и deploy.

# 3. Target contract и смысл результата

- Канонический input:
  - `POST /v1/registry-upload/bundle`
  - request body = `bundle_version + uploaded_at + config_v2 + metrics_v2 + formulas_v2`
- Канонический output:
  - JSON `RegistryUploadResult`
  - `status`
  - `bundle_version`
  - `accepted_counts`
  - `validation_errors`
  - `activated_at`
- HTTP семантика bounded шага:
  - `200` для `accepted`
  - `409` для duplicate `bundle_version`
  - `422` для contract-level rejection после parse

## 3.1 Допущение bounded шага

- Для первого live inbound boundary используется стандартный `http.server`.
- Это не является решением, что production target обязан жить на этом же framework-path.
- Deploy, auth и operator-facing trigger остаются отдельными шагами вне scope этого модуля.

# 4. Артефакты и wiring по модулю

- input artifact:
  - `artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json`
- target artifacts:
  - `artifacts/registry_upload_http_entrypoint/target/http_result__accepted__fixture.json`
  - `artifacts/registry_upload_http_entrypoint/target/http_result__duplicate_bundle_version__fixture.json`
  - `artifacts/registry_upload_http_entrypoint/target/current_state__fixture.json`
- parity:
  - `artifacts/registry_upload_http_entrypoint/parity/request-vs-runtime__comparison.md`
- evidence:
  - `artifacts/registry_upload_http_entrypoint/evidence/initial__registry-upload-http-entrypoint__evidence.md`

# 5. Кодовые части

- contracts:
  - `packages/contracts/registry_upload_http_entrypoint.py`
- application:
  - `packages/application/registry_upload_http_entrypoint.py`
- reused runtime:
  - `packages/application/registry_upload_db_backed_runtime.py`
- adapter:
  - `packages/adapters/registry_upload_http_entrypoint.py`
- live runner:
  - `apps/registry_upload_http_entrypoint_live.py`
- smoke:
  - `apps/registry_upload_http_entrypoint_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный integration smoke через `apps/registry_upload_http_entrypoint_smoke.py`.
- Smoke проверяет:
  - что HTTP entrypoint реально поднимается и принимает `POST`;
  - что request body попадает в существующий DB-backed runtime, а не в дублирующую ingest-логику;
  - что accepted response возвращается в канонической форме;
  - что current server-side truth обновляется через runtime DB;
  - что duplicate `bundle_version` возвращает rejected result и HTTP `409`.

# 7. Что уже доказано по модулю

- upload line больше не заканчивается на локальном runtime: в repo появился первый внешний вызываемый boundary.
- В будущем кнопке из `VB-Core Витрина V1` уже есть куда писать на уровне thin HTTP entrypoint.
- Новая HTTP прослойка остаётся тонкой и не тянет за собой deploy, auth и Apps Script UI.

# 8. Что пока не является частью финальной production-сборки

- Apps Script upload button;
- Google Sheets UI;
- operator workflow в таблице;
- deploy и внешняя доступность entrypoint;
- auth/hardening;
- production Postgres redesign и внешняя инфраструктура.
