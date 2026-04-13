# Evidence: registry_upload_file_backed_service

## Scope

- bounded implementation step для `registry_upload_file_backed_service_block`
- без API endpoint
- без Apps Script upload button
- без live DB storage
- без deploy и orchestration

## Source Basis

- `migration/86_registry_upload_contract.md`
- `migration/87_registry_upload_bundle_v1.md`
- `artifacts/registry_upload_bundle_v1/target/registry_upload_bundle__fixture.json`
- `artifacts/registry_upload_file_backed_service/input/registry_upload_bundle__fixture.json`
- `artifacts/registry_upload_file_backed_service/target/*.json`
- `registry/pilot_bundle/metric_runtime_registry.json`

## What Is Proven

- upload flow больше не обрывается на builder/validator: в repo есть локальный file-backed приёмник bundle
- service переиспользует существующий validator из `registry_upload_bundle_v1_block`, а не дублирует contract rules
- accepted bundle materialize-ится как versioned artifact
- current/active marker materialize-ится отдельно и указывает на активную версию
- upload result materialize-ится в канонической форме `status + bundle_version + accepted_counts + validation_errors + activated_at`
- duplicate `bundle_version` отвергается без перезаписи accepted/current state

## Checks

- `python3 apps/registry_upload_file_backed_service_smoke.py`
- `python3 apps/registry_upload_bundle_v1_smoke.py`
- `python3 -m py_compile packages/contracts/registry_upload_bundle_v1.py packages/application/registry_upload_bundle_v1.py packages/contracts/registry_upload_file_backed_service.py packages/application/registry_upload_file_backed_service.py apps/registry_upload_bundle_v1_smoke.py apps/registry_upload_file_backed_service_smoke.py`
