# Partner marketing diagnostic and ads historical recovery

## Scope

This migration is the evidence/safety stage of LOOP root #731 after the Partner production acceptance failed closed. It does not change the Partner formula. It adds the canonical means to prove the raw composition first and to prepare an independently reviewed repair of missing accepted `ads_compact` source slots.

## Read-only reconciliation

`apps/partner_finance_production_diagnostic.py` restores the exact Partner `nmId` and selected Finance weeks from server-owned settings unless an explicit bounded scope is supplied. It reads one coherent SQLite snapshot with `mode=ro`, `query_only` and rollback. For each week it reports ads, direct/account Finance marketing, allocated marketing, current Partner `other_withholdings`, the residual without allocated marketing, and a raw-component reconciliation delta.

The production history scan is ordered and streaming per week. Only aggregated groups, bounded examples and fail-closed maxima of 10,000 operation groups, 10,000 marketing-name candidates and 10,000 anomalous stored/raw identity keys are retained; invalid raw JSON is represented by one exact-count blocker plus bounded examples, and a second duplicate pass is conditional on an identity anomaly. A week missing a projection still contributes its raw count, digest, invalid-JSON and identity evidence. The diagnostic does not build the global Finance capitalization map: account-level rows in scope are unresolved by definition and cannot match a canonical nmId/supply layer, while direct acceptance/transit never contributes to current Partner Other. `apps/partner_finance_production_diagnostic_scale_smoke.py` covers 300,013 rows plus group/candidate overflow and 12,000 invalid-JSON rows, enforces a 768 MiB child peak-RSS ceiling and proves that the runtime SQLite bytes remain unchanged. This is the recovery for the first deployed diagnostic attempt, whose process was OOM-killed after materializing 2,414,082 parsed rows at approximately 7.54 GiB anonymous RSS; the OOM attempt performed no data mutation.

Operation evidence is grouped by `bonusTypeName`, `sellerOperName`, `paymentProcessing`, `docTypeName`, `nmId` presence, deduction sign, Finance classifier, and direct/allocated accounting. It retains signed, current-system and allocated sums, coefficients, bounded row identifiers and semantic Partner targets. Duplicate raw identities, stored/raw identity mismatch, marketing-like names classified as other, and `abs(negative deduction)` uplift are separate evidence sections. The output contains digests, not secrets or the database path.

## Missing ads source slots

`apps/ads_historical_recovery.py` owns only an exact reviewed list of absent accepted closed-day slots. Official `/adv/v3/fullstats` is queried within its current statuses/window/batch/rate contract. Existing snapshots cannot be replaced. Incomplete responses and a non-empty response without the selected `nmId` block the plan; a zero is accepted only as a complete global `kind=empty`, never synthesized for one SKU.

Dry-run emits the exact scope, source/target/non-target manifests, write set, blockers, backup contract and fingerprint. Apply is impossible without a fresh matching plan, explicit human approval reference, coherent verified `0600` backup and canonical write lock. Transactional and post-commit readback prove every inserted payload/closure plus unchanged non-target state; retry of an already audited fingerprint is a no-op.

## Closure boundary

Deploying these runners authorizes only read-only production reconciliation and ads dry-run. Actual ads apply remains a separate human-gated production-data mutation. Evidence from this stage determines the following same-root recovery formula/classifier/UI/XLSX change; no diagnosis is accepted from code inference alone.
