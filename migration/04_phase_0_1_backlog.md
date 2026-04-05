# Phase 0/1 Backlog

## Backlog

| ID | Задача | Ожидаемый результат | Зависимости | Рекомендуемый порядок |
| --- | --- | --- | --- | --- |
| F-01 | Зафиксировать migration charter | Sidecar migration model описана явно и не подлежит пересмотру в текущей фазе | Review reference-репозиториев | 1 |
| F-02 | Зафиксировать target architecture | Границы server-first, thin-table и modular-monolith описаны и reviewable | F-01 | 2 |
| F-03 | Зафиксировать blueprint репозитория | Понятна ownership-модель для `apps/`, `packages/`, `infra/`, `docs/`, `migration/`, `tests/` | F-02 | 3 |
| F-04 | Зафиксировать source-of-truth policy | Anti-drift rules задокументированы для code/schema/runtime/config/data/table | F-02 | 4 |
| F-05 | Собрать inventory legacy-контрактов | Минимальные непотеряемые контракты перечислены с пометками о степени уверенности | Review reference-репозиториев | 5 |
| F-06 | Зафиксировать parity и cutover rules | Для будущих переносов есть evidence gates и нет big-bang path | F-05 | 6 |
| F-07 | Зафиксировать Codex execution protocol | Работа Codex bounded, reviewable и не расширяет scope молча | F-01, F-04 | 7 |
| F-08 | Зафиксировать open questions и decision log | Известные неизвестные вынесены наружу и не спрятаны в assumptions | F-01..F-07 | 8 |
| F-09 | Оформить ADR-0001 | Migration decision закреплён в коротком долговечном ADR | F-01 | 9 |
| F-10 | Добавить минимальный baseline repo hygiene | Есть минимальный `.gitignore` и пустая базовая структура workspace | Нет | 10 |

## Примечание

Этот backlog исключает:
- business-модули;
- рабочие ingestion/jobs/api реализации;
- миграции БД;
- CI/CD;
- deployment automation;
- работу над новой operator-table.
