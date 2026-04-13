# wb-core

`wb-core` — target-core репозиторий для controlled sidecar migration проекта «Динамики и корреляции» на WB.

Legacy-репозитории остаются рабочими, но считаются maintenance-only:
- `wb-table-audit`
- `wb-ai-research`
- `wb-web-bot`

Новая разработка и canonical migration state фиксируются только в `wb-core`.

## Текущий checkpoint `main`

`origin/main` уже не является foundation-only состоянием. В `main` смёржены bounded implementation-блоки для:
- `web-source` и `official-api` snapshot family;
- `rule-based` модулей;
- `table-facing` и `projection` read-side;
- `registry` pilot line и первый upload bundle path;
- `wide matrix`, `delivery` и `sheet-side` витрины.

Подтверждённый main-confirmed contour:

`sku_display -> table_projection -> registry_pilot -> wide_matrix -> delivery -> sheet_scaffold`

В `main` также уже есть:
- live write bridge новой витрины в bound Google Sheet;
- presentation pass для `DATA_VITRINA` и `STATUS`;
- compact v3 bootstrap для `CONFIG / METRICS / FORMULAS`, который поднимает уже заполненные operator sheets и сохраняет service/status block;
- evidence и module docs по этим шагам.

Главный незакрытый gap на текущем `main`:
- upload line уже дошла до artifact-backed bundle, local validator, file-backed service, локального DB-backed runtime, тонкого live HTTP entrypoint и первого sheet-side operator trigger;
- compact v3 bootstrap уже в `main`, но controlled reverse-load server-side current truth обратно в `DATA_VITRINA` ещё не входит в текущий `main`.

На текущей PR-ветке дополнительно materialize-ится:
- `sheet_vitrina_v1_mvp_end_to_end_block` как первый bounded end-to-end MVP: expanded MVP-safe seed `33 / 7 / 7`, сохранённый upload flow и controlled load живых server-side данных обратно в `DATA_VITRINA` через lightweight plan endpoint.
- После этого блока незакрытым хвостом остаются full legacy parity, расширение beyond MVP-safe metrics/sections, stable hosted runtime URL, deploy/auth-hardening и production storage binding.

## Что repo уже содержит

- `packages/contracts`, `packages/application`, `packages/adapters` с живыми bounded modules;
- `apps/` с smoke runners и live sheet-side runners;
- `artifacts/` с fixture/parity/evidence по смёрженным блокам;
- `registry/pilot_bundle/` как pilot registry line для новой витрины;
- `artifacts/registry_upload_bundle_v1/` как первый artifact-backed upload path для V2-реестров;
- `artifacts/registry_upload_file_backed_service/` как локальный file-backed receiver для registry upload path;
- `artifacts/registry_upload_db_backed_runtime/` как локальный DB-backed runtime layer для registry upload path;
- `artifacts/registry_upload_http_entrypoint/` как первый live HTTP entrypoint для registry upload path;
- `artifacts/sheet_vitrina_v1_registry_upload_trigger/` как первый operator-facing sheet-side trigger для registry upload path;
- `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/` как compact v3 bootstrap для operator registry sheets;
- `gas/sheet_vitrina_v1/` и `.clasp.json` для bound sheet-side wiring;
- `docs/modules/` как канонический модульный reference;
- `migration/` как канонический слой migration contracts и implementation notes.

## Короткая хронология `main`

1. Foundation и control/docs слой.
2. Перенос `web-source` и `official-api` snapshot-блоков.
3. Rule-based блоки и doc-sync по канонической модульной документации.
4. Table/registry/wide/sheet line: `sku_display`, `table_projection`, `registry_pilot`, `wide_matrix`, `delivery`, `sheet_scaffold`, `live write`, `presentation`.
5. Upload contract для V2-реестров.
6. Первый artifact-backed upload bundle и local validator для V2-реестров.
7. Первый file-backed upload service, current-marker и structured upload result для V2-реестров.
8. Первый DB-backed runtime ingest и current server-side truth для V2-реестров.
9. Первый тонкий live HTTP entrypoint для registry upload path.
10. Первый operator-facing sheet-side trigger для отправки `CONFIG / METRICS / FORMULAS` в уже materialized entrypoint.
11. Compact v3 bootstrap для `CONFIG / METRICS / FORMULAS` с сохранением service/status block.
12. На текущей PR-ветке: первый bounded end-to-end MVP, где expanded MVP-safe registry seed, upload и load `DATA_VITRINA` уже работают в одном контуре.
13. После этого остаются full parity, stable hosted runtime URL, deploy/auth-hardening и production/runtime hardening вокруг уже materialized contour.

## Что не следует считать частью текущего `main`

- full legacy parity по всем историческим registry rows и metric sections;
- stable hosted runtime URL, production storage binding, deploy и auth-hardening для registry upload;
- deployed/auth-hardened API, jobs и operator runtime для registry upload;
- материализованные слои `packages/domain`, `infra/`, `tests/`, `api/`, `jobs/`, `db/`.

## Где смотреть детали

- [docs/modules/00_INDEX__MODULES.md](docs/modules/00_INDEX__MODULES.md)
- [docs/architecture/00_migration_charter.md](docs/architecture/00_migration_charter.md)
- [docs/architecture/01_target_architecture.md](docs/architecture/01_target_architecture.md)
- [migration/75_registry_v2_minimal_schema.md](migration/75_registry_v2_minimal_schema.md)
- [migration/76_metric_runtime_registry_minimal_schema.md](migration/76_metric_runtime_registry_minimal_schema.md)
- [migration/77_registry_implementation_path.md](migration/77_registry_implementation_path.md)
- [migration/78_pilot_registry_bundle.md](migration/78_pilot_registry_bundle.md)
- [migration/86_registry_upload_contract.md](migration/86_registry_upload_contract.md)
- [migration/87_registry_upload_bundle_v1.md](migration/87_registry_upload_bundle_v1.md)
- [migration/88_registry_upload_file_backed_service.md](migration/88_registry_upload_file_backed_service.md)
- [migration/89_registry_upload_db_backed_runtime.md](migration/89_registry_upload_db_backed_runtime.md)
- [migration/90_registry_upload_http_entrypoint.md](migration/90_registry_upload_http_entrypoint.md)
- [migration/91_sheet_vitrina_v1_registry_upload_trigger.md](migration/91_sheet_vitrina_v1_registry_upload_trigger.md)
- [migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md](migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md)
- [migration/93_sheet_vitrina_v1_mvp_end_to_end.md](migration/93_sheet_vitrina_v1_mvp_end_to_end.md)
