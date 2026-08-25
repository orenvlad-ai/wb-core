# Рабочий протокол `wb-core`

Этот файл — единственный operational entrypoint. Подробности CI/release живут
в [`docs/architecture/11_github_release_train.md`](docs/architecture/11_github_release_train.md),
а доменные инварианты — в `docs/architecture/*`, `docs/modules/*` и
`migration/*`. Исторические чаты, ветки, labels и Git history не являются
текущими инструкциями.

## Источники истины

1. `origin/main` и Git-tracked code задают code truth.
2. Authoritative docs задают действующие contracts.
3. GitHub задаёт PR, exact checks, merge и immutable release receipts.
4. Canonical production host и server-owned stores задают runtime/data truth.

Перед техническим выводом или изменением прочитай этот файл, только относящиеся
к задаче docs/code и fresh GitHub state. Перед записью проверь status/remotes,
выполни `git fetch --prune origin`, создай отдельную ветку/worktree от current
`origin/main` и не смешивай чужой dirty state.

Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное. Prompt не называет WebCore Data MCP обязательным путём:
это архивный read-only compatibility contour, а не prerequisite или fallback.
Current canonical source acquisition остаётся server-side.

## Жизненный цикл задачи

- Main chat владеет целью, уже принятыми business-решениями, коротким task
  passport, границами scope и owner acceptance.
- При первом substantive сообщении новой WBC-задачи main chat автоматически
  получает номер, имя `wbc NNNN <короткое русское название>` и pin. Только
  semantic часть имеет максимум 25 символов.
- Ясное намерение `реализуй` / `исправь` / `сделай` разрешает implementation
  dispatch. После design-интервью его разрешают `запускай` / `принимаю`.
- На один bounded implementation block создаётся ровно один fresh visible
  internal subagent. Его internal/task name соответствует
  `wbc NNNN SSS <latin transliteration>`: `SSS` последователен внутри main
  task, semantic часть — детерминированная латинская транслитерация русского
  названия, а не английский перевод (`istoriya-ostatkov`, не
  `inventory-history`), и не длиннее 20 символов; subagent не pin-ится.
- Implementation dispatch выполняется только current internal-subagent
  mechanism `collaboration.spawn_agent`. `codex_app.create_thread`,
  `fork_thread`, `handoff_thread` и `send_message_to_thread` не являются его
  заменой: user-owned task/thread создаётся только по прямой просьбе
  пользователя. Если internal spawn недоступен, main chat возвращает exact
  tooling blocker и не создаёт sidebar peer task. Internal subagent виден в
  `Subagents`/`Activity` main task, не pin-ится и не создаёт
  `::created-thread`.
- Dispatch передаёт compact task passport и minimal bounded context. Полная
  длинная main history не копируется по умолчанию; старые задачи читаются
  on-demand только как evidence.
- По умолчанию активен максимум один implementation subagent. Project config
  фиксирует тот же concurrency limit. Model-tier classification не используется.
- Один implementation subagent владеет ровно одной веткой и одним PR. Новый
  PR, включая infrastructure recovery, требует terminal handoff текущего блока
  и следующего последовательного `SSS`; corrections в том же PR остаются у
  текущего subagent.
- Same-scope correction продолжает того же subagent. Материально новый scope
  или новый PR получает следующий `SSS` и нового subagent после terminal state
  предыдущего.
- Subagent возвращает один terminal handoff и становится `Done`. Настоящий gate
  возвращает точный callback с одним требуемым действием и не остаётся
  неопределённо `Active`; pause/blocker является немедленным terminal
  transition, а не причиной держать implementation task активной.
- Main/subagent публикуют только meaningful state transitions. Одинаковые
  сообщения «ещё идёт», частый polling неизменного CI и heartbeat-status
  запрещены; используются event/terminal waits и UI activity. Истинный
  blocker или terminal state сообщается сразу.
- Пользователь не подтверждает повторно уже выбранную цель, business meaning,
  accepted exact plan или обычные технические решения внутри scope.

Минимальный task passport main chat: цель; accepted decisions; included и
excluded scope; acceptance; текущий implementation block и subagent identity;
PR/plan hash/terminal receipt после появления.

## Human gates

Остановись только перед одним из следующих событий:

1. genuinely new business/product meaning, не выводимый из accepted goal;
2. material scope expansion;
3. credentials/login/2FA/captcha без разрешённого non-interactive пути;
4. exact security/access/ruleset/new-destination change, не preauthorized;
5. proven irreversible action или exact production-data apply manifest.

