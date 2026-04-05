# Legacy Source Для Sales Funnel History

## Что Считается Legacy-Source

Для `sales_funnel_history_block` legacy-source фиксируется так:
- current official-source path `POST /api/analytics/v3/sales-funnel/products/history`;
- current RAW normalization semantics из `30_raw_sales_funnel.js`;
- current APPLY semantics из `71_plugins_sales_funnel.js`.

## Какая Semantics Зафиксирована

Зафиксировано:
- upstream вызывается batch-ами по `nmIds` и selectedPeriod;
- raw path пишет строки `fetched_at | date | nmId | metric | value`;
- apply берёт latest `fetched_at` per `(date,nmId,metric)`;
- apply делит на `100` только:
  - `addToCartConversion`
  - `cartToOrderConversion`
  - `buyoutPercent`.

## Откуда Берётся Bootstrap `nmId`

В checkpoint используется bootstrap sample set из уже известных проекту SKU:
- `210183919`
- `210184534`

## Честный Empty-Case

Исторический endpoint для неизвестного `nmId` возвращает item c пустым `history`, а не transport error.

Это считается естественным empty-case для checkpoint.
