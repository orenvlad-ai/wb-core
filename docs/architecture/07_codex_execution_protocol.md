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

Implementation subagent молча применяет `RED_FIRST` только когда accepted exact
behavior однозначен, есть deterministic/hermetic focused reproducer и stable
local test seam, а test-first не зависит от live production, нестабильного
external API или исследовательского UI. Это routine technical decision, не
user-facing mode, вопрос, artifact или gate. Docs/config/cosmetic/no-behavior
changes и все остальные задачи идут обычным путём без TDD overhead.

При `RED_FIRST` subagent добавляет минимальный failing assertion/test,
подтверждает target-causal red, делает минимальный fix, получает green, затем
выполняет уже действующие proportional targeted/final checks. Отдельный commit
или durable red-proof не нужен; достаточно одной compact строки в existing
technical handoff, если proof нужен для понимания результата. `RED_FIRST` не
применяется к incident/live/migration/external/UI work, пока behavior неизвестен:
сначала идут diagnosis/containment и accepted behavior; regression добавляется
позже на ближайшем реально появившемся stable seam и не задерживает recovery
ради искусственного Red.

Результат использует closed outcomes doc15:

- `AUTO_CONTINUE`, если всё однозначно и есть dominant technical path: `0`
  diagnostic packages, main сразу dispatch-ит implementation block;
- `EVIDENCE_BLOCKED`, если связи/эффекты нельзя однозначно определить без
  нового substantive technical evidence: без human gate автоматически
  dispatch-ятся `1/N` minimum-sufficient bounded diagnostic packages для
  одного ближайшего decision transition. Их количество определяется available
  evidence и dependency graph без fixed/default count. Собственный dispatch
  этих packages не требует ещё одной такой проверки; после terminal diagnoses
  owning main повторяет resolution и либо запускает следующий implementation
  block, либо применяет router без второго automatic preflight diagnostic по
  уже закрытым questions;
- `HUMAN_REQUIRED`, только если exact evidence оставляет два или более
  различных допустимых business outcomes и dominant technical choice нет. Main
  задаёт ровно один конкретный business question, кратко объясняет
  различие и даёт рекомендацию. Technical permission question запрещён.

Итого diagnostic dispatch adaptive `0/1/N`: число packages определяется только
available evidence и dependency graph, без numeric preference или default.

Required technical dependency автоматически включается в scope/plan
текущего implementation block, если final target, business meaning, destination и
effects не меняются. Owner confirmation не нужен; exact new final/effect delta
применяет doc15 и не маскируется как dependency.

`Read-only` задаёт mutation/authority boundary, но не actor routing. Каждый
bounded technical execution block имеет ровно одного visible internal subagent.
Каждый diagnostic package, первый implementation block и materially new
outcome/target/destination/effect получают fresh actor; post-merge same-family
correction использует описанную ниже реактивацию прежнего mutator.
Непосредственно перед обычным
`collaboration.spawn_agent` owning main
атомарно резервирует следующий `SSS` в уже существующем task passport и
передаёт машинный `task_name` exact вида:

`wbc_NNNN_SSS_<translit_slug>`

Исполнимый invariant поля:
`wbc_[0-9]{4}_[0-9]{3}_[a-z0-9_]{1,20}`. `NNNN` — номер owning main task;
`SSS` начинается с `001` и последовательно растёт внутри неё, в том числе при
параллельном dispatch независимых read-only blocks. `<translit_slug>` —
детерминированная транслитерация короткого русского названия блока, не
английский перевод; допускаются только lowercase `a-z`, digits и underscore,
максимум 20 символов. Поэтому `prod_gap_map` и `recovery_architecture` не
являются каноническими slug даже при формальной совместимости с alphabet.

Например, `wbc_0028_001_karta_propuskov` соответствует syntax invariant.
Машинный `task_name` не заменяет русский compact passport и user-facing
семантику блока. Дополнительный registry, роль либо human gate не создаётся.
Subagent не pin-ится. Ровно один actor на block — ownership boundary, а не общий
concurrency cap main task. Model и reasoning tier автоматически не выбираются.

Внутри одной owning main task одновременно может быть active не более одного
mutating/implementation subagent: к нему относится любой actor, способный
менять files/code/runtime/data/external state либо создавать branch/worktree/PR.
Параллельно могут быть active zero-or-more независимых bounded diagnostic/
read-only subagents. WBC не задаёт им произвольный numeric limit; фактическая
конкурентность ограничивается только capacity текущей platform.

Technical execution имеет два вида:

- diagnostic/read-only block собирает новое substantive technical evidence без
  branch/worktree/PR/mutation;
- implementation block использует одну branch и, без explicit stop-line, один
  non-draft PR по обычному repository/release flow.

Bounded diagnostic package — minimum-sufficient набор связанных evidence
questions, который разрешает ровно один ближайший decision transition. Несколько
questions входят в один package только когда у них общие или совместимые
sources, authority, immutable/snapshot boundary и один stop condition. Package
не расширяется до broad audit, дальних решений или speculative dependencies.
Он не выполняет mutation, не создаёт branch/worktree/PR, не становится monitor/
recovery duplicate и не запускает implementation либо другого mutating actor.

Dispatch adaptive и dependency-aware: `0` packages при достаточном evidence,
`1` для одного cohesive gap, `N` для действительно независимых либо
несовместимых packages; это notation, не numeric preference. Package `B`
откладывается до `A` только когда `B` зависит от ответа `A`, результат `A` с
существенной вероятностью invalidates `B` и создаст material waste либо им
нужны несовместимые live snapshot/quiet boundaries. Дешёвый независимый `B`
может идти параллельно. Active packages не дублируют один question или один
другой package. Если question касается resource, который меняет active
implementation executor, diagnostic читает только immutable/exact snapshot
boundary либо ждёт stable boundary; conclusion из дрейфующего state запрещён.

