# Unified canonical cost engine cutover contract

## Scope

- cutover: `2026-07-01`;
- legacy dates remain byte/digest preserved;
- physical sources: supplier registry, `ff_stock_ledger`, persisted WB evidence, official WB stock;
- financial sources: supplier/CNY/payment evidence, factual financial documents, accepted FF services/storage and guarded opening baseline;
- live consumers: product capital, Our WB Cost, proxy3, Finance/P&L.

## Baseline

`CanonicalCostEngine.discover_primary_baseline_shipment()` must find exactly one `accepted_ff` shipment in `2026-06-21..2026-06-24` with at least 100 000 units, full matching, certified expenses, confirmed current FF layer, reconciliation `ok` and weighted FF cost `111.181389 ± 0.01 ₽/шт`. The runner reports exact id/date/quantity/SKU count and blocks otherwise.

Owned SKU priority:

1. primary shipment `sku_ff_unit_cost_rub`;
2. nearest earlier ready snapshot metric `onec_FF_STOCK_unit_cost_rub`, strictly `<= 2026-05-16`, with bundle/date/metric provenance;
3. no fallback: whole baseline blocked.

Coverage must be 100%. Future shipment, `near_future_proxy`, WB-stage 1C cost, post-cutoff 1C, zero and hidden last-known costs are forbidden.

Opening recognized cost covers every physical unit. For production and
production-to-FF rows, paid-equivalent quantity and paid capital are still
allocated only from posted CNY payments effective on or before cutover; the
baseline never upgrades an unpaid opening shipment to fully paid.

## Derived targets

- `sheet_vitrina_v1_canonical_cost_baseline_versions` / `_lines`;
- `sheet_vitrina_v1_canonical_cost_components`;
- `sheet_vitrina_v1_canonical_cost_movement_layers`;
- `sheet_vitrina_v1_canonical_cost_wb_outstanding_layers`;
- `sheet_vitrina_v1_canonical_cost_daily_state`.

Legacy module-40/45 tables remain audit-only. Source tables and pre-cutover rows are never target tables.

## Runner safety

`apps/canonical_cost_engine_backfill.py` is the only apply-capable path. It:

- defaults to dry-run and requires exact `2026-07-01..current` scope;
- materializes a coherent SQLite backup candidate and verifies `PRAGMA integrity_check=ok`;
- reports a stable fingerprint, stage/capital/coverage reconciliation, affected Finance weeks and source/protected/pre-cutover digests;
- requires exact current fingerprint plus explicit backup directory for apply;
- creates a `0600` online backup;
- uses `BEGIN IMMEDIATE`, optimistic source/target digest recheck and in-place row replacement;
- preserves SQLite inode/WAL readers; never uses `os.replace`, force or partial mode;
- rolls back on transactional drift/mismatch; any post-commit integrity/idempotency
  failure triggers an in-place SQLite online restore from the verified backup;
- verifies source/non-target/pre-cutover digests after apply;
- requires second run with zero changes.

Production apply is forbidden until a human explicitly approves the exact dry-run fingerprint and backup plan.
