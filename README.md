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
- `wide matrix`, `delivery`, archived legacy sheet-side/export contour и текущую web-витрину.

Подтверждённый main-confirmed contour:

`sku_display -> table_projection -> registry_pilot -> wide_matrix -> delivery -> sheet_scaffold`

В `main` также уже есть:
- legacy/export write bridge, presentation pass и compact v3 bootstrap для bound Google Sheet, но этот Google Sheets/GAS contour теперь `ARCHIVED / DO NOT USE` и не является active runtime/update/write/verify target;
- отдельный bounded `COST_PRICE` server contour: sibling HTTP upload path, server-side authoritative storage seam вне compact registry bundle и read-side overlay в refresh contour для `cost_price_rub`, `total_proxy_profit_rub`, `proxy_margin_pct_total`;
- bounded promo line в двух шагах:
  - `promo_xlsx_collector_block` как thin browser-capture precursor для promo XLSX + metadata sidecar;
  - `promo_live_source_wiring_block` как current live wiring обратно в `sheet_vitrina_v1` refresh/runtime/read-side contour для `promo_by_price` и promo-backed numeric rows;
- вкладка `Отзывы` в active website/operator contour: read-only WB feedbacks load/filter table со строгими server-side `date_from/date_to/stars/is_answered` фильтрами, bounded 62-day feedback date picker independent from ready-snapshot dates, chunked `take/skip` загрузкой без тихого row cap, diagnostic meta, Excel export текущей таблицы, resizable columns, bounded AI-assisted разбор через server-side prompt+model config/OpenAI route и подраздел `Жалобы` над runtime-журналом жалоб; AI-разметка не является accepted truth/ЕБД persistence, real complaint submit доступен только через guarded CLI runner с exact match и hard caps, а неопределённые submit-попытки проверяются отдельными read-only confirmation/detail-network probe runners с direct-id или strict strong-composite proof;
- первый bounded end-to-end MVP history `prepare -> upload -> refresh -> load` сохранён как archive/migration reference; current active contour = website/operator `sheet_vitrina_v1` и public web-витрина без Google Sheets completion blocker;
- evidence и module docs по этим шагам.

Главные незакрытые gaps на текущем `main`:
- full legacy parity по всем историческим registry rows и metric sections;
- full legacy parity beyond current `102`-row uploaded metric dictionary и beyond current server-driven two-day read-side без поломки текущего contour;
- repo-owned hosted runtime deploy/probe contract теперь materialized в repo, including managed public-route allowlist publishing for current wb-core routes; active/current live hosted runtime target = `wb-core-eu-root` / `89.191.226.88` / `/opt/wb-core-runtime/state`, current public base = `https://api.selleros.pro`, and managed nginx must publish both `89.191.226.88` and `api.selleros.pro` with `443 ssl`; this HTTPS/domain publication is a hard current-live invariant, so future IP-only or HTTP-only target drift fails locally before deploy/apply-nginx mutation; legacy `selleros-root` / `178.72.152.177` target is rollback-only/read-only evidence and routine deploy/apply-nginx/restart/update/GC writes fail fast unless the explicit emergency rollback override is set; final auth-hardening вокруг already materialized upload/read линии ещё не закрыты;
- promo artifact retention встроен в hosted refresh: after normalized promo archive + ready snapshot persistence refresh runs bounded `promo_refresh_light_gc_v1`, protects current/unknown/replay-critical artifacts and surfaces GC summary in refresh diagnostics/job log;
- окончательное решение по судьбе `AI_EXPORT` как compatibility contract или прямой замене server-side contract.

## Что repo уже содержит

