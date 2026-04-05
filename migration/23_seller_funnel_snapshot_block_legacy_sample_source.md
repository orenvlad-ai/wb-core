# Legacy Source Для Seller Funnel Snapshot

## Что Считается Legacy-Source

Для первых reference samples legacy-source фиксируется так:
- текущий consumer-facing daily contract `GET /v1/sales-funnel/daily`.

На уровне repository evidence этот контракт уже упомянут в contract inventory:
- `migration/05_contract_inventory.md`

## Почему Выбран Именно Он

Он выбран потому, что:
- это уже downstream-facing snapshot contract;
- он задаёт payload shape, daily semantics и режим `404/not found`;
- новый блок должен воспроизвести именно эту границу, а не внутренний producer path.

## Почему Не Берём Другие Источники На Первом Шаге

На первом шаге не берём:
- внутренний producer/runtime path, потому что он не нужен для consumer-facing parity;
- любые предположения о способе формирования snapshot-а, потому что это не current source of truth для переноса.

## Какие Первые Два Sample Нужны

Первые два sample:
- normal-case sample для `GET /v1/sales-funnel/daily`;
- `not_found` sample для `GET /v1/sales-funnel/daily?date=1900-01-01`.
