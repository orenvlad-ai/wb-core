# WB Core · Кураторы

Необязательный локальный front door для простого flow `wb-core`.

- root `AGENTS.md` остаётся единственным execution/governance entrypoint;
- куратор уточняет цель и запускает отдельного исполнителя в чистом worktree;
- исполнитель проходит branch/PR, exact-head GitHub Release Train и передаёт
  короткий отчёт;
- владелец принимает результат вручную.

Этот каталог не создаёт registry, Task Passport, Watcher, heartbeat, lane owner
или callback в существующий chat. Его metadata и история чатов не являются
source of truth. Исторический workspace contract доступен по anchor
`e44f548982900e286a2c1a73fdf439d0c8a49843`.
