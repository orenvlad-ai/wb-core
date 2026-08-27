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
- Ясное намерение `реализуй` / `исправь` / `сделай` автоматически компилирует
  accepted goal в authorization envelope и разрешает автономный implementation
  до `COMPLETE`. После design-интервью тот же контракт активируют `запускай` /
  `принимаю` / `доведи до конца`; пользователь не выбирает режим или trust tier.
- Явная user stop-line имеет приоритет над default completion: `design-only`
  запрещает implementation; `branch-only`/`до PR` заканчивается clean tested
  branch без PR; `до merge`/`до deploy` заканчивается draft PR, потому что
  trusted Release Runner объединяет merge/deploy admission и требует non-draft.
  Draft — machine hold и Runner его не допускает. Hold снимается только явной
  user instruction, действительно расширяющей прежнюю lifecycle boundary, а не
  inference из technical success или terminal subagent status; это не повторный
  technical human gate. Без stop-line действует один non-draft PR до `COMPLETE`.
- `Read-only` задаёт mutation/authority boundary, но не выбирает actor. Любой
  substantive technical execution образует bounded block одного из двух видов:
  diagnostic/read-only block без branch/worktree/PR/mutation либо implementation
  block с одной веткой и одним PR по repository flow.
- Owner-facing технический вывод, которому нужно новое evidence из repository/
  code, logs, server, database, external API либо длительное ожидание, выполняет
  fresh visible internal subagent даже при strict read-only scope. Main curator
  напрямую делает только curator-control reads: fresh protocol/docs для routing,
  compact preflight для bounded passport и exact readback уже существующего
  immutable task/subagent/PR/check/release/apply receipt/status artifact по его
  schema/digest/identity. Такой read не создаёт technical block только при нуле
  нового domain evidence, inference, external/server/database/log investigation
  и long wait. Любая semantic interpretation, mismatch diagnosis, новое
  substantive evidence или long wait остаются fresh subagent block. Pure
  conceptual answer, clarification/design conversation и вывод из уже
  существующего exact handoff subagent-а не требуют subagent-а. Diagnostic/
  read-only dispatch внутри запрошенной цели не требует отдельного human
  confirmation и не создаёт новый gate.
- Routing определяется purpose и ownership нового evidence, а не оценкой
  `простая/сложная`, минутами либо числом tool calls.
- Каждый evidence read/tool branch обязан разрешать exact acceptance predicate,
  blocker либо current failure hypothesis; иначе он silently не выполняется.
  Full logs/manifests/receipts остаются durable source/artifact, а active context
  и handoff несут exact pointer/digest, bounded relevant ranges/component diff и
  conclusion без повторного raw-copy. Повторный read допустим только после new
  event/drift/question. Отсутствие full bytes в active context не означает
  отсутствие durable evidence; safety и provenance не ослабляются.
- На один bounded technical execution block создаётся ровно один fresh visible
  internal subagent. Его internal/task name соответствует
  `wbc NNNN SSS <latin transliteration>`: `SSS` последователен внутри main
  task, semantic часть — детерминированная латинская транслитерация русского
  названия, а не английский перевод (`istoriya-ostatkov`, не
  `inventory-history`), и не длиннее 20 символов; subagent не pin-ится.
- Technical execution dispatch выполняется только current internal-subagent
  mechanism `collaboration.spawn_agent`. `codex_app.create_thread`,
  `fork_thread`, `handoff_thread` и `send_message_to_thread` не являются его
  заменой: user-owned task/thread создаётся только по прямой просьбе
  пользователя. Если internal spawn недоступен, main chat возвращает exact
  tooling blocker и не создаёт sidebar peer task. Internal subagent виден в
  `Subagents`/`Activity` main task, не pin-ится и не создаёт
  `::created-thread`.
