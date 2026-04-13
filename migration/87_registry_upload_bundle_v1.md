# Registry Upload Bundle V1

## 1. Что Это Такое

`registry_upload_bundle_v1_block` — первый bounded implementation artifact для server-side upload path V2-реестров.

Это уже не только контракт из `migration/86_registry_upload_contract.md`, а живой bundle и локальный validator, которые можно читать и проверять руками.

## 2. Что Именно Покрывает Bundle V1

Bundle V1 покрывает:
- `CONFIG_V2`
- `METRICS_V2`
- `FORMULAS_V2`
- канонический top-level envelope:
  - `bundle_version`
  - `uploaded_at`
  - `config_v2`
  - `metrics_v2`
  - `formulas_v2`

В pilot scope bundle входят:
- `5` SKU;
- `12` метрик;
- `2` формулы;
- все три `calc_type`: `metric`, `formula`, `ratio`.

## 3. Чем Это Отличается От Чистой Схемы

Чистая схема фиксирует только shape и правила.

`registry_upload_bundle_v1` добавляет:
- реальные artifact-backed input fixtures;
- реальный target bundle fixture;
- локальный validator;
- smoke, который подтверждает, что bundle можно собрать и провалидировать без API, БД и server ingest.

## 4. Почему Это Уже Implementation Artifact

Это implementation artifact, потому что:
- bundle материализован в `artifacts/registry_upload_bundle_v1/target/registry_upload_bundle__fixture.json`;
- кодовый слой в `packages/contracts/` и `packages/application/` реально читает input artifacts и строит target bundle;
- validator реально проверяет contract rules из `migration/86` и `calc_ref` resolution через server runtime seed.

## 5. Что Остаётся Следующим Шагом

Следующий bounded step:
- добавить локальный file-backed upload service, который принимает bundle как входной payload, возвращает upload result в форме `migration/86` и готовит почву для будущего server ingest, не переходя пока к API endpoint и live DB storage.
