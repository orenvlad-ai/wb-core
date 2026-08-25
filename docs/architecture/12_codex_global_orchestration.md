# Archived Global Orchestration

Watcher, external registry, Task Passport runtime, logical lane, shepherd,
takeover, heartbeat, callback и persistent arbiter не являются active flow.
Исторический contract доступен в Git history на anchor
`e44f548982900e286a2c1a73fdf439d0c8a49843` только как audit evidence.

Current lifecycle — main WBC chat плюс один bounded visible internal subagent;
current CI/release — deterministic `pr-gate` и one-shot Release Runner. Они не
создают новый control plane и не восстанавливают historical orchestration.

Implementation subagent создаётся только `collaboration.spawn_agent` и виден в
`Subagents`/`Activity` main task. Sidebar/user-owned thread mechanisms
(`codex_app.create_thread`, `fork_thread`, `handoff_thread`,
`send_message_to_thread`) не являются dispatch fallback. Недоступный internal
spawn даёт exact tooling blocker, а не новый пользовательский task.
