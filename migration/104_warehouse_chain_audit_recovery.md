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

The economics cutover updates only canonical WB cost and Proxy 3 targets and removes centrally enumerated archived metric rows from all persisted ready snapshots, including archive-only snapshots entirely before 2026-07-01. The canonical rollback/fallback reader also preserves the public physical denominator for SKU/stage WAC; paid-equivalent quantity cannot re-enter active arithmetic. The functional cutover freezes the exact pre-cutover WB daily-cost rows: later hourly/emergency rebuilds do not read mutable ready snapshots for those dates, verify the frozen rows byte-for-semantic-byte against the reviewed plan, and write only the cutover date and later. Non-target snapshot digest equality, exact ready-snapshot manifest, coherent `0600` SQLite backup, `integrity_check=ok`, one atomic transaction and a zero-change repeat are mandatory.

## Acceptance

Production readback must prove no negative balances, positive-quantity cost gaps, missing dates from 2026-07-01, double count or changed primary-source digest. Authenticated isolated Playwright must prove all six warehouses, four recovered production identities, human-readable evidence, FF control `391662965`, zero FF→WB, bank-fee aggregate, canonical metric block, exact 17–18 July WB cost/Proxy cells, absence of archived rows and no 5xx/pageerror/console/fatal surface before `/wb-core loop accept-ui <PR>`.

Run these immutable recovery controls explicitly with `warehouse-ui-flow --acceptance-profile warehouse_chain_recovery_20260719 --evidence-dir <absolute path outside repo>`. The reusable default Flow reconciles current readback and does not freeze mutable production quantities for later releases.
