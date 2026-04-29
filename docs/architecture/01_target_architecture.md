# Target Architecture

## Current Main-Confirmed Checkpoint

`origin/main` уже реализует bounded read-side и current website/operator contour. Legacy Google Sheets/GAS contour сохранён только как archive/migration boundary и помечен `ARCHIVED / DO NOT USE`.

Подтверждённый working contour на `main`:
- `sku_display_bundle_block`
- `table_projection_bundle_block`
- `registry_pilot_bundle`
- `wide_data_matrix_v1_fixture_block`
- `wide_data_matrix_delivery_bundle_v1_block`
- `sheet_vitrina_v1_scaffold_block`

Дополнительно в `main` уже присутствуют:
- archived `sheet_vitrina_v1_write_bridge_block`;
- archived `sheet_vitrina_v1_presentation_block`;
- `migration/86_registry_upload_contract.md` как канонический контракт upload path для V2-реестров;
- `registry_upload_bundle_v1_block` как первый artifact-backed upload bundle и local validator для V2-реестров;
- `registry_upload_file_backed_service_block` как первый file-backed accept/store/activate слой для V2-реестров;
- `registry_upload_db_backed_runtime_block` как первый DB-backed runtime ingest и current-truth слой для V2-реестров.
- `registry_upload_http_entrypoint_block` как первый live HTTP/API entrypoint для V2-реестров.
- archived `sheet_vitrina_v1_registry_upload_trigger_block`; Apps Script menu/upload actions are no longer active.
- sibling `COST_PRICE` server contour внутри того же app/runtime boundary: `POST /v1/cost-price/upload` без подмешивания в compact registry bundle.
- archived `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` как migration evidence для бывших operator sheets `CONFIG / METRICS / FORMULAS`.
- `sheet_vitrina_v1_mvp_end_to_end_block` как historical bounded checkpoint; current active path is server-side refresh + public/operator web-vitrina read surfaces, not Google Sheets load.
- `promo_xlsx_collector_block` как первый repo-owned bounded browser-capture precursor для promo XLSX + metadata sidecar.
- `promo_live_source_wiring_block` как bounded wiring этого precursor обратно в current `sheet_vitrina_v1` refresh/runtime/read-side contour без отдельного shadow contour.

Главный незакрытый gap текущего `main`:
- registry upload и bounded reverse-load уже присутствуют в текущей линии;
- этот документ теперь признаёт repo-owned hosted runtime deploy/probe contract для active EU target `wb-core-eu-root` / `89.191.226.88` с `api.selleros.pro` как допустимым current live DNS name; archived `selleros-root` / `178.72.152.177` target не является active runtime/update target, хранится только как rollback/read-only evidence, а mutating deploy/apply-nginx/restart/update/GC paths должны fail-fast без explicit emergency rollback override.

После этого незакрытым хвостом остаются:
- full legacy parity по всем metric sections;
- actual granted deploy access и production-bound operator runtime;
- final auth-hardening и production storage binding.

## Server-First Architecture

Target-state — server-first:
- business-rules, ingestion, derivation, snapshots и API живут на server-side;
- operator UI читает controlled contracts, а не владеет core-computation;
- runtime-state наблюдаем вне таблицы.

Это противоположно текущему legacy-центру тяжести, где Apps Script до сих пор держит raw fetch, apply, export и operator orchestration внутри одного container-bound runtime.

## Thin-Table Target

Будущая таблица — thin operator shell:
- только manual operator inputs;
- только controlled read-models;
- без тяжёлых вычислений;
- без скрытого server-only поведения внутри Apps Script;
- без table-exclusive truth для production-логики.

Текущее состояние `main`:
- Google Sheets `DATA_VITRINA`/`STATUS`, live write, visual presentation, Apps Script upload trigger и compact v3 bootstrap переведены в archived/migration-only state;
- active/current contour = hosted website/operator `sheet_vitrina_v1`, server-side refresh/runtime snapshots, public web-витрина JSON/page composition и `/sheet-vitrina-v1/vitrina`;
- separate `COST_PRICE` server contour остаётся authoritative dataset и подключается server-side в current refresh/read contour через effective-date overlay;
- `/v1/sheet-vitrina-v1/load`, GAS menu flows, `clasp` runners and Google Sheets readback are not active completion or verification targets.

