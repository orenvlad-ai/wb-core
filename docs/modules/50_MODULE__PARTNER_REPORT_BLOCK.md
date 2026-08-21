---
title: "Модуль: Partner Report"
doc_id: "WB-CORE-MODULE-50-PARTNER-REPORT-BLOCK"
doc_type: "module"
status: "active_ui_first_xlsx"
purpose: "Server-owned UI-first отчёт доходности одной карточки с indexed Finance projection и соответствующим XLSX."
scope: "One canonical nmId, versioned settings, selected weeks, Finance per-SKU aggregate, ads_compact, canonical COGS, UI preview and XLSX."
source_basis:
  - "docs/modules/09_MODULE__ADS_COMPACT_BLOCK.md"
  - "docs/modules/40_MODULE__OUR_WB_COST_MODEL_BLOCK.md"
  - "docs/modules/44_MODULE__WB_FINANCE_WEEKLY_REPORT_BLOCK.md"
  - "docs/modules/48_MODULE__WAREHOUSE_STOCKS_BLOCK.md"
  - "migration/108_finance_canonical_cost_partner_ui_recovery.md"
  - "migration/110_finance_partner_temporal_v3.md"
  - "migration/111_partner_marketing_diagnostic_ads_recovery.md"
  - "migration/112_partner_marketing_single_count.md"
  - "migration/113_ads_historical_source_completion.md"
  - "migration/114_ads_no_statistics_envelope.md"
  - "migration/115_ads_upstream_shape_evidence.md"
  - "migration/116_ads_http_200_null_sentinel.md"
  - "migration/152_fbs_handoff_cost_and_overhead_backfill.md"
related_modules:
  - "packages/application/partner_report.py"
  - "packages/application/wb_finance_weekly.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
related_tables:
  - "partner_report_settings_versions"
  - "partner_report_settings_current"
  - "partner_report_audit"
  - "wb_finance_weekly_sku_aggregates"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/partner-report/options"
  - "POST /v1/sheet-vitrina-v1/partner-report/settings"
  - "POST /v1/sheet-vitrina-v1/partner-report/preview"
  - "POST /v1/sheet-vitrina-v1/partner-report/preview.xlsx"
source_of_truth_level: "module_canonical"
update_note: "Partner retains full sales while uncovered sales are explicit and excluded from every profit numerator and profitability denominator; coverage remains reason-coded and channel/location-aware."
---

# 1. Purpose and active surface

`Отчёты → Партнёрский отчёт` builds `Отчёт о доходности карточки` for exactly one canonical `nmId`. The primary result is the on-screen table. Excel is a secondary export of that same displayed calculation.

The active surface has no partner role/public cabinet, finalized payout write, evidence ZIP, raw Finance XLSX or permanent public download. Existing historical finalized tables/rows from an earlier revision may remain untouched for audit compatibility, but no current HTTP route creates or packages them.

All routes use the existing server-side `reports` authorization boundary. Browser/localStorage is not parameter or permission truth.

# 2. Server-owned settings

Required saved parameters are:

- one card / `nmId`;
- investor share of positive net profit, %;
- invested capital, ₽;
- replenishment reserve, %;
- weekly office expense, ₽;
- tax rate, %;
- approved common-expense rule.

There are no hidden business defaults. UI examples are placeholders only. Every changed settings save creates an immutable version with author, time and fingerprint; unchanged retry is idempotent. Invested capital must be positive, rates must be `0..100`, and office expense must be non-negative.

The week picker supports all/none/latest four/manual checkboxes. Preview may use any unique selected weeks. Current scope does not create a payout/finalization business record.

# 3. Indexed sources and stale detection

Partner preview never scans all `wb_finance_weekly_raw_rows`. It performs indexed lookups of `wb_finance_weekly_sku_aggregates` for the selected `seller + week + nmId`, plus the `__account__` projection for approved common-expense allocation.

