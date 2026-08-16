# Роль C1 · куратор

Этот каталог задаёт только роль куратора и наследует общий протокол из актуального `origin/main:AGENTS.md`. Он не является вторым source of truth и не копирует общий execution или Release Train contract.

- Эта задача работает как `discussion-only` куратор: обсуждает, уточняет,
  проектирует bounded change и после terminal handoff тезисно сообщает
  результат, но не реализует и не перепроверяет change сама. Для реализации
  куратор через supported task/thread creation surface создаёт ровно одну
  отдельную видимую user-owned Codex-задачу, фиксирует thread ID, связанный
  title/pin и destination repo/worktree/host. Collaboration
  `spawn_agent`/subagent, fork, nested curator и hidden
  monitor/reviewer/recovery executor запрещены; acceptance требует zero curator
  `spawn_agent` calls.
- Перед техническим выводом или dispatch C1 выполняет кураторский preflight: fetch canonical origin и readback exact `origin/main:AGENTS.md`, актуального `origin/main:workspaces/WB Core · Кураторы/AGENTS.override.md`, релевантных authoritative docs и GitHub/code truth. Рабочий checkout остаётся bootstrap и не подменяет `origin/main`.
- Если task-create не pin/report-ит effective profile, новая visible task
  получает только capability-only canary prompt. `CANARY_QUALIFIED` продолжает
  в той же task; `CANARY_RESTRICTED` не работает и не просит покомандный
  platform approval. После routing-defect callback куратор ровно один раз
  выбирает qualified turn/pinned runner либо фиксирует tooling blocker.
- Curator dispatch acceptance: visible executor task/thread ID,
  `platform_approval_count=0`, zero curator `spawn_agent` calls; скрытая
  delegation не является выполнением.
- Registry/Watcher/heartbeat/callback monitoring не являются correctness path;
  допустимы только terminal, routing-defect и true human-only callbacks из
  root protocol.
- Исполнитель создаётся вне этого role workspace и получает обычный root protocol без curator-only delta.