## Current Main-Confirmed Layers

Материализованные слои и каталоги в `main`:
- `apps/`: smoke runners; local live-write/presentation runners are archived fail-fast guards;
- `packages/contracts`: typed DTO, schemas и contract validation;
- `packages/application`: bundle assembly, orchestration и use-cases;
- `packages/adapters`: WB API, Sheets и другие I/O adapters в тех блоках, которые уже смёржены;
- `artifacts/`: fixture, parity и evidence для bounded implementation blocks;
- `registry/pilot_bundle/`: pilot registry artifacts для новой витрины;
- `gas/sheet_vitrina_v1/`: archived bound Apps Script wiring with explicit archive guard;
- `docs/` и `migration/`: канонические policy/module/migration документы.

## Target/Future Layers

Целевые, но пока не материализованные в `main` слои:
- `packages/domain`
- `infra/`
- `tests/`
- `api/`
- `jobs/`
- `db/`

Эти каталоги и слои нельзя описывать как текущую реализацию `main`. На текущем checkpoint они остаются target/future направлением.

## Границы

### Граница Таблицы

Таблица больше не является current operator contour. Legacy spreadsheet may remain only as archive/migration evidence.

Таблица не должна:
- быть единственным местом production-truth;
- владеть тяжёлыми transform-ами или long-running fetch-логикой;
- скрывать server behavior, которое нельзя review-ить в Git.

Текущий `main` реализует это как server/web-first rule:
- website/operator and public web-витрина read server-owned ready snapshots;
- registry upload bundle, file-backed service, DB-backed runtime and live HTTP entrypoint remain materialized;
- sheet-side upload trigger, compact v3 bootstrap and reverse-load to `DATA_VITRINA` are archived/do-not-use paths;
- live daily refresh timer builds server-side ready snapshots only.

### Граница Сервера

Сервер владеет:
- ingestion и sync jobs;
- domain-calculations;
- materialization of snapshots;
- contract validation;
- auditability и runtime observability.

Текущее ограничение `main`:
- contracts, artifact-backed bundle, file-backed service, DB-backed runtime and live HTTP entrypoint already exist;
- persisted ready snapshots in repo-owned runtime SQLite are current truth for website/operator surfaces;
- reverse load into Google Sheets is archived and must not be used as runtime update/write/load path;
- hosted runtime deploy/probe contract уже repo-owned, но actual deploy rights, final auth-hardening и production storage binding остаются отдельным незакрытым хвостом.

### Граница Web-Source

Web-source capture — это adapter, а не domain-логика.

Факты из reference:
- `wb-web-bot` захватывает payload `search-report/report` через Playwright;
- `wb-ai-research/wb-ai/web_sources/client.py` содержит прямой HTTP client для WB search report;
- `wb-table-audit/apps-script/src/44_raw_search_analytics_snapshot.js` historically consumed server endpoint `GET https://api.selleros.pro/v1/search-analytics/snapshot`; current active hosted runtime target is the EU VPS target contract, not the archived selleros SSH target.
- current `main` уже materialize-ит bounded repo-owned promo collector block, который переиспользует seller session reuse path и canonical browser seams, но не копирует весь `wb-web-bot` внутрь `wb-core`.

Следствие:
- web-source код должен жить за явными контрактами;
- browser/session детали не должны протекать в domain-модули.

## Почему Modular Monolith

Modular monolith выбран потому, что:
- текущий scope уже и так операционно раздроблен на три репозитория и runtime-контура;
- ближайшая проблема — это control, boundaries и parity, а не независимое горизонтальное масштабирование;
- общие контракты вроде `CONFIG`, `METRICS`, `AI_EXPORT`, snapshot outputs и nmId/date semantics требуют одного согласованного core.

Microservices на этом этапе только добавят операционную поверхность до того, как foundation станет стабильным.

## Обязательные Принципы

Обязательные принципы:
- server-first execution для production-логики;
- явные контракты на каждой границе;
- один source of truth на каждую ответственность;
- никакого скрытого runtime-only изменения;
- никакого смешения operator concerns и production computation;
- parity перед cutover;
- маленькие strangler-steps вместо big-bang replacement;
- legacy остаётся maintenance-only, пока replacement-модуль не доказан.