- Dispatch передаёт compact task passport и minimal bounded context. Обычный
  technical execution spawn обязан использовать exact `fork_turns:"none"`.
  Положительный history fork и `fork_turns:"all"` запрещены. Старые задачи
  читаются on-demand только как evidence.
- По умолчанию активен максимум один technical execution subagent. Project config
  фиксирует тот же concurrency limit. Model-tier classification не используется.
- Без explicit stop-line implementation subagent владеет ровно одной веткой и
  одним non-draft PR; branch/draft stop-line завершает его на указанной выше
  boundary. Diagnostic subagent branch/worktree/PR не создаёт. Terminal diagnosis
  заканчивает этот block; отдельно разрешённая затем implementation является
  следующим bounded block со следующим последовательным `SSS`. Same-scope
  correction в текущем block/PR продолжает того же subagent. Материально новый
  scope или новый PR, включая infrastructure recovery, получает нового subagent
  после terminal state предыдущего.
- Task passport относит terminal pre-submit failures и выпущенные correction PR
  к family одного accepted goal и одной operation/lifecycle family. После двух
  terminal pre-submit failures либо двух последовательно выпущенных correction
  PR той же family третий incremental patch/retry запрещён: main фиксирует
  `EVIDENCE_BLOCKED` без human gate и dispatch-ит один consolidated diagnostic
  block. Он строит bounded production-shaped lifecycle scenario matrix по уже
  доказанным failures и только применимым соседним phases: preflight, readiness,
  JIT, worker namespace, timer state, submit, readback, release interruption.
  После terminal diagnosis следующий implementation PR объединяет все
  доказанные same-family corrections, доступные до следующей live boundary.
  Правило не применяется к ordinary tasks, первой/второй isolated correction,
  materially new scope/failure family или post-submit same-operation query-only
  reconciliation и не задаёт blanket test suite. Terminal identity,
  one-submit и no-blind-retry rules сохраняются.
- Subagent возвращает один terminal handoff и становится `Done`. Это только
  terminal status technical execution block: main-task outcome отдельно остаётся
  `in_progress`, `awaiting_operation`, `blocked` или `complete` и не выводится
  из subagent `Done`/handoff автоматически. Настоящий gate
  возвращает точный callback с одним требуемым действием и не остаётся
  неопределённо `Active`; pause/blocker является немедленным terminal
  transition, а не причиной держать technical execution task активной.
- После successful internal spawn main curator сохраняет текущий turn активным
  до meaningful callback либо terminal handoff: пока subagent non-terminal,
  main не публикует final, не становится idle и не возвращает управление
  пользователю. Main держит ровно один outstanding event/terminal wait. Quiet
  mode означает отсутствие heartbeat/status-текста, а не completion turn.
- Main/subagent публикуют только meaningful state transitions. Heartbeat-
  сообщения, «ещё идёт» и polling неизменного CI запрещены полностью. Timeout
  tool-level wait разрешает только немедленный silent re-arm того же event
  wait: это renewal lease/subscription, а не progress evidence. На timeout
  запрещены `list_agents`, worktree/Git/CI/status reads и user-facing status.
  После meaningful callback/event wait можно повторить; silent timeout re-arm
  — единственное отдельное исключение. Meaningful blocker/callback либо
  terminal handoff будит main, и он в том же turn публикует owner-facing
  transition: пользователь не должен писать `посмотри` ради уже доставленного
  handoff. Один terminal payload возвращается ровно один раз, без дублирования
  идентичного terminal handoff несколькими каналами, после чего subagent
  становится `Done`.
- Это actor routing применяется к technical execution blocks, начатым после
  merge этой редакции. Уже начатый main-owned read-only turn не прерывается и
  не переклассифицируется задним числом.
