# Migration 114 — targeted warehouse and cost recovery

## Status

Repository contract implemented; production apply is permitted only through the reviewed repo-owned runner after deploy and a fresh exact dry-run.

## Root causes replaced

- Routine factual-date correction copied, integrity-checked and hashed the whole monolithic SQLite database before target classification. The unrelated Finance raw table dominated about 9.9 GB of an 11.44 GB store.
- Legacy source-anomaly preflight was global. Its chronology could place a dependent WB outbound before a supplier receipt created in the same second, and unrelated anomaly-budget excess blocked one local shipment.
- Factual correction did not hold the warehouse publication lock for the complete critical interval, so the hourly writer could surface `database is locked`.
- FF movement incorrectly used missing downstream cost as a reservation gate.
- Bank statement confirmation modeled rows independently, so one logical fee split across atomic debits had no compact, explicit selection boundary.

## Current contract

`packages/application/warehouse_targeted_replay.py` plans one supplier shipment in SQLite query-only mode. Its fingerprint includes the exact header/documents/CNY/expense revision, affected nmIDs, earliest business date, active version, target rows and non-target digest. Finance raw rows are excluded. Apply performs a capacity check, acquires the shared re-entrant warehouse lock, rechecks the plan, and in one transaction updates the header, publishes the successor functional version, completes the coalesced queue revision and stores exact before-image rollback/audit diagnostics. No routine full-DB backup, SHA or integrity scan is allowed.

Every FF replay uses one chronology helper. Immutable ingestion time is stable; when receipt and dependent outbound share a timestamp, supplier receipt is ordered first. Unrelated global anomalies remain diagnostic and cannot gate the target closure.

Physical movement and cost freshness are independent. Confirmed exact composition plus sufficient FF quantity creates one idempotent debit. Only shortage or identity/composition ambiguity may reserve. Known capital follows quantity; unavailable add-ons remain null/preliminary with reasons. Late costs rebuild only dependent cost history and never move quantity again.

Expense allocation is arithmetic-only and remains fully allocated while a newer cost revision is pending. The separate cost-freshness projection reads the shipment-scoped queue and active certification and exposes preliminary, waiting, recalculating, current-certified, replay-error or unavailable states without changing the allocation label.

Transit states preserve positive, confirmed zero, not-requested, updating, not-found, source-error, session-expired, pending replay, included and replay-error semantics. Unknown/error never becomes zero.

Whole-box correction uses per-SKU factory box size. Only one final minimum whole-box solution can apply. Gross cross-SKU evidence remains separate before correction. Apply and rollback use append-only FF compensation and exact manifest digests. Subsequent functional plan and optimistic apply gates both fingerprint the same corrected supply view plus its correction identity; the gate never compares a corrected plan to the uncorrected persisted raw-goods digest.

Bank import separates payment anchors, logical fees and atomic bank rows. One logical fee may contain several atomic debits. VAT follows purpose semantics. The confirmation UI lists only new logical groups, starts with every checkbox clear, discloses atomic rows and keeps imported/conflict/weak/ignored evidence in collapsed non-selectable blocks. Server target-revision drift rejects confirmation.

## Unified production runner

`apps/warehouse_cost_unified_recovery.py` is dry-run by default. Apply requires the exact current fingerprint and uses the shared warehouse lock. Scope includes one supplier shipment/invoice/date, one statement, explicit commission atomic amounts/counts/total, explicit WB supplies and optional final unique box correction. The dry-run reports exact identities, before/after FF projections, active version/queue and bounded I/O with `copy_bytes=0` and `finance_raw_rows_read=0`.

Bank-statement matching uses the same payment-anchor source as the operator flow, including linked supplier-payment documents in the CNY ledger. Those target-scoped CNY payment revisions participate in the stale-preview fingerprint, so changing an anchor after preview is rejected before any recovery write.

Before the first write the runner persists the reviewed plan in a durable audit journal. Bank, box, physical, factual-date, functional and economics steps checkpoint independently. A crash between mutation and checkpoint is recovered by rerunning the same fingerprint: every step has deterministic identities and re-entry is a no-op or resumes after exact scope/source-revision validation. Completion requires post-apply readback to be a no-op. The legacy standalone 26GN527 apply is disabled.

Audit writes use an explicit `BEGIN IMMEDIATE` with a bounded 300-second SQLite writer wait in addition to the warehouse publication lock. This covers short unrelated writers that still share the monolithic operational database without exposing raw `database is locked`; exhaustion reports the bounded `unified_recovery_sqlite_write_wait_expired` reason. The accumulated wait is published as `sqlite_lock_wait_ms`, and a concurrent-writer smoke proves that a checkpoint waits and then commits without duplicating a durable step.

The economics checkpoint is pinned to the same affected nmID closure and the earliest selected business date. It may update those SKU/date cells and their direct `TOTAL` consumers only; unrelated SKU cells must already reconcile to their current canonical inputs or the target run fails locally. Its exact non-target digest and snapshot before-images prove that unrelated ready-snapshot content is not rewritten.

The former transit/reservation entrypoint cannot build its monolithic snapshot even in CLI diagnostic mode and its imported apply helper fails closed. It only points operators to the canonical targeted runner.

## Verification

- `python3 apps/warehouse_targeted_replay_smoke.py`
- `python3 apps/warehouse_cost_unified_recovery_smoke.py`
- `python3 apps/wb_supply_box_correction_smoke.py`
- `python3 apps/ff_stock_reservation_smoke.py`
- `python3 apps/our_wb_costs_smoke.py`
- `python3 apps/supplier_expense_allocation_smoke.py`
- `python3 apps/supplier_financial_documents_smoke.py`
- `python3 apps/warehouse_functional_smoke.py`
- `python3 apps/sheet_vitrina_v1_supplier_shipments_browser_smoke.py`

Production acceptance additionally requires a fresh query-only baseline, exact dry-run/apply fingerprint, readback totals and identities, repeated no-op, production-scale trace proving no Finance-raw/full-DB traversal, and an isolated Playwright UI flow before LOOP acceptance.
