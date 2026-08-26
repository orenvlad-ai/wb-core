# Codex Main Task Workspace v2

Main task автоматически получает `wbc NNNN <русское название>` и pin через
local `wbc-task-intake` skill. Она хранит accepted goal/decisions, короткий
passport, scope, acceptance и final owner-facing result.

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
worktree/PR/mutation; implementation block владеет одной branch и одним PR.
Если owner-facing technical conclusion требует нового evidence из repository/
code, logs, server, database, external API либо длительного ожидания, это
technical execution block. Main напрямую делает только curator-control reads:
fresh protocol/docs для routing, task/subagent/PR/check/receipt status, compact
preflight bounded passport и exact verification terminal handoff. Pure
conceptual answer, clarification/design conversation и conclusion из уже
существующего exact handoff subagent-а не dispatch-ятся. Routing не использует
оценку сложности, времени или числа tool calls. Diagnostic/read-only dispatch
внутри запрошенной цели не требует отдельного human confirmation или gate.

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
