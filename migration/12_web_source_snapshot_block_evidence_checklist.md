# Evidence Checklist Блока Web-Source Snapshot

## Зачем Нужен Evidence Checklist

Этот checklist нужен, чтобы отсечь запуск следующего этапа без минимально достаточных доказательств.

Он отсекает:
- переход к реализации без проверяемого reference;
- размытые claims о parity без артефактов;
- скрытую semantic drift.

## Какие Доказательства Обязательны

Обязательны:
- доказательство корректного `snapshot period`;
- доказательство корректного состава `items`;
- доказательство корректной работы с `nm_id`;
- доказательство корректности числовых полей;
- доказательство стабильности payload shape;
- доказательство корректного режима `not found`;
- доказательство пригодности результата для downstream.

## Какие Артефакты Должны Быть Собраны

Должны быть собраны:
- legacy sample payloads как reference;
- target sample payloads в той же semantic зоне;
- old/new comparison по обязательным полям;
- shape-check по top-level и item-level структуре;
- сравнение минимум по нескольким датам или сценариям;
- явные примеры `not found`;
- краткая фиксация downstream assumptions.

## Что Считается Минимальным Пакетом Evidence

Дальше идти нельзя без:
- хотя бы одного зафиксированного reference sample;
- хотя бы одного target sample;
- field-by-field comparison по обязательным полям;
- отдельной проверки `not found`;
- явной фиксации допустимых и недопустимых расхождений.

Достаточным минимумом считается:
- набор sample payloads, покрывающий normal case и `not found`;
- сравнение по `snapshot period`, `nm_id`, `items`, числовым полям и payload shape;
- подтверждение, что downstream contract по смыслу не сломан.

## Что Не Входит В Этот Checklist

Не входит:
- browser/runtime implementation;
- API/jobs implementation;
- performance-проверки;
- hardening;
- cutover readiness;
- parity downstream business-логики;
- полная production observability.

## Следующий Шаг После Checklist

Следующий шаг:
- создать в `wb-core` короткий reference-fixtures plan для `web-source snapshot block`, где будет перечислено какие именно sample payloads и failure-case примеры нужно собрать первыми.