Routine repo/GitHub/tests/merge, existing live deploy и technical remediation
автономны внутри authorized scope. Accepted exact plan заранее разрешает
перечисленные в нём settings changes. Missing capability сообщает точный
tooling blocker; оно не превращается в запрос покомандных approvals.

## Repository и release flow

1. Один implementation block использует одну ветку и один non-draft PR в
   `main`. Labels не выбирают tests, release kind или state.
2. `ci/test_planner.py` строит byte-stable `test-plan.json` по exact base/head,
   base+head registry union, transitive dependencies и changed paths.
3. Unknown path, registry/workflow/core-framework change или unresolved mapping
   автоматически выбирает full regression. Пользователь и labels не выбирают
   tests.
4. Genuine `pull_request` workflow выполняет fast core, выбранные suites и один
   aggregate check `pr-gate`. Diagnostic dispatch имеет другое имя и не
   удовлетворяет required context. Untrusted PR code не получает secrets.
5. После successful exact-head `pr-gate` trusted-main Release Runner один раз
   проверяет open non-draft same-repo PR в `main`, exact head/base/plan hash и
   mergeability. Он не запускает tests и не исполняет unmerged PR code.
6. Runner делает один expected-head squash merge и exact readback. `repo_only`
   завершается receipt `done`; `live_runtime` deploy-ит exact merge SHA через
   canonical adapter, проверяет deployed SHA и пишет один receipt.
7. `production_mutation` после merge/deploy только query-only читает exact
   manifest и пишет `awaiting_apply`. Default-off Apply Runner требует exact
   OWNER/MEMBER authorization, durable operation id, один apply, readback и
   reconciliation. Blind retry запрещён.

Stable receipt states: `done`, `awaiting_apply`, `blocked`, `superseded`,
`already_terminal`. Queue, scheduled polling, blind resubmit, branch auto-sync,
release labels и отдельный recovery carousel не используются.

User artifact (`XLSX/CSV/DOCX/PDF/TXT`) — единственная requested mutation вне
репозитория: это `user-artifact`, не является `ДИАГНОСТИКОЙ` и не создаёт
branch, worktree или GitHub release. Для XLSX используй active Spreadsheets
skill и `@oai/artifact-tool`; runtime discovery предоставляет
`CODEX_PRIMARY_RUNTIME_ROOT`, `CODEX_PRIMARY_RUNTIME_NODE`,
`CODEX_PRIMARY_RUNTIME_NODE_MODULES`, `CODEX_PRIMARY_RUNTIME_PYTHON`.
Отсутствие `load_workspace_dependencies` само по себе не blocker; fallback:
installed `openpyxl`, затем `xlsxwriter`, затем dependency-free OOXML.

## Независимые safety invariants

- Никогда не deploy-ить незамёрженный PR. Live deploy использует clean exact
  merge SHA и canonical `deploy-and-verify` adapter.
- Missing/ambiguous identity, SHA, manifest, provenance или transport result
  fail closed. Не угадывай success и не делай blind retry.
- Production read сначала определяет current target/source, затем использует
  штатный SSH и query-only server-owned access (`mode=ro` и
  `PRAGMA query_only=ON` для SQLite) либо эквивалент.
- Production probe сначала разрешает exact canonical target и передаёт его
  runner-у явно (`--target-file <canonical-target>`). Первый вызов
  legacy/default target с расчётом на последующий guard не является preflight
  или acceptance evidence.
- Production mutation: dry-run default, explicit apply, bounded manifest,
  pre-change digest, backup/recovery evidence, expected affected records,
  non-target invariants, idempotency либо documented recovery, post-apply
  readback и reconciliation. Ad-hoc SQL и local/server-only mutation запрещены.
- Finance/storage additionally сохраняет lease/operation identity, coherent
  snapshot, capacity, writer/timer/barrier, exact target/generation, restore и
  non-target contracts из hosted runtime docs. Эти guards не образуют queue.
- Никаких secrets, production dumps или unrelated edits в Git. Destructive
  targets разрешаются exact paths/identities; broad recursive deletion
  запрещено.

## Проверка и handoff

Перед завершением прочитай semantic diff целиком; запусти risk-proportionate
checks; исправь findings и повтори checks; синхронизируй authoritative docs;
проверь exact PR/head/plan/check/receipt/merge/deploy state и отсутствие
unresolved review threads.

Terminal handoff содержит: status; что сделано; что исключено; PR/head/merge и
main SHA; test plan hash и checks; receipt/deploy/apply state; subagent/task
identity; сложности, risks и blockers. Technical completion не синтезирует
owner acceptance и не архивирует/открепляет пользовательские задачи.
