# Supplier historical status reconciliation

This migration is schema-only on deploy. Runtime schema initialization adds nullable-compatible header column `historical_status_exception` with default empty value and creates append-only audit table `sheet_vitrina_v1_supplier_shipment_historical_status_events`. Existing shipment rows and business data are not rewritten.

The only supported code is `legacy_ff_accepted_without_date`. Activation or reversal is available exclusively through `apps/supplier_shipment_factual_date_correction.py` as part of an exact dry-run/apply package. The runner requires the target shipment identity, exact invoice identity, reason, provenance, actor, current-state expectation and optional evidence fingerprint; it builds a coherent disposable candidate, performs the bounded canonical rebuild twice, records before/after and non-target digests, and returns one exact apply fingerprint. Apply additionally requires an explicit verified backup directory and the unchanged fingerprint.

The signal is terminal status evidence, not a factual date or inventory movement. It leaves `actual_shipment_date` and `actual_ff_acceptance_date` unchanged, does not call FF receipt/cost-layer materialization, and removes only the named shipment from supplier production/WIP projections. Existing FF ledger, stock, cost layers and downstream WB evidence remain authoritative. A repeat activation is zero-change. Reversal restores the prior exception value through another exact audited event and the same backup/restore path.

Production business activation is deliberately absent from this migration and remains behind the final human approval of the complete current dry-run fingerprint. Wildcards, force, partial rebuilds, fabricated dates, manual SQL and server-only changes are unsupported.

## Stable approval and collateral guard

The reconciliation v3 approval fingerprint is semantic. It binds exact target headers and lines, historical evidence, the bounded SKU dependency closure, canonical/anomaly policy results, target before/after, candidate canonical output and accounting reconciliation. A mutable absolute snapshot of unrelated WB, 1C, ready-snapshot or other live rows is not part of that human fingerprint.

Non-target protection remains fail-closed as a separate apply-time contract. The candidate must leave protected collateral unchanged. Apply acquires `BEGIN IMMEDIATE`, hashes all protected non-target source rows plus canonical rows outside the target SKU closure immediately before and after the approved writes, and rolls back unless the two digests match. Relevant target/evidence/canonical drift still changes the semantic fingerprint; unrelated activity before the transaction does not create an approval loop. Post-commit verification still requires SQLite integrity, immutable pre-cutover history and a zero-change canonical rebuild, with verified in-place restore on failure.
