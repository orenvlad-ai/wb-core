# WB Finance Weekly Report Block

## Status

`ACTIVE / HOSTED RUNTIME`

## Purpose and boundaries

`Отчёты -> Финотчёт ВБ` is the canonical weekly P&L read-side for the official Wildberries Finance API. It does not use the deprecated Statistics API and does not expose the Finance token to the browser.

The server-owned chain is `Finance API -> raw rows -> report metadata -> weekly Decimal aggregate -> temporal cost source -> operator table`.

Raw identity is `(seller_id, report_id, rrd_id)`. Reports with different `reportType` remain separate in metadata and are combined only in the weekly aggregate. API money remains decimal text and is never accumulated with binary float.

## Storage

The existing `registry_upload_runtime.sqlite3` owns `wb_finance_weekly_raw_rows`, `wb_finance_weekly_reports`, `wb_finance_weekly_sync`, `wb_finance_weekly_aggregates`, `wb_finance_weekly_reconciliation`, and `wb_finance_weekly_cost_coverage`. Cost coverage also persists a compact quality payload and a dependency hash used to invalidate derived weeks after cost-state or SKU-mapping changes. `wb_finance_retro_cost_map` stores the immutable business-approved May/June projection and its source-row provenance; `wb_finance_projection_audit` stores guarded backfill applications.

Schema creation is idempotent and runs at hosted application startup. Repeated sync updates changed raw rows and recalculates a week without doubling amounts.

## Classification and cost

The versioned classifier is `wb_finance_weekly_classifier_v1`. Net revenue uses `retailPriceWithDisc` for sale and return operations. The official commission control is the signed difference between that revenue and `forPay`; acquiring is disclosed as a component already included in that control total and is not double-counted in total WB expenses. `bonusTypeName` drives exclusive deduction buckets for WB Promotion, transit delivery, subscriptions, paid services, and unknown/other deductions.

Cost selection is per Finance operation, never per whole week. Formula version `wb_finance_cost_temporal_v3` has three boundaries:

- operation date resolution is `rrDate -> saleDt -> orderDt`. A movement in or after the `27.04–03.05` boundary without an exact operation date is uncovered with `operation_date_missing`, because its temporal source cannot be selected safely;
- through `2026-04-30`, the previous `COST_PRICE` method remains unchanged: current registry/canonical nomenclature chooses the group and `latest effective_from <= operation_date` chooses the legacy cost;
- from `2026-05-01` through `2026-06-30`, cost is the immutable `business_approved_retro` row for the same canonical `nmId`. It snapshots exact canonical Our WB Cost on `2026-07-01`, or the first available later daily canonical row for that same `nmId`. Its row stores unit cost, source date/table/full source row, source-row digest, calculation fingerprint, selection method, formula version and approval status. Cross-SKU guessing, mutable current cost, all-SKU average and zero fallback are prohibited;
- from `2026-07-01`, cost is the exact `operation_date + canonical nm_id` row from active `sheet_vitrina_v1_warehouse_wb_daily_cost`; the legacy daily state is usable only before functional activation under module 40/48's existing compatibility boundary;
- therefore `27.04.2026–03.05.2026` is mixed between legacy and retro, while `29.06.2026–05.07.2026` is mixed between retro and exact-date Our WB Cost;
- after the cutover there is no hidden fallback to `COST_PRICE`, adjacent dates or zero. Missing daily state/cost stays uncovered and keeps COGS, profit and final margin null;
- a present estimated/fallback Our WB unit cost participates in management P&L, while `confirmed_qty`, `estimated_qty`, `fallback_qty`, `confirmed_share_pct`, `source_status` and `component_status_json` remain separate quality evidence. Coverage and confirmation are not conflated.

Weekly coverage and confirmation use gross sale/return units, while COGS itself remains signed. Thus an equal sale/return pair may reconcile to zero COGS but cannot cancel a missing-cost blocker. Confirmation is weighted only for exact daily Our WB movements. Legacy and business-approved retro units stay visible in separate source buckets; `confirmed_share_pct=0` on a valid functional historical projection is quality/provenance, not missing cost.

Sales add `quantity * unit cost`; returns subtract it. All Finance money arithmetic remains Decimal.

## Expense and profit semantics

`total_wb_expenses` / `Все удержания/расходы WB` remains the cash-control sum of every WB deduction. `profit_period_expenses` contains only expenses not already capitalized in the selected cost. Canonical Our WB Cost includes applicable paid acceptance and FF/WB transit/inbound layers, so paid acceptance and transit dated from `2026-05-01` remain visible but are removed once from period expenses before profit. Before `2026-05-01` the existing legacy behavior remains unchanged; the implementation does not retroactively claim an unproven `COST_PRICE` composition. Acquiring is informational inside the official commission and is never subtracted separately.

`wb_expenses_without_marketing_pct = (total_wb_expenses - marketing) / net_revenue * 100`. Decimal division returns null for zero or negative net revenue. The original all-expense percentage remains.

The operator table has clean completed-week headers. Actual uncovered cost is shown once as a report-level message and remains in API/DB provenance. Every money expense cell has amount plus compact right-side percentage of weekly net revenue and only a qualitative arrow: higher is pink/red `↑`, lower green `↓`, effectively unchanged neutral/yellow `→`; the first/missing-base week has no arrow. Relative expense rows show only percentage and arrow, never a numeric delta or `п.п.`.

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

## Business-approved retro backfill

`python3 apps/wb_finance_weekly.py business-approved-backfill` is the canonical production-data preflight and apply path. Dry-run is default and bounded by `--date-from/--date-to`; normal production scope starts `2026-04-27` and ends at the latest fully closed week. It emits Finance/cost/ads manifests, union `nmId`, no-`nmId` counts, coverage/blockers, proposed immutable rows, old/expected COGS/profit/margin, source/target/non-target digests, backup/apply/reconciliation plans and an exact fingerprint.

Apply additionally requires exact `--confirm-fingerprint`, explicit `--backup-dir` and human-gate `--approval-reference`. The runner checks free space, creates a coherent SQLite online backup, verifies `integrity_check=ok`, SHA-256 and mode `0600`, then performs one `BEGIN IMMEDIATE` transaction. Source/fingerprint drift, cost gaps or non-target drift roll back everything. The same transaction writes one audit row containing the approval reference. Readback requires zero remaining target weeks/blockers. Repeating the exact already-applied fingerprint over the same scope returns a verified no-op.

Targeted verification includes `apps/wb_finance_weekly_smoke.py`, `apps/wb_finance_weekly_cost_cutover_smoke.py`, `apps/wb_finance_weekly_business_approved_backfill_smoke.py` and `apps/wb_finance_weekly_browser_smoke.py`. The tracked `22.06.2026–28.06.2026` artifact is pre-change control evidence only; post-backfill production values must come from reviewed dry-run/readback rather than being invented in repository fixtures.
## Unified cost cutover

For operations dated on/after `2026-07-01`, Finance/P&L reads `our_wb_unit_cost_rub` from canonical recognized WB daily projection. Paid WB projection is reserved for invested-capital metrics and never substitutes COGS. Late recognized evidence invalidates affected Finance weeks from its factual effective date; pre-cutover weeks stay legacy. Existing quality/coverage gates and Decimal aggregate ratios remain mandatory.
