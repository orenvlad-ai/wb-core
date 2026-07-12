# Source Of Truth Policy

## Приоритет источников

Для каждого типа фактов используется один canonical owner:

- Git-tracked code и current `origin/main` — code truth для текущей реализации;
- `README.md`, `docs/architecture/*`, `docs/modules/*` и `migration/*` — authoritative documentation truth;
- GitHub — truth для branch, commit, PR, checks, review и merge;
- WebCore Data MCP — read-only источник наблюдаемого production-состояния, диагностики и бизнес-метрик;
- production server — canonical deploy/runtime boundary;
- legacy artifacts, старые чаты, вложения и прежние project instructions — только migration evidence и do-not-lose constraints, но не current truth.

Рабочая ветка показывает proposed change, но не заменяет current `origin/main` до review и merge. Runtime-наблюдение не заменяет versioned code или contracts.

## Code И Documentation Truth

Runtime-only edits недействительны, пока эквивалентное изменение не зафиксировано в Git, не проверено и не смёржено.

Authoritative docs должны описывать текущую реализацию и устойчивые boundaries, а не служить журналом временных snapshots. Если задача меняет code, contract, module status, runtime boundary, deploy path, schema или другую зафиксированную истину, затронутые docs обновляются в той же задаче.

Корневой [`AGENTS.md`](../../AGENTS.md) — короткий самодостаточный execution/governance entrypoint для Codex и ChatGPT, читающего репозиторий. Он не дублирует доменные контракты и направляет к authoritative docs. Для текущей работы не требуется отдельный project pack или прежняя ChatGPT Project instruction.

## Кураторский Протокол

Перед техническим выводом, формулированием задачи, реализацией или проверкой результата другого агента необходимо сверить:

- актуальный GitHub state;
- корневой `AGENTS.md`;
- только релевантные authoritative docs;
- фактический Git-tracked code, если вывод касается текущей реализации.

Отчёт агента, старый чат или вложение не заменяют такую проверку. Если repository/GitHub либо другой необходимый authoritative source недоступен, нельзя уверенно заявлять current state: должен быть возвращён точный blocker.

## Production Runtime Boundary

Production server является единственной canonical границей deploy и runtime execution:

- изменения доставляются только через repo-owned deploy/runbook path;
- server-only code/config patches и ручной drift запрещены;
- ad-hoc SQL и произвольные server mutations не являются completion path;
- secrets, session state, production DB dumps и credential-bearing artifacts не попадают в Git, docs, logs или PR;
- repo хранит config shape, non-secret defaults, contracts и deployment artifacts, а environment-specific secrets остаются вне Git.

WebCore Data MCP остаётся строго read-only. Его данные пригодны для диагностики, freshness checks и бизнес-метрик, но не дают права изменять production и не становятся code truth.

## Schema, Config И Data Truth

- Schema truth живёт в versioned contracts и schema artifacts.
- Repo-owned config shape и безопасные defaults живут в Git; secret values — вне Git; operator-managed business inputs меняются только через явные interfaces.
- Accepted production data живёт в durable server-side stores и versioned/runtime-observable snapshots, а не в browser state, temporary raw tabs или памяти человека.
- User-facing `ЕБД` означает общий server-side accepted truth/runtime layer `wb-core`; это не Google Sheets/GAS, HTML UI, browser `localStorage` или private manual table.
- Server-only behavior допустимо только когда оно воспроизводимо из versioned code и наблюдаемо через bounded logs, metrics и audit evidence.

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
