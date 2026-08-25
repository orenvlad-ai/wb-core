# Codex Main Task Workspace v2

Main task автоматически получает `wbc NNNN <русское название>` и pin через
local `wbc-task-intake` skill. Она хранит accepted goal/decisions, короткий
passport, scope, acceptance и final owner-facing result.

Implementation block получает one fresh visible internal subagent
`wbc NNNN SSS <latin transliteration>` без pin. Default concurrency — one.
Same-scope corrections продолжают его; new scope/new PR после terminal state
получает следующий `SSS`. Subagent возвращает один terminal handoff либо exact
gate callback и становится Done.

Workspace не создаёт registry, scheduler, monitor, reviewer, callback service
или release state. User chats не archive/unpin/delete автоматически.
