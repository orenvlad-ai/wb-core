# Evidence Checklist Блока Seller Funnel Snapshot

## Зачем Нужен Evidence Checklist

Этот checklist нужен, чтобы отсечь переход к следующему этапу без минимально достаточных доказательств.

## Какие Доказательства Обязательны

Обязательны:
- доказательство корректного `date`;
- доказательство корректного `count`;
- доказательство корректного состава `items`;
- доказательство корректности `nm_id`, `name`, `vendor_code`;
- доказательство корректности `view_count`, `open_card_count`, `ctr`;
- доказательство стабильности payload shape;
- доказательство корректного режима `not_found`.

## Какие Артефакты Должны Быть Собраны

Должны быть собраны:
- legacy normal-case sample;
- legacy `not_found` sample;
- target normal-case sample;
- target `not_found` sample;
- normal-case comparison;
- `not_found` comparison;
- короткий evidence summary.

## Что Считается Минимальным Пакетом Evidence

Дальше идти нельзя без:
- обоих legacy samples;
- обоих target samples;
- comparison по normal-case;
- comparison по `not_found`;
- итогового evidence summary.

## Что Не Входит В Этот Checklist

Не входит:
- test framework;
- performance-checks;
- hardening;
- cutover readiness;
- production observability.

