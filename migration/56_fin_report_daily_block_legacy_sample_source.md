# Legacy Source Для Fin Report Daily

## Что Считается Legacy-Source

Для `fin_report_daily_block` legacy-source фиксируется так:
- current official source `GET /api/v5/supplier/reportDetailByPeriod?dateFrom=...&dateTo=...&rrdid=...&period=daily`;
- current RAW normalization semantics из `43_raw_fin_report_daily.js`;
- current APPLY semantics из `78_plugins_fin_report_daily.js`.

Именно эта связка сейчас задаёт downstream смысл `fin_report_daily` в табличном legacy-контуре.

## Почему Выбран Именно Он

Он выбран потому, что:
- отдельный consumer-facing HTTP contract для `fin_report_daily` сейчас не подтверждён;
- raw path уже строит canonical `RAW_FIN_REPORT_DAILY`;
- apply path однозначно фиксирует итоговые финансовые метрики на уровне `snapshot_date + nmId` и отдельно `fin_storage_fee_total`.

## Какая Semantics Зафиксирована

Зафиксировано:
- upstream читается постранично через `rrdid`;
- pagination bounded guardrail:
  - deadline `240000 ms`;
  - `maxPages = 200`;
  - `204` завершает поток;
  - отсутствие роста `rrdid` считается pagination stuck;
- raw path нормализует page rows в агрегированные строки:
  - `snapshot_date`
  - `nmId`
  - `fin_delivery_rub`
  - `fin_storage_fee`
  - `fin_deduction`
  - `fin_commission`
  - `fin_penalty`
  - `fin_additional_payment`
  - `fin_buyout_rub`
  - `fin_commission_wb_portal`
  - `fin_acquiring_fee`
  - `fin_loyalty_rub`
  - `fetched_at`;
- `snapshot_date` извлекается из `rr_dt`, fallback в `sale_dt`;
- `fin_buyout_rub` считается как `sumBuyoutSalesRub - sumBuyoutReturnsRub`;
- `fin_commission_wb_portal` считается как `sumCommSales - sumCommReturns`;
- отдельно добавляется special total row:
  - `snapshot_date`
  - `nmId = 0`
  - `fin_storage_fee = total storage fee`.

## Откуда Берётся Bootstrap `nmId`

В `wb-core` нет live `CONFIG`, поэтому checkpoint использует bootstrap sample set из уже известных проекту SKU:
- `210183919`
- `210184534`

Отдельно сохраняется special case:
- `nmId = 0` для total storage fee.

## Какие Первые Sample Нужны

Первые sample:
- normal-case sample на bootstrap `nmId` set;
- special total-row sample для `nmId = 0`.
