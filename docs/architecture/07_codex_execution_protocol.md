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
- implementation block использует одну branch и один non-draft PR по обычному
  repository/release flow.

Если owner-facing технический вывод требует нового evidence из repository/code,
logs, server, database, external API либо длительного ожидания, это technical
execution block и его выполняет subagent даже в strict read-only scope. Main
curator напрямую выполняет только curator-control reads: fresh protocol/docs
для routing, task/subagent/PR/check/receipt status, compact preflight для
bounded passport и exact verification terminal handoff. Эти исключения
замкнуты и не разрешают main собирать substantive domain evidence. Pure
conceptual answer, clarification/design conversation и вывод из уже
существующего exact handoff subagent-а technical execution block не создают.
Diagnostic/read-only dispatch внутри запрошенной цели не требует отдельного
human confirmation и не создаёт новый gate.
Routing определяется purpose и ownership нового evidence, а не оценкой
`простая/сложная`, минутами либо числом tool calls.

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

Implementation subagent владеет ровно одной branch и одним non-draft PR;
diagnostic subagent branch/worktree/PR не создаёт. Terminal diagnosis завершает
этот block. Если после неё отдельно разрешена implementation, это следующий
bounded block с новым subagent и следующим последовательным `SSS`. Same-scope
review finding, test failure или correction в текущем block/PR возвращается
тому же subagent. Любой новый PR, включая infrastructure recovery, требует
terminal handoff предыдущего блока. Новый subagent не служит monitor/reviewer/
recovery duplicate.

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

Каждый proposed interruption обязан получить ровно один outcome:
`AUTO_CONTINUE`, `EVIDENCE_BLOCKED` или `HUMAN_REQUIRED`, с closed reason codes
и byte-stable `decision_digest`/receipt. Gate классифицируется по semantic/final
effect, не по storage medium: SQLite/file/server/service/production location
сами по себе gate не создают. Protected business fact — отдельная semantic
категория; repo-declared operational control metadata не становится business
данными из-за места хранения.

`HUMAN_REQUIRED` возможен только при exact machine delta для одного из closed
predicates: новый business semantic, final target, destination, external/
publication/financial/security-access effect, credential/login/2FA/captcha
capability, protected-data final delta или irreversible final delta вне
allowlist. Субъективные `material`, `risky`, `scope expansion`, generic
`business-data mutation`, `production DB write` или одно слово `irreversible`
не разрешают question. Без доказанного predicate permission question является
protocol-invalid; dominant technical recommendation выполняется автономно,
если не нужна уникальная business preference пользователя.

Missing identity/evidence означает `EVIDENCE_BLOCKED` и automatic diagnosis/
correction. Same-goal pre-submit code/runtime defect, fresh sequential identity,
unrelated/stale warning и exact allowlisted dependency remediation означают
`AUTO_CONTINUE`. Terminal identities не переиспользуются. После
`submitted`/`ambiguous` разрешён только same-operation query-only readback и
reconciliation; blind retry запрещён. Required dependency нельзя bypass-ить:
temporary remediation или auxiliary final transition automatic только при
exact allowlist, bounded identity, zero undeclared business/finance/external/
publication/security/destination effects и preservation/readback predicates.

Goal имеет одну owner-facing surface. Non-owner route-ит structured evidence
owner-у; duplicate pending/answered gate suppress-ится, accepted extension и её
subset повторно не спрашиваются. Human capability gate остаётся exact.
Platform/tool limitation — exact blocker, не запрос покомандных approvals.

Этот router contract применяется только к technical blocks, начатым после
merge его редакции. Он не меняет и не переклассифицирует задним числом
`wbc 0008` или `wbc 0010`.

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
status. Ветка не смешивается с чужим state. Один block создаёт один non-draft
same-repository PR в `main`; тесты и release kind не задаются labels.

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

Handoff фиксирует status; included/excluded result; PR/head/merge/main SHA;
plan hash и checks; release receipt; exact deployed/apply state; task/subagent
identity; risks/blockers. Main chat использует его для owner-facing summary.
Только пользователь принимает задачу; агенты не синтезируют acceptance и не
archive/unpin пользовательские tasks автоматически.
