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
update_note: "V3 shares the warehouse temporal cost policy, exposes four reconciled business expense categories, and makes production Partner UI/XLSX acceptance fail closed."
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

The aggregate is rebuildable from raw Finance rows and shares module 44 classifier/profit/cost services. Preview fails stale when formula version, weekly raw `content_hash`, or canonical cost source digest changed. This includes migration-109 `business_approved_archival_estimate` lineage for its exact 18 legacy `nmId`; Partner never resolves that manifest independently. Source correction therefore requires projection rebuild and cannot silently reuse old values.

Per-SKU Finance values include net revenue, canonical COGS, agent remuneration, acquiring, logistics, storage, acceptance, penalties/corrections and other attributable deductions. Agent and acquiring are separate and enter the margin exactly once.

Account-level rows with no resolvable `nmId` are not silently lost or assigned wholesale. The approved rule is:

`selected SKU net revenue / total weekly net revenue`.

The allocated amount and coefficient are disclosed in provenance. Non-positive total revenue is a blocker. A different allocation rule requires a separately approved server-owned contract.

Marketing uses only accepted closed-day `ads_compact/fullstats` snapshots at exact `date + nmId`. The shared resolver accepts valid root or nested `result` envelopes. `kind=empty` means confirmed zero; a missing date, invalid value/envelope or successful payload without the selected `nmId` is a blocker and never zero. Finance marketing is not deducted simultaneously with `ads_sum`.

# 4. Decimal formulas

Formula version is `partner_report_profitability_ui_first_v3`.

For each week:

```text
net_revenue = sales − returns

finance_margin = net_revenue
                 − canonical COGS
                 − agent remuneration
                 − acquiring
                 − logistics
                 − ads_sum
                 − storage
                 − non-capitalized acceptance
                 − penalties/corrections
                 − other attributable expenses

tax = net_revenue × tax_rate
replenishment = MAX(finance_margin, 0) × replenishment_rate
net_profit = finance_margin − office − tax − replenishment
dividends = MAX(net_profit, 0) × investor_share
weekly_annualized_return = dividends × 52 / invested_capital × 100%
```

Negative net profit remains visible; negative dividends are not accrued. For several selected weeks:

`annualized_return = average weekly dividends × 52 / invested capital × 100%`.

Weekly percentages are not summed. Zero capital is a validation error. The UI tooltip explicitly says this is a calculated, not guaranteed, return.

Rows are: net revenue, COGS, agent remuneration, acquiring, logistics, storage, paid acceptance, marketing, penalties/corrections, `Прочие прямые и распределённые расходы`, Finance margin, office, tax, replenishment, net profit, dividends and calculated annualized investor return.

`Прочие прямые и распределённые расходы` includes direct expenses of the selected SKU plus the approved revenue-proportional allocation of account-level expenses. Its accessible tooltip describes the formula but does not expose the numeric allocation coefficient, account revenue or source amount. The main row expands into exactly four indented business categories:

1. `Транзитная логистика, не подтверждённая как капитализированная`;
2. `Подписка WB Jam`;
3. `Платные сервисы WB`;
4. `Прочие удержания`.

Each category combines direct and allocated amounts of that category. Unclassified account-level rows belong to `Прочие удержания`; proven capitalized transit is excluded. Partner-facing UI/XLSX show only category amounts. Direct/allocated values, source category/rule and digests remain internal machine provenance. Decimal largest-remainder reconciliation assigns any display-cent residual deterministically, so the four displayed amounts equal the rounded main row without modifying exact profit values. The main expense is deducted exactly once.

# 5. UI contract and performance

Clicking `Сформировать` immediately shows loading and a visible cancel action. A 30-second AbortController timeout produces a human-readable error. The preview, blockers and source coverage appear directly below settings; blockers are above the table. Available values remain visible in an incomplete preview while missing dependent values stay absent. Excel is enabled only for `status=ready`.

The table has metrics in rows, weeks in columns and `Итого за период`; its first metric column is sticky. At ~390 px, settings/messages/actions remain within the viewport and horizontal scrolling belongs only to the table.

The production-like regression fixture adds 295,919 unrelated raw Finance rows after projections, measures an explicit full JSON-decode baseline, and proves a two-week selected-SKU preview remains an indexed sub-two-second lookup without a synchronous raw scan. The smoke prints both timings; the current local evidence was 172 ms for the synthetic full scan versus 1 ms for indexed preview over 295,923 total raw rows (machine-specific, retained as comparative evidence rather than a production SLA).

# 6. XLSX contract

`POST .../preview.xlsx` receives the selected `nmId`, exact weeks and the visible preview's `expected_source_digest`. Source drift returns a conflict and requires rebuilding the UI preview.

The workbook is generated from the same calculation service as UI. Sheet 1 follows the supplied desktop reference: light/white palette, Arial-like 10 pt text, thin calm gray borders, compact coefficient column, metric labels left, week columns right, total column, frozen `C2`, print area/fit, appropriate widths/heights and blue emphasis for `Дивиденды` and annualized return. The renamed other-expense row is followed by the same four indented categories as the UI, with amounts only and no category percentages/allocation coefficients. Their displayed cents reconcile to the main row. Sheet 2 contains only selected report parameters, date, formula version and source digest.

The file has no macros, external workbook links, hidden sheets or other-SKU values. Its filename binds product/SKU, `nmId` and selected period. UI and XLSX values must match to Decimal rounding. Production acceptance opens and semantically verifies the workbook; file existence/non-zero size alone is never evidence.

ZIP, raw Finance workbook, ads/cost evidence workbooks and package privacy scanner are not part of the active V2 output and have no HTTP routes.

# 7. Verification

- formulas, root/nested ads, stale detection, incomplete states, indexed performance and XLSX: `python3 apps/partner_report_smoke.py`;
- immediate loading, cancel, UI-first table, digest-bound XLSX and desktop/390 px layout: `python3 apps/partner_report_browser_smoke.py`;
- authorization: `python3 apps/registry_upload_http_entrypoint_auth_smoke.py`;
- public route allowlist: `python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py`;
- authenticated production read-only acceptance: hosted `finance-ui-flow`; passed status requires ready preview, empty blockers, visible table, real XLSX download, workbook structure/content checks, UI/XLSX reconciliation, desktop/narrow screenshots and no fatal browser/network errors.

The flow records every attempted preview POST before asserting its outcome. JSON failures retain HTTP status, application code, human-readable error and blockers. A non-JSON proxy/runtime response retains HTTP status plus the bounded `response_not_json` code without copying its HTML body into evidence. Neither failure can produce `status=passed`.

The control fixture uses revenue `476034`, COGS `83837`, agent+acquiring `174797`, ads `30904`, office `10000`, tax `6%`, reserve `20%` and investor share `40%`, yielding Finance margin `186496`, net profit `110634.76` and dividends `44253.904`. Invested capital is an explicit fixture input and is not inferred from the reference screenshot.
