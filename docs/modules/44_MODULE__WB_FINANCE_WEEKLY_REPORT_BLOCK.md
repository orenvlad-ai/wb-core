# WB Finance Weekly Report Block

## Status

`ACTIVE / HOSTED RUNTIME`

## Purpose and boundaries

`Отчёты -> Финотчёт ВБ` is the canonical weekly P&L read-side for the official Wildberries Finance API. It does not use the deprecated Statistics API and does not expose the Finance token to the browser.

The server-owned chain is `Finance API -> raw rows -> report metadata -> weekly Decimal aggregate -> temporal cost source -> operator table`.

Raw identity is `(seller_id, report_id, rrd_id)`. Reports with different `reportType` remain separate in metadata and are combined only in the weekly aggregate. API money remains decimal text and is never accumulated with binary float.

## Storage

The existing `registry_upload_runtime.sqlite3` owns `wb_finance_weekly_raw_rows`, `wb_finance_weekly_reports`, `wb_finance_weekly_sync`, `wb_finance_weekly_aggregates`, `wb_finance_weekly_reconciliation`, and `wb_finance_weekly_cost_coverage`. Cost coverage also persists a compact quality payload and a dependency hash used to invalidate derived weeks after cost-state or SKU-mapping changes.

Schema creation is idempotent and runs at hosted application startup. Repeated sync updates changed raw rows and recalculates a week without doubling amounts.

## Classification and cost

The versioned classifier is `wb_finance_weekly_classifier_v1`. Net revenue uses `retailPriceWithDisc` for sale and return operations. The official commission control is the signed difference between that revenue and `forPay`; acquiring is disclosed as a component already included in that control total and is not double-counted in total WB expenses. `bonusTypeName` drives exclusive deduction buckets for WB Promotion, transit delivery, subscriptions, paid services, and unknown/other deductions.

Cost selection is per Finance operation, never per whole week:

- operation date resolution is `rrDate -> saleDt -> orderDt`; legacy `week_start` fallback is allowed only for weeks ending before `2026-07-01` and remains recorded in quality diagnostics. A mixed/post-cutover movement without an exact operation date is uncovered with `operation_date_missing`, because its temporal source cannot be selected safely;
- before `2026-07-01`, cost reuses the current `COST_PRICE` dataset, current registry `nm_id -> group_name`, canonical nomenclature aliases and its exact `clear/anti_spy/matte -> Clean/Anti-Spy/Matte` product-type mapping, with `latest effective_from <= operation_date`;
- from `2026-07-01`, cost is the existing `sheet_vitrina_v1_wb_cost_daily_state.our_wb_unit_cost_rub` row for the exact `operation_date + canonical nm_id`;
- therefore `29.06.2026–05.07.2026` is mixed: June operations use `COST_PRICE`, July operations use Our WB Cost daily state;
- after the cutover there is no hidden fallback to `COST_PRICE`, adjacent dates or zero. Missing daily state/cost stays uncovered and keeps COGS, profit and final margin null;
- a present estimated/fallback Our WB unit cost participates in management P&L, while `confirmed_qty`, `estimated_qty`, `fallback_qty`, `confirmed_share_pct`, `source_status` and `component_status_json` remain separate quality evidence. Coverage and confirmation are not conflated.

Weekly confirmation is movement-weighted from the exact daily state's confirmed/estimated/fallback quantity buckets. It is calculated only over post-cutover movements; the mixed week's June `COST_PRICE` units remain visible in the separate source breakdown and are not mislabelled as confirmed Our WB units.

Sales add `quantity * unit cost`; returns subtract it. All Finance money arithmetic remains Decimal.

## Runtime and schedule

- Read route: `GET /v1/sheet-vitrina-v1/wb-finance-report`.
- Backfill/tick CLI: `apps/wb_finance_weekly.py`.
- Timer: `wb-core-wb-finance-weekly.timer`, hourly in `Europe/Moscow`.
- The due policy starts no earlier than Monday `05:00 MSK`, retries preliminary/error state, performs stabilization, and revisits the latest two closed weeks after 24 hours.
- History begins with `29.12.2025–04.01.2026` and ends at the latest fully closed week.
- Finance resync always recalculates its week. Every Our WB Cost rebuild (including an unchanged recovery rebuild after restart) and every nomenclature mutation path compares the stored cost dependency hash and recalculates only changed post-cutover Finance weeks. Finance recalculation does not invoke Our WB Cost rebuild or web-vitrina refresh, so there is no recursive refresh loop.

## Guarded cost recalculation

`python3 apps/wb_finance_weekly.py recalculate-stale-cost ...` is read-only by default. Its bounded plan lists the exact stale weeks, expected COGS/coverage state, raw-row digests, target/non-target digests and an exact `sha256` fingerprint. Apply requires `--apply`, the same fingerprint in `--confirm-fingerprint`, and an explicit `--backup-dir`; before writing, the runner creates an online SQLite backup through the backup API, verifies `PRAGMA integrity_check=ok`, and hashes the backup.

Apply opens one `BEGIN IMMEDIATE` transaction, recomputes the complete plan, rejects fingerprint drift, recalculates every planned week in-place, checks the non-target Finance digest and proves that no stale target remains before commit. Any exception rolls back all planned weeks. A repeated dry-run has `stale_week_count=0`. Ordinary Our WB Cost/nomenclature invalidation uses the same all-or-nothing application method, without calling refresh recursively. No `os.replace`, ad-hoc SQL or partial/force mode is part of this contour.

Targeted verification is `python3 apps/wb_finance_weekly_smoke.py`. Production acceptance additionally checks control week `22.06.2026–28.06.2026`, both report IDs `764583098` and `764583099`, 72,184 raw rows, UI rendering, timer registration, and repeated-sync idempotency.