The Partner runtime opens the logical `operational` store through the shared
storage registry. Before split cutover this resolves to the canonical monolith;
after any separately gated atomic manifest switch it must resolve the exact
operational generation and matching schema identity. Partner never opens the
logical raw store in its ordinary preview/XLSX path.

The aggregate is rebuildable from raw Finance rows and shares module 44
classifier/profit/cost services. Preview fails stale when formula version,
weekly raw `content_hash`, or canonical channel/location cost source digest
changed. FBS cost comes only from exact facility/order handoff evidence;
WB/FBO keep daily warehouse WAC, including migration-109 lineage for its exact
18 legacy `nmId`. Partner never resolves either source independently. Source
correction therefore requires projection rebuild and cannot silently reuse old
values.

Per-SKU Finance values include net revenue, canonical COGS, agent remuneration, acquiring, logistics, storage, acceptance, penalties/corrections, review points and other attributable deductions. Agent and acquiring are separate and enter the margin exactly once.

Account-level rows with no resolvable `nmId` are not silently lost or assigned wholesale. The approved rule is:

`selected SKU net revenue / total weekly net revenue`.

The allocated amount and coefficient are disclosed in provenance. Account-level agent remuneration, acquiring, logistics, storage, non-capitalized acceptance and penalties/corrections are routed to the existing Partner main rows. Non-capitalized transit, WB Jam, paid services, review points and genuinely other deductions are routed to named subrows. A catch-all difference from `profit_period_expenses` is forbidden: the explicit categories must reconcile after Finance marketing and positive adjustments are excluded, otherwise preview blocks. Non-positive total revenue is a blocker. A different allocation rule requires a separately approved server-owned contract.

Marketing uses only accepted closed-day `ads_compact/fullstats` snapshots at exact `date + nmId`. The shared resolver accepts valid root or nested `result` envelopes. `kind=empty` means confirmed zero; a missing date, invalid value/envelope or successful payload without the selected `nmId` is a blocker and never zero. Direct and account-level Finance marketing remain visible in Finance but contribute exactly zero to every Partner expense row and margin because the Partner `Маркетинг WB` row already deducts `ads_sum`.

Historical recovery schema `ads_historical_recovery_v4` uses the official campaign manifest plus `adv/v3/fullstats` only. A status-7 campaign whose official `changeTime` predates the exact recovery scope is recorded and excluded as completed before scope. Every other campaign supported by `fullstats` (statuses 7/9/11) is requested in bounded windows and batches. If a batch response omits an ID, recovery must confirm that campaign with an exact singleton request: only a complete singleton response, WB's exact structured `there are no statistics for this advertising period` payload, or the production-observed exact `HTTP 200 + application/json + JSON null` singleton sentinel is accepted. The `null` sentinel is recognized only inside the `fullstats` source method; it is not a general empty-value rule. The source request manifest records the exact confirmation-signal kind. Complete `fullstats` response digests conserve every JSON value but canonicalize the API's semantically unordered arrays recursively, so an order-only replay cannot invalidate an otherwise identical reviewed fingerprint. Apply consumes the exact reviewed plan over stdin, recomputes its content fingerprint, creates the coherent backup, and then verifies the locked target and non-target digests inside the transaction before any writes; it does not replace the approved plan with a new volatile upstream replay. Empty lists, generic boundary `None`, malformed/error responses, unsupported campaigns overlapping scope and unconfirmed omissions remain blockers. An unexpected mapping is reduced to type, digest, bounded keys and allowlisted `status/origin/detail/title`; raw payload and request IDs are never copied into evidence. This is source completion, not a synthetic-zero path; only a fully reconciled global day may be persisted as `kind=empty`.

# 4. Decimal formulas

Formula version is `partner_report_profitability_ui_first_v4` and schema version is `partner_report_v4`. The inherited Finance aggregate retains full `net_revenue`, but exposes `covered_net_revenue`, `sales_without_cost_rub`, `orders_without_cost`, optional `units_without_cost`, `profit_coverage_status` and reason evidence. Uncovered sales enter neither the margin/profit numerator nor the profitability-revenue denominator; a 500-ruble uncovered sale can never become 500 rubles of profit.

