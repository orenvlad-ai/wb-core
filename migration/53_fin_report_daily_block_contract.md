# Контракт Блока Fin Report Daily

## Что Это За Блок

`fin_report_daily_block` — bounded migration unit для текущего `fin_report_daily` RAW/APPLY path из табличного legacy-контура.

Для этого блока в legacy не подтверждён отдельный consumer-facing HTTP contract.

Поэтому target block фиксируется относительно:
- official source `GET /api/v5/supplier/reportDetailByPeriod?period=daily`;
- current RAW normalization semantics из `43_raw_fin_report_daily.js`;
- current APPLY semantics из `78_plugins_fin_report_daily.js`.

## Границы Блока

В этот migration unit входит:
- request daily financial snapshot по дате и набору `nmId`;
- semantics `snapshot_date + nmId`;
- item-level финансовые поля:
  - `fin_delivery_rub`
  - `fin_storage_fee`
  - `fin_deduction`
  - `fin_commission`
  - `fin_penalty`
  - `fin_additional_payment`
  - `fin_buyout_rub`
  - `fin_commission_wb_portal`
  - `fin_acquiring_fee`
  - `fin_loyalty_rub`;
- special total row `nmId = 0` для total storage fee;
- paginated flow через `rrdid`;
- time-budget и max-pages semantics как bounded execution guardrail.

В этот migration unit не входит:
- `CONFIG/METRICS/FORMULAS` migration;
- новая таблица;
- jobs/API/production hardening;
- deploy;
- cutover любых других migration units.

## Что Блок Должен Принимать

Блок должен принимать:
- `snapshot_type`;
- `snapshot_date`;
- `nm_ids`;
- `scenario` для controlled checks.

## Что Блок Должен Отдавать

Блок должен отдавать target envelope поверх daily financial snapshot.

Для success:
- `snapshot_date`;
- `count`;
- `items`;
- `storage_total`.

Внутри item:
- `nm_id`;
- `fin_delivery_rub`;
- `fin_storage_fee`;
- `fin_deduction`;
- `fin_commission`;
- `fin_penalty`;
- `fin_additional_payment`;
- `fin_buyout_rub`;
- `fin_commission_wb_portal`;
- `fin_acquiring_fee`;
- `fin_loyalty_rub`.

Внутри `storage_total`:
- `nm_id = 0`;
- `fin_storage_fee_total`.

## Минимальная Parity Surface

Legacy и target сравниваются по:
- `snapshot_date`;
- составу `nmId`;
- всем десяти `fin_*` полям на уровне item;
- special total row `nmId = 0`.

Нельзя потерять:
- pagination cursor semantics через `rrdid`;
- bounded deadline / max-pages guardrail;
- sale/return normalization для `fin_buyout_rub`;
- sale/return normalization для `fin_commission_wb_portal`;
- special total storage fee semantics.

## Required Evidence

Чтобы считать блок корректным, нужны:
- legacy normal-case sample;
- legacy sample для special total row;
- target samples для обоих режимов;
- parity comparison по normal-case и total-row;
- короткий evidence summary;
- artifact-backed smoke;
- authoritative server-side smoke.
