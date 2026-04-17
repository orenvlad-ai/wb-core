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
- отдельный bounded `COST_PRICE` contour: лист `COST_PRICE`, separate menu actions, sibling HTTP upload path, server-side authoritative storage seam вне compact registry bundle и read-side overlay в refresh/load contour для `cost_price_rub`, `total_proxy_profit_rub`, `proxy_margin_pct_total`;
- первый bounded end-to-end MVP `prepare -> upload -> refresh -> load`, где operator seed `33 / 102 / 7`, full current `metrics_v2` dictionary для upload path и controlled reverse-load в `DATA_VITRINA` уже материализованы в коде и артефактах;
- evidence и module docs по этим шагам.

Главные незакрытые gaps на текущем `main`:
- full legacy parity по всем историческим registry rows и metric sections;
- full legacy parity beyond current `102`-row uploaded metric dictionary и beyond current server-driven two-day read-side без поломки текущего contour;
- repo-owned hosted runtime deploy/probe contract теперь materialized в repo, но actual deploy access, publish wiring hardening и production storage binding вокруг уже materialized upload/load линии ещё не закрыты;
- окончательное решение по судьбе `AI_EXPORT` как compatibility contract или прямой замене server-side contract.

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
- `artifacts/sheet_vitrina_v1_mvp_end_to_end/` как первый bounded end-to-end MVP для `VB-Core Витрина V1`;
- `packages/application/cost_price_upload.py` и `packages/contracts/cost_price_upload.py` как отдельный authoritative upload contract для `COST_PRICE`, который затем подключается server-side в `sheet_vitrina_v1` refresh/read contour;
- `gas/sheet_vitrina_v1/` и `.clasp.json` для bound sheet-side wiring;
- `wb_core_docs_master/` как compact curated-pack для project-oriented retrieval вне primary repo docs;
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
12. Первый bounded end-to-end MVP, где expanded MVP-safe registry seed, upload и load `DATA_VITRINA` уже работают в одном контуре.
13. Появление `wb_core_docs_master` как secondary compact project-pack поверх primary canonical repo docs.
14. Отдельный bounded contour для `COST_PRICE`: `COST_PRICE` sheet-side prepare/upload, sibling `POST /v1/cost-price/upload` и separate authoritative dataset в том же runtime/app boundary.
15. Server-side read-side integration `COST_PRICE` в `sheet_vitrina_v1`: authoritative resolution по `group + max(effective_from <= slot_date)`, truthful `STATUS.cost_price[*]` и operator-facing derived metrics `total_proxy_profit_rub` / `proxy_margin_pct_total`.
16. После этого остаются full parity, actual hosted deploy access/publish wiring hardening и production/runtime hardening вокруг уже materialized contour.

## Что не следует считать частью текущего `main`

- full legacy parity по всем историческим registry rows и metric sections;
- production storage binding и final auth-hardening для registry upload;
- granted deploy access + live publish wiring для already materialized hosted runtime contract;
- материализованные слои `packages/domain`, `infra/`, `tests/`, `api/`, `jobs/`, `db/`.

## Двухслойная Схема Docs

В `wb-core` действует двухслойная схема документации:
- primary canonical docs живут в `README.md`, `docs/architecture/*`, `docs/modules/*` и `migration/*`;
- secondary project-oriented pack живёт в `wb_core_docs_master/` и предназначен для retrieval/upload в отдельный ChatGPT Project.

`wb_core_docs_master` не является копией всего `docs/`. Это компактный curated-pack, который:
- пересобирается из primary source of truth;
- хранит только project-facing summary, registers, glossary и runbook;
- не должен становиться местом, где появляются новые нормы раньше primary repo docs.

## Где смотреть детали

- [docs/modules/00_INDEX__MODULES.md](docs/modules/00_INDEX__MODULES.md)
- [wb_core_docs_master/00_INDEX__WEBCORE_PROJECT_DOCS.md](wb_core_docs_master/00_INDEX__WEBCORE_PROJECT_DOCS.md)
- [docs/architecture/00_migration_charter.md](docs/architecture/00_migration_charter.md)
- [docs/architecture/01_target_architecture.md](docs/architecture/01_target_architecture.md)
- [docs/architecture/10_hosted_runtime_deploy_contract.md](docs/architecture/10_hosted_runtime_deploy_contract.md)
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
