# Warehouse Opening Snapshot

## Status

Active migration contract for the one-time `warehouse_opening_v1` initialization. Module truth is in `docs/modules/48_MODULE__WAREHOUSE_STOCKS_BLOCK.md`.

## Allowed Mutation

The only production business mutation authorized by this migration is insertion of:

- one row in `sheet_vitrina_v1_warehouse_cutovers`;
- exactly six rows in `sheet_vitrina_v1_warehouse_documents`;
- the corresponding non-zero SKU rows in `sheet_vitrina_v1_warehouse_document_lines`.

Schema creation for these new tables is part of deploy/startup/read-model initialization. Supplier shipments/invoices, CNY documents/ledger, FF ledger, WB supply cache, WB official stock source, nomenclature, ready snapshots, canonical cost tables and all economic projections are non-target and read-only.

## Schema

`sheet_vitrina_v1_warehouse_cutovers` owns the stable `warehouse_opening_v1` identity, common logical timestamp, posted status, JSON source watermarks, unique exact plan fingerprint and sanitized apply audit.

`sheet_vitrina_v1_warehouse_documents` owns stable ids/numbers, one row per canonical warehouse/type/cutover, nullable movement endpoints, source basis/watermark, exact text quantity, nullable economics and quantity-only status.

`sheet_vitrina_v1_warehouse_document_lines` owns stable line id, document FK, canonical nmID/display identity, exact text quantity, nullable economics and JSON source-record provenance. `UNIQUE(document_id,nm_id)` prevents duplicate SKU balance rows; FK cascade makes the bounded rollback complete.

There is no mutable warehouse balance table. Initial balance is the sum of posted opening-document lines. After cutover, the unified `Склад FF` balance remains the live read projection of the pre-existing canonical FF ledger; later FF operations do not mutate the frozen opening document.

## Required Execution Sequence

1. Merge/deploy the reviewed release commit through GitHub Release Train.
2. Confirm deploy SHA/runtime/service/public probes.
3. Run `warehouse-opening-dry-run --output <absolute local JSON>` through `apps/registry_upload_http_entrypoint_hosted_runtime.py`.
4. Review all six document totals, line provenance, local source digest and WB API `fetched_at`/coverage/digest. Any untraceable line, missing composition, incomplete WB coverage or negative WB acceptance discrepancy is a blocker.
5. Apply the unmodified JSON with its exact `sha256:` fingerprint through `warehouse-opening-apply`. The wrapper pins active EU target/runtime/backup paths.
6. Read back through `warehouse-opening-readback`; require six unique documents/warehouses, one cutover, exact line/header totals and all costs/capital NULL.
7. Re-run apply with the same plan if an idempotency proof is required; it must report `idempotent=true` and create no rows/backup.
8. Compare readback totals/provenance to the reviewed plan/source watermarks, then run the repo-owned `warehouse-ui-flow --evidence-dir <absolute path outside repo>`. Require six protected-detail-API-reconciled warehouse renders, opening documents reconciled with readback, current unified/legacy FF parity, expanded opening-document lines, the legacy FF transition, no unexpected `5xx`/`pageerror`/console errors, and visually inspect the retained screenshots before LOOP UI acceptance.

Apply creates an integrity-checked coherent SQLite backup before `BEGIN IMMEDIATE`. Local sources are re-digested after dry-run and once more through the same connection under the acquired write lock immediately before insertion. Cutover/header/line insertion plus reconciliation is one transaction; injected/real failure leaves no partial documents and the same exact plan can then resume without duplicates when source evidence is unchanged.

## Recovery

If apply fails before commit, rebuild a new plan only after diagnosing the changed/invalid source. Do not reuse a stale fingerprint.

If a committed cutover must be removed before acceptance, `warehouse-opening-rollback --fingerprint <exact stored fingerprint>` makes a second coherent backup and removes only this cutover/documents/lines. Source records remain untouched. A corrected attempt requires a new reviewed plan; code/UI defects require a recovery PR and normal Release Train/deploy/UI cycle.

## Non-Scope

No cost/capital backfill, future warehouse movement automation, historical movement reconstruction, WB sales depletion, returns/writeoffs, FF inventory, vitrina/Proxy 3/canonical cost switch, SKU-management change or unrelated production cleanup is authorized here.
