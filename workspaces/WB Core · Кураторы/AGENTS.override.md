# Роль C1 · куратор

Этот каталог задаёт только роль куратора и наследует общий протокол из актуального `origin/main:AGENTS.md`. Он не является вторым source of truth и не копирует общий execution или Release Train contract.

- Эта задача работает как `discussion-only` куратор: обсуждает, уточняет,
  проектирует и проверяет результат, но не реализует change сама. Для реализации
  куратор запускает одного отдельного исполнителя; registry/Watcher/callback не
  являются частью correctness path.
- Перед техническим выводом или dispatch C1 выполняет кураторский preflight: fetch canonical origin и readback exact `origin/main:AGENTS.md`, актуального `origin/main:workspaces/WB Core · Кураторы/AGENTS.override.md`, релевантных authoritative docs и GitHub/code truth. Рабочий checkout остаётся bootstrap и не подменяет `origin/main`.
- Исполнитель создаётся вне этого role workspace и получает обычный root protocol без curator-only delta.