For each week:

```text
net_revenue = sales − returns
profit_revenue = covered sales − covered returns

finance_margin = profit_revenue
                 − canonical COGS
                 − agent remuneration
                 − acquiring
                 − logistics
                 − ads_sum
                 − storage
                 − non-capitalized acceptance
                 − penalties/corrections
                 − other attributable expenses

tax = profit_revenue × tax_rate
replenishment = MAX(finance_margin, 0) × replenishment_rate
net_profit = finance_margin − office − tax − replenishment
dividends = MAX(net_profit, 0) × investor_share
weekly_annualized_return = dividends × 52 / invested_capital × 100%
```

Negative net profit remains visible; negative dividends are not accrued. For several selected weeks:

`annualized_return = average weekly dividends × 52 / invested capital × 100%`.

Weekly percentages are not summed. Zero capital is a validation error. The UI tooltip explicitly says this is a calculated, not guaranteed, return.

Rows are: full net revenue, `Продажи без себестоимости, ₽`, `Заказы без себестоимости, шт.`, evidence-backed `Единицы без себестоимости, шт.`, COGS, agent remuneration, acquiring, logistics, storage, paid acceptance, marketing, penalties/corrections, `Прочие прямые и распределённые расходы`, Finance margin, office, tax, replenishment, net profit, dividends and calculated annualized investor return. Partial coverage remains a valid truthful preview with missing dependent cells absent; it is not converted to zero or rejected as if the complete sales fact were missing.

`Прочие прямые и распределённые расходы` includes direct expenses of the selected SKU plus the approved revenue-proportional allocation of account-level subrow expenses. Its accessible tooltip describes the formula and single-count marketing boundary but does not expose the numeric allocation coefficient, account revenue or source amount. The possible indented business categories are:

1. `Транзитная логистика, не подтверждённая как капитализированная`;
2. `Подписка WB Jam`;
3. `Платные сервисы WB`;
4. `Баллы за отзывы`;
5. `Прочие удержания`.

Each category combines its signed direct and allocated amounts. Proven capitalized transit and all Finance marketing are excluded. `Прочие удержания` contains only operations that still classify as `other_deductions`; it is not a balancing residual. A category whose exact total is zero for all selected weeks is absent from both UI and XLSX, so a marketing-only period does not render a zero `Прочие удержания` row. Direct/allocated values, source category/rule and digests remain internal machine provenance. Decimal largest-remainder reconciliation assigns any display-cent residual deterministically, so displayed categories equal the rounded main row without modifying exact profit values. The main expense is deducted exactly once.

At internal Decimal working precision, every explicit main/subrow account category is multiplied by the same revenue ratio. Their combined amount must reconcile at canonical 0.0001-ruble precision with `profit_period_expenses − positive_adjustments − marketing`; no category is derived as an opaque remainder. Display-cent reconciliation remains a separate deterministic step.

# 5. UI contract and performance

Clicking `Сформировать` immediately shows loading and a visible cancel action. A 30-second AbortController timeout produces a human-readable error. The preview, blockers and source coverage appear directly below settings; blockers are above the table. Available values remain visible in an incomplete preview while missing dependent values stay absent. Excel is enabled only for `status=ready`.

The table has metrics in rows, weeks in columns and `Итого за период`; its first metric column is sticky. At ~390 px, settings/messages/actions remain within the viewport and horizontal scrolling belongs only to the table.

The production-like regression fixture adds 295,919 unrelated raw Finance rows after projections, measures an explicit full JSON-decode baseline, and proves a two-week selected-SKU preview remains an indexed sub-two-second lookup without a synchronous raw scan. The smoke prints both timings; the values are machine-specific comparative evidence rather than a production SLA.

