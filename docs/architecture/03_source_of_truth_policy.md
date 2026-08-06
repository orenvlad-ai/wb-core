# Source Of Truth Policy

## Приоритет источников

Для каждого типа фактов используется один canonical owner:

- Git-tracked code и current `origin/main` — code truth для текущей реализации;
- `README.md`, `docs/architecture/*`, `docs/modules/*` и `migration/*` — authoritative documentation truth;
- GitHub — truth для branch, commit, PR, checks, review и merge;
- production server и его current server-owned stores/documents — canonical deploy/runtime и production-data boundary;
- WebCore Data MCP — архивный read-only compatibility contour, не normal source/acquisition path и не обязательная capability;
- legacy artifacts, старые чаты, вложения и прежние project instructions — только migration evidence и do-not-lose constraints, но не current truth.

Рабочая ветка показывает proposed change, но не заменяет current `origin/main` до review и merge. Runtime-наблюдение не заменяет versioned code или contracts.

## Code И Documentation Truth

Runtime-only edits недействительны, пока эквивалентное изменение не зафиксировано в Git, не проверено и не смёржено.

Authoritative docs должны описывать текущую реализацию и устойчивые boundaries, а не служить журналом временных snapshots. Если задача меняет code, contract, module status, runtime boundary, deploy path, schema или другую зафиксированную истину, затронутые docs обновляются в той же задаче.

Корневой [`AGENTS.md`](../../AGENTS.md) — короткий самодостаточный execution/governance entrypoint для Codex и ChatGPT, читающего репозиторий. Он не дублирует доменные контракты и направляет к authoritative docs. Для текущей работы не требуется отдельный project pack или прежняя ChatGPT Project instruction.

Исторический saved project `WB Core · Кураторы` и его workspace automation не
являются active runtime или source of truth. Куратор использует корневой
протокол и передаёт реализацию отдельному исполнителю; archive pointer:
[retired curator workspace](13_codex_curator_workspace.md).

## Кураторский Протокол

Перед техническим выводом, формулированием задачи, реализацией или проверкой результата другого агента необходимо сверить:

- актуальный GitHub state;
- корневой `AGENTS.md`;
- только релевантные authoritative docs;
- фактический Git-tracked code, если вывод касается текущей реализации.

Отчёт агента, старый чат или вложение не заменяют такую проверку. Если repository/GitHub либо другой необходимый authoritative source недоступен, нельзя уверенно заявлять current state: должен быть возвращён точный blocker.

Технический путь, записанный в task prompt, также не заменяет current protocol. Название connector/tool/server/storage, конкретный access path или запрет canonical server-side read считаются гипотезой автора prompt и повторно проверяются по repository truth, даже если записаны как команда. Пользовательским ограничением является только отдельно и явно выраженное пользователем требование. Поэтому устаревший prompt с обязательным MCP не создаёт blocker и не отменяет canonical production read path.

Новый prompt для Codex описывает цель, необходимые данные, read-only или mutation boundary, ожидаемый результат и acceptance/closure criteria. Он не называет WebCore Data MCP и не выбирает за Codex server/runtime/storage/access mechanism. Для однозначного provenance prompt фиксирует: `Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.`

## Production Runtime Boundary

Production server является единственной canonical границей deploy и runtime execution:

- изменения доставляются только через repo-owned deploy/runbook path;
- server-only code/config patches и ручной drift запрещены;
- ad-hoc SQL и произвольные server mutations не являются completion path;
- secrets, session state, production DB dumps и credential-bearing artifacts не попадают в Git, docs, logs или PR;
- repo хранит config shape, non-secret defaults, contracts и deployment artifacts, а environment-specific secrets остаются вне Git.

Для production evidence/data Codex сначала определяет current active target, runtime и конкретный source по code и authoritative docs, выполняет фактический production preflight и использует штатный SSH-доступ к canonical server. Production stores читаются query-only (`mode=ro` + `PRAGMA query_only=ON` для SQLite либо эквивалентная гарантия), server-owned documents — только bounded read по текущему contract. Никакие service changes, deploy, upstream sync или production writes этим не разрешаются.

