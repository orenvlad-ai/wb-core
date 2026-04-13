# Evidence: registry_upload_bundle_v1

## Scope

- bounded implementation step для `registry_upload_bundle_v1_block`
- без API
- без Apps Script upload button
- без server-side ingest
- без БД

## Source Basis

- `migration/86_registry_upload_contract.md`
- `registry/pilot_bundle/config_v2.json`
- `registry/pilot_bundle/metrics_v2.json`
- `registry/pilot_bundle/formulas_v2.json`
- `registry/pilot_bundle/metric_runtime_registry.json`
- `artifacts/registry_upload_bundle_v1/input/config_v2__fixture.json`
- `artifacts/registry_upload_bundle_v1/input/metrics_v2__fixture.json`
- `artifacts/registry_upload_bundle_v1/input/formulas_v2__fixture.json`

## What Is Proven

- upload path больше не существует только как контракт: есть живой artifact-backed bundle в канонической форме `bundle_version + uploaded_at + config_v2 + metrics_v2 + formulas_v2`
- bundle собирается из трёх отдельных input fixtures и проверяется локальным validator-слоем
- validator проверяет правила `migration/86` без раннего API/БД слоя
- bundle остаётся table-facing и не тащит внутрь server runtime semantics
- runtime registry используется только как внешний validation seed для `calc_ref` resolution
- bounded scope покрывает:
  - `5` SKU
  - `12` метрик
  - `2` формулы
  - `metric`, `formula`, `ratio`

## Checks

- `python3 apps/registry_upload_bundle_v1_smoke.py`
- `python3 -m py_compile packages/contracts/registry_upload_bundle_v1.py packages/application/registry_upload_bundle_v1.py apps/registry_upload_bundle_v1_smoke.py`
