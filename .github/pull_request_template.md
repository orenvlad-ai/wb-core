## Проверяемый итог

<!-- Коротко: какой результат должен быть подтверждён после closure. -->

## Scope и ограничения

<!-- Укажите bounded scope, запреты и один execution-контур. -->

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

Метка `release:ready` ставится только после завершения проверок и ровно с одной `scope:*` меткой. Draft PR, production mutation и PR без успешного `baseline` в автоматический выпуск не допускаются.
