# Codex Main Task Workspace v2

Main task автоматически получает `wbc NNNN <русское название>` и pin через
local `wbc-task-intake` skill. Она хранит accepted goal/decisions, короткий
passport, scope, acceptance и final owner-facing result.

Implementation block получает one fresh visible internal subagent
`wbc NNNN SSS <latin transliteration>` без pin через
`collaboration.spawn_agent`. Semantic name — deterministic transliteration
русского имени, не English translation, максимум 20 символов. Default
concurrency — one. Он виден в `Subagents`/`Activity`, получает compact passport
вместо полного history fork и не создаёт `::created-thread`. Sidebar thread
tools не являются fallback; их недоступность заканчивается exact tooling
blocker-ом.

Same-scope corrections в одном PR продолжают его. Новый PR, включая recovery,
возможен только после terminal handoff и получает следующий `SSS`. Pause/blocker
терминален, не indefinitely Active. Progress публикуется на meaningful
transitions через event/terminal waits, без повторных «ещё идёт» и частого
polling неизменного CI. Subagent возвращает один terminal handoff либо exact
gate callback и становится Done.

Workspace не создаёт registry, scheduler, monitor, reviewer, callback service
или release state. User chats не archive/unpin/delete автоматически.
