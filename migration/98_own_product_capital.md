# Own Product Capital migration contract

> Legacy audit contract. The apply-capable successor for the `2026-07-01` cutover is
> `migration/99_unified_canonical_cost_engine.md` and
> `apps/canonical_cost_engine_backfill.py`. This file does not authorize a second
> production baseline or physical inventory truth.

## Scope

Add the independent WebCore management block `Товарный капитал — наши данные`. The additive SQLite schema persists paid ownership layers, dated movements, FF moving-average cost snapshots, WB underacceptance outstanding state, daily SKU/TOTAL materialization, blockers and operator expense-completeness certification. Existing 1C, proxy2 and proxy3 data is not rewritten.

## Repository preparation

Application startup creates the additive tables idempotently. New events are recognized only from deterministic SKU evidence and factual payment/movement dates. Payment document ingestion is parse-preview-first; upload time is never an effective date. Existing source/cache rows may remain evidence while unknown nmID or ambiguous allocation blocks capital movement.

Historical reconstruction is owned only by `apps/own_product_capital_backfill.py`. It defaults to dry-run and requires explicit `--runtime-dir`, `--date-from` and `--date-to`. Apply additionally requires the exact dry-run fingerprint and an explicit backup directory. There is no force or partial mode.

## Future production closure

After merge, the single merge/deploy coordinator must:

1. deploy through the canonical repo-owned runtime path;
2. verify schema/status endpoints and preserve the pre-existing runtime database;
3. run a bounded read-only preflight and default dry-run;
4. review blockers, target/non-target digests and stable fingerprint;
5. verify the coherent backup, then run apply with that fingerprint;
6. verify post-run reconciliation, UI/source metadata and public historical samples;
7. run a second dry-run/apply-equivalent check and require zero changes.

After the daily-state apply, historical web-vitrina publication uses the protected `webcore_product_capital` source-group refresh for each persisted date. When a date exists only in an older registry bundle, that group alone may use the prior-bundle ready snapshot as its merge base and save the merged plan under the current bundle. The merge is additive/date-scoped: it updates only `own_product_capital` metric/status rows and preserves unrelated metric groups, 1C and proxy projections. Other source groups retain the current-bundle-only date guard.

The runner builds its candidate from persisted posted CNY operations, supplier fact boundaries, dated expense documents and WB cache rows that already have canonical FF quantity-ledger debit evidence; it does not replay or rewrite those source contours. It makes a coherent SQLite online backup, verifies integrity and private permissions before apply, then opens one `BEGIN IMMEDIATE` transaction on the live database. It rechecks source/target/external fingerprints, writes the complete own-capital event/state contour and only bounded daily rows in place, preserves out-of-range own rows plus the explicit 1C/proxy2/proxy3 digest, verifies the target digest and rolls back the whole scope on any exception. It never replaces the live SQLite file, so WAL/active-reader compatibility and inode identity are preserved. Production dry-run/apply is deliberately not performed while preparing the PR.

## Backfill evidence policy

Only persisted factual payment execution, dated factual logistics/customs/tax/VAT expense documents, confirmed direct-RUB fee rows, `actual_shipment_date`, `actual_ff_acceptance_date`, idempotent FF movement, cumulative actual WB accepted quantity and `Допринято` reconciliation evidence may create historical transitions. CNY fees remain deduplicated through the CNY ledger. Upload time, quote/`К оплате`, planned quantity/date and inferred mutable status are not substitutes. Missing historical evidence remains blank/warning.

WB acceptance evidence is validated atomically before its FF writeoff event: `acceptance_date < writeoff_date` is a blocker and creates no partial event history. Rebuild invariant diagnostics include event identity/type/date/stages/SKU and exact available/requested quantities.

Historical WB rows earlier than the first positive persisted supplier-payment ownership event are outside the WebCore capital contour and are counted as skipped pre-ownership evidence. After that boundary a canonical physical FF debit authorizes the movement source but does not fabricate paid ownership: the historical sent/accepted movement is bounded by paid FF capital actually available for the SKU, and any physical remainder is recorded in deterministic diagnostics. A confirmed bank-fee statement with no direct-RUB rows stays in the already-deduplicated CNY-ledger contour; a malformed direct-RUB row remains fail closed. `Допринято` may close only outstanding rows whose final acceptance date is not later than the reconciliation date; future-layer matching is forbidden. Historical outstanding keeps physical and tracked paid quantities separately: reconciliation consumes the exact physical evidence, moves at most the tracked paid quantity, persists zero-capital untracked consumption in the audit event and blocks atomically only when requested quantity exceeds matching physical outstanding. A row with no matching outstanding at all remains outside the paid-capital contour.

One business-approved orphan classification is exact and non-generalizable. The runner may continue document `40654176` only when its date is `2026-07-06`, its full composition is `{391660889:1, 391663632:1}`, the orphan line is `391663632 / 1`, warehouse is `Склад Шушары`, upstream `original_supply_id` is absent, FIFO candidate is `40433285`, and tracked/physical/paid outstanding are all zero. It persists event `wb_reconciliation:40654176:historical_orphan:391663632` with reason `historical_orphan_doprinato_zero_capital`, zero quantity/capital/confirmed quantity and no FF writeoff; the `391660889` line still uses ordinary reconciliation. Any guard drift blocks the whole document. This is not force/partial apply and cannot infer a cost layer.

## Rollback

Code rollback leaves additive tables dormant and leaves 1C/proxy metrics untouched. If an apply damages the runtime database, restore only the runner-verified coherent backup through the canonical runtime procedure; do not issue ad-hoc SQL or delete audit/event history manually.
