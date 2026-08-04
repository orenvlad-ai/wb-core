# WB Core · Кураторы

Канонический primary folder для новых локальных C1-задач проекта `orenvlad-ai/wb-core`.

## Что даёт каталог

- root `AGENTS.md` остаётся единственным общим execution/governance entrypoint;
- ближайший `AGENTS.override.md` добавляет только роль C1;
- `.codex/config.toml` выбирает `gpt-5.6-sol` с reasoning `max` и поднимает bounded instruction limit до 64 KiB, чтобы фактическая root+nested chain не обрывалась до C1-delta;
- отдельные C2 создаются в обычном `wb-core` project/worktree и не наследуют curator-only delta.

Codex Desktop должен сохранить этот каталог как primary folder проекта с точным названием `WB Core · Кураторы`. Readback обязан показать exact path этого каталога; если Desktop нормализует primary cwd к Git root, rollout fail closed и применяется standalone fallback из authoritative contract, а не ручное копирование общего протокола.

## Естественный canary

В новом проекте создаётся локальная C1-задача с короткой пользовательской просьбой без служебного boilerplate:

> Запусти отдельную короткую диагностику: проверь, что новый кураторский контур следует актуальному протоколу и не создаёт второй Watcher.

Canary успешен только если C1 обнаружил root + nested instruction chain, создал и зарегистрировал отдельный read-only C2, выдал один короткий dispatch summary и завершил turn. Затем Global Watcher должен увидеть C2 как новую зарегистрированную задачу; C1 не выполняет polling и просыпается только по exact attention либо новому сообщению пользователя.

## Миграция

Новые задачи переключаются на этот проект только после exact saved-project и canary readbacks. `wb_core_3` остаётся доступным, пока существует хотя бы одна pre-migration C1/C2-задача, текущая initiating curator-задача не принята владельцем либо terminal attention/acceptance ещё не доставлены. Архивация legacy project допустима отдельным действием только после пустого pre-migration task set, целого registry, единственного active Watcher и подтверждённого нового front door.
