# Own Product Capital migration contract

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

The runner builds its candidate from persisted posted CNY operations, supplier fact boundaries, dated expense documents and WB cache rows that already have canonical FF quantity-ledger debit evidence; it does not replay or rewrite those source contours. It makes a coherent SQLite online backup, verifies integrity and private permissions before apply, then opens one `BEGIN IMMEDIATE` transaction on the live database. It rechecks source/target/external fingerprints, writes the complete own-capital event/state contour and only bounded daily rows in place, preserves out-of-range own rows plus the explicit 1C/proxy2/proxy3 digest, verifies the target digest and rolls back the whole scope on any exception. It never replaces the live SQLite file, so WAL/active-reader compatibility and inode identity are preserved. Production dry-run/apply is deliberately not performed while preparing the PR.

## Backfill evidence policy

Only persisted factual payment execution, dated factual logistics/customs/tax/VAT expense documents, confirmed direct-RUB fee rows, `actual_shipment_date`, `actual_ff_acceptance_date`, idempotent FF movement, cumulative actual WB accepted quantity and `Допринято` reconciliation evidence may create historical transitions. CNY fees remain deduplicated through the CNY ledger. Upload time, quote/`К оплате`, planned quantity/date and inferred mutable status are not substitutes. Missing historical evidence remains blank/warning.

WB acceptance evidence is validated atomically before its FF writeoff event: `acceptance_date < writeoff_date` is a blocker and creates no partial event history. Rebuild invariant diagnostics include event identity/type/date/stages/SKU and exact available/requested quantities.

Historical WB rows earlier than the first positive persisted supplier-payment ownership event are outside the WebCore capital contour and are counted as skipped pre-ownership evidence. After that boundary a canonical physical FF debit authorizes the movement source but does not fabricate paid ownership: the historical sent/accepted movement is bounded by paid FF capital actually available for the SKU, and any physical remainder is recorded in deterministic diagnostics. A confirmed bank-fee statement with no direct-RUB rows stays in the already-deduplicated CNY-ledger contour; a malformed direct-RUB row remains fail closed. `Допринято` may close only outstanding rows whose final acceptance date is not later than the reconciliation date; future-layer matching is forbidden. Historical outstanding keeps physical and tracked paid quantities separately: reconciliation consumes the exact physical evidence, moves at most the tracked paid quantity, persists zero-capital untracked consumption in the audit event and blocks atomically only when requested quantity exceeds matching physical outstanding. A row with no matching outstanding at all remains outside the paid-capital contour.

## Rollback

Code rollback leaves additive tables dormant and leaves 1C/proxy metrics untouched. If an apply damages the runtime database, restore only the runner-verified coherent backup through the canonical runtime procedure; do not issue ad-hoc SQL or delete audit/event history manually.
