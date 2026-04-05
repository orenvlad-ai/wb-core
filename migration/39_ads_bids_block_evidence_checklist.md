# Evidence Checklist Блока Ads Bids

## Зачем Нужен Evidence Checklist

Этот checklist нужен, чтобы не переходить дальше без минимально достаточного пакета доказательств для `ads_bids_block`.

## Какие Доказательства Обязательны

Обязательны:
- доказательство корректного `snapshot_date`;
- доказательство корректного состава `nm_id`;
- доказательство корректности `ads_bid_search`;
- доказательство корректности `ads_bid_recommendations`;
- доказательство корректного empty-case;
- доказательство server-side live execution.

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
- comparison по normal-case и empty-case;
- итогового evidence summary;
- artifact-backed smoke;
- authoritative server-side smoke.

## Что Не Входит В Этот Checklist

Не входит:
- test framework;
- performance-checks;
- cutover readiness;
- production observability;
- полнота кампаний beyond bootstrap sample set.
