# Codex Main Task Workspace v2

Main task автоматически получает `wbc NNNN <русское название>` и pin через
local `wbc-task-intake` skill. Она хранит accepted goal/decisions, короткий
passport, scope, acceptance и final owner-facing result.

Это единственная owner-facing surface goal. Пользователь не выбирает trust
tier или approval mode: implementation intent автоматически действует до
`COMPLETE`/supersede через canonical authorization envelope. Любой curator или
subagent до owner-facing gate обязан получить valid receipt
[`codex_authorization_gate.py`](../../apps/codex_authorization_gate.py) по
[`15_codex_authorization_router.md`](15_codex_authorization_router.md). Non-owner
surface маршрутизирует structured evidence сюда, а не публикует второй вопрос;
duplicate pending/answered gate и subset accepted extension suppress-ятся.

Explicit `design-only`/`branch-only`/`до PR`/`до merge`/`до deploy` stop-line
имеет приоритет над default completion. Она даёт соответственно no
implementation, clean tested branch без PR либо draft PR machine hold для обеих
последних границ, поскольку current Release Runner требует non-draft и связывает
merge/deploy. Hold снимает только явная user instruction, действительно
расширяющая boundary; technical success и terminal subagent этого не делают.

Каждый technical execution block получает one fresh visible internal subagent
`wbc NNNN SSS <latin transliteration>` без pin через
`collaboration.spawn_agent`. Semantic name — deterministic transliteration
русского имени, не English translation, максимум 20 символов. Default
concurrency — one. Он виден в `Subagents`/`Activity`, получает compact passport
вместо полного history fork и не создаёт `::created-thread`. Sidebar thread
tools не являются fallback; их недоступность заканчивается exact tooling
blocker-ом.

`Read-only` ограничивает mutation/authority, но не выбирает actor. Diagnostic/
read-only block собирает новое substantive technical evidence без branch/
worktree/PR/mutation; implementation block владеет одной branch и, без explicit
stop-line, одним non-draft PR.
Если owner-facing technical conclusion требует нового evidence из repository/
code, logs, server, database, external API либо длительного ожидания, это
technical execution block. Main напрямую делает только curator-control reads:
fresh protocol/docs для routing, compact preflight bounded passport и exact
schema/digest/identity readback уже существующего immutable task/subagent/PR/
check/release/apply receipt/status artifact. Этот read не dispatch-ится только
без нового domain evidence, inference, external/server/database/log
investigation или long wait; semantic interpretation, mismatch diagnosis,
substantive evidence и long wait требуют fresh subagent. Pure conceptual answer,
clarification/design conversation и conclusion из уже существующего exact
handoff subagent-а не dispatch-ятся. Routing не использует оценку сложности,
времени или числа tool calls. Diagnostic/read-only dispatch внутри запрошенной
цели не требует отдельного human confirmation или gate.

Evidence read/tool branch молча выполняется только для exact acceptance
predicate, blocker или current failure hypothesis. Active context и terminal
handoff ссылаются на durable full logs/manifests/receipts через exact pointer/
digest, bounded relevant ranges/component diff и conclusion, не дублируют raw
artifacts; reread разрешён после new event/drift/question. Это не добавляет
owner narration, checklist или обязательный artifact и не ослабляет provenance.

Каждый block использует compact passport, exact `fork_turns:"none"` и следующий
последовательный `SSS`. Diagnostic block заканчивается terminal diagnosis; если
после неё отдельно разрешена implementation, это следующий bounded block с
новым subagent. Правило применяется к blocks, начатым после merge этой редакции;
уже начатый main-owned read-only turn не прерывается и не переклассифицируется.

Same-scope corrections в текущем block/PR продолжают его. Новый PR, включая
recovery, возможен только после terminal handoff и получает следующий `SSS`. Pause/blocker
терминален, не indefinitely Active. После successful spawn main сохраняет
текущий turn активным до meaningful callback/terminal handoff, держит ровно
один outstanding event/terminal wait и не публикует final, не становится idle
и не возвращает управление пользователю, пока subagent non-terminal. Quiet
mode не завершает turn. Progress публикуется только на meaningful transitions:
tool timeout разрешает немедленно и молча re-arm тот же wait, но не является
progress evidence и не разрешает `list_agents`, worktree/Git/CI/status reads
или heartbeat. После meaningful callback/event wait можно повторить. Callback
или terminal handoff будит main для owner-facing transition в том же turn, без
пользовательского `посмотри`. Subagent возвращает terminal payload ровно один
раз без межканального дублирования и становится Done.

Task passport считает terminal pre-submit failures и выпущенные correction PR
одной family только при том же accepted goal и той же operation/lifecycle
failure family. После второго terminal pre-submit failure либо второго
последовательного correction release этой family третий incremental patch/retry
заменяется на automatic `EVIDENCE_BLOCKED` consolidated diagnostic block без
human gate. Он строит bounded production-shaped matrix по доказанным failures и
применимым соседним phases из execution protocol; следующий implementation PR
после terminal diagnosis консолидирует все доказанные same-family corrections
до следующей live boundary. Ordinary tasks, first/second isolated correction,
materially new family/scope и post-submit same-operation query-only
reconciliation не получают matrix; one-submit/no-blind-retry/terminal identity
guards сохраняются.

Workspace не создаёт discretionary permission questions. Proposed interruption
имеет только `AUTO_CONTINUE`, `EVIDENCE_BLOCKED` или `HUMAN_REQUIRED` с closed
reason codes. SQLite/file/server/service location не является gate; точный
semantic/final effect является. Missing evidence запускает diagnosis/correction,
pre-submit same-goal correction продолжает fresh identity, а submitted/
ambiguous operation допускает только same-operation query-only reconciliation.
`HUMAN_REQUIRED` возможен лишь для exact новых business semantic/final target/
destination/external-publication/financial/security-access/credential/protected
data/неallowlisted irreversible deltas. Слова `material`, `risky`, generic
`scope expansion` или `production DB write` gate не доказывают.

Router действует только для blocks, начатых после merge его редакции, и не
переклассифицирует существующие `wbc 0008`/`wbc 0010`.

Когда exact live resource меняют background producers, curator/executor сам
выбирает cheapest safe consistency strategy из root `AGENTS.md` и
[`07_codex_execution_protocol.md`](07_codex_execution_protocol.md); user mode,
questionnaire или gate не создаются. Ordinary repo/read-only work без consistent
boundary claim не меняется. Blanket producer stop запрещён, unknown writer/
timer/cron/job/FD даёт `EVIDENCE_BLOCKED` с automatic diagnosis, а любой
selective pause начинается late и не считается завершённым без exact
prior-state restore и catch-up/readback proof.

Post-task
[`14_codex_task_audit_checklist.md`](14_codex_task_audit_checklist.md) доступен
только одному куратору production protocol/documentation как внутреннее
read-only guidance. Обычные main/domain curators и technical execution subagents не
читают и не вызывают его как execution checklist и не меняют из-за него
поведение; для них остаются только root `AGENTS.md` и релевантные authoritative
domain docs. Checklist не создаёт workspace actor, gate, approval, test, task,
PR или mutation.

Workspace не создаёт registry, scheduler, monitor, reviewer, callback service
или release state. User chats не archive/unpin/delete автоматически.