- Post-task checklist
  [`docs/architecture/14_codex_task_audit_checklist.md`](docs/architecture/14_codex_task_audit_checklist.md)
  является внутренним read-only инструментом только одного куратора,
  оптимизирующего production protocol и его документацию. Обычные main/domain
  curators и technical execution subagents не читают и не вызывают его как
  execution checklist и не меняют из-за него поведение задачи: их единственный
  operational entrypoint остаётся этот файл плюс релевантные authoritative
  domain docs. Сам checklist не создаёт gate, approval, test, task, PR или
  mutation.
- Пользователь не подтверждает повторно уже выбранную цель, business meaning,
  accepted exact plan или обычные технические решения внутри scope.
- На goal существует одна owner-facing surface. Перед любым owner-facing gate
  обязателен canonical receipt валидатора
  [`apps/codex_authorization_gate.py`](apps/codex_authorization_gate.py) по
  контракту
  [`docs/architecture/15_codex_authorization_router.md`](docs/architecture/15_codex_authorization_router.md).
  Другой curator/subagent только маршрутизирует structured evidence owner-у;
  duplicate pending/answered gate и subset accepted extension не создают второй
  вопрос.

Минимальный task passport main chat: цель; accepted decisions; included и
excluded scope; acceptance; текущий technical execution block и subagent identity;
PR/plan hash/terminal receipt после появления.

## Authorization router и human gates

Внутри системы существует только три outcome:
`AUTO_CONTINUE`, `EVIDENCE_BLOCKED`, `HUMAN_REQUIRED`. Их reason codes закрыты,
receipt и `decision_digest` byte-stable. Storage medium (`SQLite`, file, server,
service, production location) не участвует в gate classification; учитывается
только semantic/final effect. Protected business facts отделены от repo-declared
operational control metadata.

`HUMAN_REQUIRED` допустим только когда exact machine delta доказывает хотя бы
одно из: новый business semantic; новый final target; новый destination; новый
external/publication/financial/security-access effect; credential/login/2FA/
captcha capability; protected-data final delta; irreversible final delta, не
покрытый allowlist. Слова `material`, `risky`, `production DB write`,
`business-data mutation`, `scope expansion` или `irreversible` без exact delta
не являются reason code и не разрешают вопрос. Если эти predicates не доказаны,
owner-facing permission question protocol-invalid.

Missing identity/evidence даёт `EVIDENCE_BLOCKED` и automatic diagnosis/
correction, а не вопрос. Pre-submit same-goal defect, fresh readiness/operation
identity, unrelated/stale warning и allowlisted bounded dependency remediation
дают `AUTO_CONTINUE`; terminal identity повторно не используется. После
`submitted`/`ambiguous` mutation разрешены только same-operation query-only
readback/reconciliation, blind retry запрещён. Required dependency не
обходится: automatic remediation требует exact allowlist, zero undeclared
business/financial/external/publication/security/destination effects и
preservation/readback predicates.

Routine repo/GitHub/tests/merge, existing live deploy и technical remediation
автономны внутри envelope. `approval_policy=never`/полный technical execution —
обычный standing profile, не предлагаемый пользователю режим. Exact CAS,
readiness, target/destination binding, backup/recovery, one-submit, no secrets и
readback/reconciliation остаются независимыми machine guards.

## Consistency живого ресурса

`Live resource` — DB/store, snapshot, queue/outbox, file/manifest/cache,
process-owned state или иной exact ресурс, который может менять timer, service,
cron, HTTP/manual action либо external producer. Правило включается, когда
операция требует consistent boundary для его mutation/copy/rebuild/cutover;
обычная repo/user-artifact работа и query-only observation без такого claim не
получают новых требований.

- Пользователь не выбирает consistency mode. Curator/executor по exact resource
  и producer semantics автоматически выбирает самый дешёвый safe вариант:
  semantic/material revalidation либо rebase под коротким lock для допустимого
  append-only/unrelated drift; selective quiet window только для exact pauseable
  producers на участке `fresh preflight -> one submit -> readback`; online
  snapshot/generation с tail/catch-up и коротким atomic switch для долгой работы;
  immutable exact CAS, когда ресурс обязан не меняться.
