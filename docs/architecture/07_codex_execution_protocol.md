# Codex Execution Protocol

## Назначение

Протокол задаёт простой путь от пользовательской цели до проверенного GitHub и
runtime результата. Он не создаёт orchestration registry, Task Passport,
acceptance envelope, Global Watcher, Reporter или callback в существующий Mac
Codex chat.

## Prompt И Source Boundary

Prompt фиксирует цель, acceptance, scope, риски и запрещённые mutations.
Предложенные инструменты и пути являются технической гипотезой и проверяются по
current `AGENTS.md`, authoritative docs, code и фактическому runtime.

Приоритет: current `origin/main` → authoritative docs → GitHub PR/check/merge →
canonical production readback. Старый чат и архивный MCP не являются current
truth. Пользовательский dirty checkout не очищается и не используется как
чистая base.

Новый prompt не называет WebCore Data MCP и не выбирает storage/access path.
Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.
Canonical production evidence читается через штатный SSH из server-side,
server-owned stores/documents; SQLite использует `query_only=ON`. Отсутствие
архивного MCP не является blocker.

## Куратор И Исполнитель

Куратор формулирует одну цель и запускает отдельного исполнителя. Исполнитель
владеет одной isolated branch/worktree, implementation, checks, review, PR,
Release Train closure и коротким handoff. Никакой периодический монитор или
второй scheduler не нужен.

Состояние задачи в пользовательском смысле не выводится из GitHub label.
`release:done`/`release:production` означают technical closure, после которого
владелец отдельно принимает или возвращает результат.

## Execution-Контуры

- `read-only`: диагностика без mutations.
- `user-artifact`: единственная mutation — запрошенный файл вне репозитория.
- `repo-only`: code/docs change без live эффекта.
- `live-runtime`: public route, process, deploy wiring или runtime behavior.
- `production data mutation/backfill`: bounded apply через отдельный closed
  HumanGate после dry-run/backup/reconciliation evidence.

Новый или неоднозначный code-changing запрос использует обычный
`task:standard`. LOOP session-handshake и chat ownership не используются.

### Standalone XLSX / `user-artifact`

`user-artifact` не является `ДИАГНОСТИКОЙ`: единственная mutation — exact
пользовательский файл вне repo. Branch/worktree mutation, commit, PR, label
`scope:user-artifact`, Release Train и production запрещены.

Для нового XLSX сначала вызови `load_workspace_dependencies`.
Отсутствие `load_workspace_dependencies` само по себе не blocker. Primary builder
использует `CODEX_PRIMARY_RUNTIME_ROOT`, `CODEX_PRIMARY_RUNTIME_NODE`,
`CODEX_PRIMARY_RUNTIME_NODE_MODULES` и `CODEX_PRIMARY_RUNTIME_PYTHON` во
временной директории вне repo. Bounded fallback: уже установленные `openpyxl`,
затем `xlsxwriter`, затем dependency-free ZIP/XML `OOXML`; новые зависимости
из сети не устанавливаются и source data повторно не собираются.

## Phase-Local Preflight

Repository implementation не блокируется отсутствующей capability будущей
production-фазы. Разделяй:

1. `REPOSITORY_PREFLIGHT`: exact main, rules, dirty-state isolation, GitHub auth.
2. `PRODUCTION_READ_PREFLIGHT`: query-only canonical runtime evidence.
3. `PRODUCTION_MUTATION_PREFLIGHT`: exact apply scope, backup и HumanGate.
4. `PRODUCTION_UI_PREFLIGHT`: isolated browser/runtime verification, только если
   требуется целью.

SQLite читается через `mode=ro`/`query_only`; production writes допускаются
только repo-owned runner’ом.

## Implementation И Review

Исполнитель:

1. создаёт branch от fresh `origin/main`;
2. делает минимальный coherent diff и синхронизирует docs;
3. запускает deterministic checks/fakes;
4. выполняет fresh independent semantic/security review exact head;
5. исправляет P0–P2, ведёт bounded remediation; после двух corrective cycles
   park/escalate, не запускает бесконечный model loop;
6. создаёт open non-draft PR с `task:standard` и одним scope;
7. ждёт successful `baseline` на exact head;
8. выполняет trusted exact-head enqueue:

   `/wb-core release enqueue <PR> head <HEAD_SHA>`

Manual label, stale proof или direct push не являются closure.

## Release И Runtime Verification

GitHub Release Train владеет serialization, main sync, fresh baseline, exact
merge, canonical deploy и verify. Если sync меняет head, PR остаётся fail-closed
до нового enqueue exact head.

Live verify использует canonical server-owned evidence и не владеет активной
Codex/Desktop session. HTTP-only proof достаточен только когда контракт задачи
не требует UI. Для UI используется isolated Playwright/browser context с final
URL, document/render, console/page errors и screenshot evidence; пользовательский
profile/cookies не используются без явного разрешения.

Deploy/verify failure после merge создаёт `release:halted`. Восстановление —
только repo-owned exact-SHA reconciliation; следующий release не обходит halt.

## Production Data Mutation

Apply runner обязан иметь dry-run по умолчанию, explicit apply, bounded cohort,
pre-change digest, backup/reversibility, idempotency/recovery, post-apply
readback, reconciliation и non-target invariants. HumanGate подтверждает только
сам apply. Кодовая подготовка, tests и deploy runner не ждут HumanGate.

Terminalization использует exact contract из
`docs/architecture/11_github_release_train.md`; ad-hoc SQL и server-only drift
запрещены.

## Callback Boundary

Автоматический callback в существующий Mac Codex chat не входит в production
correctness. App Server thread/turn API допустим только для session, которой
владеет конкретный клиент; internal Codex SQLite/rollout schemas не читаются.
Handoff — короткий GitHub/deploy digest, который куратор копирует/открывает
вручную.

## HumanGate И Blocker

HumanGate допустим для отсутствующего credential, login/2FA/captcha,
необратимого риска данных, нового security/permission/external-data назначения
или platform hard stop. Git/GitHub/CI/test/review/retry/merge/deploy/queue,
контекст и обратимые инженерные решения не являются HumanGate.

## Closure И Итог

`repo-only`: merged exact PR + `release:done` + origin/main readback.

`live-runtime`: merged exact PR + deployed/verified SHA +
`release:production` + live readback.

`production mutation`: applicable release closure + HumanGate + apply audit +
reconciliation.

Финальный handoff сообщает PR/SHA, checks, deploy/readback и остаточные риски.
Он не пишет «Задача принята». До явного ответа владельца итоговый статус:
`Завершена — требуется приёмка`.
