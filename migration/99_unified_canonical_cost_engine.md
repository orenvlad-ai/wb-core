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

Every operation whose authoritative business date is before `2026-07-01` is immutable legacy audit-only. It is visited and fingerprinted, but is not replayed into canonical movement, opening quantity/capital or opening underaccepted and does not require a historical movement cost. A SKU which appears only in that legacy movement history and has no opening/post-cutover physical quantity is classified `legacy_cost_not_required_after_cutover`. Official WB opening, the FF opening/replay contract, supplier registry and approved baseline costs own the cutover state.

`CUTOVER_POSTCUTOVER_SOURCE_NORMALIZATION_V1` is an exact, versioned manifest for persisted composition anomalies dated only `2026-07-02..2026-07-12`; it is not a future runtime tolerance. Each manifest row pins operation/supply/source key, business date, sent and accepted line-set fingerprints and the combined evidence fingerprint. Raw evidence and FF debit quantity/capital remain unchanged. Direct acceptance is `min(sent_sku, accepted_sku)`; only the surplus pool of that same supply may cover its own deterministic shortage pool. The supply weighted recognized/paid cost pool preserves `sent = effective accepted + underaccepted` and `FF debit capital = WB transferred capital + underaccepted capital`; normalized quantity has confirmation `0`. Missing identity/date/cost, aggregate accepted above aggregate sent, cross-supply allocation, future or fingerprint-drifted evidence remains fail-closed. No anomaly/surplus user metric is added.

Manifest V1 contains exactly four persisted operations and no wildcard: `ffso_786f3d2533374015af12 / 40422317`, `ffso_14303efbdb04425baf54 / 40436428`, `ffso_9c618c5b5e0d4957b7cf / 40564048`, `ffso_ceec1569093b40aa80d7 / 40559839`. Their full immutable fingerprints live beside the resolver in `packages/application/canonical_cost_engine.py`; any changed line/evidence set or a fifth operation fails closed.

`CUTOVER_UNMATCHED_DOPRINATO_ABSORPTION_V1` is a second, independent exact manifest for the approved set of 10 persisted `Допринято` supplies / 7 SKU / 11 units dated `2026-07-01..2026-07-12`. Each row pins supply/source identity, authoritative date, exact nmID, full persisted warehouse/destination, quantity, final-accepted status, semantic raw row/line fingerprint and an exact positive current-baseline stage recognized/paid cost reference. The manifest fingerprint also pins the human approval date, reason and diagnostic fingerprint. A matched row is classified `unmatched_doprinato_absorbed_by_official_wb_stock` with source quality `exact_unmatched_doprinato_absorbed` before direct/FIFO: it remains raw source/audit evidence but creates no FF receipt/debit, WB movement, physical quantity, recognized/paid capital, confirmation or underaccepted delta and cannot close an unrelated outstanding layer. Official WB stock remains physical truth and its existing canonical SKU WAC continues to value the stock. Missing or changed source/date/SKU/route/quantity/status/fingerprint/cost reference, deletion of an approved source row, or any future unmatched supply remains fail-closed; there is no wildcard or quantity tolerance.

`CUTOVER_UNMATCHED_DOPRINATO_ABSORPTION_V2` is a separate approval and does not alter V1 or its fingerprint. It pins 9 additional exact supply/SKU rows / 12 units across five persisted supplies. Its key is `supply_id + nmID`; each row additionally pins the empty `original_supply_id`, raw persisted supply-row fingerprint, raw goods-line fingerprint, their combined fingerprint, semantic evidence fingerprint, final-accepted status and exact current canonical stage cost. The reference recognized/paid exposure is `1 385.410826 ₽` for audit only. A match has the same official-WB-stock absorption semantics and zero physical/capital/confirmation/underaccepted delta. The shared report exposes both version fingerprints and the combined 19 rows / 23 units, while any unlisted sibling or future row remains a strict blocker.

The backfill and diagnostic reports include `canonical_cost_layer_continuity_v1`. It proves every current movement layer's quantity/coverage/unit-cost capital identity and requires each persisted outstanding child to retain the exact recognized and paid unit costs plus supply/SKU identity of its original immutable FF debit layer. Ordinary WB acceptance transfers that capital proportionally; the bounded composition normalization uses only the same supply weighted pool. Aggregate stage WAC monotonicity is deliberately not an invariant because different stages contain different SKU and lots.

Source preflight and strict replay use the same opening-boundary eligibility for outstanding candidates: checkpoint audit operations cannot satisfy direct/FIFO merely because their raw business date is post-cutover. Diagnostic quarantine is exact to `supply_id + nmID`, retained only in memory on the coherent disposable copy and propagated through later diagnostic movement checks; a blocker on one goods line therefore cannot hide another line of the same supply or alter persisted source evidence.

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

`apps/canonical_cost_engine_preflight.py` remains the fast source-wide audit. `apps/canonical_cost_engine_diagnostic.py` is the exhaustive non-apply collector: it takes a coherent SQLite copy, records stable primary/cascading BlockerRecords, quarantines only inside the disposable model, marks dependent results tainted, runs baseline/component/movement/WAC/daily/read-side/reconciliation/idempotency/integrity branches, emits a full coverage matrix and repeats diagnostic passes until no new unique blocker appears. It verifies the live inode and source/protected/pre-cutover digests and performs no production mutation. Tainted/hypothetical values are never published as canonical results.

Production apply is forbidden until a human explicitly approves the exact dry-run fingerprint and backup plan.
