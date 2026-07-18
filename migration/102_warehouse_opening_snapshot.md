# Warehouse Opening Snapshot

## Status

Completed immutable audit contract for the former one-time quantity-only `warehouse_opening_v1` initialization. It is not active warehouse/cost truth and is never altered or summed with `warehouse_functional_cutover_v1`. Active module truth is in `docs/modules/48_MODULE__WAREHOUSE_STOCKS_BLOCK.md`; active migration is `migration/103_warehouse_functional_cutover.md`.

## Allowed Mutation

The historical production business mutation authorized by this completed migration was insertion of:

- one row in `sheet_vitrina_v1_warehouse_cutovers`;
- exactly six rows in `sheet_vitrina_v1_warehouse_documents`;
- the corresponding non-zero SKU rows in `sheet_vitrina_v1_warehouse_document_lines`.

Schema creation for these new tables is part of deploy/startup/read-model initialization. Supplier shipments/invoices, CNY documents/ledger, FF ledger, WB supply cache, WB official stock source, nomenclature, ready snapshots, canonical cost tables and all economic projections are non-target and read-only.

## Schema

`sheet_vitrina_v1_warehouse_cutovers` owns the stable `warehouse_opening_v1` identity, common logical timestamp, posted status, JSON source watermarks, unique exact plan fingerprint and sanitized apply audit.

`sheet_vitrina_v1_warehouse_documents` owns stable ids/numbers, one row per canonical warehouse/type/cutover, nullable movement endpoints, source basis/watermark, document-level JSON provenance, exact text quantity, nullable economics and quantity-only status.

`sheet_vitrina_v1_warehouse_document_lines` owns stable line id, document FK, canonical nmID/display identity, exact text quantity, nullable economics and JSON source-record provenance. `UNIQUE(document_id,nm_id)` prevents duplicate SKU balance rows; FK cascade makes the bounded rollback complete.

These rows remain immutable evidence. Their NULL costs/capital and quantity totals are not active balances. The active functional version owns current six-stage quantities/economics; FF still reconciles to the pre-existing append-only ledger.

## Historical Execution Evidence

The following sequence documents how the immutable opening was created; it must not be re-run as an active initialization:

1. Merge/deploy the reviewed release commit through GitHub Release Train.
2. Confirm deploy SHA/runtime/service/public probes.
3. Run `warehouse-opening-dry-run --output <absolute local JSON>` through `apps/registry_upload_http_entrypoint_hosted_runtime.py`.
4. Review all six document totals, document/line provenance, local material-source digest and WB API `fetched_at`/coverage/digest. Any untraceable material line, missing material composition, incomplete WB coverage or negative quantity in a material opening source is a blocker. The discrepancy document must show `sku_count=0`, `total_quantity=0`, no lines and `opening_policy=zero_at_cutover`.
5. Apply the unmodified JSON with its exact `sha256:` fingerprint through `warehouse-opening-apply`. The wrapper pins active EU target/runtime/backup paths.
6. Read back through `warehouse-opening-readback`; require six unique documents/warehouses, one cutover, exact line/header totals and all costs/capital NULL.
7. Re-run apply with the same plan if an idempotency proof is required; it must report `idempotent=true` and create no rows/backup.
8. Compare readback totals/provenance to the reviewed plan/source watermarks, then run the repo-owned `warehouse-ui-flow --evidence-dir <absolute path outside repo>`. Require six protected-detail-API-reconciled warehouse renders, opening documents reconciled with readback, current unified/legacy FF parity, expanded opening-document lines, the legacy FF transition, no unexpected `5xx`/`pageerror`/console errors, and visually inspect the retained screenshots before LOOP UI acceptance.

Apply creates an integrity-checked coherent SQLite backup before `BEGIN IMMEDIATE`. Local sources are re-digested after dry-run and once more through the same connection under the acquired write lock immediately before insertion. Cutover/header/line insertion plus reconciliation is one transaction; injected/real failure leaves no partial documents and the same exact plan can then resume without duplicates when source evidence is unchanged. The hosted wrapper keeps read-only actions at a 300-second timeout and gives apply/recovery rollback a bounded 1800-second window solely for the mandatory coherent production backup, integrity check and digest; none of the mutation gates are bypassed.

`warehouse-opening-diagnostic --nm-id <nmID>` remains an optional bounded read-only investigation tool for historical ordinary-final/doprinato arithmetic. It parses the hosted dotenv file without shell evaluation and reports sanitized selected-SKU rows and a diagnostic-only digest. It never participates in dry-run/apply, never enters the opening fingerprint, does not sync/backfill/mutate the WB cache and cannot block the `zero_at_cutover` discrepancy opening.

## Recovery Boundary

If apply fails before commit, rebuild a new plan only after diagnosing the changed/invalid source. Do not reuse a stale fingerprint.

The historical cutover has been accepted and must not be rolled back or edited. Recovery of the active contour uses only the functional repo-owned runner and migration 103; it leaves all migration-102 rows untouched.

## Non-Scope

No active cost/capital, movement automation, functional cutover, Proxy settings/backfill or current warehouse mutation is authorized by migration 102.
