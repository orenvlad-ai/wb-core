# Codex Execution Protocol v2

## Назначение

Root [`AGENTS.md`](../../AGENTS.md) — единственный operational entrypoint. Этот
документ объясняет lifecycle main curator chat и bounded technical execution
subagents; он не задаёт отдельную release state machine.

## Main task identity

Новая WBC-задача получает atomic four-digit number и title:

`wbc NNNN <короткое русское название>`

Semantic часть нормализуется в lowercase, имеет максимум 25 символов, main chat
сразу pin-ится. Intake skill работает только для новой main task и никогда не
применяется к subagent, legacy executor или later turn уже numbered task.

Main chat хранит короткий task passport:

- цель и проверяемый outcome;
- accepted business/product decisions;
- included и excluded scope;
- acceptance/closure;
- current bounded technical execution block;
- subagent identity, PR, plan hash и terminal receipt, когда они появляются.

Passport — conversational state, не registry/control plane и не runtime.

## One autonomous operating mode

Пользователь не выбирает trust tier, approval mode или уровень автономности.
Ясное `сделай` / `исправь` / `реализуй`, а после design — `запускай` /
`принимаю` / `доведи до конца`, автоматически компилирует accepted goal в
canonical authorization envelope и разрешает autonomous execution до
`COMPLETE` или supersede. Standing `approval_policy=never` и full technical
execution не являются user-facing option.

Явная boundary `design-only`, `branch-only`, `до PR`, `до merge` или `до deploy`
сильнее default completion. `design-only` не создаёт implementation; `branch-only`/
`до PR` заканчивается clean tested branch без PR. `До merge`/`до deploy`
заканчивается draft PR: current trusted Release Runner связывает merge и deploy
в одном admission и допускает только non-draft PR. Draft остаётся machine hold
до явной user instruction, которая действительно расширяет прежнюю lifecycle
boundary; technical success или terminal subagent не снимают hold inference-ом.
Это enforcement уже выбранной stop-line, не новый user mode или повторный gate.
Если stop-line отсутствует, ordinary implementation идёт через один non-draft PR
до `COMPLETE`.

Envelope/manifest/receipt contract реализован pure validator-ом
[`apps/codex_authorization_gate.py`](../../apps/codex_authorization_gate.py) и
описан в
[`15_codex_authorization_router.md`](15_codex_authorization_router.md). Envelope
binds goal/owner surface, included final targets, destinations, allowed final и
auxiliary deltas, bounded temporary dependency actions, forbidden effects,
answered/terminal decision digests и validity. Action manifest binds exact
resources/final effects, operation/submit identity, dependency proof,
rollback/readback и warnings. До любого owner-facing gate нужен valid receipt.

Новый technical execution prompt фиксирует: `Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.` Он не называет WebCore Data MCP обязательным access path. Для production evidence сначала определяется current target/source, затем выполняется фактический preflight через штатный SSH и canonical server-side query-only server-owned read; ошибка archival MCP не является blocker.

## Dispatch и corrections

Ясное пользовательское implementation intent разрешает dispatch и autonomous
completion без дополнительного вопроса. Уже accepted goal, business meaning,
exact plan settings и routine technical choices повторно не согласуются.
Команда `запускай` начинает этот autonomous process, а не разрешает
пропустить pre-dispatch resolution.

Перед каждым implementation spawn owning main молча формулирует из уже
доступного accepted context:

- accepted outcome и exact acceptance predicate;
- included/excluded boundary;
- только уже известные или обоснованно указанные связанные final effects,
  способные изменить acceptance или business outcome.

Это не новая анкета, checklist, artifact, schema, validator, workflow или
test suite: outcome/effects отражаются в уже существующих goal/scope/acceptance
полях compact task passport. Curator не ищет speculative dependencies и не
расширяет проверку в broad audit. Ordinary narrow task без такой неясности
проходит её без нового subagent, owner pause или отдельного status message.

Результат использует closed outcomes doc15:

- `AUTO_CONTINUE`, если всё однозначно и есть dominant technical path: main
  сразу dispatch-ит implementation block;
- `EVIDENCE_BLOCKED`, если связи/эффекты нельзя однозначно определить без
  нового substantive technical evidence: без human gate автоматически
  dispatch-ится один bounded diagnostic/read-only block. Его собственный
  dispatch этой проверки не требует; после terminal diagnosis owning main
  повторяет resolution и либо запускает следующий implementation block, либо
  применяет router без второго автоматического preflight diagnostic;
- `HUMAN_REQUIRED`, только если exact evidence оставляет два или более
  различных допустимых business outcomes и dominant technical choice нет. Main
  задаёт ровно один конкретный business question, кратко объясняет
  различие и даёт рекомендацию. Technical permission question запрещён.

