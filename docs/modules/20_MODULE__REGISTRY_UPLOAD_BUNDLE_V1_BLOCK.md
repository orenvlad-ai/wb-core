---
title: "Модуль: registry_upload_bundle_v1_block"
doc_id: "WB-CORE-MODULE-20-REGISTRY-UPLOAD-BUNDLE-V1-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `registry_upload_bundle_v1_block`."
scope: "Первый artifact-backed upload bundle для `CONFIG_V2`, `METRICS_V2`, `FORMULAS_V2`, локальный validator, smoke без API/БД и жёсткие границы pilot implementation шага."
source_basis:
  - "migration/86_registry_upload_contract.md"
  - "migration/87_registry_upload_bundle_v1.md"
  - "artifacts/registry_upload_bundle_v1/target/registry_upload_bundle__fixture.json"
  - "artifacts/registry_upload_bundle_v1/evidence/initial__registry-upload-bundle-v1__evidence.md"
  - "registry/pilot_bundle/metric_runtime_registry.json"
related_modules:
  - "packages/contracts/registry_upload_bundle_v1.py"
  - "packages/application/registry_upload_bundle_v1.py"
related_tables:
  - "CONFIG_V2"
  - "METRICS_V2"
  - "FORMULAS_V2"
related_endpoints: []
related_runners:
  - "apps/registry_upload_bundle_v1_smoke.py"
related_docs:
  - "migration/75_registry_v2_minimal_schema.md"
  - "migration/76_metric_runtime_registry_minimal_schema.md"
  - "migration/77_registry_implementation_path.md"
  - "migration/86_registry_upload_contract.md"
  - "migration/87_registry_upload_bundle_v1.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ для первого artifact-backed upload bundle и локального validator слоя V2-реестров."
---

# 1. Идентификатор и статус

- `module_id`: `registry_upload_bundle_v1_block`
- `family`: `registry`
- `status_transfer`: pilot upload bundle перенесён в `wb-core`
- `status_verification`: artifact-backed smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: ожидает merge в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `migration/86_registry_upload_contract.md`
  - `migration/87_registry_upload_bundle_v1.md`
  - `artifacts/registry_upload_bundle_v1/input/*.json`
  - `registry/pilot_bundle/metric_runtime_registry.json`
- Семантика блока: собрать один канонический upload bundle из трёх нормализованных V2-реестров и локально проверить его contract-consistency до любого API или ingest.

# 3. Target contract и смысл результата

- Канонический output bundle содержит только:
  - `bundle_version`
  - `uploaded_at`
  - `config_v2`
  - `metrics_v2`
  - `formulas_v2`
- Runtime semantics не включаются в тело bundle.
- `calc_ref` для `metric` и `ratio` резолвится через внешний runtime seed, а не через отдельный upload payload field.

# 4. Артефакты и wiring по модулю

- input artifacts:
  - `artifacts/registry_upload_bundle_v1/input/config_v2__fixture.json`
  - `artifacts/registry_upload_bundle_v1/input/metrics_v2__fixture.json`
  - `artifacts/registry_upload_bundle_v1/input/formulas_v2__fixture.json`
- target artifact:
  - `artifacts/registry_upload_bundle_v1/target/registry_upload_bundle__fixture.json`
- parity:
  - `artifacts/registry_upload_bundle_v1/parity/input-vs-bundle__comparison.md`
- evidence:
  - `artifacts/registry_upload_bundle_v1/evidence/initial__registry-upload-bundle-v1__evidence.md`

# 5. Кодовые части

- contracts:
  - `packages/contracts/registry_upload_bundle_v1.py`
- application:
  - `packages/application/registry_upload_bundle_v1.py`
- smoke:
  - `apps/registry_upload_bundle_v1_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный artifact-backed smoke через `apps/registry_upload_bundle_v1_smoke.py`.
- Smoke проверяет:
  - что bundle собирается из трёх input fixtures в канонический top-level envelope;
  - что собранный результат совпадает с target fixture;
  - что проходят проверки уникальности `bundle_version`, `nm_id`, `display_order`, `metric_key`, `formula_id`;
  - что `scope` и `calc_type` лежат в допустимых множествах;
  - что `calc_ref` резолвится через `formulas_v2` и `registry/pilot_bundle/metric_runtime_registry.json`;
  - что pilot scope реально покрывает `metric`, `formula`, `ratio`.

# 7. Что уже доказано по модулю

- upload path больше не только договорённость в migration-doc: есть materialized bundle fixture.
- Первый bounded validator уже выражает contract rules из `migration/86` без раннего server ingest.
- Bundle остаётся table-facing и не смешивает display-слой с runtime registry в одном payload.

# 8. Что пока не является частью финальной production-сборки

- live upload endpoint;
- Apps Script upload button;
- server-side version storage;
- activation runtime;
- DB-backed uniqueness checks;
- orchestration и deploy.