Blocker допустим только после фактической попытки canonical server-side preflight/read и точной ошибки доступа либо доказанного отсутствия необходимых данных. Недоступность архивного WebCore Data MCP blocker не образует. Сохранившийся MCP implementation/runtime может использоваться только как явно заданный compatibility/archival evidence contour; он не становится code truth, не выбирается как normal path и не даёт права изменять production.

## Schema, Config И Data Truth

- Schema truth живёт в versioned contracts и schema artifacts.
- Repo-owned config shape и безопасные defaults живут в Git; secret values — вне Git; operator-managed business inputs меняются только через явные interfaces.
- Accepted production data живёт в durable server-side stores и versioned/runtime-observable snapshots, а не в browser state, temporary raw tabs или памяти человека.
- User-facing `ЕБД` означает общий server-side accepted truth/runtime layer `wb-core`; это не Google Sheets/GAS, HTML UI, browser `localStorage` или private manual table.
- Server-only behavior допустимо только когда оно воспроизводимо из versioned code и наблюдаемо через bounded logs, metrics и audit evidence.

## User Artifact Boundary

Созданный по запросу пользователя XLSX/CSV/DOCX/PDF/TXT вне репозитория является производным export/snapshot для передачи или анализа. Он не становится canonical code, schema, documentation, production или business-data source of truth и не подменяет источник, из которого был построен. Если единственная mutation — такой файл, применяется non-PR execution contour `user-artifact`; изменение Git-tracked правил или helper остаётся обычным `repo-only` change.

## Legacy Boundary

Legacy repositories, Apps Script/GAS artifacts и historical sheet/export paths используются только:

- как migration evidence;
- для parity и do-not-lose constraints;
- для явно заданного archived GAS guard scope.

Они не являются normal development, runtime, write, deploy или verification path. Полные legacy dumps и устаревшие current-state snapshots не переносятся в canonical docs.

## Anti-Drift Rules

- никакого manual production patch без эквивалентного reviewed Git change;
- никакого contract/schema change без синхронизации tests и authoritative docs;
- никакой runtime snapshot не объявляется code или schema truth;
- никакого cutover по принципу «на сервере вроде работает»;
- никакой production mutation без explicit scope, dry-run, backup/reversibility, idempotency, audit и требуемых human gates;
- никакой потери или смешивания чужого dirty state при branch/sync/merge работе;
- никакого подтверждения результата только по отчёту агента без проверки применимых branch/commit, semantic diff, checks, review state и authoritative docs;
- каждый merged change подтверждается в current `origin/main`, а live/runtime change дополнительно — canonical deploy и live/public verify.
## Canonical cost and product movement

From `2026-07-01`, cost consumers share the functional historical/current daily WB WAC projection; full physical warehouse truth begins only at production `warehouse_functional_cutover_v1`. Physical sources remain supplier registry, append-only `ff_stock_ledger`, persisted WB supply evidence and complete official WB contour snapshots. Module 40 and the public read facade of module 45 cannot own independent quantity, cost or baseline truth; module 45's stable source events are canonical inputs to the same functional projection, not a competing read model.

The functional engine owns six mutually exclusive stages. Open `packed - accepted` stays in `FF → WB` only until final acceptance; positive final difference then becomes `Расхождения приёмки WB`. Transitional unmatched doprinato is audit, not a negative warehouse. Accepted supply never adds quantity on top of the official WB snapshot.

The frozen 24.06 opening-cost map is a guarded migration fact, not a refreshed fallback. It derives missing SKU cost through explicit same-price-band/interpolation/extrapolation/fallback quality and must cover every positive historical WB quantity without silent zero/NULL. Future purchase-price changes cannot rewrite it. Any correction requires a fingerprinted targeted replay from factual effective date.

## Functional time versus audit time

Warehouse/product-capital source truth follows
`business_effective_date`/`snapshot_date`. `recorded_at`, `created_at`,
`effective_at` and `published_at` prove when evidence or a projection revision
was stored; they never move a business event to a later functional day.

The one-way contour is canonical source documents/operations → canonical
events and source revisions → exact business-date functional projection →
read-only Web Vitrina materialization. The projection may replace only its
owned stable warehouse/product-capital metric keys in memory. Vitrina cannot
write sources and cannot calculate an alternative warehouse state.