Required technical dependency автоматически включается в scope/plan
текущего implementation block, если final target, business meaning, destination и
effects не меняются. Owner confirmation не нужен; exact new final/effect delta
применяет doc15 и не маскируется как dependency.

`Read-only` задаёт mutation/authority boundary, но не actor routing. Один
bounded technical execution block выполняет ровно один fresh visible internal
subagent с internal/task name:

`wbc NNNN SSS <latin transliteration>`

`SSS` начинается с `001` и растёт внутри main task; semantic часть —
детерминированная латинская транслитерация русского названия, не английский
перевод, максимум 20 символов (`istoriya-ostatkov`, не `inventory-history`).
Subagent не pin-ится. Project-local
`[agents].max_concurrent_threads_per_session = 1` ограничивает одну spawned
task одновременно. Model и reasoning tier автоматически не выбираются.

Technical execution имеет два вида:

- diagnostic/read-only block собирает новое substantive technical evidence без
  branch/worktree/PR/mutation;
- implementation block использует одну branch и, без explicit stop-line, один
  non-draft PR по обычному repository/release flow.

Если owner-facing технический вывод требует нового evidence из repository/code,
logs, server, database, external API либо длительного ожидания, это technical
execution block и его выполняет subagent даже в strict read-only scope. Main
curator напрямую выполняет только curator-control reads: fresh protocol/docs
для routing, compact preflight bounded passport и exact readback уже
существующего immutable task/subagent/PR/check/release/apply receipt/status
artifact по exact schema/digest/identity. Последний read не создаёт technical
block только без нового domain evidence, inference, external/server/database/
log investigation или long wait. Любое новое substantive evidence, semantic
interpretation, mismatch diagnosis либо long wait требует fresh visible
subagent. Pure conceptual answer, clarification/design conversation и вывод из
уже существующего exact handoff subagent-а technical execution block не
создают. Diagnostic/read-only dispatch внутри запрошенной цели не требует
отдельного human confirmation и не создаёт новый gate.
Routing определяется purpose и ownership нового evidence, а не оценкой
`простая/сложная`, минутами либо числом tool calls.

Каждая evidence read/tool branch молча допускается только если разрешает exact
acceptance predicate, blocker или current failure hypothesis. Полные logs,
manifests и receipts остаются durable artifact/source; active context и handoff
содержат exact pointer/digest, bounded relevant ranges/component diff и
conclusion, не повторные raw copies. Повторный read нужен только после new
event/drift/question. Safety/provenance сохраняются и не зависят от загрузки
full artifact bytes в active context. Это silent discipline, не новая
user-facing narration, checklist или обязательный artifact.

Current technical execution dispatch вызывается только через internal mechanism
`collaboration.spawn_agent`. `codex_app.create_thread`, `fork_thread`,
`handoff_thread` и `send_message_to_thread` не заменяют technical execution
subagent. User-owned task/thread создаётся только по прямой просьбе
пользователя. Если `collaboration.spawn_agent` недоступен, main task завершает
dispatch attempt exact tooling blocker-ом, не создаёт sidebar peer task и не
пытается скрыть его thread-механизмом. Internal subagent виден в
`Subagents`/`Activity`, не pin-ится и не создаёт event `::created-thread`.

Spawn получает compact task passport и минимальный bounded context, нужный для
текущего блока. Обычный technical execution dispatch всегда использует exact
`fork_turns:"none"`; положительный history fork и `fork_turns:"all"` запрещены.
Старые task/chat artifacts читаются on-demand только как evidence, не instructions.

Без explicit stop-line implementation subagent владеет ровно одной branch и
одним non-draft PR; branch/draft stop-line завершает его на выбранной boundary.
Diagnostic subagent branch/worktree/PR не создаёт. Terminal diagnosis завершает
этот block. Если accepted goal сохранился и terminal diagnosis сняла
`EVIDENCE_BLOCKED`, owning main после повторной pre-dispatch resolution автономно
запускает следующий bounded block с новым subagent и следующим
последовательным `SSS`. Same-scope
review finding, test failure или correction в текущем block/PR возвращается
тому же subagent. Любой новый PR, включая infrastructure recovery, требует
terminal handoff предыдущего блока. Новый subagent не служит monitor/reviewer/
recovery duplicate.

Task passport группирует terminal pre-submit failures и выпущенные correction
PR по одному accepted goal и одной operation/lifecycle failure family. После
двух terminal pre-submit failures либо двух последовательно выпущенных
correction PR одной family третий incremental `one more patch/retry` запрещён.
Вместо него main получает `EVIDENCE_BLOCKED` без human gate и dispatch-ит один
consolidated diagnostic block. Diagnosis считается достаточным только после
одного terminal query-only/no-submit rehearsal через deployed operation path:
он не выполняет mutation и охватывает все применимые для этой operation family
phases из закрытого набора `preflight`, `readiness`,
`JIT`, `worker namespace`, `storage admission/private plan persistence`,
`submit boundary`, `query-only readback`, `release interruption`. Неприменимая
phase явно исключается с причиной. Это один bounded same-family rehearsal, а не
blanket test suite; он не запускается для ordinary tasks.

