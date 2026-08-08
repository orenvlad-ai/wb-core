# Archived Global Codex Orchestration v1

Этот документ — archive pointer. Global Watcher, local orchestration registry,
Task Passport, acceptance envelope, logical release lane, per-task reporting и
persistent arbiter больше не входят в active `wb-core` runtime или release
eligibility.

Содержимое historical contract является только migration history и не является
agent instruction. Его нельзя использовать для запуска
Watcher/registry/lane/shepherd/takeover/arbiter,
создания heartbeat/callback или постановки нового PR в retired admission path.
Retained compatibility code и historical labels сохраняют fail-closed audit и
state-machine behavior, но сами по себе не активируют этот epoch.

Последний полный source contract доступен в Git history на anchor
`e44f548982900e286a2c1a73fdf439d0c8a49843`. Runtime state сохраняется отдельно
как private sealed archive с checksums; raw registry, chat content и credentials
не публикуются в Git.

Current flow определён в `AGENTS.md`,
`docs/architecture/07_codex_execution_protocol.md` и
`docs/architecture/11_github_release_train.md`: один куратор, один прямой
исполнитель, open non-draft STANDARD PR, successful exact-head `baseline`,
`release:ready`, existing mechanical Release Train и manual owner acceptance.
