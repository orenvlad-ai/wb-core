# Warehouse Functional Cutover

## Status

Active guarded migration contract for `warehouse_functional_cutover_v1`. Module truth is `docs/modules/48_MODULE__WAREHOUSE_STOCKS_BLOCK.md`. The prior `warehouse_opening_v1` and its six quantity-only documents remain immutable audit evidence.

## Authorized bounded production mutation

The apply may create only derived functional warehouse versions/balances/documents, frozen opening-cost map, historical and post-cutover versioned WB daily-cost projection, operational sync/certification/replay state and initial calculation-parameter version. It must not alter supplier invoices/payments, CNY ledger, supplier shipment sources, customs/financial sources, append-only FF ledger, WB supplies/reports/stocks raw evidence or nomenclature.

## Execution contract

1. Deploy the exact reviewed merge SHA while the new hourly timer remains disabled.
2. Run `warehouse-functional-dry-run --output <absolute plan.json>` through the repo-owned hosted runner. On a disposable coherent SQLite copy, refresh and completeness-check official supply state, materialize only its downstream components and capture an uncached complete WB snapshot before calculation; production primary rows remain untouched.
3. Review six-stage quantity/WAC/capital/quality totals, source coverage, frozen 24.06 map, historical cost projection, watermarks/digests, zero discrepancy opening and non-negative/no-double-count invariants.
4. Apply the exact plan/fingerprint. The runner must make another uncached official snapshot, optimistically recheck business source revisions (excluding repo-owned supply capture timestamps while retaining status/quantity/goods changes), preflight filesystem capacity, create a coherent `0600` SQLite backup with `integrity_check=ok`, and publish all derived state plus initial settings inside one `BEGIN IMMEDIATE` transaction. A failed backup attempt removes only its own incomplete destination and sidecars. A pre-existing invalid partial may be removed only by the hosted exact-fingerprint cleanup contour, which proves the file is below the functional backup directory, hashes its exact stat/content, rejects coherent SQLite/live DB and retains a private audit manifest.
5. Require protected readback reconciliation. Repeat exact apply; it must return idempotent/no-op with no duplicate document/movement/capital.
6. Run `warehouse-functional-economics-dry-run --output <absolute plan.json>`, review its target cells and non-target digest, then apply the exact plan with `warehouse-functional-economics-apply --plan-file <same plan.json> --fingerprint <exact sha256:...>`. The backfill requires exact `DATA_VITRINA` header/date alignment and indexes only stable `scope|metric` projection rows; legacy presentation-only rows are preserved, while duplicate stable keys fail closed with snapshot identity. Require an idempotent second dry-run with zero changes. Then run the bounded WB sync. Enable the hourly timer only after both reconcile.
7. Run production UI Flow in a fresh isolated Playwright/Chromium context with screenshots and sanitized report outside Git. Accept UI only when protected APIs, visible values and cutover readback agree.

Any source drift, incomplete WB response, missing positive-quantity cost, negative balance, duplicate physical quantity or failed reconciliation blocks apply/publication and preserves the last good version. A code/UI defect requires a normal recovery PR/deploy iteration.

Each hourly/manual plan pins the exact base active-version id. A concurrent publication makes a stale plan fail closed; retrying an already published exact fingerprint remains idempotent. Closed post-cutover days are rebuilt from versioned official snapshots and signed acceptance/cost events, while current day is provisional and zero-stock SKU retains its last valid WAC.

## Functional boundary

The production execution timestamp is not hardcoded. All source state included in the coherent cutover watermarks/digests is absorbed and is not replayed after activation. Pre-boundary anomalies and old unmatched doprinato remain audit-only. New post-boundary events use stable supplier-flow/WB-supply/SKU identities and factual effective dates.

The opening discrepancy balance is management zero. A later pre-boundary event discovered after cutover cannot create a negative warehouse; it is placed in transitional unmatched audit. Accepted WB supply never adds quantity on top of the official WB contour snapshot.

## Recovery

`warehouse-functional-rollback --fingerprint <exact stored fingerprint>` is the only rollback path. It first disables hourly execution, creates another verified backup and removes only the functional derived state/initial settings when safe. Primary source and migration-102 evidence are invariant.
