# Target Architecture

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

Новая таблица не создаётся на Phase 0/1.

Жёсткое правило:
- новая таблица не создаётся, пока core сам не умеет отдавать нужные server-side read-models и projections.

## Слои Core

Целевые слои:
- `apps/`: ограниченные entrypoint-ы, например будущий operator shell, internal APIs или admin tools;
- `packages/domain`: чистые business/domain rules;
- `packages/application`: orchestration и use-cases;
- `packages/contracts`: typed DTO, schemas и interface contracts;
- `packages/adapters`: WB API, Sheets, DB, browser/web-source и другие I/O adapters;
- `infra/`: environment bootstrapping, local/dev ops и позже deployment descriptors.

Inference:
- точные имена пакетов могут измениться;
- жёсткое разделение между domain, application, contracts и adapters меняться не должно.

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

### Граница Сервера

Сервер владеет:
- ingestion и sync jobs;
- domain-calculations;
- materialization of snapshots;
- contract validation;
- auditability и runtime observability.

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
