## Проверяемый итог

<!-- Коротко: какой результат должен быть подтверждён после closure. -->

## Scope и ограничения

<!-- Укажите bounded scope, запреты и один execution-контур. -->

- [ ] `task:standard`
- [ ] `scope:repo-only`
- [ ] `scope:live-runtime`
- [ ] `scope:production-mutation` — автоматический выпуск запрещён до human gate

## Проверки

<!-- Перечислите реально выполненные targeted checks. -->

- [ ] Targeted checks пройдены
- [ ] Полный semantic diff прочитан
- [ ] Findings исправлены и проверки повторены
- [ ] Authoritative docs синхронизированы
- [ ] Secrets, production data и unrelated edits отсутствуют

## Release

Для ordinary PR используется `task:standard` и ровно одна `scope:*` метка.
После successful `baseline` PR получает `release:ready`. Release Train повторно
проверяет open non-draft PR, same-repository head, labels и baseline, затем
сериализует sync, merge и применимый deploy/verify. Технический terminal state
не означает owner acceptance.
