# Migration 110 — unified temporal WB cost and Partner V3

## Scope

This read-side/code migration makes the warehouse-domain `Себестоимость WB наша`
resolver the only active cost-policy implementation for Vitrina, Proxy 3, Finance
and Partner Report.

- dates before `2026-07-01` use the exact canonical value of the same `nmId` on
  `2026-07-01` as an explicit retrospective management projection;
- `2026-07-01` and later use exact daily canonical values;
- pre-boundary Proxy 3 uses historical order/ads operands with the calculation
  parameter version effective on `2026-07-01` and never substitutes Proxy 2;
- missing/non-positive/forbidden-quality values remain blockers, not zero or
  another SKU/average/legacy fallback.

Partner formula/schema V3 renames the active expense line to
`Прочие прямые и распределённые расходы` and reconciles four displayed
categories to the main line at Decimal cent precision. Partner-facing UI/XLSX
show category totals only; direct/allocated composition, allocation rule and
source digests remain machine evidence.

Production Partner acceptance is fail closed: a ready preview, empty blockers,
visible table, actual XLSX download, semantic workbook verification and UI/XLSX
reconciliation are all mandatory. No Partner settings are changed by the flow.

Finance per-SKU aggregate contract advances to
`wb_finance_weekly_sku_aggregate_v3`; both the aggregate version and every
stored canonical-cost dependency version are checked before Partner preview,
so an unchanged source-row digest cannot keep an older formula projection ready.

## Data and release boundary

This migration does not edit immutable Finance rows, ads, warehouse business
documents or raw snapshots. The formula/source digest changes invalidate
derived Finance/Partner projections. Any production rebuild therefore uses only
the repo-owned canonical dry-run/apply/readback contract with a newly reviewed
exact fingerprint, coherent backup, non-target invariants and explicit human
approval. Unknown future fingerprints are not approved by this migration.
