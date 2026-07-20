## Проверяемый итог

<!-- Коротко: какой результат должен быть подтверждён после closure. -->

## Scope и ограничения

<!-- Укажите bounded scope, запреты и один execution-контур. -->

- [ ] `task:standard`
- [ ] `task:loop` — только вместе с `scope:live-runtime`
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

Для STANDARD метка `release:ready` ставится только после завершения проверок, ровно с одной `task:*` и ровно одной `scope:*` меткой. Draft PR, production mutation и PR без successful `baseline` в автоматический выпуск не допускаются. LOOP root/ready вручную не назначаются: новый root регистрируется `/wb-core loop enqueue-new`, recovery — отдельной `/wb-core loop enqueue-recovery` с exact active gate/root proof. LOOP затем проходит exact-head `release:awaiting-agent` и после deploy остаётся на `release:awaiting-ui` до production UI acceptance.
