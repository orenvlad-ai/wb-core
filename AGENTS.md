# Рабочий протокол `wb-core`

Этот файл — самодостаточный entrypoint для разработки и выпуска `wb-core`.
Доменная архитектура живёт в `docs/architecture/**`, `docs/modules/**` и
`migration/**`.

## Источники истины

1. Актуальный `origin/main` и Git-tracked code задают code truth.
2. Authoritative docs фиксируют архитектуру, runtime и safety-контракты.
3. GitHub задаёт PR, exact head, checks, review, merge и Release Train truth.
4. Production server — canonical deploy/runtime и production-data boundary.
5. Старые чаты, архивные MCP и migration artifacts — только evidence, не
   current execution path.

Перед выводом или изменением прочитай current GitHub state, этот файл, только
релевантные authoritative docs и фактический код. Выбор инструмента из prompt
всегда перепроверяется по current protocol. Не смешивай и не очищай чужой dirty
checkout: создай отдельный clean worktree от свежего `origin/main`.

Новый prompt не называет WebCore Data MCP и не hardcode-ит source path.
Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.
Production evidence читается через штатный SSH из canonical server-side,
server-owned stores/documents; SQLite использует `query_only=ON`. Архивный
read-only MCP не является normal path, а его отсутствие не blocker.

## Простой автономный flow

Обычная задача проходит один последовательный контур:

1. Куратор формулирует цель, acceptance и границы и запускает отдельного
   исполнителя.
2. Исполнитель работает в isolated worktree/branch, реализует изменение,
   обновляет authoritative docs и запускает targeted checks.
3. После fresh independent semantic review исполнитель создаёт open non-draft
   PR с label `task:standard` и ровно одним scope:
   `scope:repo-only`, `scope:live-runtime` или, только для apply-операции,
   `scope:production-mutation`.
4. На exact PR head должен быть успешен required check `baseline`.
5. OWNER/MEMBER ставит exact head в очередь trusted comment-командой:

   `/wb-core release enqueue <PR> head <HEAD_SHA>`

6. Trusted-main GitHub Actions повторно проверяет actor, open non-draft PR,
   same-repository branch, exact SHA, scope и successful baseline, публикует
   Actions-owned enqueue proof и только затем устанавливает `release:ready`.
7. Serialized Release Train синхронизирует PR с current `main`, запускает fresh
   baseline, проверяет неизменность head/scope/proof и выполняет merge.
8. `scope:repo-only` завершается на `release:done`.
   `scope:live-runtime` проходит canonical deploy-and-verify и завершается на
   `release:production`; deploy/verify failure переводит exact merged PR в
   `release:halted` и останавливает следующую mutation до reconciliation.
9. Исполнитель передаёт короткий отчёт с PR, merge/deploy SHA, checks и
   остаточными рисками. Техническое завершение не означает owner acceptance.
   Владелец принимает результат вручную.

Ручная установка `release:ready`, прямой push в `main`, stale SHA, fork head,
draft PR, failed/missing check и comment от недоверенного actor не являются
enqueue proof и не допускают merge. Если Release Train синхронизировал branch и
head изменился, нужен новый exact-head enqueue; старый proof не переносится.

## Execution-контуры

- `read-only`: диагностика без mutations.
- `user-artifact`: только запрошенный пользовательский файл вне Git; PR не
  создаётся.
- `repo-only`: code/docs change без live эффекта; полный PR/Release Train
  closure.
- `live-runtime`: code/runtime change; полный PR, deploy и live verify.
- `production data mutation/backfill`: единственный bounded apply после
  dry-run, backup/reversibility, exact HumanGate, audit, reconciliation и
  non-target invariants. Подготовительный код остаётся repo-only/live-runtime.

Production изменения выполняются только repo-owned runner’ом. Запрещены
server-only drift, ad-hoc SQL, секреты в Git/docs/logs/PR и business mutation
вне safety-контура.

`user-artifact` не является `ДИАГНОСТИКОЙ`: разрешена запись только точного
пользовательского файла вне repository, а branch, worktree mutation, commit,
PR, GitHub labels, Release Train и production запрещены. Label
`scope:user-artifact` не существует. Для XLSX сначала используй
`load_workspace_dependencies`. Отсутствие `load_workspace_dependencies` само по себе не blocker.
Bundled path использует `CODEX_PRIMARY_RUNTIME_NODE` и
`CODEX_PRIMARY_RUNTIME_NODE_MODULES`, затем уже установленные `openpyxl`,
`xlsxwriter`, затем dependency-free `OOXML`. Новые network dependencies не
устанавливаются, а source data между fallback-попытками не собираются заново.

## Release Train invariants

- Durable очередь и terminal state хранятся на GitHub PR через labels и
  Actions-owned proof comments.
- Один workflow concurrency group сериализует queue, sync, checks, merge,
  deploy и reconciliation.
- Eligibility требует current exact-head enqueue proof. Label сам по себе не
  является authority.
- `release:halted` блокирует очередь до exact-SHA repo-owned reconciliation.
- Repo-only и live-runtime используют один обычный flow; live verify не
  владеет Codex/Desktop chat session и не требует callback.
- Production mutation сохраняет отдельный closed HumanGate и exact terminal
  evidence contract.
- Retry после исправления выполняется fresh baseline + новая exact-head enqueue
  command. Blind/manual retry не меняет state.
- Scheduled run с пустой очередью быстро и успешно сообщает idle.

Machine implementation: `.github/workflows/release-train.yml`,
`apps/github_release_train.py`, `apps/github_release_train_spec.py` и
`apps/github_release_train_smoke.py`. Подробности: [GitHub Release Train](docs/architecture/11_github_release_train.md)
и [Codex execution protocol](docs/architecture/07_codex_execution_protocol.md).

## Archived orchestration epoch

Старый Global Watcher, локальный orchestration registry, Task Passport,
acceptance envelope, curator workspace, logical release lane и LOOP
session-handshake выведены из active runtime. Они не создаются, не читаются и
не требуются для release eligibility. Исторические contracts доступны через
Git history на migration anchor `e44f548982900e286a2c1a73fdf439d0c8a49843`;
краткие archive pointers находятся в
`docs/architecture/12_codex_global_orchestration.md` и
`docs/architecture/13_codex_curator_workspace.md`.

Не запускай второй scheduler/registry writer, Global Watcher, Reporter,
heartbeat или persistent arbiter. Не удаляй пользовательские чаты и не
синтезируй фразу принятия задачи.

## Проверка и closure

Перед enqueue проверь status/branch/remotes/auth, exact `origin/main`, diff,
targeted tests, docs sync, independent semantic review и отсутствие unresolved
P0–P2 findings. Не считай завершением план, незакоммиченный diff или открытый
PR.

HumanGate допустим только для действительно human-exclusive credential/login/
2FA/captcha, необратимого риска данных, нового security/permission назначения
или platform hard stop. Git/GitHub/CI/test/retry/merge/deploy/queue и
обратимые инженерные решения не являются HumanGate.

Итог сообщает: статус, фактически изменённое, проверенное, намеренно оставленное
вне scope и единственный минимальный blocker только при его наличии. До явной
приёмки владельца финальный технический статус: `Завершена — требуется приёмка`.
