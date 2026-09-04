# Codex Main Task Workspace v2

## Main workspace

Main task автоматически получает `wbc NNNN <русское название>` и pin через
local `wbc-task-intake` skill. Она хранит accepted goal/decisions, короткий
passport, scope, acceptance и final owner-facing result. Root
[`AGENTS.md`](../../AGENTS.md) остаётся mandatory operational entrypoint;
execution lifecycle, stop-lines, actor routing и correction ownership
канонически определены в
[`07_codex_execution_protocol.md`](07_codex_execution_protocol.md).

Main task — единственная owner-facing surface goal. Пользователь не выбирает
trust tier или approval mode: implementation intent действует до
`COMPLETE`/supersede через canonical authorization envelope. До любого
owner-facing gate нужен valid receipt
[`codex_authorization_gate.py`](../../apps/codex_authorization_gate.py) по
[`15_codex_authorization_router.md`](15_codex_authorization_router.md).

## Visible internal execution

Каждый technical execution block имеет одного visible internal subagent. Каждый
diagnostic package и первый implementation block получают fresh actor через
`collaboration.spawn_agent` с `fork_turns:"none"`. Actor виден в
`Subagents`/`Activity`, не pin-ится и не создаёт sidebar task или
`::created-thread`. Same-scope correction до merge и post-merge same-family
correction реактивируют исходного mutator по execution protocol, не создавая
duplicate actor. Одновременно active допустим максимум один mutating/
implementation subagent и adaptive `0/1/N` minimum-sufficient bounded
diagnostic packages без numeric preference/cap сверх capacity platform. Exact
naming, compact passport, package/implementation boundaries и one-branch/one-PR
ownership задаёт execution protocol, а не workspace UI.

После successful spawn main сохраняет текущий turn активным до meaningful
callback либо terminal handoff и держит ровно один outstanding event/terminal
wait, покрывающий весь active set subagents. Quiet mode не завершает turn.
Meaningful callbacks обрабатываются без потери других handoffs; при оставшемся
active set один wait re-arm-ится на него. Tool timeout разрешает немедленно и
молча re-arm того же wait, но не является progress evidence и не разрешает
heartbeat, duplicate monitor либо status polling. Каждый subagent возвращает
terminal payload ровно один раз и становится `Done`; это terminal state блока,
не automatic acceptance всей main task.

Вся видимая внутренняя работа subagent-а — progress, рабочие пояснения,
сообщения и technical handoff — по умолчанию ведётся на русском. Исключения:
exact code, command, identifier, source quote и задача, явно требующая другого
target language. Правило относится только к видимым сообщениям.

## One owner surface and continuation

Diagnostic package handoff возвращается только owning main task, закрывает его
scoped questions одним stop condition и не передаётся sibling executor-у.
Read-only block не запускает mutating continuation. Optional one-shot frozen
review следует тому же package/handoff path: это не постоянная reviewer role,
не universal gate и не замена tests. После handoff только owning main task
публикует итог, задаёт допустимый business-вопрос и dispatch-ит continuation.
Другой main curator может один раз
передать owner-у structured evidence и exact pointer, но не ведёт параллельный
monitoring или управление той же целью, не пишет исполнителю повторно и не
публикует второй status либо question. Duplicate pending/answered gate и subset
accepted extension обрабатываются router-ом, а не новым owner message.

Meaningful callback или terminal handoff будит owning main для owner-facing
transition в том же turn; пользователь не должен писать `посмотри`. Pause или
blocker является terminal transition блока, а не причиной держать duplicate
executor/monitor active. Новый bounded block и continuation dispatch следуют
execution protocol и не меняют owning owner surface.

## Compact technical handoff

Один terminal handoff содержит обязательное ядро: terminal status/outcome;
фактический effect или явное отсутствие mutation; blocker либо business result;
exact durable receipt/artifact pointers; остаточный risk и next action. Для
repo block сюда также входят exact PR/head/merge/main и применимые plan/check/
release receipt bindings, но только как компактные pointers и result.

Полные raw logs, row/roster/digest lists и другие объёмные evidence остаются в
durable artifacts и не копируются в handoff. Исключение — exact данные, без
которых нельзя понять blocker или доказать terminal result. Это форма одного
сообщения, не новый artifact, checklist или gate.

## Owner-facing result

Main curator не пересылает technical handoff дословно. Он даёт короткий
outcome-first пересказ простым русским языком, понятный нетехническому взрослому:
минимум jargon и identifiers, но сохранены значимые status, blocker, risk и
next action.

Если весь финальный ответ укладывается в 1000 символов с пробелами, отдельный
summary block не нужен. Более подробный финальный ответ начинается с блока с
заголовком `Краткий итог`; body этого блока занимает не более 1000 символов с
пробелами, сам заголовок в лимит не входит. Это authoring discipline: модель
пишет с запасом ниже границы, без отдельного tool call, точного machine
counting, validator, check или gate.

`Краткий итог` содержит только применимые outcome/status, существенный blocker
или risk и required next action/user action, не повторяя последующие детали.
Одна мысль может остаться одним коротким абзацем; несколько независимых мыслей
разделяются короткими абзацами, bullets либо только необходимыми
мини-подзаголовками, а не собираются в один плотный текст. Обязательных пустых
секций и universal template нет. Детали идут ниже только когда добавляют
evidence, calculations или technical pointers.

Правило одинаково применяется к обычному final, plain-Russian пересказу
technical handoff и owner-facing представлению post-task audit/conformance.
Compact technical handoff, machine receipt/report artifact, code, table, raw
evidence и промежуточные commentary/status messages этим лимитом не
ограничиваются: при публикации main curator оборачивает их применимым owner
summary. Один конкретный business question остаётся коротким и не получает
формальный блок ради формы. Остальные технические детали предоставляются по
запросу.

Workspace не создаёт registry, scheduler, callback service, reviewer или
release state. User tasks не archive/unpin/delete автоматически.
