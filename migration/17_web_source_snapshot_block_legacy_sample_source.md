# Legacy Source Для Первых Reference Samples

## Что Считается Legacy-Source

Для первых реальных reference samples legacy-source фиксируется так:
- текущий server-side snapshot contract, который потребляет table-side consumer через `GET /v1/search-analytics/snapshot`.

На уровне repository evidence этот контракт зафиксирован в:
- `wb-table-audit/apps-script/src/44_raw_search_analytics_snapshot.js`

## Почему Выбран Именно Он

Он выбран потому, что:
- это уже используемый downstream contract;
- он задаёт payload shape, period semantics и режим `404/not found`;
- он находится на границе, которую и должен воспроизвести новый блок.

Для первого шага нам нужен не весь acquisition path, а стабильный contract-level reference.

## Почему Не Берём Другие Источники На Первом Шаге

На первом шаге не берём как основной legacy-source:
- `wb-web-bot/bot/fetch_report.py`, потому что это browser-capture path, а не downstream reference contract;
- `wb-ai-research/wb-ai/web_sources/client.py`, потому что это client/acquisition path, а не зафиксированный consumer-facing snapshot contract;
- любые предположения о внутреннем producer runtime, потому что в foundation уже отмечено, что authoritative current producer path пока не доказан полностью.

Сначала фиксируется то, что реально должен сохранить новый блок на выходе.

## Какие Первые Два Sample Нужны

Первые два sample:
- normal-case sample для `GET /v1/search-analytics/snapshot`;
- `not found` sample для того же snapshot contract.

Этого достаточно, чтобы первым шагом зафиксировать:
- нормальный payload shape;
- period semantics;
- item-level semantics по `nm_id` и числовым полям;
- отдельный non-fatal режим `not found`.