- Blanket stop всех cron/timers/services запрещён.
  `business-data-maintenance` — reusable domain mechanism для подходящих exact
  resource scopes, не universal answer. Producer, чьи пропущенные work/events
  нельзя durably replay-ить, не pause-ится ради quietness; continuous observers
  остаются active, а unrelated writes исключаются semantically либо проходят
  revalidation.
- До pause фиксируются exact resource identity, полный classified producer set
  и exact prior desired/actual control state. Unknown/unclassified writer,
  timer, cron, job или FD означает `EVIDENCE_BLOCKED` с automatic diagnosis, а
  не human gate. Pause начинается как можно позже; design/PR/CI, preparation и
  long copy остаются online, когда это безопасно.
- `COMPLETE` после pause требует exact prior-state restore и catch-up proof:
  timer/service health и next trigger, backlog/watermark/freshness, отсутствие
  gaps/loss/duplicates, а также crash/timeout-safe durable recovery и readback.
  Простого enable timer недостаточно. Это правило не заменяет target/destination
  binding, one-submit/no-blind-retry, backup/recovery, readback/reconciliation
  или domain contracts.

Эта revision router-а применяется только к technical blocks, начатым после её
merge. Она не будит, не меняет и не reclassify задним числом `wbc 0008` или
`wbc 0010`.

## Repository и release flow

1. Без explicit stop-line один implementation block использует одну ветку и
   один non-draft PR в `main`; exact stop-line boundary определена выше. Labels
   не выбирают tests, release kind или state.
2. `ci/test_planner.py` строит byte-stable `test-plan.json` исполняемым code
   exact PR base по exact base/head objects, base+head registry union,
   transitive dependencies и changed paths. Head planner активируется только
   после merge; selected groups и plan verifier также запускает exact-base
   harness с cwd exact head. Несовместимая head registry schema требует staged
   migration.
3. Unknown path, registry/workflow/core-framework change или unresolved mapping
   автоматически выбирает full regression. Пользователь и labels не выбирают
   tests.
4. Genuine `pull_request` workflow выполняет fast core, выбранные suites и один
   aggregate check `pr-gate`. Diagnostic dispatch имеет другое имя и не
   удовлетворяет required context. Untrusted PR code не получает secrets.
5. После successful exact-head `pr-gate` trusted-main Release Runner один раз
   проверяет open non-draft same-repo PR в `main`, exact head/base/planner/plan
   hash, immutable base↔head PR Gate workflow, exact successful job set и
   mergeability. Он не запускает tests и не исполняет unmerged PR code.
   Изменение самого trusted `pr-gate.yml` требует отдельного staged/bootstrap
   contour; ordinary planner/registry change проходит normal one-shot flow.
6. Runner делает один expected-head squash merge и exact readback. `repo_only`
   завершается receipt `done`; `live_runtime` deploy-ит exact merge SHA через
   canonical adapter, проверяет deployed SHA и пишет один receipt.
7. Legacy `production_mutation` после merge/deploy только query-only читает
   exact manifest и пишет `awaiting_apply`. Default-off Apply Runner сохраняет
   этот exact-manifest режим. Отдельный `scope-goal` mode принимает durable
   OWNER/MEMBER task passport и exact `live_runtime/done` receipt для bounded
   reversible scope. В task-scoped режиме manifest генерируется JIT на
   canonical host, дважды material-CAS квалифицируется с bounded regeneration
   до первого submit и не требует отдельного user hash confirmation. Mutation
   submit остаётся ровно один; затем выполняются только query-only readback и
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
  readback и reconciliation. Accepted task-scoped reversible goal разрешает
  machine regeneration/qualification manifest внутри exact passport scope;
  manifest остаётся immutable audit/recovery artifact, а не объектом повторного
  human approval. Ad-hoc SQL и local/server-only mutation запрещены.
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