- `packages/contracts`, `packages/application`, `packages/adapters` с живыми bounded modules;
- `apps/` с smoke runners; legacy live sheet-side runners сохранены как archived fail-fast guards;
- `artifacts/` с fixture/parity/evidence по смёрженным блокам;
- `registry/pilot_bundle/` как pilot registry line для новой витрины;
- `artifacts/registry_upload_bundle_v1/` как первый artifact-backed upload path для V2-реестров;
- `artifacts/registry_upload_file_backed_service/` как локальный file-backed receiver для registry upload path;
- `artifacts/registry_upload_db_backed_runtime/` как локальный DB-backed runtime layer для registry upload path;
- `artifacts/registry_upload_http_entrypoint/` как первый live HTTP entrypoint для registry upload path;
- `artifacts/sheet_vitrina_v1_registry_upload_trigger/`, `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/` и `artifacts/sheet_vitrina_v1_mvp_end_to_end/` как archived/migration evidence для бывшего sheet-side contour;
- `packages/application/cost_price_upload.py` и `packages/contracts/cost_price_upload.py` как отдельный authoritative upload contract для `COST_PRICE`, который затем подключается server-side в `sheet_vitrina_v1` refresh/read contour;
- `gas/sheet_vitrina_v1/` и `.clasp.json` для legacy bound sheet-side/export wiring; GAS содержит archive guard, не является active production/update/write/verify target и не должен использоваться как Codex completion path;
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
13. Появление `wb_core_docs_master` как derived secondary compact project-pack поверх authoritative canonical repo docs.
14. Отдельный bounded contour для `COST_PRICE`: `COST_PRICE` sheet-side prepare/upload, sibling `POST /v1/cost-price/upload` и separate authoritative dataset в том же runtime/app boundary.
15. Server-side read-side integration `COST_PRICE` в `sheet_vitrina_v1`: authoritative resolution по `group + max(effective_from <= slot_date)`, truthful `STATUS.cost_price[*]` и operator-facing derived metrics `total_proxy_profit_rub` / `proxy_margin_pct_total`.
16. Первый repo-owned bounded `promo_xlsx_collector_block`: canonical hydration/modal/drawer seams, truthful metadata sidecar, workbook inspection и bounded live integration smoke поверх existing seller session reuse path.
17. Bounded live wiring `promo_live_source_wiring_block`: `promo_by_price` больше не blocked gap, а current server-owned source seam внутри existing `refresh -> runtime -> STATUS/DATA_VITRINA` contour с accepted snapshot preservation и truthful low-confidence cross-year handling.
18. Legacy Google Sheets/GAS contour переведён в `ARCHIVED / DO NOT USE`; current contour = hosted website/operator/web-vitrina runtime.
19. В active `/sheet-vitrina-v1/vitrina` добавлены read-only `Отзывы`, подраздел `Жалобы` с runtime-журналом/status sync, compact supply/report/research operator surfaces and bounded AI-assisted feedback review over OpenAI with server-side prompt/model config, live model discovery, strict feedback period/star filters, Excel export and resizable feedback columns; controlled complaint submit остаётся CLI-only.
20. После этого остаются full parity archive questions и hosted runtime hardening вокруг current web/operator contour.
21. Docs governance переведён в authoritative/derived режим: ordinary task-flow обновляет затронутые canonical docs при truth change, а `wb_core_docs_master` пересобирается отдельным derived-sync flow.

## Что не следует считать частью текущего `main`

- full legacy parity по всем историческим registry rows и metric sections;
- production storage binding и final auth-hardening для registry upload;
- granted deploy access + live publish wiring для already materialized hosted runtime contract;
- материализованные слои `packages/domain`, `infra/`, `tests/`, `api/`, `jobs/`, `db/`.

## Governance Pointers

Канонические governance/source-of-truth правила живут не в `README.md`, а в:
- [docs/architecture/03_source_of_truth_policy.md](docs/architecture/03_source_of_truth_policy.md)
- [docs/architecture/07_codex_execution_protocol.md](docs/architecture/07_codex_execution_protocol.md)
- [docs/architecture/02_repo_workspace_blueprint.md](docs/architecture/02_repo_workspace_blueprint.md)
- [docs/architecture/10_hosted_runtime_deploy_contract.md](docs/architecture/10_hosted_runtime_deploy_contract.md)
- [docs/architecture/11_server_curator_cockpit_mvp.md](docs/architecture/11_server_curator_cockpit_mvp.md)

`README.md` остаётся summary/navigation entrypoint и не должен использоваться как самостоятельный carrier operational governance.

## Где смотреть детали

- [docs/modules/00_INDEX__MODULES.md](docs/modules/00_INDEX__MODULES.md)
- [wb_core_docs_master/00_INDEX__WEBCORE_PROJECT_DOCS.md](wb_core_docs_master/00_INDEX__WEBCORE_PROJECT_DOCS.md)
- [docs/architecture/00_migration_charter.md](docs/architecture/00_migration_charter.md)
- [docs/architecture/01_target_architecture.md](docs/architecture/01_target_architecture.md)
- [docs/architecture/10_hosted_runtime_deploy_contract.md](docs/architecture/10_hosted_runtime_deploy_contract.md)
- [docs/architecture/11_server_curator_cockpit_mvp.md](docs/architecture/11_server_curator_cockpit_mvp.md)
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
