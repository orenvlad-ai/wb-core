# Evidence Checklist Блока Prices Snapshot

## Зачем Нужен Evidence Checklist

Этот checklist нужен, чтобы не переходить дальше без минимально достаточного пакета доказательств для prices snapshot block.

## Какие Доказательства Обязательны

Обязательны:
- доказательство корректного `snapshot_date`;
- доказательство корректного состава `nm_id`;
- доказательство корректности `price_seller`;
- доказательство корректности `price_seller_discounted`;
- доказательство стабильности payload shape;
- доказательство корректного empty-case.

## Какие Артефакты Должны Быть Собраны

Должны быть собраны:
- legacy normal-case sample;
- legacy empty-case sample;
- target normal-case sample;
- target empty-case sample;
- normal-case comparison;
- empty-case comparison;
- короткий evidence summary.

## Что Считается Минимальным Пакетом Evidence

Дальше идти нельзя без:
- обоих legacy samples;
- обоих target samples;
- comparison по normal-case;
- comparison по empty-case;
- итогового evidence summary.

## Что Не Входит В Этот Checklist

Не входит:
- test framework;
- performance-checks;
- cutover readiness;
- production observability;
- полнота каталога beyond bootstrap sample set.
