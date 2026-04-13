# Evidence: registry_upload_http_entrypoint

## Scope

- bounded implementation step для `registry_upload_http_entrypoint_block`
- без Apps Script upload button
- без Google Sheets UI
- без deploy
- без jobs/orchestration
- без production Postgres redesign

## Source Basis

- `migration/86_registry_upload_contract.md`
- `migration/89_registry_upload_db_backed_runtime.md`
- `artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json`
- `artifacts/registry_upload_http_entrypoint/target/*.json`
- `packages/application/registry_upload_db_backed_runtime.py`

## What Is Proven

- в repo появился первый живой inbound HTTP boundary для registry upload
- entrypoint принимает canonical bundle payload по HTTP и делегирует ingest в существующий DB-backed runtime
- `activated_at` ставится на server-side внутри entrypoint/application слоя
- canonical `RegistryUploadResult` возвращается наружу как JSON response
- current server-side truth materialize-ится через существующий runtime DB, а не через отдельную HTTP-логику
- duplicate `bundle_version` возвращает rejected result и не двигает current state

## Bounded Assumption

- Для первого live entrypoint используется стандартный `http.server` как минимальный прозрачный inbound слой
- Это не фиксирует финальный production framework и не подменяет отдельный будущий шаг по deploy/auth/operator wiring

## Checks

- `python3 apps/registry_upload_http_entrypoint_smoke.py`
- `python3 apps/registry_upload_db_backed_runtime_smoke.py`
- `python3 apps/registry_upload_file_backed_service_smoke.py`
- `python3 apps/registry_upload_bundle_v1_smoke.py`
- `python3 -m py_compile packages/contracts/registry_upload_bundle_v1.py packages/application/registry_upload_bundle_v1.py packages/contracts/registry_upload_file_backed_service.py packages/application/registry_upload_file_backed_service.py packages/contracts/registry_upload_db_backed_runtime.py packages/application/registry_upload_db_backed_runtime.py packages/contracts/registry_upload_http_entrypoint.py packages/application/registry_upload_http_entrypoint.py packages/adapters/registry_upload_http_entrypoint.py apps/registry_upload_bundle_v1_smoke.py apps/registry_upload_file_backed_service_smoke.py apps/registry_upload_db_backed_runtime_smoke.py apps/registry_upload_http_entrypoint_live.py apps/registry_upload_http_entrypoint_smoke.py`
