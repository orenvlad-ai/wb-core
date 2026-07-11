# WB Finance Weekly Report Block

## Status

`ACTIVE / HOSTED RUNTIME`

## Purpose and boundaries

`Отчёты -> Финотчёт ВБ` is the canonical weekly P&L read-side for the official Wildberries Finance API. It does not use the deprecated Statistics API and does not expose the Finance token to the browser.

The server-owned chain is `Finance API -> raw rows -> report metadata -> weekly Decimal aggregate -> canonical COST_PRICE coverage -> operator table`.

Raw identity is `(seller_id, report_id, rrd_id)`. Reports with different `reportType` remain separate in metadata and are combined only in the weekly aggregate. API money remains decimal text and is never accumulated with binary float.

## Storage

The existing `registry_upload_runtime.sqlite3` owns `wb_finance_weekly_raw_rows`, `wb_finance_weekly_reports`, `wb_finance_weekly_sync`, `wb_finance_weekly_aggregates`, `wb_finance_weekly_reconciliation`, and `wb_finance_weekly_cost_coverage`.

Schema creation is idempotent and runs at hosted application startup. Repeated sync updates changed raw rows and recalculates a week without doubling amounts.

## Classification and cost

The versioned classifier is `wb_finance_weekly_classifier_v1`. Net revenue uses `retailPriceWithDisc` for sale and return operations. The official commission control is the signed difference between that revenue and `forPay`; acquiring is disclosed as a component already included in that control total and is not double-counted in total WB expenses. `bonusTypeName` drives exclusive deduction buckets for WB Promotion, transit delivery, subscriptions, paid services, and unknown/other deductions.

Cost reuses the current `COST_PRICE` dataset, current registry `nm_id -> group_name`, canonical nomenclature aliases and its exact `clear/anti_spy/matte -> Clean/Anti-Spy/Matte` product-type mapping, and `latest effective_from <= rrDate`. Missing mapping or cost remains a problem SKU; COGS, profit, and final margin are null until coverage is complete.

## Runtime and schedule

- Read route: `GET /v1/sheet-vitrina-v1/wb-finance-report`.
- Backfill/tick CLI: `apps/wb_finance_weekly.py`.
- Timer: `wb-core-wb-finance-weekly.timer`, hourly in `Europe/Moscow`.
- The due policy starts no earlier than Monday `05:00 MSK`, retries preliminary/error state, performs stabilization, and revisits the latest two closed weeks after 24 hours.
- History begins with `29.12.2025–04.01.2026` and ends at the latest fully closed week.

Targeted verification is `python3 apps/wb_finance_weekly_smoke.py`. Production acceptance additionally checks control week `22.06.2026–28.06.2026`, both report IDs `764583098` and `764583099`, 72,184 raw rows, UI rendering, timer registration, and repeated-sync idempotency.
