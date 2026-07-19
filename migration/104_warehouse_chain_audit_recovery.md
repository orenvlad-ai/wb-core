# Warehouse Chain Audit Recovery

## Status

Guarded recovery contract after `warehouse_functional_cutover_v1`. Module truth remains `docs/modules/40_MODULE__OUR_WB_COST_MODEL_BLOCK.md`, `docs/modules/45_MODULE__OWN_PRODUCT_CAPITAL_BLOCK.md` and `docs/modules/48_MODULE__WAREHOUSE_STOCKS_BLOCK.md`.

## Confirmed pre-change causes

- Four numeric production labels were valid WB `nmID` values, but warehouse presentation read only registry `config_v2` and omitted exact active server nomenclature.
- 17–18 July ready snapshots existed as exact period columns, while historical cost selection used only outer `snapshot.as_of_date`; WB cost and both Proxy 3 rows therefore remained NULL on those dates.
- Hourly publication reused the same unmatched audit primary key across functional versions; a later good candidate failed on the unique constraint and left the prior version active.
- Old product-capital/1C/confirmed-share definitions remained public beside the functional block; old total WAC also divided capital by paid-equivalent quantity instead of physical quantity.
- Internal quality tokens and raw provenance JSON were the primary warehouse presentation; action buttons used light backgrounds inconsistent with the site theme.

## Derived-only correction scope

The recovery may change only code, derived functional versions, ready-snapshot materializations and versioned UI/read contracts. It must not update invoice/payment/CNY source records, supplier shipments, financial source documents, append-only FF ledger, WB supplies or official WB raw snapshots.

The functional rebuild:

1. merges historical WB quantity only from an exact `stock_total` date column, with canonical daily state taking priority;
2. resolves warehouse names/barcodes by unambiguous active nomenclature `nm_id`;
3. scopes unmatched audit ids by functional version;
4. reports a complete calendar and separates missing-date gaps from positive-quantity cost gaps;
5. preserves all six-stage Decimal quantity/capital/WAC identities.

The economics cutover updates only canonical WB cost and Proxy 3 targets and removes centrally enumerated archived metric rows from all persisted ready snapshots, including archive-only snapshots entirely before 2026-07-01. The canonical rollback/fallback reader also preserves the public physical denominator for SKU/stage WAC; paid-equivalent quantity cannot re-enter active arithmetic. The functional cutover freezes the exact pre-cutover WB daily-cost rows: normal later hourly rebuilds do not read mutable ready snapshots for those dates, verify the frozen rows byte-for-semantic-byte against the reviewed plan, and write only the cutover date and later.

If the posted cutover itself omitted whole dates, an emergency plan may create one versioned append-only correction. Ready snapshots enter capture only while that frozen-calendar gap exists; an ordinary emergency rebuild with complete history has no dependency on mutable publication snapshots. The correction loader selects every persisted bundle whose `date_columns` explicitly contains a missing date, even when its outer `as_of_date` is post-cutover, while the ordinary hourly source scan remains cutover-bounded. Correction pins and persists a normalized manifest plus digest of only the selected exact date/SKU/quantity columns and source identity; unrelated ready-snapshot rows or metadata are excluded from drift so economics/legacy-metric publication cannot invalidate unchanged arithmetic. It rebuilds the full pre-cutover arithmetic from immutable frozen overlap quantities plus exact snapshot quantities only for missing dates, and must match every existing frozen identity/quantity/WAC/capital before selecting only missing dates. For each corrected date it selects one newest coherent exact `stock_total` column, rejects any blank/invalid/duplicate SKU cell, defines completeness as the union of SKU scopes declared by every persisted candidate for that exact date, and requires the projected SKU identity set to match the column exactly; a later scope loss cannot replace an older complete column and values from different snapshot versions are never stitched per SKU. The plan and audit row retain `correction_id`, exact-column source manifest, row fingerprints and `supersedes` links. Apply re-derives the complete correction metadata and exact row set from current persisted evidence both before backup and again under `BEGIN IMMEDIATE`; any extra row, already-populated date or mismatch fails closed before insert. It then creates a fresh coherent `0600` SQLite backup with `integrity_check=ok`; if no transaction commits, that attempt removes only its own backup. A committed correction inserts missing keys without upsert and commits correction audit, daily rows and the new functional version atomically; a collision fails closed and an exact repeat is a no-op. Existing frozen rows and all primary records remain unchanged. Because the full backup/apply can exceed the public proxy timeout, UI is dry-run-only and the exact mutation/readback stays in the repo-owned runner.

Economics non-target comparison canonicalizes absence of `metadata` and an empty metadata object as the same state after removing only owned markers/timestamps; any real mismatch reports the exact bundle/date and both digests. A snapshot with neither target dates nor archived rows/metadata is preserved byte-for-byte and never queued only because of JSON normalization. Non-target snapshot digest equality, exact ready-snapshot manifest, coherent backup, one atomic transaction and a zero-change repeat are mandatory.

## Acceptance

Production readback must prove no negative balances, positive-quantity cost gaps, missing dates from 2026-07-01, double count or changed primary-source digest. Authenticated isolated Playwright must prove all six warehouses, four recovered production identities, human-readable evidence, FF control `391662965`, zero FF→WB, bank-fee aggregate, canonical metric block, exact 17–18 July WB cost/Proxy cells, absence of archived rows and no 5xx/pageerror/console/fatal surface before `/wb-core loop accept-ui <PR>`.

Run these immutable recovery controls explicitly with `warehouse-ui-flow --acceptance-profile warehouse_chain_recovery_20260719 --evidence-dir <absolute path outside repo>`. The reusable default Flow reconciles current readback and does not freeze mutable production quantities for later releases.
