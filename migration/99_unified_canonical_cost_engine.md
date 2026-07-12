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
2. for nmID `497415593` and `497416931`, the explicit business decision takes precedence over 1C and uses `business_approved_primary_wac_fallback` derived from the exact current primary layer weighted FF cost, with decision date, both approved nmIDs, primary shipment/layer, method and reason provenance; coverage is full but confirmation is zero;
3. for every other absent SKU, nearest earlier ready snapshot metric `onec_FF_STOCK_unit_cost_rub`, strictly `<= 2026-05-16`, with bundle/date/metric provenance;
4. no fallback: whole baseline blocked.

Coverage must be 100%. No general estimated fallback exists. Future shipment, `near_future_proxy`, WB-stage 1C cost, post-cutoff 1C, zero and hidden last-known costs are forbidden.

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

`CUTOVER_IMMATERIAL_ANOMALY_POLICY_V1` is a reproducible one-time opening-boundary policy, not a future runtime tolerance. An anomaly is eligible only when it is an integer discrepancy with exact SKU/source/checkpoint/business-date identity, a positive permitted baseline cost, business date strictly before cutover, no more than 3 units per SKU line, 5 per operation and 20 over the whole cutover. The aggregate count/operations/SKUs, gross/net quantity, recognized/paid exposure and remaining budget are part of the candidate fingerprint.

For eligible `accepted > sent`, movement applies `min(sent, accepted)` and exposes raw/applied/surplus in internal audit; surplus is already absorbed by official WB opening and creates no movement, cost or capital and cannot close another SKU. Eligible unmatched pre-cutover `Допринято` and an eligible small pre-cutover FF replay residual likewise remain audit-only. Post-cutover anomalies, missing/ambiguous identity/date/cost, nonpositive cost, cross-SKU redistribution, duplicate operation/closure, negative current balance, digest drift and any limit breach remain fail-closed. No anomaly/surplus user metric is added.

The FF activation receipt is projected at the cutover opening boundary. Exact checkpoint writeoffs and their linked `runtime_repair` compensations remain physical audit history rather than being replayed twice; their net current ledger evidence must reconcile exactly. The explicit persisted `targeted_pre_activation_remediation` reason keeps `40561872` as a real post-cutover debit even though its source identity is checkpoint-matched; it retains its authoritative effective date and ordinary physical/WAC effect.

FF operation business dates use one canonical resolver. Supplier-shipment receipts retain
`actual_ff_acceptance_date`. WB auto-writeoffs use a valid persisted operation source timestamp
(the bounded targeted-runner `supply_timestamp` key is accepted as equivalent legacy provenance)
or require an exact persisted WB supply matched by source object plus source key and resolve its
factual acceptance/fact date, falling back to its supply business date only when no factual date
exists. `operation.created_at` is not a WB business-date fallback. Missing, ambiguous or conflicting
supply identity, invalid timestamps and absent authoritative business dates block the candidate.
The dry-run audit lists every WB auto-writeoff without the ordinary source timestamp together with
its field-level provenance, checkpoint membership, sent/accepted quantities and cutover class.

## Runner safety

`apps/canonical_cost_engine_backfill.py` is the only apply-capable path. It:

- defaults to dry-run and requires exact `2026-07-01..current` scope;
- runs the exhaustive source audit before baseline materialization/heavy replay and blocks the candidate if any anomaly is unresolved;
- materializes a coherent SQLite backup candidate and verifies `PRAGMA integrity_check=ok`;
- reports a stable fingerprint, stage/capital/coverage reconciliation, affected Finance weeks and source/protected/pre-cutover digests;
- when baseline coverage is incomplete, returns a stable `status=blocked`
  dry-run report with exact primary shipment, fallback provenance, physical
  stages and missing/conflicting SKUs; a blocked fingerprint can never apply;
- requires exact current fingerprint plus explicit backup directory for apply;
- creates a `0600` online backup;
- uses `BEGIN IMMEDIATE`, optimistic source/target digest recheck and in-place row replacement;
- preserves SQLite inode/WAL readers; never uses `os.replace`, force or partial mode;
- rolls back on transactional drift/mismatch; any post-commit integrity/idempotency
  failure triggers an in-place SQLite online restore from the verified backup;
- verifies source/non-target/pre-cutover digests after apply;
- requires second run with zero changes.

`apps/canonical_cost_engine_preflight.py` is the read-only diagnostic entrypoint. It takes a coherent SQLite copy, reports all supported anomaly classes in one pass, verifies live inode/integrity and source/protected/pre-cutover digests, and performs no production mutation.

Production apply is forbidden until a human explicitly approves the exact dry-run fingerprint and backup plan.