Production incident reconciliation does not add a raw scan to preview. The separate repo-owned `partner-finance-diagnostic` action resolves the current complete server-owned setting (or an explicit exact `nmId`/week scope) and reads raw Finance rows only in a bounded read-only transaction. Raw JSON is decoded as an ordered per-week stream; the diagnostic retains only aggregates, bounded examples, at most 10,000 operation groups, at most 10,000 marketing-name candidates and at most 10,000 anomalous stored/raw identity keys. Invalid raw JSON is reduced to an exact count plus bounded examples and one blocker. Exceeding any accumulator bound fails closed. A second streaming duplicate pass runs only when identity mismatches exist. Weeks lacking a projection still contribute their raw count, digest, invalid-JSON and identity evidence. This prevents production history from being materialized in process memory while preserving incomplete-source evidence. Evidence groups every material component by WB operation fields, `nmId` presence, deduction sign, Finance classifier and direct/allocated path, with signed/system/allocated sums, exact semantic-category totals and bounded `reportId`/`rrdId` examples. It also proves ads coverage, direct/account marketing, duplicates, classifier candidates and the former `abs(negative deduction)` uplift. Diagnostic output is external mode-`0600` evidence and cannot change preview, settings, Finance projections or source snapshots.

# 6. XLSX contract

`POST .../preview.xlsx` receives the selected `nmId`, exact weeks and the visible preview's `expected_source_digest`. Source drift returns a conflict and requires rebuilding the UI preview.

The workbook is generated from the same calculation service as UI. Sheet 1 follows the supplied desktop reference: light/white palette, Arial-like 10 pt text, thin calm gray borders, compact coefficient column, metric labels left, week columns right, total column, frozen `C2`, print area/fit, appropriate widths/heights and blue emphasis for `Дивиденды` and annualized return. The renamed other-expense row is followed by exactly the same ordered non-zero categories as the UI, with amounts only and no category percentages/allocation coefficients. Their displayed cents reconcile to the main row. Sheet 2 contains only selected report parameters, date, formula version and source digest.

The file has no macros, external workbook links, hidden sheets or other-SKU values. Its filename binds product/SKU, `nmId` and selected period. UI and XLSX values must match to Decimal rounding. Production acceptance opens and semantically verifies the workbook; file existence/non-zero size alone is never evidence.

ZIP, raw Finance workbook, ads/cost evidence workbooks and package privacy scanner are not part of the active V2 output and have no HTTP routes.

# 7. Verification

- formulas, root/nested ads, stale detection, incomplete states, indexed performance and XLSX: `python3 apps/partner_report_smoke.py`;
- immediate loading, cancel, UI-first table, digest-bound XLSX and desktop/390 px layout: `python3 apps/partner_report_browser_smoke.py`;
- authorization: `python3 apps/registry_upload_http_entrypoint_auth_smoke.py`;
- public route allowlist: `python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py`;
- authenticated production read-only acceptance: hosted `finance-ui-flow`; passed status requires ready preview, empty blockers, visible table, real XLSX download, workbook structure/content checks, UI/XLSX reconciliation, desktop/narrow screenshots and no fatal browser/network errors.
- production raw-operation reconciliation: hosted `partner-finance-diagnostic --output <external-0600-json>`.
- production-scale diagnostic memory contract: `python3 apps/partner_finance_production_diagnostic_scale_smoke.py` (300,013 streamed rows, fail-closed group/candidate pressure, 12,000 invalid-JSON rows reduced to bounded evidence, bounded peak RSS, SQLite unchanged).

The flow records every attempted preview POST before asserting its outcome. JSON failures retain HTTP status, application code, human-readable error and blockers. A non-JSON proxy/runtime response retains HTTP status plus the bounded `response_not_json` code without copying its HTML body into evidence. Neither failure can produce `status=passed`.

The control fixture uses revenue `476034`, COGS `83837`, agent+acquiring `174797`, ads `30904`, office `10000`, tax `6%`, reserve `20%` and investor share `40%`, yielding Finance margin `186496`, net profit `110634.76` and dividends `44253.904`. Invested capital is an explicit fixture input and is not inferred from the reference screenshot.
