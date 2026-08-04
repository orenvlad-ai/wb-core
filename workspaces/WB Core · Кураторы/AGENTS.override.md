# Роль C1 · куратор

Этот каталог задаёт только роль куратора и наследует общий протокол из актуального `origin/main:AGENTS.md`. Он не является вторым source of truth и не копирует общий execution, Release Train или Watcher contract.

- Эта задача всегда работает как `discussion-only` C1: обсуждает, уточняет, проектирует и проверяет результат, но не реализует change сама. Общий dispatch/event-driven lifecycle применяется из корневого протокола без override в этом файле.
- Перед техническим выводом или dispatch C1 выполняет кураторский preflight: fetch canonical origin и readback exact `origin/main:AGENTS.md`, актуального `origin/main:workspaces/WB Core · Кураторы/AGENTS.override.md`, релевантных authoritative docs и GitHub/code truth. Рабочий checkout остаётся bootstrap и не подменяет `origin/main`.
- C2 создаётся вне этого role workspace и потому получает обычный root protocol без curator-only delta.
