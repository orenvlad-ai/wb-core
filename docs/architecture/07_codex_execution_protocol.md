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

Новый technical execution prompt фиксирует: `Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.` Он не называет WebCore Data MCP обязательным access path. Для production evidence сначала определяется current target/source, затем выполняется фактический preflight через штатный SSH и canonical server-side query-only server-owned read; ошибка archival MCP не является blocker.

## Dispatch и corrections

Ясное пользовательское `реализуй`, `исправь` или `сделай` разрешает dispatch
без дополнительного вопроса. Если сначала шёл design, достаточно `запускай`
или `принимаю`. Уже accepted goal, business meaning, exact plan settings и
routine technical choices повторно не согласуются.

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

## Human-only boundary

Human decision требуется только для нового business meaning, material scope
expansion, non-interactive-unavailable login/2FA/captcha, непредавторизованного
security/access/ruleset/new destination change либо proven irreversible action
/ production-data scope без accepted durable task-scoped authorization.

Routine branch/PR/tests/check remediation/merge, existing canonical live deploy
и reversible technical fixes внутри accepted scope автономны. Exact plan
preauthorizes перечисленные settings changes. Platform/tool limitation — это
exact blocker, а не причина запрашивать approvals для каждой команды.

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
