# Evidence: registry_upload_db_backed_runtime

## Scope

- bounded implementation step для `registry_upload_db_backed_runtime_block`
- без Apps Script upload button
- без operator UI
- без внешнего API endpoint
- без deploy и orchestration

## Source Basis

- `migration/86_registry_upload_contract.md`
- `migration/87_registry_upload_bundle_v1.md`
- `migration/88_registry_upload_file_backed_service.md`
- `artifacts/registry_upload_db_backed_runtime/input/registry_upload_bundle__fixture.json`
- `artifacts/registry_upload_db_backed_runtime/target/*.json`
- `registry/pilot_bundle/metric_runtime_registry.json`

## What Is Proven

- upload line больше не ограничена только file-backed simulation: в repo есть локальный DB-backed runtime слой
- runtime ingest переиспользует существующий validator из `registry_upload_bundle_v1_block`
- accepted version persist-ится в SQLite-backed runtime storage
- current/active state читается из DB как server-side truth
- upload result persist-ится и читается обратно из runtime DB в канонической форме
- duplicate `bundle_version` отвергается без сдвига current state и version index

## Bounded Assumption

- Для этого шага SQLite-файл используется как минимальный локальный DB-backed analog server-side runtime
- Это не фиксирует окончательное production-решение по Postgres/storage model

## Checks

- `python3 apps/registry_upload_db_backed_runtime_smoke.py`
- `python3 apps/registry_upload_file_backed_service_smoke.py`
- `python3 apps/registry_upload_bundle_v1_smoke.py`
- `python3 -m py_compile packages/contracts/registry_upload_bundle_v1.py packages/application/registry_upload_bundle_v1.py packages/contracts/registry_upload_file_backed_service.py packages/application/registry_upload_file_backed_service.py packages/contracts/registry_upload_db_backed_runtime.py packages/application/registry_upload_db_backed_runtime.py apps/registry_upload_bundle_v1_smoke.py apps/registry_upload_file_backed_service_smoke.py apps/registry_upload_db_backed_runtime_smoke.py`