Optional one-shot frozen review exact candidate/diff/plan разрешён как ordinary
diagnostic/read-only package только для конкретной unresolved uncertainty на
ближайшем transition. Это не новая роль, approval или universal gate, не замена
tests и не источник mutation. Постоянный reviewer/monitor и package, который
дублирует tests либо уже собранное evidence, запрещены.

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
текущего блока. Обычный new-actor technical execution dispatch всегда
использует exact `fork_turns:"none"`; положительный history fork и
`fork_turns:"all"` запрещены. Реактивация того же mutator для same-scope/
same-family correction не является новым actor dispatch и не меняет его history.
Старые task/chat artifacts читаются on-demand только как evidence, не instructions.

Без explicit stop-line implementation subagent владеет ровно одной branch и
одним non-draft PR; branch/draft stop-line завершает его на выбранной boundary.
Diagnostic subagent branch/worktree/PR не создаёт и возвращает handoff только
owning main, не sibling executor-у и не другой owner surface. Terminal diagnosis
завершает этот block. Если accepted goal сохранился и terminal diagnosis сняла
`EVIDENCE_BLOCKED`, owning main после повторной pre-dispatch resolution автономно
запускает следующий bounded block. До merge same-scope review finding, test
failure или correction всегда возвращается/реактивирует исходного mutating
subagent в том же implementation block и PR. Post-merge same-family correction
реактивирует того же mutator в новом последовательном correction block и новом
PR, только после terminal предыдущего block. Новый mutator допускается лишь для
materially new outcome, target, destination или effect либо при доказанной
необратимой недоступности прежнего. Optional frozen review не владеет
correction. Новый subagent не служит monitor/reviewer/recovery duplicate.

Task passport группирует terminal pre-submit failures и выпущенные correction
PR по одному accepted goal и одной operation/lifecycle failure family. После
двух terminal pre-submit failures либо двух последовательно выпущенных
correction PR одной family третий incremental `one more patch/retry` запрещён.
Этот family counter монотонно включает всю immutable history accepted goal и
failure family. Новый PR, subagent, operation nonce, rebase, rename или
diagnostic не сбрасывает его и не переносит attempt в новую family. Materially
new failure family получает собственный counter, не стирая прежний.

Вместо третьего narrow retry main получает `EVIDENCE_BLOCKED` без human gate и
dispatch-ит один consolidated diagnostic package. Он собирает одно
same-family explanation и выполняет все доступные и применимые local, staging и
no-submit production-shaped checks в рамках совместимых authority/snapshot
boundaries. Production-shaped часть остаётся terminal query-only/no-submit
rehearsal через deployed operation path и охватывает применимые phases из
закрытого набора `preflight`, `readiness`, `JIT`, `worker namespace`, `storage
admission/private plan persistence`, `submit boundary`, `query-only readback`,
`release interruption`; неприменимая или недоступная phase явно исключается с
причиной. Это bounded same-family package, а не blanket test suite, и он не
запускается для ordinary tasks.

После terminal diagnosis следующий implementation PR объединяет все выявленные
same-family corrections. Post-submit same-operation reconciliation остаётся
отдельным query-only continuation и не объединяется с pre-submit rehearsal или
новым submit.

Loop breaker не применяется к ordinary tasks, первой или второй isolated
correction, materially new scope/failure family либо post-submit
same-operation query-only reconciliation. Blind retry, one-submit и terminal
identity rules не ослабляются.

### Critical-path invalidation

Отменённая или invalidated strategy/dependency помечается в существующем task
passport как `superseded`/`inactive`. Её immutable history, receipts и findings
сохраняются; она исключается только из active critical path и не исчезает из
failure-family evidence или counter.

До первого submit owning block выполняет safe stop и exact readback применимых
temporary effects, после чего может выбрать новый active path. После
`submitted`, `ambiguous` либо любого partial external effect path нельзя просто
отменить, заменить actor/nonce или объявить superseded: сначала выполняются
query-only readback, reconciliation и terminalization exact same operation.
Только затем unresolved continuation может получить новый active path по
обычному router contract. Эта норма не создаёт registry, artifact, role,
workflow или human gate.

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
активным до meaningful callback либо terminal handoff. Пока хотя бы один
subagent active, main **MUST NOT** публиковать final, становиться idle или
возвращать управление пользователю. Он держит ровно один outstanding event/
terminal wait, покрывающий весь текущий active set subagents. Meaningful
callback одного block обрабатывается без потери handoff остальных; если после
него active set не пуст, один wait re-arm-ится уже на актуальный set. Quiet mode
означает отсутствие heartbeat/status-текста, а не completion turn.

Main и subagents сообщают только meaningful state transitions. «Ещё идёт»,
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

## Post-task conformance and audit boundary

Owner phrase `«Сверка»` invokes the main-only read-only post-task pipeline from
[`16_codex_protocol_conformance_pipeline.md`](16_codex_protocol_conformance_pipeline.md).
It emits only protocol-conformance facts; the separate analysis/decision layer
remains
[`14_codex_task_audit_checklist.md`](14_codex_task_audit_checklist.md). Neither
document is part of execution lifecycle or ordinary-task overhead. Ordinary
main/domain curators and technical execution subagents do not read or apply
either document as an execution checklist. Any resulting change remains a
separately authorized repository block.

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
