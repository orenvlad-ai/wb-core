# Target Architecture

## Current Main-Confirmed Checkpoint

`origin/main` уже реализует bounded read-side и sheet-side контур, а не только foundation-документы.

Подтверждённый working contour на `main`:
- `sku_display_bundle_block`
- `table_projection_bundle_block`
- `registry_pilot_bundle`
- `wide_data_matrix_v1_fixture_block`
- `wide_data_matrix_delivery_bundle_v1_block`
- `sheet_vitrina_v1_scaffold_block`

Дополнительно в `main` уже присутствуют:
- `sheet_vitrina_v1_write_bridge_block`;
- `sheet_vitrina_v1_presentation_block`;
- `migration/86_registry_upload_contract.md` как канонический контракт upload path для V2-реестров;
- `registry_upload_bundle_v1_block` как первый artifact-backed upload bundle и local validator для V2-реестров;
- `registry_upload_file_backed_service_block` как первый file-backed accept/store/activate слой для V2-реестров;
- `registry_upload_db_backed_runtime_block` как первый DB-backed runtime ingest и current-truth слой для V2-реестров.
- `registry_upload_http_entrypoint_block` как первый live HTTP/API entrypoint для V2-реестров.
- `sheet_vitrina_v1_registry_upload_trigger_block` как первый operator-facing Apps Script trigger для отправки `CONFIG / METRICS / FORMULAS` в уже существующий HTTP entrypoint.
- `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` как compact v3 bootstrap для operator sheets `CONFIG / METRICS / FORMULAS`.

Главный незакрытый gap текущего `main`:
- upload-side artifact-backed, file-backed, DB-backed runtime, тонкий live HTTP entrypoint и первый operator-facing Apps Script trigger уже в текущей линии;
- compact v3 bootstrap уже тоже в текущей линии;
- этот документ пока не должен трактоваться как подтверждение наличия server-side readback в таблицу, deploy/auth-hardening или production storage binding для registry upload.

На текущей PR-ветке дополнительно materialize-ится:
- `sheet_vitrina_v1_mvp_end_to_end_block` как первый bounded end-to-end MVP: expanded MVP-safe seed, сохранённый upload flow и live public-source-backed readback в `DATA_VITRINA`.

После этого незакрытым хвостом остаются:
- full legacy parity по всем metric sections;
- stable hosted runtime URL и production-bound operator runtime;
- deploy/auth-hardening и production storage binding.

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
- bounded sheet-side витрина уже есть как `DATA_VITRINA` и `STATUS`;
- live write и visual presentation подтверждены для этого bounded sheet-side contour;
- operator-facing upload trigger для `CONFIG / METRICS / FORMULAS` уже materialize-ится в текущей линии;
- compact v3 bootstrap operator sheets уже тоже материализован в текущей линии;
- full replacement operator-table и обратная загрузка server-side truth в таблицу пока ещё не являются частью `main`, но на текущей PR-ветке появляется первый bounded readback path в `DATA_VITRINA`.

## Current Main-Confirmed Layers

Материализованные слои и каталоги в `main`:
- `apps/`: smoke runners, local live-write runners и sheet-side utilities;
- `packages/contracts`: typed DTO, schemas и contract validation;
- `packages/application`: bundle assembly, orchestration и use-cases;
- `packages/adapters`: WB API, Sheets и другие I/O adapters в тех блоках, которые уже смёржены;
- `artifacts/`: fixture, parity и evidence для bounded implementation blocks;
- `registry/pilot_bundle/`: pilot registry artifacts для новой витрины;
- `gas/sheet_vitrina_v1/`: bound Apps Script wiring для новой витрины;
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

Таблица может:
- собирать operator input;
- показывать operator-visible outputs;
- триггерить controlled workflows через явные интерфейсы.

Таблица не должна:
- быть единственным местом production-truth;
- владеть тяжёлыми transform-ами или long-running fetch-логикой;
- скрывать server behavior, которое нельзя review-ить в Git.

Текущий `main` уже реализует bounded read-side/sheet-side форму этого правила:
- таблица и витрина получают controlled bundles;
- registry upload bundle, file-backed service, DB-backed runtime, live HTTP entrypoint, sheet-side upload trigger и compact v3 bootstrap уже материализованы;
- bounded reverse-load обратно в `DATA_VITRINA` пока ещё не часть `main`, но на текущей PR-ветке появляется первый controlled load path без daily orchestration и без deploy.

### Граница Сервера

Сервер владеет:
- ingestion и sync jobs;
- domain-calculations;
- materialization of snapshots;
- contract validation;
- auditability и runtime observability.

Текущее ограничение `main`:
- contracts, artifact-backed bundle, file-backed service, DB-backed runtime, live HTTP entrypoint, operator-facing sheet trigger и compact v3 bootstrap уже есть;
- deploy/auth hardening и reverse load из server-side truth обратно в таблицу пока не смёржены в текущий `main`; на текущей PR-ветке reverse-load materialize-ится как bounded MVP-layer без full parity и без deploy.

### Граница Web-Source

Web-source capture — это adapter, а не domain-логика.

Факты из reference:
- `wb-web-bot` захватывает payload `search-report/report` через Playwright;
- `wb-ai-research/wb-ai/web_sources/client.py` содержит прямой HTTP client для WB search report;
- `wb-table-audit/apps-script/src/44_raw_search_analytics_snapshot.js` потребляет server endpoint `GET https://api.selleros.pro/v1/search-analytics/snapshot`.

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
