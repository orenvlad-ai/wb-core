# Evidence Checklist Блока Sf Period

## Зачем Нужен Evidence Checklist

Этот checklist нужен, чтобы не переходить дальше без минимально достаточного пакета доказательств для `sf_period_block`.

## Какие Доказательства Обязательны

Обязательны:
- доказательство корректного `snapshot_date`;
- доказательство корректного состава `nm_id`;
- доказательство корректности `localizationPercent`;
- доказательство корректности `feedbackRating`;
- доказательство стабильности payload shape для success-case;
- доказательство server-side live execution.

## Какие Артефакты Должны Быть Собраны

Должны быть собраны:
- legacy normal-case sample;
- target normal-case sample;
- normal-case comparison;
- короткий evidence summary.

## Что Считается Минимальным Пакетом Evidence

Дальше идти нельзя без:
- legacy normal-case sample;
- target normal-case sample;
- comparison по normal-case;
- итогового evidence summary;
- artifact-backed smoke;
- authoritative server-side smoke.

## Что Не Входит В Этот Checklist

Не входит:
- test framework;
- performance-checks;
- cutover readiness;
- production observability;
- полнота каталога beyond bootstrap sample set;
- synthetic empty/not-found, если upstream не даёт честного domain-level sample.
