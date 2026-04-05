# Evidence Checklist Блока Stocks

## Зачем Нужен Evidence Checklist

Этот checklist нужен, чтобы не переходить дальше без минимально достаточного пакета доказательств для `stocks_block`.

## Какие Доказательства Обязательны

Обязательны:
- доказательство корректного `snapshot_date`;
- доказательство корректного состава `nm_id`;
- доказательство корректности `stock_total`;
- доказательство корректности региональных `stock_*`;
- доказательство корректного coverage guard;
- доказательство server-side live execution.

## Какие Артефакты Должны Быть Собраны

Должны быть собраны:
- legacy normal-case sample;
- legacy partial-case sample;
- target normal-case sample;
- target partial-case sample;
- normal-case comparison;
- partial-case comparison;
- короткий evidence summary.

## Что Считается Минимальным Пакетом Evidence

Дальше идти нельзя без:
- обоих legacy samples;
- обоих target samples;
- comparison по normal-case и partial-case;
- итогового evidence summary;
- artifact-backed smoke;
- authoritative server-side smoke.

## Что Не Входит В Этот Checklist

Не входит:
- перенос full cursor/staging runtime;
- test framework;
- production observability;
- полнота каталога beyond bootstrap sample set.