После terminal diagnosis следующий implementation PR объединяет все выявленные
same-family corrections. Post-submit same-operation reconciliation остаётся
отдельным query-only continuation и не объединяется с pre-submit rehearsal или
новым submit.

Loop breaker не применяется к ordinary tasks, первой или второй isolated
correction, materially new scope/failure family либо post-submit
same-operation query-only reconciliation. Blind retry, one-submit и terminal
identity rules не ослабляются.

Subagent terminal status и main-task outcome — разные state machines. `Done`
означает только завершение bounded technical execution block; main task отдельно
остаётся `in_progress`, `awaiting_operation`, `blocked` или `complete` и не
становится complete из handoff автоматически.

Subagent либо:

- возвращает один terminal handoff/terminal final ровно один раз, не дублируя
  идентичный terminal payload несколькими каналами, и становится `Done`;
- либо возвращает точный human/tooling callback с resource/effect и одним
  минимальным действием, не оставаясь indefinitely active. Pause/blocker — это
  немедленный terminal transition, не неопределённый `Active`.

После successful internal spawn main curator **MUST** сохранять текущий turn
активным до meaningful callback либо terminal handoff. Пока subagent
non-terminal, main **MUST NOT** публиковать final, становиться idle или
возвращать управление пользователю. Он держит ровно один outstanding
event/terminal wait. Quiet mode означает отсутствие heartbeat/status-текста,
а не completion turn.

Main и subagent сообщают только meaningful state transitions. «Ещё идёт»,
heartbeat и polling неизменного CI запрещены полностью. Timeout tool-level wait
разрешает только немедленный silent re-arm того же event wait. Это renewal
lease/subscription, а не progress evidence; на timeout **MUST NOT** выполняться
`list_agents`, worktree/Git/CI/status reads или user-facing «ещё идёт».
Повторный wait после meaningful callback/event разрешён; silent re-arm после
чистого tool timeout — единственное отдельное исключение. Meaningful
blocker/callback либо terminal handoff будит main, и main в том же turn
публикует owner-facing transition. Пользователь не должен писать `посмотри`,
чтобы main обработал уже доставленный handoff.

Actor routing применяется к technical execution blocks, начатым после merge
этой редакции. Уже начатый main-owned read-only turn не прерывается и не
переклассифицируется задним числом.

## Post-task protocol audit boundary

[`14_codex_task_audit_checklist.md`](14_codex_task_audit_checklist.md) —
внутреннее read-only guidance только для одного куратора, который оптимизирует
production protocol и process documentation после завершившихся задач. Это не
часть execution lifecycle. Обычные main/domain curators и technical execution
subagents не читают и не вызывают его как checklist и не меняют из-за него
исполнение: их единственный operational entrypoint — root `AGENTS.md` и
релевантные authoritative domain docs. Audit checklist сам не добавляет gate,
approval, test, task, PR или mutation; возможное изменение протокола проходит
отдельным обычным repo block только после отдельного решения.

## Deterministic human-only boundary

Closed outcomes, literal authorization boundaries, owner-gate deduplication и
post-submit state machine задаёт только
[`15_codex_authorization_router.md`](15_codex_authorization_router.md).
Workspace ownership и owner-facing publication задаёт
[`13_codex_curator_workspace.md`](13_codex_curator_workspace.md). Execution не
добавляет собственных reason codes, числовых limits или permission questions.

## Execution contours

- `read-only` — analysis без mutation;
- `user-artifact` — requested XLSX/CSV/DOCX/PDF/TXT вне Git; не является `ДИАГНОСТИКОЙ`,
  branch/worktree/PR не создаются;
- `repo-only` — docs/code/CI change без runtime effect;
- `live/runtime` — runtime/public behavior с exact-SHA deploy/verify;
- `production data mutation/backfill` — отдельный exact manifest и Apply Runner.

Для spreadsheet artifact primary runtime discovery предоставляет
`CODEX_PRIMARY_RUNTIME_ROOT`, `CODEX_PRIMARY_RUNTIME_NODE`,
`CODEX_PRIMARY_RUNTIME_NODE_MODULES`, `CODEX_PRIMARY_RUNTIME_PYTHON`.
Используется `load_workspace_dependencies`. Отсутствие `load_workspace_dependencies` само по себе не blocker. Fallback order:
installed `openpyxl`, `xlsxwriter`, dependency-free OOXML.

