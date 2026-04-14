---
title: "Модуль: registry_upload_bundle_v1_block"
doc_id: "WB-CORE-MODULE-20-REGISTRY-UPLOAD-BUNDLE-V1-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `registry_upload_bundle_v1_block`."
scope: "Artifact-backed upload bundle для полного uploaded compact registry package `CONFIG_V2`, `METRICS_V2`, `FORMULAS_V2`, локальный validator и smoke без API/БД."
source_basis:
  - "migration/86_registry_upload_contract.md"
  - "migration/87_registry_upload_bundle_v1.md"
  - "artifacts/registry_upload_bundle_v1/target/registry_upload_bundle__fixture.json"
  - "artifacts/registry_upload_bundle_v1/evidence/initial__registry-upload-bundle-v1__evidence.md"
  - "artifacts/registry_upload_bundle_v1/input/metric_runtime_registry__fixture.json"
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
update_note: "Обновлён под uploaded compact package: bundle и validator теперь работают с полным current authoritative registry set `33 / 102 / 7`, а не с pilot-subset."
---

# 1. Идентификатор и статус

- `module_id`: `registry_upload_bundle_v1_block`
- `family`: `registry`
- `status_transfer`: authoritative upload bundle перенесён в `wb-core`
- `status_verification`: artifact-backed smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `migration/86_registry_upload_contract.md`
  - `migration/87_registry_upload_bundle_v1.md`
  - `artifacts/registry_upload_bundle_v1/input/*.json`
  - `registry/pilot_bundle/metric_runtime_registry.json`
- Семантика блока: собрать один канонический upload bundle из uploaded compact registry package и локально проверить его contract-consistency до любого API или ingest.

# 3. Target contract и смысл результата

- Канонический output bundle содержит только:
  - `bundle_version`
  - `uploaded_at`
  - `config_v2`
  - `metrics_v2`
  - `formulas_v2`
- Runtime semantics не включаются в тело bundle.
- `calc_ref` для `metric` и `ratio` резолвится через внешний runtime seed, а не через отдельный upload payload field.

## 3.1 Current authoritative package

- На текущем `main` bundle materialize-ит uploaded compact package без тихого усечения:
  - `config_v2 = 33`
  - `metrics_v2 = 102`
  - `formulas_v2 = 7`
- Validator использует repo-owned runtime registry fixture:
  - `artifacts/registry_upload_bundle_v1/input/metric_runtime_registry__fixture.json`
- Bundle допускает все три `calc_type`:
  - `metric`
  - `formula`
  - `ratio`
- Ratio-метрики проверяются как по runtime row самого metric key, так и по явным numerator/denominator связям из uploaded `calc_ref`.

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
  - что `calc_ref` резолвится через `formulas_v2` и repo-owned runtime registry fixture;
  - что current authoritative package реально покрывает `metric`, `formula`, `ratio` без pilot-usability cap.

# 7. Что уже доказано по модулю

- upload path больше не только договорённость в migration-doc: есть materialized bundle fixture.
- Первый bounded validator уже выражает contract rules из `migration/86` без раннего server ingest.
- Bundle остаётся table-facing и не смешивает display-слой с runtime registry в одном payload.
- Full uploaded compact package уже проходит локальную сборку и validation без тихого возврата к `5 / 12 / 2` pilot-subset.

# 8. Что пока не является частью финальной production-сборки

- live upload endpoint;
- Apps Script upload button;
- server-side version storage;
- activation runtime;
- DB-backed uniqueness checks;
- orchestration и deploy.