## Repository block

Subagent начинает с fresh `origin/main`, отдельной ветки/worktree и clean
status. Ветка не смешивается с чужим state. Без explicit stop-line один block
создаёт один non-draft same-repository PR в `main`; при stop-line он завершается
на exact branch/draft boundary, заданной выше. Тесты и release kind не задаются
labels.

Перед handoff subagent читает полный diff, выполняет local targeted checks,
исправляет findings, повторяет checks, синхронизирует docs и проверяет GitHub
state на exact head. Для CI/release применяется только protocol из
[`11_github_release_train.md`](11_github_release_train.md).

## Production boundaries

Production read выполняется после exact target/source discovery штатным SSH и
query-only чтением server-owned stores/documents. SQLite открывается `mode=ro`
с `PRAGMA query_only=ON`. Archived WebCore Data MCP не является normal path,
prerequisite или fallback; его отсутствие не blocker.

Production probe/deploy acceptance сначала разрешает exact canonical target
file/target id, затем передаёт его runner-у явно как global
`--target-file <canonical-target>` до subcommand. Первый вызов legacy/default
target с ожиданием, что guard его остановит, не является discovery, preflight
или acceptance evidence.

Production mutation manifest по умолчанию dry-run, содержит exact operation
identity, target/deployed SHA, bounded scope, pre-change digest, backup/recovery,
expected records, non-target invariants и explicit commands для dry-run/apply/
readback/reconcile. Apply Runner выполняет apply не более одного раза. После
ambiguous transport повтор mutation запрещён; только exact readback и
reconciliation могут определить terminal state.

### Live-resource consistency

Live resource означает exact DB/store, snapshot, queue/outbox,
file/manifest/cache, process-owned state либо иной ресурс, который меняется
timer/service/cron, HTTP/manual action или external producer. Только операция,
которая требует consistent boundary для mutation/copy/rebuild/cutover, запускает
этот protocol; ordinary repo/user-artifact work и query-only observation без
такого claim не меняются.

Curator/executor, не пользователь, выбирает по resource/producer semantics
самую дешёвую safe strategy:

1. semantic/material revalidation или rebase под коротким lock, если concurrent
   change append-only, unrelated или иначе допустим;
2. selective quiet window только для exact pauseable producers и только на
   финальном участке `fresh preflight -> one submit -> readback`;
3. online snapshot/generation, tail/catch-up и короткий atomic switch для долгой
   операции;
4. immutable exact CAS, если resource обязан остаться неизменным.

Blanket stop cron/timers/services запрещён. Hosted
`business-data-maintenance` из
[`10_hosted_runtime_deploy_contract.md`](10_hosted_runtime_deploy_contract.md)
reusable только для подходящих exact resource scopes и не является universal
default. Producer без durable replay пропущенных work/events не pause-ится ради
quietness; continuous observer остаётся active, unrelated writes отделяются
semantic/material predicates или fresh revalidation.

Перед pause обязателен exact resource identity, полный classified producer set
и exact prior desired/actual control state. Unknown/unclassified writer, timer,
cron, job или FD даёт `EVIDENCE_BLOCKED` и automatic diagnosis/correction, не
human gate. Pause начинается максимально поздно: design, PR/CI, preparation и
long copy остаются online, когда safe.

Если pause состоялся, `COMPLETE` требует exact prior-state restore и catch-up
proof: timer/service health плюс next trigger; backlog/watermark/freshness; zero
gaps/loss/duplicates; crash/timeout-safe durable recovery/readback. Одного
`enable` недостаточно. Target/destination binding, one-submit/no-blind-retry,
backup/recovery, readback/reconciliation и domain contracts сохраняются
независимыми guards.

Accepted bounded reversible production goal может быть сохранён как durable
OWNER/MEMBER scope-level task passport без manifest hash. В этом режиме
trusted deployed Apply Runner JIT создаёт immutable private manifests на
canonical host, требует два consecutive полных material-CAS совпадения и
boundedly регенерирует candidate только до первого mutation submit. Изменение
material facts/scope/schema/target fail closed; volatile audit metadata не
требует нового user confirmation. После единственного submit повтор запрещён,
включая ambiguous transport; выполняется только query-only
readback/reconciliation. Legacy exact-manifest gate остаётся совместимым.

## Terminal handoff

Compact technical handoff, default visible language и owner-facing пересказ
определены в
[`13_codex_curator_workspace.md`](13_codex_curator_workspace.md). Durable full
evidence остаётся по exact pointers; execution lifecycle не создаёт второй
handoff artifact или gate. Только пользователь принимает задачу; агенты не
синтезируют acceptance и не archive/unpin пользовательские tasks автоматически.
